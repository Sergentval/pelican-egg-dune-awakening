# Admin commands

This egg ships a server-side admin pipeline that bypasses the locked
in-game console entirely. Admin actions reach the running UE5 Sietches
by publishing an AMQP envelope to Funcom's `heartbeats` exchange; the
`UDuneServerCommandSubsystem` (a.k.a. *seabass*) consumes the envelope
and executes the command server-side.

The pipeline is fully working end-to-end on this stack (verified
2026-05-28: in-game banner rendered across all 5 Sietches on first try
after the `ServerCommandsAuthToken` cmdline override was wired in).

## Two invocation surfaces

### 1. Pelican panel console (recommended)

Type `admin <subcommand> [args]` into the **Console** tab of your
server's Pelican panel. The `console.sh` stdin listener inside the
container picks up the line, parses argv via `shlex`, and forwards it
to `scripts/admin-publish.sh`. Output is prefixed `[admin]` and shows
up in the same console.

```text
admin broadcast "Hello" "Server-side admin is live" 15
```

The same console also takes `panel <status|restart|stop>`, which acts on
the admin panel process itself rather than publishing to the game:

```text
panel status     # is admin-http running, and on which pid
panel restart    # stop it (SIGTERM, then SIGKILL after 3s) and start it again
panel stop       # stop it and leave it stopped
```

Use it if the panel becomes unreachable while the game is fine. The
restart re-reads the password and session secret from `server/state/`, so
**you stay logged in** — browser sessions survive it. It never touches the
game server: no player is disconnected and no world state is written.

There is deliberately no automatic restart. A service that is crashing for
a reason should surface that reason rather than be looped silently, and
nothing else in `console.sh` self-heals. What you get instead is a
one-line notice in the console when a non-critical service dies
(`admin-http`, `fls-stub`, `mock-k8s`), pointing at its log — the decision
to restart stays yours.

### 2. HTTP loopback (for tooling / mods)

`scripts/admin-http.py` runs in two modes:

**Internal mode** (default, `DUNE_ADMIN_UI_ENABLED=0`) — listens on
`127.0.0.1:8089`. JSON in, JSON out. Map 1:1 to subcommands:

```bash
docker exec <container> curl -sS -X POST http://127.0.0.1:8089/admin/broadcast \
  -H 'Content-Type: application/json' \
  -d '{"title":"Hello","body":"Hello from curl","duration":15}'
```

If you bind beyond loopback in internal mode you must also set
`DUNE_ADMIN_HTTP_AUTH=<secret>` — the server refuses to start
otherwise. Calls then need `Authorization: Bearer <secret>`.

**UI mode** (`DUNE_ADMIN_UI_ENABLED=1`) — exposes the React admin
panel + a session-authenticated API. Authentication on POST routes
is HttpOnly session cookie + CSRF double-submit:

```bash
# Login — stores session cookie + returns csrf token
curl -sS -c jar.txt -X POST http://panel.example/api/login \
  -H 'Content-Type: application/json' -d '{"password":"..."}'
# CSRF cookie is dune_csrf; copy the value into X-CSRF-Token
CSRF=$(grep dune_csrf jar.txt | awk '{print $NF}')
curl -sS -b jar.txt -H "X-CSRF-Token: $CSRF" \
  -X POST http://panel.example/admin/broadcast \
  -H 'Content-Type: application/json' \
  -d '{"title":"Hello","body":"Hi","duration":15}'
# Logout revokes the session jti and clears cookies
curl -sS -b jar.txt -H "X-CSRF-Token: $CSRF" \
  -X POST http://panel.example/api/logout
```

Bearer auth still works in UI mode (the JSON response from `/api/login`
includes `token` for legacy callers), but the browser SPA uses cookies
exclusively. Mutating cookie-auth requests without `X-CSRF-Token`
return 403. Logins are rate-limited to 5 attempts per 15 min per
source IP (`DUNE_ADMIN_UI_LOGIN_MAX_ATTEMPTS` /
`DUNE_ADMIN_UI_LOGIN_WINDOW_SECS` to tune); an attempt is charged
before the password is checked and refunded when it turns out to be
correct, so parallel guesses cannot overrun the quota.

#### Settings changed in the panel now stick

`apply-config.sh` rewrites every **env-backed** setting from its Pelican
variable on *every* boot. So changing one of those in the admin panel used
to be undone by the restart the panel itself asked for — silently, and
increasingly often now that the scheduler restarts unattended.

Of the 195 settings in the catalogue, **24 carry an `env`** and were
affected; the other 171 are panel-only and always survived. `PUT
/api/settings` now writes the egg variable first and only touches the INI
if the panel accepted it:

- Panel refuses the value → **nothing is written**, and the error says why
  (`"The value must be a number."` comes straight from the panel).
- Panel unreachable, or `DUNE_PELICAN_{URL,CLIENT_KEY,SERVER_ID}` unset →
  the change is **refused**, not applied-then-lost.
- `HTTP 400 — the environment variable does not exist` → the panel's egg
  predates that variable. Re-import the egg; a reinstall updates
  `scripts/` but not the egg definition.

The value sent to the panel is the same rendering that goes into the INI,
which is why the egg rules match it (`in:True,False` for booleans,
`numeric` for multipliers).

#### The command audit survives restarts

`GET /api/history` — the **Command audit** view in the Events tab — is
persisted to `server/state/admin-history.db`, not held in memory. That
matters because the panel restarts with the server: the scheduler's
unattended restart, `panel restart`, a reinstall. The trail you want
precisely when something went wrong used to be the first thing lost.

`server/state/` is also what a reinstall does not touch, so the audit
survives an egg update along with your world and database.

Two things shape what you see there:

- **Read-only traffic is not recorded.** The Live Map polls
  `/api/map/markers` every 4 seconds, and every poll used to land in the
  buffer — 200 slots of `map-markers` evicted every real action inside a
  quarter of an hour. Queries (`players`, `db-sample`, `db-sql`, status
  reads…) are skipped; anything that changes the world is kept, including
  subcommands added later, which are recorded unless deliberately
  classified as read-only.
- **Each boot is an entry.** A restart reads as `(admin panel started)`
  in the trail rather than as an unexplained gap in it.

Retention is the newest 500 entries, and each entry's stdout/stderr is
clipped so a database dump cannot bloat the file. Scheduler-driven
actions run `admin-publish.sh` directly rather than through the panel, so
they appear in the Scheduler tab's own run history instead.

#### Serving without TLS: `DUNE_ADMIN_UI_TLS`

The session cookies carry `Secure` when the panel believes TLS terminates
in front of it. A browser **discards a `Secure` cookie arriving over plain
HTTP**, and the SPA authenticates by that cookie alone — so if the panel
gets that judgement wrong, `/api/login` answers `200` and you land back on
the login screen with no error anywhere.

`DUNE_ADMIN_UI_DOMAIN` used to decide it, which conflates the hostname
browsers use (needed for CORS) with whether TLS is in front (needed for
this flag). Those coincide behind a reverse proxy and diverge without one:
point a DNS record straight at an exposed box, fill in the domain, and
there was **no working URL at all** — `http://` dropped the cookie and
nothing served `https://`.

| Value | Effect |
| --- | --- |
| `auto` (default) | A declared proxy's `X-Forwarded-Proto` decides. With none declared, falls back to the old rule: `Secure` when a domain is set and the bind is not loopback. |
| `on` | Always `Secure`. |
| `off` | Never `Secure` — the escape hatch for "I have a domain and no TLS". |

Explicit beats inferred: `on`/`off` override what a proxy reports. The
boot log states the effective mode, and a login that cannot possibly stick
is now called out by name:

```text
WARN login from 203.0.113.9 arrived over plain HTTP with no proxy in front, and
DUNE_ADMIN_UI_DOMAIN is set — the browser WILL DISCARD the session cookie and the
login will not stick. Serve the panel over https, or set DUNE_ADMIN_UI_TLS=off if
this panel is genuinely plain HTTP.
```

`off` means the session token crosses the network in the clear. That is
fine on a trusted LAN and not fine on anything the internet can reach.

#### Automatic HTTPS from the egg

The panel can obtain and renew its own Let's Encrypt certificate, for the
operator who points a DNS record straight at an exposed box and has nothing
in front to terminate TLS. Off unless `DUNE_ACME_DNS_BACKEND` is set.

It uses **DNS-01**, not by preference: HTTP-01 validates against port 80 of
your domain and a panel allocates arbitrary high ports, so it is a dead end
for most servers. TLS-ALPN-01 has the same problem on 443. DNS-01 proves
control by publishing a TXT record and needs no inbound port at all.

That TXT is published by a credential the container holds, which is the
whole security question. **Three shapes, and the choice is yours:**

| Backend | The container can | Costs |
| --- | --- | --- |
| `acme-dns`, own instance (`DUNE_ACME_DNS_URL`) | write **one TXT** in a throwaway zone, nothing else | one more service to run |
| `acme-dns`, public instance (default) | the same one TXT | a third party sees every validation, and could answer challenges for the delegated name |
| `cloudflare` | **rewrite every record in the zone** — Cloudflare tokens cannot be scoped to one record | nothing to set up |

The first two rest on CNAME delegation, which Let's Encrypt supports: you
create one record, once, and the container never holds a credential for
your real domain.

```text
_acme-challenge.dune-admin.example.com.  CNAME  <the validation zone>.
```

The boot log prints the exact CNAME to create the first time an acme-dns
backend registers. Set `DUNE_ACME_STAGING=1` while you get the delegation
right — staging certificates are not browser-trusted, but its rate limits
are far looser than production's, and a failed order costs an attempt.

Renewal runs in the scheduler, checked twice a day and renewed below 30
days remaining. The new certificate is written to `server/state/tls/` and
the panel picks it up **without restarting** — so set `DUNE_ACME_EMAIL`,
because otherwise a silently failed renewal is only noticed when the
certificate expires and the panel stops being reachable.

#### Behind a reverse proxy: declare it, or the rate limit is one bucket

**If you serve the panel through a reverse proxy, set
`DUNE_ADMIN_UI_TRUSTED_PROXIES`.** Otherwise every request arrives from
the proxy's address, all logins share a single bucket, and 5 failures
from anywhere on the internet lock *you* out for the window — the rate
limit stops protecting the account and becomes a denial of service on it.
The boot log tells you which mode you are in:

```text
proxies : none declared — bucketing on the socket peer (behind a reverse proxy that is ONE bucket for everyone; set DUNE_ADMIN_UI_TRUSTED_PROXIES)
proxies : trusting X-Forwarded-For from 10.99.0.1/32
```

Accepts IPs and CIDRs, comma or space separated (`10.99.0.1`,
`10.99.0.0/24, 172.20.0.0/16`). Set it to the address **admin-http sees
the proxy connecting from** — read it straight off a request line in
`logs/admin-http.log`, which now prints the peer:

```text
[admin-http] [INFO] 10.99.0.1 POST /api/login -> "POST /api/login HTTP/1.1" 200 -
```

Only declared peers are believed, and the client is resolved to the
rightmost `X-Forwarded-For` entry that isn't itself declared — a client
can prepend anything it likes to that header, so only hops you have
vouched for are allowed to vouch for what they appended. Leave it empty
(the default) and the header is ignored entirely.

#### Listener limits

UI mode binds `0.0.0.0`, so the socket is scanned from the open
internet within hours. Two knobs bound what that costs, and the
defaults suit any normal panel:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DUNE_ADMIN_HTTP_TIMEOUT_SECS` | `30` | Per-connection socket timeout. A peer that connects and never finishes its request line is dropped after this. |
| `DUNE_ADMIN_HTTP_MAX_CONNS` | `64` | Connections served at once. Beyond this, new connections are closed immediately rather than queued. |

Requests are served on threads, but every command that shells out to
`admin-publish.sh` or an `admin_*.py` helper is serialised behind a
single lock — those helpers read-modify-write shared JSON/INI state and
are not safe to overlap.

### 3. Direct shell (for ops / debugging)

```bash
docker exec <container> bash /home/container/scripts/admin-publish.sh broadcast \
  "Title" "Body" 15
```

All three paths converge on `admin-publish.sh`, which builds the
correct `BroadcastPayload` shape per command and publishes via
`rabbitmqctl eval` on the GAME broker (`rabbit-game@localhost`).

## Token

The seabass handler validates each envelope against a 32-hex token
embedded into UE5 at boot via the `-ini:engine:` cmdline override.
`prestart.sh` generates the token (`$STATE/svc-cmd-token`) once per
install; `lib.sh` exports it to every script; `start-ue5.sh` passes
it to UE5; `admin-publish.sh` embeds the same value in the AMQP
envelope so both sides agree.

If your config drifts and dispatch silently fails: regenerate by
deleting `runtime/state/svc-cmd-token` and restarting the container.

## Command reference

### Player targeting

Every per-player command takes a `<player_id>` argument. **In-game
character names ("Sergentval", "Atreides", etc.) cannot be used** —
Funcom stores them encrypted in
`dune.encrypted_player_state.encrypted_character_name` (BYTEA) and the
decryption key is not available to admin tooling.

Four accepted forms:

| Form | Example | Meaning |
|---|---|---|
| 16-char hex | `DE0BCCAA2501BF22` | FLS id, the canonical wire form |
| `me` | `me` | The single currently-online account (errors if 0 or >1) |
| `steam:<digits>` | `steam:76561198041278656` | Resolved via the unencrypted Steam platform id |
| `*` | `*` | All online players (where the handler supports it) |

### Lookup helpers

All read-only — no AMQP publish.

**Player lookups (query postgres):**

```text
admin players              # list every known account with FLS id + Steam id + online state
admin players online       # same, filtered to currently-connected
admin pos me               # current X/Y/Z + ready-to-paste teleport/vehicle commands
admin pos <fls_id>         # any player's last-known position
admin resolve me           # debug: what does 'me' resolve to right now?
admin resolve steam:76561198041278656
```

`admin pos` joins `dune.actors` (the `BP_DunePlayerCharacter` row) to
`encrypted_accounts.user` via `owner_account_id`. The actor's
`transform` postgres composite holds both position and rotation
quaternion — only the position vector is extracted.

**Catalogue lookups (read bundled `data/admin/*.json`, sourced from
adainrivers/dune-dedicated-server-manager MIT):**

```text
admin vehicles                  # 9 vehicle classes + their TemplateName options
admin items <search>            # 2558 items, case-insensitive id+name match
admin skills <search>           # 145 skill modules
admin items-json <ItemFName>    # raw JSON for one item
```

Searches cap at 40 (items) / 50 (skills) results to keep panel output
usable. Narrow the search term if you're hitting the cap.

Sample output of `admin players`:

```text
      fls_id      |     steam_id      | platform_name | life  | online |    last_avatar_activity
------------------+-------------------+---------------+-------+--------+-----------------------------
 DE0BCCAA2501BF22 | 76561198041278656 | Steam         | Alive | Online | 2026-05-28 07:22:05.861+00
```

Copy the `fls_id` value into per-player commands. Or use the `me` /
`steam:<id>` shortcuts to skip the copy step.

### `broadcast` — server-wide notification banner

```text
admin broadcast <title> <body> [duration_secs=30]
```

Renders a `Title` + `Body` banner for `duration_secs` seconds on every
client. Quote args with spaces. Duration is per-pulse, not total.

```text
admin broadcast "Maintenance" "Restart in 5 minutes" 20
admin broadcast "Welcome" "Have fun and don't trust the spice" 30
```

Wire shape: `BroadcastPayload.LocalizedText[]` array with one entry per
locale (we send `en` and `en-US`). Missing this field is fatal — the
dispatch logs but the renderer drops the banner.

### `shutdown` — scheduled server restart with countdown

```text
admin shutdown <Restart|Maintenance|Update|cancel> [lead_secs=600] [freq_secs=60]
```

Broadcasts a countdown notice to players and **nothing else** — it does
not stop, restart or shut down anything. Verified: a 90-second `Restart`
notice left the container up with every UE5 instance alive. The game
banners every `freq_secs` until `lead_secs` has elapsed, then the notice
simply expires. The `cancel` form withdraws it (no other args needed).

To actually restart the server, use the admin panel's **Scheduled
restart** card (Server → Shutdown & Restart) or the Scheduler tab: both
arm the scheduler's pending restart, which calls the panel's power API
when the countdown runs out. Note that the game re-shows the banner every
`freq_secs` for the whole countdown, so a long `lead_secs` with a short
`freq_secs` is hundreds of banners — the panel card arms the announcement
to open shortly before the restart instead of running the whole time.

```text
admin shutdown Restart 300 30      # restart in 5 min, ping every 30 s
admin shutdown Maintenance 1800    # maintenance in 30 min, default ping
admin shutdown cancel              # abort the countdown
```

### `kick` — disconnect a player

```text
admin kick <player_id>
```

```text
admin kick "*"                     # boot everyone
admin kick A1B2C3D4E5F60718
```

### `clean` — wipe a player's inventory

⚠️ **Destructive — there is no undo.**

```text
admin clean <player_id>
```

### `reset` — reset a player's XP and skills

⚠️ **Destructive — wipes XP, skill levels, unspent points.**

```text
admin reset <player_id>
```

### `water` — refill water containers

```text
admin water <player_id> [amount=1000000]
```

Refills jerrycans, stills, and other fillable water containers carried
by the target. Default `1 000 000` fills everything.

### `give` — grant an item

```text
admin give <player_id> <ItemFName> [qty=1] [durability=1.0]
```

`ItemFName` is the internal asset name (case-insensitive). The list of
valid names lives in `DT_ItemTemplates`; community datasets are at
<https://github.com/adainrivers/dune-dedicated-server-manager/tree/main/crates/dune-server-data>.

```text
admin give A1B2C3D4 AAR1_Spice 100
admin give A1B2C3D4 Weapon_KnifeAssassin 1 0.85
```

### `xp` — award generic player XP

```text
admin xp <player_id> <amount>
```

```text
admin xp A1B2C3D4 10000
```

The wrapper injects `Category: "Combat"` automatically — the seabass
handler silently no-ops without that field. The value is ignored; every
award lands as generic player XP regardless of category.

### `skill` — set a specific skill module's level

```text
admin skill <player_id> <Module> <Level>
```

`Module` uses the canonical UE5 ability id form: `Skills.Ability.<Name>`
or `Skills.Attribute.<Name>`. Run `admin skills <search>` to browse —
adainrivers' specs.rs helper hints at a `Swordmaster_T1`-style shorthand
that no longer works on shipping builds.

```text
admin skills swordmaster                        # list Swordmaster modules
admin skill me Skills.Ability.BattleCry 3       # max out Inspiration
admin skill me Skills.Attribute.Blade1 3        # max blade damage
```

### `points` — set unspent skill points

```text
admin points <player_id> <amount>
```

### `teleport` — move a player to exact XYZ

```text
admin teleport <player_id> <x> <y> <z> [yaw]
```

`TeleportToExact`. Drops the player at the exact coordinates with no
safety adjustment. Optional `yaw` rotates around the vertical axis.

```text
admin teleport A1B2C3D4 101000 285000 4300        # Survival_1 spawn-ish
admin teleport A1B2C3D4 101000 285000 4300 180    # facing south
```

### `tpsafe` — move a player to nearest safe XYZ

```text
admin tpsafe <player_id> <x> <y> <z> [yaw]
```

`TeleportTo` (safe variant). Snaps to the nearest navigable,
non-clipping, on-ground location near the requested coordinates. Use
when you don't know the exact terrain height.

### `vehicle` — spawn a vehicle next to a player

```text
admin vehicle <player_id> <ClassName> <x> <y> <z> <TemplateName> [rotation] [persistent=1.0]
```

`ClassName` and `TemplateName` are both row keys from
`DT_VehicleTemplates`. `Persistent=1.0` survives server restart; `0.0`
is transient. Optional `rotation` controls heading.

```text
admin vehicle A1B2C3D4 Sandbike 101000 285000 4300 T6_Combat
admin vehicle A1B2C3D4 Buggy 101000 285000 4300 T6_Combat 90 0.0
```

### Tier-graded items — see the catalogue

For copy-paste examples organised by faction and tier (Atre / Hark /
Smug weapon families, armor sets, consumables, augments, B1C4 unique
weapons), see [`ADMIN-TIER-ITEMS.md`](./ADMIN-TIER-ITEMS.md). The full
2 558-row item table lives in `data/admin/items.json`; the admin web
UI's Items tab exposes the same dataset with category filters.

### What's missing or only partially working — feedback for Funcom

See [`ADMIN-FUNCOM-GAPS.md`](./ADMIN-FUNCOM-GAPS.md) for a Funcom-
facing catalogue of admin actions the seabass handler doesn't expose
(no heal / hydrate / direct-Solari / despawn-vehicle / faction-rep
/ schematic-unlock, plus story and skill bulk-ops — 35 candidate
names tested, all rejected) and commands that publish OK but only
half-apply state (`AwardXP` `Category` quirk, `AddItemToInventory`
hardcoded `Quality=0`, `Journey*` family no-ops,
`CheatScript`/`ServerExec` accepted but never executed,
`AwardXPByEventTag` C++ function exists but no MQ dispatch entry).
Suitable for sharing as constructive feedback with the game team.

> The `raw` subcommand that accepted arbitrary `ServerCommand` JSON
> was removed in the Phase 1 / Phase 2 security pass. It accepted any
> JSON body and round-tripped it through a shell→python heredoc, which
> let attacker-controlled values become live Python code. For protocol
> debugging, `DUNE_ADMIN_DRY_RUN=1` on the per-subcommand scripts
> prints the assembled envelope without publishing — that covers the
> same diagnostic use case safely.

## Character backups (native transfer subsystem)

Full-character snapshots via the game's own server-to-server transfer procs
(`dune.character_transfer_export` / `character_transfer_import`, ~50-table
footprint). Player must be OFFLINE for both. Ported from
Icehunter/dune-admin v0.46.0 (MIT); see ATTRIBUTION.md.

```text
admin char-backup <player> [action] [reason]   # export -> backups/char/char-<fls>-<ts>.json (+ .meta.json)
admin char-backup-list [player]                # JSON list, newest first
admin char-backup-delete <file>                # delete one backup (data + sidecar)
admin char-restore <file>                      # FULL REPLACE of that FLS id's character
```

- The `.meta.json` sidecar records the `_patches_checksum` of the game patch
  the backup was taken on; `char-restore` refuses a mismatched patch BEFORE
  touching anything (take a fresh backup after each game update).
- `char-restore` tears the current character down first (the import proc's
  internal `delete_account` leaves natural-key rows behind that collide on
  re-import), then imports, then sweeps stale `player_state` rows.
- `account-delete` now takes a verified `pre-delete` char-backup first and
  aborts if it fails — the backup is the undo for a fat-fingered delete.
- Retention: newest `DUNE_CHAR_BACKUP_RETENTION` (default 10) per player.
- HTTP: `GET /api/players/<id>/char-backups`, `POST /api/players/<id>/char-backup`,
  `POST /api/char-backups/restore` (force-confirmed), `POST /api/char-backups/delete`.
  UI: Players → Character → "Character backups".

## Connection doctor

Read-only diagnosis of the "boots fine, nobody can join" family. Run it
whenever players report join timeouts or hung map travel:

```text
admin doctor    # JSON: 11 typed checks, ok/warn/error/skip
```

Checks: `DUNE_EXTERNAL_IP` set / public / matching the real WAN IP (ipify,
5s cap), per-map advertised game + IGW addresses in `farm_state` (loopback
IGW is legitimate on this stack — every instance shares one container),
per-map UDP port collisions (the in-game 2G2 error), advertised ports with
no actual UDP listener, alive-but-not-ready instances, registrations without
a `world_partition` row, and the freshest server-state heartbeat age from
the director log. Diagnose-only — it never fixes anything itself.

HTTP: `GET /api/doctor` (on demand — never polled; it performs the public-IP
lookup). UI: 🩺 card on the Overview page. Ported from DST's P34 connection
doctor (Apache-2.0) and Red-Blink's doctor.sh checks (MIT); see ATTRIBUTION.md.

## Known no-ops on seabass servers

Per [adainrivers' live-testing](https://github.com/adainrivers/dune-dedicated-server-manager)
(2026-05-26), the following commands publish successfully but the
seabass handler does not apply state. They're kept in the script for
protocol parity:

| Subcommand | ServerCommand | Notes |
|---|---|---|
| `exec` | `ServerExec` | Handler logs the call, no execution |
| `cheat` | `CheatScript` | Same — logs the script name, no state change |
| (n/a) | `AwardXPByEventTag` | Binary has the method but no MQ handler |
| (n/a) | `JourneySetCheckpoint` + family | Handlers fire but no DB / gameplay effect |

The `xp` subcommand auto-injects `Category` because that field's
*absence* (not its value) is the no-op trigger.

## Troubleshooting

### "Dispatch fired but nothing happened in-game"

Look at the per-Sietch UE5 logs immediately after the dispatch:

```bash
docker exec <container> bash -c '
  for f in /home/container/logs/ue5-*.log; do
    echo "=== $(basename $f) ==="
    grep -A 2 "Now running ServerCommand" "$f" | tail -10
  done
'
```

The marker line is `LogDuneServerCommands: Log: Now running
ServerCommand '<name>' with parameters '...'`. If you see it, the
seabass handler accepted the publish. If you also see `LogJson:
Warning: Field <name> was not found` or `LogJson: Error: Json Value
of type 'Null' used as a 'Array'` immediately after, the payload
shape is wrong — see the canonical shapes above.

If the marker is missing, either the publish never reached the
broker (`admin-publish.sh` would have reported `WARN no publish=ok`)
or the seabass handler dropped it for a wrong AuthToken — regenerate
the svc-cmd-token and restart.

### "publish=ok but nothing in any log"

Verify queue bindings on the game broker:

```bash
docker exec <container> bash -c '
  source /home/container/scripts/lib.sh /home/container
  export HOME="$BASE/runtime/mq-game-home"
  "$BASE/extracted/mq/opt/rabbitmq/sbin/rabbitmqctl" --node rabbit-game@localhost \
    list_bindings exchange_name routing_key destination_name
' | grep heartbeats
```

Expected: one binding per Sietch from `heartbeats` to
`queue.server.<sid>` with routing key `notifications`. Zero bindings
= a Sietch race; restart the container to repopulate.

### "Dispatch fires in some Sietches but not others"

Each Sietch declares its own consumer queue at boot. A Sietch that
crashed mid-init may have skipped the declare. `console.sh`'s
`UE5_DEAD_GRACE` health check exits the container after 90 s of zero
live UE5 processes, letting Wings recreate it cleanly.

## Attribution

Protocol shape, ServerCommand catalogue, no-op flags, and the harmless
fallback token all reverse-engineered + verified by
[adainrivers](https://github.com/adainrivers) in the
[dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager)
Rust+Tauri admin app (MIT-licensed). Our `admin-publish.sh` ports
their `crates/dune-server-service/src/admin/commands/build.rs` shape
to bash + python, and `admin-http.py` wraps it for tool integration.

The full wire-protocol reverse-engineering note lives in the wiki
under `dune-rmq-admin-protocol`.
