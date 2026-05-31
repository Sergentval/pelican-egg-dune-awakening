package spawner

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// newBareSpawner builds a Spawner with a fixed, controllable clock for tests.
func newBareSpawner(t *testing.T) (*Spawner, *fakeClock) {
	t.Helper()
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	spw.now = clk.Now
	spw.startedAt = clk.Now()
	return spw, clk
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) Now() time.Time          { return c.t }
func (c *fakeClock) Advance(d time.Duration) { c.t = c.t.Add(d) }

func TestBackoff_CurveAndReset(t *testing.T) {
	spw, clk := newBareSpawner(t)
	const k = "default/m"

	// First failure: retry immediately (no backoff window).
	spw.recordFailure(k)
	if spw.inBackoff(k) {
		t.Errorf("after 1 failure: inBackoff=true, want false (retry next tick)")
	}

	// Second failure: 1 minute window.
	spw.recordFailure(k)
	if !spw.inBackoff(k) {
		t.Fatalf("after 2 failures: inBackoff=false, want true")
	}
	clk.Advance(59 * time.Second)
	if !spw.inBackoff(k) {
		t.Errorf("59s into a 1m backoff: inBackoff=false, want true")
	}
	clk.Advance(2 * time.Second) // now 61s > 60s
	if spw.inBackoff(k) {
		t.Errorf("61s into a 1m backoff: inBackoff=true, want false")
	}

	// Reset clears the state.
	spw.resetBackoff(k)
	spw.recordFailure(k)
	if spw.inBackoff(k) {
		t.Errorf("after reset+1 failure: inBackoff=true, want false")
	}
}

func TestBackoff_CapsAtMax(t *testing.T) {
	spw, clk := newBareSpawner(t)
	const k = "default/m"
	for i := 0; i < 20; i++ {
		spw.recordFailure(k)
	}
	if !spw.inBackoff(k) {
		t.Fatal("expected to be in backoff after many failures")
	}
	clk.Advance(maxBackoff - time.Second)
	if !spw.inBackoff(k) {
		t.Errorf("just before maxBackoff: inBackoff=false, want true")
	}
	clk.Advance(2 * time.Second)
	if spw.inBackoff(k) {
		t.Errorf("just after maxBackoff: inBackoff=true, want false (capped at %s)", maxBackoff)
	}
}

func TestSnapshot_ReflectsPoolInstancesAndMaps(t *testing.T) {
	spw, _ := newBareSpawner(t)

	// One ServerSetScale desiring 1 replica, with one live tracked instance.
	spw.store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "m"},
		Spec:     map[string]any{"mapName": "Survival_1", "replicas": int64(1)},
	})
	alloc, _ := spw.pool.Acquire()
	spw.mu.Lock()
	spw.instances["default/m"] = []instance{{Suffix: "p0", MapName: "Survival_1", Allocation: alloc, PID: 1234, StartTime: 99}}
	spw.reapedTotal = 2
	spw.respawnedTotal = 2
	spw.mu.Unlock()

	snap := spw.Snapshot()

	if snap.Pool.Used != 1 || snap.Pool.Size != 5 {
		t.Errorf("pool used/size = %d/%d, want 1/5", snap.Pool.Used, snap.Pool.Size)
	}
	if snap.Instances.Tracked != 1 || snap.Instances.ReapedTotal != 2 || snap.Instances.RespawnedTotal != 2 {
		t.Errorf("instances = %+v, want tracked=1 reaped=2 respawned=2", snap.Instances)
	}
	if len(snap.Maps) != 1 {
		t.Fatalf("maps = %d, want 1", len(snap.Maps))
	}
	m := snap.Maps[0]
	if m.Map != "Survival_1" || m.Desired != 1 || m.Current != 1 || m.Status != "healthy" {
		t.Errorf("map = %+v, want Survival_1 desired=1 current=1 healthy", m)
	}
}

func TestSnapshot_FailingMapShowsBackoff(t *testing.T) {
	spw, _ := newBareSpawner(t)
	spw.store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "m"},
		Spec:     map[string]any{"mapName": "Overmap", "replicas": int64(1)},
	})
	// Desired 1, current 0, and the map is in backoff -> "failing".
	spw.recordFailure("default/m")
	spw.recordFailure("default/m") // failures=2 -> 1m window

	snap := spw.Snapshot()
	m := snap.Maps[0]
	if m.Status != "failing" {
		t.Errorf("status = %q, want failing", m.Status)
	}
	if m.ConsecutiveFailures != 2 {
		t.Errorf("consecutiveFailures = %d, want 2", m.ConsecutiveFailures)
	}
	if m.NextRetry == nil {
		t.Error("nextRetry = nil, want a time for a failing map")
	}
}

func TestPersist_CountsErrors(t *testing.T) {
	spw, _ := newBareSpawner(t)
	// Point statePath at an un-writable location to force a Save error.
	spw.statePath = filepath.Join(t.TempDir(), "nope", "\x00bad", "state.json")
	spw.persist()
	snap := spw.Snapshot()
	if snap.Persist.Errors == 0 {
		t.Error("persist error not counted")
	}
	if snap.Persist.LastError == "" {
		t.Error("lastPersistError not recorded")
	}
}
