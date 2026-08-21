#!/usr/bin/env python3
"""Base-backup wipe guard: text surgery on Funcom's season-cleanup function.

The problem (DST's discovery, verified on our stack 2026-08-20): a base
backup is not a blob. `dune.base_backup_save` keeps the totem/building/
placeable actor rows and flips them to `actor_state.state = 'BaseBackup'`.
At Coriolis season end (the weekly Deep Desert reset),
`dune.coriolis_cleanup_partition` calls
`dune.delete_actors_and_respawns_on_server` for every map outside the
shieldwall, and that function deletes every actor EXCEPT states 'Travel',
'VehicleBackup' and 'VehicleRecovery'. 'BaseBackup' is a real ActorState
value but is missing from the list — Funcom never allowed the base backup
tool in the Deep Desert, so the case never arose for them. The moment an
operator adds DeepDesert to m_BaseBackupToolMapRestriction, the weekly wipe
eats every stored backup's actors and the tool can only offer Recycle.

The fix is ONE predicate added to that exclusion list:

    AND s.state IS DISTINCT FROM 'BaseBackup'

This module is the pure-text half: detect / insert / remove the predicate
in a `pg_get_functiondef` capture. Everything else in Funcom's body is
preserved byte-for-byte, so their future changes survive our patch. The
insertion anchors on the LAST predicate of the existing exclusion list
('VehicleRecovery') rather than a line number or a hard-coded body — if
Funcom ever renames or drops that predicate the anchor stops matching and
we FAIL CLOSED instead of guessing where to inject SQL.

The DB round-trip (read definition, CREATE OR REPLACE, re-read to verify)
lives in admin-publish.sh `base-guard-*`; a game update can ship a
migration that silently replaces the function, so the boot sequence
re-applies after migrate-db when the operator has opted in.

Ported from coastal-ms/DST-DuneServerTool v13.3.0 BaseBackupGuard.ps1
(Apache-2.0) — see ATTRIBUTION.md.
"""
from __future__ import annotations

import json
import os
import re
import sys

PREDICATE = "AND s.state IS DISTINCT FROM 'BaseBackup'"

# Boot re-apply opt-in. Same data/admin home as the other feature configs
# (chat-commands.json, welcome-kit.json); disabled by default like them.
CONFIG_PATH = "data/admin/base-guard.json"

# Whitespace-tolerant detection: a reformatted Funcom body still reads as
# "already patched" instead of getting a second copy of the predicate.
_DETECT_RE = re.compile(r"IS\s+DISTINCT\s+FROM\s+'BaseBackup'")

_ANCHOR_RE = re.compile(
    r"(?m)^([ \t]*)AND\s+s\.state\s+IS\s+DISTINCT\s+FROM\s+'VehicleRecovery'[ \t]*\r?$")

# Removal only matches the predicate on a line of its own — i.e. the exact
# shape add_predicate() produces. Anything else means a human (or another
# tool) merged it into different SQL, and blind regex surgery there could
# leave the function unparseable.
_REMOVE_LINE_RE = re.compile(
    r"(?m)^[ \t]*AND\s+s\.state\s+IS\s+DISTINCT\s+FROM\s+'BaseBackup'[ \t]*\r?\n")


def is_applied(definition: str) -> bool:
    return bool(definition) and bool(_DETECT_RE.search(definition))


def add_predicate(definition: str) -> dict:
    """Insert the guard predicate after the VehicleRecovery exclusion.

    Returns {ok, reason, definition, changed}. On any failure the input
    definition is returned unchanged — callers must never fall back to a
    hard-coded function body.
    """
    if not definition:
        return {"ok": False, "reason": "empty-definition", "definition": "",
                "changed": False}
    if is_applied(definition):
        return {"ok": True, "reason": "already-applied",
                "definition": definition, "changed": False}
    m = _ANCHOR_RE.search(definition)
    if not m:
        return {"ok": False, "reason": "anchor-not-found",
                "definition": definition, "changed": False}
    indent = m.group(1)
    # Preserve the capture's line-ending style: psql hands us LF, but a
    # definition that transited a Windows tool can be CRLF, and normalising
    # here would rewrite every following line and break the exact revert
    # round-trip.
    is_crlf = m.group(0).endswith("\r")
    eol = "\r\n" if is_crlf else "\n"
    anchor = m.group(0).rstrip("\r")
    insert = anchor + eol + indent + PREDICATE + ("\r" if is_crlf else "")
    patched = definition[:m.start()] + insert + definition[m.end():]
    return {"ok": True, "reason": "patched", "definition": patched,
            "changed": True}


def remove_predicate(definition: str) -> dict:
    """Inverse of add_predicate: drop our line, leave everything else."""
    if not definition:
        return {"ok": False, "reason": "empty-definition", "definition": "",
                "changed": False}
    if not is_applied(definition):
        return {"ok": True, "reason": "already-absent",
                "definition": definition, "changed": False}
    stripped = _REMOVE_LINE_RE.sub("", definition, count=1)
    if stripped == definition:
        return {"ok": False, "reason": "predicate-not-on-its-own-line",
                "definition": definition, "changed": False}
    return {"ok": True, "reason": "reverted", "definition": stripped,
            "changed": True}


def config_path(base: str) -> str:
    return os.path.join(base, CONFIG_PATH)


def load_enabled(base: str) -> bool:
    """Missing, unreadable or malformed config all read as DISABLED — the
    guard rewrites a Funcom-owned function, so nothing short of an explicit
    {"enabled": true} may arm the boot re-apply."""
    try:
        with open(config_path(base), encoding="utf-8") as f:
            return json.load(f).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def save_enabled(base: str, enabled: bool) -> bool:
    path = config_path(base)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def main(argv: list[str], stdin_text: str | None = None) -> int:
    """CLI for publish.sh. stdin = the function definition; stdout = the
    resulting definition (patch/unpatch). Exit codes are the contract:
    0 = did it (check: applied), 1 = check: not applied, 2 = bad usage or
    empty input, 3 = refused (reason on stderr), 4 = nothing to do (the
    input is already in the requested state; stdout echoes it unchanged).
    """
    cmd = argv[0] if argv else ""
    if cmd == "enabled":
        # No stdin: reads the boot re-apply opt-in. Exit 0 = enabled.
        if len(argv) < 2:
            print("usage: admin_baseguard.py enabled <base-dir>", file=sys.stderr)
            return 2
        return 0 if load_enabled(argv[1]) else 1
    if cmd not in ("check", "patch", "unpatch"):
        print("usage: admin_baseguard.py check|patch|unpatch < functiondef"
              "  |  enabled <base-dir>", file=sys.stderr)
        return 2
    text: str = stdin_text if stdin_text is not None else sys.stdin.read()
    if not text.strip():
        print("admin_baseguard: empty definition on stdin", file=sys.stderr)
        return 2
    if cmd == "check":
        return 0 if is_applied(text) else 1
    res = add_predicate(text) if cmd == "patch" else remove_predicate(text)
    if not res["ok"]:
        print(f"admin_baseguard: {res['reason']}", file=sys.stderr)
        return 3
    sys.stdout.write(res["definition"])
    return 0 if res["changed"] else 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
