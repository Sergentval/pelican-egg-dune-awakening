# Design: mock-k8s self-healing reconcile loop + health endpoints

- **Date:** 2026-05-31
- **Status:** Approved (design); pending implementation plan
- **Scope:** `mock-k8s/` Go module only
- **Branch:** `feat/mock-k8s-self-healing`

## 1. Motivation

`mock-k8s` is the shim that lets the Dune: Awakening dedicated server (UE5) run
under Pelican Wings by faking the slice of the Kubernetes API that Funcom's
Battlegroup Director expects. It spawns/stops UE5 processes and allocates UDP
ports.

Two gaps remain after the correctness hardening (#2/#4/#6):

1. **No periodic reconcile.** Reconciliation is purely event-driven —
   `OnSpecChange` fires only when the Director patches `spec.replicas` (or a map
   is lazy-created). The `AlwaysWarm` pre-spawn fires once at boot. So if a UE5
   server **crashes mid-session**, nothing notices until the Director happens to
   patch that map again — which it may not. The map stays down silently.
2. **Observability is log-only.** The existing `/healthz`, `/livez`, `/readyz`
   handlers return a static `"ok"`. There's no way to see port-pool utilization,
   per-map health, or how many instances have been reaped/respawned without
   grepping logs.

This design adds a self-healing reconcile loop (closing gap 1) and JSON
`/status` + Prometheus `/metrics` endpoints (closing gap 2).

## 2. Goals / Non-goals

**Goals**
- A periodic loop that detects crashed UE5 instances (via the `(pid,
  start-time)` identity check from #6), reaps them, and respawns each map back
  up to its `ServerSetScale` desired replica count.
- Crash-loop backoff so a perpetually-crashing map does not hammer the host.
- A JSON `/status` and a Prometheus-text `/metrics` endpoint, both reading the
  same in-memory snapshot.
- On by default, tunable/disable-able via one env var.
- Zero new module dependencies (hand-rolled Prometheus text; module stays on
  only `gopkg.in/yaml.v3`).

**Non-goals**
- The loop does **not** scale *down*. Scale-down stays Director-driven via
  `OnSpecChange` (avoids holding the reconcile lock across a 15s SIGTERM grace,
  and avoids fighting the Director). A map at `desired==0` is left alone.
- No external metrics push / Prometheus client library; no `/metrics` auth (the
  server's threat model is a single trusted Director on a local port).
- No change to the empty-`LIST` Director-crash workaround.
- No change to the existing `/healthz|/livez|/readyz` response shape (the
  Director may probe them).

## 3. Architecture (Approach A)

The loop *is* spawner work — it sweeps and respawns the very `instances` the
spawner tracks — so it lives in the `spawner` package, split across small
single-purpose files. The HTTP handlers are pure presentation and live in a new
`internal/health` package.

| File | Responsibility |
|------|----------------|
| `internal/spawner/reconcile_loop.go` (new) | `Reconcile(ctx, interval)` ticker; per-tick `sweep` + `reconcileUp`; crash-loop backoff state machine. |
| `internal/spawner/stats.go` (new) | `Stats` counters + per-map backoff state (guarded by `s.mu`); `Snapshot()` returning an immutable copy. Snapshot types (`Snapshot`, `MapStatus`, …). |
| `internal/spawner/spawner.go` (edit) | Extract a shared `reconcileUpLocked(obj, respectBackoff)` used by both `OnSpecChange` (up-branch) and the loop; add stats/backoff fields to `Spawner`; bump counters where instances are reaped/spawned/restored/persist-fails. |
| `internal/health/health.go` (new) | `StatusHandler(get func() spawner.Snapshot) http.HandlerFunc` (JSON) and `MetricsHandler(get func() spawner.Snapshot) http.HandlerFunc` (Prometheus text). |
| `cmd/mock-k8s/main.go` (edit) | Parse `MOCK_K8S_RECONCILE_INTERVAL`; `go spw.Reconcile(ctx, interval)` off the existing shutdown ctx; mount `/status` + `/metrics`. |

No import cycle: `health` imports `spawner` (for the `Snapshot` type); `spawner`
imports neither.

## 4. The reconcile loop

```
func (s *Spawner) Reconcile(ctx context.Context, interval time.Duration)
```

- If `interval <= 0`, return immediately (loop disabled; endpoints still serve
  current/last state).
- A `time.Ticker(interval)`; on each tick call `s.reconcileTick()`; return on
  `ctx.Done()`.
- `reconcileTick()` runs under `s.reconcileMu` (the SAME lock `OnSpecChange`
  takes) so a tick can never interleave with a Director-driven reconcile.
  - The tick must not panic the process: wrap the body so a per-map error is
    logged + counted and the sweep continues.

### 4.1 Sweep (reap dead / failed instances)

Walk every tracked instance (under `s.mu`, mutating the same way `teardown`
does). Classify each instance:

- **`PID > 0`** → it has a recorded identity. Reap it if the process is gone:
  `!proc.SameProcess(PID, StartTime)`, falling back to `!proc.Alive(PID)` when
  `StartTime == 0` (legacy/restored). A dead process needs **no** SIGTERM, so
  reaping is: remove from `instances`, `pool.Release(index)`, remove the stale
  pidfile, `persist()`. This is fast — **no 15s grace under the lock.**
- **`PID == 0` and `pidReady` closed** → `capturePID` finished without ever
  reading a pid (the spawn failed / the script died before `write_pid`). This is
  the "phantom instance" case. Reap it the same way (release slot, persist) so
  reconcile can retry. (Non-blocking check: `select { case <-pidReady: …
  default: … }`.)
- **`PID == 0` and `pidReady` open** → still starting; **skip** (must not reap a
  just-launched instance whose pid hasn't been captured yet).
- **`PID == 0` and `pidReady == nil`** → should not occur; skip defensively.

Each reaped instance increments its map's `consecutiveFailures` and the global
`reapedTotal`, and records `lastReapPid`.

### 4.2 Reconcile up (respawn to desired)

For each `obj` in `s.store.List("default")`:

- Parse `mapName` / `partitionID` / `desired` exactly as `OnSpecChange` does
  (`safeInstanceName`, `readReplicas`, `readPartitionID`).
- `current = len(s.instances[key])` (after the sweep).
- If `desired > current`: call `reconcileUpLocked(obj, respectBackoff=true)`
  which spawns `desired-current` instances **unless** the map is in backoff
  (`now < nextRetry[key]`), in which case it spawns nothing and the map is
  reported `failing`.
- `desired <= current`: do nothing (loop is up-only).

`reconcileUpLocked(obj, respectBackoff)` is the shared core:
- `OnSpecChange`'s up-branch calls it with `respectBackoff=false` — an explicit
  Director patch is honored immediately regardless of backoff.
- The loop calls it with `respectBackoff=true`.

The mock-k8s namespace is `"default"` throughout (lazy-create and tests use it).

### 4.3 Reset / "survived a clean tick"

Per-map backoff state must reset only once a respawn has *survived*, not the
instant it is launched (a just-spawned instance is alive but may crash next
tick). At the end of each tick, for each map `K`:

> if `consecutiveFailures[K] > 0` **and** no instance of `K` was reaped this
> tick **and** no instance of `K` was spawned this tick **and** `current ==
> desired` → reset `consecutiveFailures[K] = 0`, clear `nextRetry[K]`.

So a crashed map must come back to desired and stay there for one full clean
tick before it is considered `healthy` again.

## 5. Crash-loop backoff

Per-map state: `consecutiveFailures int`, `nextRetry time.Time`.

On a reap (§4.1), after incrementing `consecutiveFailures` (`cf`):

| `cf` | delay before next respawn |
|------|---------------------------|
| 1 | 0 (retry next tick — no penalty for a one-off crash) |
| 2 | `baseBackoff` = 1 min |
| 3 | 2 min |
| 4 | 4 min |
| 5 | 8 min |
| ≥6 | capped at `maxBackoff` = 15 min |

Formula for `cf ≥ 2`: `delay = min(baseBackoff * 2^(cf-2), maxBackoff)`; set
`nextRetry = now + delay`. Constants: `baseBackoff = 1*time.Minute`,
`maxBackoff = 15*time.Minute`.

The loop never permanently gives up — backoff only bounds the retry rate. A map
in backoff is surfaced as `failing` with its `consecutiveFailures` and
`nextRetry` in `/status` and `/metrics`, so an operator can see it.

## 6. Stats model

In `internal/spawner/stats.go`. All mutable stats + backoff state are guarded by
`s.mu` (they change exactly where instances change, which is already under
`s.mu`). `Snapshot()` assembles a consistent copy under `s.mu`, plus
`pool.Stats()`.

```go
type Snapshot struct {
    UptimeSeconds int64
    Reconcile     ReconcileStats
    Pool          PoolStats
    Instances     InstanceStats
    Persist       PersistStats
    Maps          []MapStatus
}
type ReconcileStats struct {
    Enabled         bool
    IntervalSeconds int
    Sweeps          int64
    LastSweep       time.Time // zero if never run
}
type PoolStats struct{ Size, Used, Free int }
type InstanceStats struct {
    Tracked        int
    ReapedTotal    int64
    RespawnedTotal int64
    RestoredAtBoot int64
}
type PersistStats struct {
    Errors    int64
    LastError string
}
type MapStatus struct {
    Map                 string
    Key                 string
    Desired             int
    Current             int
    Status              string     // "healthy" | "starting" | "failing" | "idle"
    ConsecutiveFailures int        // json:",omitempty" — dropped at 0
    NextRetry           *time.Time // json:",omitempty" — nil (dropped) when not in backoff
}
```

All JSON fields use `omitempty` where a zero value is meaningful absence.
`NextRetry` is a pointer specifically because a zero `time.Time` is a struct and
would not be dropped by `omitempty`; nil renders as absent, matching the
`/status` example where `healthy` maps carry neither `consecutiveFailures` nor
`nextRetry`.

`Status` derivation per map:
- `desired == 0` → `"idle"`
- `current >= desired` → `"healthy"`
- `current < desired` **and** in backoff (`now < nextRetry`) → `"failing"`
- `current < desired` and not in backoff → `"starting"`

Counters wired into existing code paths:
- `ReapedTotal` — incremented in the sweep.
- `RespawnedTotal` — incremented when the loop spawns (not when `OnSpecChange`
  spawns on a Director patch — that's not a "respawn").
- `RestoredAtBoot` — set by `Restore()` to the number of adopted instances.
- `Persist.Errors` / `LastError` — incremented in `persist()` on `state.Save`
  failure (currently only logged).
- `Sweeps`, `LastSweep` — updated each tick.

Spawner gains (all under `s.mu`): `startedAt time.Time`, the counters above, and
`backoff map[string]backoffState` where `backoffState{ failures int; nextRetry
time.Time }`. `reconcileInterval` is stored so `/status` can report it and
`Enabled`.

## 7. Endpoints

Mounted in `main.go` on the existing mux (same TLS server). Both always
mounted; if the loop is disabled they show current/last state.

### 7.1 `GET /status` — JSON

`health.StatusHandler(spw.Snapshot)` marshals the `Snapshot` to indented JSON,
`Content-Type: application/json`. Example:

```json
{
  "uptimeSeconds": 3600,
  "reconcile": { "enabled": true, "intervalSeconds": 30, "sweeps": 120, "lastSweep": "2026-05-31T07:00:00Z" },
  "pool": { "size": 64, "used": 8, "free": 56 },
  "instances": { "tracked": 8, "reapedTotal": 3, "respawnedTotal": 3, "restoredAtBoot": 2 },
  "persist": { "errors": 0, "lastError": "" },
  "maps": [
    { "map": "Survival_1", "key": "default/dune-world-survival-1", "desired": 1, "current": 1, "status": "healthy" },
    { "map": "Overmap", "key": "default/dune-world-overmap", "desired": 1, "current": 0, "status": "failing", "consecutiveFailures": 4, "nextRetry": "2026-05-31T07:04:00Z" }
  ]
}
```

### 7.2 `GET /metrics` — Prometheus text

`health.MetricsHandler(spw.Snapshot)` hand-writes the exposition format,
`Content-Type: text/plain; version=0.0.4`. Series:

```
# HELP mock_k8s_pool_slots_used Port-pool slots currently in use.
# TYPE mock_k8s_pool_slots_used gauge
mock_k8s_pool_slots_used 8
mock_k8s_pool_slots_total 64
mock_k8s_instances_tracked 8
mock_k8s_instances_reaped_total 3
mock_k8s_instances_respawned_total 3
mock_k8s_reconcile_sweeps_total 120
mock_k8s_persist_errors_total 0
mock_k8s_map_desired{map="Survival_1"} 1
mock_k8s_map_current{map="Survival_1"} 1
mock_k8s_map_failing{map="Survival_1"} 0
mock_k8s_map_desired{map="Overmap"} 1
mock_k8s_map_current{map="Overmap"} 0
mock_k8s_map_failing{map="Overmap"} 1
```

`_total` series are typed `counter`, the rest `gauge`. The `map` label value is
escaped per the Prometheus text rules (`\`, `"`, newline); map names are already
allowlist-constrained, so escaping is defensive.

## 8. Configuration

- `MOCK_K8S_RECONCILE_INTERVAL` — Go duration string (e.g. `30s`, `1m`).
  Default `30s` when unset/empty/unparseable. A value of `0`, `off`, or a
  non-positive duration **disables** the loop (endpoints stay up). Parsed in
  `main.go`; logged at startup.

## 9. Concurrency & locking

- The loop's tick takes `s.reconcileMu` for the whole sweep+reconcile, so it
  serializes with `OnSpecChange` (same lock). Within the tick, instance/stat
  mutations take `s.mu` exactly as `teardown`/`scaleDown`/`spawnOne` already do.
- Lock order is always `reconcileMu` → `s.mu` (never the reverse), matching the
  existing code. `Snapshot()` takes only `s.mu`.
- The loop is **up-only**, so it never calls `teardown` and never holds a lock
  across the 15s SIGTERM grace.
- `spawnOne` only does a non-blocking `cmd.Start` plus a detached `capturePID`
  goroutine, so spawning under the tick lock does not block.

## 10. Error handling

- A tick never aborts the loop: a per-map spawn/parse error is logged and the
  tick continues to the next map.
- The loop stops only on `ctx.Done()` (SIGTERM/SIGINT) and returns; `main`
  already waits for in-flight `bg` goroutines via the server shutdown path.
- `persist()` failures are counted (`Persist.Errors`/`LastError`) in addition to
  the existing log line.
- Endpoints degrade gracefully: if the loop never ran, `reconcile.sweeps == 0`
  and `lastSweep` is the JSON zero time / omitted.

## 11. Testing (TDD, RED→GREEN per behaviour)

`internal/spawner/reconcile_loop_test.go`:
- **Reap + respawn:** spawn via a fake `start-ue5.sh` (existing test helper),
  `SIGKILL` the backing sleeper, run one `reconcileTick()`, assert the dead
  instance is gone, its slot is reused, and the map is respawned to desired.
- **One-off crash resets:** after a single reap+respawn that then survives a
  clean tick, `consecutiveFailures` returns to 0 / status `healthy`.
- **Crash-loop backoff:** a fake script that exits immediately (never writes a
  live pid) makes the map fail repeatedly; assert it is NOT respawned every tick
  (respects `nextRetry`), `consecutiveFailures` climbs, and status is `failing`.
  Use an injectable clock (see below) to advance time deterministically.
- **`desired==0` left alone:** a map the Director set to 0 is never spawned by
  the loop.
- **Phantom reap:** an instance with `PID==0` and a closed `pidReady` is reaped.
- **Disabled / ctx cancel:** `interval<=0` returns immediately; a cancelled ctx
  stops the loop.

`internal/spawner/stats_test.go`:
- `Snapshot()` reflects pool used/free, tracked instances, and per-map
  desired/current/status; counters increment on reap/respawn/restore/persist
  error.

`internal/health/health_test.go`:
- `/status` returns valid JSON that round-trips to the expected shape.
- `/metrics` returns text that parses into the expected series/values (assert on
  specific lines, including a `failing` map and label escaping).

**Determinism:** backoff timing needs an injectable clock. Add an unexported
`now func() time.Time` field on `Spawner` defaulting to `time.Now`, used by the
loop/backoff (mirrors the existing swappable `terminate` seam). Tests set it to
a controllable clock. (`time.Now` is otherwise fine in production.)

## 12. File-by-file change summary

- **new** `internal/spawner/reconcile_loop.go` — `Reconcile`, `reconcileTick`,
  `sweepLocked`, `reconcileUpLocked`, backoff helpers.
- **new** `internal/spawner/stats.go` — snapshot types, `Snapshot()`, counter
  helpers, status derivation.
- **edit** `internal/spawner/spawner.go` — add `startedAt`, the counters, the
  `backoff` map, `reconcileInterval`, and a `now func() time.Time` seam to
  `Spawner` (`reconcileMu` already exists); factor the `OnSpecChange` up-branch
  into `reconcileUpLocked(obj, false)`; count `RestoredAtBoot` in `Restore`;
  count persist errors in `persist`.
- **new** `internal/health/health.go` — `StatusHandler`, `MetricsHandler`.
- **edit** `cmd/mock-k8s/main.go` — parse `MOCK_K8S_RECONCILE_INTERVAL`, start
  the loop goroutine, set `spw` interval/`startedAt`, mount `/status` +
  `/metrics`.
- **new** test files as in §11.

## 13. Out of scope / future

- A `/metrics` scraper (Grafana) is not wired today; `/metrics` is provided for
  when one is. (User runs Coolify/Homarr; Homarr can display `/status`.)
- Persist schema versioning (`state.Save` already stamps `Version`).
- The `LIST` null-key Director crash (separate, environment-gated effort).
