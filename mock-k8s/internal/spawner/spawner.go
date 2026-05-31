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
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
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

	mu        sync.Mutex
	instances map[string][]instance // key: namespace/name → instance list

	// saveMu serializes ledger writes so concurrent persist() calls (e.g. a
	// spawn and a teardown finishing at once) can't interleave file writes.
	saveMu sync.Mutex

	// bg tracks background goroutines (pid capture + scale-down teardown)
	// so tests — and a future graceful shutdown — can wait for them.
	bg sync.WaitGroup

	// terminate is the process-termination seam; defaults to proc.Terminate.
	// Swappable so tests can assert teardown behaviour without real signals.
	terminate func(pid int, grace time.Duration) error
}

type instance struct {
	Suffix      string
	MapName     string
	PartitionID int
	Allocation  pool.Allocation
	PID         int // 0 until capturePID reads the pidfile
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
// still alive, reserving their original port slots so game/IGW ports stay
// stable across a mock-k8s restart. Dead entries are dropped (their maps
// respawn through normal reconciliation). MUST run before the AlwaysWarm
// pre-spawn so a re-adopted map's OnSpecChange sees desired == current and
// does not double-spawn a process that is already running.
func (s *Spawner) Restore() {
	if s.statePath == "" {
		return
	}
	prev, err := state.Load(s.statePath)
	if err != nil {
		// Quarantine the unreadable ledger so it is preserved for inspection
		// and does not keep failing on every boot, then start fresh.
		corrupt := s.statePath + ".corrupt." + strconv.FormatInt(time.Now().UnixNano(), 10)
		if rnErr := os.Rename(s.statePath, corrupt); rnErr != nil {
			slog.Error("spawner: state ledger unreadable and could not be quarantined; starting fresh",
				"path", s.statePath, "err", err, "rename_err", rnErr)
		} else {
			slog.Error("spawner: state ledger unreadable; quarantined and starting fresh",
				"path", s.statePath, "quarantined_to", corrupt, "err", err)
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
		// The pidfile on disk is the source of truth (start-ue5.sh wrote
		// it); fall back to the ledger's recorded pid if it's gone.
		pid := proc.ReadPidFile(pidPath)
		if pid == 0 {
			pid = pi.PID
		}
		if !proc.Alive(pid) {
			slog.Info("spawner: dropping dead instance from prior state",
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
		s.mu.Lock()
		s.instances[pi.Key] = append(s.instances[pi.Key], instance{
			Suffix:      pi.Suffix,
			MapName:     pi.MapName,
			PartitionID: pi.PartitionID,
			Allocation:  alloc,
			PID:         pid,
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
func (s *Spawner) OnSpecChange(obj serversetscale.Object) {
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	mapName, _ := obj.Spec["mapName"].(string)
	if mapName == "" {
		mapName, _ = obj.Spec["map"].(string) // tolerate alt field name
	}
	if !safeInstanceName(mapName) {
		slog.Error("spawner: refusing ServerSetScale with unsafe map name",
			"key", key, "map", mapName)
		return
	}
	desired := readReplicas(obj.Spec)
	partitionID := readPartitionID(obj.Spec)

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
// dying UE5 still holds it.
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
	pidPath := s.pidPath(inst.MapName, inst.Suffix)
	pid := proc.ReadPidFile(pidPath)
	if pid == 0 {
		pid = inst.PID
	}
	if pid > 0 {
		if err := s.terminate(pid, terminateGrace); err != nil {
			slog.Error("spawner: terminate failed", "key", key, "suffix", inst.Suffix, "pid", pid, "err", err)
		} else {
			slog.Info("spawner: terminated UE5 on scale-down", "key", key, "suffix", inst.Suffix, "pid", pid)
		}
		_ = os.Remove(pidPath)
	} else {
		slog.Warn("spawner: scale-down with no pid to terminate", "key", key, "suffix", inst.Suffix)
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
	inst := instance{
		Suffix:      suffix,
		MapName:     mapName,
		PartitionID: partitionID,
		Allocation:  alloc,
	}

	cmd := exec.CommandContext(context.Background(),
		"bash", s.scriptPath, s.baseDir, mapName, suffix)
	cmd.Env = append([]string{}, mockChildEnv(partitionID, alloc)...)
	// Inherit existing process env so $PATH, $HOME, $DUNE_* (set by
	// pelican-entrypoint.sh) flow through.
	cmd.Env = append(cmd.Env, os.Environ()...)
	slog.Info("spawner: starting UE5", "map", mapName, "suffix", suffix,
		"partition", partitionID, "game_port", alloc.GamePort, "igw_port", alloc.IGWPort)

	if err := cmd.Start(); err != nil {
		slog.Error("spawner: cmd.Start failed", "err", err)
		s.pool.Release(alloc.Index)
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
		s.capturePID(key, alloc.Index, s.pidPath(inst.MapName, inst.Suffix))
	}()
}

// capturePID polls for the pidfile start-ue5.sh writes and records the pid
// onto the matching tracked instance, then persists. Best-effort: if the
// pidfile never appears within pidWaitTimeout we log and move on (teardown
// re-reads the pidfile directly, so a missed capture is not fatal).
func (s *Spawner) capturePID(key string, index int, pidPath string) {
	deadline := time.Now().Add(pidWaitTimeout)
	for {
		if pid := proc.ReadPidFile(pidPath); pid > 0 {
			s.mu.Lock()
			for i := range s.instances[key] {
				if s.instances[key][i].Allocation.Index == index {
					s.instances[key][i].PID = pid
					break
				}
			}
			s.mu.Unlock()
			s.persist()
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
// statePath is empty. The snapshot is taken under s.mu but the file write
// happens under saveMu only, so a slow disk never stalls reconciliation.
func (s *Spawner) persist() {
	if s.statePath == "" {
		return
	}
	s.mu.Lock()
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
			})
		}
	}
	s.mu.Unlock()

	s.saveMu.Lock()
	defer s.saveMu.Unlock()
	if err := state.Save(s.statePath, st); err != nil {
		slog.Error("spawner: persist state failed", "path", s.statePath, "err", err)
	}
}

// safeInstanceName reports whether a map name or suffix is safe to embed in
// a pidfile path: non-empty and free of path separators or "..", so a
// crafted ServerSetScale spec cannot make the spawner read, write, or
// delete files outside runtime/pids.
func safeInstanceName(s string) bool {
	if s == "" {
		return false
	}
	if strings.ContainsAny(s, "/\\") {
		return false
	}
	return !strings.Contains(s, "..")
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
