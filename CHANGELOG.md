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

## Unreleased

- Deep Desert per-partition PvP (`DUNE_PVP_PARTITIONS`) + Deep Desert picker
  routing (`DUNE_DD_PICKER_ROUTING`) + 5 reconnect/ping QoL settings — built
  on branch `feat/106-deep-desert-pvp` (PR #109), in-game labels confirmed by
  the reporter, final soak in progress before merge. (#106)

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
