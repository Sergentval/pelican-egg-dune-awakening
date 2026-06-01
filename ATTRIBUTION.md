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
