package spawner

import (
	"path/filepath"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/state"
)

// persist() snapshots under s.mu but writes under saveMu, so two concurrent
// calls can snapshot in one order yet win the saveMu race in the reverse
// order — letting an older snapshot overwrite a newer ledger. A monotonic
// generation guard must make the write skip when its snapshot generation is
// no newer than the one already on disk.
func TestPersist_SkipsStaleGenerationWrite(t *testing.T) {
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	a0, _ := pl.Acquire()
	a1, _ := pl.Acquire()
	spw.mu.Lock()
	spw.instances["k"] = []instance{
		{Suffix: "p0", MapName: "Survival_1", Allocation: a0, PID: 111},
		{Suffix: "p1", MapName: "Survival_1", Allocation: a1, PID: 222},
	}
	spw.mu.Unlock()

	// The "newer" write: two instances committed to disk.
	spw.persist()
	if got, err := state.Load(spw.statePath); err != nil || len(got.Instances) != 2 {
		t.Fatalf("setup: want 2 instances on disk, got %d (err=%v)", len(got.Instances), err)
	}

	// Simulate a persist that snapshotted an OLDER one-instance state and lost
	// the saveMu race: rewind persistGen so its generation lands <= the one
	// already committed. The guard must skip the write and keep the 2-entry
	// ledger.
	spw.mu.Lock()
	spw.instances["k"] = spw.instances["k"][:1]
	spw.persistGen = spw.lastSavedGen - 1
	spw.mu.Unlock()
	spw.persist()

	got, err := state.Load(spw.statePath)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Instances) != 2 {
		t.Fatalf("stale-generation persist clobbered the newer ledger: got %d instances, want 2", len(got.Instances))
	}
}
