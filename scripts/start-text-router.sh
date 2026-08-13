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

# Two gates, because this service is the RMQ auth backend for BOTH brokers
# and every later stage depends on it answering. A bound socket only proves
# Kestrel started; /status proves the app can serve. If we advanced on the
# socket alone, the Director could issue its RMQ login into a text-router
# that is not serving yet — RabbitMQ turns that into ACCESS_REFUSED and the
# Director reports "RMQ unreachable" (issue #82), which points the operator
# at the broker instead of at the auth backend that actually failed.
if wait_for_port 127.0.0.1 "$DUNE_TEXT_ROUTER_PORT" 30 \
   && wait_for_http "http://127.0.0.1:$DUNE_TEXT_ROUTER_PORT/status" 200 30; then
  log "Text Router ready: success (pid $(read_pid text-router), /status 200)"
else
  tail -30 "$LOGS/text-router.log" >&2
  die "Text Router failed to become ready within 60s (port bind + /status 200). \
Every RMQ login is authorised by this service — the Director will report \
'RMQ unreachable' until it answers. See logs/text-router.log."
fi
