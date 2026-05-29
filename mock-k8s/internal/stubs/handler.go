// Package stubs implements the two Funcom Battlegroup Director IGW
// resources that mock-k8s previously 404'd on:
//
//   - battlegroupdirectorstats — Director PATCHes this every ~10s with
//     per-partition player counts and activity ratios. Real K8s would
//     persist it for FLS telemetry to drain; we only need to accept the
//     write so Director's CCS reporter doesn't keep its in-memory queue
//     of un-acked PATCHes (which eventually starves Director's main
//     reconcile loop and blocks partition allocations).
//
//   - serversets — read-only enumeration of physical UE5 partition
//     instances. Director consumes this to learn which partitions are
//     live and route cross-Sietch travel handovers to a specific
//     instance. Returning an empty list still satisfies the request
//     shape but causes Director to treat every map as "no live
//     instance" and refuse handovers. We synthesize one ServerSet per
//     populated ServerSetScale instead — Director gets a non-empty
//     enumeration and starts allocating slots.
//
// Both handlers are deliberately minimal: no persistence beyond the
// last-seen stats blob (used by /healthz introspection), no spec
// reconciliation, no watch streams. They exist purely to flip the
// Director's reconcile loop from "missing dependencies" to "ready".
package stubs

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// StatsStore retains the most recent stats PATCH per battlegroup name.
// Lookups are exposed only to in-process observers (e.g. an admin
// /healthz/stats endpoint we may add later); the Director never reads
// stats back from K8s — it pushes one-way.
type StatsStore struct {
	mu     sync.Mutex
	latest map[string]map[string]any // (namespace + "/" + name) → status blob
}

// NewStatsStore returns an empty StatsStore.
func NewStatsStore() *StatsStore {
	return &StatsStore{latest: make(map[string]map[string]any)}
}

// Snapshot copies the current per-battlegroup status blobs for read.
// Used by /healthz/stats; not on Director's hot path.
func (s *StatsStore) Snapshot() map[string]map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]map[string]any, len(s.latest))
	for k, v := range s.latest {
		out[k] = v
	}
	return out
}

func (s *StatsStore) put(namespace, name string, status map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.latest[namespace+"/"+name] = status
}

// BattlegroupDirectorStatsHandler returns an http.HandlerFunc serving
//
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	PUT    /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	PATCH  /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats/{name}
//	POST   /apis/igw.funcom.com/v1/namespaces/{ns}/battlegroupdirectorstats
//
// The Director PATCHes a single object per battlegroup; PUT and POST
// are accepted defensively in case client-library version drift sends
// either. GET returns the last-seen blob so debugging tools can
// inspect what the Director thinks the world looks like.
func BattlegroupDirectorStatsHandler(store *StatsStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ns, name, ok := parseBgdsPath(r.URL.Path)
		if !ok {
			http.NotFound(w, r)
			return
		}
		switch r.Method {
		case http.MethodGet:
			handleStatsGet(store, ns, name, w)
		case http.MethodPut, http.MethodPatch, http.MethodPost:
			handleStatsWrite(store, ns, name, w, r)
		case http.MethodDelete:
			// Real K8s would 200 with the deleted object; we just ack.
			writeOK(w, statsObject(ns, name, nil))
		default:
			w.Header().Set("Allow", "GET, PUT, PATCH, POST, DELETE")
			writeStatus(w, http.StatusMethodNotAllowed,
				"battlegroupdirectorstats", name,
				"method not allowed")
		}
	}
}

func parseBgdsPath(path string) (namespace, name string, ok bool) {
	path = strings.TrimPrefix(path, "/apis/igw.funcom.com/v1")
	path = strings.TrimPrefix(path, "/")
	parts := strings.Split(path, "/")
	// Forms we accept:
	//   battlegroupdirectorstats                          (cluster list)
	//   namespaces/{ns}/battlegroupdirectorstats          (list / create)
	//   namespaces/{ns}/battlegroupdirectorstats/{name}   (one)
	if len(parts) == 1 && parts[0] == "battlegroupdirectorstats" {
		return "", "", true
	}
	if len(parts) >= 3 && parts[0] == "namespaces" && parts[2] == "battlegroupdirectorstats" {
		switch len(parts) {
		case 3:
			return parts[1], "", true
		case 4:
			return parts[1], parts[3], true
		}
	}
	return "", "", false
}

func handleStatsGet(store *StatsStore, ns, name string, w http.ResponseWriter) {
	if name == "" {
		// List form. Cheap synthesis from the in-memory map — we
		// don't bother filtering by namespace because every PATCH so
		// far has come from the default namespace.
		snap := store.Snapshot()
		items := make([]map[string]any, 0, len(snap))
		for k, status := range snap {
			parts := strings.SplitN(k, "/", 2)
			if len(parts) != 2 || (ns != "" && parts[0] != ns) {
				continue
			}
			items = append(items, statsObject(parts[0], parts[1], status))
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"apiVersion": "igw.funcom.com/v1",
			"kind":       "BattleGroupDirectorStatsList",
			"metadata":   map[string]any{"resourceVersion": "1"},
			"items":      items,
		})
		return
	}
	snap := store.Snapshot()
	status := snap[ns+"/"+name]
	writeOK(w, statsObject(ns, name, status))
}

func handleStatsWrite(store *StatsStore, ns, name string, w http.ResponseWriter, r *http.Request) {
	if name == "" {
		writeStatus(w, http.StatusBadRequest, "battlegroupdirectorstats", "",
			"name required for write")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeStatus(w, http.StatusBadRequest, "battlegroupdirectorstats", name,
			fmt.Sprintf("read body: %v", err))
		return
	}
	var payload map[string]any
	if len(body) == 0 {
		payload = map[string]any{}
	} else if err := json.Unmarshal(body, &payload); err != nil {
		writeStatus(w, http.StatusBadRequest, "battlegroupdirectorstats", name,
			fmt.Sprintf("decode body: %v", err))
		return
	}
	status, _ := payload["status"].(map[string]any)
	if status == nil {
		// Some clients PATCH with just the bare status fields; store
		// the whole payload so debugging tools see exactly what
		// Director sent.
		status = payload
	}
	store.put(ns, name, status)
	writeOK(w, statsObject(ns, name, status))
}

func statsObject(ns, name string, status map[string]any) map[string]any {
	obj := map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "BattleGroupDirectorStats",
		"metadata": map[string]any{
			"name":            name,
			"namespace":       ns,
			"resourceVersion": "1",
		},
	}
	if status != nil {
		obj["status"] = status
	}
	return obj
}

// ServerSetsHandler returns an http.HandlerFunc serving
//
//	GET    /apis/igw.funcom.com/v1/serversets
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/serversets
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/serversets/{name}
//
// The Director uses these to enumerate physical partition instances.
// We synthesize ServerSet objects on the fly from the ServerSetScale
// store — each scaled-up partition (status.completedReplicas > 0)
// gets one ServerSet entry whose metadata.name mirrors the SSS name
// with a "-0" instance suffix (Director only ever runs one replica
// per partition in self-hosted mode).
func ServerSetsHandler(sssStore *serversetscale.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeStatus(w, http.StatusMethodNotAllowed, "serversets", "",
				"method not allowed")
			return
		}
		ns, name, ok := parseServerSetsPath(r.URL.Path)
		if !ok {
			http.NotFound(w, r)
			return
		}
		items := synthesizeServerSets(sssStore, ns)
		if name == "" {
			writeJSON(w, http.StatusOK, map[string]any{
				"apiVersion": "igw.funcom.com/v1",
				"kind":       "ServerSetList",
				"metadata":   map[string]any{"resourceVersion": sssStore.CurrentResourceVersion()},
				"items":      items,
			})
			return
		}
		for _, it := range items {
			meta, _ := it["metadata"].(map[string]any)
			if meta == nil {
				continue
			}
			if got, _ := meta["name"].(string); got == name {
				writeOK(w, it)
				return
			}
		}
		writeStatus(w, http.StatusNotFound, "serversets", name,
			fmt.Sprintf("serversets.igw.funcom.com %q not found", name))
	}
}

func parseServerSetsPath(path string) (namespace, name string, ok bool) {
	path = strings.TrimPrefix(path, "/apis/igw.funcom.com/v1")
	path = strings.TrimPrefix(path, "/")
	parts := strings.Split(path, "/")
	if len(parts) == 1 && parts[0] == "serversets" {
		return "", "", true
	}
	if len(parts) >= 3 && parts[0] == "namespaces" && parts[2] == "serversets" {
		switch len(parts) {
		case 3:
			return parts[1], "", true
		case 4:
			return parts[1], parts[3], true
		}
	}
	return "", "", false
}

func synthesizeServerSets(sssStore *serversetscale.Store, ns string) []map[string]any {
	sssItems := sssStore.List(ns)
	out := make([]map[string]any, 0, len(sssItems))
	for _, sss := range sssItems {
		replicas := readInt(sss.Spec, "replicas")
		if replicas <= 0 {
			// Scaled to zero — no live instance to surface. Director
			// would skip it anyway; omitting saves a list slot.
			continue
		}
		out = append(out, serverSetObject(sss))
	}
	return out
}

func serverSetObject(sss serversetscale.Object) map[string]any {
	name := sss.Metadata.Name + "-0"
	labels := map[string]string{}
	for k, v := range sss.Metadata.Labels {
		labels[k] = v
	}
	labels["igw.funcom.com/serverset-scale-name"] = sss.Metadata.Name
	labels["igw.funcom.com/instance-index"] = "0"

	mapName := readString(sss.Spec, "mapName")
	bgName := readString(sss.Spec, "battlegroupName")

	return map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "ServerSet",
		"metadata": map[string]any{
			"name":              name,
			"namespace":         sss.Metadata.Namespace,
			"resourceVersion":   sss.Metadata.ResourceVersion,
			"uid":               sss.Metadata.UID + "-0",
			"creationTimestamp": fallback(sss.Metadata.CreationTimestamp, nowRFC3339()),
			"labels":            labels,
			"ownerReferences": []map[string]any{
				{
					"apiVersion":         "igw.funcom.com/v1",
					"kind":               "ServerSetScale",
					"name":               sss.Metadata.Name,
					"uid":                sss.Metadata.UID,
					"controller":         true,
					"blockOwnerDeletion": true,
				},
			},
		},
		"spec": map[string]any{
			"battlegroupName": bgName,
			"mapName":         mapName,
			"partitionId":     readInt(sss.Spec, "partitionId"),
			"instanceIndex":   0,
		},
		"status": map[string]any{
			"phase": "Running",
			"ready": true,
		},
	}
}

func readInt(spec map[string]any, key string) int64 {
	if spec == nil {
		return 0
	}
	switch v := spec[key].(type) {
	case int64:
		return v
	case int:
		return int64(v)
	case float64:
		return int64(v)
	case json.Number:
		n, _ := v.Int64()
		return n
	case string:
		n, err := strconv.ParseInt(v, 10, 64)
		if err == nil {
			return n
		}
	}
	return 0
}

func readString(spec map[string]any, key string) string {
	if spec == nil {
		return ""
	}
	s, _ := spec[key].(string)
	return s
}

func fallback(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

func nowRFC3339() string {
	return time.Now().UTC().Format(time.RFC3339)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeOK(w http.ResponseWriter, payload any) {
	writeJSON(w, http.StatusOK, payload)
}

func writeStatus(w http.ResponseWriter, code int, kind, name, msg string) {
	writeJSON(w, code, map[string]any{
		"kind":       "Status",
		"apiVersion": "v1",
		"metadata":   map[string]any{},
		"status":     "Failure",
		"message":    msg,
		"reason":     reasonForCode(code),
		"details": map[string]any{
			"name":  name,
			"group": "igw.funcom.com",
			"kind":  kind,
		},
		"code": code,
	})
}

func reasonForCode(code int) string {
	switch code {
	case http.StatusBadRequest:
		return "BadRequest"
	case http.StatusNotFound:
		return "NotFound"
	case http.StatusMethodNotAllowed:
		return "MethodNotAllowed"
	case http.StatusConflict:
		return "AlreadyExists"
	default:
		return "Failure"
	}
}

// Silence the import if slog ends up unused after future refactors —
// kept for parity with the other handler packages.
var _ = slog.Default
