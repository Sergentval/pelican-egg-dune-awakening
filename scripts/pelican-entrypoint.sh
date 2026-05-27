#!/bin/bash
# Pelican / Wings entrypoint for Dune: Awakening.
#
# This is THIS REPO's contribution — it replaces AMP's PreStartStages with
# a single foreground command that Pelican Wings can launch.
#
# Sequence matches upstream duneawakeningstart.json:
#   prestart → start-pg → migrate-db → start-mq-admin → start-mq-game
#   → start-text-router → start-mock-k8s → start-director → start-gateway
#   → console.sh (foreground, traps SIGTERM/SIGINT)
#
# UE5 instances are NOT started here — mock-k8s spawns them on demand
# from $STATE/ondemand.ini's AlwaysWarmMaps list.
#
# Prerequisites set up by the install script + Dockerfile:
#   - Funcom OCI depot extracted under /home/container/extracted/
#   - CubeCoders scripts at /home/container/scripts/
#   - /var/run/secrets/kubernetes.io/serviceaccount/ exists and is writable
#     by the `container` user (provisioned in the Dockerfile)

set -euo pipefail

BASE="${DUNE_BASE_DIR:-/home/container}"
export DUNE_BASE_DIR="$BASE"
cd "$BASE"

# Sanity: the runtime image must pre-create the K8s ServiceAccount mount.
# AMP does this via a root-run customstart.sh hook; we do it in the
# Dockerfile so the path already exists when the unprivileged container
# user takes over.
SA_DIR="/var/run/secrets/kubernetes.io/serviceaccount"
if [ ! -d "$SA_DIR" ] || [ ! -w "$SA_DIR" ]; then
    echo "[entrypoint] [ERROR] $SA_DIR missing or not writable." >&2
    echo "[entrypoint] [ERROR] Your runtime Docker image must pre-create this" >&2
    echo "[entrypoint] [ERROR] directory with container-user ownership (see docker/Dockerfile)." >&2
    exit 1
fi

# Generate a stable mock-k8s ServiceAccount bearer token on first boot.
# mock-k8s-go validates the token against the regex ServerId=([A-Za-z0-9_+/=\-]+)
# (baked into the binary as a holdover from CubeCoders' anti-tamper check).
# Any string matching that prefix works; the suffix only needs to be stable
# across restarts so the Director's cached identity stays valid.
mkdir -p "$BASE/server/state"
SA_TOKEN_FILE="$BASE/server/state/sa-token"
if [ ! -f "$SA_TOKEN_FILE" ]; then
    { printf 'ServerId='; head -c 32 /dev/urandom | base64 -w0; } > "$SA_TOKEN_FILE"
fi
export AMP_TOKEN="$(cat "$SA_TOKEN_FILE")"

echo "[entrypoint] [INFO] Dune Awakening — Pelican boot sequence starting"
echo "[entrypoint] [INFO]   BASE=$BASE"
echo "[entrypoint] [INFO]   World title: ${DUNE_WORLD_TITLE:-<unset>}"
echo "[entrypoint] [INFO]   Region:      ${DUNE_REGION:-<unset>}"
echo "[entrypoint] [INFO]   External IP: ${DUNE_EXTERNAL_IP:-<unset>}"

bash scripts/prestart.sh         "$BASE"

# Apply panel-driven overrides to Funcom's UE5 ini files after prestart
# seeds the templates. Every boot rewrites whatever the operator changed
# in the panel; empty/unset env vars are skipped so manual edits survive.
# Full mapping (env → file/section/key) lives in scripts/apply-config.sh.
bash scripts/apply-config.sh "$BASE"

bash scripts/start-pg.sh         "$BASE"
bash scripts/migrate-db.sh       "$BASE"
bash scripts/start-mq-admin.sh   "$BASE"
bash scripts/start-mq-game.sh    "$BASE"
bash scripts/start-text-router.sh "$BASE"
bash scripts/start-mock-k8s.sh   "$BASE"
bash scripts/start-director.sh   "$BASE"
bash scripts/start-gateway.sh    "$BASE"

exec bash scripts/console.sh "$BASE"
