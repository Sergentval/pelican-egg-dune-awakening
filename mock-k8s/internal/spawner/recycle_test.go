package spawner

import (
	"fmt"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
)

// Recycle exists for issue #119: the game applies a new Coriolis cycle only
// when a Deep Desert server BOOTS, so a DD that is up across the Tuesday
// boundary keeps the old layout until someone restarts it. Recycling restarts
// one instance in place.
//
// The property that matters most is ordering. Two UE5 servers on the same
// partition claim the same IGW index and the incumbent dies on it (the 1.5
// crash loop, see spawn_race_test.go), so the replacement must not start
// until the old process is gone.

// spawnOneLive brings key up to one tracked instance with a captured pid and
// returns it.
func spawnOneLive(t *testing.T, spw *Spawner, key string) instance {
	t.Helper()
	spw.reconcileTick()
	spw.Wait()
	spw.mu.Lock()
	defer spw.mu.Unlock()
	list := spw.instances[key]
	if len(list) != 1 || list[0].PID <= 0 {
		t.Fatalf("setup: want 1 live instance for %s, got %+v", key, list)
	}
	return list[0]
}

func waitDead(pid int) {
	for i := 0; i < 300 && proc.Alive(pid); i++ {
		time.Sleep(10 * time.Millisecond)
	}
}

func TestRecycle_RestartsInstanceWithFreshProcess(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("dd", "DeepDesert_1", 1))
	old := spawnOneLive(t, spw, "default/dd")

	respawned, err := spw.Recycle("default/dd", old.Suffix)
	if err != nil || !respawned {
		t.Fatalf("Recycle = %v, %v; want a respawn and no error", respawned, err)
	}
	spw.Wait()
	waitDead(old.PID)

	spw.mu.Lock()
	list := append([]instance(nil), spw.instances["default/dd"]...)
	recycled := spw.recycledTotal
	spw.mu.Unlock()
	if len(list) != 1 {
		t.Fatalf("tracked %d instances after recycle, want exactly 1", len(list))
	}
	if list[0].PID <= 0 || list[0].PID == old.PID {
		t.Errorf("replacement pid = %d, want a fresh process (old %d)", list[0].PID, old.PID)
	}
	if proc.Alive(old.PID) {
		t.Errorf("old UE5 (pid %d) still alive after recycle", old.PID)
	}
	if recycled != 1 {
		t.Errorf("recycledTotal = %d, want 1", recycled)
	}
}

// While the old process is still shutting down, neither the reconcile loop
// nor a Director patch may start its replacement.
func TestRecycle_NoReplacementWhileOldProcessAlive(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("dd", "DeepDesert_1", 1))
	old := spawnOneLive(t, spw, "default/dd")

	var duringShutdown int
	spw.terminate = func(pid int, grace time.Duration) error {
		// The old UE5 is still running here. Give every respawn path a chance.
		spw.reconcileTick()
		obj, _ := spw.store.Get("default", "dd")
		spw.OnSpecChange(obj)
		spw.Wait()
		spw.mu.Lock()
		duringShutdown = len(spw.instances["default/dd"])
		spw.mu.Unlock()
		return proc.Terminate(pid, grace)
	}

	if _, err := spw.Recycle("default/dd", old.Suffix); err != nil {
		t.Fatalf("Recycle: %v", err)
	}
	spw.Wait()
	if duringShutdown != 0 {
		t.Fatalf("%d replacement(s) started while the old UE5 was still alive: two servers on one partition is the IGW collision", duringShutdown)
	}
	spw.mu.Lock()
	after := len(spw.instances["default/dd"])
	spw.mu.Unlock()
	if after != 1 {
		t.Errorf("tracked %d after recycle, want 1", after)
	}
}

// If the old process cannot be stopped it is still the live server: keep it
// tracked and start nothing next to it.
func TestRecycle_TerminateFailureKeepsIncumbent(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	pl := spw.pool
	spw.store.Create(sssObj("dd", "DeepDesert_1", 1))
	old := spawnOneLive(t, spw, "default/dd")
	spw.terminate = func(int, time.Duration) error { return fmt.Errorf("simulated kill failure") }

	if _, err := spw.Recycle("default/dd", old.Suffix); err == nil {
		t.Fatal("Recycle returned nil although the old process could not be stopped")
	}
	spw.reconcileTick()
	spw.Wait()

	spw.mu.Lock()
	list := append([]instance(nil), spw.instances["default/dd"]...)
	spw.mu.Unlock()
	if len(list) != 1 || list[0].PID != old.PID {
		t.Fatalf("after a failed recycle want the incumbent (pid %d) alone, got %+v", old.PID, list)
	}
	if used, _, _ := pl.Stats(); used != 1 {
		t.Errorf("pool used = %d, want 1 (no replacement, incumbent keeps its slot)", used)
	}
	_ = syscall.Kill(old.PID, syscall.SIGKILL)
}

func TestRecycle_RefusesUnknownAndStartingInstances(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	if _, err := spw.Recycle("default/nope", "p0"); err == nil {
		t.Error("Recycle of an untracked instance returned nil")
	}

	spw.mu.Lock()
	spw.instances["default/dd"] = []instance{{Suffix: "p3", MapName: "DeepDesert_1", pidReady: make(chan struct{})}}
	spw.mu.Unlock()
	if _, err := spw.Recycle("default/dd", "p3"); err == nil {
		t.Error("Recycle of an instance still starting (pid 0) returned nil")
	}
	spw.mu.Lock()
	n := len(spw.instances["default/dd"])
	spw.mu.Unlock()
	if n != 1 {
		t.Errorf("a refused recycle changed the ledger: %d instances, want 1", n)
	}
}

func TestInstancesOf_ListsOnlyThatMap(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.mu.Lock()
	spw.instances["default/dd"] = []instance{{Suffix: "p1", MapName: "DeepDesert_1", PID: 11}}
	spw.instances["default/dd2"] = []instance{{Suffix: "p2", MapName: "DeepDesert_1", PID: 0}}
	spw.instances["default/hagga"] = []instance{{Suffix: "p0", MapName: "Survival_1", PID: 10}}
	spw.mu.Unlock()

	got := spw.InstancesOf("DeepDesert_1")
	if len(got) != 2 {
		t.Fatalf("InstancesOf(DeepDesert_1) = %+v, want the two DD instances", got)
	}
	for _, r := range got {
		if r.Key == "default/hagga" {
			t.Errorf("Hagga listed as a Deep Desert instance: %+v", r)
		}
	}
}

// A map whose ServerSetScale vanished during the shutdown gets no
// replacement, and Recycle says so: the caller must not wait for one.
func TestRecycle_ReportsNoRespawnWhenScaleGone(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("dd", "DeepDesert_1", 1))
	old := spawnOneLive(t, spw, "default/dd")
	spw.terminate = func(pid int, grace time.Duration) error {
		spw.store.Delete("default", "dd")
		return proc.Terminate(pid, grace)
	}
	respawned, err := spw.Recycle("default/dd", old.Suffix)
	if err != nil {
		t.Fatalf("Recycle: %v", err)
	}
	if respawned {
		t.Fatal("Recycle reported a respawn although the ServerSetScale is gone")
	}
}

// A Director scale-down followed quickly by a scale-up must not start the new
// server while the old one is still shutting down: a UE5 in PreShutdown is
// still in the farm on its partition.
func TestScaleDown_NoRespawnUntilOldProcessGone(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("dd", "DeepDesert_1", 1))
	spw.store.OnSpecChange = spw.OnSpecChange
	old := spawnOneLive(t, spw, "default/dd")

	var duringShutdown = -1
	release := make(chan struct{})
	spw.terminate = func(pid int, grace time.Duration) error {
		<-release // the old UE5 is still shutting down
		return proc.Terminate(pid, grace)
	}

	obj, _ := spw.store.Get("default", "dd")
	obj.Spec["replicas"] = int64(0)
	spw.OnSpecChange(obj) // scale-down: teardown starts, blocked on release

	obj.Spec["replicas"] = int64(1)
	spw.OnSpecChange(obj) // the Director changes its mind
	spw.reconcileTick()
	spw.mu.Lock()
	duringShutdown = len(spw.instances["default/dd"])
	spw.mu.Unlock()
	close(release)
	spw.Wait()
	waitDead(old.PID)

	if duringShutdown != 0 {
		t.Fatalf("%d replacement(s) started while the scaled-down UE5 was still shutting down", duringShutdown)
	}
	spw.reconcileTick() // once it is gone, the map comes back
	spw.Wait()
	spw.mu.Lock()
	after := len(spw.instances["default/dd"])
	spw.mu.Unlock()
	if after != 1 {
		t.Errorf("tracked %d after the old process exited, want 1", after)
	}
}
