package spawner

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// A corrupt/unparseable ledger must not crash Restore or be silently
// discarded — it is quarantined (renamed) so the operator can inspect it,
// and the spawner starts fresh.
func TestSpawner_RestoreQuarantinesCorruptLedger(t *testing.T) {
	base := t.TempDir()
	sp := filepath.Join(base, "server", "state", "mock-k8s-state.json")
	if err := os.MkdirAll(filepath.Dir(sp), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(sp, []byte("{ this is not valid json"), 0o644); err != nil {
		t.Fatal(err)
	}

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore() // must not panic

	if _, err := os.Stat(sp); !os.IsNotExist(err) {
		t.Errorf("corrupt ledger left at original path (stat err=%v); want it quarantined", err)
	}
	matches, _ := filepath.Glob(sp + ".corrupt.*")
	if len(matches) == 0 {
		t.Errorf("no quarantined .corrupt.* file was created")
	}
	if used, _, _ := pl.Stats(); used != 0 {
		t.Errorf("pool should be empty after a corrupt-ledger restore, got %d used", used)
	}
}

// A transient I/O error reading the ledger (e.g. EACCES, EIO) must NOT be
// treated as corruption: the ledger may be perfectly valid, so Restore leaves
// it in place (no rename) and skips this boot's restore so a later boot can
// still re-adopt the live instances it records. Quarantining it here would
// destroy a healthy ledger and orphan every running UE5.
func TestSpawner_RestoreIOErrorPreservesLedger(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("chmod 000 does not deny root; cannot simulate a transient I/O error")
	}
	base := t.TempDir()
	sp := filepath.Join(base, "server", "state", "mock-k8s-state.json")
	if err := os.MkdirAll(filepath.Dir(sp), 0o755); err != nil {
		t.Fatal(err)
	}
	// A structurally valid ledger — the failure is I/O, not parse.
	if err := os.WriteFile(sp, []byte(`{"version":1,"instances":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(sp, 0o000); err != nil { // force EACCES on read
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(sp, 0o644) })

	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	spw.Restore() // must not panic

	if _, err := os.Stat(sp); err != nil {
		t.Errorf("valid-but-unreadable ledger was not preserved (stat err=%v); a transient I/O error must not quarantine it", err)
	}
	if matches, _ := filepath.Glob(sp + ".corrupt.*"); len(matches) != 0 {
		t.Errorf("ledger was quarantined on a transient I/O error: %v", matches)
	}
}
