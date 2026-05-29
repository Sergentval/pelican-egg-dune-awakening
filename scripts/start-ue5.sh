#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-ue5.sh BASE_DIR MAP_NAME
#
# Launches one UE5 dedicated server for the given map.  MAP_NAME is one of
# the stock partitions seeded in dune.world_partition: Survival_1, Overmap.
# Reads per-map env from $RUNTIME/ue5-$MAP.env (DUNE_PARTITION, GAME_PORT,
# IGW_PORT) that prestart.sh wrote.

BASE="${1:-${DUNE_BASE_DIR:-}}"
MAP="${2:-}"
SUFFIX="${3:-}"          # optional per-instance tag (e.g. "p8" for partition 8)
[ -n "$MAP" ] || { echo "usage: $0 BASE_DIR MAP_NAME [SUFFIX]" >&2; exit 1; }

# Unique identifier for log/pid files: ue5-MAP or ue5-MAP-SUFFIX
INSTANCE_ID="ue5-$MAP"
[ -n "$SUFFIX" ] && INSTANCE_ID="ue5-$MAP-$SUFFIX"

export SOURCE="$INSTANCE_ID"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

# Per-map env file (written by prestart.sh for the always-on maps).
# On-demand launches via mock-k8s set DUNE_PARTITION/GAME_PORT/IGW_PORT
# inline; skip reading the env file when they're already in the env.
if [ -z "${DUNE_PARTITION:-}" ]; then
  ENV_FILE="$RUNTIME/ue5-$MAP.env"
  [ -f "$ENV_FILE" ] || die "per-map env file missing — did pre-start run?"
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

# UE5 refuses to register with FLS if it saves farm_state with a
# wildcard igw_addr.  When AMP binds 0.0.0.0 we substitute loopback for
# the IGW listener — director+gateway speak to UE5 over RMQ, not IGW.
if [ "$DUNE_BIND_IP" = "0.0.0.0" ]; then
  IGW_BIND=127.0.0.1
else
  IGW_BIND="$DUNE_BIND_IP"
fi

log "Starting UE5 server: map=$MAP, partition=$DUNE_PARTITION, bind=$DUNE_BIND_IP UDP $DUNE_GAME_PORT..."

GS=$(rootfs game-server)
SERVER_SH="$GS/home/dune/server/DuneSandboxServer.sh"
[ -x "$SERVER_SH" ] || die "UE5 server binary missing or not executable — did the Update step run?"

cd "$GS/home/dune/server"

# --------------------------------------------------------------------------
# UE5 loads its user-tier config (UserEngine.ini / UserGame.ini) from
# $HOME/.config/Epic/Unreal Engine/Engine/Config/. Funcom's stock run.sh
# symlinks that directory onto Saved/UserSettings so the admin-editable
# User*.ini files are applied; we replaced run.sh, so we replicate the
# symlink here. Without it UE5 silently ignores UserEngine.ini entirely
# (no [ConsoleVariables], no gameplay overrides).
UE5_HOME="$STATE/ue5-saved"
UE5_USER_CFG="$UE5_HOME/.config/Epic/Unreal Engine/Engine/Config"
mkdir -p "$UE5_HOME/.config/Epic/Unreal Engine/Engine"
if [ -d "$UE5_USER_CFG" ] && [ ! -L "$UE5_USER_CFG" ]; then
  rm -rf "$UE5_USER_CFG"
fi
ln -sfn "$UE5_HOME/UserSettings" "$UE5_USER_CFG"

# --------------------------------------------------------------------------
# FLS stub redirection. Only enabled when start-fls-stub.sh has produced its
# CA bundle (the absence is a noisy startup failure, not silent skip — but
# we tolerate it here to keep the warm-only path functional during
# development of the stub itself).
#
# Director still talks to real FLS; only UE5's calls flow through us, and
# of those only Battlegroups_IsPlayerAuthorized is stubbed (everything else
# is reverse-proxied to real FLS so heartbeats and char-data keep working).
FLS_STUB_CA="$STATE/fls-stub/ca-bundle.pem"
FLS_STUB_PORT="${DUNE_FLS_STUB_PORT:-8443}"
FLS_URL_OVERRIDES=()
FLS_ENV_OVERRIDES=()
if [ -s "$FLS_STUB_CA" ]; then
  # PLAINTEXT redirection — UE5 is statically linked (only libc.so loaded
  # at runtime), so LD_PRELOAD shims to disable CURLOPT_SSL_VERIFYPEER
  # don't reach the call site. And UE5's libcurl ignores SSL_CERT_FILE +
  # CURL_CA_BUNDLE env vars (CURLOPT_CAINFO hardcoded). Pelican's
  # ReadonlyRootfs:true also blocks appending our cert to the system bundle.
  # Loopback HTTP eliminates the TLS trust problem entirely — the stub
  # still reverse-proxies upstream over verified TLS, so we don't expose
  # plaintext credentials to the network. If UE5 strictly enforces https://
  # for FLS, fall back to bind-mounting the system CA bundle via egg JSON.
  FLS_URL_OVERRIDES=(
    "-ini:engine:[FuncomLiveServices_$DUNE_FLS_ENV]:FlsHostUrl=http://127.0.0.1:$FLS_STUB_PORT/"
  )
  FLS_ENV_OVERRIDES=()
  log "  FLS redirection: -> http://127.0.0.1:$FLS_STUB_PORT/ (plaintext loopback)"
fi

launch_bg "$INSTANCE_ID" "$LOGS/$INSTANCE_ID.log" -- env \
  HOME="$STATE/ue5-saved" \
  POD_IP="$DUNE_BIND_IP" \
  POD_NAME="$DUNE_WORLD_NAME-$MAP" \
  NODE_NAME="$DUNE_WORLD_NAME" \
  FARM_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP="$DUNE_WORLD_NAME" \
  BATTLEGROUP_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_DISPLAY_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_TITLE="$DUNE_WORLD_TITLE" \
  FC_CRASHREPORTER_LOGS="$STATE/ue5-saved/CrashReporterLogs" \
  FuncomLiveServices__ServiceAuthToken="$DUNE_JWT" \
  RMQ_HTTP_TOKEN_AUTH_SECRET="$DUNE_RMQ_SEC" \
  TZ=Etc/UTC \
  "${FLS_ENV_OVERRIDES[@]}" \
  "$SERVER_SH" "$MAP" \
    -log -unattended -stdout -FullStdOutLogOutput \
    -FarmRegion="$DUNE_REGION" \
    "-ini:engine:[FuncomLiveServices]:ServiceAuthToken=$DUNE_JWT" \
    "-ini:engine:[FuncomLiveServices]:DefaultFlsEnvironment=$DUNE_FLS_ENV" \
    "-ini:engine:[FuncomLiveServices]:ServerCommandsAuthToken=${DUNE_SVC_CMD_TOKEN:-}" \
    "-ini:engine:[FuncomLiveServices_$DUNE_FLS_ENV]:ServerCommandsAuthToken=${DUNE_SVC_CMD_TOKEN:-}" \
    "${FLS_URL_OVERRIDES[@]}" \
    "-ini:engine:[OnlineSubsystem]:ServerName=$DUNE_WORLD_NAME" \
    "-ini:engine:[OnlineSubsystem]:DatacenterId=$DUNE_REGION" \
    -RMQGameTlsEnabled=true \
    -ServerName="$DUNE_WORLD_NAME" \
    -MultiHome="$DUNE_BIND_IP" \
    -ExternalAddress="$DUNE_EXTERNAL_IP" \
    -Port="$DUNE_GAME_PORT" \
    -DatabaseName=dune \
    -DatabaseHost="127.0.0.1:$DUNE_PG_PORT" \
    -DatabaseUser=dune \
    -DatabasePassword=dune \
    -PartitionIndex="$DUNE_PARTITION" \
    -battlegroup-director-url="127.0.0.1:$DUNE_DIRECTOR_PORT" \
    --RMQGameHostname=127.0.0.1 --RMQGamePort="$DUNE_MQ_GAME_PORT" \
    --RMQAdminHostname=127.0.0.1 --RMQAdminPort="$DUNE_MQ_ADMIN_PORT" \
    -IGWBindAddress="$IGW_BIND"

# UE5 takes a while — wait for the UDP port to be bound, then poll
# dune.farm_state for ready=true.
log "Waiting for UE5 server ($MAP) to bind UDP $DUNE_GAME_PORT. This may take a minute or two without visible activity."
if ! wait_for_udp_bind "$INSTANCE_ID" "$DUNE_GAME_PORT" 180; then
  tail -50 "$LOGS/$INSTANCE_ID.log" >&2
  die "UE5 server ($MAP) failed to bind UDP $DUNE_GAME_PORT within 180s"
fi
log "UE5 server ($MAP) ready: success (pid $(read_pid "ue5-$MAP"))"
# Don't block PreStart on the farm_state.ready handshake — that takes
# minutes and director drives it async over RMQ.  UDP bind is the
# canonical "the process is alive and listening" signal.
