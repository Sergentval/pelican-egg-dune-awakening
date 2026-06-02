# Attribution

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

## Icehunter/dune-admin (MIT) — ported admin capabilities

Portions of the admin tooling are ported from
[Icehunter/dune-admin](https://github.com/Icehunter/dune-admin) (MIT). We
reimplement against our own stack (admin-publish.sh + admin-http.py + the
web SPA) rather than running dune-admin as a dependency.

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
