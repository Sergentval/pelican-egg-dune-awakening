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
