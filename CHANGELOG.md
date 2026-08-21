# Changelog

All notable changes to the Pelican egg for Dune: Awakening.

Dated sections, newest first. Entries reference the pull request (`#NN`) or
commit that shipped them. Sections up to and including 2026-08-20 were
reconstructed retroactively from the git and PR history on 2026-08-20; from
here on the log is maintained with each merge.

**How to pick up changes on an existing server** — two different levers:

- Changes to *scripts, panel, or runtime* → **Reinstall** the server in
  Pelican (world data under `server/state/` survives; `data/admin/` daemon
  configs are preserved).
- Changes to *egg variables or their validation rules* → **re-import the
  egg JSON** into the panel, then Reinstall. An imported egg is a copy; the
  panel never picks up new variables on its own.

## 2026-08-21 — Guild management (dune-admin port)

- New **Guilds** tab (Players group) + `guild-*` subcommands, ported from
  Icehunter/dune-admin #117 (MIT). Reads: all guilds with faction + member
  count, per-guild roster (canonical controller ids, roles, online status)
  and pending invites. Writes go through the game's own guild procs — they
  self-acquire the guild advisory lock and `pg_notify('guild_notify_channel')`
  so the running maps apply changes **live**: set description, transfer
  leadership (100 = the single leader slot; promoting demotes the sitting
  leader to 50), kick (refuses the leader loudly where the game proc would
  silently skip), disband, and admin-side guild creation (name uniqueness +
  per-player cap enforced by the game). Rename is the one lock-guarded
  UPDATE — no game proc or notify verb exists, so it shows in-game after the
  next restart. Every write is verified by re-read. Beyond dune-admin's
  surface: kick, disband and create are new.

## 2026-08-21 — Database tab + base owner resolution survives the DD wipe

- **Bug fix — Deep Desert bases showed "unclaimed".** The Bases owner column
  resolved the custodian through `permission_actor_rank.player_id → dune.actors
  → account`, but the Deep Desert weekly wipe rotates the player-controller
  actor, orphaning that middle link so a very-much-owned base read as
  unclaimed. Owner resolution now falls back to the **base actor's own
  `owner_account_id`** (which rides with the base across the wipe) when the rank
  path comes up empty — strictly additive, so it only ever replaces "unclaimed"
  with the real owner, never changes a base that already resolved.
- **New: Database tab (read-only).** The backend already exposed
  `POST /api/database/sql` (SELECT / WITH / EXPLAIN / SHOW only, capped at 200
  rows) but nothing in the UI surfaced it — so inspecting the game DB meant
  shelling into Postgres on the host. There's now a first-class query console
  in the panel, with one-click presets for base owners, Deep Desert
  coordinates, and the table list. Writes are rejected server-side.

## 2026-08-21 — Live Map shows player coordinates (self-service calibration)

- Player markers now expose their **raw world (x, y)** on hover, on every map —
  the data was always fetched to plot the dot, just never shown. On Deep Desert
  a new **Grid calibration** side-panel readout lists each live player with
  their raw coords and the sector the current projection lands them in. This
  closes a gap: grid calibration (#116) previously meant asking an operator to
  run a `db-sql` query, but the panel exposes no database tool — now the two
  calibration points (raw coords + real in-game sector) can be read straight
  from the panel with no SQL.

## 2026-08-21 — Item edit (quantity / quality)

- New `item-edit <item_id> [stack=N] [quality=N]` — the missing verb between
  `give-item` (INSERT) and `item-delete` (DELETE). Edits one stack's quantity
  and/or quality in place via a bounded direct `UPDATE dune.items` (no safe
  server proc exists), reusing `item-delete`'s ownership resolution and gating:
  player-carried items require the owner **offline**, world/base items require
  the **map down** (the running map caches inventory in memory and would clobber
  a live edit). Bounds: stack 1..1,000,000; quality capped at the highest tier
  the world already proves (floor 6) so an out-of-domain tier can't ghost the
  item. The write is re-read and verified before reporting success. Surfaced as
  an ✎ edit control on every item row in the **Player inventory** tab and in
  **base containers**; HTTP `POST /api/items/<item_id>/edit`.

## 2026-08-21 — Coriolis seed control

- The Deep Desert side panel gained a **Coriolis seed control**. It reads the
  effective `m_ForcedCoriolisWorldSeed` override and lets you pin one of the
  12 fixed layouts (or restore automatic weekly rotation) from a picker that
  previews each seed's POI composition — count, large-spice sectors and
  confidence — drawn from the same Wick Maps catalogue. The setting was
  already writable in the Settings tab as a bare integer; this turns it into
  an informed choice tied to the map. Applying writes through the validated
  settings path (no new endpoint) and **takes effect at the next Deep Desert
  regeneration** (cycle end / DB wipe), not on the running map; picking is a
  two-step (select → Apply) to prevent an accidental repin. Backend adds
  `admin_wickmaps.layouts_summary` (unit-tested) and enriches
  `GET /api/map/deepdesert-layout` with `forcedSeed`, `forcedSeedExplicit`
  and `summaries`.

## 2026-08-21 — Deep Desert sector map (Wick Maps)

- The Live Map's Deep Desert now draws a proper **9x9 sector grid** (A-I ×
  1-9) and overlays the week's points of interest — wrecks, caves, spice
  fields, testing stations, titanium, stravidium, taxi. The Deep Desert
  cycles through 12 fixed layouts by Coriolis seed; the panel detects the
  active seed and shows the matching POIs, so it self-updates each cycle
  with no re-rendering and no terrain image (nothing goes stale). Side
  panel shows the seed, confidence and a legend. Ported from DST
  (Apache-2.0) — see ATTRIBUTION.md. Grid bounds are being calibrated
  against real player positions (#116).

## 2026-08-21 — Panel UI updates no longer need a hard refresh

- `index.html` was served with no cache headers, so browsers applied
  heuristic freshness and kept pointing at the previous build's
  content-hashed JS — a reinstall silently served the old UI (this is
  why the #116 map fix looked undeployed). `index.html` is now
  `no-cache, must-revalidate`; hashed `/assets/` get an immutable
  long max-age. Every future frontend change lands on the next reload.

## 2026-08-21 — Live map: Deep Desert pins were vertically mirrored

- A player at the Deep Desert southern arrival zone rendered at the top
  of the live map (#116): world Y grows southward in this game and the
  DD map config was missing the vertical flip that Hagga Basin and
  Arrakeen already carry. Harko Village had the same latent flaw — both
  fixed; the click-to-teleport picker follows the same transform. DD
  bounds remain estimates: report any residual offset with a known
  in-game position and they'll be calibrated from it.

## 2026-08-21 — Player events + battlepass

- **Live player events** (🎯 sub-tab in Events & Diagnostics): zone races
  pay listed participants who reach a sphere (first in list order, one
  per tick); milestones pay every online player crossing a level or
  holding an achievement tag. Rewards are a shared spec (solaris, items,
  specialization XP) delivered in order with a claim ledger — each player
  is paid exactly once, partial failures resume without re-paying, and a
  daemon restart never re-announces past deeds.
- **Battlepass** (🎖 sub-tab): a 188-tier catalog (1,619 intel + 86
  schematic tiers, extracted from dune-admin's own generator) over
  levels, quests and exploration. Pre-existing progress baselines and is
  never paid; a tier earns only when the engine watches it happen
  (`award_past` opts out — set it BEFORE a player's first scan). Intel
  delivery is money-safe and waits for the player to be offline;
  demote/purge resets ship with the storm-safe semantics upstream
  documented the hard way.
- Both engines are OFF by default, run in one self-pacing daemon, touch
  the game only through the audited command layer, and were proven
  single-payer under 6-way process races. New grant subcommands:
  `award-intel` (clamped ≤2779, offline-gated) and `award-track-xp`
  (clamped ≤44,182).
- Ported from Icehunter/dune-admin's events + battlepass engines (MIT) —
  see ATTRIBUTION.md.

## 2026-08-21 — Deep Desert per-partition PvP

- **Per-instance PvP designation** via `DUNE_PVP_PARTITIONS` (e.g.
  `8,101,102,103` for the hot Deep Desert and the three tunnels): rendered
  as Funcom's own `+m_PvpEnabledPartitions=<id>` lines in the shared
  UserGame.ini — the syntax their template documents in a comment. In-game
  PvP labels and rules confirmed on all four partitions by the issue
  reporter, plus a full-day dual-Deep-Desert soak. Fixes #106. (#109)
- `DUNE_DD_PICKER_ROUTING` flips the DeepDesert_1 matchmaker rule from
  FirstOfGroup to HomeDimension so the in-game destination picker's choice
  actually routes (Survival_1 already ships HomeDimension — the "honour
  the choice" rule).
- Five verified QoL settings join the catalogue (reconnect grace ×2, ping
  system ×3); the settings engine gains repeated `+key=` handling and a
  drift sentinel on the matchmaker tuples.
- `DUNE_EGG_REF` is now a declared egg variable (default `main`) — it had
  always been consumed by the install script without being declared.
  **Upgrade: re-import the egg, then Reinstall.**

## 2026-08-20 — World reset, gated and reversible (C6)

- **Season resets without fear.** `world-reset-arm "RESET WORLD"` verifies
  zero players online, takes a verified database backup (optionally a
  per-character backup sweep too), and writes a durable marker — the world
  is untouched until the NEXT RESTART, which sets the current datadir
  aside (moved, never deleted) and boots a fresh, empty world under the
  same battlegroup identity, tokens, and config. 🌍 card in the Scheduler
  tab, chained to the restart-now flow.
- **Rollback is a swap**: `world-rollback-arm "ROLL BACK WORLD"` restores
  the preserved world at the next boot; progress on the fresh world is
  parked (`pg.rolled-back-<ts>`), not lost. Retention: 2 preserved
  pre-reset worlds, 1 rolled-back world.
- Every gate fails closed: confirmation phrases (re-validated
  server-side), zero-online check, backup re-verified at arm AND at boot,
  and any doubt boots the old world untouched. The boot hook can
  structurally never brick the boot.
- Proven live end-to-end on a real server: reset → fresh world (the
  wipe-guard's armed boot re-apply re-patched it automatically) →
  rollback → original world back, characters and permissions intact.
- Pairing with character backups: after a reset, a player joins the fresh
  world once, then `char-restore` brings their old character back.
- Ported from coastal-ms/DST-DuneServerTool's worldreset-2 (Apache-2.0),
  reshaped for this single-container stack — see ATTRIBUTION.md.

## 2026-08-20 — Base backup wipe-guard (C3.5)

- **Stored base backups can now survive the weekly Deep Desert reset.** A
  base backup is not a blob: the game keeps the actor rows in state
  `'BaseBackup'`, and Funcom's season cleanup deletes every actor whose
  state is not Travel/VehicleBackup/VehicleRecovery — `'BaseBackup'` is
  missing from that list, so allowing the backup tool in the Deep Desert
  fed stored backups to the wipe. The guard adds the one missing
  exclusion to the live cleanup function: anchored (refuses a function
  body it does not recognise), byte-preserving, idempotent, verified by
  re-reading the definition after every write. `base-guard-status` /
  `base-guard-apply` / `base-guard-revert`, plus a 🛡 card in the Bases
  tab. Commit `9a42b8d`.
- Optional **boot re-apply** (`data/admin/base-guard.json`, off by
  default): the guarded function is Funcom-owned and a game update can
  replace it, so the entrypoint re-patches right after `migrate-db` when
  armed. Never blocks the boot.
- The setting that makes this matter — **Base Backup Tool Allowed Maps**
  (`m_BaseBackupToolMapRestriction`) — joins the settings catalogue (196
  entries); add `DeepDesert` to it to let players use the backup tool
  there.
- Behaviour proven against Funcom's real cleanup function on a live
  server: with the guard, a `BaseBackup` actor survives the wipe; without
  it, it is deleted.
- Ported from coastal-ms/DST-DuneServerTool v13.3.0 BaseBackupGuard
  (Apache-2.0) — see ATTRIBUTION.md.

## 2026-08-20 — Base permission writes (C3.4)

- Edit base permissions from the panel and CLI: set/add a player's rank
  (1 = Owner, 2 = Co-Owner, 3 = Associate), remove a player, and a
  player picker limited to ids the game actually honours
  (`base-permission-set` / `base-permission-remove` /
  `base-permission-candidates`). Commit `2b891c6`.
- Transfer a base's ownership to a reserved **system custodian**
  (`base-transfer-custodian`): existing access is preserved, the outgoing
  Owner becomes Co-Owner, and the reserved Server persona (Red-Blink
  Care-Package-compatible tuple) is created on first use. Reversible.
- These writes apply **to the running map immediately** — they go through
  the game's own stored procedures, which notify live servers; no restart
  and no map-down gate. (Direct DML on `permission_actor_rank` is the trap:
  it skips the marker refresh + notify and the running map reverts it.)
- Server-enforced invariants: exactly one Owner (promote demotes the old
  Owner in the same transaction), the `m_MaxPermissionsPerActor` roster cap,
  claimed-base checks with friendly refusals.
- The permission roster read gains a `canonical` flag (marks rows the game
  ignores) and labels for the reserved Server/GM identities.
- Ported from Red-Blink's base permission editor (MIT) — see ATTRIBUTION.md.

## 2026-08-20 — Ecosystem port wave (six features in one day)

- **Per-character backup/restore** via the game's native transfer subsystem:
  `char-backup`, `char-restore`, retention pruning, and a pre-delete safety
  net on `account-delete`. Two upstream dune-admin bugs fixed in passing
  (respawn-uuid collision on self-restore; account-id reuse on import). (#110)
- **Connection doctor**: 11 read-only checks (advertised IP, ports,
  readiness, FLS heartbeat…) behind `GET /api/doctor` and a 🩺 card on the
  Overview tab. (#111)
- **Bases inventory + water management**: claimed-base list, per-type water
  levels (cisterns, windtraps, blood purifiers) and a refill that fails
  closed unless the base's map is fully stopped — a running map rewrites
  base state from memory on flush. (#112)
- **Player chat commands** (`!ping`, `!kit`): a second bounded queue on the
  game broker's `chat.intercept` exchange copies chat with zero
  interference; off by default, per-command opt-in, per-player cooldowns.
  DST v13.4 port. (#113)
- **Generator fuel**: per-device fuel levels (Oil / SpicedFuelCell / turbine
  lubricants, measured burn rates) and a transactional refill bounded by
  stack sizes and inventory slots, same map-down gate as water. (#114)
- **Base containers + permission roster (read)**: every stored item stack
  per base with delete, permission roster per base, and a hardened
  `item-delete` that distinguishes world inventories (map-down gate) from
  player-carried ones (offline gate). (#115)

## 2026-08-19 — Passwordless servers

- Clearing `DUNE_SERVER_PASSWORD` now yields a public, passwordless server
  instead of silently keeping the previous password. Requires re-importing
  the egg (new `nullable` rule on the variable). Fixes #107. (#108)

## 2026-08-13 → 2026-08-16 — Depot sync, panel hardening, HTTPS

- 2026-08-12 depot sync + the text-router failure behind #82. (#83)
- Admin panel screenshots in the README (#84); Grant-item durability preset
  clipping fix (#86).
- Stop one stalled connection wedging the whole admin panel — the
  single-thread `HTTPServer` issue #89. (#90)
- `panel restart` console command + notices when a non-critical service
  dies. (#91)
- Reverse-proxy correctness: trust `X-Forwarded-For` only from declared
  proxies (#93); stop `DUNE_ADMIN_UI_DOMAIN` implying TLS is in front (#94)
  and advertise the HTTPS domain instead of an unusable `http://IP:port`
  (#88).
- Scheduler fixes: failed auto-restart diagnosis without leaking the API
  key (#95), restart recorded/consumed at the right moment (#100, #101).
- Command audit persisted across restarts (SQLite under `server/state/`),
  read-only polling no longer evicts real actions. (#96)
- Panel-changed settings no longer revert on restart. (#97)
- Player Hard Cap exposed as an egg variable. (#98)
- Restart card with presets + quiet warning window. (#99)
- **HTTPS from the egg**: direct TLS termination, ACME DNS-01, three
  certificate backends. (#104, docs #105)

## 2026-06-13 — Importable eggs for both panels

- Egg JSON ships in two formats: Pelican `PLCN_v1` and Pterodactyl
  `PTDL_v2` (the previous `PLCN_v3` export was importable by neither).
  Closes #41. (#81)

## 2026-06-07 → 2026-06-09 — Admin UI redesign

- Information architecture: 22 tabs regrouped into 4 sections / 14
  workspaces (spec #62; phases #63–#68).
- Readability pass: friendly setting names, unified badges, live sync,
  grouped sietch settings. (#61)
- Global Live auto-refresh across tabs. (#69, #70)
- Design overhaul in 8 phases: desert theme, two-tier top bar, ⌘K command
  palette, toasts + audit, tweaks panel + theme switcher, custom steppers,
  unified player picker, SVG line icons. (#71–#78)
- Overview KPI de-dup, inventory containers, item tier/rarity +
  moderation color-coding. (#79, #80)

## 2026-06-05 → 2026-06-06 — Autoscaler + sietch parking

- Demand-based autoscaler for the on-demand maps: live-connection counting
  (never evicts hub visitors), travel-demand parsing, Deep-Desert-dimension
  load-proportional scaling, live pool view + Discord webhook alerts,
  fast-wake latency work (cold→warm in ~5 s). (#43–#44, #47–#49, #51–#55)
- Security: stop leaking the admin-ui password and DRY-RUN token to the
  console stream (#50); redact JSON-quoted secrets from the Logs tab (#42).
- Daemon configs moved to persistent `server/state/`, surviving reinstalls.
  (#45, #46)
- Sietch parking: park/unpark a Survival instance from the Instances tab,
  boot-skip filter, per-state badges, orphan `farm_state` auto-sweep.
  (#56–#60)

## 2026-06-03 → 2026-06-04 — Operations suite

- Unattended scheduler: auto-restart + auto-backup (#23); generic scheduled
  tasks — broadcasts + time-based instance scaling (#37).
- Live player map + inventory viewer. (#22)
- Player editor: faction tier, progression unlock/lock, current-state panel
  (solaris/XP/faction/journey), remove-faction. (#24, #26)
- Market bot: economy writer (NPC orders) + Market tab (#28), autonomous
  loop + d12 gamble-buy (#29).
- Spicefield economy controls (#27); Loot & Spice tab with curated loot
  controls (#36).
- Instance management: topology view, map spin-up/down/scale via mock-k8s,
  DeepDesert dimension spin down/up, player-chosen multi-Sietch instances,
  per-sietch heterogeneous config (PvP/PvE, names). (#31–#35)
- Logs tab + single-service restart (#30); tech unlock-all/lock-all with
  optional welcome broadcast (#38, #39); write routes no longer return 200
  on failure behind Cloudflare (#25); FLS reconcile crash that kept removed
  sietches in the in-game browser (#40).

## 2026-06-01 → 2026-06-02 — Admin panel foundations

- dune-admin ports (MIT, reimplemented against our stack): Database tab +
  player/character reads + PlayerGuard (#8); character writes — currency,
  rename, tags, char-XP, keystones, items, faction rep (#9); destructive
  writes — item-delete, reset-spec, account-delete (#10).
- Live server status grid (#13); settings/INI reconciliation with
  `GET/POST /api/settings` (#14); settings catalogue re-mapped to real
  cvars extracted from the server binary — 144 binary-verified knobs
  (#15–#17, #21).
- Welcome kits (#18); first SPA tabs — status, settings, welcome kits
  (#19); market pricing engine + read-only market view (#20).
- Boot fixes: deterministic `ExtractMapPartitions` (UE5 index-0 crash-loop,
  #12); build mock-k8s off `/tmp` to survive Wings' 100 MiB tmpfs (#11).

## 2026-05-31 — mock-k8s hardening

- SIGTERM handling, state persistence/restore, LIST bisection, security
  pass (#2); PID-reuse identity check + env override order (#6); ledger I/O
  safety, persist-generation guard, allowlist, deep omit (#4); self-healing
  reconcile loop + `/status` and `/metrics` endpoints (#7).

## 2026-05-26 → 2026-05-28 — Initial release

- Pelican egg + custom runtime image running Funcom's official self-host
  binaries on native Linux Docker — no Hyper-V, no K3s (commit `e374069`
  onward).
- Our own open-source **mock-k8s-go** replacing CubeCoders' closed binary:
  K8s API discovery, BattleGroup CR, ServerSetScale, UE5 spawn-on-demand.
  The egg runs entirely on MIT-licensed code.
- The eight boot-blockers found and fixed: SteamCMD 32-bit TLS, patchelf
  deps, volume ownership, `$STARTUP` eval, ServiceAccount mount under
  ReadonlyRootfs, `/mnt/server` symlink, stale initdb data, AMP anti-tamper.
- First admin commands over the game's RabbitMQ (broadcast, shutdown,
  kick…), console stdin listener, UE5-aware health check.
- Panel variables: server password + display name, then 22 tunable knobs
  via `apply-config.sh`.
- First real player connected and created a character on 2026-05-27.
