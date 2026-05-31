// Package health renders the spawner's Snapshot as a JSON /status page and a
// Prometheus /metrics exposition. Pure presentation — no reconciliation logic.
package health

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
)

// StatusHandler serves the snapshot as indented JSON.
func StatusHandler(get func() spawner.Snapshot) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		_ = enc.Encode(get())
	}
}

// MetricsHandler serves the snapshot in Prometheus text exposition format.
func MetricsHandler(get func() spawner.Snapshot) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		s := get()
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		var b strings.Builder
		gauge(&b, "mock_k8s_pool_slots_used", "Port-pool slots currently in use.", s.Pool.Used)
		gauge(&b, "mock_k8s_pool_slots_total", "Port-pool capacity.", s.Pool.Size)
		gauge(&b, "mock_k8s_instances_tracked", "UE5 instances currently tracked.", s.Instances.Tracked)
		counter(&b, "mock_k8s_instances_reaped_total", "UE5 instances reaped as dead.", s.Instances.ReapedTotal)
		counter(&b, "mock_k8s_instances_respawned_total", "UE5 instances respawned by the loop.", s.Instances.RespawnedTotal)
		counter(&b, "mock_k8s_reconcile_sweeps_total", "Reconcile sweeps run.", s.Reconcile.Sweeps)
		counter(&b, "mock_k8s_persist_errors_total", "Ledger persist failures.", s.Persist.Errors)
		mapGauge(&b, "mock_k8s_map_desired", "Desired replicas per map.", s.Maps, func(m spawner.MapStatus) int { return m.Desired })
		mapGauge(&b, "mock_k8s_map_current", "Current replicas per map.", s.Maps, func(m spawner.MapStatus) int { return m.Current })
		mapGauge(&b, "mock_k8s_map_failing", "1 if the map is in crash-loop backoff.", s.Maps, func(m spawner.MapStatus) int {
			if m.Status == "failing" {
				return 1
			}
			return 0
		})
		_, _ = w.Write([]byte(b.String()))
	}
}

func gauge(b *strings.Builder, name, help string, v int) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s gauge\n%s %d\n", name, help, name, name, v)
}

func counter(b *strings.Builder, name, help string, v int64) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s counter\n%s %d\n", name, help, name, name, v)
}

func mapGauge(b *strings.Builder, name, help string, maps []spawner.MapStatus, val func(spawner.MapStatus) int) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s gauge\n", name, help, name)
	for _, m := range maps {
		fmt.Fprintf(b, "%s{map=\"%s\"} %d\n", name, escapeLabel(m.Map), val(m))
	}
}

func escapeLabel(s string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(s)
}
