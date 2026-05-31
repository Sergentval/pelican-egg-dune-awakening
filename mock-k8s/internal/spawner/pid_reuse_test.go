package spawner

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/state"
)

// A crashed UE5 leaves a stale pidfile; if that pid is recycled onto an
// unrelated process, teardown must NOT signal it (proc.sendSignal also hits
// the whole process group). The recorded start-time no longer matching is the
// tell: skip the kill, still free the slot.
func TestSpawner_TeardownSkipsReusedPid(t *testing.T) {
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	alloc, _ := pl.Acquire()
	terminated := false
	spw.terminate = func(pid int, grace time.Duration) error { terminated = true; return nil }

	live := startSleeper(t) // a real, live, UNRELATED process holding the recycled pid
	inst := instance{
		Suffix:     "p" + itoa(alloc.Index),
		MapName:    "Survival_1",
		Allocation: alloc,
		PID:        live,
		StartTime:  1, // recorded identity ≠ the live process's real start-time
	}
	spw.teardown("default/x", inst)

	if terminated {
		t.Error("teardown signalled a pid whose recorded start-time no longer matches — would SIGKILL a reused-pid stranger and its group")
	}
	if used, _, _ := pl.Stats(); used != 0 {
		t.Errorf("teardown left the slot reserved after detecting pid reuse: used=%d, want 0", used)
	}
}

// Restore must not re-adopt a live pid whose recorded start-time mismatches:
// the original UE5 exited and the pid was recycled, so adopting it would block
// a slot and mark a dead map as running.
func TestSpawner_RestoreDropsReusedPid(t *testing.T) {
	base := t.TempDir()
	live := startSleeper(t)
	writePidfile(t, base, "Survival_1", "p2", live)
	writeLedger(t, base, state.Instance{
		Key: "default/sietch-survival", MapName: "Survival_1", PartitionID: 1,
		PoolIndex: 2, Suffix: "p2", PID: live, StartTime: 1, // mismatched identity
	})

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)
	spw.Restore()

	if used, _, _ := pl.Stats(); used != 0 {
		t.Errorf("Restore adopted a live pid whose recorded start-time mismatched: used=%d, want 0", used)
	}
}
