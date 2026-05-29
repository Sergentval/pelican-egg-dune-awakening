#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-director.sh BASE_DIR
#
# Launches the Battlegroup Director .NET service.  This is the brain — it
# talks to FLS over HTTPS to declare partitions UP/DOWN, heartbeats the
# battlegroup, and routes RPC over RabbitMQ.  Internal HTTP on 11717
# (UE5 instances call -battlegroup-director-url=127.0.0.1:11717).

export SOURCE="director"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Starting Battlegroup Director on 127.0.0.1:$DUNE_DIRECTOR_PORT..."

DR=$(rootfs director)
WORK="$DR/Tools/Battlegroups/Director/BattlegroupDirector"
[ -d "$WORK" ] || die "Battlegroup Director workdir missing"

cd "$WORK"

launch_bg director "$LOGS/director.log" -- env \
  IGNORE_IGWO_API_SERVER_CHECK=true \
  KUBECONFIG="$RUNTIME/mock-k8s-kubeconfig.yaml" \
  KUBERNETES_SERVICE_HOST=127.0.0.1 \
  KUBERNETES_SERVICE_PORT="${K8S_MOCK_PORT:-6443}" \
  LD_LIBRARY_PATH=$(ldlib director) \
  ICU_DATA=$(icu_dir director) \
  DOTNET_RUNNING_IN_CONTAINER=true \
  DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=false \
  ASPNETCORE_URLS="http://127.0.0.1:$DUNE_DIRECTOR_PORT" \
  ASPNETCORE_CONTENTROOT="$RUNTIME/director-conf.d" \
  Database_address="127.0.0.1:$DUNE_PG_PORT" \
  Database_name=dune \
  Database_user=dune \
  Database_password=dune \
  FuncomLiveServices__ServiceAuthToken="$DUNE_JWT" \
  FuncomLiveServices__DefaultFlsEnvironment="$DUNE_FLS_ENV" \
  FuncomLiveServices__RmqTlsEnabled=true \
  FuncomLiveServices__BattlegroupAuthorizationPreset=BattlegroupInternal \
  RMQ_HTTP_TOKEN_AUTH_SECRET="$DUNE_RMQ_SEC" \
  BATTLEGROUP="$DUNE_WORLD_NAME" \
  BATTLEGROUP_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_DISPLAY_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_TITLE="$DUNE_WORLD_TITLE" \
  BATTLEGROUP_REGION_NAME="$DUNE_REGION" \
  BATTLEGROUP_LANGUAGE=en-US \
  HOST_DATACENTER_ID="${DUNE_HOST_DC_ID:-dune-amp}" \
  HOST_DATACENTER_IP_ADDRESS="$DUNE_EXTERNAL_IP" \
  "$WORK/Director" \
    --RMQGameHostname=127.0.0.1 --RMQGamePort="$DUNE_MQ_GAME_PORT" \
    --RMQAdminHostname=127.0.0.1 --RMQAdminPort="$DUNE_MQ_ADMIN_PORT"

if wait_for_port 127.0.0.1 "$DUNE_DIRECTOR_PORT" 60; then
  log "Battlegroup Director ready: success (pid $(read_pid director))"
else
  tail -50 "$LOGS/director.log" >&2
  die "Battlegroup Director failed to listen within 60s"
fi
