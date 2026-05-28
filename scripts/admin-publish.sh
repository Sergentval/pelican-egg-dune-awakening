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
# Subcommands:
#   broadcast <title> <body> [duration_secs]   -- ServiceBroadcast (Generic)
#   shutdown <type> <lead_secs> [freq_secs]    -- ServerShutdown (Restart|Maintenance|Update)
#   kick <player_id>                            -- KickPlayer (FLS id or "*" for all online)
#   give <player_id> <item_fname> [qty] [dura] -- AddItemToInventory
#   xp <player_id> <amount>                     -- AwardXP
#   teleport <player_id> <x> <y> <z>            -- TeleportToExact
#   exec <raw_console_command>                  -- ServerExec (escape hatch)
#   raw '<inner-json>'                          -- arbitrary ServerCommand JSON
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

# rabbitmqctl is only required for the actual publish — DRY_RUN should be
# usable on any host (e.g. for local development of new subcommands).
if [ "${DUNE_ADMIN_DRY_RUN:-0}" != "1" ] && [ ! -x "$RMQ_SBIN/rabbitmqctl" ]; then
    echo "[admin-publish] ERROR rabbitmqctl missing at $RMQ_SBIN/rabbitmqctl" >&2
    echo "[admin-publish]   run from inside the Pelican container, or set DUNE_ADMIN_DRY_RUN=1" >&2
    exit 1
fi

usage() {
    sed -n '1,55p' "$0" | sed -n 's/^# \?//p'
}

cmd="${1:-}"
if [ -z "$cmd" ]; then
    usage
    exit 2
fi
shift

# --------------------------------------------------------------------------
# Build the inner JSON payload based on the subcommand. Each subcommand
# emits a single line of compact JSON to stdout via python3.
# --------------------------------------------------------------------------
build_inner() {
    local sub="$1"; shift
    case "$sub" in
        broadcast)
            local title="${1:?title required}" body="${2:?body required}" dur="${3:-30}"
            python3 -c "
import json, sys
print(json.dumps({
    'ServerCommand': 'ServiceBroadcast',
    'BroadcastType': 'Generic',
    'BroadcastPayload': {'Title': '''$title''', 'Body': '''$body'''},
    'BroadcastDuration': int('$dur'),
}, separators=(',',':')))
"
            ;;
        shutdown)
            local stype="${1:?type required (Restart|Maintenance|Update)}"
            local lead="${2:?lead seconds required}"
            local freq="${3:-60}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'ServiceBroadcast',
    'BroadcastType': 'ServerShutdown',
    'ShutdownType': '$stype',
    'ShutdownDuration': int('$lead'),
    'BroadcastFrequency': int('$freq'),
    'BroadcastDuration': 30,
}, separators=(',',':')))
"
            ;;
        kick)
            local pid="${1:?player id required (or \"*\")}"
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'KickPlayer', 'PlayerId': '''$pid'''}, separators=(',',':')))
"
            ;;
        give)
            local pid="${1:?player id required}"
            local item="${2:?item fname required}"
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
            local pid="${1:?player id required}"
            local amt="${2:?xp amount required}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'AwardXP',
    'PlayerId': '''$pid''',
    'Experience': int('$amt'),
}, separators=(',',':')))
"
            ;;
        teleport)
            local pid="${1:?player id required}"
            local x="${2:?x required}" y="${3:?y required}" z="${4:?z required}"
            python3 -c "
import json
print(json.dumps({
    'ServerCommand': 'TeleportToExact',
    'PlayerId': '''$pid''',
    'X': float('$x'), 'Y': float('$y'), 'Z': float('$z'),
}, separators=(',',':')))
"
            ;;
        exec)
            local raw_cmd="${1:?console command required}"
            python3 -c "
import json
print(json.dumps({'ServerCommand': 'ServerExec', 'Command': '''$raw_cmd'''}, separators=(',',':')))
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
