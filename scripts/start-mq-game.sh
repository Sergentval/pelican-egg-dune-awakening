#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-mq-game.sh BASE_DIR
#
# Launches the "game" RabbitMQ broker — TLS-only (AMQPS).  This is what
# external clients hit for game messaging.  Binds to $DUNE_BIND_IP (the
# container's externally-routable interface).

export SOURCE="mq-game"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Starting RabbitMQ game broker on $DUNE_BIND_IP:$DUNE_MQ_GAME_PORT (TLS)..."

MQ=$(rootfs mq)
HOME_DIR="$RUNTIME/mq-game-home"
mkdir -p "$HOME_DIR/mnesia" "$HOME_DIR/log" "$HOME_DIR/plugins-expand"

echo '[rabbitmq_management,rabbitmq_prometheus,rabbitmq_auth_backend_http,rabbitmq_auth_backend_cache].' \
  > "$RUNTIME/mq-game/enabled_plugins"

env -i HOME="$HOME_DIR" LC_ALL=C \
  LD_LIBRARY_PATH=$(ldlib_mq) \
  ERL_EPMD_ADDRESS=127.0.0.1 \
  ERL_ROOTDIR="$MQ/opt/erlang/lib/erlang" \
  PATH="$MQ/opt/rabbitmq/sbin:$MQ/opt/erlang/bin:$MQ/opt/openssl/bin:/usr/local/bin:/usr/bin:/bin" \
  RABBITMQ_NODENAME="rabbit-game@localhost" \
  RABBITMQ_NODE_IP_ADDRESS="$DUNE_BIND_IP" \
  RABBITMQ_DIST_PORT=25673 \
  RABBITMQ_NODE_PORT="$DUNE_MQ_GAME_PORT" \
  RABBITMQ_MNESIA_BASE="$HOME_DIR/mnesia" \
  RABBITMQ_LOG_BASE="$HOME_DIR/log" \
  RABBITMQ_PLUGINS_EXPAND_DIR="$HOME_DIR/plugins-expand" \
  RABBITMQ_ENABLED_PLUGINS_FILE="$RUNTIME/mq-game/enabled_plugins" \
  RABBITMQ_CONFIG_FILE="$RUNTIME/mq-game/rabbitmq" \
  RABBITMQ_PID_FILE="$RUNTIME/pids/mq-game-rmq.pid" \
  RABBITMQ_HOME="$MQ/opt/rabbitmq" \
  RUNNING_UNDER_SYSTEMD=true \
  setsid "$MQ/opt/rabbitmq/sbin/rabbitmq-server" \
  >>"$LOGS/mq-game.log" 2>&1 < /dev/null &

write_pid mq-game $!
disown

if wait_for_port "$DUNE_BIND_IP" "$DUNE_MQ_GAME_PORT" 60; then
  log "RabbitMQ game broker ready: success (pid $(read_pid mq-game))"
else
  tail -50 "$LOGS/mq-game.log" >&2
  die "RabbitMQ game broker failed to listen within 60s"
fi
