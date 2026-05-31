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

// safeInstanceName must be an allowlist, not a denylist: it has to accept the
// real Funcom map names and generated suffixes while rejecting newlines (log
// injection), leading dashes (argv flag injection), whitespace, and shell
// metacharacters that a denylist of only "/ \ .." lets through.
func TestSafeInstanceName_Allowlist(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want bool
	}{
		// accept — real map names + generated suffixes
		{"map Survival_1", "Survival_1", true},
		{"map Overmap", "Overmap", true},
		{"map DeepDesert_1", "DeepDesert_1", true},
		{"map SH_FallenLight", "SH_FallenLight", true},
		{"suffix p0", "p0", true},
		{"suffix p42", "p42", true},
		{"single alpha", "A", true},
		{"dotted", "Map.v2", true},
		{"hyphenated", "Map-v2", true},
		{"dotdot inside a component", "foo..bar", true}, // no separator → no traversal

		// reject — security-relevant inputs
		{"empty", "", false},
		{"newline log-injection", "Survival_1\nINFO [mock-k8s] forged", false},
		{"embedded newline", "foo\nbar", false},
		{"leading dash arg-injection", "-rf", false},
		{"space", "Survival 1", false},
		{"tab", "Survival\t1", false},
		{"semicolon", "foo;bar", false},
		{"pipe", "foo|bar", false},
		{"dollar", "foo$bar", false},
		{"backtick", "foo`bar", false},
		{"ampersand", "foo&bar", false},
		{"forward slash", "../../etc/passwd", false},
		{"backslash", "foo\\bar", false},
		{"bare dotdot", "..", false},
		{"leading dot", ".hidden", false},
		{"glob star", "foo*bar", false},
		{"null byte", "foo\x00bar", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := safeInstanceName(tc.in); got != tc.want {
				t.Errorf("safeInstanceName(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}
