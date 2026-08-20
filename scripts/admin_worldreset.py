#!/usr/bin/env python3
"""World-reset marker/state engine (DST worldreset-2 port, reshaped).

DST's reversible world restart replaces the Postgres StatefulSet storage on
Funcom's K3s stack after a verified logical backup, with an automatic
rollback on any ambiguous failure. On this single-container stack the same
operation is both simpler and MORE reversible:

  * The whole world lives in the Postgres datadir (`server/state/pg`);
    prestart.sh initdb's + loads the schema whenever that dir is fresh —
    the ordinary first-boot path, proven by every install.
  * So a reset is: verified `db-backup` + optional per-character backups,
    then a DURABLE marker; the boot hook (apply-world-reset.sh, ordered
    BEFORE prestart) consumes the marker and moves `pg` aside to
    `pg.pre-reset-<ts>` — never deletes it. The next boot builds a fresh
    empty world under the same battlegroup identity and config.
  * Rollback is a datadir swap back — no restore-from-dump on the critical
    path; the logical backup is the documented second line of defence.
  * Nothing mutates while the server runs, and a marker that fails
    verification at boot (backup missing/short) aborts the wipe and boots
    the OLD world untouched — fail closed, like DST's admission checks.

This module is the pure file-state half: the markers the arm subcommands
write and the boot hook consumes, plus the preserved-datadir bookkeeping.
Ported from coastal-ms/DST-DuneServerTool WorldRestart.ps1 (Apache-2.0) —
see ATTRIBUTION.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

PENDING = "server/state/world-reset-pending.json"
ROLLBACK = "server/state/world-rollback-pending.json"
RESULT = "server/state/world-reset-last.json"

# Preserved datadirs live next to server/state/pg and match exactly this —
# anything else (including "pg" itself) is never a rollback target.
_PRESERVED_RE = re.compile(r"^pg\.pre-reset-\d{8}-\d{6}$")

# Backup files are plain basenames under $BASE/backups/ — the boot hook
# joins and trusts the result, so a separator or traversal never validates.
_BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def pending_path(base: str) -> str:
    return os.path.join(base, PENDING)


def rollback_path(base: str) -> str:
    return os.path.join(base, ROLLBACK)


def result_path(base: str) -> str:
    return os.path.join(base, RESULT)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# ---- reset marker ---------------------------------------------------------

def arm_reset(base: str, backup_file: str, backup_bytes: int,
              char_backups: list[str]) -> bool:
    """Write the reset marker — only after re-proving the backup it names
    exists with the exact recorded size. The boot hook verifies AGAIN before
    touching anything; both checks fail closed."""
    if not backup_file or not _BACKUP_NAME_RE.match(backup_file):
        return False
    path = os.path.join(base, "backups", backup_file)
    try:
        if os.path.getsize(path) != int(backup_bytes) or int(backup_bytes) <= 0:
            return False
    except OSError:
        return False
    return _write_json(pending_path(base), {
        "backup_file": backup_file,
        "backup_bytes": int(backup_bytes),
        "char_backups": list(char_backups),
        "requested_at": int(time.time()),
    })


def read_pending(base: str) -> dict | None:
    data = _read_json(pending_path(base))
    if not data or not isinstance(data.get("backup_file"), str):
        return None
    return data


def clear_pending(base: str) -> bool:
    return _remove(pending_path(base))


def verify_pending(base: str, pending: dict) -> str:
    """'' when the marker's backup still checks out; otherwise the refusal
    reason. Run by the boot hook immediately before the wipe."""
    name = str(pending.get("backup_file") or "")
    if not name or not _BACKUP_NAME_RE.match(name):
        return "backup file name is invalid"
    path = os.path.join(base, "backups", name)
    try:
        size = os.path.getsize(path)
    except OSError:
        return f"backup file {name} is missing"
    if size != int(pending.get("backup_bytes") or -1):
        return f"backup file {name} size changed ({size} bytes vs recorded {pending.get('backup_bytes')})"
    return ""


# ---- rollback marker ------------------------------------------------------

def preserved_dirs(base: str) -> list[str]:
    """pg.pre-reset-<ts> dirs that look like real datadirs, newest first."""
    state = os.path.join(base, "server", "state")
    out = []
    try:
        names = os.listdir(state)
    except OSError:
        return []
    for name in names:
        if not _PRESERVED_RE.match(name):
            continue
        if os.path.isfile(os.path.join(state, name, "data", "PG_VERSION")):
            out.append(name)
    return sorted(out, reverse=True)


def arm_rollback(base: str, restore_dir: str) -> bool:
    if not _PRESERVED_RE.match(restore_dir or ""):
        return False
    if restore_dir not in preserved_dirs(base):
        return False
    return _write_json(rollback_path(base), {
        "restore_dir": restore_dir,
        "requested_at": int(time.time()),
    })


def read_rollback(base: str) -> dict | None:
    data = _read_json(rollback_path(base))
    if not data or not isinstance(data.get("restore_dir"), str):
        return None
    return data


def clear_rollback(base: str) -> bool:
    return _remove(rollback_path(base))


# ---- boot result ----------------------------------------------------------

def write_result(base: str, operation: str, ok: bool, detail: str,
                 preserved: str = "") -> bool:
    return _write_json(result_path(base), {
        "operation": operation,
        "ok": bool(ok),
        "detail": detail,
        "preserved": preserved,
        "at": int(time.time()),
    })


def read_result(base: str) -> dict | None:
    return _read_json(result_path(base))


# ---- CLI (for the bash layers) --------------------------------------------

def main(argv: list[str]) -> int:
    """Exit codes: 0 ok, 1 negative/absent, 2 bad usage or refused.
    Subcommands print JSON (or nothing) on stdout; bash parses stdout and
    branches on the code."""
    cmd = argv[0] if argv else ""
    if cmd == "pending" and len(argv) == 2:
        data = read_pending(argv[1])
        if data is None:
            return 1
        print(json.dumps(data))
        return 0
    if cmd == "rollback-pending" and len(argv) == 2:
        data = read_rollback(argv[1])
        if data is None:
            return 1
        print(json.dumps(data))
        return 0
    if cmd == "result" and len(argv) == 2:
        data = read_result(argv[1])
        if data is None:
            return 1
        print(json.dumps(data))
        return 0
    if cmd == "preserved" and len(argv) == 2:
        print(json.dumps(preserved_dirs(argv[1])))
        return 0
    if cmd == "arm-reset" and len(argv) == 5:
        base, backup_file, backup_bytes = argv[1], argv[2], argv[3]
        chars = [c for c in argv[4].split(",") if c]
        try:
            size = int(backup_bytes)
        except ValueError:
            return 2
        return 0 if arm_reset(base, backup_file, size, chars) else 2
    if cmd == "arm-rollback" and len(argv) == 3:
        return 0 if arm_rollback(argv[1], argv[2]) else 2
    if cmd == "verify-pending" and len(argv) == 2:
        pending = read_pending(argv[1])
        if pending is None:
            print("no reset marker", file=sys.stderr)
            return 1
        reason = verify_pending(argv[1], pending)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        return 0
    if cmd == "clear-pending" and len(argv) == 2:
        return 0 if clear_pending(argv[1]) else 2
    if cmd == "clear-rollback" and len(argv) == 2:
        return 0 if clear_rollback(argv[1]) else 2
    if cmd == "write-result" and len(argv) in (5, 6):
        base, op, ok_s, detail = argv[1], argv[2], argv[3], argv[4]
        preserved = argv[5] if len(argv) == 6 else ""
        return 0 if write_result(base, op, ok_s == "ok", detail, preserved) else 2
    print("usage: admin_worldreset.py pending|rollback-pending|result|preserved|"
          "verify-pending|clear-pending|clear-rollback <base>"
          " | arm-reset <base> <backup_file> <bytes> <chars,csv>"
          " | arm-rollback <base> <dir>"
          " | write-result <base> <op> ok|fail <detail> [preserved]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
