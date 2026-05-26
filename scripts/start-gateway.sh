#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-gateway.sh BASE_DIR
#
# Launches the Gateway python service.  This is the FLS bridge — it tells
# FLS about server up/down events, declares character-deletion intents,
# and crucially advertises the public RMQ endpoint clients should connect
# to.  Internal HTTP on 8080.
#
# Critical: --RMQGameHostname here is what gets PUBLISHED to FLS as the
# AMQPS address.  Must be the externally-reachable IP, not 127.0.0.1.

export SOURCE="gateway"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Starting FLS Gateway (advertising RMQ at $DUNE_EXTERNAL_IP:$DUNE_MQ_GAME_PORT)..."

GW=$(rootfs gateway)
WORK="$GW/Tools/Battlegroups/GatewayService"
[ -d "$WORK" ] || die "Gateway workdir missing"

# Gateway creates a 'logs/' subdir in CWD — ensure it's writable.
# (We can't chown the read-only-ish extracted tree to mutate; mkdir is fine.)
mkdir -p "$WORK/logs" 2>/dev/null || true

cd "$WORK"

launch_bg gateway "$LOGS/gateway.log" -- env \
  LD_LIBRARY_PATH=$(ldlib gateway) \
  PYTHONUNBUFFERED=1 \
  PATH="$GW/usr/local/bin:/usr/local/bin:/usr/bin:/bin" \
  DuneDatabaseInterfacePSQL_DatabaseHost="127.0.0.1:$DUNE_PG_PORT" \
  DuneDatabaseInterfacePSQL_DatabaseName=dune \
  DuneDatabaseInterfacePSQL_DatabaseUser=dune \
  DuneDatabaseInterfacePSQL_DatabasePassword=dune \
  FuncomLiveServices__ServiceAuthToken="$DUNE_JWT" \
  FuncomLiveServices__DefaultFlsEnvironment="$DUNE_FLS_ENV" \
  FuncomLiveServices__BattlegroupAuthorizationPreset=BattlegroupInternal \
  FuncomLiveServices__RmqTlsEnabled=true \
  BATTLEGROUP="$DUNE_WORLD_NAME" \
  BATTLEGROUP_DISPLAY_NAME="$DUNE_WORLD_NAME" \
  BATTLEGROUP_TITLE="$DUNE_WORLD_TITLE" \
  gateway_display_name="$DUNE_WORLD_TITLE" \
  gateway_game_rmq_hostname="$DUNE_EXTERNAL_IP" \
  gateway_game_rmq_port="$DUNE_MQ_GAME_PORT" \
  gateway_game_rmq_http_port="$DUNE_MQ_GAME_MGMT_PORT" \
  gateway_game_rmq_secret="$DUNE_RMQ_SEC" \
  gateway_host_datacenter_id="${DUNE_HOST_DC_ID:-dune-amp}" \
  gateway_host_datacenter_ip_address="$DUNE_EXTERNAL_IP" \
  HOST_DATACENTER_ID="${DUNE_HOST_DC_ID:-dune-amp}" \
  HOST_DATACENTER_IP_ADDRESS="$DUNE_EXTERNAL_IP" \
  OnlineSubsystem_ServerName="$DUNE_WORLD_NAME" \
  OnlineSubsystem_DatacenterId="$DUNE_REGION" \
  RMQ_HTTP_TOKEN_AUTH_SECRET="$DUNE_RMQ_SEC" \
  "$GW/usr/local/bin/python3.12" -m service -c ./service/configs/service.conf \
    --RMQGameHostname="$DUNE_EXTERNAL_IP" \
    --RMQGamePort="$DUNE_MQ_GAME_PORT" \
    --RMQGameHttpPort="$DUNE_MQ_GAME_MGMT_PORT" \
    --RMQAdminHostname=127.0.0.1 \
    --RMQAdminPort="$DUNE_MQ_ADMIN_PORT"

# Gateway doesn't bind a listenable TCP socket we can poll cleanly; instead
# wait for it to log the boot-complete line.
log "Waiting for FLS Gateway boot. This may take a minute without visible activity."
for i in $(seq 1 30); do
  if grep -q "Monitoring for servers going up or down" "$LOGS/gateway.log" 2>/dev/null; then
    log "FLS Gateway ready: success (pid $(read_pid gateway))"
    exit 0
  fi
  sleep 1
done
tail -30 "$LOGS/gateway.log" >&2
die "FLS Gateway didn't reach 'Monitoring for servers' within 30s"
