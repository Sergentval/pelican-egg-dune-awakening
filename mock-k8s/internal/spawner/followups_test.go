package spawner

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/state"
)

// testInstances returns copies of the tracked instances for a key.
func (s *Spawner) testInstances(key string) []instance {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]instance(nil), s.instances[key]...)
}

// #4: a ledger pid that is alive but whose recorded start-time doesn't match
// the live process (a recycled pid) must NOT be adopted.
func TestRestore_RejectsRecycledPid(t *testing.T) {
	base := t.TempDir()
	live := startSleeper(t)
	writePidfile(t, base, "Survival_1", "p0", live)
	writeLedger(t, base, state.Instance{
		Key: "default/sietch-survival", MapName: "Survival_1", PartitionID: 1,
		PoolIndex: 0, GamePort: 7900, IGWPort: 7950, Suffix: "p0", PID: live,
		StartTime: 999999999999, // deliberately wrong → looks recycled
	})
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore()

	if used, _, _ := pl.Stats(); used != 0 {
		t.Fatalf("recycled pid was adopted: pool used = %d, want 0", used)
	}
	if got := spw.testPIDs("default/sietch-survival"); len(got) != 0 {
		t.Fatalf("recycled pid tracked: %v", got)
	}
}

// #8: when K8S_POOL_*_BASE changed across the restart, restore reports the
// persisted ports the live UE5 actually bound to, not the recomputed ones.
func TestRestore_PrefersPersistedPorts(t *testing.T) {
	base := t.TempDir()
	live := startSleeper(t)
	st, ok := proc.StartTime(live)
	if !ok {
		t.Fatal("could not read sleeper start-time")
	}
	writePidfile(t, base, "Survival_1", "p0", live)
	writeLedger(t, base, state.Instance{
		Key: "default/sietch-survival", MapName: "Survival_1", PartitionID: 1,
		PoolIndex: 0, GamePort: 8000, IGWPort: 8050, Suffix: "p0", PID: live, StartTime: st,
	})
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5) // index 0 recomputes 7900/7950, ledger says 8000/8050
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore()

	insts := spw.testInstances("default/sietch-survival")
	if len(insts) != 1 {
		t.Fatalf("adopted %d instances, want 1", len(insts))
	}
	if insts[0].Allocation.GamePort != 8000 || insts[0].Allocation.IGWPort != 8050 {
		t.Errorf("restored ports = %d/%d, want persisted 8000/8050",
			insts[0].Allocation.GamePort, insts[0].Allocation.IGWPort)
	}
}

// #7: a transient (unreadable) ledger must NOT be quarantined or destroyed —
// it stays in place for the next boot to retry.
func TestRestore_TransientIODoesNotQuarantine(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses file permission bits")
	}
	base := t.TempDir()
	sp := filepath.Join(base, "server", "state", "mock-k8s-state.json")
	if err := os.MkdirAll(filepath.Dir(sp), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(sp, []byte(`{"version":1,"instances":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(sp, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(sp, 0o644) })

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore() // must not panic, must not rename

	_ = os.Chmod(sp, 0o644)
	if _, err := os.Stat(sp); err != nil {
		t.Errorf("ledger removed/renamed on transient error: %v", err)
	}
	if m, _ := filepath.Glob(sp + ".corrupt.*"); len(m) != 0 {
		t.Errorf("transient I/O wrongly quarantined the ledger: %v", m)
	}
}
