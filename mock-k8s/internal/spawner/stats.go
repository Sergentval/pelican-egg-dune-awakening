package spawner

import "time"

// reconcileNamespace is the single namespace mock-k8s serves (lazy-create and
// the Director both use "default").
const reconcileNamespace = "default"

// Snapshot is an immutable view of the spawner's health, rendered by the
// /status and /metrics handlers.
type Snapshot struct {
	UptimeSeconds int64          `json:"uptimeSeconds"`
	Reconcile     ReconcileStats `json:"reconcile"`
	Pool          PoolStats      `json:"pool"`
	Instances     InstanceStats  `json:"instances"`
	Persist       PersistStats   `json:"persist"`
	Maps          []MapStatus    `json:"maps"`
}

type ReconcileStats struct {
	Enabled         bool      `json:"enabled"`
	IntervalSeconds int       `json:"intervalSeconds"`
	Sweeps          int64     `json:"sweeps"`
	LastSweep       time.Time `json:"lastSweep"`
}

type PoolStats struct {
	Size int `json:"size"`
	Used int `json:"used"`
	Free int `json:"free"`
}

type InstanceStats struct {
	Tracked        int   `json:"tracked"`
	ReapedTotal    int64 `json:"reapedTotal"`
	RespawnedTotal int64 `json:"respawnedTotal"`
	RestoredAtBoot int64 `json:"restoredAtBoot"`
}

type PersistStats struct {
	Errors    int64  `json:"errors"`
	LastError string `json:"lastError,omitempty"`
}

type MapStatus struct {
	Map                 string     `json:"map"`
	Key                 string     `json:"key"`
	Desired             int        `json:"desired"`
	Current             int        `json:"current"`
	Status              string     `json:"status"` // healthy | starting | failing | idle
	ConsecutiveFailures int        `json:"consecutiveFailures,omitempty"`
	NextRetry           *time.Time `json:"nextRetry,omitempty"`
}

// Snapshot assembles a consistent view. Pool and store are read through their
// own locks (outside s.mu); instance counts, counters, and backoff are read
// under s.mu.
func (s *Spawner) Snapshot() Snapshot {
	used, free, total := s.pool.Stats()
	objs := s.store.List(reconcileNamespace)

	s.mu.Lock()
	tracked := 0
	for _, list := range s.instances {
		tracked += len(list)
	}
	snap := Snapshot{
		UptimeSeconds: int64(s.now().Sub(s.startedAt).Seconds()),
		Reconcile: ReconcileStats{
			Enabled:         s.reconcileInterval > 0,
			IntervalSeconds: int(s.reconcileInterval / time.Second),
			Sweeps:          s.reconcileSweeps,
			LastSweep:       s.lastSweep,
		},
		Pool:      PoolStats{Size: total, Used: used, Free: free},
		Instances: InstanceStats{Tracked: tracked, ReapedTotal: s.reapedTotal, RespawnedTotal: s.respawnedTotal, RestoredAtBoot: s.restoredAtBoot},
		Persist:   PersistStats{Errors: s.persistErrors, LastError: s.lastPersistError},
	}
	for _, obj := range objs {
		key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
		mapName, _ := obj.Spec["mapName"].(string)
		if mapName == "" {
			mapName, _ = obj.Spec["map"].(string)
		}
		desired := readReplicas(obj.Spec)
		current := len(s.instances[key])
		ms := MapStatus{Map: mapName, Key: key, Desired: desired, Current: current}
		bs, hasBackoff := s.backoff[key]
		inBackoff := hasBackoff && bs.failures > 1 && s.now().Before(bs.nextRetry)
		switch {
		case desired == 0:
			ms.Status = "idle"
		case current >= desired:
			ms.Status = "healthy"
		case inBackoff:
			ms.Status = "failing"
		default:
			ms.Status = "starting"
		}
		if hasBackoff && bs.failures > 0 {
			ms.ConsecutiveFailures = bs.failures
			if !bs.nextRetry.IsZero() {
				nr := bs.nextRetry
				ms.NextRetry = &nr
			}
		}
		snap.Maps = append(snap.Maps, ms)
	}
	s.mu.Unlock()
	return snap
}
