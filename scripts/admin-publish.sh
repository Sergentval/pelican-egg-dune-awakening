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
            local resolved
            resolved=$(dune_psql -tAc "SELECT \"user\" FROM dune.encrypted_accounts WHERE platform_id='$sid' AND platform_name='Steam' LIMIT 1" 2>/dev/null | tr -d '\r\n')
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
            local resolved
            resolved=$(dune_psql -tAc "
                SELECT a.\"user\"
                FROM dune.encrypted_accounts a
                JOIN dune.encrypted_player_state ps ON ps.account_id=a.id
                WHERE lower(convert_from(ps.encrypted_character_name, 'UTF8')) = lower('$nm')
                LIMIT 1
            " 2>/dev/null | tr -d '\r\n')
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
        row=$(dune_psql -tAF $'\t' -c "
            SELECT ac.map, ac.partition_id, ac.transform::text
            FROM dune.actors ac
            JOIN dune.encrypted_accounts a ON a.id = ac.owner_account_id
            WHERE a.\"user\" = '$fls_id'
              AND ac.class LIKE '%BP_DunePlayerCharacter%'
            ORDER BY ac.id DESC
            LIMIT 1
        " 2>/dev/null)
        if [ -z "$row" ]; then
            echo "[admin-publish] ERROR no BP_DunePlayerCharacter row for $fls_id" >&2
            echo "                player may be offline or in a Sietch we haven't queried." >&2
            exit 1
        fi
        # row is "map<TAB>partition_id<TAB>("(x,y,z)","(qx,qy,qz,qw)")"
        python3 -c "
import re, sys
row = sys.stdin.read().strip()
parts = row.split('\t')
if len(parts) < 3:
    sys.stderr.write(f'unexpected row: {row}\n'); sys.exit(1)
mapname, partition, tform = parts[0], parts[1], '\t'.join(parts[2:])
m = re.search(r'\((-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)\)', tform)
if not m:
    sys.stderr.write(f'could not parse transform: {tform}\n'); sys.exit(1)
x, y, z = (float(g) for g in m.groups())
print(f'FLS:        $fls_id')
print(f'Map:        {mapname}  (partition {partition})')
print(f'Position:   X={x:.2f}  Y={y:.2f}  Z={z:.2f}')
print()
print('Ready-to-paste commands:')
print(f'  admin teleport $raw {x:.0f} {y:.0f} {z:.0f}')
print(f'  admin vehicle  $raw Sandbike {x:.0f} {y:.0f} {z:.0f} T3_Boost')
print(f'  admin vehicle  $raw OrnithopterLight {x:.0f} {y:.0f} {int(z+200)} T6_Combat')
print()
print(f'Raw transform: {tform}')
" <<< "$row"
        exit 0
        ;;
    vehicles|items|skills|items-json)
        # Catalogue lookups — read-only, hit the bundled data/admin/*.json
        # files. Saves humans from grepping 293KB of items.json by hand.
        # Data files are MIT-licensed copies from
        # adainrivers/dune-dedicated-server-manager — see ATTRIBUTION.md.
        DUNE_BASE_DIR="$BASE" exec python3 "$BASE/scripts/admin-lookup.py" "$cmd" "$@"
        ;;
esac

# --------------------------------------------------------------------------
# Build the inner JSON payload based on the subcommand. Each subcommand
# emits a single line of compact JSON to stdout via python3.
# --------------------------------------------------------------------------
build_inner() {
    local sub="$1"; shift
    case "$sub" in
        broadcast)
            # UE5's broadcast renderer requires a LocalizedText[] array with
            # at least one locale entry. Without it: LogJson "Field
            # LocalizedText not found" + "Null used as Array" -> banner
            # dropped (handler logs the dispatch but never shows it
            # in-game). BroadcastDuration also lives INSIDE BroadcastPayload,
            # not at the top level. Shape verified against
            # adainrivers/dune-dedicated-server-manager build.rs.
            local title="${1:?title required}" body="${2:?body required}" dur="${3:-30}"
            python3 -c "
import json, sys
print(json.dumps({
    'ServerCommand': 'ServiceBroadcast',
    'BroadcastType': 'Generic',
    'BroadcastPayload': {
        'BroadcastDuration': int('$dur'),
        'LocalizedText': [
            {'Key': 'en',    'Title': '''$title''', 'Body': '''$body'''},
            {'Key': 'en-US', 'Title': '''$title''', 'Body': '''$body'''},
        ],
    },
}, separators=(',',':')))
"
            ;;
        shutdown)
            # Same shape rule as broadcast: BroadcastPayload holds the real
            # fields; ShutdownTimestamp + DateTimestamp are required by the
            # ServerShutdown parser. Use 'cancel' as the stype to abort a
            # pending shutdown without any other metadata.
            local stype="${1:?type required (Restart|Maintenance|Update|cancel)}"
            local lead="${2:-600}"
            local freq="${3:-60}"
            python3 -c "
import json, time
stype = '$stype'
if stype.lower() == 'cancel':
    inner = {
        'ServerCommand': 'ServiceBroadcast',
        'BroadcastType': 'ServerShutdown',
        'BroadcastPayload': {'ShouldCancel': True},
    }
else:
    now = int(time.time())
    lead = max(1, int('$lead'))
    inner = {
        'ServerCommand': 'ServiceBroadcast',
        'BroadcastType': 'ServerShutdown',
        'BroadcastPayload': {
            'ShutdownType': stype,
            'DateTimestamp': now,
            'ShutdownDuration': lead,
            'ShutdownTimestamp': now + lead,
            'BroadcastFrequency': max(1, int('$freq')),
            'BroadcastDuration': 30,
        },
    }
print(json.dumps(inner, separators=(',',':')))
"
            ;;
        kick)
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'KickPlayer', 'PlayerId': '''$pid'''}, separators=(',',':')))
"
            ;;
        clean)
            # CleanPlayerInventory — DESTRUCTIVE. Wipes the target's inventory.
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'CleanPlayerInventory', 'PlayerId': '''$pid'''}, separators=(',',':')))
"
            ;;
        reset)
            # ResetProgression — DESTRUCTIVE. Wipes XP/skills/journey for target.
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'ResetProgression', 'PlayerId': '''$pid'''}, separators=(',',':')))
"
            ;;
        water)
            # UpdateAllWaterFillables — refills water in target's fillable
            # containers (jerrycans, stills, etc). Default amount is 1 000 000.
            local pid_raw="${1:?player id required — pass FLS id, me, steam:<id>, or *}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local amt="${2:-1000000}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'UpdateAllWaterFillables',
    'PlayerId': '''$pid''',
    'WaterAmount': int('$amt'),
}, separators=(',',':')))
"
            ;;
        give)
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local item="${2:?item FName required (case-insensitive)}"
            local qty="${3:-1}"
            local dura="${4:-1.0}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'AddItemToInventory',
    'PlayerId': '''$pid''',
    'ItemName': '''$item''',
    'Quantity': int('$qty'),
    'Durability': float('$dura'),
}, separators=(',',':')))
"
            ;;
        xp)
            # AwardXP. CRITICAL: the seabass handler silently no-ops unless
            # `Category` is present in the payload. The value itself is
            # ignored (every category lands as generic player XP) but the
            # field must exist. Injecting "Combat" as a known-accepted value.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local amt="${2:?xp amount required}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'AwardXP',
    'PlayerId': '''$pid''',
    'Experience': int('$amt'),
    'Category': 'Combat',
}, separators=(',',':')))
"
            ;;
        skill)
            # SkillsSetModuleLevel — sets a specific skill module's level.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local module="${2:?module name required (e.g. Swordmaster_T1)}"
            local lvl="${3:?level required}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'SkillsSetModuleLevel',
    'PlayerId': '''$pid''',
    'Module': '''$module''',
    'Level': int('$lvl'),
}, separators=(',',':')))
"
            ;;
        points)
            # SkillsSetUnspentSkillPoints — sets the unspent skill point pool.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local pts="${2:?skill points required}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'SkillsSetUnspentSkillPoints',
    'PlayerId': '''$pid''',
    'SkillPoints': int('$pts'),
}, separators=(',',':')))
"
            ;;
        teleport)
            # TeleportToExact — drops the player at the EXACT XYZ. No safety
            # snapping. Use tpsafe instead if you want collision/safe-spawn.
            # Optional yaw rotates the player around vertical axis.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local x="${2:?x required}" y="${3:?y required}" z="${4:?z required}"
            local yaw="${5:-}"
            python3 -c "
import json
inner = {
    'ServerCommand': 'TeleportToExact',
    'PlayerId': '''$pid''',
    'X': float('$x'), 'Y': float('$y'), 'Z': float('$z'),
}
yaw = '$yaw'
if yaw:
    inner['Yaw'] = float(yaw)
print(json.dumps(inner, separators=(',',':')))
"
            ;;
        tpsafe)
            # TeleportTo — snaps to the nearest safe (non-clipping, on-ground)
            # location near the requested XYZ. Same field set as teleport.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local x="${2:?x required}" y="${3:?y required}" z="${4:?z required}"
            local yaw="${5:-}"
            python3 -c "
import json
inner = {
    'ServerCommand': 'TeleportTo',
    'PlayerId': '''$pid''',
    'X': float('$x'), 'Y': float('$y'), 'Z': float('$z'),
}
yaw = '$yaw'
if yaw:
    inner['Yaw'] = float(yaw)
print(json.dumps(inner, separators=(',',':')))
"
            ;;
        vehicle)
            # SpawnVehicleAt — spawns a vehicle of <class> with <template>
            # variant at XYZ. ClassName + TemplateName are DT_VehicleTemplates
            # row keys. Persistent defaults to 1.0 (persists across restart).
            # Optional Faction (free-text) overrides the default CHOAM skin —
            # try Atreides / Harkonnen / Choam / Smuggler / faction tag.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local cls="${2:?vehicle class required (e.g. Sandbike, Buggy)}"
            local x="${3:?x required}" y="${4:?y required}" z="${5:?z required}"
            local tpl="${6:?template name required (e.g. T6_Combat)}"
            local rot="${7:-}"
            local persist="${8:-1.0}"
            local faction="${9:-}"
            python3 -c "
import json
inner = {
    'ServerCommand': 'SpawnVehicleAt',
    'PlayerId': '''$pid''',
    'ClassName': '''$cls''',
    'X': float('$x'), 'Y': float('$y'), 'Z': float('$z'),
    'TemplateName': '''$tpl''',
    'Persistent': float('$persist'),
}
rot = '$rot'
if rot:
    inner['Rotation'] = float(rot)
faction = '''$faction'''
if faction:
    inner['Faction'] = faction
print(json.dumps(inner, separators=(',',':')))
"
            ;;
        cheat)
            # CheatScript — runs a [CheatScript.<name>] block from
            # DefaultGame.ini. KNOWN NO-OP on seabass servers (handler logs
            # the call but applies no state). Kept for protocol parity.
            local pid_raw="${1:?player id required — pass FLS id, me, or steam:<id>}"
            local pid
            pid=$(resolve_player_id "$pid_raw") || exit 1
            local name="${2:?script name required (e.g. PlaytestSetupAdmin)}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'CheatScript',
    'PlayerId': '''$pid''',
    'ScriptName': '''$name''',
}, separators=(',',':')))
"
            ;;
        exec)
            # ServerExec — raw console/exec passthrough. Field name is "Exec"
            # (NOT "Command" — we had this wrong before commit 4f69d10).
            # KNOWN NO-OP on seabass servers (publishes but handler doesn't
            # execute). Kept for protocol parity.
            local raw_cmd="${1:?exec command required}"
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'ServerExec', 'Exec': '''$raw_cmd'''}, separators=(',',':')))
"
            ;;
        raw)
            local inner="${1:?inline JSON required}"
            # Round-trip through python to validate it's parseable JSON.
            python3 -c "
import json, sys
s = '''$inner'''
print(json.dumps(json.loads(s), separators=(',',':')))
"
            ;;
        *)
            echo "[admin-publish] ERROR unknown subcommand: $sub" >&2
            usage
            exit 2
            ;;
    esac
}

INNER_JSON=$(build_inner "$cmd" "$@")

# rabbitmqctl is only required for the actual publish. Lookup
# subcommands exited above; only publish paths reach this far.
if [ "${DUNE_ADMIN_DRY_RUN:-0}" != "1" ] && [ ! -x "$RMQ_SBIN/rabbitmqctl" ]; then
    echo "[admin-publish] ERROR rabbitmqctl missing at $RMQ_SBIN/rabbitmqctl" >&2
    echo "[admin-publish]   run from inside the Pelican container, or set DUNE_ADMIN_DRY_RUN=1" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Build the outer envelope: {Version: 2, AuthToken: <token>, MessageContent: <inner-as-string>}
# Note MessageContent is a STRING containing JSON, not a nested object.
# --------------------------------------------------------------------------
OUTER_B64=$(python3 -c "
import base64, json
outer = {'Version': 2, 'AuthToken': '''$ADMIN_TOKEN''', 'MessageContent': '''$INNER_JSON'''}
print(base64.standard_b64encode(json.dumps(outer, separators=(',',':')).encode()).decode())
")

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
    echo "Inner JSON:  $INNER_JSON"
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
