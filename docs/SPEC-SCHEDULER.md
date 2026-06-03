# Spec: Unattended Scheduler — auto-restart + auto-backup

Status: PROPOSED (planning). Phased; ≤5 files/phase. OFF by default. Mirrors the
welcome-scanner daemon pattern. Inspired by ddsm's host service (auto-restart /
auto-backup with independent enable switches), ported into our in-container stack.

## Goals

1. **Scheduled auto-restart** — daily (or on a set schedule) graceful restart with a
   pre-restart in-game countdown broadcast. Turns the egg from "admin console" into a
   self-running server; also applies pending settings (which take effect on restart)
   and clears long-uptime memory creep.
2. **Scheduled auto-backup** — periodic `pg_dump` of the `dune` schema (the
   irreplaceable player/world state) to an operator-visible dir, with retention prune.
3. **Run history + manual triggers** — every scheduled/manual run recorded; "run now"
   buttons; a Tasks tab.

## Confirmed mechanism facts (live-verified on server 30)

- **Graceful stop is data-safe.** `console.sh` traps SIGTERM/SIGINT → phased
  `shutdown_all`; "UE5 saves character/world state to Postgres on exit" (console.sh
  L36). So a restart that goes through a clean stop loses no data.
- **DB:** `"$PG_BIN/psql" -h "$BASE/runtime/postgresql" -p 15432 -U dune -d dune`
  (the `dune_psql` wrapper, admin-publish.sh L191). `pg_dump` ships in the same
  extracted postgres17 dir (`…/db-utils/usr/libexec/postgresql17/pg_dump`).
- **Countdown broadcast exists:** `admin-publish.sh shutdown <Restart|Maintenance|Update|cancel> <lead_secs> <freq_secs>` → RMQ Shutdown command (warns players).
- **Restart power:** the egg's stop is `^C`; Wings does NOT auto-restart a *clean*
  stop, and the `papp_` application key cannot send power. The only clean in-container
  restart is the **Pelican client API**: `POST {PANEL_URL}/api/client/servers/{ID}/power {"signal":"restart"}` with a `ptlc_` client key. ⇒ auto-restart REQUIRES an
  operator-provided client key; auto-backup does NOT.

## Design decisions

- **Restart = broadcast then Pelican client-API power:restart.** At `warn_lead` before
  the slot, call `shutdown Restart <lead> <freq>` (player countdown). At T-0, `POST /power {signal:restart}`. Wings SIGTERMs → console.sh saves + stops → Wings starts →
  entrypoint reboots (re-applies settings). If the client-API vars are unset, the
  restart task is **disabled with a clear log** (never a half-working restart).
  (For our own dev server, restart can also be exercised host-side via `docker restart`;
  the shippable path is the client API.)
- **Backup = `pg_dump -Fc` of schema `dune`** via the existing connection →
  `/home/container/backups/dune-YYYYMMDD-HHMMSS.dump` (under the Pelican file root, so
  it's visible + downloadable in the panel / over SFTP). Verify size>0; prune to
  `retention` newest. Restore = `pg_restore`, **hard offline-gated** (refuse unless
  zero live UE5 procs) + explicit confirm token, **CLI-only** initially (blast radius).
- **Scheduler = in-container loop** (`start-scheduler.sh`) supervised by console.sh,
  launched from pelican-entrypoint after the welcome scanner. Reads
  `data/admin/schedule.json` (OFF default), records runs in a sqlite ledger in the
  state dir. No-ops cheaply while disabled (welcome-scanner pattern).
- **Config:** `data/admin/schedule.json` (shipped OFF, editable via panel file
  manager + API), independent `enabled` per task. Run ledger persists in
  `server/state/scheduler.db` (survives restarts).

## Phase 1 — DB backup foundation (no scheduler yet; ships value immediately)

Files (4):
- `scripts/admin-publish.sh` — `db-backup` (`pg_dump -Fc -n dune` → timestamped file
  in `$BASE/backups`, verify, echo path), `db-backup-list` (CSV: file, bytes, mtime),
  `db-restore <file> <confirm>` (offline-gated `pg_restore`; CLI-only).
- `scripts/admin_backup.py` (new) — pure, testable: backup filename builder + the
  retention selector (given filenames + keep-N → which to prune) + list parsing.
- `scripts/admin-http.py` — `GET /api/database/backups` (list), `POST /api/database/backup`
  (trigger, auth+csrf). Restore stays CLI-only.
- `scripts/test_admin_backup.py` (new) — retention/prune + filename tests.

Verify: pg_dump produces a restorable dump on the dev server; full test suite green.

## Phase 2 — scheduler daemon

Files (5):
- `data/admin/schedule.json` (new, OFF) — e.g.
  `{"restart":{"enabled":false,"time":"08:00","tz":"UTC","days":["mon".."sun"],"warn_lead_secs":600,"warn_freq_secs":120},"backup":{"enabled":false,"every_hours":24,"at":"04:00","retention":7}}`.
- `scripts/admin_schedule.py` (new) — pure due-logic: `due_tasks(config, now, last_runs)`
  decides which tasks fire (once per window, tz-aware), + the run ledger (sqlite,
  mirrors `admin_welcome.WelcomeLedger`).
- `scripts/start-scheduler.sh` (new) — supervised loop (mirrors
  start-welcome-scanner.sh): each tick load config → `due_tasks` → run backup
  (`admin-publish db-backup`) / restart (broadcast `shutdown` then Pelican power API)
  → record. Restart requires `DUNE_PELICAN_URL`/`DUNE_PELICAN_CLIENT_KEY`/`DUNE_PELICAN_SERVER_ID`
  panel vars; logs + skips if unset.
- `scripts/console.sh` + `scripts/pelican-entrypoint.sh` — launch the scheduler
  (one `launch_bg` line, after the welcome scanner).
- `scripts/test_admin_schedule.py` (new) — due-logic + dedupe + tz + disabled-skip.

Verify: with a near-future test slot, observe a backup run recorded; restart path
dry-runnable (broadcast + power-API call mockable). Restart requires the client key.

## Phase 3 — Tasks tab + API

Files (4):
- `scripts/admin-http.py` — `GET/POST /api/schedule` (read/update config),
  `GET /api/tasks/runs` (history), `POST /api/tasks/trigger/<backup|restart>` (run now).
- `web/src/api.ts` — wrappers + types.
- `web/src/tabs.tsx` (or a new `SchedulerTab.tsx`) — schedule editor (per-task enable +
  time/retention), run-history table, "run now" buttons. Surface the "restart needs a
  Pelican client key" requirement inline when unset.
- `web/src/App.tsx` — register the Tasks tab.

Verify: tsc + vite; manual trigger from the panel records a run.

## Phase 4 — live-verify + PR

- Backup: trigger from panel → confirm a dump file appears + is `pg_restore --list`-able.
- Restart: with a `ptlc_` key configured, schedule a near-future slot → confirm the
  countdown broadcasts in-game and the server restarts cleanly (state preserved). If no
  key, verify the task self-disables with a clear message.
- ATTRIBUTION (ddsm inspiration), memory + wiki backlog update, single PR.

## Risks / caveats

- **Auto-restart needs a `ptlc_` client key** (operator panel var). No clean keyless
  self-restart exists. Backup is independent + works without it.
- **Restore is destructive** — offline-gated + confirm-token + CLI-only at first.
- **Wings owns the lifecycle** — we restart *through* the panel API, not by self-exec.
- **`data/admin/schedule.json` resets on egg reinstall** (like welcome-kit.json); the
  run ledger in `state/` persists. Document it.
- **pg_dump version** — use the same extracted postgres17 `$PG_BIN` as the live server
  to avoid a version-mismatch dump.
- **Backup disk** — `pg_dump -Fc` of the dune schema is modest, but retention bounds it;
  prune enforces `retention`.

## Recommended order

Phase 1 (backup — independent, safe, immediately useful) → Phase 2 (scheduler) →
Phase 3 (Tasks UI) → Phase 4 (live-verify + PR). Backup first means we have a safety
net before anything that restarts the server.
