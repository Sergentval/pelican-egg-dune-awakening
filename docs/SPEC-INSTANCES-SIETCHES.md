# Instances & Sietches management — phased spec

Port-don't-adopt of DST-DuneServerTool's instance-management surface (Sietches
experimental, per-map spin-up/shutdown, Map SpinUp) onto **our** stack, which
already owns the engine DST drives over k3s: **mock-k8s** (`ServerSetScale.spec.replicas`
→ spawner spawns/reaps UE5, reconcile-enforced + self-healing) plus the
`world_partition` / `multi-sietch-config.sh` / `start-ue5-dimensions.sh`
dimensional machinery.

DST mutates a `battlegroup` CRD in real k3s via `kubectl patch`. We drive the
equivalent through mock-k8s's `serversetscales` API (`PATCH .../serversetscales/<world>-<map>`
`{spec:{replicas:N}}`) — the exact, idempotent, reconcile-reinforced contract the
Funcom Director already uses. `fix-on-demand-maps` (DST's pinned-partition reset)
is already solved structurally for us (deterministic `ExtractMapPartitions`, PR #12),
so it is not re-ported.

All writes reuse admin-http's session/jti auth + CSRF and return HTTP 200 +
`ok` (Cloudflare-safe); all DB access goes through `admin-publish.sh`'s
`dune_psql`/`dune_psql_q` (Funcom psql `-c` has no `:'var'` interpolation).

---

## Phase 0 — Topology VIEW (read-only) [this PR]

A new 🧩 **Instances** tab that shows the full instance + partition topology by
combining the existing `/api/status` (per-map desired/current/status/players)
with a new partition read.

- **`admin-publish.sh world-partition-list`** — read-only: `SELECT partition_id,
  map, dimension_index, label, blocked, server_id FROM dune.world_partition`,
  LEFT JOIN `dune.farm_state` (game_port, ready) on `server_id` so the UI can show
  which dim partitions are actually live. Emits pipe lines.
- **`GET /api/partitions`** (admin-http) — parses that into
  `{partitions:[{partition_id, map, dimension, label, blocked, server_id,
  game_port, ready}]}`.
- **🧩 Instances tab** — per-map cards: scale (current/desired + status pill from
  `/api/status`), live player count, and the map's partitions split into **warm**
  (dimension 0) vs **dimensional** (101/102/…), each flagged live/declared. Pool
  + reconcile + uptime header. Read-only; no writes.

Risk: none (read-only). Delivers the "see every DD/sietch instance + partition"
view DST lacks at this fidelity.

## Phase 1 — Map spin-up / shutdown / scale (mock-k8s engine)

Drive the proven `ServerSetScale` path; covers DST feature #2 (per-map
spin-up/shutdown) and "manage the DeepDesert instance" at the map level.

- **mock-k8s call**: `PATCH https://127.0.0.1:6443/apis/igw.funcom.com/v1/namespaces/default/serversetscales/<world>-<map>`
  `{"spec":{"replicas":N}}` (merge-patch), `Authorization: Bearer $AMP_TOKEN`, CA-pinned
  (unverified fallback). `replicas:0` = stop, `1` = start, `N` = N instances.
  Name = `ServerSetScaleName(DUNE_WORLD_NAME, map)` (lowercase, `_`→`-`). `GET`
  first to lazy-create if absent. Driven from admin-publish.sh (`instance-scale <map> <n>`)
  or a small admin-http helper.
- **Player-online guard** (DST parity): before a scale-down, count online players
  on that map (reuse `server-status`); if `>0` and not `force`, return
  `{ok:false, requiresConfirmation:true, players, ids}`; the SPA confirms →
  re-POST with `force:true`.
- **`POST /api/instances/{map}/scale` `{replicas, force?}`** + Start/Stop/scale
  buttons on the Instances tab.

Risk: low — drives the same self-healing engine the Director uses; idempotent.
Caveat: desired replicas are in-memory in mock-k8s (lost on its restart; re-derived
from AlwaysWarm) — surface that in the UI.

## Phase 2 — DeepDesert dimension runtime control

The per-player tunnel partitions (101/102/103) are outside mock-k8s — boot-only,
no teardown today. Add runtime control.

- **Spin up a dimension**: INSERT a `world_partition` dim row (id from the
  `multi-sietch-config.sh` range) + live `world-template.yaml`/CR patch (today
  boot-only, marker-gated) + spawn (targeted `start-ue5-dimensions.sh` or a
  per-partition spawn).
- **Spin down a dimension** (the real gap): SIGTERM the specific UE5 by
  partition/port, free the port slot, NULL `server_id`, DELETE the row, remove the
  YAML/CR entry.
- **`GET /api/instances/dimensions` + POST add/remove** + dimension controls on
  the Instances tab (per DeepDesert/Arrakeen/Harko).

Risk: medium — new partition-keyed teardown; must not disrupt the warm instance or
in-tunnel players. Live-verify on the disposable dev char.

## Phase 3 — Survival_1 experimental shards ("sietch")

DST's experimental page: add/remove extra Survival_1 instances for capacity.
Net-new for us — no sharding model exists.

- Extend `multi-sietch-config.sh`'s id-range model to survival maps; allocate a
  shard partition id; seed `world_partition` + world-template; set ServerSetScale
  `replicas>1` (engine supports it).
- **Verify Director/IGW routing** to the extra shard (the risky unknown).
- Gated behind an "I understand" unlock + RAM-budget warning (mirror DST). Needs a
  battlegroup/container restart to apply.

Risk: high / experimental — Funcom-unsupported, routing unverified, RAM-heavy.
Ships behind an explicit unlock.

---

## Cross-phase notes

- **mock-k8s auth/TLS**: HTTPS :6443 self-signed + `AMP_TOKEN` bearer; admin-http
  already CA-pins it for `/status` (`fetch_mock_status`). Reuse that for the PATCH.
- **Never `DELETE` a ServerSetScale to stop** — it drops desired state without a
  graceful teardown (orphaned UE5 holding its UDP port). Use `replicas:0`.
- **Read vs write split**: every phase keeps reads in `/api/status` + `/api/partitions`
  and writes behind auth+csrf, allowlisted, with the player-online guard on
  anything that can disconnect players.
