// Package spawner reconciles ServerSetScale spec.replicas → running UE5
// processes by shelling out to scripts/start-ue5.sh.
//
// This is the only place where mock-k8s actually touches the host: we
// fork start-ue5.sh with the per-instance env (DUNE_PARTITION, GAME_PORT,
// IGW_PORT) the script's preamble expects (see scripts/start-ue5.sh line
// 26-31). Process lifetime is owned by start-ue5.sh's launch_bg — we get
// just an "exec returned 0" once the script has backgrounded the
// UE5 process and written its pid file.
package spawner

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"strconv"
	"sync"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// Spawner ties the ServerSetScale store, the port Pool, and the
// start-ue5.sh script together.
type Spawner struct {
	store      *serversetscale.Store
	pool       *pool.Pool
	scriptPath string // absolute path to scripts/start-ue5.sh
	baseDir    string // $BASE for the script

	mu        sync.Mutex
	instances map[string][]instance // key: namespace/name → instance list
}

type instance struct {
	Suffix     string
	PartitionID int
	Allocation pool.Allocation
}

// New constructs a Spawner. scriptPath should be absolute (e.g.
// /home/container/scripts/start-ue5.sh). baseDir is what we pass as $1
// to the script.
func New(store *serversetscale.Store, pool *pool.Pool, scriptPath, baseDir string) *Spawner {
	return &Spawner{
		store:      store,
		pool:       pool,
		scriptPath: scriptPath,
		baseDir:    baseDir,
		instances:  make(map[string][]instance),
	}
}

// OnSpecChange is meant to be assigned to store.OnSpecChange. It diffs
// the desired replica count against current instances and starts /
// stops as needed.
func (s *Spawner) OnSpecChange(obj serversetscale.Object) {
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	mapName, _ := obj.Spec["mapName"].(string)
	if mapName == "" {
		mapName, _ = obj.Spec["map"].(string) // tolerate alt field name
	}
	desired := readReplicas(obj.Spec)
	partitionID := readPartitionID(obj.Spec)

	s.mu.Lock()
	current := s.instances[key]
	s.mu.Unlock()

	if desired == len(current) {
		return
	}
	if desired > len(current) {
		for i := len(current); i < desired; i++ {
			s.spawnOne(obj, mapName, partitionID, i)
		}
	} else {
		// scale-down: just remove tracking entries; start-ue5.sh's
		// child process lifetime is owned by the parent script. A
		// proper implementation would SIGTERM the recorded pid here;
		// for first-boot parity we accept the leak and rely on
		// container restart to clean up.
		s.mu.Lock()
		removed := s.instances[key][desired:]
		s.instances[key] = s.instances[key][:desired]
		s.mu.Unlock()
		for _, inst := range removed {
			s.pool.Release(inst.Allocation.Index)
			slog.Info("spawner: scale-down (leak warning, no SIGTERM yet)",
				"key", key, "suffix", inst.Suffix, "index", inst.Allocation.Index)
		}
	}

	// Reflect spec change into status.
	_, _ = s.store.UpdateStatus(obj.Metadata.Namespace, obj.Metadata.Name, map[string]any{
		"observedGeneration": obj.Metadata.Generation,
		"completedReplicas":  int64(desired),
	})
}

func (s *Spawner) spawnOne(obj serversetscale.Object, mapName string, partitionID, indexInSet int) {
	alloc, err := s.pool.Acquire()
	if err != nil {
		slog.Error("spawner: pool exhausted", "key", obj.Metadata.Name, "err", err)
		return
	}
	suffix := fmt.Sprintf("p%d", alloc.Index)

	cmd := exec.CommandContext(context.Background(),
		"bash", s.scriptPath, s.baseDir, mapName, suffix)
	cmd.Env = append([]string{}, mockChildEnv(partitionID, alloc)...)
	// Inherit existing process env so things like $PATH, $HOME, $DUNE_*
	// (set by pelican-entrypoint.sh) flow through.
	for _, e := range cmdEnviron() {
		cmd.Env = append(cmd.Env, e)
	}
	slog.Info("spawner: starting UE5", "map", mapName, "suffix", suffix,
		"partition", partitionID, "game_port", alloc.GamePort, "igw_port", alloc.IGWPort)

	if err := cmd.Start(); err != nil {
		slog.Error("spawner: cmd.Start failed", "err", err)
		s.pool.Release(alloc.Index)
		return
	}
	// We deliberately don't Wait() — start-ue5.sh backgrounds the real
	// UE5 process via launch_bg and exits itself; Wait would block on
	// the script's lifecycle, not the UE5 process.
	go func() {
		_ = cmd.Wait()
	}()

	s.mu.Lock()
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	s.instances[key] = append(s.instances[key], instance{
		Suffix:      suffix,
		PartitionID: partitionID,
		Allocation:  alloc,
	})
	s.mu.Unlock()
}

// mockChildEnv returns the env vars start-ue5.sh's preamble reads to
// override the per-map env file (lines 26-31 of the script).
func mockChildEnv(partitionID int, alloc pool.Allocation) []string {
	return []string{
		"DUNE_PARTITION=" + strconv.Itoa(partitionID),
		"DUNE_GAME_PORT=" + strconv.Itoa(alloc.GamePort),
		"DUNE_IGW_PORT=" + strconv.Itoa(alloc.IGWPort),
	}
}

// cmdEnviron returns os.Environ wrapped in a function so unit tests can
// fake it. Trivial today; kept as a hook for future test ergonomics.
func cmdEnviron() []string {
	return osEnviron()
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
