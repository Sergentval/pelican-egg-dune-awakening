#!/usr/bin/env python3
"""Mock-k8s instance primitives shared by admin-http.py (the interactive
/api/instances/<map>/scale endpoint) and admin_schedule.py (the scale-instance
scheduled task).

This module owns the CA-pinned mock-k8s transport (fetch_mock_status,
mock_k8s_request) and the pure ServerSetScale key resolution (resolve_scale_key).
It deliberately does NOT contain the player-online guard, the SCALABLE_MAPS
allowlist enforcement, idempotency policy, or any HTTP auth/csrf — those are
caller policy (admin-http keeps its interactive requiresConfirmation flow; the
scheduler records a "skipped" run instead). Mechanism here, policy at the call
site.

admin-http.py was the original home of these primitives; it now imports them
back and re-exports the names so its existing route code is unchanged.
"""
import json
import os
import ssl
import urllib.error
import urllib.request

# mock-k8s serves its API (incl. /status and the ServerSetScale CRD) over HTTPS.
MOCK_K8S_PORT = int(os.environ.get("K8S_MOCK_PORT", "6443"))
# In-cluster service-account mount; mock-k8s writes its self-signed ca.crt here.
SA_MOUNT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount"
# Maps that may be scaled via mock-k8s ServerSetScale replicas (on-demand maps).
SCALABLE_MAPS = ("DeepDesert_1", "SH_Arrakeen", "SH_HarkoVillage")
SCALE_REPLICAS_MAX = 4


def _mock_ssl_context():
    """CA-pinned context (mock-k8s self-signed cert carries 127.0.0.1/localhost
    SANs) with an unverified-loopback fallback when the ca.crt is absent
    (mock-k8s not up yet). Returns (ctx, ok) — ok False means pinning was
    requested but the cert failed to load, so the caller should bail."""
    ca = os.path.join(SA_MOUNT_PATH, "ca.crt")
    ctx = ssl.create_default_context()
    if os.path.exists(ca):
        try:
            ctx.load_verify_locations(ca)
        except (ssl.SSLError, OSError):
            return ctx, False
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx, True


def fetch_mock_status(port: int = MOCK_K8S_PORT, timeout: int = 5) -> "dict | None":
    """GET mock-k8s's /status (CA-pinned, unverified-loopback fallback) and return
    the parsed snapshot, or None if unreachable/unparseable. /status needs no auth.
    Used by GET /api/status + the scale paths to read per-map instance/scale state."""
    ctx, ok = _mock_ssl_context()
    if not ok:
        return None
    url = f"https://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:  # noqa: S310 (localhost, CA-pinned)
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def mock_k8s_request(method: str, path: str, body: "dict | None" = None,
                     port: int = MOCK_K8S_PORT, timeout: int = 10):
    """CA-pinned (unverified-loopback fallback) HTTPS request to mock-k8s. Returns
    (status_code, parsed_json) or (None, None) on transport failure. mock-k8s
    enforces no bearer (it trusts the container netns), so no token is sent."""
    ctx, ok = _mock_ssl_context()
    if not ok:
        return None, None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/merge-patch+json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (localhost, CA-pinned)
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def resolve_scale_key(mock_status: "dict | None", mp: str) -> "tuple[str, str, int] | None":
    """Pure. Given a mock-k8s /status snapshot, find map `mp`'s ServerSetScale
    and return (namespace, name, current_desired), or None if the map isn't
    tracked or carries no key. The key is "namespace/name"; a slash-less key
    yields (whole-string, "", desired) via str.partition — the whole string lands
    in the namespace slot and name is empty (pathological; real keys always split)."""
    if not isinstance(mock_status, dict):
        return None
    entry = next((m for m in mock_status.get("maps", []) if m.get("map") == mp), None)
    if not entry or not entry.get("key"):
        return None
    ns, _, name = str(entry["key"]).partition("/")
    return ns, name, int(entry.get("desired") or 0)
