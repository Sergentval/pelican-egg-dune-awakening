#!/usr/bin/env python3
"""DB-backup helpers (scheduler Phase 1): backup-file naming + retention.

The pg_dump / pg_restore themselves run in admin-publish.sh (the only layer with
the Postgres connection). This module is the pure, unit-tested retention logic +
a thin `prune` CLI that admin-publish calls after a successful dump. Backup files
are named `dune-YYYYMMDD-HHMMSS.dump`; the zero-padded timestamp makes lexical
sort == chronological sort.
"""
import os
import re
import sys

BACKUP_PREFIX = "dune-"
BACKUP_SUFFIX = ".dump"
BACKUP_RE = re.compile(r"^dune-\d{8}-\d{6}\.dump$")


def backup_name(ts: str) -> str:
    """Filename for a backup taken at `ts` ('YYYYMMDD-HHMMSS')."""
    return f"{BACKUP_PREFIX}{ts}{BACKUP_SUFFIX}"


def is_backup(name: str) -> bool:
    return bool(BACKUP_RE.match(name))


def select_for_prune(names, keep: int) -> list:
    """Given backup filenames (any order) + keep-N, return the names to DELETE
    (everything older than the newest `keep`). Non-backup names are ignored."""
    backups = sorted(n for n in names if is_backup(n))
    if keep <= 0:
        return list(backups)
    return backups[:-keep] if len(backups) > keep else []


def _prune_dir(directory: str, keep: int) -> list:
    """List `directory`, delete the prune set, return the deleted names."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    doomed = select_for_prune(names, keep)
    for n in doomed:
        try:
            os.remove(os.path.join(directory, n))
        except OSError:
            pass
    return doomed


def _main(argv) -> int:
    if len(argv) >= 4 and argv[1] == "prune":
        keep = int(argv[3]) if argv[3].lstrip("-").isdigit() else 7
        for n in _prune_dir(argv[2], keep):
            print(f"pruned {n}")
        return 0
    print("usage: admin_backup.py prune <dir> <keep>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
