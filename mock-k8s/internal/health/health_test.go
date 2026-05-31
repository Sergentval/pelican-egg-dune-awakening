package health

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
)

func sampleSnapshot() spawner.Snapshot {
	nr := time.Unix(1_700_000_060, 0).UTC()
	return spawner.Snapshot{
		UptimeSeconds: 3600,
		Reconcile:     spawner.ReconcileStats{Enabled: true, IntervalSeconds: 30, Sweeps: 120},
		Pool:          spawner.PoolStats{Size: 64, Used: 8, Free: 56},
		Instances:     spawner.InstanceStats{Tracked: 8, ReapedTotal: 3, RespawnedTotal: 3, RestoredAtBoot: 2},
		Persist:       spawner.PersistStats{Errors: 0},
		Maps: []spawner.MapStatus{
			{Map: "Survival_1", Key: "default/s1", Desired: 1, Current: 1, Status: "healthy"},
			{Map: "Overmap", Key: "default/om", Desired: 1, Current: 0, Status: "failing", ConsecutiveFailures: 4, NextRetry: &nr},
		},
	}
}

func TestStatusHandler_JSON(t *testing.T) {
	rec := httptest.NewRecorder()
	StatusHandler(sampleSnapshot)(rec, httptest.NewRequest(http.MethodGet, "/status", nil))
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("content-type = %q, want application/json", ct)
	}
	var got spawner.Snapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, rec.Body.String())
	}
	if got.Pool.Used != 8 || len(got.Maps) != 2 || got.Maps[1].Status != "failing" {
		t.Errorf("decoded snapshot wrong: %+v", got)
	}
}

func TestMetricsHandler_Prometheus(t *testing.T) {
	rec := httptest.NewRecorder()
	MetricsHandler(sampleSnapshot)(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := rec.Body.String()
	for _, want := range []string{
		"mock_k8s_pool_slots_used 8",
		"mock_k8s_pool_slots_total 64",
		"mock_k8s_instances_reaped_total 3",
		"mock_k8s_reconcile_sweeps_total 120",
		`mock_k8s_map_current{map="Overmap"} 0`,
		`mock_k8s_map_failing{map="Overmap"} 1`,
		`mock_k8s_map_failing{map="Survival_1"} 0`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics missing %q\n--- body ---\n%s", want, body)
		}
	}
}
