package spawner

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// Game/IGW ports are derived from poolIndex via the pool base, so persisting
// them is redundant dead data that silently goes stale if the pool base
// changes across a restart (Restore re-derives ports from poolIndex via
// AcquireSpecific and never reads the persisted ones). The ledger must not
// carry them.
func TestPersist_LedgerOmitsRedundantPortFields(t *testing.T) {
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)

	alloc, _ := pl.Acquire()
	spw.mu.Lock()
	spw.instances["k"] = []instance{{Suffix: "p0", MapName: "Survival_1", Allocation: alloc, PID: 4242}}
	spw.mu.Unlock()
	spw.persist()

	raw, err := os.ReadFile(spw.statePath)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"gamePort", "igwPort"} {
		if strings.Contains(string(raw), k) {
			t.Errorf("ledger persists redundant %q key (derive from poolIndex instead):\n%s", k, raw)
		}
	}
}
