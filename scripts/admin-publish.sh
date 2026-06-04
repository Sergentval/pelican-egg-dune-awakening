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
#   award-char-xp <player_id> <amount>           -- award/deduct char XP; recompute level/SP/intel (offline only)
#   grant-keystones <player_id>                  -- grant all 205 keystones + recompute SP (offline only)
#   tech-unlock <player_id> <unlock-all|lock-all> -- flip discovered recipes' UnlockedState on the pawn (offline only, reversible)
#   give-item <player_id> <ItemTemplate> [qty=1] [quality=0]
#                                                -- grant items: online+quality0 -> RMQ AddItemToInventory;
#                                                   offline -> direct dune.items INSERT (stack/slot/volume planned)
#   faction-rep <player_id> <atreides|harkonnen|1|2> <signed-amount>
#                                                -- adjust Great-House reputation (offline only); rebuilds the
#                                                   FactionPlayerComponent.m_FactionDataArray jsonb on the controller
#
# Player/character DESTRUCTIVE write subcommands (offline-gated; hardened beyond
# dune-admin, which applies no server-side guard). Ported from Icehunter/
# dune-admin (MIT). See ATTRIBUTION.md.
#   item-delete <item_id>                        -- hard-delete one item stack by dune.items.id
#                                                   (offline-gated on the owner + existence-checked; no DB backup)
#   reset-spec <player_id>                       -- DESTRUCTIVE: wipe ALL spec tracks + purchased keystones
#                                                   (controller-keyed, offline only; does not recompute FLevel SP)
#   account-delete <player_id> <confirm-fls> [reason]
#                                                -- IRREVERSIBLE: delete the whole account/character + cascade.
#                                                   <confirm-fls> MUST equal the resolved 16-hex FLS id; offline only.
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

# Write absolute reputation for one Great House on a CONTROLLER actor and rebuild
# the FactionPlayerComponent.m_FactionDataArray jsonb cache (both houses, Atreides
# first, never clobbering the other) in one transaction. Echoes psql output
# (last line = the stored reputation) and returns PSQL's exit code (no internal
# pipe, so ON_ERROR_STOP rollbacks surface). Shared by faction-rep (delta) and
# set-faction-tier (absolute). Ported from dune-admin syncFactionComponent (MIT).
dune_apply_faction_rep() {
    local ctrl=$1 fid=$2 newrep=$3
    dune_psql_q -q -v ON_ERROR_STOP=1 \
        --set=ctrl="$ctrl" --set=fid="$fid" --set=newrep="$newrep" -tA 2>&1 <<'SQL'
BEGIN;
SELECT dune.set_player_faction_reputation(:'ctrl'::bigint, :'fid'::smallint, :'newrep'::integer);
UPDATE dune.actors SET properties = jsonb_set(
    properties,
    '{FactionPlayerComponent,m_FactionDataArray}',
    (SELECT jsonb_agg(
         jsonb_build_object(
             'Faction', jsonb_build_object('Name', gh.name),
             'timestamp', extract(epoch FROM clock_timestamp()),
             'ReputationAmount', COALESCE(r.reputation_amount, 0)
         ) ORDER BY gh.id)
     FROM (VALUES (1::smallint, 'Atreides'), (2::smallint, 'Harkonnen')) AS gh(id, name)
     LEFT JOIN dune.player_faction_reputation r
       ON r.actor_id = :'ctrl'::bigint AND r.faction_id = gh.id),
    true)
WHERE id = :'ctrl'::bigint;
COMMIT;
SELECT reputation_amount FROM dune.player_faction_reputation
WHERE actor_id = :'ctrl'::bigint AND faction_id = :'fid'::smallint;
SQL
}

# Set every entry's UnlockedState (+ bIsNewEntry) in the pawn's
# TechKnowledgePlayerComponent.m_TechKnowledge.m_TechKnowledgeData array in one
# transaction (ON_ERROR_STOP so a failure rolls back). Echoes "<at-state>/<total>"
# as the last line. Mirrors dune_apply_faction_rep's jsonb-rebuild shape. Only the
# entries already in the array (recipes the player has discovered) are touched;
# m_TechKnowledgePoints / m_NextTechTreeUpgradeIndex are left as-is.
dune_apply_tech_unlock() {
    local pawn=$1 state=$2 bnew=$3
    dune_psql_q -q -v ON_ERROR_STOP=1 \
        --set=pawn="$pawn" --set=state="$state" --set=bnew="$bnew" -tA 2>&1 <<'SQL'
BEGIN;
UPDATE dune.actors SET properties = jsonb_set(
    properties,
    '{TechKnowledgePlayerComponent,m_TechKnowledge,m_TechKnowledgeData}',
    (SELECT jsonb_agg(
         jsonb_set(elem, '{UnlockedState}', to_jsonb(:'state'::text))
           || jsonb_build_object('bIsNewEntry', :'bnew'::boolean)
         ORDER BY ord)
     FROM jsonb_array_elements(
            properties->'TechKnowledgePlayerComponent'->'m_TechKnowledge'->'m_TechKnowledgeData'
          ) WITH ORDINALITY AS t(elem, ord)),
    true)
WHERE id = :'pawn'::bigint
  AND (properties->'TechKnowledgePlayerComponent'->'m_TechKnowledge') ? 'm_TechKnowledgeData';
COMMIT;
SELECT count(*) FILTER (WHERE e->>'UnlockedState' = :'state') || '/' || count(*)
FROM dune.actors a,
     jsonb_array_elements(a.properties->'TechKnowledgePlayerComponent'->'m_TechKnowledge'->'m_TechKnowledgeData') e
WHERE a.id = :'pawn'::bigint;
SQL
}

# Provision/find the market-bot identity. Echoes "owner_id|exchange_id|access_point_id|inventory_id".
# Creates the synthetic 'Revy' bot actor + its exchange user on first call.
# Exchange/AP detection mirrors dune-admin Init (accesspoint's exchange first, so
# bot listings land on the exchange players actually reach). Ported (MIT).
dune_market_provision() {
    local owner part exch ap inv
    owner=$(dune_psql_q -tA -q 2>/dev/null <<'SQL' | head -1
SELECT id FROM dune.actors WHERE class = 'Revy' LIMIT 1
SQL
)
    if [ -z "$owner" ]; then
        part=$(dune_psql_q -tA -q 2>/dev/null <<'SQL' | head -1
SELECT partition_id FROM dune.world_partition ORDER BY partition_id LIMIT 1
SQL
)
        owner=$(dune_psql_q -tA -q --set=part="${part:-1}" 2>/dev/null <<'SQL' | head -1
INSERT INTO dune.actors (class, serial, gas_attributes, properties, dimension_index, partition_id)
VALUES ('Revy', 0, '{}', '{}', 0, :'part'::bigint) RETURNING id
SQL
)
    fi
    [ -n "$owner" ] || return 1
    dune_psql_q -tA -q --set=o="$owner" >/dev/null 2>&1 <<'SQL'
SELECT dune.dune_exchange_get_user_id(:'o'::bigint)
SQL
    exch=$(dune_psql_q -tA -q 2>/dev/null <<'SQL' | head -1
SELECT COALESCE(
  (SELECT ap.exchange_id FROM dune.dune_exchange_accesspoints ap
     JOIN dune.dune_exchanges e ON e.id = ap.exchange_id ORDER BY ap.id LIMIT 1),
  (SELECT exchange_id FROM dune.dune_exchange_orders WHERE is_npc_order = FALSE LIMIT 1),
  (SELECT id FROM dune.dune_exchanges ORDER BY id LIMIT 1),
  dune.get_dune_exchange_id('Global'))
SQL
)
    [ -n "$exch" ] || return 1
    ap=$(dune_psql_q -tA -q --set=e="$exch" 2>/dev/null <<'SQL' | head -1
SELECT COALESCE(
  (SELECT id FROM dune.dune_exchange_accesspoints WHERE exchange_id = :'e'::bigint ORDER BY id LIMIT 1),
  (SELECT DISTINCT access_point_id FROM dune.dune_exchange_orders WHERE exchange_id = :'e'::bigint LIMIT 1),
  1)
SQL
)
    inv=$(dune_psql_q -tA -q --set=e="$exch" 2>/dev/null <<'SQL' | head -1
SELECT dune.get_exchange_inventory_id(:'e'::bigint)
SQL
)
    [ -n "$inv" ] || return 1
    echo "${owner}|${exch}|${ap}|${inv}"
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

    db-backup)
        # pg_dump -Fc of the whole `dune` database -> $BASE/backups/dune-<ts>.dump
        # (under the Pelican file root, so it's visible + downloadable in the panel).
        # Verifies the dump is non-empty, then prunes to retention. Read of DB,
        # write of a file; safe to run while the server is up (pg_dump is a
        # consistent snapshot).
        keep="${1:-${DUNE_BACKUP_RETENTION:-7}}"
        backups="$BASE/backups"
        mkdir -p "$backups"
        ts=$(date -u +%Y%m%d-%H%M%S)
        out="$backups/dune-$ts.dump"
        if LD_LIBRARY_PATH="$PG_LIBS" ICU_DATA="$PG_ICU" "$PG_BIN/pg_dump" \
                -h "$PG_SOCK" -p "$PG_PORT" -U dune -d dune -Fc -f "$out"; then
            sz=$(stat -c%s "$out" 2>/dev/null || echo 0)
            if [ "${sz:-0}" -lt 1 ]; then
                echo "[admin-publish] ERROR db-backup: dump is empty" >&2
                rm -f "$out"; exit 1
            fi
            python3 "$BASE/scripts/admin_backup.py" prune "$backups" "$keep" >/dev/null 2>&1 || true
            echo "backup=ok file=dune-$ts.dump bytes=$sz"
            exit 0
        fi
        echo "[admin-publish] ERROR db-backup: pg_dump failed" >&2
        rm -f "$out"; exit 1
        ;;

    db-backup-list)
        # CSV (file,bytes,mtime) of existing backups, newest first.
        backups="$BASE/backups"
        echo "file,bytes,mtime"
        [ -d "$backups" ] || exit 0
        for f in $(ls -1t "$backups"/dune-*.dump 2>/dev/null); do
            printf '%s,%s,%s\n' "$(basename "$f")" "$(stat -c%s "$f" 2>/dev/null)" \
                "$(date -u -d "@$(stat -c%Y "$f" 2>/dev/null)" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
        done
        exit 0
        ;;

    db-restore)
        # DESTRUCTIVE, CLI-ONLY. pg_restore --clean a backup into the dune DB.
        # Hard-gated: confirm token must be RESTORE, and NO UE5 game server may be
        # running (restore needs an idle DB; a live server would race + corrupt).
        file="${1:?backup filename required}"
        confirm="${2:-}"
        file=$(basename "$file")   # no path traversal
        path="$BASE/backups/$file"
        [ -f "$path" ] || { echo "[admin-publish] ERROR db-restore: no such backup '$file'" >&2; exit 1; }
        [ "$confirm" = "RESTORE" ] || { echo "[admin-publish] ERROR db-restore: pass confirm token RESTORE as arg 2" >&2; exit 2; }
        if pgrep -f 'DuneSandboxServer-Linux-Shipping' >/dev/null 2>&1; then
            echo "[admin-publish] ERROR db-restore: UE5 server is running — stop the server first (restore is destructive + needs an idle DB)" >&2
            exit 1
        fi
        if LD_LIBRARY_PATH="$PG_LIBS" ICU_DATA="$PG_ICU" "$PG_BIN/pg_restore" \
                -h "$PG_SOCK" -p "$PG_PORT" -U dune -d dune --clean --if-exists "$path"; then
            echo "restore=ok file=$file"
            exit 0
        fi
        echo "[admin-publish] ERROR db-restore: pg_restore failed" >&2; exit 1
        ;;

    server-status)
        # Phase 4: per-map live player count for the server status grid. Counts
        # online players' BP_DunePlayerCharacter actors grouped by their current
        # map. Read-only (session pinned READ ONLY); emits CSV `map,players`
        # (header included). admin-http.py GET /api/status merges this with
        # mock-k8s /status (instance/scale status) via admin_status.merge_status.
        # No user input -> the static query is safe to pass via -c.
        dune_psql -q --csv \
            -c "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" \
            -c "SELECT ac.map AS map, COUNT(*) AS players
                FROM dune.actors ac
                JOIN dune.encrypted_player_state ps ON ps.account_id = ac.owner_account_id
                WHERE ac.class LIKE '%BP_DunePlayerCharacter%'
                  AND ps.online_status = 'Online'
                GROUP BY ac.map
                ORDER BY ac.map"
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
       inv.inventory_type AS inv_type,
       i.template_id  AS template_id,
       i.stack_size   AS stack_size,
       i.quality_level AS quality,
       COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'CurrentDurability'), 'N/A') AS durability,
       COALESCE((i.stats->'FItemStackAndDurabilityStats'->1->>'MaxDurability'), 'N/A')     AS max_durability,
       i.position_index AS slot
FROM dune.items i
JOIN dune.inventories inv ON i.inventory_id = inv.id
WHERE inv.actor_id = :'actor'::bigint
ORDER BY inv.inventory_type, i.position_index
SQL
        exit 0
        ;;
    map-markers)
        # Live-map player markers on one map (read-only). Map is whitelisted so
        # caller input never reaches the query as an unexpected value. Position
        # is the player's PAWN actor transform; fls is accounts."user" (the
        # canonical PlayerId), matching admin_map.parse_markers' expected header.
        #
        # CONFIRMED SCHEMA (live-verified on server 30, 2026-06-02): unlike the
        # other reads here (which decrypt encrypted_player_state.encrypted_character_name
        # via convert_from), dune.player_state ALSO carries plaintext character_name +
        # online_status, and dune.accounts."user" is the FLS id. This join returns the
        # online char with its name + Online status. dune_require_tables only checks
        # table existence, so these columns were confirmed by that live test, not the guard.
        map_name="${1:?map name required: HaggaBasin|DeepDesert|Arrakeen|HarkoVillage}"
        case "$map_name" in
            HaggaBasin|DeepDesert|Arrakeen|HarkoVillage) ;;
            *) echo "[admin-publish] ERROR map-markers: unsupported map '$map_name'" >&2; exit 2 ;;
        esac
        dune_require_tables dune.actors dune.player_state dune.accounts || exit 3
        dune_psql_q --csv --set=map="$map_name" <<'SQL'
SELECT a.id AS id,
       COALESCE(NULLIF(ps.character_name, ''), 'Unknown') AS name,
       COALESCE(ps.online_status::text, '') AS online,
       COALESCE(a.partition_id, 0) AS partition,
       COALESCE(ac."user", '') AS fls,
       ((a.transform).location).x AS x,
       ((a.transform).location).y AS y,
       ((a.transform).location).z AS z
FROM dune.actors a
JOIN dune.player_state ps ON ps.player_pawn_id = a.id
LEFT JOIN dune.accounts ac ON ac.id = ps.account_id
WHERE a.map = :'map' AND a.transform IS NOT NULL
ORDER BY name
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
    award-char-xp)
        # Award (or deduct) character XP and re-derive level / skill points /
        # intel, then jsonb_set FLevelComponent (XP + both SP fields) on the
        # PAWN's fgl entity and TechKnowledgePlayerComponent intel on the pawn
        # actor. Ported from dune-admin cmdAwardCharXP: read current XP+spent
        # SP (pawn), keystone bonus (controller's purchased keystones), compute
        # via admin-inventory.py (capped at maxCharXP=344440), write back.
        raw="${1:?usage: award-char-xp <fls_id|me|steam:<id>|name:<n>> <amount>}"
        amount="${2:?usage: award-char-xp <player> <amount>}"
        amt_digits="${amount#-}"
        case "$amt_digits" in
            ''|*[!0-9]*)
                echo "[admin-publish] ERROR award-char-xp: amount must be an integer, got '$amount'" >&2
                exit 2
                ;;
        esac
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR award-char-xp: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.fgl_entities dune.actor_fgl_entities dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        pawn=$(dune_pc_actor_id "$fls_id")
        if [ -z "$pawn" ]; then
            echo "[admin-publish] ERROR award-char-xp: no player-character actor for $fls_id" >&2
            exit 1
        fi
        ctrl=$(dune_controller_actor_id "$fls_id")   # may be empty -> 0 keystones
        # Current FLevel state (TotalXPEarned <TAB> non-starter SkillPointsSpent), via pawn.
        state=$(dune_psql_q --set=pawn="$pawn" -tAF $'\t' 2>/dev/null <<'SQL'
SELECT
  COALESCE((fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint, 0),
  COALESCE((SELECT SUM((v->>'SkillPointsSpent')::int)
            FROM jsonb_each(fe.components->'FLevelComponent'->1->'ModuleData') AS kv(k, v)
            WHERE k != format('(TagName="%s")',
                fe.components->'FLevelComponent'->1->'StarterSkillTreeTag'->>'TagName')), 0)
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
WHERE afe.actor_id = :'pawn'::bigint AND afe.slot_name = 'DuneCharacter'
SQL
)
        cur_xp=$(printf '%s' "$state" | cut -f1)
        spent_sp=$(printf '%s' "$state" | cut -f2)
        if [ -z "$cur_xp" ]; then
            echo "[admin-publish] ERROR award-char-xp: no FLevelComponent for $fls_id (DuneCharacter fgl entity not found)" >&2
            exit 1
        fi
        # Purchased keystone ids (controller) -> CSV for the SP bonus.
        ks_csv=""
        if [ -n "$ctrl" ]; then
            ks_csv=$(dune_psql_q --set=ctrl="$ctrl" -tA 2>/dev/null <<'SQL' | paste -sd, -
SELECT keystone_id FROM dune.purchased_specialization_keystones WHERE player_id = :'ctrl'::bigint
SQL
)
        fi
        # Compute the new values (pure, no DB) via the argv-only helper.
        out=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" award-char-xp \
              --current-xp "$cur_xp" --spent-sp "$spent_sp" --keystones "$ks_csv" --amount "$amount") || {
            echo "[admin-publish] ERROR award-char-xp: compute failed" >&2; exit 1; }
        new_xp=""; new_level=""; new_tsp=""; new_usp=""; new_intel=""
        while IFS='=' read -r k v; do
            case "$k" in
                new_xp) new_xp=$v ;;
                new_level) new_level=$v ;;
                new_total_sp) new_tsp=$v ;;
                new_unspent_sp) new_usp=$v ;;
                new_intel) new_intel=$v ;;
            esac
        done <<EOF2
$out
EOF2
        if [ -z "$new_xp" ] || [ -z "$new_tsp" ] || [ -z "$new_usp" ] || [ -z "$new_intel" ]; then
            echo "[admin-publish] ERROR award-char-xp: malformed compute output" >&2
            exit 1
        fi
        # Apply: FLevel XP+SP (pawn fgl entity) and intel (pawn actor properties),
        # then read the stored XP back (last line, after the UPDATE tags).
        applied_xp=$(dune_psql_q --set=pawn="$pawn" --set=xp="$new_xp" --set=tsp="$new_tsp" \
                     --set=usp="$new_usp" --set=intel="$new_intel" -tA 2>/dev/null <<'SQL' | tail -n1 | tr -d '\r\n'
UPDATE dune.fgl_entities SET components = jsonb_set(jsonb_set(jsonb_set(components,
    '{FLevelComponent,1,TotalXPEarned}',      to_jsonb(:'xp'::bigint)),
    '{FLevelComponent,1,TotalSkillPoints}',   to_jsonb(:'tsp'::bigint)),
    '{FLevelComponent,1,UnspentSkillPoints}', to_jsonb(:'usp'::bigint))
WHERE entity_id = (SELECT entity_id FROM dune.actor_fgl_entities
                   WHERE actor_id = :'pawn'::bigint AND slot_name = 'DuneCharacter');
UPDATE dune.actors SET properties = jsonb_set(properties,
    '{TechKnowledgePlayerComponent,m_TechKnowledgePoints}', to_jsonb(:'intel'::bigint))
WHERE id = :'pawn'::bigint AND properties ? 'TechKnowledgePlayerComponent';
SELECT (fe.components->'FLevelComponent'->1->>'TotalXPEarned')
FROM dune.fgl_entities fe JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
WHERE afe.actor_id = :'pawn'::bigint AND afe.slot_name = 'DuneCharacter';
SQL
)
        echo "[admin-publish] OK award-char-xp fls=$fls_id pawn=$pawn level=$new_level xp=$applied_xp total_sp=$new_tsp unspent_sp=$new_usp intel=$new_intel"
        echo "publish=db-write award-char-xp pawn=$pawn xp=$applied_xp level=$new_level"
        exit 0
        ;;
    tech-unlock)
        # Flip every DISCOVERED recipe in the pawn's TechKnowledgePlayerComponent
        # .m_TechKnowledge.m_TechKnowledgeData to Purchased (unlock-all) or
        # NotPurchased (lock-all). PAWN-keyed, offline-gated, reversible. Does NOT
        # add recipes the player never encountered (those aren't in the array), and
        # leaves m_TechKnowledgePoints untouched (admin unlock is free).
        raw="${1:?usage: tech-unlock <fls_id|me|steam:<id>|name:<n>> <unlock-all|lock-all>}"
        mode="${2:?usage: tech-unlock <player> <unlock-all|lock-all>}"
        case "$mode" in
            unlock-all) tk_state="Purchased";    tk_bnew="false" ;;
            lock-all)   tk_state="NotPurchased"; tk_bnew="true"  ;;
            *) echo "[admin-publish] ERROR tech-unlock: mode must be unlock-all|lock-all" >&2; exit 2 ;;
        esac
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR tech-unlock: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        pawn=$(dune_pc_actor_id "$fls_id")
        if [ -z "$pawn" ]; then
            echo "[admin-publish] ERROR tech-unlock: no player-character actor for $fls_id (never spawned?)" >&2
            exit 1
        fi
        out=$(dune_apply_tech_unlock "$pawn" "$tk_state" "$tk_bnew") || {
            echo "[admin-publish] ERROR tech-unlock: db write failed" >&2
            printf '%s\n' "$out" >&2
            exit 1
        }
        result=$(printf '%s' "$out" | tail -1 | tr -d '[:space:]')
        echo "publish=ok tech-unlock pawn=$pawn mode=$mode purchased/total=$result"
        exit 0
        ;;

    grant-keystones)
        # Grant all 205 specialization keystones (insert into purchased_
        # specialization_keystones on the CONTROLLER, ON CONFLICT DO NOTHING)
        # and re-derive FLevel skill points (level + 54 keystone bonus) on the
        # PAWN's fgl entity. Ported from dune-admin cmdGrantAllKeystones
        # (insertAllPurchasedKeystones + grantAllKeystoneTargets +
        # updateLevelComponentSkillPoints). XP/intel are unchanged.
        raw="${1:?usage: grant-keystones <fls_id|me|steam:<id>|name:<n>>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR grant-keystones: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.purchased_specialization_keystones dune.fgl_entities dune.actor_fgl_entities dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        pawn=$(dune_pc_actor_id "$fls_id")
        if [ -z "$pawn" ]; then
            echo "[admin-publish] ERROR grant-keystones: no player-character actor for $fls_id" >&2
            exit 1
        fi
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR grant-keystones: no player-controller actor for $fls_id" >&2
            exit 1
        fi
        # Current FLevel XP + non-starter spent SP (via pawn) — same read as award-char-xp.
        state=$(dune_psql_q --set=pawn="$pawn" -tAF $'\t' 2>/dev/null <<'SQL'
SELECT
  COALESCE((fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint, 0),
  COALESCE((SELECT SUM((v->>'SkillPointsSpent')::int)
            FROM jsonb_each(fe.components->'FLevelComponent'->1->'ModuleData') AS kv(k, v)
            WHERE k != format('(TagName="%s")',
                fe.components->'FLevelComponent'->1->'StarterSkillTreeTag'->>'TagName')), 0)
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
WHERE afe.actor_id = :'pawn'::bigint AND afe.slot_name = 'DuneCharacter'
SQL
)
        cur_xp=$(printf '%s' "$state" | cut -f1)
        spent_sp=$(printf '%s' "$state" | cut -f2)
        if [ -z "$cur_xp" ]; then
            echo "[admin-publish] ERROR grant-keystones: no FLevelComponent for $fls_id" >&2
            exit 1
        fi
        out=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" grant-keystones \
              --current-xp "$cur_xp" --spent-sp "$spent_sp") || {
            echo "[admin-publish] ERROR grant-keystones: compute failed" >&2; exit 1; }
        exp_total=""; exp_unspent=""
        while IFS='=' read -r k v; do
            case "$k" in
                expected_total_sp) exp_total=$v ;;
                expected_unspent_sp) exp_unspent=$v ;;
            esac
        done <<EOF2
$out
EOF2
        if [ -z "$exp_total" ] || [ -z "$exp_unspent" ]; then
            echo "[admin-publish] ERROR grant-keystones: malformed compute output" >&2
            exit 1
        fi
        # Insert all 205 keystones (controller) + set FLevel SP (pawn), read count back.
        count=$(dune_psql_q --set=ctrl="$ctrl" --set=pawn="$pawn" --set=tsp="$exp_total" --set=usp="$exp_unspent" -tA 2>/dev/null <<'SQL' | tail -n1 | tr -d '\r\n'
INSERT INTO dune.purchased_specialization_keystones (player_id, keystone_id)
SELECT :'ctrl'::bigint, generate_series(1, 205) ON CONFLICT DO NOTHING;
UPDATE dune.fgl_entities SET components = jsonb_set(jsonb_set(components,
    '{FLevelComponent,1,TotalSkillPoints}',   to_jsonb(:'tsp'::bigint)),
    '{FLevelComponent,1,UnspentSkillPoints}', to_jsonb(:'usp'::bigint))
WHERE entity_id = (SELECT entity_id FROM dune.actor_fgl_entities
                   WHERE actor_id = :'pawn'::bigint AND slot_name = 'DuneCharacter');
SELECT count(*) FROM dune.purchased_specialization_keystones WHERE player_id = :'ctrl'::bigint;
SQL
)
        echo "[admin-publish] OK grant-keystones fls=$fls_id controller=$ctrl pawn=$pawn keystones=$count total_sp=$exp_total unspent_sp=$exp_unspent"
        echo "publish=db-write grant-keystones controller=$ctrl keystones=$count"
        exit 0
        ;;
    give-item)
        # Grant items to a player. Ported from dune-admin handleGiveItem +
        # runGiveItem + applyGiveItemChanges (MIT). Stack/slot/volume planning
        # is pure math in admin-inventory.py; stack_max + per-item volume are
        # LEARNED from existing world items (dune-admin's MAX(stack_size) /
        # MAX(volume_override) fallback) because our catalogue carries no
        # stack_max/volume.
        #
        # Routing — our stack is STRICTER than dune-admin (we never DB-write a
        # live inventory, which the running server holds in memory):
        #   Online  + quality 0  -> RMQ AddItemToInventory (delegate to `give`)
        #   Online  + quality >0 -> refuse (log out first; DB write needs offline)
        #   Offline (any quality)-> INSERT into dune.items (this path), topping
        #                           up existing matching stacks first.
        raw="${1:?usage: give-item <player> <ItemTemplate> [qty=1] [quality=0]}"
        template="${2:?usage: give-item <player> <ItemTemplate> [qty=1] [quality=0]}"
        qty="${3:-1}"
        quality="${4:-0}"
        case "$qty" in ''|*[!0-9]*) echo "[admin-publish] ERROR give-item: qty must be a positive integer, got '$qty'" >&2; exit 2 ;; esac
        [ "$qty" -ge 1 ] || { echo "[admin-publish] ERROR give-item: qty must be >= 1" >&2; exit 2; }
        case "$quality" in ''|*[!0-9]*) echo "[admin-publish] ERROR give-item: quality must be a non-negative integer, got '$quality'" >&2; exit 2 ;; esac
        # Item template ids look like OrnithopterLightLauncher_6 / T6_Augment_Acuracy1:
        # letters, digits, underscore, dot only. Also bound via :'tmpl' in SQL.
        case "$template" in ''|*[!A-Za-z0-9_.]*) echo "[admin-publish] ERROR give-item: invalid item template '$template'" >&2; exit 2 ;; esac
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR give-item: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.items dune.inventories dune.actors || exit 3

        # ---- Route on live online status (mirrors assert_player_offline) ----
        status=$(dune_online_status "$fls_id")
        if [ -z "$status" ]; then
            echo "[admin-publish] ERROR give-item: no account for $fls_id (run 'admin players')" >&2
            exit 2
        fi
        if [ "$status" != "Offline" ]; then
            if [ "$quality" -eq 0 ]; then
                # Live player: hand off to the RMQ server-command path so the
                # running server adds the item to its in-memory inventory.
                exec bash "$BASE/scripts/admin-publish.sh" give "$fls_id" "$template" "$qty"
            fi
            echo "[admin-publish] ERROR give-item: $fls_id is $status; quality>0 grants need a DB write — have them log out first" >&2
            exit 1
        fi

        # ---- Offline DB INSERT path ----
        assert_player_offline "$fls_id" || exit $?
        pawn=$(dune_pc_actor_id "$fls_id")
        if [ -z "$pawn" ]; then
            echo "[admin-publish] ERROR give-item: no player-character actor for $fls_id (never spawned a character)" >&2
            exit 1
        fi

        # Target inventory: the player's backpack (inventory_type=0), falling
        # back to any inventory of the pawn. Returns "id|maxslots|maxvol".
        inv_row=$(dune_psql_q --set=actor="$pawn" -tAF'|' <<'SQL'
SELECT id, COALESCE(max_item_count, -1), COALESCE(max_item_volume, -1)
FROM dune.inventories WHERE actor_id = :'actor'::bigint AND inventory_type = 0
LIMIT 1
SQL
)
        if [ -z "$inv_row" ]; then
            inv_row=$(dune_psql_q --set=actor="$pawn" -tAF'|' <<'SQL'
SELECT id, COALESCE(max_item_count, -1), COALESCE(max_item_volume, -1)
FROM dune.inventories WHERE actor_id = :'actor'::bigint
LIMIT 1
SQL
)
        fi
        if [ -z "$inv_row" ]; then
            echo "[admin-publish] ERROR give-item: no inventory for pawn $pawn" >&2
            exit 1
        fi
        IFS='|' read -r inv_id max_slots max_volume <<EOF2
$inv_row
EOF2

        # Existing matching stacks (same template + quality) as "id:size,..." .
        stacks=$(dune_psql_q --set=inv="$inv_id" --set=tmpl="$template" --set=q="$quality" -tA <<'SQL'
SELECT COALESCE(string_agg(id || ':' || stack_size, ','), '')
FROM dune.items
WHERE inventory_id = :'inv'::bigint AND template_id = :'tmpl' AND quality_level = :'q'::bigint
SQL
)
        # Inventory usage: row count, max position, used volume (per-row override only).
        agg=$(dune_psql_q --set=inv="$inv_id" -tAF'|' <<'SQL'
SELECT COUNT(*),
       COALESCE(MAX(position_index), -1),
       COALESCE(SUM(CASE WHEN volume_override IS NOT NULL AND volume_override > 0
                         THEN volume_override * stack_size ELSE 0 END), 0)
FROM dune.items WHERE inventory_id = :'inv'::bigint
SQL
)
        IFS='|' read -r used_slots max_pos used_volume <<EOF2
$agg
EOF2

        # stack_max: largest existing stack of this template+quality, floored at 1.
        stack_max=$(dune_psql_q --set=tmpl="$template" --set=q="$quality" -tA <<'SQL'
SELECT GREATEST(COALESCE(MAX(stack_size), 0), 1)
FROM dune.items WHERE template_id = :'tmpl' AND quality_level = :'q'::bigint
SQL
)
        # per-item volume: largest stored override for this template, else 0.
        per_item_vol=$(dune_psql_q --set=tmpl="$template" -tA <<'SQL'
SELECT COALESCE(MAX(volume_override), 0)
FROM dune.items WHERE template_id = :'tmpl' AND volume_override IS NOT NULL
SQL
)
        [ -n "$stack_max" ] || stack_max=1
        [ -n "$per_item_vol" ] || per_item_vol=0
        [ -n "$used_slots" ] || used_slots=0
        [ -n "$max_pos" ] || max_pos=-1
        [ -n "$used_volume" ] || used_volume=0

        # Pure planner: emits UPDATE/NEW/SUMMARY lines, or a single ERROR line.
        plan=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" give-item \
               --qty "$qty" --stack-max "$stack_max" --template "$template" \
               --stacks "$stacks" --max-pos "$max_pos" \
               --max-slots "$max_slots" --used-slots "$used_slots" \
               --max-volume "$max_volume" --used-volume "$used_volume" \
               --per-item-vol "$per_item_vol") || {
            echo "[admin-publish] ERROR give-item: plan compute failed" >&2; exit 1; }

        if printf '%s\n' "$plan" | grep -q '^ERROR '; then
            msg=$(printf '%s\n' "$plan" | sed -n 's/^ERROR //p')
            echo "[admin-publish] ERROR give-item: $msg" >&2
            exit 4
        fi

        # Build the transaction from the plan. The planner emits ONLY integers
        # for stack ids / adds / sizes / positions (re-validated below); the
        # template, quality and inventory id are bound via :'var'.
        txn="BEGIN;"
        topped=0; created=0
        while read -r kind a b; do
            case "$kind" in
                UPDATE)
                    case "$a$b" in ''|*[!0-9]*) echo "[admin-publish] ERROR give-item: malformed UPDATE plan line" >&2; exit 1 ;; esac
                    txn+=$'\n'"UPDATE dune.items SET stack_size = stack_size + ${b}::bigint WHERE id = ${a}::bigint;"
                    topped=$((topped + 1))
                    ;;
                NEW)
                    case "$a$b" in ''|*[!0-9]*) echo "[admin-publish] ERROR give-item: malformed NEW plan line" >&2; exit 1 ;; esac
                    txn+=$'\n'"INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, quality_level, stats) VALUES (:'inv'::bigint, ${a}::bigint, ${b}::bigint, :'tmpl', :'q'::bigint, '{}'::jsonb);"
                    created=$((created + 1))
                    ;;
            esac
        done <<EOF2
$plan
EOF2
        txn+=$'\n'"COMMIT;"
        txn+=$'\n'"SELECT COALESCE(SUM(stack_size), 0) FROM dune.items WHERE inventory_id = :'inv'::bigint AND template_id = :'tmpl' AND quality_level = :'q'::bigint;"

        # Run the txn. ON_ERROR_STOP aborts before COMMIT on any failure, so the
        # whole grant rolls back atomically; -q suppresses command tags so only
        # the final read-back total reaches stdout.
        rc=0
        out=$(printf '%s\n' "$txn" | dune_psql_q -q -v ON_ERROR_STOP=1 \
              --set=inv="$inv_id" --set=tmpl="$template" --set=q="$quality" -tA 2>&1) || rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR give-item: transaction failed (rolled back)" >&2
            printf '%s\n' "$out" >&2
            exit 1
        fi
        total_now=$(printf '%s\n' "$out" | tail -n1 | tr -d '\r\n')
        echo "[admin-publish] OK give-item fls=$fls_id pawn=$pawn inv=$inv_id template=$template quality=$quality qty=$qty topped_up=$topped created=$created total_now=$total_now"
        echo "publish=db-write give-item pawn=$pawn template=$template qty=$qty topped_up=$topped created=$created"
        exit 0
        ;;
    faction-rep)
        # Adjust a player's Great-House reputation by a signed delta on the
        # player-CONTROLLER actor, then rebuild the FactionPlayerComponent.
        # m_FactionDataArray jsonb cache from the rep table (always both houses,
        # Atreides then Harkonnen — never clobbering the other). Ported from
        # dune-admin applyFactionRepDelta + syncFactionComponent/
        # buildFactionDataArray (MIT). Only the two Great Houses (1=Atreides,
        # 2=Harkonnen) have a tier/reputation system + the jsonb component;
        # this is a pure rep change (it does NOT call change_player_faction, so
        # an unaligned character's house alignment is left untouched).
        raw="${1:?usage: faction-rep <player> <atreides|harkonnen|1|2> <signed-amount>}"
        fac="${2:?usage: faction-rep <player> <atreides|harkonnen|1|2> <signed-amount>}"
        delta="${3:?usage: faction-rep <player> <atreides|harkonnen|1|2> <signed-amount>}"
        case "$(printf '%s' "$fac" | tr '[:upper:]' '[:lower:]')" in
            1|atreides)  fid=1; fname=Atreides ;;
            2|harkonnen) fid=2; fname=Harkonnen ;;
            *) echo "[admin-publish] ERROR faction-rep: faction must be atreides|harkonnen (1|2) — only the two Great Houses have reputation tiers" >&2; exit 2 ;;
        esac
        d_digits="${delta#-}"
        case "$d_digits" in
            ''|*[!0-9]*)
                echo "[admin-publish] ERROR faction-rep: amount must be an integer, got '$delta'" >&2
                exit 2
                ;;
        esac
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR faction-rep: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.player_faction_reputation dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR faction-rep: no player-controller actor for $fls_id" >&2
            exit 1
        fi
        # Current absolute rep (0 if no row yet) for this house.
        cur=$(dune_psql_q --set=ctrl="$ctrl" --set=fid="$fid" -tA 2>/dev/null <<'SQL' | tr -d '\r\n'
SELECT COALESCE(reputation_amount, 0) FROM dune.player_faction_reputation
WHERE actor_id = :'ctrl'::bigint AND faction_id = :'fid'::smallint
SQL
)
        [ -n "$cur" ] || cur=0
        # Compute the clamped new rep + tier (pure math).
        out=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" faction-rep \
              --current "$cur" --delta "$delta" --faction "$fid") || {
            echo "[admin-publish] ERROR faction-rep: compute failed" >&2; exit 1; }
        new_rep=""; tier=""; tier_name=""
        while IFS='=' read -r k v; do
            case "$k" in
                new_rep) new_rep=$v ;;
                tier) tier=$v ;;
                tier_name) tier_name=$v ;;
            esac
        done <<EOF2
$out
EOF2
        if [ -z "$new_rep" ]; then
            echo "[admin-publish] ERROR faction-rep: malformed compute output" >&2
            exit 1
        fi
        # Set the rep via the audited proc, then rebuild m_FactionDataArray from
        # the rep table (both Great Houses, Atreides first), all in one txn so
        # the in-game jsonb cache never diverges from the rep row. Read the
        # stored rep back as the last line. ON_ERROR_STOP rolls back atomically.
        rc=0
        applied=$(dune_apply_faction_rep "$ctrl" "$fid" "$new_rep") || rc=$?
        applied=$(printf '%s\n' "$applied" | tail -n1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR faction-rep: transaction failed (rolled back)" >&2
            printf '%s\n' "$applied" >&2
            exit 1
        fi
        echo "[admin-publish] OK faction-rep fls=$fls_id controller=$ctrl faction=$fname rep=$applied tier=$tier ($tier_name)"
        echo "publish=db-write faction-rep controller=$ctrl faction=$fname rep=$applied tier=$tier"
        exit 0
        ;;
    set-faction-tier)
        # Align a player to a Great House AND set their reputation tier directly
        # (0-20). Unlike faction-rep (a pure rep delta that leaves alignment
        # untouched), this first calls change_player_faction to UPSERT the house
        # alignment (so an unaligned character becomes aligned), then writes the
        # tier's reputation + rebuilds m_FactionDataArray on the player-CONTROLLER
        # actor via the shared helper. Ported from dune-admin cmdSetFactionTier
        # (MIT). Offline-gated like every character write.
        raw="${1:?usage: set-faction-tier <player> <atreides|harkonnen|1|2> <tier 0-20>}"
        fac="${2:?usage: set-faction-tier <player> <atreides|harkonnen|1|2> <tier 0-20>}"
        tier="${3:?usage: set-faction-tier <player> <atreides|harkonnen|1|2> <tier 0-20>}"
        case "$(printf '%s' "$fac" | tr '[:upper:]' '[:lower:]')" in
            1|atreides)  fid=1; fname=Atreides ;;
            2|harkonnen) fid=2; fname=Harkonnen ;;
            *) echo "[admin-publish] ERROR set-faction-tier: faction must be atreides|harkonnen (1|2) — only the two Great Houses have tiers" >&2; exit 2 ;;
        esac
        case "$tier" in
            ''|*[!0-9]*) echo "[admin-publish] ERROR set-faction-tier: tier must be an integer 0-20, got '$tier'" >&2; exit 2 ;;
        esac
        if [ "$tier" -gt 20 ]; then
            echo "[admin-publish] ERROR set-faction-tier: tier must be 0-20, got '$tier'" >&2; exit 2
        fi
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR set-faction-tier: needs a single player, not '*'" >&2; exit 2
        fi
        dune_require_tables dune.player_faction dune.player_faction_reputation dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR set-faction-tier: no player-controller actor for $fls_id" >&2; exit 1
        fi
        # tier -> target reputation (pure compute)
        out=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" faction-tier \
              --faction "$fid" --tier "$tier") || {
            echo "[admin-publish] ERROR set-faction-tier: compute failed" >&2; exit 1; }
        new_rep=""; tier_name=""
        while IFS='=' read -r k v; do
            case "$k" in rep) new_rep=$v ;; tier_name) tier_name=$v ;; esac
        done <<EOF2
$out
EOF2
        if [ -z "$new_rep" ]; then
            echo "[admin-publish] ERROR set-faction-tier: malformed compute output" >&2; exit 1
        fi
        # 1) Align to the house first — change_player_faction upserts the
        # player_faction row (neutral_faction_id=3 'None') + fires pg_notify.
        if ! dune_psql_q -q -v ON_ERROR_STOP=1 --set=ctrl="$ctrl" --set=fid="$fid" -tA >/dev/null 2>&1 <<'SQL'
SELECT dune.change_player_faction(:'ctrl'::bigint, :'fid'::smallint, 3::smallint, NOW()::timestamp);
SQL
        then
            echo "[admin-publish] ERROR set-faction-tier: change_player_faction failed (alignment unchanged)" >&2; exit 1
        fi
        # 2) Write the tier's reputation + rebuild the jsonb cache (shared helper).
        rc=0
        applied=$(dune_apply_faction_rep "$ctrl" "$fid" "$new_rep") || rc=$?
        applied=$(printf '%s\n' "$applied" | tail -n1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR set-faction-tier: rep transaction failed (rolled back; alignment was already set)" >&2
            printf '%s\n' "$applied" >&2
            exit 1
        fi
        echo "[admin-publish] OK set-faction-tier fls=$fls_id controller=$ctrl faction=$fname tier=$tier ($tier_name) rep=$applied"
        echo "publish=db-write set-faction-tier controller=$ctrl faction=$fname tier=$tier rep=$applied"
        exit 0
        ;;
    progression-unlock|progression-lock)
        # Complete (unlock) or reset (lock) a journey-progression PRESET for a
        # player via the game's OWN procs. Each preset names root story-nodes;
        # we gather each root + every descendant (story_node_id LIKE 'root.%')
        # that exists for the account, then call the proc with the full id array
        # (the procs act on exact ids only — verified no child expansion).
        # Offline-gated; takes effect on the player's next login. Ported from
        # dune-admin progression presets (MIT); LOCK has no dune-admin analogue.
        mode="$cmd"
        raw="${1:?usage: $cmd <player> <preset_id>}"
        preset="${2:?usage: $cmd <player> <preset_id>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR $mode: needs a single player, not '*'" >&2; exit 2
        fi
        dune_require_tables dune.journey_story_node dune.encrypted_accounts || exit 3
        assert_player_offline "$fls_id" || exit $?
        # preset id -> root-node pg array (pure compute; validates the preset id).
        out=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" progression-preset --id "$preset" 2>&1) || {
            echo "[admin-publish] ERROR $mode: $out" >&2; exit 2; }
        roots=""; pname=""; ncount=""
        while IFS='=' read -r k v; do
            case "$k" in roots_array) roots=$v ;; name) pname=$v ;; node_count) ncount=$v ;; esac
        done <<EOF2
$out
EOF2
        [ -n "$roots" ] || { echo "[admin-publish] ERROR $mode: empty root set for preset $preset" >&2; exit 1; }
        if [ "$mode" = "progression-unlock" ]; then
            proc="complete_journey_story_nodes_for_player"; verb="unlocked"
        else
            proc="reset_journey_story_nodes_for_player"; verb="locked"
        fi
        # Gather root+descendants for THIS account, call the proc with the full
        # id array, and return how many ids were affected (last line). The proc
        # node count. Single statement (atomic under ON_ERROR_STOP): the derived
        # table g aggregates the gathered ids + count in one row, the proc is
        # called once on g.ids, and we emit g.n as field 1 (proc's void result is
        # field 2). One statement avoids the cross-statement CTE-scope/auto-commit
        # trap (a multi-statement version would commit the proc before a 2nd query).
        rc=0
        affected=$(dune_psql_q -q -v ON_ERROR_STOP=1 --set=fls="$fls_id" --set=roots="$roots" -tA 2>&1 <<SQL
SELECT g.n, dune.${proc}(:'fls', g.ids)
FROM (
    SELECT COALESCE(array_agg(js.story_node_id), '{}'::text[]) AS ids,
           count(*)::int AS n
    FROM dune.journey_story_node js
    JOIN dune.encrypted_accounts a ON a.id = js.account_id
    JOIN (SELECT unnest(:'roots'::text[]) AS root) rootset
      ON (js.story_node_id = rootset.root OR js.story_node_id LIKE rootset.root || '.%')
    WHERE a."user" = :'fls'
) g;
SQL
) || rc=$?
        affected=$(printf '%s\n' "$affected" | tail -n1 | cut -d'|' -f1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR $mode: journey proc failed (no changes)" >&2
            printf '%s\n' "$affected" >&2
            exit 1
        fi
        echo "[admin-publish] OK $mode fls=$fls_id preset=$preset ($pname) $verb nodes=$affected (~$ncount catalogued) — takes effect on next login"
        echo "publish=db-write $mode preset=$preset nodes=$affected"
        exit 0
        ;;
    player-summary)
        # Read-only roll-up for the Player Editor "current state" panel: solaris
        # balance, character XP, faction alignment + per-house reputation, and
        # per-preset journey completion. Emits KV lines (admin-http enriches with
        # level / xp-to-next / tier names via admin_progression). Controller-keyed
        # for solaris+faction; pawn-keyed for the FLevel XP.
        raw="${1:?usage: player-summary <player>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR player-summary: needs a single player, not '*'" >&2; exit 2
        fi
        dune_require_tables dune.actors dune.encrypted_accounts || exit 3
        ctrl=$(dune_controller_actor_id "$fls_id")
        pawn=$(dune_pc_actor_id "$fls_id")
        if [ -z "$ctrl" ] || [ -z "$pawn" ]; then
            echo "[admin-publish] ERROR player-summary: no player actors for $fls_id" >&2; exit 1
        fi
        # All scalars in one NULL-safe query (CSV: header + one data row).
        scal=$(dune_psql_q --csv --set=ctrl="$ctrl" --set=pawn="$pawn" <<'SQL'
SELECT
  (SELECT COALESCE(balance, 0) FROM dune.player_virtual_currency_balances
     WHERE player_controller_id = :'ctrl'::bigint AND currency_id = 0)                        AS solaris,
  COALESCE((SELECT (fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint
     FROM dune.fgl_entities fe JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
     WHERE afe.slot_name = 'DuneCharacter' AND afe.actor_id = :'pawn'::bigint), 0)            AS char_xp,
  COALESCE((SELECT faction_id FROM dune.player_faction WHERE actor_id = :'ctrl'::bigint), 0)  AS align,
  COALESCE((SELECT reputation_amount FROM dune.player_faction_reputation
     WHERE actor_id = :'ctrl'::bigint AND faction_id = 1), 0)                                 AS rep_1,
  COALESCE((SELECT reputation_amount FROM dune.player_faction_reputation
     WHERE actor_id = :'ctrl'::bigint AND faction_id = 2), 0)                                 AS rep_2
SQL
)
        IFS=',' read -r s_solaris s_xp s_align s_rep1 s_rep2 <<EOF2
$(printf '%s\n' "$scal" | tail -n1)
EOF2
        echo "solaris=${s_solaris:-0}"
        echo "char_xp=${s_xp:-0}"
        echo "align=${s_align:-0}"
        echo "rep_1=${s_rep1:-0}"
        echo "rep_2=${s_rep2:-0}"
        # Per-preset journey completion (one query via the all-roots arrays).
        if rootsout=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin-inventory.py" progression-all-roots 2>/dev/null); then
            pids=""; roots=""
            while IFS='=' read -r k v; do case "$k" in pids) pids=$v ;; roots) roots=$v ;; esac; done <<EOF3
$rootsout
EOF3
            if [ -n "$pids" ] && dune_require_tables dune.journey_story_node >/dev/null 2>&1; then
                jrows=$(dune_psql_q --csv --set=fls="$fls_id" --set=pids="$pids" --set=roots="$roots" <<'SQL'
WITH pr AS (SELECT * FROM unnest(:'pids'::text[], :'roots'::text[]) AS u(preset_id, root)),
acct AS (SELECT id FROM dune.encrypted_accounts WHERE "user" = :'fls'),
node AS (
  SELECT pr.preset_id, (js.complete_condition_state = 'true'::jsonb) AS done
  FROM pr
  JOIN dune.journey_story_node js
    ON js.account_id = (SELECT id FROM acct)
   AND (js.story_node_id = pr.root OR js.story_node_id LIKE pr.root || '.%')
)
SELECT preset_id, count(*) FILTER (WHERE done) AS complete, count(*) AS total
FROM node GROUP BY preset_id
SQL
)
                jline=$(printf '%s\n' "$jrows" | tail -n +2 | awk -F',' 'NF>=3 && $1!="" {printf "%s%s:%s:%s", sep, $1, $2, $3; sep=","}')
                echo "journey=$jline"
            fi
        fi
        echo "publish=ok player-summary"
        exit 0
        ;;
    remove-faction)
        # Remove a player from their Great House entirely (inverse of
        # set-faction-tier): set alignment to None via change_player_faction,
        # delete both houses' reputation rows, and clear the m_FactionDataArray
        # jsonb cache. Offline-gated, controller-keyed. One atomic transaction.
        raw="${1:?usage: remove-faction <player>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR remove-faction: needs a single player, not '*'" >&2; exit 2
        fi
        dune_require_tables dune.player_faction dune.player_faction_reputation dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR remove-faction: no player-controller actor for $fls_id" >&2; exit 1
        fi
        rc=0
        out=$(dune_psql_q -q -v ON_ERROR_STOP=1 --set=ctrl="$ctrl" -tA 2>&1 <<'SQL'
BEGIN;
SELECT dune.change_player_faction(:'ctrl'::bigint, 3::smallint, 3::smallint, NOW()::timestamp);
DELETE FROM dune.player_faction_reputation WHERE actor_id = :'ctrl'::bigint;
UPDATE dune.actors SET properties = jsonb_set(
    properties, '{FactionPlayerComponent,m_FactionDataArray}', '[]'::jsonb, true)
WHERE id = :'ctrl'::bigint;
COMMIT;
SELECT COALESCE((SELECT faction_id::text FROM dune.player_faction WHERE actor_id = :'ctrl'::bigint), 'none');
SQL
) || rc=$?
        align_after=$(printf '%s\n' "$out" | tail -n1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR remove-faction: transaction failed (rolled back)" >&2
            printf '%s\n' "$out" >&2; exit 1
        fi
        echo "[admin-publish] OK remove-faction fls=$fls_id controller=$ctrl alignment=$align_after (None)"
        echo "publish=db-write remove-faction controller=$ctrl alignment=$align_after"
        exit 0
        ;;
    spice-list)
        # Read-only spicefield economy snapshot: one row per (field_type, map,
        # dimension) with the spawn toggle + live active/primed vs caps. Server-
        # wide economy state, not per-player. CSV out; admin-http -> JSON.
        dune_require_tables dune.spicefield_types || exit 3
        dune_psql --csv -f - <<'SQL'
SELECT spicefield_type_id, field_type, map_name, dimension_index, is_spawning_active,
       current_globally_active, max_globally_active,
       current_globally_primed, max_globally_primed, global_spawn_weight
FROM dune.spicefield_types
ORDER BY map_name, dimension_index, field_type
SQL
        exit 0
        ;;
    spice-set)
        # Toggle spawning for one spicefield type via the game's own proc
        # (also resets requested_spawned_of_type so the spawner re-evaluates).
        # Server-wide economy lever, applies live (no offline gate).
        tid="${1:?usage: spice-set <spicefield_type_id> <on|off>}"
        state="${2:?usage: spice-set <spicefield_type_id> <on|off>}"
        case "$tid" in ''|*[!0-9]*) echo "[admin-publish] ERROR spice-set: type id must be an integer, got '$tid'" >&2; exit 2 ;; esac
        case "$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')" in
            on|1|true|yes)  active=true ;;
            off|0|false|no) active=false ;;
            *) echo "[admin-publish] ERROR spice-set: state must be on|off, got '$state'" >&2; exit 2 ;;
        esac
        dune_require_tables dune.spicefield_types || exit 3
        if ! dune_psql_q -q -v ON_ERROR_STOP=1 --set=active="$active" --set=tid="$tid" -tA >/dev/null 2>&1 <<'SQL'
SELECT dune.update_spice_field_spawn_state(:'active'::boolean, :'tid'::integer);
SQL
        then
            echo "[admin-publish] ERROR spice-set: update failed (type $tid)" >&2; exit 1
        fi
        echo "[admin-publish] OK spice-set type=$tid spawning=$active"
        echo "publish=db-write spice-set type=$tid spawning=$active"
        exit 0
        ;;
    spice-caps)
        # Adjust the global active/primed caps for one spicefield type (more spice
        # in circulation = bigger economy). Game proc update_global_spice_field_rules
        # takes (max_primed, max_active, type_id). Applies live.
        tid="${1:?usage: spice-caps <spicefield_type_id> <max_active> <max_primed>}"
        max_active="${2:?usage: spice-caps <spicefield_type_id> <max_active> <max_primed>}"
        max_primed="${3:?usage: spice-caps <spicefield_type_id> <max_active> <max_primed>}"
        for v in "$tid" "$max_active" "$max_primed"; do
            case "$v" in ''|*[!0-9]*) echo "[admin-publish] ERROR spice-caps: type id + caps must be non-negative integers, got '$v'" >&2; exit 2 ;; esac
        done
        dune_require_tables dune.spicefield_types || exit 3
        if ! dune_psql_q -q -v ON_ERROR_STOP=1 --set=tid="$tid" --set=a="$max_active" --set=p="$max_primed" -tA >/dev/null 2>&1 <<'SQL'
SELECT dune.update_global_spice_field_rules(:'p'::integer, :'a'::integer, :'tid'::integer);
SQL
        then
            echo "[admin-publish] ERROR spice-caps: update failed (type $tid)" >&2; exit 1
        fi
        echo "[admin-publish] OK spice-caps type=$tid max_active=$max_active max_primed=$max_primed"
        echo "publish=db-write spice-caps type=$tid max_active=$max_active max_primed=$max_primed"
        exit 0
        ;;
    market-bot-status)
        # Read-only: market-bot (Revy) NPC order count + total market orders.
        dune_require_tables dune.dune_exchange_orders || exit 3
        prov=$(dune_market_provision) || { echo "[admin-publish] ERROR market-bot-status: provision failed" >&2; exit 1; }
        IFS='|' read -r OWNER EXCH AP INV <<<"$prov"
        dune_psql_q --csv --set=o="$OWNER" <<'SQL'
SELECT
  (SELECT count(*) FROM dune.dune_exchange_orders WHERE owner_id = :'o'::bigint AND is_npc_order) AS bot_orders,
  (SELECT count(*) FROM dune.dune_exchange_orders WHERE is_npc_order) AS npc_orders,
  (SELECT count(*) FROM dune.dune_exchange_orders WHERE NOT is_npc_order) AS player_orders
SQL
        echo "market-bot owner=$OWNER exchange=$EXCH access_point=$AP inventory=$INV"
        echo "publish=ok market-bot-status"
        exit 0
        ;;
    market-bot-post)
        # Seed the exchange with NPC sell orders from the priced+masked catalog.
        # Each order = a real backing item in the exchange inventory + a
        # dune_exchange_orders (is_npc_order) row + a dune_exchange_sell_orders row
        # (one atomic batch). Server-wide economy write; applies live. Ported from
        # dune-admin marketbot (MIT) but using our pricing + category encoder.
        limit="${1:-50}"
        case "$limit" in ''|*[!0-9]*) echo "[admin-publish] ERROR market-bot-post: limit must be an integer, got '$limit'" >&2; exit 2 ;; esac
        dune_require_tables dune.dune_exchange_orders dune.dune_exchange_sell_orders dune.items dune.actors || exit 3
        prov=$(dune_market_provision) || { echo "[admin-publish] ERROR market-bot-post: provision failed" >&2; exit 1; }
        IFS='|' read -r OWNER EXCH AP INV <<<"$prov"
        listings=$(DUNE_BASE_DIR="$BASE" python3 "$BASE/scripts/admin_market.py" listings --limit "$limit" "$BASE") || {
            echo "[admin-publish] ERROR market-bot-post: listings compute failed" >&2; exit 1; }
        if [ -z "$listings" ]; then echo "[admin-publish] ERROR market-bot-post: no listings to post" >&2; exit 1; fi
        # Build one atomic batch. Per item: chained data-modifying CTEs link the
        # backing item -> order -> sell order. position_index = MAX+1 (sees prior
        # inserts within the txn). expiration sentinel 999999999 = never-expire.
        batch="BEGIN;"
        n=0
        while IFS='|' read -r tmpl qty qual price mask depth; do
            [ -z "$tmpl" ] && continue
            esc=${tmpl//\'/\'\'}
            batch+="
WITH it AS (
  INSERT INTO dune.items (inventory_id,stack_size,position_index,template_id,quality_level,stats)
  VALUES ($INV,$qty,(SELECT COALESCE(MAX(position_index),-1)+1 FROM dune.items WHERE inventory_id=$INV),'$esc',$qual,'{}')
  RETURNING id),
ord AS (
  INSERT INTO dune.dune_exchange_orders
    (exchange_id,access_point_id,owner_id,is_npc_order,expiration_time,template_id,
     durability_cur,durability_max,category_mask,category_depth,item_price,quality_level,item_id)
  SELECT $EXCH,$AP,$OWNER,TRUE,999999999,'$esc',1.0,1.0,$mask,$depth,$price,$qual,it.id FROM it
  RETURNING id)
INSERT INTO dune.dune_exchange_sell_orders (order_id,initial_stack_size,wear_normalized_price)
SELECT ord.id,$qty,$price FROM ord;"
            n=$((n+1))
        done <<EOF2
$listings
EOF2
        batch+="
COMMIT;"
        if ! printf '%s' "$batch" | dune_psql_q -q -v ON_ERROR_STOP=1 >/dev/null 2>&1; then
            echo "[admin-publish] ERROR market-bot-post: batch insert failed (rolled back)" >&2; exit 1
        fi
        total=$(dune_psql_q -tA -q --set=o="$OWNER" 2>/dev/null <<'SQL' | head -1
SELECT count(*) FROM dune.dune_exchange_orders WHERE owner_id = :'o'::bigint AND is_npc_order
SQL
)
        echo "[admin-publish] OK market-bot-post posted=$n exchange=$EXCH bot_orders_now=$total"
        echo "publish=db-write market-bot-post posted=$n bot_orders=$total"
        exit 0
        ;;
    market-bot-clear)
        # Remove ALL of the market-bot's NPC orders + their backing items (sell
        # orders -> orders -> items, FK-safe). The "off switch" / reset.
        dune_require_tables dune.dune_exchange_orders dune.dune_exchange_sell_orders dune.items dune.actors || exit 3
        prov=$(dune_market_provision) || { echo "[admin-publish] ERROR market-bot-clear: provision failed" >&2; exit 1; }
        IFS='|' read -r OWNER EXCH AP INV <<<"$prov"
        rc=0
        out=$(dune_psql_q -q -v ON_ERROR_STOP=1 --set=o="$OWNER" -tA 2>&1 <<'SQL'
BEGIN;
CREATE TEMP TABLE _bot_items ON COMMIT DROP AS
  SELECT item_id FROM dune.dune_exchange_orders
  WHERE owner_id = :'o'::bigint AND is_npc_order AND item_id IS NOT NULL;
DELETE FROM dune.dune_exchange_sell_orders
  WHERE order_id IN (SELECT id FROM dune.dune_exchange_orders WHERE owner_id = :'o'::bigint AND is_npc_order);
DELETE FROM dune.dune_exchange_orders WHERE owner_id = :'o'::bigint AND is_npc_order;
DELETE FROM dune.items WHERE id IN (SELECT item_id FROM _bot_items);
COMMIT;
SELECT count(*) FROM dune.dune_exchange_orders WHERE owner_id = :'o'::bigint AND is_npc_order;
SQL
) || rc=$?
        remaining=$(printf '%s\n' "$out" | tail -n1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR market-bot-clear: transaction failed (rolled back)" >&2
            printf '%s\n' "$out" >&2; exit 1
        fi
        echo "[admin-publish] OK market-bot-clear owner=$OWNER bot_orders_remaining=$remaining"
        echo "publish=db-write market-bot-clear remaining=$remaining"
        exit 0
        ;;
    market-bot-buy-list)
        # Read-only: PLAYER sell orders (is_npc_order FALSE) on the bot's exchange
        # that the bot may buy to inject demand. Emits pipe lines:
        #   order_id|template|price|item_id|seller|stack|grade
        # The python buy loop (admin_market.py) applies the price gate + d-die
        # gamble and then calls market-bot-buy per chosen order. Excludes the
        # bot's own listings structurally (they are is_npc_order TRUE).
        limit="${1:-50}"
        case "$limit" in ''|*[!0-9]*) echo "[admin-publish] ERROR market-bot-buy-list: limit must be an integer, got '$limit'" >&2; exit 2 ;; esac
        dune_require_tables dune.dune_exchange_orders dune.dune_exchange_sell_orders || exit 3
        prov=$(dune_market_provision) || { echo "[admin-publish] ERROR market-bot-buy-list: provision failed" >&2; exit 1; }
        IFS='|' read -r OWNER EXCH AP INV <<<"$prov"
        dune_psql_q -tA -q --set=e="$EXCH" --set=lim="$limit" <<'SQL'
SELECT o.id || '|' || o.template_id || '|' || o.item_price || '|' ||
       COALESCE(o.item_id,0) || '|' || o.owner_id || '|' ||
       COALESCE(i.stack_size, s.initial_stack_size, 1) || '|' ||
       COALESCE(o.quality_level,0)
FROM dune.dune_exchange_orders o
JOIN dune.dune_exchange_sell_orders s ON s.order_id = o.id
LEFT JOIN dune.items i ON i.id = o.item_id
WHERE o.is_npc_order = FALSE AND o.exchange_id = :'e'::bigint
LIMIT :'lim'::int
SQL
        echo "publish=ok market-bot-buy-list"
        exit 0
        ;;
    market-bot-buy)
        # Buy ONE player sell order (is_npc_order FALSE) as the market bot, to
        # inject demand. Faithful port of dune-admin buyPlayerListings (MIT): the
        # bot pays the SELLER (a completion_type=4 'Take Solari' log order owned by
        # the seller), debits its own exchange-user balance, then removes the
        # player's sell order + order + backing item. The purchased item is
        # destroyed — the bot is a pure demand sink and keeps no inventory. One
        # atomic BEGIN/COMMIT tx; ON_ERROR_STOP rolls back on any failure (e.g. the
        # order vanished between list and buy). Refuses NPC orders (the bot's own).
        oid="${1:-}"
        case "$oid" in ''|*[!0-9]*) echo "[admin-publish] ERROR market-bot-buy: order_id must be an integer, got '$oid'" >&2; exit 2 ;; esac
        dune_require_tables dune.dune_exchange_orders dune.dune_exchange_sell_orders dune.dune_exchange_fulfilled_orders dune.dune_exchange_users dune.items dune.actors || exit 3
        prov=$(dune_market_provision) || { echo "[admin-publish] ERROR market-bot-buy: provision failed" >&2; exit 1; }
        IFS='|' read -r OWNER EXCH AP INV <<<"$prov"
        rc=0
        out=$(dune_psql_q -q -v ON_ERROR_STOP=1 --set=o="$OWNER" --set=ord="$oid" -tA 2>&1 <<'SQL'
BEGIN;
-- Bot balance floor: top up to 9e12 when below 1e12 (faithful initBotUser seed)
-- so a buy never drives the synthetic bot balance negative.
UPDATE dune.dune_exchange_users
   SET solari_balance = COALESCE(solari_balance,0) + 9000000000000
 WHERE owner_id = :'o'::bigint AND COALESCE(solari_balance,0) < 1000000000000;
-- Snapshot the player order being bought (must still exist + be a player order).
-- One row required; if it vanished, the following statements error -> rollback.
SELECT o.id AS oid, o.exchange_id AS exch, o.access_point_id AS ap,
       o.owner_id AS seller, o.template_id AS tmpl,
       COALESCE(o.item_id,0) AS item, o.item_price AS price,
       COALESCE(i.stack_size, s.initial_stack_size, 1) AS stack
FROM dune.dune_exchange_orders o
JOIN dune.dune_exchange_sell_orders s ON s.order_id = o.id
LEFT JOIN dune.items i ON i.id = o.item_id
WHERE o.id = :'ord'::bigint AND o.is_npc_order = FALSE
\gset
-- Seller payment-log order (completion_type 4 = sale fulfilled; owner=seller so
-- the seller sees the claimed-solaris toast). item_price = total sale value.
INSERT INTO dune.dune_exchange_orders
  (exchange_id,access_point_id,owner_id,template_id,expiration_time,
   durability_cur,durability_max,item_price,category_mask,category_depth,is_npc_order)
VALUES (:exch,:ap,:seller,:'tmpl',999999999,1.0,1.0,:price*:stack,0,0,FALSE)
RETURNING id AS logid
\gset
INSERT INTO dune.dune_exchange_fulfilled_orders
  (order_id,source_order_id,completion_type,stack_size,original_order_id)
VALUES (:logid,NULL,4,:stack,:oid);
UPDATE dune.dune_exchange_users
   SET solari_balance = COALESCE(solari_balance,0) - (:price*:stack)
 WHERE owner_id = :'o'::bigint;
DELETE FROM dune.dune_exchange_sell_orders WHERE order_id = :oid;
DELETE FROM dune.dune_exchange_orders WHERE id = :oid;
DELETE FROM dune.items WHERE id = :item AND :item <> 0;
COMMIT;
SELECT :oid || '|' || :'tmpl' || '|' || (:price*:stack);
SQL
) || rc=$?
        info=$(printf '%s\n' "$out" | tail -n1 | tr -d '\r\n')
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR market-bot-buy: transaction failed (rolled back)" >&2
            printf '%s\n' "$out" >&2; exit 1
        fi
        echo "[admin-publish] OK market-bot-buy order=$oid result=$info"
        echo "publish=db-write market-bot-buy order=$oid"
        exit 0
        ;;
    svc-restart)
        # Restart ONE supervised background service without bouncing the container.
        # console.sh's watchdog is stateless — it re-derives liveness from the
        # pidfile every 5s and never respawns — so the safe recipe is: recover the
        # service's own launch env, kill it (TERM, 10s grace, KILL — mirroring
        # console.sh's per-service shutdown), then re-launch via its start-<svc>.sh.
        # Restartable set is an allowlist (subset of SERVICES): NO postgres/mq-*
        # (dependents cascade) and NO ue5-* (slow start + the 90s all-UE5-dead
        # grace can recycle the container). Mirror the allowlist in admin_logs.py.
        svc="${1:-}"
        case "$svc" in
            admin-http|scheduler|welcome-scanner|market-bot|mock-k8s|director|gateway|text-router|fls-stub) ;;
            *) echo "[admin-publish] ERROR svc-restart: refusing to restart '$svc' (not in the restartable allowlist)" >&2; exit 2 ;;
        esac
        start="$BASE/scripts/start-$svc.sh"
        [ -r "$start" ] || { echo "[admin-publish] ERROR svc-restart: $start missing" >&2; exit 1; }
        pidf="$BASE/runtime/pids/$svc.pid"
        pid=""; [ -r "$pidf" ] && pid="$(tr -dc '0-9' < "$pidf" 2>/dev/null)"
        # Pick the env source: the LIVE service's own /proc/<pid>/environ is the
        # only complete source (egg vars + prestart-generated secrets like the
        # session secret are NOT all present in PID 1 / console.sh). Fall back to
        # console.sh only if the service is already down. Copy it to a temp file
        # BEFORE the kill (bash vars can't hold the NUL-separated environ).
        envtmp="$BASE/runtime/.svc-restart-env.$$"
        : > "$envtmp" 2>/dev/null || envtmp=""
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ -r "/proc/$pid/environ" ]; then
            [ -n "$envtmp" ] && cat "/proc/$pid/environ" > "$envtmp" 2>/dev/null
        else
            csh="$(pgrep -f 'scripts/console.sh' 2>/dev/null | head -1)"
            [ -n "$csh" ] && [ -r "/proc/$csh/environ" ] && [ -n "$envtmp" ] && cat "/proc/$csh/environ" > "$envtmp" 2>/dev/null
        fi
        # Kill the running instance (leader pid; matches console.sh shutdown).
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
            kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
        fi
        # Re-launch detached in a CLEAN subshell that restores the recovered env,
        # so killing admin-http (our own caller) can't abort the relaunch.
        # start-<svc>.sh -> launch_bg rewrites the pidfile the watchdog reads.
        (
            if [ -n "$envtmp" ] && [ -s "$envtmp" ]; then
                while IFS= read -r -d '' kv; do
                    case "${kv%%=*}" in [A-Za-z_][A-Za-z0-9_]*) export "$kv" 2>/dev/null || true ;; esac
                done < "$envtmp"
            fi
            exec setsid bash "$start" "$BASE" >/dev/null 2>&1 </dev/null
        ) &
        sleep 3
        [ -n "$envtmp" ] && rm -f "$envtmp" 2>/dev/null || true
        newpid=""; [ -r "$pidf" ] && newpid="$(tr -dc '0-9' < "$pidf" 2>/dev/null)"
        if [ -n "$newpid" ] && kill -0 "$newpid" 2>/dev/null; then
            echo "[admin-publish] OK svc-restart $svc (pid $newpid)"
        else
            echo "[admin-publish] WARN svc-restart $svc relaunched; liveness not yet confirmed (it may still be starting)" >&2
        fi
        echo "publish=ok svc-restart $svc"
        exit 0
        ;;
    world-partition-list)
        # Read-only: the world_partition topology (warm dim=0 + dimensional dim>0
        # rows) joined to farm_state liveness. Powers the Instances tab. Emits pipe
        # lines: partition_id|map|dimension|label|blocked|server_id|game_port|ready|alive|players
        dune_require_tables dune.world_partition || exit 3
        dune_psql_q -tA -q <<'SQL'
SELECT wp.partition_id || '|' || wp.map || '|' || wp.dimension_index || '|' ||
       COALESCE(wp.label,'') || '|' || wp.blocked || '|' ||
       COALESCE(wp.server_id,'') || '|' ||
       COALESCE(fs.game_port::text,'') || '|' ||
       COALESCE(fs.ready::text,'') || '|' ||
       COALESCE(fs.alive::text,'') || '|' ||
       COALESCE(fs.connected_players::text,'')
FROM dune.world_partition wp
LEFT JOIN dune.farm_state fs ON fs.server_id = wp.server_id
ORDER BY wp.map, wp.dimension_index, wp.partition_id
SQL
        echo "publish=ok world-partition-list"
        exit 0
        ;;
    dimension-down)
        # Take ONE dimensional UE5 partition offline (REVERSIBLE): kill its process
        # group, NULL world_partition.server_id, delete its stale farm_state row.
        # Director stops routing next refresh tick (server_id NULL -> GetServerState
        # NotAvailable; the dim entry is never pruned -> no crash). The
        # world_partition row stays (server_id NULL) so dimension-up can respawn it.
        # Isolated: warm DD (#8) and the other dims have separate pidfiles/ports/rows.
        pid_arg="${1:-}"
        case "$pid_arg" in ''|*[!0-9]*) echo "[admin-publish] ERROR dimension-down: partition_id must be an integer, got '$pid_arg'" >&2; exit 2 ;; esac
        dune_require_tables dune.world_partition dune.farm_state || exit 3
        row=$(dune_psql_q -tA -q --set=p="$pid_arg" <<'SQL'
SELECT map || '|' || dimension_index || '|' || COALESCE(server_id,'')
FROM dune.world_partition WHERE partition_id = :'p'::bigint AND dimension_index > 0
SQL
)
        if [ -z "$row" ]; then echo "[admin-publish] ERROR dimension-down: $pid_arg is not a dimensional partition (dimension_index>0)" >&2; exit 1; fi
        IFS='|' read -r DMAP DDIM DSID <<<"$row"
        # Kill the dim's UE5 process group via its pidfile (setsid leader == PGID).
        pidf="$BASE/runtime/pids/ue5-${DMAP}-dim${DDIM}-p${pid_arg}.pid"
        DPORT=""
        if [ -r "$pidf" ]; then
            dpid="$(tr -dc '0-9' < "$pidf" 2>/dev/null)"
            if [ -n "$dpid" ] && kill -0 "$dpid" 2>/dev/null; then
                [ -r "/proc/$dpid/cmdline" ] && DPORT=$(tr '\0' '\n' < "/proc/$dpid/cmdline" 2>/dev/null | sed -n 's/^-Port=\([0-9]\{1,\}\)$/\1/p' | head -1)
                kill -TERM -- "-$dpid" 2>/dev/null || kill -TERM "$dpid" 2>/dev/null || true
                for _ in $(seq 1 10); do kill -0 "$dpid" 2>/dev/null || break; sleep 1; done
                kill -0 "$dpid" 2>/dev/null && { kill -KILL -- "-$dpid" 2>/dev/null || kill -KILL "$dpid" 2>/dev/null || true; }
            fi
            rm -f "$pidf"
        fi
        # NULL the writeback + drop the stale farm_state registration (by server_id AND
        # the dead UE5's port, so a NULL/mismatched server_id can't leave a heartbeat-
        # less orphan that crashes the Director's FLS reconcile). Atomic.
        rc=0
        dune_psql_q -q -v ON_ERROR_STOP=1 --set=p="$pid_arg" --set=sid="$DSID" --set=port="${DPORT:-0}" --set=m="$DMAP" >/dev/null 2>&1 <<'SQL' || rc=$?
BEGIN;
UPDATE dune.world_partition SET server_id = NULL WHERE partition_id = :'p'::bigint;
DELETE FROM dune.farm_state WHERE server_id = NULLIF(:'sid', '');
DELETE FROM dune.farm_state WHERE map = :'m' AND game_port = NULLIF(:'port', '0')::int;
COMMIT;
SQL
        if [ "$rc" -ne 0 ]; then echo "[admin-publish] ERROR dimension-down: DB update failed (rolled back)" >&2; exit 1; fi
        echo "[admin-publish] OK dimension-down partition=$pid_arg map=$DMAP dim=$DDIM"
        echo "publish=db-write dimension-down $pid_arg"
        exit 0
        ;;
    dimension-up)
        # Bring ONE downed dimensional partition back online: background
        # spawn-dimension.sh (canonical port -> spawn UE5 -> wait farm_state ->
        # writeback server_id). Returns immediately; the spawn takes minutes, so
        # poll /api/partitions until the dim shows live.
        pid_arg="${1:-}"
        case "$pid_arg" in ''|*[!0-9]*) echo "[admin-publish] ERROR dimension-up: partition_id must be an integer, got '$pid_arg'" >&2; exit 2 ;; esac
        dune_require_tables dune.world_partition || exit 3
        [ -r "$BASE/scripts/spawn-dimension.sh" ] || { echo "[admin-publish] ERROR dimension-up: spawn-dimension.sh missing" >&2; exit 1; }
        st=$(dune_psql_q -tA -q --set=p="$pid_arg" <<'SQL'
SELECT CASE WHEN dimension_index > 0 AND server_id IS NULL THEN 'ok'
            WHEN dimension_index > 0 THEN 'up'
            ELSE 'notdim' END
FROM dune.world_partition WHERE partition_id = :'p'::bigint
SQL
)
        case "${st:-missing}" in
            ok) ;;
            up) echo "[admin-publish] dimension-up: partition $pid_arg already online" >&2; echo "publish=ok dimension-up $pid_arg already-online"; exit 0 ;;
            notdim) echo "[admin-publish] ERROR dimension-up: $pid_arg is not a dimensional partition" >&2; exit 1 ;;
            *) echo "[admin-publish] ERROR dimension-up: partition $pid_arg not found" >&2; exit 1 ;;
        esac
        setsid bash "$BASE/scripts/spawn-dimension.sh" "$BASE" "$pid_arg" >> "$BASE/logs/spawn-dimension.log" 2>&1 </dev/null &
        echo "[admin-publish] OK dimension-up partition=$pid_arg spawning (poll /api/partitions; ~1-3 min to live)"
        echo "publish=ok dimension-up $pid_arg spawning"
        exit 0
        ;;
    sietch-add)
        # Add a player-choosable Survival_1 sietch: INSERT the next Survival_1
        # dimension-partition row (dim = max+1, id in the sietch range) + background
        # spawn its UE5. Survival_1 is Dimension mode (Funcom director_config), so
        # the new sietch becomes selectable (client TargetDimension) on the
        # Director's next cache refresh — no restart. dim 0 = stock Abbir sietch.
        # Optional label arg (default "Sietch <n>"). Shares the one DD/Arrakeen/Harko.
        label="${1:-}"
        dune_require_tables dune.world_partition || exit 3
        base="${DUNE_SURVIVAL_SIETCH_ID_BASE:-200}"
        case "$base" in ''|*[!0-9]*) base=200 ;; esac
        newrow=$(dune_psql_q -tA -q --set=b="$base" <<'SQL'
SELECT COALESCE(MAX(dimension_index),0)+1 || '|' ||
       (GREATEST(COALESCE(MAX(partition_id) FILTER (WHERE partition_id >= :'b'::bigint), :'b'::bigint - 1), :'b'::bigint - 1)+1)
FROM dune.world_partition WHERE map='Survival_1'
SQL
)
        IFS='|' read -r NEWDIM NEWPID <<<"$(printf '%s' "$newrow" | tr -d ' ')"
        case "${NEWDIM:-}" in ''|*[!0-9]*) echo "[admin-publish] ERROR sietch-add: could not compute next sietch dim" >&2; exit 1 ;; esac
        case "${NEWPID:-}" in ''|*[!0-9]*) echo "[admin-publish] ERROR sietch-add: could not compute next sietch id" >&2; exit 1 ;; esac
        [ -n "$label" ] || label="Sietch $((NEWDIM + 1))"
        rc=0
        dune_psql_q -q -v ON_ERROR_STOP=1 --set=pid="$NEWPID" --set=dim="$NEWDIM" --set=lbl="$label" >/dev/null 2>&1 <<'SQL' || rc=$?
INSERT INTO dune.world_partition (partition_id, map, partition_definition, dimension_index, blocked, label)
VALUES (:'pid'::bigint, 'Survival_1',
        '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb,
        :'dim'::int, false, :'lbl')
ON CONFLICT (partition_id) DO NOTHING;
SQL
        if [ "$rc" -ne 0 ]; then echo "[admin-publish] ERROR sietch-add: insert failed" >&2; exit 1; fi
        # Background the spawn EXACTLY like dimension-up: the `&` must bind to the
        # `setsid ...` (whose fds are redirected) so the backgrounded job does NOT
        # inherit admin-publish's stdout pipe. Do NOT use `[ -r ] && setsid &` —
        # that backgrounds the whole AND-list in a subshell that keeps the pipe
        # open, hanging the HTTP caller until run_publish's timeout.
        if [ -r "$BASE/scripts/spawn-dimension.sh" ]; then
            setsid bash "$BASE/scripts/spawn-dimension.sh" "$BASE" "$NEWPID" >> "$BASE/logs/spawn-dimension.log" 2>&1 </dev/null &
        fi
        echo "[admin-publish] OK sietch-add partition=$NEWPID dim=$NEWDIM label='$label' (spawning; poll /api/partitions)"
        echo "publish=db-write sietch-add $NEWPID"
        exit 0
        ;;
    sietch-remove)
        # Remove a player-choosable Survival_1 sietch entirely: tear its UE5 down,
        # ROBUSTLY drop its farm_state, DELETE the world_partition row, then resync the
        # Director so the sietch leaves the in-game browser. Refuses the stock Abbir
        # sietch (dimension_index 0) and non-Survival_1 partitions.
        #
        # farm_state is dropped by server_id AND by the live UE5's game port (captured
        # from /proc before the kill). A sietch whose world_partition.server_id was NULL
        # at removal (downed / post-reboot orphan) would otherwise leave a stale,
        # heartbeat-less farm_state row that CRASHES the Director's FLS reconcile
        # (PrepareServerHeartbeatUpdates -> "Could not find heartbeat for Server"),
        # which makes deleted sietches linger in the browser forever. The Director
        # never self-prunes a removed partition from its in-memory battlegroup, so we
        # svc-restart it to flush (use `repair-browser` for a global orphan sweep).
        pid_arg="${1:-}"
        case "$pid_arg" in ''|*[!0-9]*) echo "[admin-publish] ERROR sietch-remove: partition_id must be an integer, got '$pid_arg'" >&2; exit 2 ;; esac
        dune_require_tables dune.world_partition dune.farm_state || exit 3
        row=$(dune_psql_q -tA -q --set=p="$pid_arg" <<'SQL'
SELECT dimension_index || '|' || COALESCE(server_id,'')
FROM dune.world_partition WHERE partition_id = :'p'::bigint AND map='Survival_1' AND dimension_index > 0
SQL
)
        if [ -z "$row" ]; then echo "[admin-publish] ERROR sietch-remove: $pid_arg is not a removable Survival_1 sietch (need map=Survival_1, dimension_index>0; the Abbir base sietch cannot be removed)" >&2; exit 1; fi
        IFS='|' read -r SDIM SSID <<<"$row"
        pidf="$BASE/runtime/pids/ue5-Survival_1-dim${SDIM}-p${pid_arg}.pid"
        SPORT=""
        if [ -r "$pidf" ]; then
            dpid="$(tr -dc '0-9' < "$pidf" 2>/dev/null)"
            if [ -n "$dpid" ] && kill -0 "$dpid" 2>/dev/null; then
                [ -r "/proc/$dpid/cmdline" ] && SPORT=$(tr '\0' '\n' < "/proc/$dpid/cmdline" 2>/dev/null | sed -n 's/^-Port=\([0-9]\{1,\}\)$/\1/p' | head -1)
                kill -TERM -- "-$dpid" 2>/dev/null || kill -TERM "$dpid" 2>/dev/null || true
                for _ in $(seq 1 10); do kill -0 "$dpid" 2>/dev/null || break; sleep 1; done
                kill -0 "$dpid" 2>/dev/null && { kill -KILL -- "-$dpid" 2>/dev/null || kill -KILL "$dpid" 2>/dev/null || true; }
            fi
            rm -f "$pidf"
        fi
        rc=0
        dune_psql_q -q -v ON_ERROR_STOP=1 --set=p="$pid_arg" --set=sid="$SSID" --set=port="${SPORT:-0}" >/dev/null 2>&1 <<'SQL' || rc=$?
BEGIN;
DELETE FROM dune.farm_state WHERE server_id = NULLIF(:'sid', '');
DELETE FROM dune.farm_state WHERE map = 'Survival_1' AND game_port = NULLIF(:'port', '0')::int;
DELETE FROM dune.world_partition WHERE partition_id = :'p'::bigint AND map='Survival_1' AND dimension_index > 0;
COMMIT;
SQL
        if [ "$rc" -ne 0 ]; then echo "[admin-publish] ERROR sietch-remove: DB delete failed (rolled back)" >&2; exit 1; fi
        # Flush the Director's sticky in-memory battlegroup (it never self-prunes a
        # deleted partition) so the sietch disappears from the browser. Best-effort.
        bash "$BASE/scripts/admin-publish.sh" svc-restart director >/dev/null 2>&1 || true
        echo "[admin-publish] OK sietch-remove partition=$pid_arg dim=$SDIM (Director resynced)"
        echo "publish=db-write sietch-remove $pid_arg"
        exit 0
        ;;
    repair-browser)
        # Fix the in-game server browser when a removed sietch/dimension lingers.
        # (1) Sweep ALL orphan farm_state (server_id claimed by no world_partition row):
        # these stale, heartbeat-less servers crash the Director's FLS reconcile
        # (PrepareServerHeartbeatUpdates) and keep deleted sietches in the browser.
        # (2) svc-restart the Director to flush its sticky in-memory battlegroup so it
        # re-declares only the partitions that actually exist. Safe any time: live
        # players' maps keep their farm_state link (untouched); the only theoretical
        # risk is a sietch mid-spawn whose server_id isn't linked yet — re-run after.
        dune_require_tables dune.world_partition dune.farm_state || exit 3
        swept=$(dune_psql_q -tA -q <<'SQL'
WITH del AS (
  DELETE FROM dune.farm_state fs
   WHERE NOT EXISTS (SELECT 1 FROM dune.world_partition wp WHERE wp.server_id = fs.server_id)
   RETURNING 1)
SELECT count(*) FROM del
SQL
)
        swept=$(printf '%s' "$swept" | tr -dc '0-9'); swept="${swept:-0}"
        bash "$BASE/scripts/admin-publish.sh" svc-restart director >/dev/null 2>&1 || true
        echo "[admin-publish] OK repair-browser: swept $swept orphan farm_state row(s), Director resynced"
        echo "publish=ok repair-browser swept=$swept"
        exit 0
        ;;
    sietch-rename)
        # Rename a sietch: its world_partition.label = the browser display name
        # (start-ue5.sh feeds it to the per-instance UserEngine.ini on (re)spawn).
        # Survival_1 dim>0 only (never the Abbir base / other maps). Applies on
        # the sietch's next restart.
        pid_arg="${1:-}"; newlabel="${2:-}"
        case "$pid_arg" in ''|*[!0-9]*) echo "[admin-publish] ERROR sietch-rename: partition_id must be an integer" >&2; exit 2 ;; esac
        [ -n "$newlabel" ] || { echo "[admin-publish] ERROR sietch-rename: a non-empty label is required" >&2; exit 2; }
        case "$newlabel" in *'"'*) echo "[admin-publish] ERROR sietch-rename: label cannot contain a double quote" >&2; exit 2 ;; esac
        dune_require_tables dune.world_partition || exit 3
        n=$(dune_psql_q -tA -q --set=p="$pid_arg" --set=l="$newlabel" <<'SQL'
UPDATE dune.world_partition SET label = :'l'
WHERE partition_id = :'p'::bigint AND map='Survival_1' AND dimension_index > 0
RETURNING partition_id
SQL
)
        [ -n "$(printf '%s' "$n" | tr -d '[:space:]')" ] || { echo "[admin-publish] ERROR sietch-rename: $pid_arg is not a Survival_1 sietch (dimension_index>0)" >&2; exit 1; }
        echo "[admin-publish] OK sietch-rename partition=$pid_arg label='$newlabel'"
        echo "publish=db-write sietch-rename $pid_arg"
        exit 0
        ;;
    item-delete)
        # Hard-delete a single item stack by its dune.items.id via dune.delete_item.
        # Ported from dune-admin cmdDeleteItem (MIT). Hardened beyond dune-admin:
        # we resolve the OWNING character and require it offline (the running
        # server caches inventory in memory and would overwrite / ghost a live
        # edit), and we verify the item exists first (dune-admin silently
        # "succeeds" on a missing id). Hard delete — no DB backup; capture the
        # row first (inventory-list shows item_id) if you might want it back.
        item_id="${1:?usage: item-delete <item_id>}"
        case "$item_id" in
            ''|*[!0-9]*) echo "[admin-publish] ERROR item-delete: item_id must be a positive integer, got '$item_id'" >&2; exit 2 ;;
        esac
        [ "$item_id" -ge 1 ] || { echo "[admin-publish] ERROR item-delete: item_id must be >= 1" >&2; exit 2; }
        dune_require_tables dune.items dune.inventories dune.actors || exit 3
        # Resolve item -> template/stack + owning FLS (via inventory.actor_id ->
        # actor.owner_account_id). Empty result => item (or its inventory) absent.
        row=$(dune_psql_q --set=iid="$item_id" -tAF'|' <<'SQL'
SELECT i.template_id, i.stack_size, COALESCE(ea."user", '')
FROM dune.items i
JOIN dune.inventories inv ON inv.id = i.inventory_id
JOIN dune.actors ac ON ac.id = inv.actor_id
LEFT JOIN dune.encrypted_accounts ea ON ea.id = ac.owner_account_id
WHERE i.id = :'iid'::bigint
SQL
)
        if [ -z "$row" ]; then
            echo "[admin-publish] ERROR item-delete: no item with id $item_id (or it has no valid inventory)" >&2
            exit 1
        fi
        IFS='|' read -r it_template it_stack it_fls <<EOF2
$row
EOF2
        # Offline-gate the owning character when one is resolvable (skip for
        # container/vehicle inventories with no player owner).
        if [ -n "$it_fls" ]; then
            assert_player_offline "$it_fls" || exit $?
        fi
        rc=0
        dune_psql_q --set=iid="$item_id" -tA >/dev/null <<'SQL' || rc=$?
SELECT dune.delete_item(:'iid'::bigint)
SQL
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR item-delete: delete_item proc failed (rc=$rc)" >&2
            exit 1
        fi
        still=$(dune_psql_q --set=iid="$item_id" -tA <<'SQL' | tr -d '\r\n'
SELECT count(*) FROM dune.items WHERE id = :'iid'::bigint
SQL
)
        if [ "$still" != "0" ]; then
            echo "[admin-publish] ERROR item-delete: item $item_id still present after delete (count=$still)" >&2
            exit 1
        fi
        echo "[admin-publish] OK item-delete id=$item_id template=$it_template stack=$it_stack owner=${it_fls:-none}"
        echo "publish=db-write item-delete id=$item_id template=$it_template"
        exit 0
        ;;
    reset-spec)
        # Reset ALL specialization tracks + purchased keystones for a player via
        # dune.reset_specialization_tracks + dune.reset_specialization_keystones
        # (both keyed on the player-CONTROLLER actor). Ported from dune-admin
        # cmdResetSpecializations all-mode (MIT). Hardened: offline-gated
        # (dune-admin is not). Mirrors dune-admin in NOT recomputing the pawn
        # FLevel skill points — the game reconciles level/SP on next login.
        # DESTRUCTIVE: wipes spec XP + every keystone purchase.
        raw="${1:?usage: reset-spec <player>}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR reset-spec: needs a single player, not '*'" >&2
            exit 2
        fi
        dune_require_tables dune.specialization_tracks dune.purchased_specialization_keystones dune.actors || exit 3
        assert_player_offline "$fls_id" || exit $?
        ctrl=$(dune_controller_actor_id "$fls_id")
        if [ -z "$ctrl" ]; then
            echo "[admin-publish] ERROR reset-spec: no player-controller actor for $fls_id" >&2
            exit 1
        fi
        before=$(dune_psql_q --set=ctrl="$ctrl" -tAF'|' <<'SQL'
SELECT (SELECT count(*) FROM dune.specialization_tracks WHERE player_id = :'ctrl'::bigint),
       (SELECT count(*) FROM dune.purchased_specialization_keystones WHERE player_id = :'ctrl'::bigint)
SQL
)
        IFS='|' read -r tracks_before ks_before <<EOF2
$before
EOF2
        rc=0
        dune_psql_q --set=ctrl="$ctrl" -v ON_ERROR_STOP=1 -tA >/dev/null <<'SQL' || rc=$?
SELECT dune.reset_specialization_tracks(:'ctrl'::bigint);
SELECT dune.reset_specialization_keystones(:'ctrl'::bigint);
SQL
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR reset-spec: reset proc(s) failed (rc=$rc)" >&2
            exit 1
        fi
        after=$(dune_psql_q --set=ctrl="$ctrl" -tAF'|' <<'SQL'
SELECT (SELECT count(*) FROM dune.specialization_tracks WHERE player_id = :'ctrl'::bigint),
       (SELECT count(*) FROM dune.purchased_specialization_keystones WHERE player_id = :'ctrl'::bigint)
SQL
)
        IFS='|' read -r tracks_after ks_after <<EOF2
$after
EOF2
        # Confirm the reset actually cleared both tables — a proc that silently
        # no-ops (or a future schema change) must not be reported as "OK".
        if [ "${tracks_after:-x}" != "0" ] || [ "${ks_after:-x}" != "0" ]; then
            echo "[admin-publish] ERROR reset-spec: reset left rows behind (tracks=$tracks_after keystones=$ks_after) — proc may have silently failed" >&2
            exit 1
        fi
        echo "[admin-publish] OK reset-spec fls=$fls_id controller=$ctrl tracks=${tracks_before}->${tracks_after} keystones=${ks_before}->${ks_after}"
        echo "publish=db-write reset-spec controller=$ctrl tracks_cleared=$tracks_before keystones_cleared=$ks_before"
        exit 0
        ;;
    account-delete)
        # DELETE AN ENTIRE ACCOUNT/CHARACTER via dune.delete_account(fls, reason).
        # Ported from dune-admin cmdDeleteAccount (MIT). IRREVERSIBLE: cascades to
        # every per-character table (items, currency, faction, spec, journey,
        # buildings, vehicles, land claims, ...) and mutates shared guild/party/
        # ownership state. dune-admin applies NO server-side guard; we REQUIRE
        #   (1) a <confirm-fls> arg that exactly equals the resolved 16-hex FLS id, and
        #   (2) the character to be offline.
        # There is NO DB backup — export the character first if you might want it back.
        raw="${1:?usage: account-delete <player> <confirm-fls> [reason]}"
        confirm="${2:?usage: account-delete <player> <confirm-fls> [reason] — pass the exact 16-hex FLS id to confirm}"
        reason="${3:-admin delete via admin-publish.sh}"
        fls_id=$(resolve_player_id "$raw") || exit 1
        if [ "$fls_id" = "*" ]; then
            echo "[admin-publish] ERROR account-delete: needs a single player, not '*'" >&2
            exit 2
        fi
        # Confirmation: the caller must pass the exact resolved FLS id (case-
        # insensitive). Blocks accidental / fat-fingered invocation of an
        # irreversible wipe.
        confirm_uc=$(printf '%s' "$confirm" | tr '[:lower:]' '[:upper:]')
        if [ "$confirm_uc" != "$fls_id" ]; then
            echo "[admin-publish] ERROR account-delete: confirmation '$confirm' does not match the resolved FLS id ($fls_id) for '$raw' — refusing." >&2
            echo "                Pass the exact 16-hex FLS id as the 2nd argument to confirm the deletion." >&2
            exit 2
        fi
        dune_require_tables dune.accounts dune.player_state || exit 3
        assert_player_offline "$fls_id" || exit $?
        # dune.delete_account keys on dune.accounts."user" (the hex FLS id) and
        # resolves the 3 player actors via dune.player_state. Confirmed live that
        # accounts."user" == encrypted_accounts."user" on this build.
        rc=0
        # Capture psql's own exit (ON_ERROR_STOP surfaces a proc error as rc 3).
        # Do NOT pipe the heredoc — a pipe masks psql's exit behind tail's, so a
        # failed irreversible delete could slip past the rc check below.
        raw_out=$(dune_psql_q --set=fls="$fls_id" --set=reason="$reason" -v ON_ERROR_STOP=1 -tA <<'SQL'
SELECT dune.delete_account(:'fls'::text, :'reason'::text)
SQL
) || rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "[admin-publish] ERROR account-delete: delete_account proc failed (rc=$rc)" >&2
            exit 1
        fi
        result=$(printf '%s' "$raw_out" | tail -n1 | tr -d '\r\n')
        if [ "$result" != "t" ]; then
            echo "[admin-publish] WARN account-delete: proc returned '$result' (no matching player_state actors — nothing cascaded)" >&2
        fi
        echo "[admin-publish] OK account-delete fls=$fls_id found=$result reason=$reason"
        echo "publish=db-write account-delete fls=$fls_id found=$result"
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
