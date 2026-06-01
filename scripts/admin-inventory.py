#!/usr/bin/env python3
"""Argv-only compute helper for player-write subcommands. Holds NO database /
network / psycopg state: admin-publish.sh reads current state via dune_psql,
calls this to compute the new values, then writes them back via jsonb_set.
Reuses the pure math in admin_progression.py. (Same argv-only discipline as
admin-payload.py — values come via argv, never shell-interpolated source.)

Subcommands:
  award-char-xp --current-xp N --spent-sp N --keystones CSV --amount N
      Emits KV lines (new_xp, new_level, new_total_sp, new_unspent_sp,
      new_intel, capped) for the caller to jsonb_set into FLevelComponent +
      TechKnowledgePlayerComponent.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]


def _emit(pairs: dict) -> None:
    for key, value in pairs.items():
        print(f"{key}={value}")


def cmd_award_char_xp(args: argparse.Namespace) -> None:
    ids = [int(x) for x in args.keystones.split(",") if x.strip()] if args.keystones else []
    bonus = ap.keystone_sp_bonus(ids)
    o = ap.award_char_xp_outcome(args.current_xp, args.spent_sp, bonus, args.amount)
    _emit({
        "new_xp": o["new_xp"],
        "new_level": o["new_level"],
        "new_total_sp": o["new_total_sp"],
        "new_unspent_sp": o["new_unspent_sp"],
        "new_intel": o["new_intel"],
        "keystone_bonus": bonus,
        "capped": 1 if o["capped"] else 0,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="player-write compute helpers (no DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ax = sub.add_parser("award-char-xp", help="compute new FLevel XP/SP/intel")
    ax.add_argument("--current-xp", type=int, required=True)
    ax.add_argument("--spent-sp", type=int, required=True)
    ax.add_argument("--keystones", default="", help="CSV of purchased keystone ids")
    ax.add_argument("--amount", type=int, required=True)
    ax.set_defaults(fn=cmd_award_char_xp)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
