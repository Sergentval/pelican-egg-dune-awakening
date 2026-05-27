#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# install.sh BASE_DIR
#
# Run by AMP's update stages after SteamCMD finishes the depot download.
# Extracts each Funcom OCI image, patchelfs the musl-linked binaries to
# point at each image's own loader, and ensures permissions are sane.
#
# Idempotent — re-run is a no-op if depot hasn't changed.
#
# Expected layout going in:
#   $BASE/depot/images/{prerequisites,battlegroup}/*.tar  (from SteamCMD app 3014830)
#
# Output:
#   $BASE/extracted/{postgres,mq,director,text-router,gateway,db-utils,game-server}/
#   each with .extracted marker.

# --------------------------------------------------------------------------
# Host-side dependency check.  We are container-OPTIONAL: nothing here is
# exotic, but a minimal/headless host may be missing patchelf, jq, gawk
# or gettext-base.  Fail early with one collated message naming the
# Debian / RHEL package per missing tool so the operator can apt/dnf
# install in one shot instead of round-tripping through later failures.
# --------------------------------------------------------------------------
declare -A DUNE_DEP_PKG=(
  [patchelf]="patchelf"
  [jq]="jq"
  [tar]="tar"
  [gzip]="gzip"
  [base64]="coreutils"
  [file]="file"
  [openssl]="openssl"
  [python3]="python3"
  [gawk]="gawk"
  [sed]="sed"
  [grep]="grep"
  [ip]="iproute2 (Debian) / iproute (RHEL)"
  [setsid]="util-linux"
)
DUNE_MISSING=()
for cmd in "${!DUNE_DEP_PKG[@]}"; do
  command -v "$cmd" >/dev/null 2>&1 || DUNE_MISSING+=("  $cmd  (package: ${DUNE_DEP_PKG[$cmd]})")
done
if [ ${#DUNE_MISSING[@]} -gt 0 ]; then
  printf '\n*** Missing host dependencies — install these before retrying: ***\n' >&2
  printf '%s\n' "${DUNE_MISSING[@]}" >&2
  printf '\nDebian/Ubuntu:  sudo apt-get install <pkg> ...\nRHEL/Alma:      sudo dnf install <pkg> ...\n\n' >&2
  exit 1
fi

export SOURCE="update"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$@"

log "Extracting Funcom server binaries. This may take several minutes without visible activity."


[ -d "$DEPOT/images" ] || die "depot images missing — was the SteamCMD download successful?"


mkdir -p "$EXTRACTED"

# --------------------------------------------------------------------------
# 1. Extract each OCI image's layers in order
# --------------------------------------------------------------------------
extract_oci() {
  local name=$1 tarball=$2
  local out="$EXTRACTED/$name"
  if [ -f "$out/.extracted" ] && [ "$tarball" -ot "$out/.extracted" ]; then
    log "  [$name] already extracted, skipping"
    return
  fi
  log "  [$name] extracting from $(basename "$tarball")"
  rm -rf "$out"
  mkdir -p "$out/.oci"
  tar -xf "$tarball" -C "$out/.oci"
  local layers
  layers=$(jq -r '.[0].Layers[]' "$out/.oci/manifest.json")
  for l in $layers; do
    tar -xf "$out/.oci/$l" -C "$out/" 2>/dev/null || true
  done
  rm -rf "$out/.oci"
  touch "$out/.extracted"
}

extract_oci postgres    "$DEPOT/images/prerequisites/igw-postgres.tar"
extract_oci mq          "$DEPOT/images/battlegroup/server-rabbitmq.tar"
extract_oci director    "$DEPOT/images/battlegroup/server-bg-director.tar"
extract_oci text-router "$DEPOT/images/battlegroup/server-text-router.tar"
extract_oci gateway     "$DEPOT/images/battlegroup/server-gateway.tar"
extract_oci db-utils    "$DEPOT/images/battlegroup/server-db-utils.tar"
extract_oci game-server "$DEPOT/images/battlegroup/server.tar"

# --------------------------------------------------------------------------
# 2. patchelf the musl loader onto each main executable
# --------------------------------------------------------------------------
log "Adjusting binary loaders for non-container hosts..."

patch_interp() {
  local rootname=$1 binrel=$2
  local r; r=$(rootfs "$rootname")
  local bin="$r$binrel"
  [ -f "$bin" ] || { warn "    missing: $bin"; return; }
  patchelf --set-interpreter "$r/lib/ld-musl-x86_64.so.1" "$bin" 2>/dev/null || true
  log "    patched: $rootname$binrel"
}

# Core service binaries
patch_interp director    /Tools/Battlegroups/Director/BattlegroupDirector/Director
patch_interp text-router /Tools/Battlegroups/TextRouter/TextRouter/TextRouter
patch_interp gateway     /usr/local/bin/python3.12
patch_interp db-utils    /usr/local/bin/python3.12
patch_interp mq          /opt/erlang/lib/erlang/erts-14.2.5.12/bin/beam.smp
patch_interp postgres    /usr/local/bin/postgres

# Postgres helpers
for pgbin in initdb psql createdb createuser pg_ctl pg_isready pg_dump pg_restore; do
  patch_interp postgres "/usr/local/bin/$pgbin"
done

# Erlang ERTS helpers (memsup, cpu_sup, erlexec are all musl-linked)
ERTS_BIN=$(rootfs mq)/opt/erlang/lib/erlang/erts-14.2.5.12/bin
if [ -d "$ERTS_BIN" ]; then
  for b in erlexec ct_run dialyzer dyn_erl epmd erlc erl_call erl_child_setup \
           escript heart inet_gethost run_erl typer yielding_c_fun; do
    if [ -f "$ERTS_BIN/$b" ] && file "$ERTS_BIN/$b" 2>/dev/null | grep -q musl; then
      patchelf --set-interpreter "$(rootfs mq)/lib/ld-musl-x86_64.so.1" "$ERTS_BIN/$b" 2>/dev/null && \
        log "    patched: erts/$b"
    fi
  done
fi

# os_mon priv helpers — required for RMQ broker boot (memsup/cpu_sup)
OSMON=$(rootfs mq)/opt/erlang/lib/erlang/lib/os_mon-2.9.1/priv/bin
for b in memsup cpu_sup; do
  if [ -f "$OSMON/$b" ] && file "$OSMON/$b" 2>/dev/null | grep -q musl; then
    patchelf --set-interpreter "$(rootfs mq)/lib/ld-musl-x86_64.so.1" "$OSMON/$b" 2>/dev/null && \
      log "    patched: os_mon/$b"
  fi
done

# Game server: glibc binary — no patch needed normally; check anyway
SGBIN=$(rootfs game-server)/home/dune/server/DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping
if [ -f "$SGBIN" ] && file "$SGBIN" 2>/dev/null | grep -q ld-musl; then
  patchelf --set-interpreter "$(rootfs game-server)/lib/ld-musl-x86_64.so.1" "$SGBIN" 2>/dev/null
  log "    patched (musl): game-server/DuneSandboxServer"
else
  log "    glibc-linked: game-server/DuneSandboxServer (no patch needed)"
fi

# --------------------------------------------------------------------------
# 2a. Ensure the panel-editable Bgd.* keys are present in Funcom's
# UserEngine.ini template. Funcom's stock template doesn't ship them at all,
# and scripts/apply-config.sh runs in update-only mode (it rewrites lines
# but doesn't insert top-level [ConsoleVariables] keys from nothing). So we
# pre-seed the lines here, on the depot template, before any state file is
# generated from it. Idempotent: won't double-add.
# --------------------------------------------------------------------------
UE_TPL="$DEPOT/scripts/setup/config/UserEngine.ini"
if [ -f "$UE_TPL" ] && ! grep -q '^Bgd\.ServerDisplayName=' "$UE_TPL"; then
  log "Activating panel-editable Bgd.* keys in UserEngine.ini template..."
  # Strip legacy commented forms first (in case Funcom adds them later),
  # then insert under [ConsoleVariables].
  sed -i -E \
    -e 's/^[[:space:]]*;[[:space:]]*(Bgd\.ServerDisplayName[[:space:]]*=)/\1/' \
    -e 's/^[[:space:]]*;[[:space:]]*(Bgd\.ServerLoginPassword[[:space:]]*=)/\1/' \
    "$UE_TPL"
  if ! grep -q '^Bgd\.ServerDisplayName=' "$UE_TPL"; then
    sed -i '/^\[ConsoleVariables\]/a Bgd.ServerDisplayName="Sietch Cubern"\nBgd.ServerLoginPassword=' "$UE_TPL"
  fi
fi

# --------------------------------------------------------------------------
# 2b. Seed the state-side INIs so apply-config.sh has files to merge into
# on the very first start. Without this, apply-config.sh would log "target
# missing" warnings on first boot and panel variables would silently fail
# to apply until restart #2. Idempotent: never overwrites an existing
# (admin-edited) state file.
# --------------------------------------------------------------------------
STATE_US="$STATE/ue5-saved/UserSettings"
mkdir -p "$STATE_US" 2>/dev/null || true
for f in UserEngine.ini UserGame.ini; do
  if [ ! -f "$STATE_US/$f" ] && [ -f "$DEPOT/scripts/setup/config/$f" ]; then
    cp "$DEPOT/scripts/setup/config/$f" "$STATE_US/$f"
    log "  seeded state admin file: $f"
  fi
done
if [ ! -f "$STATE/ondemand.ini" ]; then
  mkdir -p "$STATE" 2>/dev/null || true
  printf '%s\n' '; On-Demand Instancing - consumed by mock-k8s.' \
    'MaxConcurrentInstances=8' 'AutomaticStopDuration=10m' \
    'AlwaysWarmMaps=Survival_1,Overmap,DeepDesert_1' > "$STATE/ondemand.ini"
  log "  seeded state admin file: ondemand.ini"
fi

# --------------------------------------------------------------------------
# 3. Ownership/perms — game-server has UIDs from the Alpine image that
# collide with random host users.  Reset everything to the current uid/gid.
# --------------------------------------------------------------------------
log "Setting file ownership..."
chown -R "$(id -u):$(id -g)" "$EXTRACTED/game-server" 2>/dev/null || true
chmod -R u+rwX "$EXTRACTED" 2>/dev/null || true

# Note: under Pelican Wings the K8s ServiceAccount mount at
# /var/run/secrets/kubernetes.io/serviceaccount is pre-created by the
# runtime Docker image (see docker/Dockerfile: VOLUME + 0777 mkdir). The
# root-run customstart.sh hook that AMP used here is no longer needed
# and the file has been deleted.

log "Update complete: success."
