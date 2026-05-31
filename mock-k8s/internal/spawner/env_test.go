package spawner

import (
	"strings"
	"testing"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
)

func effectiveEnv(env []string) map[string]string {
	m := map[string]string{}
	for _, kv := range env {
		if i := strings.IndexByte(kv, '='); i >= 0 {
			m[kv[:i]] = kv[i+1:] // last occurrence wins, as getenv resolves it
		}
	}
	return m
}

func countEnvKey(env []string, key string) int {
	n := 0
	for _, kv := range env {
		if i := strings.IndexByte(kv, '='); i >= 0 && kv[:i] == key {
			n++
		}
	}
	return n
}

// The per-instance DUNE_* port/partition vars must override anything inherited
// from mock-k8s's own environment — otherwise a single stray DUNE_GAME_PORT in
// the container env collapses every UE5 instance onto the same UDP port. The
// inherited copy must be dropped entirely (not just shadowed), so the child's
// getenv can't pick it up regardless of duplicate-resolution order.
func TestSpawnEnv_PerInstancePortsBeatInheritedEnv(t *testing.T) {
	t.Setenv("DUNE_GAME_PORT", "1111")
	t.Setenv("DUNE_IGW_PORT", "2222")
	t.Setenv("DUNE_PARTITION", "9")

	env := spawnEnv(5, pool.Allocation{Index: 3, GamePort: 7903, IGWPort: 7953})

	eff := effectiveEnv(env)
	for k, want := range map[string]string{
		"DUNE_PARTITION": "5",
		"DUNE_GAME_PORT": "7903",
		"DUNE_IGW_PORT":  "7953",
	} {
		if eff[k] != want {
			t.Errorf("effective %s = %q, want %q (per-instance value must win)", k, eff[k], want)
		}
		if n := countEnvKey(env, k); n != 1 {
			t.Errorf("%s appears %d times in env, want exactly 1 (inherited copy must be dropped)", k, n)
		}
	}

	// Unrelated inherited vars must still flow through (the child needs $PATH).
	if eff["PATH"] == "" {
		t.Error("spawnEnv dropped inherited PATH")
	}
}
