#!/bin/bash
# Originally © 2026 CubeCoders Limited (MIT). Modified for Pelican Wings.
# See ATTRIBUTION.md.
#
# prestart.sh BASE_DIR
#
# Run by pelican-entrypoint.sh as the first boot stage on every container
# start. Idempotent: safe to re-run. Responsibilities:
#
#   1. Decode the JWT, derive/persist WorldName (FLS-registered identity)
#   2. Generate RMQ secret + self-signed TLS cert (replaces cert-manager)
#   3. Render runtime config files (rabbitmq.conf, director.ini, postgres.conf)
#   4. Ensure postgres data dir exists & is initdb'd (first run only)
#   5. Boot postgres briefly to load the schema via ToolsDB resetdb if missing
#   6. Seed dune.world_partition for the two stock maps
#
# Required env vars (set by Pelican Wings from the egg variables):
#   DUNE_JWT              — Funcom IGW JWT (Required on the egg variable)
#   DUNE_WORLD_TITLE      — display name in server browser
#   DUNE_REGION           — "Europe Test" / "North America Test"
#   DUNE_BIND_IP          — what services bind to
#   DUNE_EXTERNAL_IP      — advertised to FLS
# Optional:
#   DUNE_WORLD_NAME_OVERRIDE — paste the Funcom-issued name to skip auto-gen
#
# Pelican vs AMP: AMP ran each PreStartStage as a separate bash invocation,
# so it materialised env vars to runtime/amp.env to bridge between stages.
# Under Pelican Wings, pelican-entrypoint.sh runs every stage in the same
# parent shell — children inherit the env naturally, no amp.env needed.

export SOURCE="prestart"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

mkdir -p "$RUNTIME"

# Guard: the runtime image must pre-create the K8s ServiceAccount mount
# (see docker/Dockerfile). pelican-entrypoint.sh already enforces this,
# but defending in depth is cheap.
SA_DIR="/var/run/secrets/kubernetes.io/serviceaccount"
if [ ! -d "$SA_DIR" ] || [ ! -w "$SA_DIR" ]; then
  die "K8s ServiceAccount mount $SA_DIR missing or not writable — runtime Docker image must pre-create it."
fi

log "Running pre-start setup..."
log "  Bind IP:      $DUNE_BIND_IP"
log "  External IP:  $DUNE_EXTERNAL_IP"
log "  World title:  ${DUNE_WORLD_TITLE:-<unset>}"
log "  Region:       ${DUNE_REGION:-<unset>}"

[ -n "${DUNE_JWT:-}" ] || die "Self-Host Service Token not set — paste your Funcom token into the AMP config"
[ -d "$EXTRACTED/postgres" ] || die "extracted prerequisites missing — run the Update step first"

# --------------------------------------------------------------------------
# 1. World identity
# --------------------------------------------------------------------------
log "Verifying world identity..."

# Decode JWT payload (base64url, no nested pipes — pipefail-safe)
PAYLOAD=$(awk -F. '{print $2}' <<< "$DUNE_JWT")
while [ $((${#PAYLOAD} % 4)) -ne 0 ]; do PAYLOAD="${PAYLOAD}="; done
PAYLOAD_STD=$(printf '%s' "$PAYLOAD" | tr '_-' '/+')
DECODED=$(printf '%s' "$PAYLOAD_STD" | base64 -d 2>/dev/null || true)
HOST_ID=$(printf '%s' "$DECODED" | jq -r '.HostId // empty' 2>/dev/null || true)
[ -n "$HOST_ID" ] || die "couldn't decode HostId from JWT — is the token valid?"
log "  HostId: $HOST_ID"

WN_FILE="$STATE/world-name"
FIRST_BIND=0
if [ -n "${DUNE_WORLD_NAME_OVERRIDE:-}" ]; then
  # Explicit override always wins. Use this to rebind to a previously
  # deployed BG name (e.g. after wiping state but wanting to keep FLS
  # identity) or to migrate from the legacy `-tczine` constant.
  printf '%s' "$DUNE_WORLD_NAME_OVERRIDE" > "$WN_FILE"
elif [ ! -f "$WN_FILE" ]; then
  # First install. Mirror Funcom's setup/world.sh:
  #     sh-<hostid>-<6 random lowercase letters>
  # The 6-letter suffix is arbitrary content-wise but must be unique per
  # battlegroup AND stable across boots — FLS keys characters/data by the
  # full WorldName, so changing the suffix after first registration looks
  # to FLS like a different battlegroup. We write the generated name to
  # state/world-name and never touch it again; the override above is the
  # only supported way to rebind.
  FIRST_BIND=1
  # 6 random lowercase letters. Pure bash, no pipeline: prestart.sh is
  # launched by AMP (a .NET process), and .NET sets SIGPIPE to SIG_IGN
  # process-wide; children inherit it. With SIGPIPE ignored the old
  # `tr -dc a-z </dev/urandom | fold | head` form span forever -- tr
  # was never killed when head closed the pipe. $RANDOM needs no pipe.
  _wn_alpha=abcdefghijklmnopqrstuvwxyz
  SUFFIX=
  for _ in 1 2 3 4 5 6; do SUFFIX+="${_wn_alpha:RANDOM%26:1}"; done
  WORLD_NAME="sh-$(echo "$HOST_ID" | tr 'A-Z' 'a-z')-$SUFFIX"
  echo "$WORLD_NAME" > "$WN_FILE"
fi
WORLD_NAME=$(cat "$WN_FILE")
export DUNE_WORLD_NAME="$WORLD_NAME"

if [ "$FIRST_BIND" = 1 ]; then
  log ""
  log "=========================================================================="
  log "FIRST-START FLS BINDING — RECORD THIS VALUE NOW"
  log ""
  log "  WorldName: $WORLD_NAME"
  log ""
  log "Funcom PERMANENTLY binds this name to your Self-Host Service Token on"
  log "first registration.  Save it somewhere durable (outside this server)."
  log "If you ever redeploy without a backup, paste it into the FLS World Name"
  log "(override) field in the AMP config to reclaim the same FLS slot."
  log "=========================================================================="
  log ""
else
  log "  WorldName: $WORLD_NAME"
fi

# RMQ HTTP-token auth secret
RMQ_SEC_FILE="$STATE/rmq-secret"
if [ ! -f "$RMQ_SEC_FILE" ]; then
  umask 077
  openssl rand -base64 64 | tr -d '\n' > "$RMQ_SEC_FILE"
fi
RMQ_SEC=$(cat "$RMQ_SEC_FILE")
export DUNE_RMQ_SEC="$RMQ_SEC"

# ServerCommandsAuthToken — the seabass server-command handler in each UE5
# instance validates inbound admin RMQ messages against this. start-ue5.sh
# feeds it to the UE5 servers via -ini:engine:[FuncomLiveServices*]:
# ServerCommandsAuthToken overrides; scripts/admin-publish.sh embeds the
# same value in the AMQP envelope so the dispatcher accepts the publish.
# Without this, admin-publish.sh succeeds (publish=ok at the broker) but
# UE5 silently drops every message.
SVC_CMD_FILE="$STATE/svc-cmd-token"
if [ ! -f "$SVC_CMD_FILE" ]; then
  umask 077
  openssl rand -hex 16 | tr -d '\n' > "$SVC_CMD_FILE"
fi
export DUNE_SVC_CMD_TOKEN="$(cat "$SVC_CMD_FILE")"

# Admin web UI secrets. Only relevant when DUNE_ADMIN_UI_ENABLED=1, but we
# generate the session secret unconditionally so the value is stable across
# enable/disable toggles (changing it would log everyone out).
ADMIN_UI_SESSION_FILE="$STATE/admin-ui-session-secret"
if [ ! -f "$ADMIN_UI_SESSION_FILE" ]; then
  umask 077
  openssl rand -hex 32 | tr -d '\n' > "$ADMIN_UI_SESSION_FILE"
fi
export DUNE_ADMIN_UI_SESSION_SECRET="$(cat "$ADMIN_UI_SESSION_FILE")"

# Password: only generate if UI is enabled AND operator left the env blank.
# Persist so the value survives restarts but operator-supplied values
# always take precedence on next boot.
if [ "${DUNE_ADMIN_UI_ENABLED:-0}" = "1" ]; then
  ADMIN_UI_PW_FILE="$STATE/admin-ui-password"
  if [ -n "${DUNE_ADMIN_UI_PASSWORD:-}" ]; then
    umask 077
    printf '%s' "$DUNE_ADMIN_UI_PASSWORD" > "$ADMIN_UI_PW_FILE"
    log "admin-ui: using operator-supplied DUNE_ADMIN_UI_PASSWORD"
  elif [ ! -f "$ADMIN_UI_PW_FILE" ]; then
    umask 077
    openssl rand -hex 12 | tr -d '\n' > "$ADMIN_UI_PW_FILE"
    GENERATED_PW=$(cat "$ADMIN_UI_PW_FILE")
    log "admin-ui generated password: $GENERATED_PW"
    log "admin-ui: copy this into DUNE_ADMIN_UI_PASSWORD to persist your choice;"
    log "admin-ui: otherwise this auto-generated value will be re-used until you change it."
    export DUNE_ADMIN_UI_PASSWORD="$GENERATED_PW"
  else
    export DUNE_ADMIN_UI_PASSWORD="$(cat "$ADMIN_UI_PW_FILE")"
    log "admin-ui: re-using persisted auto-generated password (see prior boot log for value)"
  fi
fi

# --------------------------------------------------------------------------
# 2. Self-signed RMQ TLS cert (replaces cert-manager from the k3s build)
# --------------------------------------------------------------------------
CERTDIR="$STATE/rmq-certs"
if [ ! -f "$CERTDIR/cert.pem" ] || [ ! -f "$CERTDIR/key.pem" ]; then
  log "Generating self-signed TLS certificate..."
  mkdir -p "$CERTDIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERTDIR/key.pem" -out "$CERTDIR/cert.pem" \
    -subj "/CN=$DUNE_EXTERNAL_IP" \
    -addext "subjectAltName=IP:$DUNE_EXTERNAL_IP,IP:$DUNE_BIND_IP,IP:127.0.0.1,DNS:localhost" 2>/dev/null
  cp "$CERTDIR/cert.pem" "$CERTDIR/cacert.pem"
  chmod 0644 "$CERTDIR"/*.pem
fi

# --------------------------------------------------------------------------
# 3. Render runtime config files
# --------------------------------------------------------------------------
log "Rendering runtime configuration..."

# RMQ system.conf files — envsubst expands $(VAR) inside.  We export the
# vars that the template references, then run envsubst.  Note: RabbitMQ
# itself ALSO does $(VAR) substitution at startup; we only do envsubst here
# for fields that should be baked rather than runtime-substituted.
mkdir -p "$RUNTIME/mq-admin" "$RUNTIME/mq-game"

# mq-admin: plain AMQP, auth backend = HTTP cache → text-router.
# All listeners bind to loopback only — mq-admin is purely internal.
cat > "$RUNTIME/mq-admin/rabbitmq.conf" <<EOF
auth_backends.1 = cache
auth_cache.cache_ttl = 5000
auth_cache.cached_backend = http
auth_http.http_method   = post
auth_http.user_path     = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/user
auth_http.vhost_path    = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/vhost
auth_http.resource_path = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/resource
auth_http.topic_path    = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/topic

# Internal-only HTTP listeners
management.tcp.ip   = 127.0.0.1
management.tcp.port = $DUNE_MQ_ADMIN_MGMT_PORT
prometheus.tcp.ip   = 127.0.0.1
prometheus.tcp.port = 15692
EOF

# mq-game: TLS AMQPS on the EXTERNALLY-REACHABLE bind IP
cat > "$RUNTIME/mq-game/rabbitmq.conf" <<EOF
auth_backends.1 = cache
auth_cache.cache_ttl = 5000
auth_cache.cached_backend = http
auth_http.http_method   = post
auth_http.user_path     = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/user
auth_http.vhost_path    = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/vhost
auth_http.resource_path = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/resource
auth_http.topic_path    = http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/v0/auth/topic

listeners.tcp = none
listeners.ssl.default = $DUNE_BIND_IP:$DUNE_MQ_GAME_PORT
ssl_options.cacertfile = $CERTDIR/cacert.pem
ssl_options.certfile   = $CERTDIR/cert.pem
ssl_options.keyfile    = $CERTDIR/key.pem
ssl_options.verify     = verify_none

management.tcp.ip   = $DUNE_BIND_IP
management.tcp.port = $DUNE_MQ_GAME_MGMT_PORT
prometheus.tcp.ip   = 127.0.0.1
prometheus.tcp.port = $DUNE_MQ_GAME_PROM_PORT
EOF

# postgres extension config (loaded via include_if_exists)
cat > "$RUNTIME/postgres.conf" <<EOF
idle_in_transaction_session_timeout = '60s'
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = all
pg_stat_statements.max = 10000
track_activity_query_size = 2048
EOF

# director.ini — pulled from templates (k3s-extracted, then customised)
if [ -f "$TEMPLATES/director.ini" ]; then
  mkdir -p "$RUNTIME/director-conf.d"
  cp "$TEMPLATES/director.ini" "$RUNTIME/director-conf.d/director.ini"
fi

# --------------------------------------------------------------------------
# 4. Postgres data dir — initdb if first run
# --------------------------------------------------------------------------
PGDATA="$STATE/pg/data"
mkdir -p "$PGDATA"
chmod 0700 "$PGDATA"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  log "Initialising database (first run). This may take a minute without visible activity."
  env -i HOME=/tmp LC_ALL=C \
    LD_LIBRARY_PATH=$(ldlib postgres) \
    ICU_DATA=$(icu_dir postgres) \
    "$EXTRACTED/postgres/usr/local/bin/initdb" \
      -D "$PGDATA" -U postgres \
      --auth-host=scram-sha-256 --auth-local=trust \
      -E UTF8 --locale=C 2>&1 | tail -10

  # Append our overrides
  cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = '127.0.0.1'
port = $DUNE_PG_PORT
unix_socket_directories = '$RUNTIME/postgresql'
include_if_exists = '$RUNTIME/postgres.conf'
EOF

  cat > "$PGDATA/pg_hba.conf" <<'EOF'
local   all   all                trust
host    all   all   127.0.0.1/32 trust
host    all   all   ::1/128      trust
EOF
fi

# --------------------------------------------------------------------------
# 5. Schema load — boot pg briefly via pg_ctl, run resetdb if schema missing,
# then stop pg cleanly so start-pg.sh can claim it.
# --------------------------------------------------------------------------
SCHEMA_MARKER="$STATE/schema-loaded"
if [ ! -f "$SCHEMA_MARKER" ]; then
  log "Loading database schema (first run). This may take several minutes without visible activity."
  PG_BIN="$EXTRACTED/postgres/usr/local/bin"
  PG_ENV="LD_LIBRARY_PATH=$(ldlib postgres) ICU_DATA=$(icu_dir postgres)"

  env -i HOME=/tmp LC_ALL=C $PG_ENV \
    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOGS/pg-init.log" -w -t 30 start \
    || { tail -20 "$LOGS/pg-init.log"; die "Postgres failed to start during schema load"; }

  # Trap to ensure we always stop pg even on failure below
  trap 'env -i HOME=/tmp LC_ALL=C '"$PG_ENV"' '"$PG_BIN"'/pg_ctl -D '"$PGDATA"' -w -t 30 stop -m fast >/dev/null 2>&1 || true' EXIT

  # Wait for ready
  for i in $(seq 1 30); do
    env -i HOME=/tmp LC_ALL=C $PG_ENV "$PG_BIN/pg_isready" -h 127.0.0.1 -p "$DUNE_PG_PORT" >/dev/null 2>&1 && break
    sleep 1
  done

  # resetdb expects the project user (`dune`) to already exist — it manages
  # databases but not roles.  Create it now if missing.
  USER_EXISTS=$(env -i HOME=/tmp LC_ALL=C $PG_ENV \
    "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d postgres -tA \
    -c "SELECT 1 FROM pg_roles WHERE rolname='dune'" 2>/dev/null || true)
  if [ -z "$USER_EXISTS" ]; then
    log "  creating role 'dune'..."
    env -i HOME=/tmp LC_ALL=C $PG_ENV \
      "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d postgres \
      -c "CREATE USER dune WITH PASSWORD 'dune' NOCREATEDB NOCREATEROLE" >/dev/null
  fi

  log "  invoking resetdb (loads schema via ToolsDB)..."
  ( cd "$EXTRACTED/db-utils/root/PSQL" && \
    LD_LIBRARY_PATH=$(ldlib db-utils) PYTHONPATH=. \
    "$EXTRACTED/db-utils/usr/local/bin/python3.12" resetdb.py \
      --local-as-remote --no-backup --unattended \
      --host 127.0.0.1:$DUNE_PG_PORT \
      --project-database dune --project-user dune --project-password dune \
      --admin-user postgres --admin-password '' \
      --schema-path "$EXTRACTED/game-server/home/dune/server/DuneSandbox/Database" \
      2>&1 | tail -20 ) || die "Database schema load failed — see output above"

  # Set default search_path so the world_partition trigger resolves the
  # unqualified procedure name it calls (dune.create_event_log_partition_table).
  env -i HOME=/tmp LC_ALL=C $PG_ENV \
    "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune <<'SQL' >/dev/null
ALTER DATABASE dune SET search_path TO dune,public;
ALTER ROLE dune SET search_path TO dune,public;
SQL

  touch "$SCHEMA_MARKER"
  log "Database schema loaded: success."
fi

# --------------------------------------------------------------------------
# 6. Seed world_partition (idempotent — ON CONFLICT DO NOTHING).
# pg is still running from the schema-load block above.
# --------------------------------------------------------------------------
PG_RUN_ENV="LD_LIBRARY_PATH=$(ldlib postgres) ICU_DATA=$(icu_dir postgres)"
PG_BIN="$EXTRACTED/postgres/usr/local/bin"

# Ensure pg is up (in case schema was already loaded on a previous run and
# we skipped that block).
env -i HOME=/tmp LC_ALL=C $PG_RUN_ENV \
  "$PG_BIN/pg_isready" -h 127.0.0.1 -p "$DUNE_PG_PORT" >/dev/null 2>&1 || \
  env -i HOME=/tmp LC_ALL=C $PG_RUN_ENV \
    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOGS/pg-init.log" -w -t 30 start >/dev/null 2>&1 || true

PCOUNT=$(env -i HOME=/tmp LC_ALL=C $PG_RUN_ENV \
  "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune -tA \
  -c "SELECT count(*) FROM dune.world_partition" 2>/dev/null || echo 0)

if [ "${PCOUNT:-0}" -lt 29 ]; then
  log "Seeding world partitions..."
  env -i HOME=/tmp LC_ALL=C $PG_RUN_ENV \
    "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune <<'SQL' || warn "world_partition seed failed (non-fatal)"
SET search_path TO dune,public;
INSERT INTO dune.world_partition (partition_id, map, partition_definition, dimension_index, blocked, label)
VALUES
  (1, 'Survival_1', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Abbir'),
  (2, 'Overmap', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Overland'),
  (3, 'SH_Arrakeen', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Arrakeen'),
  (4, 'SH_HarkoVillage', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Harko Village'),
  (5, 'CB_Story_Hephaestus', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Hephaestus'),
  (6, 'CB_Story_Ecolab_Carthag', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Carthag Ecolab'),
  (7, 'CB_Story_WaterFatManor', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'WaterFat Manor'),
  (8, 'DeepDesert_1', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Deep Desert'),
  (9, 'Story_ProcesVerbal', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Proces Verbal'),
  (10, 'DLC_Story_LostHarvest_EcolabA', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Lost Harvest Ecolab A'),
  (11, 'DLC_Story_LostHarvest_EcolabB', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Lost Harvest Ecolab B'),
  (12, 'DLC_Story_LostHarvest_ForgottenLab', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Forgotten Lab'),
  (13, 'Story_ArtOfKanly', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Art of Kanly'),
  (14, 'CB_Dungeon_Hephaestus', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Hephaestus Dungeon'),
  (15, 'CB_Dungeon_OldCarthag', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Old Carthag'),
  (16, 'Story_Faction_Outpost_Atre', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Atreides Outpost'),
  (17, 'Story_Faction_Outpost_Hark', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Harkonnen Outpost'),
  (18, 'Story_HeighlinerDungeon', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Heighliner'),
  (19, 'CB_Ecolab_Bronze_Green_089', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Ecolab 089'),
  (20, 'CB_Ecolab_Bronze_Green_152', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Ecolab 152'),
  (21, 'CB_Ecolab_Bronze_Green_024', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Ecolab 024'),
  (22, 'CB_Ecolab_Bronze_Green_195', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Ecolab 195'),
  (23, 'CB_Ecolab_Bronze_Green_136', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Ecolab 136'),
  (24, 'CB_Overland_M_01', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Overland M01'),
  (25, 'CB_Overland_S_04', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Overland S04'),
  (26, 'CB_Overland_S_06', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Overland S06'),
  (27, 'CB_Story_BanditFortress01', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Bandit Fortress'),
  (28, 'CB_Overland_S_07', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Overland S07'),
  -- SH_FallenLight warm anchor. Funcom ships this map (GameTweaks
  -- PlayerHardCap=80) but CubeCoders' AMP module never seeded it. We
  -- always insert the row — costs nothing if the operator doesn't
  -- DUNE_ENABLE_FALLEN_LIGHT, and removes a setup step if they do.
  -- Id 130 is the base of multi-sietch-config.sh's FallenLight range
  -- (warm = base-1, dims = base..base+9). See dune-multi-sietch wiki.
  (130, 'SH_FallenLight', '{"box":{"max_x":1,"max_y":1,"min_x":0,"min_y":0},"type":"box2d_array"}'::jsonb, 0, false, 'Fallen Light')
ON CONFLICT (partition_id) DO NOTHING;
SQL
fi

# --------------------------------------------------------------------------
# 6b. Seed dimensional partitions for multi-Sietch travel destinations.
#
# Per multi-sietch-config.sh, each enabled destination map gets N
# dimensional partitions (one per concurrent sandstorm tunnel). These
# rows are what Director's DimensionServerGroup picks from when
# handling Overland_to_DeepDesert1_S{N}, Overland_to_Arrakeen, etc.
# Without them, the picker has nothing → Code 17. Counts are
# configurable via DUNE_DD_DIMENSIONS / DUNE_ARRAKEEN_DIMENSIONS /
# DUNE_HARKO_DIMENSIONS / DUNE_FALLEN_LIGHT_DIMENSIONS.
#
# Idempotent (ON CONFLICT DO NOTHING). Operator-changeable: increase
# the dim count via panel env vars and the rows append on next boot
# (decreasing the count does NOT delete rows — operator must DELETE
# from world_partition manually, otherwise the spawner re-runs but
# Director still sees the row).
# --------------------------------------------------------------------------
source "$(dirname "$(readlink -f "$0")")/multi-sietch-config.sh"
DIM_VALUES="$(multi_sietch_partition_values)"
if [ -n "$DIM_VALUES" ]; then
  log "Seeding dimensional partitions for multi-Sietch travel..."
  env -i HOME=/tmp LC_ALL=C $PG_RUN_ENV \
    "$PG_BIN/psql" -h 127.0.0.1 -p "$DUNE_PG_PORT" -U postgres -d dune <<SQL || warn "dim partition seed failed (non-fatal)"
SET search_path TO dune,public;
INSERT INTO dune.world_partition (partition_id, map, partition_definition, dimension_index, blocked, label)
VALUES
$DIM_VALUES
ON CONFLICT (partition_id) DO NOTHING;
SQL
  log "  dimensional partitions seeded"
fi

# --------------------------------------------------------------------------
# 7. Stop pg if we started it — start-pg.sh will bring it up as the real
# long-lived background process.
# --------------------------------------------------------------------------
env -i HOME=/tmp LC_ALL=C \
  LD_LIBRARY_PATH=$(ldlib postgres) ICU_DATA=$(icu_dir postgres) \
  "$EXTRACTED/postgres/usr/local/bin/pg_ctl" -D "$PGDATA" -w -t 30 stop -m fast \
  >/dev/null 2>&1 || true
trap - EXIT

# --------------------------------------------------------------------------
# 8. UE5 saved dir — symlink into the extracted game-server tree so the
# binary writes persistent data into $STATE/ue5-saved/ instead of the
# read-only(ish) extraction.
# --------------------------------------------------------------------------
mkdir -p "$STATE/ue5-saved"
SAVED_TARGET="$EXTRACTED/game-server/home/dune/server/DuneSandbox/Saved"
mkdir -p "$(dirname "$SAVED_TARGET")"
rm -rf "$SAVED_TARGET"
ln -sfn "$STATE/ue5-saved" "$SAVED_TARGET"

# --------------------------------------------------------------------------
# 8a. Admin-editable config files
#
# Funcom ships two user-editable INI templates in the depot at
# scripts/setup/config/ — UE5 reads them from DuneSandbox/Saved/UserSettings/.
# We seed those on first run, then expose them at the instance root via
# symlinks so the AMP file browser shows them next to README etc., matching
# the layout in Funcom's official documentation.
#
# Same treatment for director_config.ini — Funcom's authoritative
# director config (per-map player caps, transfer rules, instancing modes,
# world-closure flags).
# --------------------------------------------------------------------------
USERSETTINGS="$STATE/ue5-saved/UserSettings"
mkdir -p "$USERSETTINGS"

for f in UserEngine.ini UserGame.ini; do
  if [ ! -f "$USERSETTINGS/$f" ] && [ -f "$DEPOT/scripts/setup/config/$f" ]; then
    cp "$DEPOT/scripts/setup/config/$f" "$USERSETTINGS/$f"
    log "  seeded admin file: UserSettings/$f"
  fi
done

if [ ! -f "$STATE/director_config.ini" ]; then
  DIR_TPL="$EXTRACTED/director/Tools/Battlegroups/Director/BattlegroupDirector/director_config.ini"
  if [ -f "$DIR_TPL" ]; then
    cp "$DIR_TPL" "$STATE/director_config.ini"
    # Funcom ships level=debug; default to info to cut log spam. Admins
    # can still edit this file afterwards (we only seed when absent).
    sed -i 's/^level=debug\b/level=info/' "$STATE/director_config.ini"
    log "  seeded admin file: director_config.ini (logging: info)"
  fi
fi

# AMP's metaconfig automap now targets the real state files directly
# ($STATE/ue5-saved/UserSettings/*.ini, $STATE/ondemand.ini — see
# metaconfig.json). We deliberately do NOT symlink them into server/:
# AMP merges config with an atomic temp+rename that turns a symlink into
# a regular file, and this script (running AFTER AMP's merge) would then
# clobber that write by recreating the link. Pointing AMP straight at the
# state files removes the race entirely.
BASEDIR="$BASE/server"
for stale in UserEngine.ini UserGame.ini director_config.ini; do
  [ -L "$BASEDIR/$stale" ] && rm -f "$BASEDIR/$stale"
done

# Seed ondemand.ini so AMP's automap has a file to merge into (AMP logged
# "file does not exist" otherwise). Defaults mirror mock-k8s's built-ins.
if [ ! -f "$STATE/ondemand.ini" ]; then
  cat > "$STATE/ondemand.ini" <<'ONDEMAND_SEED'
; On-Demand Instancing - consumed by mock-k8s for the dynamic server pool.
MaxConcurrentInstances=8
AutomaticStopDuration=10m
AlwaysWarmMaps=Survival_1,Overmap,DeepDesert_1
ONDEMAND_SEED
  log "  seeded admin file: ondemand.ini"
fi

# Make a fresh copy of director_config.ini into the runtime conf dir on
# every start, so director picks up any admin edits.  Named both ways
# because we're not 100% sure which the .NET binary expects.
mkdir -p "$RUNTIME/director-conf.d"
cp -f "$STATE/director_config.ini" "$RUNTIME/director-conf.d/director_config.ini" 2>/dev/null || true
cp -f "$STATE/director_config.ini" "$RUNTIME/director-conf.d/director.ini"        2>/dev/null || true


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Merge UserOverrides.ini into UserGame.ini.
#
# DefaultGame.ini exposes hundreds of [/Script/DuneSandbox.*] tuning keys;
# only a handful are practical to surface in AMP's settings UI. The rest
# are admin-editable as raw INI in state/UserOverrides.ini, which we append
# verbatim to the end of UserGame.ini here - after AMP's automap merge has
# run - so the overrides take precedence and survive AMP rewriting the file.
# Marker comments delimit the appended block: we strip any prior copy and
# re-append, so editing UserOverrides.ini just needs a restart to apply.
# --------------------------------------------------------------------------
STATE_UG="$STATE/ue5-saved/UserSettings/UserGame.ini"
OVERRIDES_INI="$STATE/UserOverrides.ini"
OVR_BEGIN="; >>>>> AMP: UserOverrides.ini appended below - do not edit past this line >>>>>"
OVR_END="; <<<<< AMP: end of UserOverrides.ini <<<<<"

if [ ! -f "$OVERRIDES_INI" ]; then
  cat > "$OVERRIDES_INI" <<'OVR_TEMPLATE'
; ============================================================================
; UserOverrides.ini  -  advanced dedicated-server gameplay overrides
;
; Everything in this file is appended verbatim to UserGame.ini on every
; server start, after AMP has written its managed settings. Use it for the
; large set of [/Script/DuneSandbox.*] tuning keys that are not surfaced in
; the AMP settings UI.
;
; The full list of available sections, keys and default values is in the
; game's own DuneSandbox/Config/DefaultGame.ini. Copy in only what you want
; to change, for example:
;
;   [/Script/DuneSandbox.SandwormSettings]
;   SomeKey=SomeValue
;
; Keys here take precedence over AMP-managed values. Changes apply on the
; next server restart.
; ============================================================================
OVR_TEMPLATE
  log "  seeded admin file: UserOverrides.ini"
fi

if [ -f "$STATE_UG" ]; then
  # Strip any previously-appended block (BEGIN..END inclusive).
  if grep -qF "$OVR_BEGIN" "$STATE_UG"; then
    awk -v b="$OVR_BEGIN" -v e="$OVR_END" \
      '$0==b{skip=1} !skip{print} skip&&$0==e{skip=0}' \
      "$STATE_UG" > "$STATE_UG.tmp" && mv "$STATE_UG.tmp" "$STATE_UG"
  fi
  # Re-append a fresh copy.
  {
    printf '%s\n' "$OVR_BEGIN"
    cat "$OVERRIDES_INI"
    printf '%s\n' "$OVR_END"
  } >> "$STATE_UG"
  log "  merged UserOverrides.ini into UserGame.ini"
fi

log "Pre-start setup: success."
