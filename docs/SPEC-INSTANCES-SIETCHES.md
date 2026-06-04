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

## Phase 0 — Topology VIEW (read-only) [DONE, PR #31 merged]

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

## Phase 1 — Map spin-up / shutdown / scale (mock-k8s engine) [this PR]

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

## Phase 2 — DeepDesert dimension runtime control [this PR — spin down/up of configured dims]

Scope note: this PR ships **spin down / spin up of already-configured dims**
(101/102/103) — the clean, reversible, restart-free core (the decompiled Director
routes on `world_partition.server_id` → `farm_state`, and our
`IGNORE_IGWO_API_SERVER_CHECK` neutralizes the CR brown-gate, so down = NULL
server_id + kill is graceful and up = respawn at the canonical port + writeback
routes within a tick). Changing the dim COUNT (add/remove a partition id) stays a
boot-config op (`DUNE_DD_DIMENSIONS`) for persistence consistency, since
prestart re-seeds from the env on each boot.


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

## Phase 3 — Multi-Sietch: player-chosen Survival_1 instances [DONE, this PR]

Reframed from "auto-balanced capacity shards" to the **official model**: many
**Sietches** (each = a `Survival_1` partition keyed by `dimension_index`) on one
world, all sharing the same DeepDesert / Arrakeen / Harko (the Director keys hubs
by map name), with the player **choosing** their Sietch.

DE-RISKING FINDING: **Survival_1 already runs `InstancingMode=Dimension`** — the
Director reads Funcom's own `extracted/.../BattlegroupDirector/director_config.ini`
(its cwd), which sets `Survival_1=Dimension` (DimensionMaps:2 = Survival_1 +
DeepDesert; our egg's `DeepDesert_1=ClassicalInstancing` line is dead). So NO
instancing-mode flip is needed — a Sietch is just a `Survival_1` `world_partition`
row with `dimension_index>0`, spawned by the Phase-2 dimension machinery, and the
Director routes the client's `TargetDimension` to it (persisted per-player via
`save_login_target_dimension`). New rows are picked up live (cache refresh, no
restart), exactly like dimensions.

Build (all reuses Phase 2):
- `multi-sietch-config.sh` `DUNE_SURVIVAL_SIETCHES` (+ id base 200) +
  `survival_sietch_values()`; `prestart.sh` step 6c seeds the extra sietches
  (dim 1..N) for persistence. dim 0 = the stock Abbir sietch.
- `admin-publish.sh` `sietch-add` (INSERT next Survival_1 dim row + background
  `spawn-dimension.sh`) / `sietch-remove` (Phase-2 teardown + DELETE row;
  refuses Abbir/dim 0). Spin up/down of a sietch reuses dimension-up/down.
- `admin-http.py` `POST /api/sietches` + `POST /api/sietches/<pid>/remove`
  (player-online guard); `InstancesTab` "➕ Add Sietch" + ✕-remove on Survival_1
  dims. api.ts addSietch/removeSietch.

Live-verified server-side on server 30: added a sietch live (no restart) →
Abbir + "Sietch 2" both live (`farm_state ready`), the Director tracks them as
`Survival_1_0` / `Survival_1_1`; add/remove return instantly; DD/hubs untouched.

THE ONE REMAINING UNKNOWN (client-side, needs the game): does the FLS server
browser list + let a player PICK the extra sietches? The pre-connect picker is a
Funcom-FLS-backend feature a private host can only push declarations to (each
carries a `Dimension`); whether real FLS shows multiple Sietch declarations under
one self-host world is not verifiable without a live client. The encouraging
sign: the Director already password-tracks per-sietch (`Survival_1_0/1/2`). If the
browser does NOT surface them, the fallback is per-player home-sietch assignment
(`save_login_target_dimension`) and/or in-game travel.

## Phase 3 (original framing) — Survival_1 experimental shards ("sietch")

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
