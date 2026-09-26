#!/usr/bin/env python3
# © 2026 Sergent_val. MIT — see ATTRIBUTION.md.
"""Player denylist (issue #118), enforced BEFORE a player reaches the world.

The game has no ban command. What we do own is the FLS stub: every UE5 server
asks it `Battlegroups_IsPlayerAuthorized` on every login and every travel, before
DatabaseLogin, with the player's FLS id in the body:

    {"BattlegroupId": "sh-...", "PlayerId": "DE0BCCAA2501BF22", "RegionId": "Europe", ...}

(captured on our test server, 2026-09-26). Answering "not authorized" is what a
real FLS outage did on 2026-05-29: UE5 logged `Player unauthorized to join
server.` and refused the connection. This module is the list the stub checks.

State: server/state/admin/bans.json, {"bans": [ {fls_id, name, reason,
banned_at, expires_at|null, by} ]}, written atomically (tmp + os.replace).

FAIL-OPEN (the safety boundary): a missing / corrupt / wrong-shape file, a bad
entry, or a request body we cannot read all mean "not banned". The list gates
every login on the server, so a read error must never lock everybody out. The
stub logs what it refuses; a broken file shows up as bans silently not applying,
which `list` makes visible.

CLI (JSON out, for admin-http / admin-publish):
  admin_ban.py list <base>
  admin_ban.py ban <base> <fls_id> <duration_secs|0 for permanent> <by> <name> <reason...>
  admin_ban.py unban <base> <fls_id>
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_FLS_ID = re.compile(r"^[0-9A-F]{16}$")


def bans_path(state_dir):
    return os.path.join(state_dir, "admin", "bans.json")


def _state(base):
    return os.path.join(base, "server", "state")


def normalise_id(fls_id):
    """Uppercase, trimmed FLS id; ValueError unless it is 16 hex digits."""
    fid = str(fls_id).strip().upper()
    if not _FLS_ID.match(fid):
        raise ValueError(f"not an FLS id (16 hex digits): {fls_id!r}")
    return fid


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _read(state_dir):
    """Every well-formed entry in the file. Never raises; see FAIL-OPEN."""
    try:
        with open(bans_path(state_dir), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("bans"), list):
        return []
    out = []
    for b in raw["bans"]:
        if not isinstance(b, dict) or not isinstance(b.get("fls_id"), str):
            continue
        try:
            fid = normalise_id(b["fls_id"])
        except ValueError:
            continue
        exp = b.get("expires_at")
        if exp is not None and _parse_iso(exp) is None:
            continue  # an unreadable expiry is corruption, not "forever"
        out.append({**b, "fls_id": fid})
    return out


def _is_active(b, now):
    exp = b.get("expires_at")
    return exp is None or _parse_iso(exp) > now


def list_bans(state_dir, now=None):
    now = now or datetime.now(timezone.utc)
    return [b for b in _read(state_dir) if _is_active(b, now)]


def active_ban(state_dir, fls_id, now=None):
    """The active ban on fls_id, or None (also for an invalid id)."""
    try:
        fid = normalise_id(fls_id)
    except ValueError:
        return None
    for b in list_bans(state_dir, now):
        if b["fls_id"] == fid:
            return b
    return None


def _write(base, bans):
    p = bans_path(_state(base))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bans": bans}, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def ban(base, fls_id, name, reason, duration_secs, by, now=None):
    """Ban fls_id (permanently when duration_secs is None). A new ban on the same
    player replaces the old one. Expired entries are pruned on write."""
    now = now or datetime.now(timezone.utc)
    fid = normalise_id(fls_id)
    if duration_secs is not None and int(duration_secs) <= 0:
        raise ValueError("duration must be positive, or None for a permanent ban")
    entry = {
        "fls_id": fid,
        "name": str(name or "")[:64],
        "reason": str(reason or "")[:500],
        "banned_at": _iso(now),
        "expires_at": None if duration_secs is None else _iso(now + timedelta(seconds=int(duration_secs))),
        "by": str(by or "")[:64],
    }
    kept = [b for b in _read(_state(base)) if b["fls_id"] != fid and _is_active(b, now)]
    _write(base, kept + [entry])
    return entry


def unban(base, fls_id):
    """Lift the ban on fls_id. Returns whether there was one."""
    fid = normalise_id(fls_id)
    bans = _read(_state(base))
    kept = [b for b in bans if b["fls_id"] != fid]
    if len(kept) == len(bans):
        return False
    _write(base, kept)
    return True


def authorization_verdict(state_dir, body, now=None):
    """(authorized, ban) for a Battlegroups_IsPlayerAuthorized request body.
    Anything we cannot read is authorized: see FAIL-OPEN."""
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError, TypeError):
        return True, None
    if not isinstance(req, dict) or not isinstance(req.get("PlayerId"), str):
        return True, None
    b = active_ban(state_dir, req["PlayerId"], now)
    return (b is None), b


def _main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, base = argv[1], argv[2]
    try:
        if cmd == "list":
            print(json.dumps({"ok": True, "bans": list_bans(_state(base))}))
        elif cmd == "ban" and len(argv) >= 7:
            secs = int(argv[4])
            entry = ban(base, argv[3], name=argv[6], reason=" ".join(argv[7:]),
                        duration_secs=None if secs == 0 else secs, by=argv[5])
            print(json.dumps({"ok": True, "ban": entry}))
        elif cmd == "unban" and len(argv) >= 4:
            print(json.dumps({"ok": True, "lifted": unban(base, argv[3])}))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
