#!/bin/bash
# admin-publish.sh — invoke Funcom server-command admin via AMQP publish.
#
# Bypasses the locked in-game console entirely. Publishes a base64-encoded
# {Version,AuthToken,MessageContent} envelope to the `heartbeats` exchange
# (routing key `notifications`, user_id `fls`, app_id `fls_backend`) on the
# admin RabbitMQ broker via `rabbitmqctl eval`. Funcom's seabass server-
# command handler reads from that exchange and executes the inner command.
#
# Protocol reverse-engineered + verified by adainrivers in
#   https://github.com/adainrivers/dune-dedicated-server-manager
# (MIT-licensed). The base64 envelope shape, the Erlang publish snippet,
# the 14 ServerCommand catalogue, and the harmless built-in AuthToken
# value all come from their work. See ATTRIBUTION.md and
# ~/projects/llm-wikid/wiki/syntheses/dune-rmq-admin-protocol.md.
#
# This script is meant to be run from INSIDE the running Pelican container
# (which has the mq broker + rabbitmqctl + Erlang on PATH). Typical invocation:
#
#   docker exec <container> bash /home/container/scripts/admin-publish.sh broadcast \
#     "Server announcement" "Hello from admin"
#
# Subcommands.
#
# PlayerId argument forms (accepted everywhere a <player_id> is documented):
#   <16-hex>       FLS id, the canonical wire form (e.g. DE0BCCAA2501BF22)
#   me             single currently-online account (errors if 0 or >1 online)
#   name:<text>    character name (case-insensitive). Works when Funcom's
#                  User-data encryption is "As-is" (default for self-host).
#   steam:<digits> resolved from encrypted_accounts.platform_id
#   *              all online players (where the seabass handler supports it)
#
# Lookup helpers (no AMQP publish):
#   players [all|online]                         -- list accounts + FLS ids + Steam info
#   resolve <fls_id|me|steam:<id>|*>             -- debug what resolve_player_id() returns
#   pos <player_id>                              -- look up X/Y/Z (handy for vehicle/teleport)
#   vehicles                                     -- list 9 vehicle ClassName + TemplateName combos
#   items <search>                               -- search 2558 items by id/name (case-insensitive)
#   skills <search>                              -- search 145 skill modules
#   items-json <id>                              -- raw JSON for one item
#
# Postgres-direct subcommands (Funcom doesn't expose these as
# ServerCommands; the DB writes are the same ones the in-game UI does):
#   vehicle-list                                 -- list all spawned vehicle actors
#   vehicle-delete <actor_id>                    -- delete one vehicle (cascade-safe)
#
# Database-inspection subcommands (read-only; emit CSV for the admin UI's
# Database tab). SQL/queries ported from Icehunter/dune-admin
# cmd/dune-admin/handlers_database.go + db.go (MIT). See ATTRIBUTION.md.
#   db-tables                                    -- list dune.* tables + live row counts
#   db-describe <table>                          -- column name/type/nullable for one table
#   db-sample <table> [limit=20]                 -- first N rows of a table (max 500)
#   db-search <term>                             -- find tables/columns matching a term (ILIKE)
#   db-sql <select-query>                        -- run a read-only query (SELECT/EXPLAIN/SHOW/WITH)
#
# Player/character read subcommands (read-only; emit CSV for the admin UI's
# Players tab). Queries ported from Icehunter/dune-admin cmd/dune-admin
# {db.go,handlers_players.go,keystones.go} (MIT). See ATTRIBUTION.md. The
# online check + actor resolution use our confirmed encrypted_*/actors schema;
# char-xp/inventory/tags target the Funcom fgl/items/tags schema and are
# guarded by a to_regclass preflight that degrades to a clear message if the
# build doesn't expose those tables.
#   player-offline <player_id>                   -- PlayerGuard: exit 0 if offline, 1 if online, 2 if unknown
#   player-state <player_id>                     -- account/online/life-state + player-character actor + map
#   char-xp-read <player_id>                     -- FLevelComponent TotalXPEarned/TotalSkillPoints/spent SP
#   inventory-list <player_id>                   -- player-character inventory items + durability
#   tags-get <player_id>                         -- player progression tags
#
# Player/character WRITE subcommands (Phase 3; mutate persisted state via
# Funcom stored procs, gated on assert_player_offline). Ported from
# Icehunter/dune-admin cmd/dune-admin/db.go (MIT). See ATTRIBUTION.md.
#   give-currency <player_id> <amount>           -- adjust Solaris balance (signed; offline only)
#   rename <player_id> <new-name>                -- set character name (offline only)
#   tags-update <player_id> <add-csv> <remove-csv> -- add/remove progression tags (offline only)
#
# AMQP publish subcommands:
#   broadcast <title> <body> [duration_secs=30]              -- ServiceBroadcast (Generic)
#   shutdown <Restart|Maintenance|Update|cancel> [lead_secs=600] [freq_secs=60]
#                                                            -- ServiceBroadcast (ServerShutdown)
#   kick <player_id>                                         -- KickPlayer
#   clean <player_id>                                        -- CleanPlayerInventory (DESTRUCTIVE)
#   reset <player_id>                                        -- ResetProgression (DESTRUCTIVE: wipes XP+skills)
#   water <player_id> [amount=1_000_000]                     -- UpdateAllWaterFillables
#   give <player_id> <ItemFName> [qty=1] [durability=1.0]    -- AddItemToInventory
#   xp <player_id> <amount>                                  -- AwardXP (Category injected for handler)
#   skill <player_id> <Module> <Level>                       -- SkillsSetModuleLevel (Module e.g. Swordmaster_T1)
#   points <player_id> <amount>                              -- SkillsSetUnspentSkillPoints
#   teleport <player_id> <x> <y> <z> [yaw]                   -- TeleportToExact (exact XYZ, no safe snap)
#   tpsafe <player_id> <x> <y> <z> [yaw]                     -- TeleportTo (snaps to nearest safe location)
#   vehicle <player_id> <ClassName> <x> <y> <z> <TemplateName> [rotation] [persistent=1.0] [faction]
#                                                            -- SpawnVehicleAt
#   cheat <player_id> <ScriptName>                           -- CheatScript (NO-OP on seabass, kept for parity)
#   exec <exec_command>                                      -- ServerExec   (NO-OP on seabass, kept for parity)
#   raw '<inner-json>'                                       -- arbitrary ServerCommand JSON
#
# Known no-ops (adainrivers live-tested 2026-05-26): ServerExec, CheatScript,
# Journey*, AwardXP without Category, AwardXPByEventTag — all publish=ok
# but the seabass handler doesn't apply state. The xp subcommand auto-
# injects Category="Combat" to work around the known no-op.
#
# Env:
#   DUNE_ADMIN_TOKEN   override the auth token (defaults to the known-good
#                      Funcom-fallback "Nu6VmPWUMvdPMeB7qErr")
#   DUNE_ADMIN_NODE    override RMQ node (default rabbit-admin@localhost)
#   DUNE_ADMIN_DRY_RUN if =1, print the Erlang publish snippet and exit 0
#                      without running rabbitmqctl

set -euo pipefail

resolve_admin_token() {
    # Operator override always wins.
    if [ -n "${DUNE_ADMIN_TOKEN:-}" ]; then
        printf '%s' "$DUNE_ADMIN_TOKEN"
        return
    fi
    # prestart.sh writes the per-boot ServerCommandsAuthToken to this state
    # file BEFORE start-ue5.sh hands it to the UE5 instances via -ini:engine:
    # overrides. Using the same value here is what makes the seabass handler
    # accept the publish (it runs a token check that drops unrecognized
    # tokens silently — looking like 'publish ok but no dispatch').
    local state_token="${DUNE_BASE_DIR:-/home/container}/server/state/svc-cmd-token"
    if [ -f "$state_token" ]; then
        # Trim any trailing newline/CR.
        tr -d '\r\n' < "$state_token"
        return
    fi
    # adainrivers' Funcom-confirmed-harmless fallback. Works on Funcom-stock
    # VM images where the token gate isn't enforced, and during early boot
    # before our seabass handler initialises its token check. Otherwise
    # rejected silently.
    printf '%s' 'Nu6VmPWUMvdPMeB7qErr'
}
ADMIN_TOKEN="$(resolve_admin_token)"
# Default to rabbit-game@localhost: on our Pelican stack only the game
# broker has consumer queues bound to heartbeats:notifications (one per
# Sietch). adainrivers' original code defaults to rabbit-admin because
# their Funcom-installed setup federates both brokers; ours doesn't.
# Override with DUNE_ADMIN_NODE if you need the admin broker.
ADMIN_NODE="${DUNE_ADMIN_NODE:-rabbit-game@localhost}"

# --------------------------------------------------------------------------
# Locate the rabbitmq binaries inside the Funcom OCI extraction. These paths
# match start-mq-admin.sh's expectations on the same volume.
# --------------------------------------------------------------------------
BASE="${DUNE_BASE_DIR:-/home/container}"
MQ_ROOT="$BASE/extracted/mq"
ERL_ROOT="$MQ_ROOT/opt/erlang/lib/erlang"
RMQ_SBIN="$MQ_ROOT/opt/rabbitmq/sbin"

# rabbitmqctl check moved below — lookup subcommands (players, vehicles,
# items, skills, resolve) don't need it. The check fires just before the
# publish path actually invokes rabbitmqctl.

usage() {
    sed -n '1,70p' "$0" | sed -n 's/^# \?//p'
}

# --------------------------------------------------------------------------
# Postgres helper. Wraps Funcom's extracted psql with the right library
# paths so it can connect via the unix socket the gateway uses.
# Used by both `players` listing and `resolve_player_id` (steam: lookup).
# --------------------------------------------------------------------------
PG_BIN="$BASE/extracted/postgres/usr/local/bin"
PG_LIBS="$BASE/extracted/postgres/lib:$BASE/extracted/postgres/usr/lib:$BASE/extracted/postgres/usr/local/lib"
PG_ICU=""
if [ -d "$BASE/extracted/postgres/usr/local/share/icu" ]; then
    PG_ICU=$(find "$BASE/extracted/postgres/usr/local/share/icu" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1 || true)
fi
PG_PORT="${DUNE_PG_PORT:-15432}"
PG_SOCK="$BASE/runtime/postgresql"

dune_psql() {
    LD_LIBRARY_PATH="$PG_LIBS" ICU_DATA="$PG_ICU" \
        "$PG_BIN/psql" -h "$PG_SOCK" -p "$PG_PORT" -U dune -d dune "$@"
}

# Parameterised-query helper. CRITICAL: Funcom's extracted psql (17.4) does
# NOT interpolate :'var' bindings supplied with --set when the query comes
# from -c "..." — it sends the literal ":'var'" to the server, which fails
# with `syntax error at or near ":"`. Interpolation only happens when the
# query is read from stdin / a file (-f -). So every parameterised query
# (anything using :'var') MUST feed its SQL on stdin via this wrapper:
#
#     result=$(dune_psql_q --set=x="$x" -tA <<'SQL'
#     SELECT ... WHERE col = :'x'
#     SQL
#     )
#
# Raw queries with no :'var' (values shell-interpolated after validation, or
# no params at all) can still use dune_psql ... -c "..." directly.
dune_psql_q() { dune_psql "$@" -f -; }

# --------------------------------------------------------------------------
# Player-id resolver. PlayerIds on the wire MUST be the 16-hex FLS id;
# in-game character names cannot be looked up (Funcom stores them
# encrypted in encrypted_player_state.encrypted_character_name). This
# helper accepts the user-friendly forms:
#
#   <16-hex>      pass through unchanged
#   *             pass through (means "all online" to the handler)
#   steam:<digits> resolve Steam platform id -> FLS id via encrypted_accounts
#   me            resolve the single currently-online account (errors if 0 or >1)
#
# Anything else exits non-zero with a hint to run `admin players` to
# find the FLS id. Sentry checks keep us from sending obviously-wrong
# strings that the seabass handler would silently drop.
# --------------------------------------------------------------------------
resolve_player_id() {
    local raw="$1"
    case "$raw" in
        '')
            echo "[admin-publish] ERROR empty player id" >&2
            return 1
            ;;
        '*')
            printf '%s' '*'
            return 0
            ;;
        steam:*)
            local sid="${raw#steam:}"
            case "$sid" in *[!0-9]*|'') echo "[admin-publish] ERROR steam: requires numeric SteamID, got '$sid'" >&2; return 1 ;; esac
            # psql variable binding via --set + :'var' — properly escapes
            # SQL even though the regex above already restricts to digits.
            # Defence in depth.
            local resolved
            resolved=$(dune_psql_q --set=sid="$sid" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT "user" FROM dune.encrypted_accounts WHERE platform_id=:'sid' AND platform_name='Steam' LIMIT 1
SQL
)
            if [ -z "$resolved" ]; then
                echo "[admin-publish] ERROR no FLS account for Steam id $sid (run 'admin players' to list)" >&2
                return 1
            fi
            printf '%s' "$resolved"
            ;;
        name:*)
            # Character-name lookup. Works when Funcom's User-data encryption
            # is set to "As-is" (the default for self-host) — encrypted_
            # character_name is then just raw UTF-8 bytes. Case-insensitive
            # exact match; if it ever stops returning rows on your stack,
            # check the migrate-db log for "User-data encryption: ..." and
            # see if Funcom turned encryption on.
            local nm="${raw#name:}"
            if [ -z "$nm" ]; then echo "[admin-publish] ERROR name: requires a character name" >&2; return 1; fi
            # Length cap: Dune character names are <= 32 chars in the
            # client UI; anything larger is malformed.
            if [ "${#nm}" -gt 64 ]; then
                echo "[admin-publish] ERROR name: too long (${#nm} chars, max 64)" >&2
                return 1
            fi
            # psql variable binding (:'nm') escapes the value safely
            # regardless of content. Closes the shell-interpolation SQL
            # injection that existed when this query used '$nm'.
            local resolved
            resolved=$(dune_psql_q --set=nm="$nm" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT a."user"
FROM dune.encrypted_accounts a
JOIN dune.encrypted_player_state ps ON ps.account_id=a.id
WHERE lower(convert_from(ps.encrypted_character_name, 'UTF8')) = lower(:'nm')
LIMIT 1
SQL
)
            if [ -z "$resolved" ]; then
                echo "[admin-publish] ERROR no character named '$nm' (run 'admin players' for the live list)" >&2
                echo "                Note: only works when User-data encryption is 'As-is'; if Funcom" >&2
                echo "                turns encryption on a future patch, fall back to the FLS id." >&2
                return 1
            fi
            printf '%s' "$resolved"
            ;;
        me)
            local online_count online_id
            online_count=$(dune_psql -tAc "SELECT count(*) FROM dune.encrypted_accounts a JOIN dune.encrypted_player_state ps ON ps.account_id=a.id WHERE ps.online_status='Online'" 2>/dev/null | tr -d '\r\n')
            case "$online_count" in
                0|'') echo "[admin-publish] ERROR 'me' shortcut: no players online" >&2; return 1 ;;
                1)
                    online_id=$(dune_psql -tAc "SELECT a.\"user\" FROM dune.encrypted_accounts a JOIN dune.encrypted_player_state ps ON ps.account_id=a.id WHERE ps.online_status='Online' LIMIT 1" 2>/dev/null | tr -d '\r\n')
                    printf '%s' "$online_id"
                    ;;
                *) echo "[admin-publish] ERROR 'me' shortcut: $online_count players online — pass an explicit FLS id (run 'admin players')" >&2; return 1 ;;
            esac
            ;;
        *)
            # Accept anything that looks like a 16-hex FLS id. Reject the rest
            # with a helpful pointer to `admin players`.
            if [ "${#raw}" -eq 16 ] && [ -z "${raw//[0-9A-Fa-f]/}" ]; then
                printf '%s' "$raw" | tr 'a-f' 'A-F'
                return 0
            fi
            echo "[admin-publish] ERROR '$raw' is not a valid FLS id (16-hex). Funcom stores character" >&2
            echo "                names encrypted so 'Sergentval'-style names can't be resolved." >&2
            echo "                Run 'admin players' to list accounts, then re-issue with the FLS id," >&2
            echo "                or use 'me' / 'steam:<steamID>' shortcuts." >&2
            return 1
            ;;
    esac
}

# --------------------------------------------------------------------------
# Player-read helpers (used by the Players-tab read subcommands). All resolve
# against our CONFIRMED schema (encrypted_accounts + actors). FLS ids are
# bound via psql --set/:'var'; table names passed to dune_require_tables are
# developer-supplied constants, never user input.
# --------------------------------------------------------------------------

# FLS hex -> dune.encrypted_accounts.id (account id), or empty if none.
dune_account_id() {
    dune_psql_q --set=fls="$1" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT id FROM dune.encrypted_accounts WHERE "user" = :'fls' LIMIT 1
SQL
}

# FLS hex -> current player-character (pawn) actor id, or empty if the player
# has no BP_DunePlayerCharacter actor (offline / never spawned). Newest wins.
dune_pc_actor_id() {
    dune_psql_q --set=fls="$1" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT ac.id FROM dune.actors ac
JOIN dune.encrypted_accounts a ON a.id = ac.owner_account_id
WHERE a."user" = :'fls' AND ac.class LIKE '%BP_DunePlayerCharacter%'
ORDER BY ac.id DESC LIMIT 1
SQL
}

# FLS hex -> player-CONTROLLER actor id, or empty if none. The controller (not
# the pawn) is the key for currency/faction/journey state — e.g.
# dune.player_virtual_currency_balances.player_controller_id. Confirmed live:
# an account owns BP_DunePlayerController + DunePlayerState + BP_DunePlayer
# Character actors; writes that say "controller_id" mean this one.
dune_controller_actor_id() {
    dune_psql_q --set=fls="$1" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT ac.id FROM dune.actors ac
JOIN dune.encrypted_accounts a ON a.id = ac.owner_account_id
WHERE a."user" = :'fls' AND ac.class LIKE '%BP_DunePlayerController%'
ORDER BY ac.id DESC LIMIT 1
SQL
}

# FLS hex -> current online status text (Offline/Online/...), or empty if the
# account doesn't exist. A missing player_state row coalesces to Offline.
# Confirmed encrypted_* schema.
dune_online_status() {
    dune_psql_q --set=fls="$1" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT COALESCE(ps.online_status::text, 'Offline')
FROM dune.encrypted_accounts a
LEFT JOIN dune.encrypted_player_state ps ON ps.account_id = a.id
WHERE a."user" = :'fls'
ORDER BY ps.last_avatar_activity DESC NULLS LAST
LIMIT 1
SQL
}

# PlayerGuard precondition shared by every Phase-3 character write. Returns 0
# if offline (safe to edit), 1 if online (refuse, message on stderr), 2 if the
# account is unknown. Writes MUST call this before mutating.
assert_player_offline() {
    local fls="$1" st
    st=$(dune_online_status "$fls")
    if [ -z "$st" ]; then
        echo "[admin-publish] ERROR no account for $fls (run 'admin players')" >&2
        return 2
    fi
    if [ "$st" != "Offline" ]; then
        echo "[admin-publish] player is currently $st — log out first, then apply the edit" >&2
        return 1
    fi
    return 0
}

# Return 0 only if every named relation exists; else name the missing ones and
# return 1. Uses to_regclass so a dune.* table absent on this Funcom build
# degrades to a clear message instead of a raw 42P01 from the read query.
dune_require_tables() {
    local t missing=""
    for t in "$@"; do
        if [ "$(dune_psql -tAc "SELECT to_regclass('$t') IS NOT NULL" 2>/dev/null | tr -d '\r\n')" != "t" ]; then
            missing="$missing $t"
        fi
    done
    if [ -n "$missing" ]; then
        echo "[admin-publish] ERROR required table(s) not present on this server build:$missing" >&2
        echo "                This read targets the Funcom fgl/items/tags schema. Run 'admin" >&2
        echo "                db-tables' to confirm which tables your build exposes." >&2
        return 1
    fi
    return 0
}

cmd="${1:-}"
if [ -z "$cmd" ]; then
    usage
    exit 2
fi
shift

# --------------------------------------------------------------------------
# Short-circuit subcommands that don't go through the publish path.
#   admin players              — list all accounts with FLS id + Steam info
#   admin players online       — filter to currently-online
#   admin resolve <id>         — debug: print what resolve_player_id() returns
# --------------------------------------------------------------------------
case "$cmd" in
    players)
        sub="${1:-all}"
        case "$sub" in
            all)    where_clause="" ;;
            online) where_clause="WHERE ps.online_status='Online'" ;;
            *)
                echo "[admin-publish] ERROR usage: players [all|online]" >&2
                exit 2
                ;;
        esac
        # The encrypted_character_name column is plain UTF-8 bytes when
        # Funcom's User-data encryption is set to "As-is" (default for
        # self-host). convert_from('UTF8') decodes it; on a stack where
        # encryption is enabled this returns gibberish and the UI just
        # falls back to the FLS id.
        dune_psql -c "
            SELECT a.\"user\"           AS fls_id,
                   COALESCE(convert_from(ps.encrypted_character_name, 'UTF8'), '-') AS character,
                   a.platform_id        AS steam_id,
                   a.platform_name,
                   COALESCE(ps.life_state::text, '-')      AS life,
                   COALESCE(ps.online_status::text, '-')   AS online,
                   ps.last_avatar_activity
            FROM dune.encrypted_accounts a
            LEFT JOIN dune.encrypted_player_state ps ON ps.account_id = a.id
            $where_clause
            ORDER BY ps.last_avatar_activity DESC NULLS LAST
            LIMIT 100
        "
        exit 0
        ;;
    resolve)
        raw="${1:?usage: resolve <fls_id|me|steam:<id>|*>}"
        if resolved=$(resolve_player_id "$raw"); then
            printf '%s -> %s\n' "$raw" "$resolved"
            exit 0
        else
            exit 1
        fi
        ;;
    pos|where)
        # Look up a player's current XYZ position by joining their
        # BP_DunePlayerCharacter actor row to encrypted_accounts via
        # owner_account_id. Transform is a postgres composite holding
        # ("(x,y,z)","(qx,qy,qz,qw)") — we extract the position vector.
        # Outputs three blocks: 1) parsed numbers, 2) ready-to-paste
        # admin commands you can copy directly, 3) raw transform text.
        raw="${1:?usage: pos <fls_id|me|steam:<id>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR pos: cannot look up '*' — pass a single player" >&2
            exit 2
        fi
        # Parameterised via psql --set + :'fls' to keep the resolver
        # contract (16-hex or DB-fetched account.user) honest even if
        # a future account row gets seeded with weird characters.
        row=$(dune_psql_q --set=fls="$fls_id" -tAF $'\t' 2>/dev/null <<'SQL'
SELECT ac.map, ac.partition_id, ac.transform::text
FROM dune.actors ac
JOIN dune.encrypted_accounts a ON a.id = ac.owner_account_id
WHERE a."user" = :'fls'
  AND ac.class LIKE '%BP_DunePlayerCharacter%'
ORDER BY ac.id DESC
LIMIT 1
SQL
)
        if [ -z "$row" ]; then
            echo "[admin-publish] ERROR no BP_DunePlayerCharacter row for $fls_id" >&2
            echo "                player may be offline or in a Sietch we haven't queried." >&2
            exit 1
        fi
        # row is "map<TAB>partition_id<TAB>("(x,y,z)","(qx,qy,qz,qw)")"
        # Pass $fls_id, $raw, and $row via env vars — NEVER interpolate
        # them into the Python source. The previous form let a player-id
        # like `'''+__import__('os').system(...)+'''` become live Python
        # code at shell-expansion time.
        FLS_ID="$fls_id" RAW_INPUT="$raw" ROW_DATA="$row" python3 - <<'PYEOF'
import os, re, sys
fls_id = os.environ.get("FLS_ID", "")
raw_input = os.environ.get("RAW_INPUT", "")
row = os.environ.get("ROW_DATA", "").strip()
parts = row.split("\t")
if len(parts) < 3:
    sys.stderr.write(f"unexpected row: {row}\n"); sys.exit(1)
mapname, partition, tform = parts[0], parts[1], "\t".join(parts[2:])
m = re.search(r"\((-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)\)", tform)
if not m:
    sys.stderr.write(f"could not parse transform: {tform}\n"); sys.exit(1)
x, y, z = (float(g) for g in m.groups())
print(f"FLS:        {fls_id}")
print(f"Map:        {mapname}  (partition {partition})")
print(f"Position:   X={x:.2f}  Y={y:.2f}  Z={z:.2f}")
print()
print("Ready-to-paste commands:")
print(f"  admin teleport {raw_input} {x:.0f} {y:.0f} {z:.0f}")
print(f"  admin vehicle  {raw_input} Sandbike {x:.0f} {y:.0f} {z:.0f} T3_Boost")
print(f"  admin vehicle  {raw_input} OrnithopterLight {x:.0f} {y:.0f} {int(z+200)} T6_Combat")
print()
print(f"Raw transform: {tform}")
PYEOF
        exit 0
        ;;
    vehicles|items|skills|items-json)
        # Catalogue lookups — read-only, hit the bundled data/admin/*.json
        # files. Saves humans from grepping 293KB of items.json by hand.
        # Data files are MIT-licensed copies from
        # adainrivers/dune-dedicated-server-manager — see ATTRIBUTION.md.
        DUNE_BASE_DIR="$BASE" exec python3 "$BASE/scripts/admin-lookup.py" "$cmd" "$@"
        ;;
    vehicle-list)
        # List every spawned vehicle in dune.actors. Funcom doesn't ship
        # a DespawnVehicle ServerCommand (35 candidates probed, all
        # rejected by seabass), so cleanup has to go through postgres.
        # We filter to the 9 known vehicle blueprints + the unique-named
        # actor_class entries from data/admin/vehicles.json so we never
        # show NPCs / buildings / items here.
        dune_psql -c "
            SELECT id, class, map, partition_id, transform::text AS transform
            FROM dune.actors
            WHERE class ILIKE '%BP_Sandbike%'
               OR class ILIKE '%BP_Buggy%'
               OR class ILIKE '%BP_Tank%'
               OR class ILIKE '%BP_SandCrawler%'
               OR class ILIKE '%BP_LightOrnithopter%'
               OR class ILIKE '%BP_MediumOrnithopter%'
               OR class ILIKE '%BP_TransportOrnithopter%'
               OR class ILIKE '%BP_TreadWheel%'
               OR class ILIKE '%BP_ContainerVehicle%'
            ORDER BY id
        "
        exit 0
        ;;
    vehicle-delete)
        # Hard-delete a vehicle actor row by id. Cascades clean every FK
        # we audited (actor_state, inventories, base_backup_linked_actors,
        # …); overmap_players.vehicle_id is SET NULL so the player who
        # last drove the vehicle keeps their overmap row.
        #
        # Safety: only proceed if the row's class string matches one of
        # the known vehicle blueprint prefixes, so a typo of the id
        # can't accidentally vaporise a player character or a building.
        actor_id="${1:?usage: vehicle-delete <actor_id>}"
        case "$actor_id" in
            ''|*[!0-9]*) echo "[admin-publish] ERROR vehicle-delete: actor_id must be a positive integer, got '$actor_id'" >&2; exit 2 ;;
        esac
        # Inspect first.
        row=$(dune_psql -tAc "SELECT class FROM dune.actors WHERE id = $actor_id" 2>/dev/null | tr -d '\r\n')
        if [ -z "$row" ]; then
            echo "[admin-publish] ERROR no actor with id $actor_id" >&2
            exit 1
        fi
        case "$row" in
            *BP_Sandbike*|*BP_Buggy*|*BP_Tank*|*BP_SandCrawler*|*BP_LightOrnithopter*|*BP_MediumOrnithopter*|*BP_TransportOrnithopter*|*BP_TreadWheel*|*BP_ContainerVehicle*)
                ;;
            *)
                echo "[admin-publish] ERROR refusing to delete non-vehicle actor: $row" >&2
                exit 1
                ;;
        esac
        if dune_psql -c "DELETE FROM dune.actors WHERE id = $actor_id" >/dev/null 2>&1; then
            echo "[admin-publish] OK vehicle-delete actor_id=$actor_id class=$(printf '%s' "$row" | sed 's|.*/||')"
            echo "publish=db-delete actor_id=$actor_id"
        else
            echo "[admin-publish] ERROR vehicle-delete: postgres DELETE failed" >&2
            exit 1
        fi
        exit 0
        ;;

    # ----------------------------------------------------------------------
    # Database-inspection subcommands. Read-only; emit CSV (header row +
    # data rows) so admin-http.py can parse into {headers, rows, truncated}
    # for the Database tab. Ported from Icehunter/dune-admin
    # handlers_database.go + db.go (MIT) — see ATTRIBUTION.md. Table/term
    # values are bound via psql --set/:'var' (or validated to an identifier
    # allowlist) so the queries stay injection-safe.
    # ----------------------------------------------------------------------
    db-tables)
        # pg_stat_user_tables scoped to the dune schema; n_live_tup is the
        # planner's live-row estimate (cheap, no full COUNT scan).
        dune_psql --csv -c "
            SELECT relname AS table, COALESCE(n_live_tup, 0) AS rows
            FROM pg_stat_user_tables
            WHERE schemaname = 'dune'
            ORDER BY relname
        "
        exit 0
        ;;
    db-describe)
        tbl="${1:?usage: db-describe <table>}"
        case "$tbl" in
            *[!A-Za-z0-9_]*|'')
                echo "[admin-publish] ERROR db-describe: invalid table name '$tbl'" >&2
                exit 2
                ;;
        esac
        dune_psql_q --csv --set=tbl="$tbl" <<'SQL'
SELECT column_name AS column,
       data_type   AS type,
       CASE is_nullable WHEN 'YES' THEN 'null' ELSE 'not null' END AS nullable
FROM information_schema.columns
WHERE table_schema = 'dune' AND table_name = :'tbl'
ORDER BY ordinal_position
SQL
        exit 0
        ;;
    db-sample)
        tbl="${1:?usage: db-sample <table> [limit]}"
        case "$tbl" in
            *[!A-Za-z0-9_]*|'')
                echo "[admin-publish] ERROR db-sample: invalid table name '$tbl'" >&2
                exit 2
                ;;
        esac
        lim="${2:-20}"
        case "$lim" in *[!0-9]*|'') lim=20 ;; esac
        [ "$lim" -gt 500 ] && lim=500
        [ "$lim" -lt 1 ] && lim=1
        # $tbl is validated to ^[A-Za-z0-9_]+$ above, so it cannot break out
        # of the double-quoted identifier — safe to interpolate.
        dune_psql --csv -c "SELECT * FROM dune.\"$tbl\" LIMIT $lim"
        exit 0
        ;;
    db-search)
        term="${1:?usage: db-search <term>}"
        if [ "${#term}" -gt 64 ]; then
            echo "[admin-publish] ERROR db-search: term too long (${#term} chars, max 64)" >&2
            exit 2
        fi
        dune_psql_q --csv --set=pat="%$term%" <<'SQL'
SELECT table_name  AS table,
       column_name AS column,
       data_type   AS type
FROM information_schema.columns
WHERE table_schema = 'dune'
  AND (column_name ILIKE :'pat' OR table_name ILIKE :'pat')
ORDER BY table_name, column_name
LIMIT 500
SQL
        exit 0
        ;;
    db-sql)
        sql="${1:?usage: db-sql <select-query>}"
        # Defence in depth: admin-http.py already applies is_read_only_sql()
        # before calling us, but we ALSO pin the psql session read-only so a
        # direct CLI call (or any guard bypass) still cannot mutate — a write
        # then fails with "cannot execute X in a read-only transaction". The
        # SET applies to the subsequent -c in the same connection. -q
        # suppresses the SET command tag so only the query's CSV is emitted.
        dune_psql -q --csv \
            -c "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" \
            -c "$sql"
        exit 0
        ;;

    # ----------------------------------------------------------------------
    # Player/character read subcommands. See the header block for provenance.
    # ----------------------------------------------------------------------
    player-offline)
        # PlayerGuard primitive: the precondition every Phase-3 character
        # write must pass first. Exit 0 = offline (safe to edit), 1 = online
        # (refuse), 2 = unknown player / usage. Built on the confirmed
        # encrypted_* schema. A missing player_state row (LEFT JOIN -> NULL)
        # coalesces to Offline; a missing account returns no row at all.
        raw="${1:?usage: player-offline <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR player-offline: needs a single player, not '*'" >&2
            exit 2
        fi
        # assert_player_offline does the work (shared with the Phase-3 writes);
        # it prints the refusal/unknown message on stderr and returns 0/1/2.
        if assert_player_offline "$fls_id"; then
            echo "offline=true fls=$fls_id"
            exit 0
        else
            rc=$?
            [ "$rc" -eq 1 ] && echo "online=true fls=$fls_id"
            exit "$rc"
        fi
        ;;
    player-state)
        # Single-player detail on the confirmed schema: account, online/life
        # state, last activity, and the current player-character actor + map.
        raw="${1:?usage: player-state <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR player-state: needs a single player, not '*'" >&2
            exit 2
        fi
        # Distinguish a non-existent account (exit 2 -> HTTP 404) from a found
        # account that is merely offline / has no character. Without this a
        # well-formed-but-unknown FLS id would return an ambiguous empty 200.
        # Mirrors tags-get. The main query below is then guaranteed one row.
        if [ -z "$(dune_account_id "$fls_id")" ]; then
            echo "[admin-publish] ERROR player-state: no account for $fls_id (run 'admin players')" >&2
            exit 2
        fi
        dune_psql_q --csv --set=fls="$fls_id" <<'SQL'
SELECT a."user"                            AS fls_id,
       a.id                                AS account_id,
       a.platform_id                       AS platform_id,
       a.platform_name                     AS platform_name,
       COALESCE(ps.online_status::text, 'Offline') AS online,
       COALESCE(ps.life_state::text, '-')  AS life,
       ps.last_avatar_activity             AS last_activity,
       pc.pc_actor_id                      AS pc_actor_id,
       pc.map                              AS map
FROM dune.encrypted_accounts a
LEFT JOIN dune.encrypted_player_state ps ON ps.account_id = a.id
LEFT JOIN LATERAL (
    SELECT ac.id AS pc_actor_id, ac.map
    FROM dune.actors ac
    WHERE ac.owner_account_id = a.id
      AND ac.class LIKE '%BP_DunePlayerCharacter%'
    ORDER BY ac.id DESC LIMIT 1
) pc ON true
WHERE a."user" = :'fls'
LIMIT 1
SQL
        exit 0
        ;;
    char-xp-read)
        # FLevelComponent read (TotalXPEarned / TotalSkillPoints / non-starter
        # SkillPointsSpent), ported verbatim from dune-admin readLevelComponent
        # SkillState — but anchored on OUR confirmed actor resolution rather
        # than its player_state controller->pawn hop. The level/intel are
        # derived in admin_progression.py, not in SQL. A quoted heredoc avoids
        # shell-escaping the embedded JSONB format('(TagName="%s")', ...) term.
        raw="${1:?usage: char-xp-read <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR char-xp-read: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.fgl_entities dune.actor_fgl_entities dune.actors || exit 3
        dune_psql --csv --set=fls="$fls_id" -f - <<'SQL'
SELECT
    COALESCE((fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint, 0)    AS total_xp,
    COALESCE((fe.components->'FLevelComponent'->1->>'TotalSkillPoints')::bigint, 0) AS total_skill_points,
    COALESCE((
        SELECT SUM((v->>'SkillPointsSpent')::int)
        FROM jsonb_each(fe.components->'FLevelComponent'->1->'ModuleData') AS kv(k, v)
        WHERE k != format('(TagName="%s")',
            fe.components->'FLevelComponent'->1->'StarterSkillTreeTag'->>'TagName')
    ), 0) AS spent_skill_points
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
WHERE afe.slot_name = 'DuneCharacter'
  AND afe.actor_id = (
    SELECT ac.id FROM dune.actors ac
    JOIN dune.encrypted_accounts a ON a.id = ac.owner_account_id
    WHERE a."user" = :'fls' AND ac.class LIKE '%BP_DunePlayerCharacter%'
    ORDER BY ac.id DESC LIMIT 1
  )
SQL
        exit 0
        ;;
    inventory-list)
        # Player-character inventory with durability extracted from the
        # FItemStackAndDurabilityStats JSONB — ported verbatim from dune-admin
        # cmdFetchInventory, keyed on our resolved pawn actor id.
        raw="${1:?usage: inventory-list <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR inventory-list: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.items dune.inventories dune.actors || exit 3
        pc_actor=$(dune_pc_actor_id "$fls_id")
        if [ -z "$pc_actor" ]; then
            echo "[admin-publish] ERROR inventory-list: no player-character actor for $fls_id (offline / no character)" >&2
            exit 1
        fi
        # pc_actor is a DB-sourced integer id; bind it as an integer.
        dune_psql_q --csv --set=actor="$pc_actor" <<'SQL'
SELECT i.id           AS item_id,
       i.template_id  AS template_id,
       i.stack_size   AS stack_size,
       i.quality_level AS quality,
       COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability'), 'N/A') AS durability,
       COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability'), 'N/A')     AS max_durability,
       i.position_index AS slot
FROM dune.items i
JOIN dune.inventories inv ON i.inventory_id = inv.id
WHERE inv.actor_id = :'actor'::bigint
ORDER BY i.template_id
SQL
        exit 0
        ;;
    tags-get)
        # Player progression tags, ported verbatim from dune-admin. Keyed on
        # the confirmed account id.
        raw="${1:?usage: tags-get <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR tags-get: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.player_tags || exit 3
        aid=$(dune_account_id "$fls_id")
        if [ -z "$aid" ]; then
            echo "[admin-publish] ERROR tags-get: no account for $fls_id (run 'admin players')" >&2
            exit 2
        fi
        dune_psql_q --csv --set=aid="$aid" <<'SQL'
SELECT tag FROM dune.player_tags WHERE account_id = :'aid'::bigint ORDER BY tag
SQL
        exit 0
        ;;

    # ----------------------------------------------------------------------
    # Player/character WRITE subcommands (Phase 3). Each mutates persisted
    # player state and MUST pass assert_player_offline first. Ported from
    # Icehunter/dune-admin cmd/dune-admin/db.go (MIT) — see ATTRIBUTION.md.
    # ----------------------------------------------------------------------
    give-currency)
        # Adjust a player's Solaris balance via the audited stored proc
        # dune.adjust_player_virtual_currency_balance(controller_id, currency_id,
        # delta), keyed on the player-CONTROLLER actor (not the pawn), then read
        # the new balance back. amount may be negative to deduct; the proc
        # enforces non-negative balances. Ported from dune-admin cmdGiveCurrency.
        raw="${1:?usage: give-currency <fls_id|me|steam:<id>|name:<n>> <amount>}"
        amount="${2:?usage: give-currency <player> <amount>}"
        # Signed-integer validation: strip one optional leading '-', rest digits.
        amt_digits="${amount#-}"
        case "$amt_digits" in
            ''|*[!0-9]*)
                echo "[admin-publish] ERROR give-currency: amount must be an integer, got '$amount'" >&2
                exit 2
                ;;
        esac
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR give-currency: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.player_virtual_currency_balances dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR give-currency: no player-controller actor for $fls_id" >&2
            exit 1
        fi
        # Proc call + read-back in one stdin batch so :'var' interpolates.
        # Both statements print under -tA (proc return, then balance); the
        # read-back is authoritative, so keep the last line.
        new_balance=$(dune_psql_q --set=ctrl="$ctrl" --set=amt="$amount" -tA 2>/dev/null <<'SQL' | tail -n1 | tr -d '\r\n'
SELECT dune.adjust_player_virtual_currency_balance(:'ctrl'::bigint, dune.get_solaris_id(), :'amt'::bigint);
SELECT balance FROM dune.player_virtual_currency_balances
WHERE player_controller_id = :'ctrl'::bigint AND currency_id = dune.get_solaris_id();
SQL
)
        if [ -z "$new_balance" ]; then
            echo "[admin-publish] ERROR give-currency: proc call failed (no balance read back)" >&2
            exit 1
        fi
        echo "[admin-publish] OK give-currency fls=$fls_id controller=$ctrl delta=$amount solaris_balance=$new_balance"
        echo "publish=db-write currency=solaris controller=$ctrl delta=$amount balance=$new_balance"
        exit 0
        ;;
    rename)
        # Rename a character via dune.set_character_name(account_id, name) +
        # read the stored name back. Account-keyed. Ported from dune-admin
        # cmdRenameCharacter.
        raw="${1:?usage: rename <fls_id|me|steam:<id>|name:<n>> <new-name>}"
        new_name="${2:?usage: rename <player> <new-name>}"
        if [ -z "${new_name//[[:space:]]/}" ]; then
            echo "[admin-publish] ERROR rename: name is empty" >&2
            exit 2
        fi
        if [ "${#new_name}" -gt 64 ]; then
            echo "[admin-publish] ERROR rename: name too long (${#new_name} chars, max 64)" >&2
            exit 2
        fi
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR rename: needs a single player, not '*'" >&2
            exit 2
        fi
        assert_player_offline "$fls_id" || exit $?
        aid=$(dune_account_id "$fls_id")
        if [ -z "$aid" ]; then
            echo "[admin-publish] ERROR rename: no account for $fls_id (run 'admin players')" >&2
            exit 2
        fi
        # Proc then read-back the stored name (encrypted_character_name is plain
        # UTF-8 when User-data encryption is 'As-is'). Last line = the name.
        stored=$(dune_psql_q --set=aid="$aid" --set=nm="$new_name" -tA 2>/dev/null <<'SQL' | tail -n1 | tr -d '\r\n'
SELECT dune.set_character_name(:'aid'::bigint, :'nm');
SELECT COALESCE(convert_from(encrypted_character_name, 'UTF8'), '')
FROM dune.encrypted_player_state WHERE account_id = :'aid'::bigint;
SQL
)
        echo "[admin-publish] OK rename fls=$fls_id account=$aid stored_name=$stored"
        echo "publish=db-write rename account=$aid name=$stored"
        exit 0
        ;;
    tags-update)
        # Add and/or remove player progression tags via
        # dune.update_player_tags(account_id, add[], remove[]). Add/remove are
        # comma-separated tag lists ("" for none). Account-keyed. Ported from
        # dune-admin cmdUpdatePlayerTags.
        raw="${1:?usage: tags-update <player> <add-csv> <remove-csv>}"
        add_csv="${2-}"
        rem_csv="${3-}"
        if [ -z "$add_csv" ] && [ -z "$rem_csv" ]; then
            echo "[admin-publish] ERROR tags-update: nothing to add or remove" >&2
            exit 2
        fi
        # Tags are dotted identifiers; validate to a safe charset (also the CSV
        # separator). The :'var' binding is injection-safe; this is defence in depth.
        for v in "$add_csv" "$rem_csv"; do
            case "$v" in
                '') : ;;
                *[!A-Za-z0-9._,]*)
                    echo "[admin-publish] ERROR tags-update: tags must match [A-Za-z0-9._] (comma-separated), got '$v'" >&2
                    exit 2
                    ;;
            esac
        done
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR tags-update: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.player_tags || exit 3
        assert_player_offline "$fls_id" || exit $?
        aid=$(dune_account_id "$fls_id")
        if [ -z "$aid" ]; then
            echo "[admin-publish] ERROR tags-update: no account for $fls_id (run 'admin players')" >&2
            exit 2
        fi
        # COALESCE(string_to_array(NULLIF(:'x',''),','),'{}') -> {} for empty,
        # else the split array. Read the new tag count back (last line).
        count=$(dune_psql_q --set=aid="$aid" --set=add="$add_csv" --set=rem="$rem_csv" -tA 2>/dev/null <<'SQL' | tail -n1 | tr -d '\r\n'
SELECT dune.update_player_tags(
    :'aid'::bigint,
    COALESCE(string_to_array(NULLIF(:'add',''), ','), '{}')::text[],
    COALESCE(string_to_array(NULLIF(:'rem',''), ','), '{}')::text[]
);
SELECT count(*) FROM dune.player_tags WHERE account_id = :'aid'::bigint;
SQL
)
        echo "[admin-publish] OK tags-update fls=$fls_id account=$aid add='$add_csv' remove='$rem_csv' tag_count=$count"
        echo "publish=db-write tags-update account=$aid tag_count=$count"
        exit 0
        ;;
esac

# --------------------------------------------------------------------------
# Build the outer envelope by delegating to admin-payload.py, which takes
# every value via argv (NOT via shell-interpolated source) and emits the
# base64-encoded {Version, AuthToken, MessageContent} envelope.
#
# This replaces the previous build_inner() that constructed Python source
# code with shell-interpolated heredocs. A single triple-quote in any
# attacker-controllable field (item name, broadcast title, etc.) closed
# the Python string literal and let the rest of the value become live
# Python — pre-auth-RCE-grade vulnerability. See security audit notes.
# --------------------------------------------------------------------------
PAYLOAD_PY="$BASE/scripts/admin-payload.py"
if [ ! -f "$PAYLOAD_PY" ]; then
    echo "[admin-publish] ERROR admin-payload.py missing at $PAYLOAD_PY" >&2
    exit 1
fi

build_outer() {
    local sub="$1"; shift
    case "$sub" in
        broadcast)
            local title="${1:?title required}" body="${2:?body required}" dur="${3:-30}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" broadcast \
                --title "$title" --body "$body" --duration "$dur"
            ;;
        shutdown)
            local stype="${1:?type required (Restart|Maintenance|Update|cancel)}"
            local lead="${2:-600}"
            local freq="${3:-60}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" shutdown \
                --type "$stype" --lead-secs "$lead" --freq-secs "$freq"
            ;;
        kick|clean|reset)
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" "$sub" --player-id "$pid"
            ;;
        water)
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local amt="${2:-1000000}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" water \
                --player-id "$pid" --amount "$amt"
            ;;
        give)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local item="${2:?item FName required (case-insensitive)}"
            local qty="${3:-1}"
            local dura="${4:-1.0}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" give \
                --player-id "$pid" --item "$item" --qty "$qty" --durability "$dura"
            ;;
        xp)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local amt="${2:?xp amount required}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" xp \
                --player-id "$pid" --amount "$amt"
            ;;
        skill)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local module="${2:?module name required (e.g. Swordmaster_T1)}"
            local lvl="${3:?level required}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" skill \
                --player-id "$pid" --module "$module" --level "$lvl"
            ;;
        points)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local pts="${2:?skill points required}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" points \
                --player-id "$pid" --amount "$pts"
            ;;
        teleport|tpsafe)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local x="${2:?x required}" y="${3:?y required}" z="${4:?z required}"
            local yaw="${5:-}"
            local -a args=(--token "$ADMIN_TOKEN" "$sub" \
                --player-id "$pid" --x "$x" --y "$y" --z "$z")
            if [ -n "$yaw" ]; then
                args+=(--yaw "$yaw")
            fi
            python3 "$PAYLOAD_PY" "${args[@]}"
            ;;
        vehicle)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local cls="${2:?vehicle class required (e.g. Sandbike, Buggy)}"
            local x="${3:?x required}" y="${4:?y required}" z="${5:?z required}"
            local tpl="${6:?template name required (e.g. T6_Combat)}"
            local rot="${7:-}"
            local persist="${8:-1.0}"
            local faction="${9:-}"
            local -a args=(--token "$ADMIN_TOKEN" vehicle \
                --player-id "$pid" --class "$cls" \
                --x "$x" --y "$y" --z "$z" \
                --template "$tpl" --persistent "$persist")
            if [ -n "$rot" ]; then
                args+=(--rotation "$rot")
            fi
            if [ -n "$faction" ]; then
                args+=(--faction "$faction")
            fi
            python3 "$PAYLOAD_PY" "${args[@]}"
            ;;
        cheat)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local name="${2:?script name required (e.g. PlaytestSetupAdmin)}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" cheat \
                --player-id "$pid" --script "$name"
            ;;
        exec)
            local raw_cmd="${1:?exec command required}"
            python3 "$PAYLOAD_PY" --token "$ADMIN_TOKEN" serverexec \
                --command "$raw_cmd"
            ;;
        *)
            # NOTE: the `raw` subcommand was deliberately removed — it
            # accepted arbitrary JSON and round-tripped it through a
            # python -c heredoc, giving any HTTP-reachable caller an
            # avenue for Python code execution.
            echo "[admin-publish] ERROR unknown subcommand: $sub" >&2
            usage
            exit 2
            ;;
    esac
}

# rabbitmqctl is only required for the actual publish. Lookup
# subcommands exited above; only publish paths reach this far.
if [ "${DUNE_ADMIN_DRY_RUN:-0}" != "1" ] && [ ! -x "$RMQ_SBIN/rabbitmqctl" ]; then
    echo "[admin-publish] ERROR rabbitmqctl missing at $RMQ_SBIN/rabbitmqctl" >&2
    echo "[admin-publish]   run from inside the Pelican container, or set DUNE_ADMIN_DRY_RUN=1" >&2
    exit 1
fi

OUTER_B64=$(build_outer "$cmd" "$@")

# Sanitise the label (used in the Erlang io:format and the MsgId prefix —
# only ASCII letters/digits/underscore/dash, max 64 chars).
LABEL=$(printf '%s' "$cmd" | tr -c 'A-Za-z0-9_-' '_' | cut -c1-64)
[ -n "$LABEL" ] || LABEL=smgmt

# --------------------------------------------------------------------------
# Compose the Erlang publish snippet. Format matches adainrivers/mq.rs
# byte-for-byte so the server-side log line stays consistent across
# different admin clients.
# --------------------------------------------------------------------------
read -r -d '' ERLANG_SRC <<EOF || true
Outer = base64:decode(<<"$OUTER_B64">>),
XName = rabbit_misc:r(<<"/">>, exchange, <<"heartbeats">>),
X = rabbit_exchange:lookup_or_die(XName),
MsgId = list_to_binary("smgmt-$LABEL-" ++ integer_to_list(erlang:system_time(millisecond))),
P = {list_to_atom("P_basic"), <<"Content">>, undefined, [], undefined, undefined, undefined, undefined, undefined, MsgId, undefined, undefined, <<"fls">>, <<"fls_backend">>, undefined},
Content = rabbit_basic:build_content(P, Outer),
{ok, Msg} = rabbit_basic:message(XName, <<"notifications">>, Content),
Result = rabbit_queue_type:publish_at_most_once(X, Msg),
io:format("publish=~p exchange=heartbeats routing=notifications app_id=fls_backend user_id=fls label=$LABEL~n", [Result]).
EOF

if [ "${DUNE_ADMIN_DRY_RUN:-0}" = "1" ]; then
    echo "=== DRY RUN (DUNE_ADMIN_DRY_RUN=1) ==="
    echo "Subcommand:  $cmd $*"
    # Decode the outer envelope back to the inner JSON for diagnostics.
    DECODED_OUTER=$(printf '%s' "$OUTER_B64" | base64 -d 2>/dev/null || echo "<base64 decode failed>")
    echo "Outer JSON:  $DECODED_OUTER"
    echo "Outer base64: $OUTER_B64"
    echo "Token:       $ADMIN_TOKEN"
    echo "Node:        $ADMIN_NODE"
    echo "Erlang publish snippet:"
    printf '%s\n' "$ERLANG_SRC"
    exit 0
fi

# --------------------------------------------------------------------------
# Run rabbitmqctl eval. PATH / ERL_LIBS need the extracted Funcom paths so
# that beam.smp + escript resolve from the OCI tree.
# --------------------------------------------------------------------------
export PATH="$RMQ_SBIN:$ERL_ROOT/erts-14.2.5.12/bin:$ERL_ROOT/bin:$PATH"
export ERL_LIBS="$ERL_ROOT/lib"
export LD_LIBRARY_PATH="$MQ_ROOT/usr/lib:$MQ_ROOT/usr/local/lib:${LD_LIBRARY_PATH:-}"

# rabbitmqctl reads its .erlang.cookie from $HOME/.erlang.cookie. The
# admin broker (rabbit-admin@localhost) and the game broker
# (rabbit-game@localhost) use DIFFERENT cookies, stored under different
# runtime directories. Pick the one that matches DUNE_ADMIN_NODE.
case "$ADMIN_NODE" in
    rabbit-game@*)  export HOME="$BASE/runtime/mq-game-home" ;;
    rabbit-admin@*) export HOME="$BASE/runtime/mq-admin-home" ;;
    *)              export HOME="${DUNE_ADMIN_HOME:-$BASE/runtime/mq-admin-home}" ;;
esac

OUTPUT=$("$RMQ_SBIN/rabbitmqctl" --node "$ADMIN_NODE" eval "$ERLANG_SRC" 2>&1) || rc=$?
rc="${rc:-0}"

if [ "$rc" -ne 0 ]; then
    echo "[admin-publish] ERROR rabbitmqctl eval exited $rc" >&2
    echo "$OUTPUT" >&2
    exit "$rc"
fi

if printf '%s' "$OUTPUT" | grep -q 'publish=ok'; then
    echo "[admin-publish] OK $cmd label=$LABEL"
    printf '%s\n' "$OUTPUT"
else
    echo "[admin-publish] WARN no publish=ok in response — broker may have rejected" >&2
    printf '%s\n' "$OUTPUT"
    exit 3
fi
