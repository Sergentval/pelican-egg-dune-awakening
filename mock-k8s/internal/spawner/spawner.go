// Package spawner reconciles ServerSetScale spec.replicas → running UE5
// processes by shelling out to scripts/start-ue5.sh.
//
// This is the only place where mock-k8s actually touches the host: we fork
// start-ue5.sh with the per-instance env (DUNE_PARTITION, GAME_PORT,
// IGW_PORT) the script's preamble expects. start-ue5.sh launches the UE5
// process via lib.sh's launch_bg, which setsid's it and records the pid at
// $BASE/runtime/pids/ue5-<map>-<suffix>.pid. We read that pidfile to learn
// the pid (for state persistence) and to terminate the instance on
// scale-down.
//
// The spawner persists its instance ledger to $BASE/server/state via the
// state package after every change, so Restore can re-adopt still-live UE5
// processes on their original ports after a mock-k8s restart.
package spawner

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/state"
)

const (
	// terminateGrace is how long a UE5 instance gets to exit on SIGTERM
	// during scale-down before mock-k8s escalates to SIGKILL.
	terminateGrace = 15 * time.Second

	// pidWaitTimeout bounds how long capturePID waits for start-ue5.sh to
	// write the instance pidfile after we launch the script.
	pidWaitTimeout = 20 * time.Second

	// pidPollInterval is how often capturePID re-checks for the pidfile.
	pidPollInterval = 250 * time.Millisecond
)

// Spawner ties the ServerSetScale store, the port Pool, and start-ue5.sh
// together.
type Spawner struct {
	store      *serversetscale.Store
	pool       *pool.Pool
	scriptPath string // absolute path to scripts/start-ue5.sh
	baseDir    string // $BASE for the script (also roots runtime/pids)
	statePath  string // on-disk instance ledger (empty disables persistence)

	// reconcileMu serializes OnSpecChange so a single map's
	// read-count → decide → spawn/scale is atomic. Without it, two
	// concurrent reconciles for the same map both observe the stale replica
	// count and over-spawn past spec.replicas, exhausting the port pool.
	reconcileMu sync.Mutex

	mu        sync.Mutex
	instances map[string][]instance // key: namespace/name → instance list

	// saveMu serializes ledger writes so concurrent persist() calls can't
	// interleave file writes.
	saveMu sync.Mutex

	// persistGen is bumped under s.mu on every snapshot; lastSavedGen (under
	// saveMu) is the newest generation committed to disk. Because persist()
	// snapshots under s.mu but writes under saveMu, two calls can win the
	// saveMu race in the opposite order to their snapshots — the guard makes a
	// call skip its write when its generation is no newer than what is already
	// on disk, so a stale snapshot can never clobber a newer ledger.
	persistGen   uint64
	lastSavedGen uint64

	// bg tracks background goroutines (pid capture + scale-down teardown)
	// so tests — and a future graceful shutdown — can wait for them.
	bg sync.WaitGroup

	// terminate is the process-termination seam; defaults to proc.Terminate.
	terminate func(pid int, grace time.Duration) error

	// now is the clock seam; defaults to time.Now. Swappable so backoff timing
	// is deterministic in tests.
	now func() time.Time

	startedAt         time.Time     // process start, for /status uptime
	reconcileInterval time.Duration // 0 when the loop is disabled
	reconcileSweeps   int64         // ticks run
	lastSweep         time.Time     // zero until the first tick
	reapedTotal       int64
	respawnedTotal    int64
	restoredAtBoot    int64
	persistErrors     int64
	lastPersistError  string

	// backoff holds per-map crash-loop state, guarded by s.mu.
	backoff map[string]backoffState
}

type instance struct {
	Suffix      string
	MapName     string
	PartitionID int
	Allocation  pool.Allocation
	PID         int    // 0 until capturePID reads the pidfile
	StartTime   uint64 // /proc start-time of PID; with PID, a reuse-proof identity

	// pidReady is closed by capturePID once the pidfile has been read (or the
	// wait timed out). teardown blocks on it so a scale-down that races a
	// just-started spawn waits for the real pid instead of seeing pid==0 and
	// orphaning the process. nil for restored instances (pid already known).
	pidReady chan struct{}
}

// backoffState tracks crash-loop backoff for one map key.
type backoffState struct {
	failures  int
	nextRetry time.Time // zero when failures <= 1 (retry immediately)
}

// New constructs a Spawner. scriptPath should be absolute (e.g.
// /home/container/scripts/start-ue5.sh). baseDir is passed as $1 to the
// script and roots both runtime/pids and the server/state ledger.
func New(store *serversetscale.Store, pool *pool.Pool, scriptPath, baseDir string) *Spawner {
	return &Spawner{
		store:      store,
		pool:       pool,
		scriptPath: scriptPath,
		baseDir:    baseDir,
		statePath:  filepath.Join(baseDir, "server", "state", "mock-k8s-state.json"),
		instances:  make(map[string][]instance),
		terminate:  proc.Terminate,
		now:        time.Now,
		startedAt:  time.Now(),
		backoff:    make(map[string]backoffState),
	}
}

// Wait blocks until all in-flight background goroutines (pid capture and
// scale-down teardown) finish. Primarily a test seam.
func (s *Spawner) Wait() { s.bg.Wait() }

// Restore re-adopts UE5 instances recorded in the on-disk ledger that are
// still alive, reserving their original port slots so game/IGW ports stay
// stable across a mock-k8s restart. Dead entries are dropped. MUST run
// before the AlwaysWarm pre-spawn so a re-adopted map's OnSpecChange sees
// desired == current and does not double-spawn.
func (s *Spawner) Restore() {
	if s.statePath == "" {
		return
	}
	prev, err := state.Load(s.statePath)
	if err != nil {
		if errors.Is(err, state.ErrCorrupt) {
			// The ledger is present but unparseable. Quarantine it so the
			// operator can inspect it and it stops failing every boot, then
			// start fresh — safe precisely because we KNOW the file is bad.
			corrupt := s.statePath + ".corrupt." + strconv.FormatInt(time.Now().UnixNano(), 10)
			if rnErr := os.Rename(s.statePath, corrupt); rnErr != nil {
				slog.Error("spawner: corrupt state ledger could not be quarantined; starting fresh",
					"path", s.statePath, "err", err, "rename_err", rnErr)
			} else {
				slog.Error("spawner: corrupt state ledger quarantined; starting fresh",
					"path", s.statePath, "quarantined_to", corrupt, "err", err)
			}
		} else {
			// Transient I/O error (EACCES, EIO, …). The ledger may be healthy;
			// do NOT rename it — log and skip restore so the next boot can try
			// again and still re-adopt the instances it records.
			slog.Error("spawner: transient I/O error reading state ledger; skipping restore (ledger preserved)",
				"path", s.statePath, "err", err)
		}
		return
	}
	if len(prev.Instances) == 0 {
		return
	}
	adopted := 0
	for _, pi := range prev.Instances {
		if !safeInstanceName(pi.MapName) || !safeInstanceName(pi.Suffix) {
			slog.Warn("spawner: skipping restored instance with unsafe name",
				"key", pi.Key, "map", pi.MapName, "suffix", pi.Suffix)
			continue
		}
		pidPath := s.pidPath(pi.MapName, pi.Suffix)
		pid := proc.ReadPidFile(pidPath)
		if pid == 0 {
			pid = pi.PID
		}
		// If the ledger recorded a process identity, require it to still match
		// so a recycled pid (original UE5 crashed, pid reused) isn't adopted as
		// live. Old ledgers without a start-time fall back to a plain liveness
		// probe, and we re-establish the identity below for future restarts.
		live := proc.Alive(pid)
		if live && pi.StartTime != 0 {
			live = proc.SameProcess(pid, pi.StartTime)
		}
		if !live {
			slog.Info("spawner: dropping dead or pid-reused instance from prior state",
				"key", pi.Key, "map", pi.MapName, "suffix", pi.Suffix)
			_ = os.Remove(pidPath)
			continue
		}
		startTime := pi.StartTime
		if startTime == 0 {
			startTime, _ = proc.StartTime(pid)
		}
		alloc, err := s.pool.AcquireSpecific(pi.PoolIndex)
		if err != nil {
			slog.Warn("spawner: cannot reserve slot for re-adopted instance; leaving it orphaned",
				"key", pi.Key, "index", pi.PoolIndex, "err", err)
			continue
		}
		s.mu.Lock()
		s.instances[pi.Key] = append(s.instances[pi.Key], instance{
			Suffix:      pi.Suffix,
			MapName:     pi.MapName,
			PartitionID: pi.PartitionID,
			Allocation:  alloc,
			PID:         pid,
			StartTime:   startTime,
		})
		s.mu.Unlock()
		adopted++
		slog.Info("spawner: re-adopted live UE5 across restart",
			"key", pi.Key, "map", pi.MapName, "pid", pid, "game_port", alloc.GamePort)
	}
	slog.Info("spawner: state restore complete", "adopted", adopted, "of", len(prev.Instances))
	s.persist()
}

// OnSpecChange is assigned to store.OnSpecChange. It diffs the desired
// replica count against current instances and starts / stops as needed.
// The read → decide → act is serialized by reconcileMu so concurrent
// reconciles for the same map cannot both act on a stale count.
func (s *Spawner) OnSpecChange(obj serversetscale.Object) {
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	mapName, _ := obj.Spec["mapName"].(string)
	if mapName == "" {
		mapName, _ = obj.Spec["map"].(string) // tolerate alt field name
	}
	if !safeInstanceName(mapName) {
		slog.Error("spawner: refusing ServerSetScale with unsafe map name", "key", key, "map", mapName)
		return
	}
	desired := readReplicas(obj.Spec)

	s.reconcileMu.Lock()
	s.mu.Lock()
	current := len(s.instances[key])
	s.mu.Unlock()
	switch {
	case desired < current:
		s.scaleDown(key, desired)
	case desired > current:
		s.reconcileUpLocked(obj, false) // honor a Director patch immediately
	}
	s.reconcileMu.Unlock()

	// Reflect spec change into status.
	_, _ = s.store.UpdateStatus(obj.Metadata.Namespace, obj.Metadata.Name, map[string]any{
		"observedGeneration": obj.Metadata.Generation,
		"completedReplicas":  int64(desired),
	})
}

// scaleDown removes instances above the desired count and tears each one
// down asynchronously: SIGTERM (then SIGKILL after grace) the UE5 process,
// and only release its port slot once the process is gone — releasing
// earlier would let a concurrent scale-up grab the same UDP port while the
// dying UE5 still holds it. Called with reconcileMu held; the teardown
// goroutines run after it returns (and after reconcileMu is released), so
// the 15s grace never blocks reconciliation.
func (s *Spawner) scaleDown(key string, desired int) {
	s.mu.Lock()
	list := s.instances[key]
	if desired >= len(list) {
		s.mu.Unlock()
		return
	}
	removed := append([]instance(nil), list[desired:]...)
	s.instances[key] = append([]instance(nil), list[:desired]...)
	s.mu.Unlock()

	// Record the reduced ledger immediately; teardown persists again once
	// each slot is actually freed.
	s.persist()

	for _, inst := range removed {
		s.bg.Add(1)
		go func(inst instance) {
			defer s.bg.Done()
			s.teardown(key, inst)
		}(inst)
	}
}

// teardown terminates one instance's UE5 process and releases its slot.
func (s *Spawner) teardown(key string, inst instance) {
	// Wait for the spawn to have recorded its pid (or to have definitively
	// failed) before deciding there's nothing to kill. A scale-down that
	// lands in the spawn window would otherwise see pid==0 and orphan an
	// about-to-start UE5, which later collides on its UDP port (EADDRINUSE).
	if inst.pidReady != nil {
		select {
		case <-inst.pidReady:
		case <-time.After(pidWaitTimeout + 2*time.Second):
		}
	}

	pidPath := s.pidPath(inst.MapName, inst.Suffix)
	pid := proc.ReadPidFile(pidPath)
	if pid == 0 {
		pid = inst.PID
	}
	switch {
	case pid <= 0:
		slog.Warn("spawner: scale-down with no pid (process never started?)", "key", key, "suffix", inst.Suffix)
	case inst.StartTime != 0 && !proc.SameProcess(pid, inst.StartTime):
		// The recorded UE5 exited and this pid may now belong to an unrelated
		// process — and sendSignal also hits the whole process group (-pid).
		// Do NOT signal it; just drop the stale pidfile and free the slot.
		slog.Warn("spawner: recorded UE5 is gone and pid may be reused — not terminating",
			"key", key, "suffix", inst.Suffix, "pid", pid)
		_ = os.Remove(pidPath)
	default:
		if err := s.terminate(pid, terminateGrace); err != nil {
			// The process may still be holding its UDP port; do NOT release
			// the slot, or a later spawn could collide on it.
			slog.Error("spawner: terminate failed; keeping slot reserved",
				"key", key, "suffix", inst.Suffix, "pid", pid, "index", inst.Allocation.Index, "err", err)
			return
		}
		slog.Info("spawner: terminated UE5 on scale-down", "key", key, "suffix", inst.Suffix, "pid", pid)
		_ = os.Remove(pidPath)
	}
	// Release the slot only now that the port is actually free.
	s.pool.Release(inst.Allocation.Index)
	s.persist()
}

func (s *Spawner) spawnOne(obj serversetscale.Object, mapName string, partitionID, indexInSet int) {
	alloc, err := s.pool.Acquire()
	if err != nil {
		slog.Error("spawner: pool exhausted", "key", obj.Metadata.Name, "err", err)
		return
	}
	suffix := fmt.Sprintf("p%d", alloc.Index)
	ready := make(chan struct{})
	inst := instance{
		Suffix:      suffix,
		MapName:     mapName,
		PartitionID: partitionID,
		Allocation:  alloc,
		pidReady:    ready,
	}

	cmd := exec.CommandContext(context.Background(),
		"bash", s.scriptPath, s.baseDir, mapName, suffix)
	cmd.Env = spawnEnv(partitionID, alloc)
	slog.Info("spawner: starting UE5", "map", mapName, "suffix", suffix,
		"partition", partitionID, "game_port", alloc.GamePort, "igw_port", alloc.IGWPort)

	if err := cmd.Start(); err != nil {
		slog.Error("spawner: cmd.Start failed", "err", err)
		s.pool.Release(alloc.Index)
		close(ready) // unblock any future waiter; nothing was tracked
		return
	}
	// Don't Wait() on the foreground script's result — start-ue5.sh
	// backgrounds the real UE5 process via launch_bg and blocks on the
	// UDP-bind handshake; reap it so it doesn't become a zombie.
	go func() { _ = cmd.Wait() }()

	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	s.mu.Lock()
	s.instances[key] = append(s.instances[key], inst)
	s.mu.Unlock()
	s.persist()

	// Learn the real UE5 pid from the pidfile start-ue5.sh writes.
	s.bg.Add(1)
	go func() {
		defer s.bg.Done()
		s.capturePID(key, alloc.Index, s.pidPath(inst.MapName, inst.Suffix), ready)
	}()
}

// capturePID polls for the pidfile start-ue5.sh writes, records the pid onto
// the matching tracked instance, then persists. It closes ready on return
// (pid found or timed out) so teardown can stop waiting. Best-effort: if the
// pidfile never appears the instance keeps PID 0 (teardown re-reads the
// pidfile directly, so a missed capture is not fatal).
func (s *Spawner) capturePID(key string, index int, pidPath string, ready chan struct{}) {
	defer close(ready)
	deadline := time.Now().Add(pidWaitTimeout)
	for {
		if pid := proc.ReadPidFile(pidPath); pid > 0 {
			// Record the start-time alongside the pid so teardown/Restore can
			// later tell this exact process from a recycled pid.
			startTime, _ := proc.StartTime(pid)
			s.mu.Lock()
			found := false
			for i := range s.instances[key] {
				if s.instances[key][i].Allocation.Index == index {
					s.instances[key][i].PID = pid
					s.instances[key][i].StartTime = startTime
					found = true
					break
				}
			}
			s.mu.Unlock()
			if found {
				s.persist()
			} else {
				// Instance was torn down before we captured its pid. teardown
				// re-reads the pidfile directly, so this is not fatal — but
				// log it so an orphan is diagnosable.
				slog.Warn("spawner: captured pid for an instance no longer tracked",
					"key", key, "index", index, "pid", pid)
			}
			return
		}
		if time.Now().After(deadline) {
			slog.Warn("spawner: pidfile not seen before timeout", "key", key, "path", pidPath)
			return
		}
		time.Sleep(pidPollInterval)
	}
}

// persist snapshots the current instance ledger to disk so a later Restore
// can re-adopt still-live UE5 processes on their original ports. No-op when
// statePath is empty. The snapshot is taken under s.mu (which also bumps
// persistGen) but the file write happens under saveMu only, so a slow disk
// never stalls reconciliation; a monotonic generation guard stops a snapshot
// that lost the saveMu race from overwriting a newer one.
func (s *Spawner) persist() {
	if s.statePath == "" {
		return
	}
	s.mu.Lock()
	s.persistGen++
	gen := s.persistGen
	st := state.State{Version: 1}
	for key, list := range s.instances {
		for _, in := range list {
			st.Instances = append(st.Instances, state.Instance{
				Key:         key,
				MapName:     in.MapName,
				PartitionID: in.PartitionID,
				PoolIndex:   in.Allocation.Index,
				Suffix:      in.Suffix,
				PID:         in.PID,
				StartTime:   in.StartTime,
			})
		}
	}
	s.mu.Unlock()

	s.saveMu.Lock()
	defer s.saveMu.Unlock()
	if gen <= s.lastSavedGen {
		// A newer snapshot already reached disk; this one is stale — skip it.
		return
	}
	if err := state.Save(s.statePath, st); err != nil {
		slog.Error("spawner: persist state failed", "path", s.statePath, "err", err)
		return
	}
	s.lastSavedGen = gen
}

// safeNameRe is the allowlist for a map name or suffix: it must start with an
// alphanumeric and then contain only alphanumerics, '_', '.', and '-'. That
// rejects the empty string, path separators, a leading '.' (so a bare ".." or
// any dot-led traversal fails the first-character class), a leading '-' (argv
// flag injection into the UE5 binary), whitespace and newlines (log
// injection), and every shell metacharacter — none of which appear in a real
// Funcom map name (Survival_1, Overmap, …) or a generated p<N> suffix.
var safeNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

// safeInstanceName reports whether a map name or suffix is safe to embed in a
// pidfile path (no traversal), a log line (no newlines), and an exec argv (no
// leading dash, whitespace, or shell metacharacters). It is an allowlist, not
// a denylist, so unanticipated dangerous inputs fail closed.
func safeInstanceName(s string) bool {
	return safeNameRe.MatchString(s)
}

// pidPath returns the pidfile path start-ue5.sh writes for an instance,
// matching scripts/lib.sh's pid_file() + start-ue5.sh's INSTANCE_ID. Names
// are validated upstream by safeInstanceName; filepath.Base on the filename
// is defence-in-depth so even a crafted map/suffix can never resolve
// outside runtime/pids.
func (s *Spawner) pidPath(mapName, suffix string) string {
	name := filepath.Base("ue5-" + mapName + "-" + suffix + ".pid")
	return filepath.Join(s.baseDir, "runtime", "pids", name)
}

// spawnEnv builds the environment for a UE5 child: the inherited process env
// (so $PATH, $HOME, $DUNE_* set by pelican-entrypoint.sh flow through) with
// the per-instance DUNE_PARTITION / DUNE_GAME_PORT / DUNE_IGW_PORT taking
// precedence. Any inherited copy of an overridden key is dropped rather than
// merely shadowed, so the child's getenv can't resolve a stale value
// regardless of how it handles duplicate names — otherwise a stray inherited
// DUNE_GAME_PORT would collapse every instance onto one UDP port.
func spawnEnv(partitionID int, alloc pool.Allocation) []string {
	return overrideEnv(os.Environ(), mockChildEnv(partitionID, alloc))
}

// overrideEnv returns base with every "KEY=value" whose KEY is set by
// overrides removed, then overrides appended — so the override value is the
// only occurrence of that key.
func overrideEnv(base, overrides []string) []string {
	keys := make(map[string]bool, len(overrides))
	for _, kv := range overrides {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			keys[kv[:i]] = true
		}
	}
	out := make([]string, 0, len(base)+len(overrides))
	for _, kv := range base {
		if i := strings.IndexByte(kv, '='); i >= 0 && keys[kv[:i]] {
			continue
		}
		out = append(out, kv)
	}
	return append(out, overrides...)
}

// mockChildEnv returns the env vars start-ue5.sh's preamble reads to
// override the per-map env file.
func mockChildEnv(partitionID int, alloc pool.Allocation) []string {
	return []string{
		"DUNE_PARTITION=" + strconv.Itoa(partitionID),
		"DUNE_GAME_PORT=" + strconv.Itoa(alloc.GamePort),
		"DUNE_IGW_PORT=" + strconv.Itoa(alloc.IGWPort),
	}
}

func readReplicas(spec map[string]any) int {
	if v, ok := spec["replicas"]; ok {
		switch n := v.(type) {
		case float64:
			return int(n)
		case int:
			return n
		case int64:
			return int(n)
		}
	}
	return 0
}

func readPartitionID(spec map[string]any) int {
	if v, ok := spec["partitionId"]; ok {
		switch n := v.(type) {
		case float64:
			return int(n)
		case int:
			return n
		case int64:
			return int(n)
		case string:
			if i, err := strconv.Atoi(n); err == nil {
				return i
			}
		}
	}
	return 1 // sane default — partition 1 is Survival_1's slot per the schema seed
}
