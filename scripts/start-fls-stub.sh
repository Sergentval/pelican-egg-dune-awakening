#!/usr/bin/env bash
#
# Bootstrap + launch the FLS stub.
#
# The container's rootfs is read-only and CAP_NET_BIND_SERVICE is dropped,
# so we can't patch /etc/hosts, can't append to /etc/ssl/certs, and can't
# bind port 443. Instead the stub listens on 8443 in the writable network
# namespace, and UE5 is told to use https://127.0.0.1:8443/ as FlsHostUrl
# via a command-line override in start-ue5.sh. SSL trust is plumbed by
# pointing UE5's SSL_CERT_FILE env var at our combined CA bundle (system
# bundle copied + our self-signed appended).
#
# Director and other clients keep talking to real FLS over the internet —
# only UE5's traffic flows through the stub, only Battlegroups_IsPlayerAuthorized
# is intercepted, everything else is reverse-proxied to real FLS.
#
# State layout (everything under writable $STATE/fls-stub/):
#
#   cert.pem        self-signed for SAN=DNS:sb-retail.fls.funcom.com,IP:127.0.0.1
#   key.pem         matching private key
#   ca-bundle.pem   system CA bundle copy + cert.pem appended (for SSL_CERT_FILE)
#
# Idempotent: re-running regenerates only what's missing.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-}}"
export SOURCE="fls-stub"
source "$(dirname "$(readlink -f "$0")")/lib.sh" "$BASE"

CERT_DIR="$STATE/fls-stub"
CERT_PEM="$CERT_DIR/cert.pem"
KEY_PEM="$CERT_DIR/key.pem"
CA_BUNDLE="$CERT_DIR/ca-bundle.pem"
SYSTEM_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
FLS_HOST="sb-retail.fls.funcom.com"
STUB_PORT=8443

mkdir -p "$CERT_DIR"

# --------------------------------------------------------------------------
# 1. Generate cert if missing. The SAN list includes BOTH the FLS hostname
# (so libcurl's SNI/hostname check passes if someone hits us via the name)
# AND the loopback IP (so it passes when UE5 connects via https://127.0.0.1
# directly per the FlsHostUrl override).
if [ ! -s "$CERT_PEM" ] || [ ! -s "$KEY_PEM" ]; then
  log "Generating self-signed cert for $FLS_HOST + 127.0.0.1"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_PEM" -out "$CERT_PEM" -days 3650 \
    -subj "/CN=$FLS_HOST" \
    -addext "subjectAltName=DNS:$FLS_HOST,IP:127.0.0.1" \
    >/dev/null 2>&1
  log "  cert: $CERT_PEM"
fi

# --------------------------------------------------------------------------
# 2. Combined CA bundle for UE5's SSL_CERT_FILE. We can't write to the
# system bundle (RO rootfs), so we copy it then append our cert. UE5's
# libcurl honors SSL_CERT_FILE before falling back to the system path.
if [ ! -s "$CA_BUNDLE" ] || [ "$CERT_PEM" -nt "$CA_BUNDLE" ]; then
  log "Building combined CA bundle at $CA_BUNDLE"
  if [ -r "$SYSTEM_CA_BUNDLE" ]; then
    cp "$SYSTEM_CA_BUNDLE" "$CA_BUNDLE"
  else
    : > "$CA_BUNDLE"
  fi
  printf '\n# fls-stub self-signed for %s\n' "$FLS_HOST" >> "$CA_BUNDLE"
  cat "$CERT_PEM" >> "$CA_BUNDLE"
  log "  bundle entries: $(grep -c '^-----BEGIN CERT' "$CA_BUNDLE")"
fi

# --------------------------------------------------------------------------
# 3. Start the stub on 8443. launch_bg routes stdout/stderr through
# console.sh's classifier and writes the pid file alongside every other
# service.
log "Starting FLS stub on 127.0.0.1:$STUB_PORT (plaintext mode)"
launch_bg fls-stub "$LOGS/fls-stub.log" -- \
  /usr/bin/python3 -u "$SCRIPTS/fls-stub.py" \
  --plaintext \
  --bind 127.0.0.1 \
  --port "$STUB_PORT"

# --------------------------------------------------------------------------
# 4. Readiness probe — block downstream UE5 starts until we are accepting.
# Otherwise the first IsPlayerAuthorized call after a fast UE5 boot races
# the stub and falls back to real FLS (the very thing we're working around).
if wait_for_port 127.0.0.1 "$STUB_PORT" 10; then
  log "FLS stub ready: success (pid $(read_pid fls-stub))"
else
  tail -30 "$LOGS/fls-stub.log" >&2
  die "FLS stub failed to listen within 10s"
fi
