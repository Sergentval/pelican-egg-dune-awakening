# © 2026 Sergent_val. MIT — see ATTRIBUTION.md.
"""Redaction for the fls-stub capture mode.

The capture exists to learn which player identifiers each FLS call carries
(issue #118: the ban gate has to match on one of them). Those bodies also
carry credentials: the player's FlsServerToken, the server's ServiceAuthKey
inside its JWT, session tickets. Nothing secret may reach the log, so:

- a field whose NAME looks secret is replaced by "<redacted len=N>";
- a JWT anywhere is replaced by its claims (signature dropped), themselves
  sanitized, because the identity claims are what we are after;
- the ?code= query string (the server's own JWT) is dropped from paths.
"""
import base64
import json
import re

_SECRET_NAME = re.compile(r"token|secret|key|password|passwd|ticket|session|code|auth", re.I)
_JWT = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*$")
# Identifier-shaped names that contain a secret-looking word but are not
# secrets (e.g. "AccountId" contains no secret word, but "SessionId" does).
_ALLOWED_NAMES = {"sessionid", "authorized", "isplayerauthorized", "isauthorized"}


def _jwt_claims(value):
    """Decoded payload of a JWT-shaped string, or None."""
    if not isinstance(value, str) or not _JWT.match(value):
        return None
    try:
        payload = value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError):
        return None
    return claims if isinstance(claims, dict) else None


def sanitize(obj):
    """Return a copy of obj that is safe to log."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            claims = _jwt_claims(v)
            if claims is not None:
                out[k] = {"jwt_claims": sanitize(claims)}
            elif _SECRET_NAME.search(str(k)) and str(k).lower() not in _ALLOWED_NAMES \
                    and not isinstance(v, (dict, list, bool)) and v is not None:
                out[k] = f"<redacted len={len(str(v))}>"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    claims = _jwt_claims(obj)
    if claims is not None:
        return {"jwt_claims": sanitize(claims)}
    return obj


def sanitize_body(body):
    """Sanitized JSON body; a non-JSON body is summarised by size only."""
    if not body:
        return {}
    try:
        return sanitize(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return {"non_json_bytes": len(body)}


def safe_path(path):
    """The request path without its query string (which carries ?code=JWT)."""
    return path.split("?", 1)[0]
