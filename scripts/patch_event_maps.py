#!/usr/bin/env python3
"""Register event maps that the game ships but world-template.yaml leaves out.

Funcom's live events add maps to the game build without adding them to the
self-hosting world-template.yaml. The November 2026 build (26332623) ships the
Halloween event wreck, CB_Halloween_Event_Shipwreck_01 ("Wreck of the
Hieronymus" in game), and the template has no entry for it:

  - the Director binds one RabbitMQ travel queue per template map
    (routing key ``req.<map>``), so the Overmap's travel request for the wreck
    is published with a key nothing listens on and RabbitMQ drops it;
  - the client waits on "Connecting to Wreck of the Hieronymus" forever.

For each event map this adds, to the template text:

  1. a ``worldPartitions`` entry with a partition id this egg reserves
     (prestart.sh seeds the matching ``dune.world_partition`` row), and
  2. a copy of an existing instanced map's server set, with only ``map``
     changed, so mock-k8s and the Director handle it like that map.

A map Funcom already lists is left alone: their entry wins. The patch works on
the text, not on parsed YAML, so the rest of the file stays byte-identical
(and no YAML library is needed in the runtime image). Idempotent.

Usage: patch_event_maps.py <world-template.yaml>
Exit codes: 0 patched or nothing to do, 1 template shape not recognised (the
file is then left untouched).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EventMap:
    name: str
    # Reserved id: above Funcom's sequential ids (1..35 in build 26332623) and
    # below multi-sietch-config.sh's dimension ranges (101+).
    partition_id: int
    # An instanced map in the same template whose server set is copied.
    server_set_from: str


EVENT_MAPS: tuple[EventMap, ...] = (
    EventMap("CB_Halloween_Event_Shipwreck_01", 90, "CB_Story_Hephaestus"),
)


class TemplateShapeError(Exception):
    """The template does not look the way this patch expects."""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _world_partitions_span(lines: list[str]) -> tuple[int, int, str]:
    """(first item line, end line exclusive, item indent) of the list."""
    try:
        head = next(i for i, ln in enumerate(lines) if ln.strip() == "worldPartitions:")
    except StopIteration:
        raise TemplateShapeError("no worldPartitions list") from None
    first = next((i for i in range(head + 1, len(lines)) if _is_content(lines[i])), None)
    if first is None or not lines[first].lstrip().startswith("- map: "):
        raise TemplateShapeError("worldPartitions is not a list of maps")
    item_indent = _indent(lines[first])
    end = len(lines)
    for i in range(first + 1, len(lines)):
        ln = lines[i]
        if not _is_content(ln):
            continue
        ind = _indent(ln)
        if ind < item_indent or (ind == item_indent and not ln.lstrip().startswith("- ")):
            end = i
            break
    return first, end, " " * item_indent


def _listed_maps(lines: list[str], start: int, end: int, indent: str) -> set[str]:
    prefix = indent + "- map: "
    return {ln[len(prefix):].strip() for ln in lines[start:end] if ln.startswith(prefix)}


def _server_set_span(lines: list[str], source: str) -> tuple[int, int, int]:
    """(set start, set end exclusive, index of its map line) for `source`."""
    try:
        group = next(i for i, ln in enumerate(lines) if ln.strip() == "serverGroup:")
    except StopIteration:
        raise TemplateShapeError("no serverGroup") from None
    target = f"map: {source}"
    map_line = next(
        (i for i in range(group + 1, len(lines)) if lines[i].strip() == target),
        None,
    )
    if map_line is None:
        raise TemplateShapeError(f"no server set for {source}")
    item_indent = _indent(lines[map_line]) - 2
    start = next(
        (i for i in range(map_line, group, -1)
         if _indent(lines[i]) == item_indent and lines[i].lstrip().startswith("- ")),
        None,
    )
    if start is None:
        raise TemplateShapeError(f"server set for {source} has no list item start")
    end = len(lines)
    for i in range(map_line + 1, len(lines)):
        if _is_content(lines[i]) and _indent(lines[i]) <= item_indent:
            end = i
            break
    return start, end, map_line


def _partition_block(indent: str, m: EventMap) -> list[str]:
    field = indent + "  "
    entry = field + "  "
    return [
        f"{indent}- map: {m.name}\n",
        f"{field}partitions:\n",
        f"{field}- dimension: 0\n",
        f"{entry}disable: false\n",
        f"{entry}id: {m.partition_id}\n",
        f"{entry}maxX: 1\n",
        f"{entry}maxY: 1\n",
        f"{entry}minX: 0\n",
        f"{entry}minY: 0\n",
    ]


def _cloned_set(lines: list[str], m: EventMap) -> list[str]:
    start, end, map_line = _server_set_span(lines, m.server_set_from)
    pad = lines[map_line][: _indent(lines[map_line])]
    return [
        f"{pad}map: {m.name}\n" if i == map_line else lines[i]
        for i in range(start, end)
    ]


def patch_template(text: str, maps: tuple[EventMap, ...] = EVENT_MAPS) -> tuple[str, list[str]]:
    """Return (patched text, names of the maps added). Raises TemplateShapeError
    before changing anything when the template's shape is not recognised."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start, end, indent = _world_partitions_span(lines)
    listed = _listed_maps(lines, start, end, indent)
    todo = [m for m in maps if m.name not in listed]
    if not todo:
        return text, []

    # Build every insertion from the original lines first, so a shape error
    # on the second map cannot leave the first half-applied.
    partition_lines = [ln for m in todo for ln in _partition_block(indent, m)]
    set_inserts = [(_server_set_span(lines, m.server_set_from)[1], _cloned_set(lines, m)) for m in todo]
    if any(at < end for at, _ in set_inserts):
        raise TemplateShapeError("serverGroup comes before worldPartitions")

    # Apply bottom-up (server sets sit after worldPartitions) so earlier
    # indices stay valid.
    out = list(lines)
    for at, block in sorted(set_inserts, key=lambda p: p[0], reverse=True):
        out[at:at] = block
    out[end:end] = partition_lines
    return "".join(out), [m.name for m in todo]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: patch_event_maps.py <world-template.yaml>\n")
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text(encoding="utf-8")
        patched, added = patch_template(text)
    except (OSError, TemplateShapeError) as exc:
        sys.stderr.write(f"patch_event_maps: left {path} untouched: {exc}\n")
        return 1
    if not added:
        print("event maps: nothing to add")
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(patched, encoding="utf-8")
    tmp.replace(path)
    print(f"event maps: added {', '.join(added)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
