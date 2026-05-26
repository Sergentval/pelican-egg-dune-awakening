#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-text-router.sh BASE_DIR
#
# Launches the text-router .NET service.  Provides the HTTP auth backend
# that both RabbitMQ brokers query (auth_http.* in the rabbitmq.conf),
# and also dispatches in-game chat.  Internal-only — binds 127.0.0.1.

export SOURCE="text-router"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Starting Text Router on 127.0.0.1:$DUNE_TEXT_ROUTER_PORT..."

TR=$(rootfs text-router)
WORK="$TR/Tools/Battlegroups/TextRouter/TextRouter"
[ -d "$WORK" ] || die "Text Router workdir missing"

cd "$WORK"

# .NET AOT respects ASPNETCORE_URLS/HTTP_PORTS; bind explicitly to loopback
launch_bg text-router "$LOGS/text-router.log" -- env \
  LD_LIBRARY_PATH=$(ldlib text-router) \
  ICU_DATA=$(icu_dir text-router) \
  DOTNET_RUNNING_IN_CONTAINER=true \
  DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=false \
  ASPNETCORE_URLS="http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT" \
  Database_address=127.0.0.1 \
  Database_port="$DUNE_PG_PORT" \
  Database_user=dune \
  Database_password=dune \
  Database_name=dune \
  FuncomLiveServices__ServiceAuthToken="$DUNE_JWT" \
  FuncomLiveServices__DefaultFlsEnvironment="$DUNE_FLS_ENV" \
  FuncomLiveServices__RmqTlsEnabled=true \
  RMQ_HTTP_TOKEN_AUTH_SECRET="$DUNE_RMQ_SEC" \
  BATTLEGROUP_DISPLAY_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_TITLE="$DUNE_WORLD_TITLE" \
  BATTLEGROUP_REGION_NAME="$DUNE_REGION" \
  BATTLEGROUP_LANGUAGE=en-US \
  HOST_DATACENTER_ID="${DUNE_HOST_DC_ID:-dune-amp}" \
  HOST_DATACENTER_IP_ADDRESS="$DUNE_EXTERNAL_IP" \
  "$WORK/TextRouter" --RMQGameHostname=127.0.0.1 --RMQGamePort="$DUNE_MQ_GAME_PORT"

if wait_for_port 127.0.0.1 "$DUNE_TEXT_ROUTER_PORT" 30; then
  log "Text Router ready: success (pid $(read_pid text-router))"
else
  tail -30 "$LOGS/text-router.log" >&2
  die "Text Router failed to listen within 30s"
fi
