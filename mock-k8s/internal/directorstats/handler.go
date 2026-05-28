// Package directorstats serves the battlegroupdirectorstats Custom
// Resource the Funcom Battlegroup Director publishes to ~every 30s.
//
// The Director PATCHes its own runtime statistics (player counts, world
// revision, partition health snapshots) into a CR that, in a real K8s,
// would land in etcd for cluster operators to read. We have no real
// persistence layer for these stats and nothing downstream consumes
// them, but the Director treats a 404 as a hard error and logs:
//
//	[director] [ERROR] IGWO DirectorStats request failed with HTTP
//	  status code "NotFound", took 0.003 seconds.
//
// followed by a "Slow operation: Reload settings: 85 ms" because the
// failure round-trips through the K8s client. This package accepts and
// echoes the PATCHes so both warnings go silent. Memory-only by design.
package directorstats

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
)

// Store holds the latest stats body the Director has PATCHed for each
// (namespace, name) pair. Memory-only; restart re-derives.
type Store struct {
	mu    sync.Mutex
	items map[string]map[string]any
}

// NewStore returns an empty in-memory stats store.
func NewStore() *Store {
	return &Store{items: make(map[string]map[string]any)}
}

func (s *Store) get(ns, name string) (map[string]any, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	obj, ok := s.items[ns+"/"+name]
	return obj, ok
}

func (s *Store) put(ns, name string, obj map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items[ns+"/"+name] = obj
}

func (s *Store) listNS(ns string) []map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]map[string]any, 0, len(s.items))
	prefix := ns + "/"
	for k, v := range s.items {
		if strings.HasPrefix(k, prefix) && v != nil {
			out = append(out, v)
		}
	}
	return out
}

// Handler returns the http.HandlerFunc rooted at the directorstats
// segment of the IGW API group. main.go dispatches to it when the URL
// path contains "/battlegroupdirectorstats".
//
// Routes (all return 200 unless noted):
//
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	PATCH  /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	PUT    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	POST   /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats
//	DELETE /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
func Handler(s *Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/apis/igw.funcom.com/v1")
		path = strings.TrimPrefix(path, "/")
		parts := strings.Split(path, "/")

		// Required shape: /namespaces/{ns}/battlegroupdirectorstats[/{name}]
		if len(parts) < 3 || parts[0] != "namespaces" || parts[2] != "battlegroupdirectorstats" {
			http.NotFound(w, r)
			return
		}
		ns := parts[1]
		name := ""
		if len(parts) >= 4 {
			name = parts[3]
		}

		switch r.Method {
		case http.MethodGet:
			if name == "" {
				writeJSON(w, http.StatusOK, listEnvelope(s, ns))
				return
			}
			obj, ok := s.get(ns, name)
			if !ok {
				obj = stubObj(ns, name)
			}
			writeJSON(w, http.StatusOK, obj)

		case http.MethodPatch, http.MethodPut:
			obj := mergeBodyOrStub(r, ns, name)
			s.put(ns, name, obj)
			writeJSON(w, http.StatusOK, obj)

		case http.MethodPost:
			// POST creates; name comes from the request body's metadata.name.
			body, _ := io.ReadAll(r.Body)
			obj := stubObj(ns, name)
			var bodyJSON map[string]any
			if len(body) > 0 {
				_ = json.Unmarshal(body, &bodyJSON)
			}
			if metaMap, ok := bodyJSON["metadata"].(map[string]any); ok {
				if n, ok := metaMap["name"].(string); ok && n != "" {
					name = n
					obj = stubObj(ns, name)
				}
			}
			for k, v := range bodyJSON {
				obj[k] = v
			}
			s.put(ns, name, obj)
			writeJSON(w, http.StatusCreated, obj)

		case http.MethodDelete:
			s.put(ns, name, nil)
			writeJSON(w, http.StatusOK, map[string]any{
				"kind":       "Status",
				"apiVersion": "v1",
				"status":     "Success",
				"code":       200,
			})

		default:
			slog.Warn("directorstats: unsupported method", "method", r.Method, "path", r.URL.Path)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		}
	}
}

// mergeBodyOrStub reads the request body, parses it as JSON if possible,
// and merges into a fresh stub envelope. Best-effort: a non-JSON body
// (Director may send strategic-merge-patch+json or json-patch+json with
// different shapes) still yields a valid stub so the Director sees 200.
func mergeBodyOrStub(r *http.Request, ns, name string) map[string]any {
	obj := stubObj(ns, name)
	body, _ := io.ReadAll(r.Body)
	if len(body) == 0 {
		return obj
	}
	var bodyJSON map[string]any
	if err := json.Unmarshal(body, &bodyJSON); err != nil {
		// Body wasn't JSON-object — could be a JSON Patch array
		// ([{op,path,value}]) for example. Echo the stub; the Director
		// only cares about the 2xx status, not the response shape.
		return obj
	}
	for k, v := range bodyJSON {
		obj[k] = v
	}
	return obj
}

func stubObj(ns, name string) map[string]any {
	return map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "BattleGroupDirectorStats",
		"metadata": map[string]any{
			"name":            name,
			"namespace":       ns,
			"resourceVersion": "1",
		},
	}
}

func listEnvelope(s *Store, ns string) map[string]any {
	return map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "BattleGroupDirectorStatsList",
		"metadata":   map[string]any{"resourceVersion": "1"},
		"items":      s.listNS(ns),
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
