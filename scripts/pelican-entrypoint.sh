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

# Inject dimensional partitions into world-template.yaml so the
# BattleGroup CR mock-k8s exposes to Director includes them. Must come
# before start-mock-k8s.sh because mock-k8s reads the YAML once at boot.
bash scripts/patch-world-template.sh "$BASE"

bash scripts/start-pg.sh         "$BASE"
bash scripts/migrate-db.sh       "$BASE"
bash scripts/start-mq-admin.sh   "$BASE"
bash scripts/start-mq-game.sh    "$BASE"
bash scripts/start-text-router.sh "$BASE"
# fls-stub must come before mock-k8s — mock-k8s pre-spawns AlwaysWarmMaps
# (Survival_1, Overmap, DeepDesert_1) immediately at startup, and those
# UE5 instances call FLS Battlegroups_IsPlayerAuthorized on first travel
# attempt. If the stub isn't listening yet they fall back to real FLS,
# which returns 500 "Invalid JSON" for self-hosted JWTs and blocks travel.
bash scripts/start-fls-stub.sh   "$BASE"
bash scripts/start-mock-k8s.sh   "$BASE"
bash scripts/start-director.sh   "$BASE"
bash scripts/start-gateway.sh    "$BASE"
bash scripts/start-admin-http.sh "$BASE"
# Welcome-kit scanner (Phase 6). No-op while welcome-kit.json enabled:false, so
# always safe to launch; grants run only after an operator enables a package.
bash scripts/start-welcome-scanner.sh "$BASE"
# Unattended scheduler (auto-restart + auto-backup). No-op while both tasks are
# disabled in data/admin/schedule.json, so always safe to launch.
bash scripts/start-scheduler.sh "$BASE"
# Market-bot autonomous loop (7b-3). No-op while market-bot.json enabled:false,
# so always safe to launch; tops up listings + gamble-buys only once armed.
bash scripts/start-market-bot.sh "$BASE"
# Demand-based autoscaler. No-op while autoscaler.json enabled:false, so always
# safe to launch; drains idle on-demand maps + wakes/load-scales them once armed.
bash scripts/start-autoscaler.sh "$BASE"

# Spawn dimensional UE5 partitions for cross-Sietch travel destinations.
# prestart.sh has already seeded the dim>0 world_partition rows; this
# script materializes each as a UE5 process and wires server_id back to
# the row. Backgrounded because the warm partitions are already up by
# now, and the dimensional ones can finish booting while console.sh
# starts watching logs. Combined with IGNORE_IGWO_API_SERVER_CHECK on
# Director (start-director.sh), Sandstorm travel to DD / Arrakeen /
# HarkoVillage allocates correctly instead of returning Code 17
# (TravelResponseCode.DestinationTemporarilyUnavailable). See
# wiki/syntheses/dune-director-ignore-igwo-fix-2026-05-29.md.
bash scripts/start-ue5-dimensions.sh "$BASE" &

exec bash scripts/console.sh "$BASE"
