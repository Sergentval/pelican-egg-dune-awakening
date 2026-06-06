#!/usr/bin/env python3
"""Parked-sietch id set: the reboot-durable list of Survival_1 dim>0 partitions the
operator has PARKED (paused but kept — UE5 stopped to free RAM, while the world_partition
row + every player structure is preserved). The two boot spawn-scans skip these ids so a
parked sietch never auto-respawns on reboot; unpark removes the id and respawns it.

State: server/state/admin/parked-sietches.json — a JSON array of ints, written atomically
(tmp + os.replace), mirroring admin_sietch.py's sietch-overrides pattern. Lives in the
persistent server/state volume so it survives reboot.

FAIL-SAFE (the safety boundary): a missing / corrupt / wrong-shape file reads as the EMPTY
set, and non-int entries are dropped. The parked set GATES boot spawning, so a read error
must degrade to "spawn everything" (safe) and can NEVER wrongly skip a live sietch.

CLI (for the bash spawn-scans + the admin verbs):
  admin_park.py csv <base>            -> comma-separated parked ids (or empty), for membership
  admin_park.py is-parked <pid> <base>-> exit 0 if parked, 1 if not (for `if` in bash)
  admin_park.py list <base>           -> {"ok":true,"parked":[...]} JSON (for HTTP)
  admin_park.py park|unpark <pid> <base> -> mutate + print JSON
"""
import json
import os
import sys


def park_path(base):
    return os.path.join(base, "server", "state", "admin", "parked-sietches.json")


def parked_ids(base):
    """Set of parked partition_ids. Empty on missing/corrupt/wrong-shape file; only clean
    ints survive (bools and non-numeric entries dropped). Never raises."""
    try:
        with open(park_path(base), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    # Accept ONLY true ints — never coerce a float/string. Coercing e.g. 3.5 -> 3 would
    # park the WRONG (real) partition; a malformed entry is corruption and is dropped.
    return {x for x in raw if isinstance(x, int) and not isinstance(x, bool)}


def _write(base, ids):
    p = park_path(base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    return set(ids)


def park(base, pid):
    ids = parked_ids(base)
    ids.add(int(pid))
    return _write(base, ids)


def unpark(base, pid):
    ids = parked_ids(base)
    ids.discard(int(pid))
    return _write(base, ids)


def is_parked(base, pid):
    try:
        return int(pid) in parked_ids(base)
    except (TypeError, ValueError):
        return False


def prune(base, valid_ids):
    """Drop parked ids that are no longer real partitions (e.g. a removed sietch); keep
    only those in valid_ids. Returns the pruned set."""
    valid = set()
    for v in valid_ids:
        try:
            valid.add(int(v))
        except (TypeError, ValueError):
            continue
    return _write(base, parked_ids(base) & valid)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "csv":
        base = argv[2] if len(argv) > 2 else os.environ.get("DUNE_BASE_DIR", "/home/container")
        print(",".join(str(i) for i in sorted(parked_ids(base))))
        return 0
    if cmd == "list":
        base = argv[2] if len(argv) > 2 else os.environ.get("DUNE_BASE_DIR", "/home/container")
        print(json.dumps({"ok": True, "parked": sorted(parked_ids(base))}))
        return 0
    if cmd in ("park", "unpark", "is-parked") and len(argv) > 2:
        pid = argv[2]
        base = argv[3] if len(argv) > 3 else os.environ.get("DUNE_BASE_DIR", "/home/container")
        if cmd == "is-parked":
            return 0 if is_parked(base, pid) else 1
        fn = park if cmd == "park" else unpark
        print(json.dumps({"ok": True, "parked": sorted(fn(base, pid))}))
        return 0
    print("usage: admin_park.py <csv|list|park <pid>|unpark <pid>|is-parked <pid>> [BASE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
