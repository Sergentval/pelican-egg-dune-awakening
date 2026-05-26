#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# start-mq-admin.sh BASE_DIR
#
# Launches the "admin" RabbitMQ broker — plain AMQP, internal-only.
# Used by director/gateway/text-router for inter-service messaging.

export SOURCE="mq-admin"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Starting RabbitMQ admin broker on 127.0.0.1:$DUNE_MQ_ADMIN_PORT..."

MQ=$(rootfs mq)
HOME_DIR="$RUNTIME/mq-admin-home"
mkdir -p "$HOME_DIR/mnesia" "$HOME_DIR/log" "$HOME_DIR/plugins-expand"

# rabbitmq looks for $RABBITMQ_CONFIG_FILE.conf — drop the trailing .conf
echo '[rabbitmq_management,rabbitmq_prometheus,rabbitmq_auth_backend_http,rabbitmq_auth_backend_cache].' \
  > "$RUNTIME/mq-admin/enabled_plugins"

env -i HOME="$HOME_DIR" LC_ALL=C \
  LD_LIBRARY_PATH=$(ldlib_mq) \
  ERL_EPMD_ADDRESS=127.0.0.1 \
  ERL_ROOTDIR="$MQ/opt/erlang/lib/erlang" \
  PATH="$MQ/opt/rabbitmq/sbin:$MQ/opt/erlang/bin:$MQ/opt/openssl/bin:/usr/local/bin:/usr/bin:/bin" \
  RABBITMQ_NODENAME="rabbit-admin@localhost" \
  RABBITMQ_NODE_IP_ADDRESS=127.0.0.1 \
  RABBITMQ_DIST_PORT=25672 \
  RABBITMQ_NODE_PORT="$DUNE_MQ_ADMIN_PORT" \
  RABBITMQ_MNESIA_BASE="$HOME_DIR/mnesia" \
  RABBITMQ_LOG_BASE="$HOME_DIR/log" \
  RABBITMQ_PLUGINS_EXPAND_DIR="$HOME_DIR/plugins-expand" \
  RABBITMQ_ENABLED_PLUGINS_FILE="$RUNTIME/mq-admin/enabled_plugins" \
  RABBITMQ_CONFIG_FILE="$RUNTIME/mq-admin/rabbitmq" \
  RABBITMQ_PID_FILE="$RUNTIME/pids/mq-admin-rmq.pid" \
  RABBITMQ_HOME="$MQ/opt/rabbitmq" \
  RUNNING_UNDER_SYSTEMD=true \
  setsid "$MQ/opt/rabbitmq/sbin/rabbitmq-server" \
  >>"$LOGS/mq-admin.log" 2>&1 < /dev/null &

write_pid mq-admin $!
disown

if wait_for_port 127.0.0.1 "$DUNE_MQ_ADMIN_PORT" 60; then
  log "RabbitMQ admin broker ready: success (pid $(read_pid mq-admin))"
else
  tail -50 "$LOGS/mq-admin.log" >&2
  die "RabbitMQ admin broker failed to listen within 60s"
fi
