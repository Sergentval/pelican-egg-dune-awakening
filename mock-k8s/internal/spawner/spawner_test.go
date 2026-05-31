package spawner

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// fakeUE5Script stands in for scripts/start-ue5.sh: it backgrounds a long
// sleep (our pretend UE5 process) and records its pid at the exact path
// lib.sh's launch_bg would (`$BASE/runtime/pids/ue5-<map>-<suffix>.pid`),
// then exits — mirroring start-ue5.sh's "write pidfile, detach" shape.
const fakeUE5Script = `#!/usr/bin/env bash
BASE="$1"; MAP="$2"; SUFFIX="$3"
mkdir -p "$BASE/runtime/pids"
sleep 300 &
echo $! > "$BASE/runtime/pids/ue5-$MAP-$SUFFIX.pid"
`

func writeFakeScript(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(fakeUE5Script), 0o755); err != nil {
		t.Fatalf("write fake script: %v", err)
	}
}

// testPIDs snapshots the tracked pids for a key (same-package test access).
func (s *Spawner) testPIDs(key string) []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]int, 0, len(s.instances[key]))
	for _, in := range s.instances[key] {
		out = append(out, in.PID)
	}
	return out
}

func newTestSpawner(t *testing.T) (*Spawner, *pool.Pool, *serversetscale.Store, string) {
	t.Helper()
	base := t.TempDir()
	script := filepath.Join(base, "fake-start-ue5.sh")
	writeFakeScript(t, script)

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatalf("pool.New: %v", err)
	}
	spw := New(store, pl, script, base)
	store.OnSpecChange = spw.OnSpecChange
	return spw, pl, store, base
}

func mustCreate(t *testing.T, store *serversetscale.Store, name string, replicas int64) {
	t.Helper()
	_, ok := store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: name},
		Spec: map[string]any{
			"mapName":     "Survival_1",
			"partitionId": int64(1),
			"replicas":    replicas,
		},
	})
	if !ok {
		t.Fatalf("create %s failed", name)
	}
}

func patchReplicas(t *testing.T, store *serversetscale.Store, name string, replicas int64) {
	t.Helper()
	_, ok := store.Patch("default", name, map[string]any{
		"spec": map[string]any{"replicas": replicas},
	}, false)
	if !ok {
		t.Fatalf("patch %s replicas=%d failed", name, replicas)
	}
}

func aliveCount(pids []int) (n int) {
	for _, p := range pids {
		if proc.Alive(p) {
			n++
		}
	}
	return n
}

func TestSpawner_ScaleUpCapturesPidsAndPorts(t *testing.T) {
	spw, pl, store, _ := newTestSpawner(t)
	const key = "default/sietch-survival"

	mustCreate(t, store, "sietch-survival", 2)
	spw.Wait() // let capturePID read both pidfiles

	if used, _, _ := pl.Stats(); used != 2 {
		t.Fatalf("pool used = %d, want 2", used)
	}
	pids := spw.testPIDs(key)
	if len(pids) != 2 {
		t.Fatalf("tracked instances = %d, want 2", len(pids))
	}
	t.Cleanup(func() {
		for _, p := range pids {
			_ = syscall.Kill(p, syscall.SIGKILL)
		}
	})
	for i, p := range pids {
		if p <= 0 {
			t.Errorf("instance %d has no captured pid", i)
		} else if !proc.Alive(p) {
			t.Errorf("instance %d pid %d not alive", i, p)
		}
	}
}

func TestSpawner_ScaleDownTerminatesAndReleases(t *testing.T) {
	spw, pl, store, _ := newTestSpawner(t)
	const key = "default/sietch-survival"

	mustCreate(t, store, "sietch-survival", 2)
	spw.Wait()
	pids := spw.testPIDs(key)
	if len(pids) != 2 {
		t.Fatalf("setup: tracked instances = %d, want 2", len(pids))
	}
	t.Cleanup(func() {
		for _, p := range pids {
			_ = syscall.Kill(p, syscall.SIGKILL)
		}
	})

	// Scale 2 → 1: exactly one UE5 must die and its slot must free.
	patchReplicas(t, store, "sietch-survival", 1)
	spw.Wait()

	if used, _, _ := pl.Stats(); used != 1 {
		t.Fatalf("after scale to 1, pool used = %d, want 1", used)
	}
	if got := aliveCount(pids); got != 1 {
		t.Fatalf("after scale to 1, alive pids = %d, want 1", got)
	}

	// Scale 1 → 0: everything gone, no leaked slots.
	patchReplicas(t, store, "sietch-survival", 0)
	spw.Wait()

	if used, _, _ := pl.Stats(); used != 0 {
		t.Fatalf("after scale to 0, pool used = %d, want 0", used)
	}
	if got := aliveCount(pids); got != 0 {
		t.Fatalf("after scale to 0, alive pids = %d, want 0", got)
	}
}

func TestSpawner_TeardownToleratesMissingPidfile(t *testing.T) {
	spw, pl, _, _ := newTestSpawner(t)

	// An instance whose pidfile was never written and with no captured pid
	// must not panic and must still release its pool slot.
	alloc, err := pl.Acquire()
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	inst := instance{Suffix: "p" + itoa(alloc.Index), MapName: "Overmap", Allocation: alloc}

	done := make(chan struct{})
	go func() { spw.teardown("default/sietch-overmap", inst); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("teardown hung on missing pidfile")
	}
	if used, _, _ := pl.Stats(); used != 0 {
		t.Fatalf("teardown leaked a slot: used = %d, want 0", used)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}
