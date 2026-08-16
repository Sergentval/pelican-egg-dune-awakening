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

Broadcasts a countdown notice every `freq_secs` and triggers a server
shutdown of the given type after `lead_secs`. The `cancel` form aborts
a pending shutdown (no other args needed).

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
