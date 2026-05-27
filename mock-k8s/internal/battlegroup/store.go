package battlegroup

import (
	"strconv"
	"sync"
)

// Store is a single-resource registry for the BattleGroup CR. Unlike the
// ServerSetScale store this is effectively a singleton — the Director
// only ever queries ONE BattleGroup per world. Modeling it as a map
// keyed by name keeps the semantics consistent with the K8s API
// (namespaces/<ns>/battlegroups/<name>) for free.
type Store struct {
	mu              sync.Mutex
	objects         map[key]map[string]any
	resourceVersion int64
}

type key struct{ namespace, name string }

// NewStore returns an empty Store.
func NewStore() *Store {
	return &Store{objects: make(map[key]map[string]any)}
}

// Put inserts or replaces an object. resourceVersion is auto-stamped so
// the Director sees a monotonic version even across mock-k8s restarts.
func (s *Store) Put(obj map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ns, name := metaStr(obj, "namespace"), metaStr(obj, "name")
	s.resourceVersion++
	if meta, ok := obj["metadata"].(map[string]any); ok {
		meta["resourceVersion"] = strconv.FormatInt(s.resourceVersion, 10)
	}
	s.objects[key{ns, name}] = obj
}

// Get returns one BattleGroup by (namespace, name).
func (s *Store) Get(namespace, name string) (map[string]any, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.objects[key{namespace, name}]
	return o, ok
}

// List returns every BattleGroup in the given namespace (empty namespace
// = list across all namespaces, matching K8s cluster-scoped list semantics).
func (s *Store) List(namespace string) []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []map[string]any
	for k, v := range s.objects {
		if namespace != "" && k.namespace != namespace {
			continue
		}
		out = append(out, v)
	}
	return out
}

// CurrentResourceVersion returns the cluster-wide RV stamp for ?resourceVersion=
// pagination cursors and watch resume tokens.
func (s *Store) CurrentResourceVersion() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strconv.FormatInt(s.resourceVersion, 10)
}

func metaStr(obj map[string]any, field string) string {
	meta, ok := obj["metadata"].(map[string]any)
	if !ok {
		return ""
	}
	s, _ := meta[field].(string)
	return s
}
