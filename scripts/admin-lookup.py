#!/usr/bin/env python3
"""admin-lookup.py — query the bundled data catalogues.

Read-only helpers backing `admin vehicles`, `admin items <search>`, and
`admin skills <search>` subcommands in admin-publish.sh. Data files
live in $DUNE_BASE_DIR/data/admin/*.json (bundled from
adainrivers/dune-dedicated-server-manager, MIT — see ATTRIBUTION.md).

Usage:
    admin-lookup.py vehicles
    admin-lookup.py items <search-text>
    admin-lookup.py skills <search-text>
    admin-lookup.py items-json <id>    # raw JSON for one id
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


DATA_DIR = Path(os.environ.get("DUNE_BASE_DIR", "/home/container")) / "data" / "admin"


def load(name: str) -> list[dict]:
    path = DATA_DIR / f"{name}.json"
    if not path.is_file():
        print(f"[admin-lookup] ERROR data file missing: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open() as fh:
        return json.load(fh)


def print_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> None:
    if not rows:
        print("(no matches)")
        return
    widths = [
        max(len(headers[i]), *(len(str(r[i])) for r in rows)) for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))


def cmd_vehicles() -> int:
    data = load("vehicles")
    rows = [(v["id"], ", ".join(v.get("templates", []))) for v in data]
    print_table(rows, ("ClassName", "TemplateName options"))
    print(f"\n({len(rows)} vehicle classes)")
    return 0


def cmd_items(search: str) -> int:
    data = load("items")
    if not search:
        print("[admin-lookup] ERROR items lookup needs a search term (e.g. 'items spice')", file=sys.stderr)
        return 2
    needle = search.lower()
    matches = [
        (it["id"], it.get("name", "?"), it.get("category", "?"), it.get("source", "?"))
        for it in data
        if needle in it.get("id", "").lower() or needle in it.get("name", "").lower()
    ]
    matches.sort(key=lambda r: r[0].lower())
    # Cap to keep output usable in the panel console
    limit = 40
    truncated = len(matches) > limit
    print_table(matches[:limit], ("ItemFName (for admin give)", "Display name", "Category", "Source"))
    suffix = f" — showing first {limit}" if truncated else ""
    print(f"\n({len(matches)} matches for '{search}'{suffix})")
    return 0


def cmd_skills(search: str) -> int:
    data = load("skill-modules")
    needle = (search or "").lower()
    matches = [
        (m["id"], m.get("name", "?"), m.get("category", "?"), str(m.get("maxLevel", "?")))
        for m in data
        if not needle
        or needle in m.get("id", "").lower()
        or needle in m.get("name", "").lower()
        or needle in m.get("category", "").lower()
    ]
    matches.sort(key=lambda r: r[0].lower())
    limit = 50
    truncated = len(matches) > limit
    print_table(matches[:limit], ("Module (for admin skill)", "Display name", "Category", "MaxLvl"))
    suffix = f" — showing first {limit}" if truncated else ""
    print(f"\n({len(matches)} matches{' for ' + repr(search) if search else ''}{suffix})")
    return 0


def cmd_items_json(item_id: str) -> int:
    data = load("items")
    needle = item_id.lower()
    for it in data:
        if it.get("id", "").lower() == needle:
            print(json.dumps(it, indent=2))
            return 0
    print(f"[admin-lookup] no item with id={item_id}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, *rest = argv
    if cmd == "vehicles":
        return cmd_vehicles()
    if cmd == "items":
        return cmd_items(" ".join(rest))
    if cmd == "skills":
        return cmd_skills(" ".join(rest))
    if cmd == "items-json":
        if not rest:
            print("[admin-lookup] usage: items-json <id>", file=sys.stderr)
            return 2
        return cmd_items_json(rest[0])
    print(f"[admin-lookup] unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
