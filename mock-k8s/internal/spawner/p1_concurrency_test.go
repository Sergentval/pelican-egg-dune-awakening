package spawner

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// P1-A: concurrent reconciles for the SAME map must not over-spawn past
// spec.replicas. Without the reconcile lock, each goroutine reads the stale
// count (0) and spawns, exhausting the pool.
func TestSpawner_ConcurrentReconcileDoesNotOverspawn(t *testing.T) {
	spw, pl, _, _ := newTestSpawner(t)
	const key = "default/sietch-survival"
	obj := serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "sietch-survival"},
		Spec:     map[string]any{"mapName": "Survival_1", "partitionId": int64(1), "replicas": int64(1)},
	}
	t.Cleanup(func() {
		for _, p := range spw.testPIDs(key) {
			_ = syscall.Kill(p, syscall.SIGKILL)
		}
	})

	const N = 50
	var wg sync.WaitGroup
	for i := 0; i < N; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); spw.OnSpecChange(obj) }()
	}
	wg.Wait()
	spw.Wait()

	if used, _, _ := pl.Stats(); used != 1 {
		t.Fatalf("%d concurrent reconciles over-spawned: pool used = %d, want 1", N, used)
	}
	if got := spw.testPIDs(key); len(got) != 1 {
		t.Fatalf("tracked instances = %d, want 1", len(got))
	}
}

// P1-B (i): teardown must NOT release the pool slot when termination fails —
// the process may still be holding its UDP port.
func TestSpawner_TeardownKeepsSlotWhenTerminateFails(t *testing.T) {
	spw, pl, _, base := newTestSpawner(t)
	spw.terminate = func(int, time.Duration) error { return fmt.Errorf("simulated kill failure") }

	alloc, err := pl.Acquire()
	if err != nil {
		t.Fatal(err)
	}
	suffix := "p" + itoa(alloc.Index)
	pid := startSleeper(t)
	writePidfile(t, base, "Overmap", suffix, pid)

	spw.teardown("default/sietch-overmap", instance{Suffix: suffix, MapName: "Overmap", Allocation: alloc})

	if used, _, _ := pl.Stats(); used != 1 {
		t.Fatalf("slot released despite terminate error: pool used = %d, want 1", used)
	}
}

// delayedUE5Script backgrounds the fake UE5 process but delays writing its
// pidfile, so a scale-down issued right after scale-up lands in the spawn
// window. It records the real pid in a side-file the test can read.
const delayedUE5Script = `#!/usr/bin/env bash
BASE="$1"; MAP="$2"; SUFFIX="$3"
mkdir -p "$BASE/runtime/pids"
sleep 300 &
SP=$!
echo "$SP" > "$BASE/sleeper-$SUFFIX.pid"
sleep 1
echo "$SP" > "$BASE/runtime/pids/ue5-$MAP-$SUFFIX.pid"
`

// P1-B (ii): a scale-down landing in the spawn window must wait for the pid
// and still kill the process, not orphan it.
func TestSpawner_TeardownWaitsForSpawnWindow(t *testing.T) {
	base := t.TempDir()
	script := filepath.Join(base, "delayed-start-ue5.sh")
	if err := os.WriteFile(script, []byte(delayedUE5Script), 0o755); err != nil {
		t.Fatal(err)
	}
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, script, base)
	store.OnSpecChange = spw.OnSpecChange

	mustCreate(t, store, "sietch-survival", 1)    // spawns; pidfile delayed ~1s
	patchReplicas(t, store, "sietch-survival", 0) // scale-down in the window
	spw.Wait()

	b, err := os.ReadFile(filepath.Join(base, "sleeper-p0.pid"))
	if err != nil {
		t.Fatalf("sleeper side-file missing: %v", err)
	}
	sp, _ := strconv.Atoi(strings.TrimSpace(string(b)))
	t.Cleanup(func() { _ = syscall.Kill(sp, syscall.SIGKILL) })

	if sp > 0 && proc.Alive(sp) {
		t.Fatalf("UE5 (pid %d) orphaned — teardown didn't wait for the spawn window", sp)
	}
	if used, _, _ := pl.Stats(); used != 0 {
		t.Errorf("pool slot not released after teardown: used = %d, want 0", used)
	}
}
