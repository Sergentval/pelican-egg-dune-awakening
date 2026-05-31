# mock-k8s Self-Healing Reconcile Loop + Health Endpoints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a periodic loop that reaps crashed UE5 instances and respawns each map to its desired replica count (with crash-loop backoff), plus JSON `/status` and Prometheus `/metrics` endpoints.

**Architecture:** Approach A — the loop and its stats live in the `spawner` package (it already owns the store, pool, instances, spawn primitives, and the `(pid,start-time)` identity check); the HTTP handlers live in a new `internal/health` package and read a `spawner.Snapshot()`. The loop runs under the existing `reconcileMu` and is **up-only** (scale-down stays Director-driven).

**Tech Stack:** Go 1.22, stdlib only (`net/http`, `encoding/json`, `time`, `context`). No new module dependencies — Prometheus text is hand-rolled.

**Spec:** `docs/superpowers/specs/2026-05-31-mock-k8s-self-healing-reconcile-design.md`

**Working dir for all commands:** `~/projects/pelican-egg-dune-awakening/mock-k8s`

---

## File Structure

| File | Created/Modified | Responsibility |
|------|------------------|----------------|
| `internal/spawner/spawner.go` | Modify | New `Spawner` fields (clock seam, counters, backoff map, interval, startedAt); set them in `New`; refactor `OnSpecChange` up-branch into `reconcileUpLocked`; count `RestoredAtBoot`/persist errors. |
| `internal/spawner/stats.go` | Create | Snapshot types, `Snapshot()`, status derivation. |
| `internal/spawner/reconcile_loop.go` | Create | `Reconcile`, `reconcileTick`, `sweep`, `reconcileUpLocked`, backoff helpers, `aliveAs`, `isChanClosed`. |
| `internal/spawner/stats_test.go` | Create | `Snapshot()` + backoff timing tests. |
| `internal/spawner/reconcile_loop_test.go` | Create | Loop behaviour tests. |
| `internal/health/health.go` | Create | `StatusHandler` (JSON), `MetricsHandler` (Prometheus). |
| `internal/health/health_test.go` | Create | Endpoint tests. |
| `cmd/mock-k8s/main.go` | Modify | Parse `MOCK_K8S_RECONCILE_INTERVAL`, start the loop, mount `/status` + `/metrics`. |

---

## Task 1: Spawner fields + clock seam + backoff state

Adds the data the loop and stats need. No behaviour change yet — just fields and their initialisation, plus the backoff helpers (pure logic, deterministically testable via an injectable clock).

**Files:**
- Modify: `internal/spawner/spawner.go` (struct + `New`)
- Create: `internal/spawner/reconcile_loop.go` (backoff helpers only, for now)
- Test: `internal/spawner/stats_test.go`

- [ ] **Step 1: Write the failing test for backoff timing**

Create `internal/spawner/stats_test.go`:

```go
package spawner

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// newBareSpawner builds a Spawner with a fixed, controllable clock for tests.
func newBareSpawner(t *testing.T) (*Spawner, *fakeClock) {
	t.Helper()
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	spw := New(store, pl, filepath.Join(base, "noop.sh"), base)
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	spw.now = clk.Now
	spw.startedAt = clk.Now()
	return spw, clk
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) Now() time.Time     { return c.t }
func (c *fakeClock) Advance(d time.Duration) { c.t = c.t.Add(d) }

func TestBackoff_CurveAndReset(t *testing.T) {
	spw, clk := newBareSpawner(t)
	const k = "default/m"

	// First failure: retry immediately (no backoff window).
	spw.recordFailure(k)
	if spw.inBackoff(k) {
		t.Errorf("after 1 failure: inBackoff=true, want false (retry next tick)")
	}

	// Second failure: 1 minute window.
	spw.recordFailure(k)
	if !spw.inBackoff(k) {
		t.Fatalf("after 2 failures: inBackoff=false, want true")
	}
	clk.Advance(59 * time.Second)
	if !spw.inBackoff(k) {
		t.Errorf("59s into a 1m backoff: inBackoff=false, want true")
	}
	clk.Advance(2 * time.Second) // now 61s > 60s
	if spw.inBackoff(k) {
		t.Errorf("61s into a 1m backoff: inBackoff=true, want false")
	}

	// Reset clears the state.
	spw.resetBackoff(k)
	spw.recordFailure(k)
	if spw.inBackoff(k) {
		t.Errorf("after reset+1 failure: inBackoff=true, want false")
	}
}

func TestBackoff_CapsAtMax(t *testing.T) {
	spw, clk := newBareSpawner(t)
	const k = "default/m"
	for i := 0; i < 20; i++ {
		spw.recordFailure(k)
	}
	if !spw.inBackoff(k) {
		t.Fatal("expected to be in backoff after many failures")
	}
	clk.Advance(maxBackoff - time.Second)
	if !spw.inBackoff(k) {
		t.Errorf("just before maxBackoff: inBackoff=false, want true")
	}
	clk.Advance(2 * time.Second)
	if spw.inBackoff(k) {
		t.Errorf("just after maxBackoff: inBackoff=true, want false (capped at %s)", maxBackoff)
	}
}
```

- [ ] **Step 2: Run the test to verify it fails (compile error: fields/methods missing)**

Run: `go test ./internal/spawner/ -run TestBackoff`
Expected: FAIL — `spw.now undefined`, `recordFailure undefined`, `inBackoff undefined`, `maxBackoff undefined`.

- [ ] **Step 3: Add the Spawner fields**

In `internal/spawner/spawner.go`, add to the `Spawner` struct (right after the `terminate` field):

```go
	// terminate is the process-termination seam; defaults to proc.Terminate.
	terminate func(pid int, grace time.Duration) error

	// now is the clock seam; defaults to time.Now. Swappable so backoff timing
	// is deterministic in tests.
	now func() time.Time

	startedAt        time.Time // process start, for /status uptime
	reconcileInterval time.Duration // 0 when the loop is disabled
	reconcileSweeps  int64         // ticks run
	lastSweep        time.Time     // zero until the first tick
	reapedTotal      int64
	respawnedTotal   int64
	restoredAtBoot   int64
	persistErrors    int64
	lastPersistError string

	// backoff holds per-map crash-loop state, guarded by s.mu.
	backoff map[string]backoffState
```

Add the `backoffState` type just below the `instance` struct:

```go
// backoffState tracks crash-loop backoff for one map key.
type backoffState struct {
	failures  int
	nextRetry time.Time // zero when failures <= 1 (retry immediately)
}
```

Initialise the new fields in `New` (add to the returned struct literal):

```go
		terminate:  proc.Terminate,
		now:        time.Now,
		startedAt:  time.Now(),
		backoff:    make(map[string]backoffState),
```

- [ ] **Step 4: Add the backoff helpers**

Create `internal/spawner/reconcile_loop.go`:

```go
package spawner

import "time"

const (
	baseBackoff = time.Minute
	maxBackoff  = 15 * time.Minute
)

// recordFailure increments a map's consecutive-failure count and sets the next
// eligible retry time. The first failure has no delay (retry next tick); each
// further failure doubles the delay (1m, 2m, 4m, …) capped at maxBackoff.
func (s *Spawner) recordFailure(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	bs := s.backoff[key]
	bs.failures++
	bs.nextRetry = s.backoffUntilLocked(bs.failures)
	s.backoff[key] = bs
}

// backoffUntilLocked returns the next-retry time for the given failure count.
// Caller holds s.mu (it reads s.now()). A failure count <= 1 means "no delay".
func (s *Spawner) backoffUntilLocked(failures int) time.Time {
	if failures <= 1 {
		return time.Time{}
	}
	delay := baseBackoff << (failures - 2) // base * 2^(failures-2)
	if delay <= 0 || delay > maxBackoff {  // <=0 guards int64 shift overflow
		delay = maxBackoff
	}
	return s.now().Add(delay)
}

// inBackoff reports whether a map is currently within its crash-loop backoff
// window and must not be respawned yet.
func (s *Spawner) inBackoff(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	bs, ok := s.backoff[key]
	return ok && bs.failures > 1 && s.now().Before(bs.nextRetry)
}

// resetBackoff clears a map's backoff state (called once a respawn survives a
// clean tick).
func (s *Spawner) resetBackoff(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.backoff, key)
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `go test ./internal/spawner/ -run TestBackoff -v`
Expected: PASS (`TestBackoff_CurveAndReset`, `TestBackoff_CapsAtMax`).

- [ ] **Step 6: Verify the whole module still builds and tests pass**

Run: `go build ./... && go test ./internal/spawner/`
Expected: build OK; all spawner tests PASS.

- [ ] **Step 7: Commit**

```bash
git add internal/spawner/spawner.go internal/spawner/reconcile_loop.go internal/spawner/stats_test.go
git commit -m "feat(mock-k8s): spawner clock seam + crash-loop backoff state"
```

---

## Task 2: Snapshot types + Snapshot()

Exposes the in-memory state the endpoints render. Pure read path; no loop yet.

**Files:**
- Create: `internal/spawner/stats.go`
- Test: `internal/spawner/stats_test.go` (append)

- [ ] **Step 1: Write the failing test**

Append to `internal/spawner/stats_test.go`:

```go
func TestSnapshot_ReflectsPoolInstancesAndMaps(t *testing.T) {
	spw, _ := newBareSpawner(t)

	// One ServerSetScale desiring 1 replica, with one live tracked instance.
	spw.store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "m"},
		Spec:     map[string]any{"mapName": "Survival_1", "replicas": int64(1)},
	})
	alloc, _ := spw.pool.Acquire()
	spw.mu.Lock()
	spw.instances["default/m"] = []instance{{Suffix: "p0", MapName: "Survival_1", Allocation: alloc, PID: 1234, StartTime: 99}}
	spw.reapedTotal = 2
	spw.respawnedTotal = 2
	spw.mu.Unlock()

	snap := spw.Snapshot()

	if snap.Pool.Used != 1 || snap.Pool.Size != 5 {
		t.Errorf("pool used/size = %d/%d, want 1/5", snap.Pool.Used, snap.Pool.Size)
	}
	if snap.Instances.Tracked != 1 || snap.Instances.ReapedTotal != 2 || snap.Instances.RespawnedTotal != 2 {
		t.Errorf("instances = %+v, want tracked=1 reaped=2 respawned=2", snap.Instances)
	}
	if len(snap.Maps) != 1 {
		t.Fatalf("maps = %d, want 1", len(snap.Maps))
	}
	m := snap.Maps[0]
	if m.Map != "Survival_1" || m.Desired != 1 || m.Current != 1 || m.Status != "healthy" {
		t.Errorf("map = %+v, want Survival_1 desired=1 current=1 healthy", m)
	}
}

func TestSnapshot_FailingMapShowsBackoff(t *testing.T) {
	spw, _ := newBareSpawner(t)
	spw.store.Create(serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: "m"},
		Spec:     map[string]any{"mapName": "Overmap", "replicas": int64(1)},
	})
	// Desired 1, current 0, and the map is in backoff -> "failing".
	spw.recordFailure("default/m")
	spw.recordFailure("default/m") // failures=2 -> 1m window

	snap := spw.Snapshot()
	m := snap.Maps[0]
	if m.Status != "failing" {
		t.Errorf("status = %q, want failing", m.Status)
	}
	if m.ConsecutiveFailures != 2 {
		t.Errorf("consecutiveFailures = %d, want 2", m.ConsecutiveFailures)
	}
	if m.NextRetry == nil {
		t.Error("nextRetry = nil, want a time for a failing map")
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./internal/spawner/ -run TestSnapshot`
Expected: FAIL — `spw.Snapshot undefined`.

- [ ] **Step 3: Implement `stats.go`**

Create `internal/spawner/stats.go`:

```go
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `go test ./internal/spawner/ -run TestSnapshot -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/spawner/stats.go internal/spawner/stats_test.go
git commit -m "feat(mock-k8s): Snapshot() health view for the spawner"
```

---

## Task 3: Sweep — reap dead and phantom instances

**Files:**
- Modify: `internal/spawner/reconcile_loop.go` (add `sweep`, `aliveAs`, `isChanClosed`)
- Test: `internal/spawner/reconcile_loop_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/spawner/reconcile_loop_test.go`:

```go
package spawner

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

// liveSleeper starts a real process and returns its pid + start-time identity.
func liveSleeper(t *testing.T) (pid int, st uint64) {
	t.Helper()
	cmd := exec.Command("sleep", "300")
	if err := cmd.Start(); err != nil {
		t.Fatalf("start sleeper: %v", err)
	}
	pid = cmd.Process.Pid
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	st, _ = proc.StartTime(pid)
	return pid, st
}

func TestSweep_ReapsDeadKeepsLive(t *testing.T) {
	spw, _ := newBareSpawner(t)

	// Live instance (should be kept).
	livePID, liveST := liveSleeper(t)
	aLive, _ := spw.pool.Acquire()
	// Dead instance (should be reaped): start then kill.
	deadPID, deadST := liveSleeper(t)
	_ = syscall.Kill(deadPID, syscall.SIGKILL)
	for i := 0; i < 200 && proc.Alive(deadPID); i++ {
		time.Sleep(10 * time.Millisecond)
	}
	aDead, _ := spw.pool.Acquire()

	spw.mu.Lock()
	spw.instances["default/m"] = []instance{
		{Suffix: "p" + itoa(aLive.Index), MapName: "Survival_1", Allocation: aLive, PID: livePID, StartTime: liveST},
		{Suffix: "p" + itoa(aDead.Index), MapName: "Survival_1", Allocation: aDead, PID: deadPID, StartTime: deadST},
	}
	spw.mu.Unlock()

	spw.sweep()

	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 1 {
		t.Fatalf("after sweep: %d instances tracked, want 1 (dead reaped, live kept)", got)
	}
	if used, _, _ := spw.pool.Stats(); used != 1 {
		t.Errorf("after sweep: pool used = %d, want 1 (dead slot released)", used)
	}
	if spw.reapedTotal != 1 {
		t.Errorf("reapedTotal = %d, want 1", spw.reapedTotal)
	}
	// One reap records exactly one failure (failures==1 means retry next tick).
	spw.mu.Lock()
	failures := spw.backoff["default/m"].failures
	spw.mu.Unlock()
	if failures != 1 {
		t.Errorf("backoff failures = %d, want 1 after one reap", failures)
	}
}

func TestSweep_ReapsPhantomSkipsStarting(t *testing.T) {
	spw, _ := newBareSpawner(t)

	closed := make(chan struct{})
	close(closed)      // capture finished, never got a pid -> phantom
	open := make(chan struct{}) // still starting

	aPhantom, _ := spw.pool.Acquire()
	aStarting, _ := spw.pool.Acquire()
	spw.mu.Lock()
	spw.instances["default/m"] = []instance{
		{Suffix: "p" + itoa(aPhantom.Index), MapName: "Survival_1", Allocation: aPhantom, PID: 0, pidReady: closed},
		{Suffix: "p" + itoa(aStarting.Index), MapName: "Survival_1", Allocation: aStarting, PID: 0, pidReady: open},
	}
	spw.mu.Unlock()

	spw.sweep()

	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 1 {
		t.Fatalf("after sweep: %d instances, want 1 (phantom reaped, starting kept)", got)
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./internal/spawner/ -run TestSweep`
Expected: FAIL — `spw.sweep undefined`.

- [ ] **Step 3: Implement `sweep`, `aliveAs`, `isChanClosed`**

First, replace the import line at the top of `internal/spawner/reconcile_loop.go` with this block:

```go
import (
	"os"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
)
```

Then append these functions to `internal/spawner/reconcile_loop.go`:

```go
// aliveAs reports whether pid is still the same process incarnation recorded by
// startTime; a zero start-time falls back to a plain liveness probe (legacy /
// restored instances).
func aliveAs(pid int, startTime uint64) bool {
	if startTime == 0 {
		return proc.Alive(pid)
	}
	return proc.SameProcess(pid, startTime)
}

// isChanClosed non-blockingly reports whether ch (only ever closed, never sent
// to) is closed.
func isChanClosed(ch chan struct{}) bool {
	if ch == nil {
		return false
	}
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// sweep reaps tracked instances whose process is gone (a crashed UE5) or whose
// spawn failed (pid never captured), releasing their slots and recording a
// failure for backoff. Live and still-starting instances are left alone.
// Returns the set of map keys that had at least one reap.
func (s *Spawner) sweep() map[string]bool {
	// Phase 1: snapshot identities under s.mu (no syscalls under the lock).
	type probe struct {
		key        string
		allocIndex int
		pid        int
		startTime  uint64
		phantom    bool
		pidPath    string
	}
	var probes []probe
	s.mu.Lock()
	for key, list := range s.instances {
		for _, in := range list {
			probes = append(probes, probe{
				key:        key,
				allocIndex: in.Allocation.Index,
				pid:        in.PID,
				startTime:  in.StartTime,
				phantom:    in.PID == 0 && isChanClosed(in.pidReady),
				pidPath:    s.pidPath(in.MapName, in.Suffix),
			})
		}
	}
	s.mu.Unlock()

	// Phase 2: classify outside the lock.
	var dead []probe
	for _, p := range probes {
		if p.pid > 0 {
			if !aliveAs(p.pid, p.startTime) {
				dead = append(dead, p)
			}
		} else if p.phantom {
			dead = append(dead, p)
		}
	}
	if len(dead) == 0 {
		return nil
	}

	// Phase 3: remove dead under s.mu (match by pool index + pid), count, record.
	reaped := make(map[string]bool, len(dead))
	s.mu.Lock()
	for _, d := range dead {
		list := s.instances[d.key]
		for i := range list {
			if list[i].Allocation.Index == d.allocIndex && list[i].PID == d.pid {
				s.instances[d.key] = append(list[:i:i], list[i+1:]...)
				s.reapedTotal++
				reaped[d.key] = true
				break
			}
		}
	}
	s.mu.Unlock()

	// Phase 4: side effects + backoff outside s.mu.
	for _, d := range dead {
		s.pool.Release(d.allocIndex)
		_ = os.Remove(d.pidPath)
	}
	for key := range reaped {
		s.recordFailure(key)
	}
	s.persist()
	return reaped
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `go test ./internal/spawner/ -run TestSweep -v`
Expected: PASS (`TestSweep_ReapsDeadKeepsLive`, `TestSweep_ReapsPhantomSkipsStarting`).

- [ ] **Step 5: Verify build + go vet**

Run: `go build ./... && go vet ./internal/spawner/`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add internal/spawner/reconcile_loop.go internal/spawner/reconcile_loop_test.go
git commit -m "feat(mock-k8s): reconcile sweep reaps dead + phantom instances"
```

---

## Task 4: reconcileUpLocked + OnSpecChange refactor

Factors the shared "spawn up to desired" path. The loop will call it with backoff; the Director path keeps honoring patches immediately.

**Files:**
- Modify: `internal/spawner/spawner.go` (`OnSpecChange`)
- Modify: `internal/spawner/reconcile_loop.go` (add `reconcileUpLocked`)
- Test: `internal/spawner/reconcile_loop_test.go` (append)

- [ ] **Step 1: Write the failing test**

Append to `internal/spawner/reconcile_loop_test.go`:

```go
// fakeUE5 writes a start-ue5.sh stub that backgrounds a real sleeper and
// records its pid where capturePID expects it. Returns the script path.
func fakeUE5Script(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "start-ue5.sh")
	// $1=BASE $2=MAP $3=SUFFIX ; write runtime/pids/ue5-<map>-<suffix>.pid
	body := "#!/bin/bash\nset -e\nsleep 300 &\nPID=$!\nmkdir -p \"$1/runtime/pids\"\necho $PID > \"$1/runtime/pids/ue5-$2-$3.pid\"\n"
	if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

func newLoopSpawner(t *testing.T) (*Spawner, *fakeClock) {
	t.Helper()
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	// Use a fake script that actually launches a trackable process.
	spw := New(store, pl, fakeUE5Script(t), base)
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	spw.now = clk.Now
	spw.startedAt = clk.Now()
	return spw, clk
}

func sssObj(name, mapName string, replicas int64) serversetscale.Object {
	return serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: name},
		Spec:     map[string]any{"mapName": mapName, "replicas": replicas, "partitionId": int64(1)},
	}
}

func TestReconcileUp_SpawnsToDesired(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	obj := sssObj("m", "Survival_1", 2)

	spw.reconcileMu.Lock()
	n := spw.reconcileUpLocked(obj, true)
	spw.reconcileMu.Unlock()
	spw.Wait() // let capturePID finish

	if n != 2 {
		t.Errorf("spawned %d, want 2", n)
	}
	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 2 {
		t.Errorf("tracked %d, want 2", got)
	}
}

func TestReconcileUp_RespectsBackoff(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	obj := sssObj("m", "Survival_1", 1)

	// Put the map in backoff (failures=2 -> 1m window).
	spw.recordFailure("default/m")
	spw.recordFailure("default/m")

	spw.reconcileMu.Lock()
	withBackoff := spw.reconcileUpLocked(obj, true)  // must NOT spawn
	spw.reconcileMu.Unlock()
	if withBackoff != 0 {
		t.Errorf("respectBackoff=true spawned %d, want 0 (map in backoff)", withBackoff)
	}

	spw.reconcileMu.Lock()
	ignoreBackoff := spw.reconcileUpLocked(obj, false) // Director patch honored
	spw.reconcileMu.Unlock()
	spw.Wait()
	if ignoreBackoff != 1 {
		t.Errorf("respectBackoff=false spawned %d, want 1", ignoreBackoff)
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./internal/spawner/ -run TestReconcileUp`
Expected: FAIL — `spw.reconcileUpLocked undefined`.

- [ ] **Step 3: Add `reconcileUpLocked`**

First, update the import block at the top of `internal/spawner/reconcile_loop.go` to:

```go
import (
	"log/slog"
	"os"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)
```

Then append these functions:

```go
// reconcileUpLocked spawns instances for obj until current == desired (only
// when desired > current). Caller MUST hold s.reconcileMu. When respectBackoff
// is true and the map is in crash-loop backoff, it spawns nothing. Returns the
// number spawned.
func (s *Spawner) reconcileUpLocked(obj serversetscale.Object, respectBackoff bool) int {
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	mapName, _ := obj.Spec["mapName"].(string)
	if mapName == "" {
		mapName, _ = obj.Spec["map"].(string)
	}
	if !safeInstanceName(mapName) {
		slog.Error("spawner: refusing ServerSetScale with unsafe map name", "key", key, "map", mapName)
		return 0
	}
	desired := readReplicas(obj.Spec)
	partitionID := readPartitionID(obj.Spec)

	s.mu.Lock()
	current := len(s.instances[key])
	s.mu.Unlock()
	if desired <= current {
		return 0
	}
	if respectBackoff && s.inBackoff(key) {
		return 0
	}
	for i := current; i < desired; i++ {
		s.spawnOne(obj, mapName, partitionID, i)
	}
	return desired - current
}
```

- [ ] **Step 4: Refactor `OnSpecChange` to use it**

In `internal/spawner/spawner.go`, find this region of `OnSpecChange` (it follows
the `safeInstanceName(mapName)` early-return check):

```go
	desired := readReplicas(obj.Spec)
	partitionID := readPartitionID(obj.Spec)

	s.reconcileMu.Lock()
	s.mu.Lock()
	current := len(s.instances[key])
	s.mu.Unlock()

	switch {
	case desired > current:
		for i := current; i < desired; i++ {
			s.spawnOne(obj, mapName, partitionID, i)
		}
	case desired < current:
		s.scaleDown(key, desired)
	}
	s.reconcileMu.Unlock()
```

and replace it with:

```go
	desired := readReplicas(obj.Spec)

	s.reconcileMu.Lock()
	if desired < currentCount(s, key) {
		s.scaleDown(key, desired)
	} else {
		s.reconcileUpLocked(obj, false) // honor a Director patch immediately
	}
	s.reconcileMu.Unlock()
```

Key points: the `partitionID` and `current` locals are **removed** (Go would
error on the now-unused `partitionID`); `reconcileUpLocked` re-derives
`mapName`/`partitionID`/`desired` itself. Keep the `desired := readReplicas(...)`
line — it's still used by the `UpdateStatus` call further down. Keep the earlier
`mapName` local and its `safeInstanceName` early-return check (the only other
use of `mapName`).

Add this helper near `reconcileUpLocked` in `reconcile_loop.go`:

```go
// currentCount returns the number of instances tracked for key.
func currentCount(s *Spawner, key string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.instances[key])
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `go test ./internal/spawner/ -run 'TestReconcileUp|TestSpawner_|TestOnSpecChange' -v`
Expected: PASS — new tests AND the existing spawner/OnSpecChange tests (the refactor must not regress them).

- [ ] **Step 6: Run the full spawner suite with the race detector**

Run: `go test -race ./internal/spawner/`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add internal/spawner/spawner.go internal/spawner/reconcile_loop.go internal/spawner/reconcile_loop_test.go
git commit -m "refactor(mock-k8s): share reconcileUpLocked between OnSpecChange and loop"
```

---

## Task 5: The reconcile tick + loop

Ties sweep + reconcile-up + reset + counters together, and the `Reconcile(ctx, interval)` driver.

**Files:**
- Modify: `internal/spawner/reconcile_loop.go` (`reconcileTick`, `Reconcile`)
- Test: `internal/spawner/reconcile_loop_test.go` (append)

- [ ] **Step 1: Write the failing tests**

Append to `internal/spawner/reconcile_loop_test.go`:

```go
func TestReconcileTick_ReapsAndRespawns(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("m", "Survival_1", 1))

	// First tick spawns the desired instance.
	spw.reconcileTick()
	spw.Wait()
	spw.mu.Lock()
	pid := spw.instances["default/m"][0].PID
	spw.mu.Unlock()
	if pid <= 0 {
		t.Fatalf("expected a captured pid after first tick, got %d", pid)
	}

	// Kill it -> next tick must reap and respawn.
	_ = syscall.Kill(pid, syscall.SIGKILL)
	for i := 0; i < 200 && proc.Alive(pid); i++ {
		time.Sleep(10 * time.Millisecond)
	}
	spw.reconcileTick()
	spw.Wait()

	spw.mu.Lock()
	newPID := spw.instances["default/m"][0].PID
	tracked := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if tracked != 1 {
		t.Fatalf("tracked %d, want 1 after respawn", tracked)
	}
	if newPID == pid || newPID <= 0 {
		t.Errorf("expected a fresh pid after respawn, got %d (old %d)", newPID, pid)
	}
	if spw.respawnedTotal == 0 {
		t.Error("respawnedTotal not incremented")
	}
}

func TestReconcileTick_OneOffCrashResetsBackoff(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("m", "Survival_1", 1))

	spw.reconcileTick() // spawn
	spw.Wait()
	spw.mu.Lock()
	pid := spw.instances["default/m"][0].PID
	spw.mu.Unlock()
	_ = syscall.Kill(pid, syscall.SIGKILL)
	for i := 0; i < 200 && proc.Alive(pid); i++ {
		time.Sleep(10 * time.Millisecond)
	}

	spw.reconcileTick() // reap (failures=1) + respawn
	spw.Wait()
	spw.reconcileTick() // survives a clean tick -> reset
	spw.Wait()

	spw.mu.Lock()
	_, hasBackoff := spw.backoff["default/m"]
	spw.mu.Unlock()
	if hasBackoff {
		t.Error("backoff state should be cleared after a respawn survives a clean tick")
	}
}

func TestReconcileTick_DesiredZeroLeftAlone(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	spw.store.Create(sssObj("m", "Survival_1", 0)) // Director scaled to 0
	spw.reconcileTick()
	spw.Wait()
	if used, _, _ := spw.pool.Stats(); used != 0 {
		t.Errorf("loop spawned a desired=0 map: pool used = %d, want 0", used)
	}
}

func TestReconcileTick_BacksOffAfterRepeatedCrashes(t *testing.T) {
	spw, clk := newLoopSpawner(t)
	spw.store.Create(sssObj("m", "Survival_1", 1))
	const k = "default/m"

	// Stand in for two prior crashes the sweep would have recorded: failures=2
	// puts the map in a 1m backoff window from "now".
	spw.recordFailure(k)
	spw.recordFailure(k)

	// A tick during the backoff window must NOT respawn.
	spw.reconcileTick()
	spw.Wait()
	if used, _, _ := spw.pool.Stats(); used != 0 {
		t.Errorf("respawned during backoff: pool used = %d, want 0", used)
	}
	if got := spw.Snapshot().Maps[0].Status; got != "failing" {
		t.Errorf("status = %q, want failing during backoff", got)
	}

	// Once the window elapses, a tick respawns.
	clk.Advance(2 * time.Minute)
	spw.reconcileTick()
	spw.Wait()
	if used, _, _ := spw.pool.Stats(); used != 1 {
		t.Errorf("did not respawn after backoff elapsed: pool used = %d, want 1", used)
	}
}

func TestReconcile_DisabledReturnsImmediately(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	done := make(chan struct{})
	go func() { spw.Reconcile(testCtx(t), 0); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Reconcile(interval=0) did not return immediately")
	}
}

func TestReconcile_StopsOnContextCancel(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	ctx, cancel := contextWithCancel()
	done := make(chan struct{})
	go func() { spw.Reconcile(ctx, 20*time.Millisecond); close(done) }()
	time.Sleep(60 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Reconcile did not stop after context cancel")
	}
}
```

Add the small context helpers at the top of the test file's body (after the imports) — and add `"context"` to the test imports:

```go
func testCtx(t *testing.T) context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	return ctx
}

func contextWithCancel() (context.Context, context.CancelFunc) {
	return context.WithCancel(context.Background())
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `go test ./internal/spawner/ -run 'TestReconcileTick|TestReconcile_'`
Expected: FAIL — `spw.reconcileTick undefined`, `spw.Reconcile undefined`.

- [ ] **Step 3: Implement `reconcileTick` and `Reconcile`**

Append to `internal/spawner/reconcile_loop.go` (add `"context"` to the import block):

```go
// Reconcile runs the self-healing loop until ctx is cancelled. interval <= 0
// disables the loop (it records the disabled state and returns). Intended to
// run in its own goroutine.
func (s *Spawner) Reconcile(ctx context.Context, interval time.Duration) {
	s.mu.Lock()
	s.reconcileInterval = interval
	s.mu.Unlock()
	if interval <= 0 {
		slog.Info("spawner: reconcile loop disabled (interval <= 0)")
		return
	}
	slog.Info("spawner: reconcile loop started", "interval", interval)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			slog.Info("spawner: reconcile loop stopped")
			return
		case <-t.C:
			s.reconcileTick()
		}
	}
}

// reconcileTick runs one sweep + up-reconcile pass under reconcileMu. It reaps
// dead/phantom instances, respawns each map up to desired (honoring backoff),
// and resets the backoff of any map that has survived a clean tick.
func (s *Spawner) reconcileTick() {
	s.reconcileMu.Lock()
	defer s.reconcileMu.Unlock()

	reaped := s.sweep()

	objs := s.store.List(reconcileNamespace)
	spawned := make(map[string]bool, len(objs))
	desiredByKey := make(map[string]int, len(objs))
	for _, obj := range objs {
		key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
		desiredByKey[key] = readReplicas(obj.Spec)
		if n := s.reconcileUpLocked(obj, true); n > 0 {
			spawned[key] = true
			s.mu.Lock()
			s.respawnedTotal += int64(n)
			s.mu.Unlock()
		}
	}

	// Reset backoff for maps that survived a clean tick (no reap, no spawn,
	// already at desired).
	s.mu.Lock()
	for key, desired := range desiredByKey {
		if reaped[key] || spawned[key] {
			continue
		}
		if _, has := s.backoff[key]; has && len(s.instances[key]) >= desired {
			delete(s.backoff, key)
		}
	}
	s.reconcileSweeps++
	s.lastSweep = s.now()
	s.mu.Unlock()
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `go test ./internal/spawner/ -run 'TestReconcileTick|TestReconcile_' -v`
Expected: PASS (all five).

- [ ] **Step 5: Full spawner suite, plain and race**

Run: `go test ./internal/spawner/ && go test -race ./internal/spawner/`
Expected: PASS both.

- [ ] **Step 6: Commit**

```bash
git add internal/spawner/reconcile_loop.go internal/spawner/reconcile_loop_test.go
git commit -m "feat(mock-k8s): self-healing reconcile tick + loop"
```

---

## Task 6: Health endpoints (`/status` + `/metrics`)

**Files:**
- Create: `internal/health/health.go`
- Test: `internal/health/health_test.go`

- [ ] **Step 1: Write the failing test**

Create `internal/health/health_test.go`:

```go
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./internal/health/`
Expected: FAIL — package/handlers do not exist.

- [ ] **Step 3: Implement `health.go`**

Create `internal/health/health.go`:

```go
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `go test ./internal/health/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add internal/health/health.go internal/health/health_test.go
git commit -m "feat(mock-k8s): /status (JSON) + /metrics (Prometheus) handlers"
```

---

## Task 7: Wire the loop + endpoints into main

**Files:**
- Modify: `cmd/mock-k8s/main.go`
- Test: `cmd/mock-k8s/main_test.go` (interval parsing) — create if absent.

- [ ] **Step 1: Write the failing test for interval parsing**

Create (or append to) `cmd/mock-k8s/main_test.go`:

```go
package main

import (
	"testing"
	"time"
)

func TestParseReconcileInterval(t *testing.T) {
	cases := []struct {
		in   string
		want time.Duration
	}{
		{"", 30 * time.Second},          // default
		{"45s", 45 * time.Second},       // explicit
		{"1m", time.Minute},             // explicit
		{"0", 0},                        // disabled
		{"off", 0},                      // disabled
		{"garbage", 30 * time.Second},   // unparseable -> default
		{"-5s", 0},                      // non-positive -> disabled
	}
	for _, c := range cases {
		if got := parseReconcileInterval(c.in); got != c.want {
			t.Errorf("parseReconcileInterval(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./cmd/mock-k8s/ -run TestParseReconcileInterval`
Expected: FAIL — `parseReconcileInterval undefined`.

- [ ] **Step 3: Add the parse helper**

In `cmd/mock-k8s/main.go`, add (near the other helpers, e.g. next to `envOr`):

```go
// parseReconcileInterval reads the reconcile-loop interval. Empty/unparseable
// values fall back to 30s; "0", "off", or a non-positive duration disables the
// loop.
func parseReconcileInterval(v string) time.Duration {
	const def = 30 * time.Second
	switch strings.TrimSpace(strings.ToLower(v)) {
	case "":
		return def
	case "off", "false", "no":
		return 0
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	if d <= 0 {
		return 0
	}
	return d
}
```

Ensure `"time"` and `"strings"` are imported (both already are in main.go).

- [ ] **Step 4: Run the test to verify it passes**

Run: `go test ./cmd/mock-k8s/ -run TestParseReconcileInterval -v`
Expected: PASS.

- [ ] **Step 5: Wire the loop and endpoints**

In `cmd/mock-k8s/main.go`, add the `health` import:

```go
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/health"
```

After the existing `/healthz|/livez|/readyz` handler registrations (right before `handler := server.LogMiddleware(mux, 4096)`), add:

```go
	mux.HandleFunc("/status", health.StatusHandler(spw.Snapshot))
	mux.HandleFunc("/metrics", health.MetricsHandler(spw.Snapshot))
```

Then, after the server is constructed and before `server.Run(ctx, srv)` (the `ctx` from `signal.NotifyContext` is already in scope), start the loop:

```go
	reconcileInterval := parseReconcileInterval(os.Getenv("MOCK_K8S_RECONCILE_INTERVAL"))
	slog.Info("self-healing reconcile", "interval", reconcileInterval, "enabled", reconcileInterval > 0)
	go spw.Reconcile(ctx, reconcileInterval)
```

(Place these three lines immediately after `ctx, cancel := signal.NotifyContext(...)` / `defer cancel()` so `ctx` is defined.)

- [ ] **Step 6: Build and vet the whole module**

Run: `go build ./... && go vet ./...`
Expected: clean.

- [ ] **Step 7: Full test suite, plain and race**

Run: `go test ./... && go test -race ./...`
Expected: PASS, all packages.

- [ ] **Step 8: gofmt check (only the pre-existing 3 files may show)**

Run: `gofmt -l . `
Expected: at most `cmd/mock-k8s/main.go`, `internal/battlegroup/loader.go`, `internal/pool/pool.go` (pre-existing). If any NEW file appears, run `gofmt -w <file>` and re-commit.
Note: if your `main.go` edits land in the already-dirty `buildPlaceholders` region they won't add new dirt; if `gofmt -l` flags a *new* file, fix it.

- [ ] **Step 9: Commit**

```bash
git add cmd/mock-k8s/main.go cmd/mock-k8s/main_test.go
git commit -m "feat(mock-k8s): start reconcile loop + mount /status and /metrics"
```

---

## Task 8: RestoredAtBoot + persist-error counters

Wire the two remaining counters into their existing call sites so `/status` reports them.

**Files:**
- Modify: `internal/spawner/spawner.go` (`Restore`, `persist`)
- Test: `internal/spawner/stats_test.go` (append)

- [ ] **Step 1: Write the failing test**

Append to `internal/spawner/stats_test.go`:

```go
func TestPersist_CountsErrors(t *testing.T) {
	spw, _ := newBareSpawner(t)
	// Point statePath at an un-writable location to force a Save error.
	spw.statePath = filepath.Join(t.TempDir(), "nope", "\x00bad", "state.json")
	spw.persist()
	snap := spw.Snapshot()
	if snap.Persist.Errors == 0 {
		t.Error("persist error not counted")
	}
	if snap.Persist.LastError == "" {
		t.Error("lastPersistError not recorded")
	}
}
```

(`Restore`'s `RestoredAtBoot` is covered indirectly by the existing
`TestSpawner_RestoreReadoptsLiveInstance`; assert the counter there in Step 4.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `go test ./internal/spawner/ -run TestPersist_CountsErrors`
Expected: FAIL — `Persist.Errors` stays 0.

- [ ] **Step 3: Count persist errors**

In `internal/spawner/spawner.go`, in `persist()`, replace the save-error log:

```go
	if err := state.Save(s.statePath, st); err != nil {
		slog.Error("spawner: persist state failed", "path", s.statePath, "err", err)
		return
	}
	s.lastSavedGen = gen
```

with:

```go
	if err := state.Save(s.statePath, st); err != nil {
		slog.Error("spawner: persist state failed", "path", s.statePath, "err", err)
		s.mu.Lock()
		s.persistErrors++
		s.lastPersistError = err.Error()
		s.mu.Unlock()
		return
	}
	s.lastSavedGen = gen
```

(`persist()` holds `saveMu` here, not `s.mu`, so taking `s.mu` briefly is safe — no reentrancy.)

- [ ] **Step 4: Count RestoredAtBoot**

In `Restore()`, after the loop finishes and just before the final `s.persist()`, add:

```go
	s.mu.Lock()
	s.restoredAtBoot += int64(adopted)
	s.mu.Unlock()
```

Then add an assertion to the existing `TestSpawner_RestoreReadoptsLiveInstance` (in `restore_test.go`), right after `spw.Restore()`:

```go
	if spw.Snapshot().Instances.RestoredAtBoot != 1 {
		t.Errorf("RestoredAtBoot = %d, want 1", spw.Snapshot().Instances.RestoredAtBoot)
	}
```

- [ ] **Step 5: Run the affected tests**

Run: `go test ./internal/spawner/ -run 'TestPersist_CountsErrors|TestSpawner_RestoreReadoptsLiveInstance' -v`
Expected: PASS.

- [ ] **Step 6: Full module verification**

Run: `go build ./... && go vet ./... && go test ./... && go test -race ./...`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add internal/spawner/spawner.go internal/spawner/stats_test.go internal/spawner/restore_test.go
git commit -m "feat(mock-k8s): count restored-at-boot + persist errors in /status"
```

---

## Final verification (after Task 8)

- [ ] Run from a fresh build: `go build ./... && go vet ./... && go test ./... && go test -race ./...` — all green.
- [ ] `gofmt -l .` shows only the 3 pre-existing files.
- [ ] Manual smoke (optional, needs a built binary): set `MOCK_K8S_RECONCILE_INTERVAL=5s`, start mock-k8s, `curl -sk https://localhost:<port>/status | jq` and `curl -sk https://localhost:<port>/metrics`.
- [ ] Open a PR to `main` summarizing the loop + endpoints, referencing the spec.

## Spec coverage map

| Spec section | Task |
|--------------|------|
| §4 reconcile loop (sweep + up) | 3, 5 |
| §4.1 sweep classification (dead / phantom / starting) | 3 |
| §4.2 reconcile-up + shared `reconcileUpLocked` | 4 |
| §4.3 reset after a clean tick | 5 |
| §5 crash-loop backoff curve + cap | 1 |
| §6 stats model + `Snapshot()` | 2, 8 |
| §7.1 `/status` JSON | 6 |
| §7.2 `/metrics` Prometheus | 6 |
| §8 `MOCK_K8S_RECONCILE_INTERVAL` config | 7 |
| §9 concurrency (reconcileMu / s.mu, up-only) | 4, 5 |
| §10 error handling (no panic, ctx stop, persist errors) | 5, 8 |
| §11 testing (clock seam, all behaviours) | 1–8 |
