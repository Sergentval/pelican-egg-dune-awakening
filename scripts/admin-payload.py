#!/usr/bin/env python3
"""admin-payload.py — build the base64-encoded outer envelope for one
admin ServerCommand without any shell-source-code interpolation.

Replaces the original `python3 -c "<heredoc with $vars>"` pattern in
admin-publish.sh. Values arrive via argparse argv — they never become
Python source, so a name like `'''+__import__('os').system(...)+'''`
is treated as a literal string field instead of executable code.

Stdout is exactly one line: base64(json.dumps({Version, AuthToken,
MessageContent})). admin-publish.sh feeds that to rabbitmqctl eval.

Subcommands map 1:1 to the AMQP publish branches in admin-publish.sh.
Lookup/postgres subcommands (players, resolve, pos, vehicle-list,
vehicle-delete, items, skills) stay in bash because they don't go
through the publish path.

SECURITY NOTE: this script never runs subprocess / os.system / eval.
The `serverexec` subcommand below builds a *JSON field* named "Exec"
for Funcom's ServerExec ServerCommand — that field is parsed by their
server, not by us. We do not execute any code.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from typing import Any


# --------------------------------------------------------------------------
# Inner-payload builders. Each returns the ServerCommand dict; the outer
# envelope is appended once at the end.
# --------------------------------------------------------------------------
def build_broadcast(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ServerCommand": "ServiceBroadcast",
        "BroadcastType": "Generic",
        "BroadcastPayload": {
            "BroadcastDuration": args.duration,
            # UE5's broadcast renderer requires a LocalizedText[] array
            # with at least one locale entry. Shape verified against
            # adainrivers/dune-dedicated-server-manager build.rs.
            "LocalizedText": [
                {"Key": "en", "Title": args.title, "Body": args.body},
                {"Key": "en-US", "Title": args.title, "Body": args.body},
            ],
        },
    }


def build_shutdown(args: argparse.Namespace) -> dict[str, Any]:
    if args.type.lower() == "cancel":
        return {
            "ServerCommand": "ServiceBroadcast",
            "BroadcastType": "ServerShutdown",
            "BroadcastPayload": {"ShouldCancel": True},
        }
    now = int(time.time())
    lead = max(1, args.lead_secs)
    return {
        "ServerCommand": "ServiceBroadcast",
        "BroadcastType": "ServerShutdown",
        "BroadcastPayload": {
            "ShutdownType": args.type,
            "DateTimestamp": now,
            "ShutdownDuration": lead,
            "ShutdownTimestamp": now + lead,
            "BroadcastFrequency": max(1, args.freq_secs),
            "BroadcastDuration": 30,
        },
    }


def build_kick(args: argparse.Namespace) -> dict[str, Any]:
    return {"ServerCommand": "KickPlayer", "PlayerId": args.player_id}


def build_clean(args: argparse.Namespace) -> dict[str, Any]:
    return {"ServerCommand": "CleanPlayerInventory", "PlayerId": args.player_id}


def build_reset(args: argparse.Namespace) -> dict[str, Any]:
    return {"ServerCommand": "ResetProgression", "PlayerId": args.player_id}


def build_water(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ServerCommand": "UpdateAllWaterFillables",
        "PlayerId": args.player_id,
        "WaterAmount": args.amount,
    }


def build_give(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ServerCommand": "AddItemToInventory",
        "PlayerId": args.player_id,
        "ItemName": args.item,
        "Quantity": args.qty,
        "Durability": args.durability,
    }


def build_xp(args: argparse.Namespace) -> dict[str, Any]:
    # seabass handler silently no-ops unless Category is set. The value
    # is ignored but the field must exist (verified by adainrivers live
    # test, 2026-05-28).
    return {
        "ServerCommand": "AwardXP",
        "PlayerId": args.player_id,
        "Experience": args.amount,
        "Category": "Combat",
    }


def build_skill(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ServerCommand": "SkillsSetModuleLevel",
        "PlayerId": args.player_id,
        "Module": args.module,
        "Level": args.level,
    }


def build_points(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ServerCommand": "SkillsSetUnspentSkillPoints",
        "PlayerId": args.player_id,
        "SkillPoints": args.amount,
    }


def build_teleport(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ServerCommand": "TeleportToExact",
        "PlayerId": args.player_id,
        "X": args.x,
        "Y": args.y,
        "Z": args.z,
    }
    if args.yaw is not None:
        payload["Yaw"] = args.yaw
    return payload


def build_tpsafe(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ServerCommand": "TeleportTo",
        "PlayerId": args.player_id,
        "X": args.x,
        "Y": args.y,
        "Z": args.z,
    }
    if args.yaw is not None:
        payload["Yaw"] = args.yaw
    return payload


def build_vehicle(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ServerCommand": "SpawnVehicleAt",
        "PlayerId": args.player_id,
        "ClassName": args.vehicle_class,
        "X": args.x,
        "Y": args.y,
        "Z": args.z,
        "TemplateName": args.template,
        "Persistent": args.persistent,
    }
    if args.rotation is not None:
        payload["Rotation"] = args.rotation
    if args.faction:
        payload["Faction"] = args.faction
    return payload


def build_cheat(args: argparse.Namespace) -> dict[str, Any]:
    # CheatScript — known no-op on seabass, kept for protocol parity.
    return {
        "ServerCommand": "CheatScript",
        "PlayerId": args.player_id,
        "ScriptName": args.script,
    }


def build_serverexec(args: argparse.Namespace) -> dict[str, Any]:
    # Funcom's ServerExec ServerCommand. Known no-op on seabass.
    # The string ends up as a JSON field; this script does NOT execute
    # anything itself (no subprocess, no os.system, no eval anywhere
    # in this file).
    return {"ServerCommand": "ServerExec", "Exec": args.command}


# --------------------------------------------------------------------------
# argparse wiring.
# --------------------------------------------------------------------------
def _add_player_id(p: argparse.ArgumentParser) -> None:
    p.add_argument("--player-id", required=True, help="Resolved FLS id, * for all, or me.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="admin-payload",
        description="Build the AMQP outer envelope for one Dune Awakening "
        "ServerCommand. Stdout: base64 of {Version, AuthToken, "
        "MessageContent}. Stdin: nothing.",
    )
    p.add_argument(
        "--token",
        required=True,
        help="AuthToken for the outer envelope. Usually the per-boot "
        "ServerCommandsAuthToken; admin-publish.sh resolves it from "
        "state/svc-cmd-token and forwards it here.",
    )
    p.add_argument(
        "--inner-only",
        action="store_true",
        help="Emit the inner ServerCommand JSON on stdout instead of "
        "the base64-encoded outer envelope. For DRY-RUN diagnostics.",
    )

    subs = p.add_subparsers(dest="cmd", required=True)

    b = subs.add_parser("broadcast", help="ServiceBroadcast (Generic)")
    b.add_argument("--title", required=True)
    b.add_argument("--body", required=True)
    b.add_argument("--duration", type=int, default=30)

    sh = subs.add_parser("shutdown", help="ServiceBroadcast (ServerShutdown)")
    sh.add_argument("--type", required=True, help="Restart | Maintenance | Update | cancel")
    sh.add_argument("--lead-secs", type=int, default=600)
    sh.add_argument("--freq-secs", type=int, default=60)

    for name in ("kick", "clean", "reset"):
        s = subs.add_parser(name)
        _add_player_id(s)

    w = subs.add_parser("water")
    _add_player_id(w)
    w.add_argument("--amount", type=int, default=1_000_000)

    g = subs.add_parser("give")
    _add_player_id(g)
    g.add_argument("--item", required=True)
    g.add_argument("--qty", type=int, default=1)
    g.add_argument("--durability", type=float, default=1.0)

    x = subs.add_parser("xp")
    _add_player_id(x)
    x.add_argument("--amount", type=int, required=True)

    sk = subs.add_parser("skill")
    _add_player_id(sk)
    sk.add_argument("--module", required=True)
    sk.add_argument("--level", type=int, required=True)

    pts = subs.add_parser("points")
    _add_player_id(pts)
    pts.add_argument("--amount", type=int, required=True)

    for name in ("teleport", "tpsafe"):
        s = subs.add_parser(name)
        _add_player_id(s)
        s.add_argument("--x", type=float, required=True)
        s.add_argument("--y", type=float, required=True)
        s.add_argument("--z", type=float, required=True)
        s.add_argument("--yaw", type=float, default=None)

    v = subs.add_parser("vehicle")
    _add_player_id(v)
    v.add_argument("--class", dest="vehicle_class", required=True)
    v.add_argument("--x", type=float, required=True)
    v.add_argument("--y", type=float, required=True)
    v.add_argument("--z", type=float, required=True)
    v.add_argument("--template", required=True)
    v.add_argument("--rotation", type=float, default=None)
    v.add_argument("--persistent", type=float, default=1.0)
    v.add_argument("--faction", default="")

    c = subs.add_parser("cheat")
    _add_player_id(c)
    c.add_argument("--script", required=True)

    se = subs.add_parser("serverexec", help="ServerExec ServerCommand (no-op on seabass)")
    se.add_argument("--command", required=True)

    return p


_BUILDERS = {
    "broadcast": build_broadcast,
    "shutdown": build_shutdown,
    "kick": build_kick,
    "clean": build_clean,
    "reset": build_reset,
    "water": build_water,
    "give": build_give,
    "xp": build_xp,
    "skill": build_skill,
    "points": build_points,
    "teleport": build_teleport,
    "tpsafe": build_tpsafe,
    "vehicle": build_vehicle,
    "cheat": build_cheat,
    "serverexec": build_serverexec,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    builder = _BUILDERS.get(args.cmd)
    if builder is None:
        print(f"admin-payload: unknown subcommand: {args.cmd}", file=sys.stderr)
        return 2
    inner = builder(args)
    inner_json = json.dumps(inner, separators=(",", ":"))
    if args.inner_only:
        print(inner_json)
        return 0
    outer = {"Version": 2, "AuthToken": args.token, "MessageContent": inner_json}
    outer_json = json.dumps(outer, separators=(",", ":"))
    print(base64.standard_b64encode(outer_json.encode()).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
