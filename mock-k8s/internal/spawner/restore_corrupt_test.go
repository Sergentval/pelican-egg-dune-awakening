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
