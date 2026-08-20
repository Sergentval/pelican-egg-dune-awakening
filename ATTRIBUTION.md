# Attribution

## acme-tiny (MIT) — ACME client core

`scripts/admin_acme.py` derives its ACME plumbing from
[acme-tiny](https://github.com/diafygi/acme-tiny) by Daniel Roesler, MIT
licensed. Reused faithfully: JWS signing through the `openssl` CLI (the
runtime has no Python asymmetric-crypto library), nonce handling with
badNonce retry, and the order/finalize/download flow.

Replaced: the challenge half. acme-tiny implements HTTP-01, which
validates against port 80 — a Pelican server is allocated arbitrary high
ports, so that is a dead end for most deployments. `admin_acme` implements
DNS-01 instead, which needs no inbound port at all.

## CubeCoders Limited - AMP Dune Awakening template

This Pelican egg adapts work originally published by **CubeCoders Limited**
(<https://cubecoders.com/>) under their AMP server management product.

Original source (MIT-licensed):
<https://github.com/CubeCoders/AMPTemplates/tree/main/scripts> (Dune Awakening
launch scripts), commit observed during initial port: April 2024.

### What we owe them

CubeCoders worked out the non-obvious mechanics of running Funcom's Dune
Awakening dedicated server on Linux without Hyper-V or a real Kubernetes
cluster. Specifically, the reverse-engineering they published as MIT-licensed
code taught us:

- The seven Funcom OCI image tarballs that ship in the SteamCMD depot
  (postgres, mq, director, text-router, gateway, db-utils, server) and how to
  extract their layers without an OCI runtime.
- The musl-loader patchelf trick (the Funcom binaries link against an Alpine
  musl that doesn't exist on a stock Debian host).
- The boot sequence (`prestart` -> `pg` -> `migrate-db` -> `mq-admin` ->
  `mq-game` -> `text-router` -> `mock-k8s` -> `director` -> `gateway`) and the
  readiness gates between stages.
- The K8s ServiceAccount mount contract the Battlegroup Director expects, and
  the BattleGroup + ServerSetScale custom resources the Director talks to a
  mock K8s API server about.
- The Erlang/RabbitMQ + .NET text-router + Python gateway env var surface
  needed to glue Funcom's services together.

Without their MIT-licensed publication, building a self-host stack on a stock
Linux box would have required reverse-engineering Funcom's whole pipeline from
network captures. We are grateful that they chose to ship the recipe openly.

### What this repo does differently

- Ports the same architecture to **Pelican Wings** (no AMP runtime), which
  drops the customstart.sh root-hook, the AMP env-var sourcing, and the AMP
  metaconfig automap layer.
- Replaces CubeCoders' closed-source `mock-k8s-go` binary (anti-tamper checks
  refuse to run outside AMP) with an open-source Go re-implementation in
  `mock-k8s/`, MIT-licensed and built from source at install time.
- Adds a panel-driven `apply-config.sh` helper exposing 22 game-side knobs
  (loot multipliers, sandstorms, sandworms, PvP, building limits, on-demand
  pool tuning) under Pelican's variables UI - mirroring the surface AMP
  exposes via its metaconfig automap.
- Owns the install path: scripts are vendored in this repo and pulled at
  install time from here, not from the upstream AMPTemplates tarball. Lets us
  ship Pelican-specific fixes for Funcom DB migration regressions, partition
  ID handling, phased shutdown grace periods, etc. without an upstream race.

The CubeCoders copyright notice is retained in `LICENSE` and on every
individual script header where their original code is present in any
meaningful form. The MIT license they shipped under permits both modification
and re-distribution; this repo also ships under MIT.

If you found this useful, consider checking out [AMP](https://cubecoders.com/AMP)
for the proprietary version with a wider game catalogue and managed UX.

## adainrivers - dune-dedicated-server-manager

`scripts/admin-publish.sh` is a bash adaptation of the AMQP-based admin
protocol reverse-engineered and verified by adainrivers in
<https://github.com/adainrivers/dune-dedicated-server-manager> (MIT,
Rust + Tauri). All of the following come from their work:

- The envelope shape: base64-encoded
  `{Version:2, AuthToken, MessageContent}` published to the
  `heartbeats` exchange with routing key `notifications`, user_id
  `fls`, app_id `fls_backend`.
- The Erlang publish snippet executed via `rabbitmqctl eval` on the
  admin broker pod, byte-equivalent to their `mq.rs` so server-side
  log lines stay consistent across admin clients.
- The catalogue of 14 verified ServerCommand names
  (`AddItemToInventory`, `ServiceBroadcast`, `KickPlayer`, `AwardXP`,
  `SkillsSetModuleLevel`, `TeleportTo`, `SpawnVehicleAt`, `ServerExec`,
  and others).
- The "Funcom-confirmed harmless" built-in fallback AuthToken value.
- The negative results documented in their source comments (Journey
  commands silently no-op, XP Category field ignored,
  AwardXPByEventTag returns "unknown ServerCommand") — saved us
  reverse-engineering the same dead ends.
- The bundled catalogue JSON files in `data/admin/` — verbatim copies
  of `vehicles.json` (9 vehicle classes + templates), `items.json`
  (2558 items), and `skill-modules.json` (145 ability/attribute
  modules) from their `crates/dune-server-service/data/`. Used by
  `scripts/admin-lookup.py` to back the `admin vehicles | items |
  skills` panel subcommands.

If you want a polished desktop GUI for managing a Dune server (item
grants, vehicle spawns, player lookup, scheduled restarts), check out
their app directly. Our bash wrapper is intentionally minimal — meant
for ad-hoc admin from inside the Pelican container, not as a
replacement for their work.

The **unattended scheduler** (`scripts/admin_schedule.py` + `start-scheduler.sh`,
auto-restart + auto-backup with independent enable switches and a run ledger) is
our own in-container implementation, inspired by ddsm's host management service
which offers the same daily-restart / scheduled-backup feature set. No code was
lifted — only the feature concept.

## Icehunter/dune-admin (MIT) — ported admin capabilities

Portions of the admin tooling are ported from
[Icehunter/dune-admin](https://github.com/Icehunter/dune-admin) (MIT). We
reimplement against our own stack (admin-publish.sh + admin-http.py + the
web SPA) rather than running dune-admin as a dependency.

Base containers + permission roster (2026-08, C3.3) port, with thanks,
Red-Blink's bases container feature and listBasePermissions read model (MIT),
reimplemented as base-containers / base-permissions; the item-delete gate
split (world inventories → map-down, player inventories → offline) is ours.

Base permission writes (2026-08, C3.4) port, with thanks, Red-Blink's
setBasePermissions / transferBaseToSystemCustodian / permission-candidates
model (MIT): the shipped-procedure write path (never direct DML — the procs
notify the running map), the one-Owner + roster-cap + controller-id-only
invariants, the removals→ranks→Owner-last write order, the claim-actor row
lock, the unclaimed/picked-up refusals, and the reserved Server persona
tuple (account 9000002 / 900000201-3, kept identical to their Care Package
identity for cross-stack compatibility) with GM fallback and
create-on-first-use. Reimplemented as per-operation subcommands
(base-permission-set / -remove / base-transfer-custodian) instead of their
whole-roster PUT.

Generator fuel (2026-08, C3.2) ports, with thanks, Red-Blink's
baseGenerators / baseGeneratorFuelLevels / refillBaseGenerators (MIT): the
generator allowlist + accepted-fuel table with measured burn rates, the
per-device (not per-type) level model, and the refill's locking + top-up +
bounded-insert discipline — reimplemented as one plpgsql transaction behind
our shared fail-closed map-down gate instead of their pending-refill queue.

Player chat commands (2026-08) port, with thanks,
coastal-ms/DST-DuneServerTool v13.4's ChatCommands mechanism (Apache-2.0):
the chat.intercept copy-queue discovery (bounded declare + catch-all bind +
basic_get drain via rabbitmqctl eval) and its safety posture (off by
default, per-command opt-in, per-player cooldowns, bounded queue, drop on
disable), reimplemented as the chat-* subcommands + scripts/admin_chatcmd.py.

World reset (2026-08, C6) port, with thanks,
coastal-ms/DST-DuneServerTool's worldreset-2 / WorldRestart.ps1
(Apache-2.0): the reversible same-battlegroup restart shape — verified
logical backup before anything destructive, admission gates (confirmation
phrase, zero players online, fail-closed on every ambiguous read), the
durable recovery marker that survives a reboot, and the
preserve-don't-delete storage discipline. Reshaped for this
single-container stack: the "storage replacement" is an atomic datadir
set-aside consumed by a boot hook ordered before prestart (whose ordinary
first-boot path builds the fresh world), and rollback is the reverse swap
instead of a restore — their K8s StatefulSet/PVC dance and periodic
research-recovery audit do not apply here (research/entitlement recovery
deliberately not ported; per-character backups via our native
char-backup/char-restore cover the operator need).

Base backup wipe-guard (2026-08, C3.5) port, with thanks,
coastal-ms/DST-DuneServerTool v13.3.0's BaseBackupGuard.ps1 (Apache-2.0):
the discovery that base backups are live actor rows in state 'BaseBackup'
which the weekly Deep Desert reset deletes (the state is missing from
delete_actors_and_respawns_on_server's exclusion list), the one-predicate
fix, the anchored fail-closed insertion, the verify-by-re-read discipline,
and the re-apply posture against game updates replacing the Funcom-owned
function — reimplemented as scripts/admin_baseguard.py + the base-guard-*
subcommands, with a boot-time re-apply (after migrate-db) in place of
their periodic tick, since on this stack migrations only run at boot.

Bases + water management (2026-08) ports, with thanks, Red-Blink/
dune-awakening-selfhost-docker's bases feature (MIT: the listBases claim
model incl. the picked-up-base exclusion, the baseWater device resolution
with its guarded ContainerInventory lateral, the water-type capacity table,
and the jsonb_set refill write), reimplemented as the `bases` /
`base-water` / `base-water-refill` subcommands with an explicit
fail-closed map-down gate in place of their queue/flush system.

Connection doctor (2026-08) ports, with thanks, the check catalogue of
coastal-ms/DST-DuneServerTool's P34 connection doctor (Apache-2.0: advertised
vs real IP, per-map address drift, port-range misconfig) and
Red-Blink/dune-awakening-selfhost-docker's doctor.sh (MIT: heartbeat recency,
listener inventory, partition coherence), reimplemented as `admin doctor` +
`scripts/admin_doctor.py` with our own single-container IGW semantics.

Character backup/restore (2026-08) ports, with thanks, the v0.46.0
native-transfer flow (`cmd/dune-admin/db.go`: processCaptureCharacterBackup /
processRestoreCharacterBackup / cleanupOrphanActorsForAccount, and
`character_backups_store.go`), reimplemented as the `char-backup*` /
`char-restore` subcommands + `scripts/admin_charbackup.py`. Our flow
additionally tears the current character down BEFORE the import (their
post-import cleanup collides on self-restore) and adds the same-account
stale player_state sweep.

Phase 1 (Database tab) lifts, with thanks:
- the read-only SQL guard `is_read_only_sql()` in `scripts/admin-http.py`
  (from `cmd/dune-admin/handlers_database.go`), and
- the table-list / describe / sample / column-search / read-only-SQL
  queries, ported as the `db-*` subcommands in `scripts/admin-publish.sh`
  (from `handlers_database.go` + `db.go`).

Phase 2 (Players/Character reads + PlayerGuard) lifts, with thanks:
- the FLevelComponent character-XP read (`readLevelComponentSkillState`), the
  inventory durability read (`cmdFetchInventory`), and the player-tags read —
  ported as the `char-xp-read` / `inventory-list` / `tags-get` subcommands in
  `scripts/admin-publish.sh` (from `cmd/dune-admin/db.go`). We anchor them on
  our confirmed `encrypted_accounts`/`actors` resolution instead of
  dune-admin's `player_state` controller→pawn hop, and guard the
  Funcom-schema-dependent reads behind a `to_regclass` preflight.
- the PlayerGuard offline precondition (`checkPlayerOffline`), ported as the
  `player-offline` subcommand (our confirmed `encrypted_player_state` schema).
- the pure progression math in `scripts/admin_progression.py` — `xpToLevel`
  binary search, the `intelAtLevel` curve, and `keystoneSPBonus` /
  `grantAllKeystoneTargets` (from `db.go` + `keystones.go`).
- the static data tables in `data/admin/{skill-xp-per-level,keystones,
  factions}.json` — the 201-level cumulative-XP table + `maxCharXP`, the 205
  keystone definitions, and the 21 faction-tier thresholds. These are
  mechanically derived from `db.go`/`keystones.go` (each file records its
  `_source`), not hand-transcribed.

Phase 3 (Players/Character writes) lifts, with thanks:
- the give-currency flow (`cmdGiveCurrency`): the audited proc call
  `dune.adjust_player_virtual_currency_balance(controller_id, get_solaris_id(),
  delta)` + balance read-back, ported as the `give-currency` subcommand. We
  resolve the player-CONTROLLER actor (verified live as the currency key) via
  our `encrypted_accounts`/`actors` join and gate it on `assert_player_offline`.
- `cmdRenameCharacter` (`dune.set_character_name(account_id, name)`) → the
  `rename` subcommand, and `cmdUpdatePlayerTags`
  (`dune.update_player_tags(account_id, add[], remove[])`) → the `tags-update`
  subcommand. Both account-keyed, gated on `assert_player_offline`, with
  read-back. Live-verified reversibly (rename round-trip; tag add/remove).
- the award-char-xp flow (`cmdAwardCharXP`, `computeAwardCharXPOutcome`,
  `applyAwardCharXPFLevelUpdate`/`applyAwardCharXPIntelUpdate`): cap at
  `maxCharXP=344440`, re-derive level/skill-points (from the controller's
  purchased-keystone bonus) and intel, then `jsonb_set` FLevelComponent (pawn
  fgl entity) + TechKnowledgePlayerComponent intel (pawn actor). Ported as the
  `award-char-xp` subcommand; the pure math is `admin_progression.award_char_xp
  _outcome` and the argv-only compute lives in `scripts/admin-inventory.py`.
- the grant-all-keystones flow (`cmdGrantAllKeystones`,
  `insertAllPurchasedKeystones`, `grantAllKeystoneTargets`,
  `updateLevelComponentSkillPoints`): insert `generate_series(1,205)` into
  `purchased_specialization_keystones` (controller) and re-derive FLevel
  TotalSkillPoints/UnspentSkillPoints (level + 54 bonus) on the pawn. Ported as
  the `grant-keystones` subcommand; math is `admin_progression.grant_all_keystone
  _targets` via `admin-inventory.py`.
- the give-item flow (`runGiveItem`, `planGiveItemStacks`/`fillExistingStacks`,
  `ensureGiveItemSlotCapacity`/`ensureGiveItemVolumeCapacity`,
  `maxItemsByVolume`/`requiredStackCount`/`formatGiveItemResult`/
  `validateGiveItemInput`, `findGiveItemInventory`/`applyGiveItemChanges`, and
  the `handleGiveItem` RMQ-vs-DB routing): the pure stack/slot/volume planner is
  `scripts/admin_inventory_plan.py` (argv-only compute exposed via
  `admin-inventory.py give-item`), and the INSERT-into-`dune.items` transaction
  (top-up existing matching stacks largest-first, then new stacks at
  MAX(position_index)+1, `stats='{}'`) + the `inventory_type=0` backpack
  resolution are the `give-item` subcommand. We DROP dune-admin's
  item-definition JSON resolvers (`resolveStackMax`/`resolveItemVolume`) because
  our catalogue carries no stack_max/volume, and instead use dune-admin's
  secondary DB fallback (`MAX(stack_size)`/`MAX(volume_override)` over existing
  world items). Routing diverges deliberately: we are STRICTER — online+quality0
  delegates to the RMQ `give` (AddItemToInventory) path, but online+quality>0 is
  REFUSED (DB writes require offline) rather than DB-written into a live
  inventory. The planner's expected values are pinned by a Python port of
  dune-admin's `db_cmd_give_item_test.go` oracle in `scripts/test_give_item.py`.
- the set-faction-reputation flow (`applyFactionRepDelta`,
  `syncFactionComponent`/`buildFactionDataArray`/`writeFactionComponent`,
  `repToTier`/`factionTierName`/`factionDisplayName`/`factionRepCap`/
  `factionTierThresholds`): the pure tier math is `admin_progression.py`
  (`clamp_faction_rep`/`rep_to_tier`/`faction_tier_name`/`faction_display_name`/
  `faction_rep_outcome`, pinned by `scripts/test_faction_rep.py`), exposed via
  `admin-inventory.py faction-rep`. The `faction-rep` subcommand keys on the
  player-CONTROLLER actor, calls `dune.set_player_faction_reputation(controller,
  faction_id, rep)`, then rebuilds `FactionPlayerComponent.m_FactionDataArray`
  wholesale from the rep table (both Great Houses, Atreides then Harkonnen, each
  `{"Faction":{"Name":…},"timestamp":<epoch_float>,"ReputationAmount":<int>}`) via
  `jsonb_set(properties,'{FactionPlayerComponent,m_FactionDataArray}', …, true)` —
  the no-clobber guarantee comes from re-reading the rep table, not patching one
  array element. We mirror dune-admin's give-rep delta path (we do NOT call
  `change_player_faction`, so house alignment is untouched) and restrict to the
  two Great Houses (1=Atreides, 2=Harkonnen). HTTP: POST
  /api/players/<id>/faction-rep.
- the destructive writes (`cmdDeleteItem`, `cmdResetSpecializations` all-mode,
  `cmdDeleteAccount`): `item-delete <item_id>` → `dune.delete_item(id)`;
  `reset-spec <player>` → `dune.reset_specialization_tracks(controller)` +
  `dune.reset_specialization_keystones(controller)`; `account-delete <player>
  <confirm-fls> [reason]` → `dune.delete_account(fls, reason)`. We HARDEN beyond
  dune-admin, which applies no server-side guard on any of these: every one is
  offline-gated; `item-delete` additionally resolves the owning character and
  verifies the item exists first; `account-delete` requires a `<confirm-fls>`
  argument that exactly equals the resolved 16-hex FLS id. `reset-spec` mirrors
  dune-admin in NOT recomputing the pawn FLevel skill points (the game
  reconciles on next login). HTTP: POST /api/items/<id>/delete, POST
  /api/players/<id>/reset-spec, POST /api/players/<id>/account-delete.
- the market-bot pricing engine (`internal/marketbot/pricing.go`) ported to
  `scripts/admin_market.py`, with the **Coastal "sane pricing"** model from
  coastal-ms/DST-DuneServerTool's `0001-sane-pricing-100k-cap.patch` (Apache-2.0):
  per-quality grade multipliers `[1, 1.25, 1.55, 2, 2.6, 3.3]` and a hard 100k
  cap. Verified against dune-admin's `pricing_test.go` golden values in
  `scripts/test_admin_market.py`. The item catalogue `data/admin/item-data.json`
  (vendor prices + categories + tiers/rarities for ~1.6k items) is lifted
  verbatim from dune-admin's `item-data.json` (MIT, © 2026 Ryan Wilson). Phase 7a
  is pricing + read-only market view; the order-posting / d12-gamble-buy bot
  (`exchange.go`, `bot.go`) is deferred to 7b.

- **Live map + inventory containers (Phases 1-4):** the map tab's top-down images
  (`web/public/{hagga-basin,deepdesert,arrakeen,harko}.webp`) and the per-map
  world→pixel projection bounds (`web/src/MapTab.tsx`) are lifted from dune-admin's
  `web/public/` + `LiveMapTab.tsx` (MIT). The `map-markers` position query
  (`scripts/admin-publish.sh`) follows dune-admin's `cmdFetchMapMarkers`
  (`cmd/dune-admin/db.go`), and the inventory-container mapping (`inventory_type`
  → Backpack/Hotbar/Equipped/…) in `web/src/tabs.tsx` follows dune-admin's
  `repairGearInventoryTypes`. We hand-roll the pan/zoom (no Leaflet dependency)
  and keep teleport locations in our own `server/state/` JSON store.
