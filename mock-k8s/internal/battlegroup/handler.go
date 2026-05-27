package battlegroup

import (
	"encoding/json"
	"net/http"
	"strings"
)

// Handler returns the http.HandlerFunc tree rooted at the BattleGroup
// segment of the API group. It is mounted under the same prefix as
// serversetscale (/apis/igw.funcom.com/v1/) by main.go, so the mux
// dispatches to either handler based on the resource plural in the URL.
//
// Routes:
//
//	GET    /apis/igw.funcom.com/v1/battlegroups                            list cluster-wide
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroups            list namespace
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroups/{name}     get one
//
// PUT/PATCH/POST/DELETE intentionally NOT implemented yet — the Director
// is a read-only consumer; the BattleGroup is owned by us (loaded from
// world-template.yaml at startup). If we ever need to support runtime
// edits via the panel, this is the place to add them.
func Handler(s *Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/apis/igw.funcom.com/v1")
		path = strings.TrimPrefix(path, "/")

		// Cluster-scoped list.
		if path == "battlegroups" {
			handleList(s, "", w, r)
			return
		}

		parts := strings.Split(path, "/")
		if len(parts) >= 3 && parts[0] == "namespaces" && parts[2] == "battlegroups" {
			ns := parts[1]
			switch len(parts) {
			case 3:
				handleList(s, ns, w, r)
				return
			case 4:
				handleGet(s, ns, parts[3], w, r)
				return
			}
		}

		http.NotFound(w, r)
	}
}

func handleList(s *Store, ns string, w http.ResponseWriter, _ *http.Request) {
	items := s.List(ns)
	if items == nil {
		items = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "BattleGroupList",
		"metadata": map[string]any{
			"resourceVersion": s.CurrentResourceVersion(),
		},
		"items": items,
	})
}

func handleGet(s *Store, ns, name string, w http.ResponseWriter, _ *http.Request) {
	obj, ok := s.Get(ns, name)
	if !ok {
		writeStatus(w, http.StatusNotFound, "battlegroup not found", name)
		return
	}
	writeJSON(w, http.StatusOK, obj)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeStatus(w http.ResponseWriter, code int, msg, name string) {
	writeJSON(w, code, map[string]any{
		"kind":       "Status",
		"apiVersion": "v1",
		"metadata":   map[string]any{},
		"status":     "Failure",
		"message":    msg,
		"reason":     "NotFound",
		"details": map[string]any{
			"name":  name,
			"group": "igw.funcom.com",
			"kind":  "battlegroups",
		},
		"code": code,
	})
}
