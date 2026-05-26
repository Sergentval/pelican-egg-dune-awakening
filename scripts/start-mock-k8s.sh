#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-mock-k8s.sh BASE_DIR
#
# Launches the minimal Kubernetes API mock that intercepts director's
# on-demand ServerSetScale CR ops and turns them into start-ue5.sh
# invocations against a pre-allocated port pool.

BASE="${1:-${DUNE_BASE_DIR:-}}"
# AMP supplies the startup-auth token via $AMPToken substitution,
# only in command-line contexts (not env). We receive it as $2 and
# export it so the env-block below can hand it to mock-k8s-go.
AMP_TOKEN="${2:-}"
export AMP_TOKEN
export SOURCE="mock-k8s"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

log "Starting mock Kubernetes API (port pool size ${K8S_POOL_SIZE:-25})..."

launch_bg mock-k8s "$LOGS/mock-k8s.log" -- \
  env PYTHONUNBUFFERED=1 \
      AMP_TOKEN="$AMP_TOKEN" \
      K8S_MOCK_PORT="${K8S_MOCK_PORT:-6443}" \
      K8S_POOL_SIZE="${K8S_POOL_SIZE:-25}" \
      K8S_POOL_GAME_PORT_BASE="${K8S_POOL_GAME_PORT_BASE:-7900}" \
      K8S_POOL_IGW_PORT_BASE="${K8S_POOL_IGW_PORT_BASE:-7950}" \
      DUNE_WORLD_NAME="$DUNE_WORLD_NAME" \
      DUNE_BIND_IP="$DUNE_BIND_IP" \
      DUNE_PG_PORT="$DUNE_PG_PORT" \
      DUNE_BASE_DIR="$BASE" \
  "$SCRIPTS/mock-k8s-go" "$BASE"

# mock-k8s logs its own "ready: success" line; wait for the TCP port
if wait_for_port 127.0.0.1 "${K8S_MOCK_PORT:-6443}" 15; then
  log "Mock Kubernetes API ready: success (pid $(read_pid mock-k8s))"
else
  tail -30 "$LOGS/mock-k8s.log" >&2
  die "Mock Kubernetes API failed to listen within 15s"
fi
