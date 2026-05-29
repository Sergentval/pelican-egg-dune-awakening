// Package apigroup serves the Kubernetes API discovery endpoints that
// every K8s client hits during bootstrap — /api, /apis, /version, and the
// per-group resource list at /apis/igw.funcom.com/v1.
//
// The K8s client (the Director here) does this dance before issuing any
// real request:
//
//	GET /api               → core API version list ("v1")
//	GET /api/v1            → core resources (pods, namespaces, …) — we serve a minimal set
//	GET /apis              → APIGroupList of non-core groups
//	GET /apis/igw.funcom.com/v1 → resource list for our CRD group
//	GET /version           → server version (informational)
//
// Returning anything other than the documented shape causes the client to
// abort with confusing parse errors. The structures below mirror the
// official metav1 types verbatim — copied here to avoid pulling in
// k8s.io/apimachinery (~30 MB).
package apigroup

import (
	"encoding/json"
	"net/http"
)

// Group identifiers we expose. Keep both consts so handlers don't drift.
const (
	IGWGroup   = "igw.funcom.com"
	IGWVersion = "v1"
)

// HandleVersion serves /version with a hard-coded Kubernetes-style payload.
// The Director only reads major+minor to gate feature flags; build dates
// are cosmetic.
func HandleVersion(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"major":      "1",
		"minor":      "29",
		"gitVersion": "v1.29.0-mock-pelican-dune",
		"goVersion":  "go1.22",
		"platform":   "linux/amd64",
	})
}

// HandleAPI serves /api — the list of core API versions.
func HandleAPI(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"kind":     "APIVersions",
		"versions": []string{"v1"},
		"serverAddressByClientCIDRs": []map[string]string{
			{"clientCIDR": "0.0.0.0/0", "serverAddress": "127.0.0.1:6443"},
		},
	})
}

// HandleCoreV1 serves /api/v1 — the list of core resources. We serve a
// minimal set (just `namespaces`) so the Director can resolve its own
// namespace if it asks, but doesn't try to list pods we don't have.
func HandleCoreV1(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"kind":         "APIResourceList",
		"groupVersion": "v1",
		"resources": []map[string]any{
			{
				"name":         "namespaces",
				"singularName": "namespace",
				"namespaced":   false,
				"kind":         "Namespace",
				"verbs":        []string{"get", "list"},
			},
		},
	})
}

// HandleAPIs serves /apis — APIGroupList of all non-core groups. We
// advertise exactly one: igw.funcom.com.
func HandleAPIs(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"kind": "APIGroupList",
		"groups": []map[string]any{
			{
				"name": IGWGroup,
				"versions": []map[string]string{
					{"groupVersion": IGWGroup + "/" + IGWVersion, "version": IGWVersion},
				},
				"preferredVersion": map[string]string{
					"groupVersion": IGWGroup + "/" + IGWVersion, "version": IGWVersion,
				},
			},
		},
	})
}

// HandleIGWv1 serves /apis/igw.funcom.com/v1 — the resource list for our
// CRD group. The Director uses this to learn which resources exist and
// what verbs they support before issuing real requests.
func HandleIGWv1(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"kind":         "APIResourceList",
		"groupVersion": IGWGroup + "/" + IGWVersion,
		"resources": []map[string]any{
			{
				"name":         "serversetscales",
				"singularName": "serversetscale",
				"namespaced":   true,
				"kind":         "ServerSetScale",
				"verbs":        []string{"get", "list", "watch", "create", "update", "patch", "delete"},
				"shortNames":   []string{"sss"},
			},
			{
				"name":         "serversetscales/status",
				"singularName": "",
				"namespaced":   true,
				"kind":         "ServerSetScale",
				"verbs":        []string{"get", "patch", "update"},
			},
			{
				"name":         "battlegroups",
				"singularName": "battlegroup",
				"namespaced":   true,
				"kind":         "BattleGroup",
				"verbs":        []string{"get", "list", "watch"},
				"shortNames":   []string{"bg"},
			},
			{
				"name":         "battlegroupdirectorstats",
				"singularName": "battlegroupdirectorstat",
				"namespaced":   true,
				"kind":         "BattleGroupDirectorStats",
				"verbs":        []string{"get", "list", "create", "update", "patch", "delete"},
				"shortNames":   []string{"bgds"},
			},
			{
				"name":         "serversets",
				"singularName": "serverset",
				"namespaced":   true,
				"kind":         "ServerSet",
				"verbs":        []string{"get", "list", "watch"},
				"shortNames":   []string{"ss"},
			},
		},
	})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
