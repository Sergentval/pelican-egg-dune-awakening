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

// instanceNameRE is the allowlist for a map name or pid-suffix: it must
// start with an alphanumeric or underscore and then contain only
// alphanumerics, underscore, or hyphen. That rules out path separators and
// "..", plus newlines (log injection), leading dashes (argument injection),
// spaces, and shell metacharacters.
var instanceNameRE = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_-]*$`)

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

	mu         sync.Mutex
	instances  map[string][]instance // key: namespace/name → instance list
	persistSeq uint64                // bumped per snapshot, under mu

	// saveMu serializes ledger writes; lastSaved (under saveMu) records the
	// highest snapshot sequence already on disk so an out-of-order writer
	// can't regress the ledger to an older snapshot.
	saveMu    sync.Mutex
	lastSaved uint64

	// bg tracks background goroutines (pid capture + scale-down teardown)
	// so tests — and a future graceful shutdown — can wait for them.
	bg sync.WaitGroup

	// terminate is the process-termination seam; defaults to proc.Terminate.
	terminate func(pid int, grace time.Duration) error
}

type instance struct {
	Suffix      string
	MapName     string
	PartitionID int
	Allocation  pool.Allocation
	PID         int    // 0 until capturePID reads the pidfile
	StartTime   uint64 // process start time, for pid-reuse identity checks

	// pidReady is closed by capturePID once the pidfile has been read (or the
	// wait timed out). teardown blocks on it so a scale-down that races a
	// just-started spawn waits for the real pid instead of seeing pid==0 and
	// orphaning the process. nil for restored instances (pid already known).
	pidReady chan struct{}
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
	}
}

// Wait blocks until all in-flight background goroutines (pid capture and
// scale-down teardown) finish. Primarily a test seam.
func (s *Spawner) Wait() { s.bg.Wait() }

// Restore re-adopts UE5 instances recorded in the on-disk ledger that are
// still alive (same pid AND same process identity), reserving their original
// port slots so game/IGW ports stay stable across a mock-k8s restart. Dead
// or recycled entries are dropped. A corrupt ledger is quarantined; a
// transient I/O error leaves the ledger untouched. MUST run before the
// AlwaysWarm pre-spawn so a re-adopted map's OnSpecChange sees
// desired == current and does not double-spawn.
func (s *Spawner) Restore() {
	if s.statePath == "" {
		return
	}
	prev, err := state.Load(s.statePath)
	if err != nil {
		if errors.Is(err, state.ErrCorrupt) {
			// Real corruption: quarantine the file so it is preserved for
			// inspection and doesn't keep failing every boot, then start fresh.
			corrupt := s.statePath + ".corrupt." + strconv.FormatInt(time.Now().UnixNano(), 10)
			if rnErr := os.Rename(s.statePath, corrupt); rnErr != nil {
				slog.Error("spawner: state ledger corrupt and could not be quarantined; starting fresh",
					"path", s.statePath, "err", err, "rename_err", rnErr)
			} else {
				slog.Error("spawner: state ledger corrupt; quarantined and starting fresh",
					"path", s.statePath, "quarantined_to", corrupt, "err", err)
			}
			return
		}
		// Transient I/O error (permissions, EIO): do NOT destroy the ledger.
		// Abort the restore and leave it in place to retry on the next boot.
		slog.Error("spawner: state ledger unreadable (transient); leaving it in place, skipping restore",
			"path", s.statePath, "err", err)
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
		// The pidfile on disk is the source of truth (start-ue5.sh wrote
		// it); fall back to the ledger's recorded pid if it's gone.
		pid := proc.ReadPidFile(pidPath)
		if pid == 0 {
			pid = pi.PID
		}
		// Identity-checked liveness: a recycled pid (a different process now
		// holding the same number) must NOT be adopted.
		if !proc.SameProcess(pid, pi.StartTime) {
			slog.Info("spawner: dropping dead/recycled instance from prior state",
				"key", pi.Key, "map", pi.MapName, "suffix", pi.Suffix)
			_ = os.Remove(pidPath)
			continue
		}
		alloc, err := s.pool.AcquireSpecific(pi.PoolIndex)
		if err != nil {
			slog.Warn("spawner: cannot reserve slot for re-adopted instance; leaving it orphaned",
				"key", pi.Key, "index", pi.PoolIndex, "err", err)
			continue
		}
		// Trust the persisted ports — that is what the live UE5 actually
		// bound to. They diverge from the pool's recomputed ports if
		// K8S_POOL_*_BASE changed across the restart.
		if pi.GamePort != 0 && (alloc.GamePort != pi.GamePort || alloc.IGWPort != pi.IGWPort) {
			slog.Warn("spawner: pool base changed since restart; trusting persisted ports",
				"key", pi.Key, "recomputed_game", alloc.GamePort, "persisted_game", pi.GamePort)
			alloc.GamePort = pi.GamePort
			alloc.IGWPort = pi.IGWPort
		}
		st := pi.StartTime
		if st == 0 {
			st, _ = proc.StartTime(pid) // backfill identity for pre-StartTime ledgers
		}
		s.mu.Lock()
		s.instances[pi.Key] = append(s.instances[pi.Key], instance{
			Suffix:      pi.Suffix,
			MapName:     pi.MapName,
			PartitionID: pi.PartitionID,
			Allocation:  alloc,
			PID:         pid,
			StartTime:   st,
		})
		s.mu.Unlock()
		adopted++
		slog.Info("spawner: re-adopted live UE5 across restart",
			"key", pi.Key, "map", pi.MapName, "pid", pid, "game_port", alloc.GamePort)
	}
	slog.Info("spawner: state restore complete", "adopted", adopted, "of", len(prev.Instances))
	// Rewrite the ledger so it reflects only the instances we kept.
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
	partitionID := readPartitionID(obj.Spec)

	s.reconcileMu.Lock()
	s.mu.Lock()
	current := len(s.instances[key])
	s.mu.Unlock()

	switch {
	case desired > current:
		for i := current; i < desired; i++ {
			s.spawnOne(obj, mapName, partitionID, i)
		}
	case desired < current:
		s.scaleDown(key, desired)
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
// goroutines run after it returns, so the 15s grace never blocks
// reconciliation.
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
	case pid > 0 && proc.SameProcess(pid, inst.StartTime):
		if err := s.terminate(pid, terminateGrace); err != nil {
			// The process may still be holding its UDP port; do NOT release
			// the slot, or a later spawn could collide on it.
			slog.Error("spawner: terminate failed; keeping slot reserved",
				"key", key, "suffix", inst.Suffix, "pid", pid, "index", inst.Allocation.Index, "err", err)
			return
		}
		slog.Info("spawner: terminated UE5 on scale-down", "key", key, "suffix", inst.Suffix, "pid", pid)
		_ = os.Remove(pidPath)
	case pid > 0:
		// Alive, but its identity doesn't match what we spawned — the
		// original UE5 died and the pid was recycled. Never signal a stranger.
		slog.Warn("spawner: scale-down pid identity mismatch (recycled?); not terminating",
			"key", key, "suffix", inst.Suffix, "pid", pid)
		_ = os.Remove(pidPath)
	default:
		slog.Warn("spawner: scale-down with no pid (process never started?)", "key", key, "suffix", inst.Suffix)
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
	// Inherit the ambient env FIRST, then our per-instance overrides, so the
	// per-instance DUNE_PARTITION/GAME_PORT/IGW_PORT win over any inherited
	// duplicates (last value wins) and instances can't collapse onto one port.
	cmd.Env = append([]string{}, os.Environ()...)
	cmd.Env = append(cmd.Env, mockChildEnv(partitionID, alloc)...)
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

// capturePID polls for the pidfile start-ue5.sh writes, records the pid and
// its start-time onto the matching tracked instance, then persists. It
// closes ready on return so teardown can stop waiting. If the instance was
// already torn down (a scale-down that raced the spawn), it reaps the
// now-orphaned process here rather than leaving it holding a UDP port.
func (s *Spawner) capturePID(key string, index int, pidPath string, ready chan struct{}) {
	defer close(ready)
	deadline := time.Now().Add(pidWaitTimeout)
	for {
		if pid := proc.ReadPidFile(pidPath); pid > 0 {
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
				return
			}
			// The instance was removed (torn down) before we captured its
			// pid, so teardown took the no-pid path and didn't kill it. Reap
			// the orphan here so it doesn't keep holding its UDP port.
			slog.Warn("spawner: reaping orphaned UE5 (instance torn down before pid capture)",
				"key", key, "index", index, "pid", pid)
			if err := s.terminate(pid, terminateGrace); err != nil {
				slog.Error("spawner: failed to reap orphan", "key", key, "pid", pid, "err", err)
			}
			_ = os.Remove(pidPath)
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
// statePath is empty. Each snapshot is stamped with a monotonic sequence so
// that if two persist() calls race, the older snapshot can never overwrite a
// newer one already on disk.
func (s *Spawner) persist() {
	if s.statePath == "" {
		return
	}
	s.mu.Lock()
	s.persistSeq++
	seq := s.persistSeq
	st := state.State{Version: 1}
	for key, list := range s.instances {
		for _, in := range list {
			st.Instances = append(st.Instances, state.Instance{
				Key:         key,
				MapName:     in.MapName,
				PartitionID: in.PartitionID,
				PoolIndex:   in.Allocation.Index,
				GamePort:    in.Allocation.GamePort,
				IGWPort:     in.Allocation.IGWPort,
				Suffix:      in.Suffix,
				PID:         in.PID,
				StartTime:   in.StartTime,
			})
		}
	}
	s.mu.Unlock()

	s.saveMu.Lock()
	defer s.saveMu.Unlock()
	if seq < s.lastSaved {
		return // a newer snapshot already hit disk; don't regress it
	}
	if err := state.Save(s.statePath, st); err != nil {
		slog.Error("spawner: persist state failed", "path", s.statePath, "err", err)
		return
	}
	s.lastSaved = seq
}

// safeInstanceName reports whether a map name or suffix passes the allowlist:
// it must be safe both as a pidfile path component and as a UE5 argv element.
func safeInstanceName(s string) bool {
	return instanceNameRE.MatchString(s)
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
