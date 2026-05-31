package spawner

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/state"
)

func startSleeper(t *testing.T) int {
	t.Helper()
	cmd := exec.Command("sleep", "300")
	if err := cmd.Start(); err != nil {
		t.Fatalf("start sleeper: %v", err)
	}
	pid := cmd.Process.Pid
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	return pid
}

func deadPID(t *testing.T) int {
	t.Helper()
	pid := startSleeper(t)
	_ = syscall.Kill(pid, syscall.SIGKILL)
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if !proc.Alive(pid) {
			return pid
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("sleeper %d did not die", pid)
	return 0
}

func writeLedger(t *testing.T, base string, insts ...state.Instance) {
	t.Helper()
	sp := filepath.Join(base, "server", "state", "mock-k8s-state.json")
	if err := state.Save(sp, state.State{Version: 1, Instances: insts}); err != nil {
		t.Fatalf("write ledger: %v", err)
	}
}

func writePidfile(t *testing.T, base, mapName, suffix string, pid int) {
	t.Helper()
	dir := filepath.Join(base, "runtime", "pids")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(dir, "ue5-"+mapName+"-"+suffix+".pid")
	if err := os.WriteFile(p, []byte(itoa(pid)), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestSpawner_RestoreReadoptsLiveInstance(t *testing.T) {
	base := t.TempDir()
	pid := startSleeper(t)
	writePidfile(t, base, "Survival_1", "p2", pid)
	writeLedger(t, base, state.Instance{
		Key: "default/sietch-survival", MapName: "Survival_1", PartitionID: 1,
		PoolIndex: 2, Suffix: "p2", PID: pid,
	})

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore()

	if spw.Snapshot().Instances.RestoredAtBoot != 1 {
		t.Errorf("RestoredAtBoot = %d, want 1", spw.Snapshot().Instances.RestoredAtBoot)
	}
	if used, _, _ := pl.Stats(); used != 1 {
		t.Fatalf("after restore, pool used = %d, want 1", used)
	}
	// The original slot must be reserved: re-acquiring it must fail.
	if _, err := pl.AcquireSpecific(2); err == nil {
		t.Fatalf("slot 2 not reserved after restore (port would not be stable)")
	}
	if got := spw.testPIDs("default/sietch-survival"); len(got) != 1 || got[0] != pid {
		t.Fatalf("adopted pids = %v, want [%d]", got, pid)
	}
}

func TestSpawner_RestoreDropsDeadInstance(t *testing.T) {
	base := t.TempDir()
	dead := deadPID(t)
	writeLedger(t, base, state.Instance{
		Key: "default/sietch-overmap", MapName: "Overmap", PartitionID: 2,
		PoolIndex: 3, Suffix: "p3", PID: dead,
	})
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore()

	if used, _, _ := pl.Stats(); used != 0 {
		t.Fatalf("dead instance reserved a slot: used = %d, want 0", used)
	}
	if got := spw.testPIDs("default/sietch-overmap"); len(got) != 0 {
		t.Fatalf("dead instance was adopted: %v", got)
	}
}

func TestSpawner_RestorePreventsDuplicateSpawn(t *testing.T) {
	spw, pl, store, base := newTestSpawner(t)
	const key = "default/sietch-survival"

	pid := startSleeper(t)
	writePidfile(t, base, "Survival_1", "p0", pid)
	writeLedger(t, base, state.Instance{
		Key: key, MapName: "Survival_1", PartitionID: 1,
		PoolIndex: 0, Suffix: "p0", PID: pid,
	})

	spw.Restore()
	t.Cleanup(func() {
		for _, p := range spw.testPIDs(key) {
			_ = syscall.Kill(p, syscall.SIGKILL)
		}
	})
	if got := spw.testPIDs(key); len(got) != 1 || got[0] != pid {
		t.Fatalf("restore adopted %v, want [%d]", got, pid)
	}

	// AlwaysWarm pre-spawn re-creates the SSS at replicas=1. Because the
	// instance was re-adopted, OnSpecChange must see desired == current and
	// NOT spawn a duplicate.
	mustCreate(t, store, "sietch-survival", 1)
	spw.Wait()

	if used, _, _ := pl.Stats(); used != 1 {
		t.Fatalf("duplicate spawn after restore: pool used = %d, want 1", used)
	}
	if got := spw.testPIDs(key); len(got) != 1 || got[0] != pid {
		t.Fatalf("instance set changed by a no-op reconcile: %v", got)
	}
	if !proc.Alive(pid) {
		t.Fatalf("re-adopted process was killed by reconcile")
	}

	// Scaling beyond the adopted count still spawns a fresh instance.
	patchReplicas(t, store, "sietch-survival", 2)
	spw.Wait()
	if used, _, _ := pl.Stats(); used != 2 {
		t.Fatalf("scale-up after restore: pool used = %d, want 2", used)
	}
}
