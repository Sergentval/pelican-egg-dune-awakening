package serversetscale

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// Handler returns the http.HandlerFunc tree for /apis/igw.funcom.com/v1/...
// The K8s URL shape we serve:
//
//	GET    /apis/igw.funcom.com/v1/serversetscales                                — list all
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales                 — list namespace, optional ?watch=true
//	POST   /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales                 — create
//	GET    /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}          — get one
//	PUT    /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}          — replace
//	PATCH  /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}          — merge-patch
//	DELETE /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}          — delete
//	PUT    /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}/status   — status subresource
//	PATCH  /apis/igw.funcom.com/v1/namespaces/{ns}/serversetscales/{name}/status   — patch status
//	GET    /apis/igw.funcom.com/v1/watch/namespaces/{ns}/serversetscales           — legacy single-stream watch
func Handler(s *Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/apis/igw.funcom.com/v1")
		path = strings.TrimPrefix(path, "/")

		// Cluster-scoped list / watch.
		if path == "serversetscales" {
			handleList(s, "", false, w, r)
			return
		}
		if path == "watch/serversetscales" {
			handleList(s, "", true, w, r)
			return
		}

		// Namespaced paths.
		// Examples:
		//   namespaces/default/serversetscales
		//   namespaces/default/serversetscales/sietch-survival
		//   namespaces/default/serversetscales/sietch-survival/status
		//   watch/namespaces/default/serversetscales
		//   watch/namespaces/default/serversetscales/sietch-survival
		parts := strings.Split(path, "/")
		legacyWatch := false
		if len(parts) > 0 && parts[0] == "watch" {
			legacyWatch = true
			parts = parts[1:]
		}
		if len(parts) >= 3 && parts[0] == "namespaces" && parts[2] == "serversetscales" {
			ns := parts[1]
			switch len(parts) {
			case 3:
				// list / create / watch on the namespace
				if r.Method == http.MethodPost && !legacyWatch {
					handleCreate(s, ns, w, r)
					return
				}
				handleList(s, ns, legacyWatch, w, r)
				return
			case 4:
				name := parts[3]
				handleOne(s, ns, name, false, w, r)
				return
			case 5:
				if parts[4] == "status" {
					handleOne(s, ns, parts[3], true, w, r)
					return
				}
			}
		}

		http.NotFound(w, r)
	}
}

func handleList(s *Store, ns string, isWatch bool, w http.ResponseWriter, r *http.Request) {
	watchParam := r.URL.Query().Get("watch") == "true" || r.URL.Query().Get("watch") == "1"
	if isWatch || watchParam {
		streamWatch(s, ns, w, r)
		return
	}
	// We deliberately return EMPTY items even when objects exist.
	// Reason: the Director's BattlegroupUtils.Igwo.Api.ListServerSetScales
	// builds a Dictionary keyed by some string property on each item,
	// and on at least one of those properties our serialized objects
	// resolve to null, throwing ArgumentNullException. At runtime
	// Director catches the exception in ProcessingLoopCycle, but at
	// startup the same throw bubbles up and prevents Director from
	// opening port 11717 — which kills start-director.sh's
	// wait_for_port and ultimately the container.
	//
	// Director doesn't actually need LIST results: it discovers maps
	// from the BattleGroup spec and then GETs each ServerSetScale by
	// name (which our lazy-create handles) and PATCHes scale changes
	// directly. Empty LIST is the cleanest workaround until we can
	// pinpoint the specific null field — without disassembling the
	// stripped .NET AOT Director binary it's a guessing game.
	writeJSON(w, http.StatusOK, map[string]any{
		"apiVersion": "igw.funcom.com/v1",
		"kind":       "ServerSetScaleList",
		"metadata": map[string]any{
			"resourceVersion": s.CurrentResourceVersion(),
		},
		"items": []Object{},
	})
}

func handleCreate(s *Store, ns string, w http.ResponseWriter, r *http.Request) {
	var obj Object
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&obj); err != nil {
		writeStatus(w, http.StatusBadRequest, fmt.Sprintf("decode body: %v", err), "")
		return
	}
	if obj.Metadata.Namespace == "" {
		obj.Metadata.Namespace = ns
	}
	if obj.Metadata.Namespace != ns {
		writeStatus(w, http.StatusBadRequest, "namespace in body does not match URL", "")
		return
	}
	if obj.Metadata.Name == "" {
		writeStatus(w, http.StatusBadRequest, "metadata.name required", "")
		return
	}
	created, ok := s.Create(obj)
	if !ok {
		writeStatus(w, http.StatusConflict,
			fmt.Sprintf("serversetscales.igw.funcom.com %q already exists", obj.Metadata.Name),
			obj.Metadata.Name)
		return
	}
	writeJSON(w, http.StatusCreated, created)
}

func handleOne(s *Store, ns, name string, statusSubresource bool, w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		obj, ok := s.GetOrLazyCreate(ns, name)
		if !ok {
			writeStatus(w, http.StatusNotFound,
				fmt.Sprintf("serversetscales.igw.funcom.com %q not found", name), name)
			return
		}
		writeJSON(w, http.StatusOK, obj)
	case http.MethodPut:
		var obj Object
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&obj); err != nil {
			writeStatus(w, http.StatusBadRequest, fmt.Sprintf("decode body: %v", err), "")
			return
		}
		obj.Metadata.Namespace = ns
		obj.Metadata.Name = name
		if statusSubresource {
			updated, ok := s.UpdateStatus(ns, name, obj.Status)
			if !ok {
				writeStatus(w, http.StatusNotFound,
					fmt.Sprintf("serversetscales.igw.funcom.com %q not found", name), name)
				return
			}
			writeJSON(w, http.StatusOK, updated)
			return
		}
		updated, ok := s.Update(obj)
		if !ok {
			writeStatus(w, http.StatusNotFound,
				fmt.Sprintf("serversetscales.igw.funcom.com %q not found", name), name)
			return
		}
		writeJSON(w, http.StatusOK, updated)
	case http.MethodPatch:
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeStatus(w, http.StatusBadRequest, fmt.Sprintf("read body: %v", err), "")
			return
		}
		// K8s clients use one of several patch encodings. Sniff the
		// first non-whitespace byte:
		//   `[` → RFC 6902 JSON Patch (array of {op, path, value} ops)
		//   `{` → RFC 7396 merge-patch
		// The Director's BattlegroupUtils.Igwo.Api sends JSON Patch
		// (Content-Type: application/json-patch+json) for scale ops.
		trimmed := bytes.TrimLeftFunc(body, func(r rune) bool { return r == ' ' || r == '\t' || r == '\n' || r == '\r' })
		var mergePatch map[string]any
		if len(trimmed) > 0 && trimmed[0] == '[' {
			var ops []map[string]any
			if err := json.Unmarshal(body, &ops); err != nil {
				writeStatus(w, http.StatusBadRequest, fmt.Sprintf("decode json-patch: %v", err), "")
				return
			}
			mergePatch, err = jsonPatchToMergePatch(ops)
			if err != nil {
				writeStatus(w, http.StatusBadRequest, fmt.Sprintf("apply json-patch: %v", err), "")
				return
			}
		} else {
			if err := json.Unmarshal(body, &mergePatch); err != nil {
				writeStatus(w, http.StatusBadRequest, fmt.Sprintf("decode merge-patch: %v", err), "")
				return
			}
		}
		updated, ok := s.Patch(ns, name, mergePatch, statusSubresource)
		if !ok {
			writeStatus(w, http.StatusNotFound,
				fmt.Sprintf("serversetscales.igw.funcom.com %q not found", name), name)
			return
		}
		writeJSON(w, http.StatusOK, updated)
	case http.MethodDelete:
		deleted, ok := s.Delete(ns, name)
		if !ok {
			writeStatus(w, http.StatusNotFound,
				fmt.Sprintf("serversetscales.igw.funcom.com %q not found", name), name)
			return
		}
		writeJSON(w, http.StatusOK, deleted)
	default:
		w.Header().Set("Allow", "GET, PUT, PATCH, DELETE")
		writeStatus(w, http.StatusMethodNotAllowed, "method not allowed", "")
	}
}

// streamWatch holds the connection open and emits one JSON event per
// line, K8s-style. Closes on client disconnect.
func streamWatch(s *Store, ns string, w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeStatus(w, http.StatusInternalServerError, "streaming unsupported", "")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Transfer-Encoding", "chunked")
	// K8s expects an initial 200 status before events start arriving.
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	ch := s.Subscribe(128)
	defer s.Unsubscribe(ch)

	// Optional: emit ADDED events for existing objects as the resync
	// snapshot. K8s clients can request this via resourceVersion=0;
	// we just always do it for simplicity.
	if r.URL.Query().Get("resourceVersion") == "0" || r.URL.Query().Get("resourceVersion") == "" {
		for _, obj := range s.List(ns) {
			if err := writeEvent(w, flusher, Event{Type: "ADDED", Object: obj}); err != nil {
				return
			}
		}
	}

	heartbeat := time.NewTicker(30 * time.Second)
	defer heartbeat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case ev, ok := <-ch:
			if !ok {
				return
			}
			if ns != "" && ev.Object.Metadata.Namespace != ns {
				continue
			}
			if err := writeEvent(w, flusher, ev); err != nil {
				return
			}
		case <-heartbeat.C:
			// Send a no-op newline so idle TCP connections stay open
			// past corporate-firewall idle timeouts. K8s clients
			// tolerate empty lines in the watch stream.
			if _, err := io.WriteString(w, "\n"); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

func writeEvent(w io.Writer, flusher http.Flusher, ev Event) error {
	if err := json.NewEncoder(w).Encode(ev); err != nil {
		// json.Encoder.Encode already writes a trailing newline.
		// If the write failed, the client has disconnected.
		return err
	}
	flusher.Flush()
	return nil
}

// writeJSON writes a JSON response with Content-Type set.
func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// writeStatus emits a K8s-shaped Status object so the client error
// handling code can parse our error responses.
func writeStatus(w http.ResponseWriter, code int, message, name string) {
	reason := codeToReason(code)
	writeJSON(w, code, map[string]any{
		"kind":       "Status",
		"apiVersion": "v1",
		"metadata":   map[string]any{},
		"status":     "Failure",
		"message":    message,
		"reason":     reason,
		"details": map[string]any{
			"name":  name,
			"group": "igw.funcom.com",
			"kind":  "serversetscales",
		},
		"code": code,
	})
}

func codeToReason(code int) string {
	switch code {
	case http.StatusBadRequest:
		return "BadRequest"
	case http.StatusConflict:
		return "AlreadyExists"
	case http.StatusNotFound:
		return "NotFound"
	case http.StatusMethodNotAllowed:
		return "MethodNotAllowed"
	default:
		return "Failure"
	}
}

// Sentinel used internally for cleaner error returns. Not currently
// emitted on the wire; placeholder for future error-categorization work.
var ErrNotFound = errors.New("not found")

// Silence the import if slog ends up unused after refactors.
var _ = slog.Default

// jsonPatchToMergePatch converts a list of RFC 6902 JSON Patch
// operations into the equivalent RFC 7396 merge-patch object.
//
// We only handle the ops the Funcom Director actually emits: `replace`,
// `add`, and `remove`. Everything else (`move`, `copy`, `test`) returns
// an error so we don't silently drop changes a real K8s server would
// honor. This lets us reuse Store.Patch's existing merge logic instead
// of writing a second patch engine.
//
// Path interpretation is a thin JSON Pointer parser: "/spec/replicas"
// → merge-patch { "spec": { "replicas": <value> } }. Leading slash
// required; ~1 / ~0 escape sequences resolved per RFC 6901.
func jsonPatchToMergePatch(ops []map[string]any) (map[string]any, error) {
	out := map[string]any{}
	for i, op := range ops {
		opName, _ := op["op"].(string)
		path, _ := op["path"].(string)
		if path == "" || path[0] != '/' {
			return nil, fmt.Errorf("op[%d]: missing or invalid path %q", i, path)
		}
		segments := decodeJSONPointer(path)
		switch opName {
		case "replace", "add":
			setAtPath(out, segments, op["value"])
		case "remove":
			// Merge-patch encodes deletion as null. Store.Patch's
			// mergeInto helper interprets a null value as "delete
			// this key", matching RFC 7396.
			setAtPath(out, segments, nil)
		default:
			return nil, fmt.Errorf("op[%d]: unsupported op %q", i, opName)
		}
	}
	return out, nil
}

// decodeJSONPointer turns "/spec/items/0" into ["spec","items","0"].
// Handles ~1 → "/" and ~0 → "~" escapes per RFC 6901.
func decodeJSONPointer(p string) []string {
	if p == "" || p == "/" {
		return nil
	}
	parts := strings.Split(p[1:], "/")
	for i, seg := range parts {
		seg = strings.ReplaceAll(seg, "~1", "/")
		seg = strings.ReplaceAll(seg, "~0", "~")
		parts[i] = seg
	}
	return parts
}

// setAtPath inserts value into out at segments, auto-creating nested
// maps as needed. Numeric segments (array indices) are treated as map
// keys here — merge-patch can't slice into arrays, and treating them as
// keys preserves the value for Store.Patch to merge.
func setAtPath(out map[string]any, segments []string, value any) {
	if len(segments) == 0 {
		return
	}
	cur := out
	for i := 0; i < len(segments)-1; i++ {
		key := segments[i]
		next, ok := cur[key].(map[string]any)
		if !ok {
			next = map[string]any{}
			cur[key] = next
		}
		cur = next
	}
	cur[segments[len(segments)-1]] = value
}
