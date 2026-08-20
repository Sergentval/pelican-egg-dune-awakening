#!/usr/bin/env python3
"""Character backup/restore — file + metadata layer (native transfer subsystem).

The dune.character_transfer_export / character_transfer_import SQL runs in
admin-publish.sh (the only layer with the Postgres connection); this module is
the pure, unit-tested logic around it. A backup is TWO files under
$BASE/backups/char/:

  char-<FLS>-<YYYYMMDD-HHMMSS>.json        raw character_transfer_export jsonb
  char-<FLS>-<YYYYMMDD-HHMMSS>.meta.json   {fls, character_name, action, reason,
                                            patches_checksum, bytes, created_at}

The zero-padded timestamp makes lexical sort == chronological sort (same trick
as admin_backup.py). '_patches_checksum' is recorded at backup time because
character_transfer_import refuses a mismatched game patch — the operator gets
a clear pre-restore signal instead of the proc's raw error.

Mechanism ported from Icehunter/dune-admin v0.46.0 (MIT) — see ATTRIBUTION.md.
"""
import json
import os
import re
import sys

BACKUP_RE = re.compile(r"^char-([0-9A-Fa-f]{6,32})-(\d{8}-\d{6})\.json$")


def backup_name(fls: str, ts: str) -> str:
    """Filename for a backup of `fls` taken at `ts` ('YYYYMMDD-HHMMSS')."""
    return f"char-{fls}-{ts}.json"


def is_backup(name: str) -> bool:
    """True iff `name` is a canonical character-backup filename (data file,
    not the .meta.json sidecar, no path components)."""
    return bool(BACKUP_RE.match(name))


def meta_name(backup: str) -> str:
    """Sidecar metadata filename for a backup data file."""
    return backup[: -len(".json")] + ".meta.json"


def fls_of(backup: str) -> str:
    """FLS id embedded in a canonical backup filename ('' if not canonical)."""
    m = BACKUP_RE.match(backup)
    return m.group(1) if m else ""


def checksum_of(export_text: str) -> str:
    """'_patches_checksum' from a character_transfer_export result. A missing
    field is not an error (returns '') — only malformed JSON is, since that
    means the export itself is unusable."""
    try:
        doc = json.loads(export_text)
    except ValueError as exc:
        raise ValueError(f"invalid transfer export json: {exc}") from exc
    v = doc.get("_patches_checksum", "") if isinstance(doc, dict) else ""
    return v if isinstance(v, str) else ""


def write_meta(dirpath: str, backup: str, fls: str, character_name: str,
               action: str, reason: str) -> dict:
    """Validate the freshly-written backup file and record its sidecar
    metadata. Raises ValueError on a malformed export — a backup an admin
    can't trust is worse than no backup at all."""
    path = os.path.join(dirpath, backup)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    checksum = checksum_of(text)  # raises on malformed export
    meta = {
        "fls": fls,
        "character_name": character_name,
        "action": action,
        "reason": reason,
        "patches_checksum": checksum,
        "bytes": os.path.getsize(path),
        "created_at": _mtime_iso(path),
    }
    tmp = os.path.join(dirpath, meta_name(backup) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
        f.write("\n")
    os.replace(tmp, os.path.join(dirpath, meta_name(backup)))
    return meta


def _mtime_iso(path: str) -> str:
    import datetime
    ts = os.path.getmtime(path)
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_backups(dirpath: str, fls: "str | None" = None) -> "list[dict]":
    """Backups (newest first) with their sidecar metadata merged in. A missing
    or unreadable sidecar degrades to blank fields rather than hiding the
    backup — the data file is the thing that matters."""
    try:
        names = sorted((n for n in os.listdir(dirpath) if is_backup(n)), reverse=True)
    except OSError:
        return []
    rows = []
    for n in names:
        if fls and fls_of(n).lower() != fls.lower():
            continue
        row = {"file": n, "fls": fls_of(n), "character_name": "", "action": "",
               "reason": "", "patches_checksum": "", "bytes": 0, "created_at": ""}
        try:
            with open(os.path.join(dirpath, meta_name(n)), encoding="utf-8") as f:
                meta = json.load(f)
            if isinstance(meta, dict):
                row.update({k: meta[k] for k in row if k in meta and k != "file"})
        except (OSError, ValueError):
            pass
        try:
            row["bytes"] = os.path.getsize(os.path.join(dirpath, n))
        except OSError:
            pass
        rows.append(row)
    return rows


def select_for_prune(names: "list[str]", keep: int) -> "list[str]":
    """Backups to delete so each PLAYER keeps its newest `keep`. Per-player
    grouping is the point: pruning after player A's new backup must never
    delete player B's only one."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    by_fls: "dict[str, list[str]]" = {}
    for n in sorted(names, reverse=True):
        if is_backup(n):
            by_fls.setdefault(fls_of(n).lower(), []).append(n)
    doomed: "list[str]" = []
    for group in by_fls.values():
        doomed.extend(group[keep:])
    return sorted(doomed)


def safe_backup_path(dirpath: str, name: str) -> "str | None":
    """Absolute path of an EXISTING canonical backup inside `dirpath`, or None.
    The HTTP layer passes operator-supplied names through here — allowlist
    shape + basename + realpath containment, same policy as admin_logs."""
    if not is_backup(name) or os.path.basename(name) != name:
        return None
    path = os.path.join(dirpath, name)
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(dirpath):
        return None
    return path if os.path.isfile(path) else None


def _main(argv: "list[str]") -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "meta" and len(argv) >= 7:
        # meta <dir> <backup> <fls> <name> <action> [reason]
        reason = argv[7] if len(argv) > 7 else ""
        try:
            meta = write_meta(argv[2], argv[3], argv[4], argv[5], argv[6], reason)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps({"ok": True, "meta": meta}))
        return 0
    if cmd == "list" and len(argv) >= 3:
        # list <dir> [fls]
        print(json.dumps({"ok": True,
                          "backups": list_backups(argv[2], argv[3] if len(argv) > 3 else None)}))
        return 0
    if cmd == "prune" and len(argv) >= 4:
        # prune <dir> <keep> — delete data+meta of pruned backups
        try:
            keep = int(argv[3])
            names = os.listdir(argv[2]) if os.path.isdir(argv[2]) else []
            doomed = select_for_prune(names, keep)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        for n in doomed:
            for f in (n, meta_name(n)):
                try:
                    os.remove(os.path.join(argv[2], f))
                except OSError:
                    pass
        print(json.dumps({"ok": True, "pruned": doomed}))
        return 0
    if cmd == "path" and len(argv) >= 4:
        # path <dir> <name> — safe resolution for the HTTP layer
        p = safe_backup_path(argv[2], argv[3])
        print(json.dumps({"ok": p is not None, "path": p or ""}))
        return 0 if p else 1
    print(json.dumps({"ok": False,
                      "error": "usage: meta <dir> <backup> <fls> <name> <action> [reason] | "
                               "list <dir> [fls] | prune <dir> <keep> | path <dir> <name>"}))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
