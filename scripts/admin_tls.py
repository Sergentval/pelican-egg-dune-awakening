#!/usr/bin/env python3
"""TLS termination for the admin panel.

admin-http has always spoken plain HTTP, leaving TLS to whatever fronts it.
That is right for a proxied deployment and useless for an operator who
points a DNS record straight at an exposed box: there is nothing in front,
so there is no https:// to reach, and a domain set without one produces a
login that answers 200 and bounces (see DUNE_ADMIN_UI_TLS).

This serves TLS directly from a certificate on disk. Where that certificate
comes from is deliberately not this module's problem — an operator copying
one in, a host-side certbot renewing into the same path, or the ACME client
that writes there — so all of those work without changing anything here.

The certificate is re-read when it changes on disk. A renewal must not
require restarting the panel: the whole point of automating renewal is that
nobody is watching when it happens.
"""
from __future__ import annotations

import os
import ssl
import subprocess
import threading
import time

# Where a renewal is expected to land. Under server/state/ so it survives a
# reinstall along with the rest of the operator's state.
DEFAULT_CERT = "server/state/tls/fullchain.pem"
DEFAULT_KEY = "server/state/tls/privkey.pem"
RELOAD_POLL_SECS = 60


def cert_paths(base: str) -> tuple[str, str]:
    """(cert, key), from the environment or the conventional location."""
    cert = (os.environ.get("DUNE_ADMIN_UI_TLS_CERT") or "").strip()
    key = (os.environ.get("DUNE_ADMIN_UI_TLS_KEY") or "").strip()
    return (cert or os.path.join(base, DEFAULT_CERT),
            key or os.path.join(base, DEFAULT_KEY))


def available(base: str) -> bool:
    cert, key = cert_paths(base)
    return os.path.isfile(cert) and os.path.isfile(key)


def _stamp(paths) -> tuple:
    """Cheap change detector: (mtime, size) per file. A renewal rewrites
    both, and comparing content would mean reading a key we do not need to
    hold in memory any longer than the handshake requires."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def build_context(base: str, log=print):
    """An SSLContext for the panel, or None when no usable certificate is
    on disk. A malformed pair is reported and treated as absent — serving
    plain HTTP is recoverable, refusing to boot is not."""
    cert, key = cert_paths(base)
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as exc:
        log(f"WARN TLS certificate at {cert} unusable ({exc}) — serving plain HTTP")
        return None
    return ctx


def watch(base: str, ctx, log=print, poll: int = RELOAD_POLL_SECS) -> threading.Thread:
    """Reload `ctx` in place when the files change, so a renewal takes
    effect without a restart. Reloading an SSLContext affects handshakes
    from that point on; connections already up are untouched."""
    cert, key = cert_paths(base)
    # Baseline taken here, synchronously, not inside the thread: a renewal
    # landing between build_context() and the thread getting scheduled would
    # otherwise become the baseline and never be noticed at all.
    baseline = _stamp((cert, key))

    def loop() -> None:
        last = baseline
        while True:
            time.sleep(poll)
            now = _stamp((cert, key))
            if now == last or None in now:
                continue
            last = now
            try:
                ctx.load_cert_chain(cert, key)
                log(f"TLS certificate reloaded from {cert}")
            except (ssl.SSLError, OSError) as exc:
                # Keep serving with the certificate we already hold: a
                # half-written renewal must not take the panel down.
                log(f"WARN reloaded TLS certificate rejected ({exc}) — keeping the previous one")

    t = threading.Thread(target=loop, daemon=True, name="tls-reload")
    t.start()
    return t


def describe(base: str) -> str:
    """One line for the boot banner."""
    cert, _key = cert_paths(base)
    try:
        out = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-subject", "-enddate"],
                             capture_output=True, text=True, timeout=10)
        fields = dict(
            line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line)
        subject = (fields.get("subject") or "").strip()
        expiry = (fields.get("notAfter") or "").strip()
        return f"serving TLS ({subject or cert}), expires {expiry or 'unknown'}"
    except (OSError, ValueError, subprocess.SubprocessError):
        return f"serving TLS from {cert}"
