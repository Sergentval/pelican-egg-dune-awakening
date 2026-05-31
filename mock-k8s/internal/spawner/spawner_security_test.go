package spawner

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// A ServerSetScale whose mapName contains path-traversal characters must
// never cause a spawn — otherwise start-ue5.sh would write a pidfile (and
// mock-k8s would later read/delete one) outside the runtime/pids directory.
func TestSpawner_RefusesUnsafeMapName(t *testing.T) {
	spw, pl, store, _ := newTestSpawner(t)

	if _, ok := store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "sietch-evil"},
		Spec: map[string]any{
			"mapName":     "../../escape",
			"partitionId": int64(1),
			"replicas":    int64(1),
		},
	}); !ok {
		t.Fatal("create failed")
	}
	spw.Wait()

	if used, _, _ := pl.Stats(); used != 0 {
		t.Fatalf("unsafe map name spawned an instance: pool used = %d, want 0", used)
	}
	if got := spw.testPIDs("default/sietch-evil"); len(got) != 0 {
		t.Fatalf("unsafe map name tracked instances: %v", got)
	}
}

// Even if a malformed name slips through, pidPath must never resolve outside
// the runtime/pids directory.
func TestSpawner_PidPathIsContained(t *testing.T) {
	spw, _, _, base := newTestSpawner(t)
	pidsDir := filepath.Clean(filepath.Join(base, "runtime", "pids"))

	for _, evil := range []string{"../../etc/cron.d/x", "a/b/c", "..", "../escape"} {
		got := filepath.Clean(spw.pidPath(evil, "p0"))
		if !strings.HasPrefix(got, pidsDir+string(os.PathSeparator)) {
			t.Errorf("pidPath(%q) = %q escaped %q", evil, got, pidsDir)
		}
	}
}
