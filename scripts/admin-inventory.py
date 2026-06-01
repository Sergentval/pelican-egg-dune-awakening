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
  grant-keystones --current-xp N --spent-sp N
      Emits KV lines (expected_total_sp, expected_unspent_sp, keystone_bonus,
      keystone_count) for the grant-all-205-keystones FLevel SP recompute.
  give-item --qty N --stack-max N --template T [--stacks CSV] [--max-pos N]
            [--max-slots N --used-slots N --max-volume F --used-volume F
             --per-item-vol F]
      Emits a line protocol (UPDATE/NEW/SUMMARY, or a single ERROR line) for
      the caller to apply as stack top-ups + new dune.items rows in a txn.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import admin_progression as ap  # noqa: E402  # type: ignore[import-not-found]
import admin_inventory_plan as ip  # noqa: E402  # type: ignore[import-not-found]


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


def cmd_grant_keystones(args: argparse.Namespace) -> None:
    total, unspent, bonus = ap.grant_all_keystone_targets(args.current_xp, args.spent_sp)
    _emit({
        "expected_total_sp": total,
        "expected_unspent_sp": unspent,
        "keystone_bonus": bonus,
        "keystone_count": len(ap.all_keystone_ids()),
    })


def _parse_stacks(csv: str) -> list[tuple[int, int]]:
    """Parse the --stacks "id:size,id:size" CSV produced by the bash caller
    (DB-sourced integers) into [(stack_id, size)]. Empty -> []."""
    out: list[tuple[int, int]] = []
    for part in csv.split(","):
        part = part.strip()
        if not part:
            continue
        sid, _, size = part.partition(":")
        out.append((int(sid), int(size)))
    return out


def cmd_give_item(args: argparse.Namespace) -> None:
    """Compute the give-item plan and emit a line protocol the bash layer
    executes inside a transaction. Output is one of:

      ERROR <message>                       -- validation/capacity refusal
    or, on success:
      UPDATE <stack_id> <add>               -- (0+) top-ups of existing stacks
      NEW <size> <position_index>           -- (1+) new stacks to insert
      SUMMARY topped_up=<n> created=<m> qty=<q>

    The compute itself always exits 0; a refusal is a valid answer carried by
    the ERROR line, not a crash."""
    plan = ip.build_give_item_plan(
        qty=args.qty,
        stack_max=args.stack_max,
        template=args.template,
        stacks=_parse_stacks(args.stacks),
        max_pos=args.max_pos,
        max_slots=args.max_slots,
        used_slots=args.used_slots,
        max_volume=args.max_volume,
        used_volume=args.used_volume,
        per_item_vol=args.per_item_vol,
    )
    if plan.error is not None:
        print(f"ERROR {plan.error}")
        return
    for upd in plan.updates:
        print(f"UPDATE {upd.stack_id} {upd.add}")
    for new in plan.new_stacks:
        print(f"NEW {new.size} {new.position_index}")
    print(f"SUMMARY topped_up={len(plan.updates)} created={len(plan.new_stacks)} qty={args.qty}")


def main() -> None:
    parser = argparse.ArgumentParser(description="player-write compute helpers (no DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ax = sub.add_parser("award-char-xp", help="compute new FLevel XP/SP/intel")
    ax.add_argument("--current-xp", type=int, required=True)
    ax.add_argument("--spent-sp", type=int, required=True)
    ax.add_argument("--keystones", default="", help="CSV of purchased keystone ids")
    ax.add_argument("--amount", type=int, required=True)
    ax.set_defaults(fn=cmd_award_char_xp)

    gk = sub.add_parser("grant-keystones", help="compute FLevel SP after granting all keystones")
    gk.add_argument("--current-xp", type=int, required=True)
    gk.add_argument("--spent-sp", type=int, required=True)
    gk.set_defaults(fn=cmd_grant_keystones)

    gi = sub.add_parser("give-item", help="compute give-item stack/slot/volume plan")
    gi.add_argument("--qty", type=int, required=True)
    gi.add_argument("--stack-max", type=int, required=True)
    gi.add_argument("--template", required=True)
    gi.add_argument("--stacks", default="", help="CSV of existing matching stacks 'id:size,id:size'")
    gi.add_argument("--max-pos", type=int, default=-1, help="MAX(position_index) in the inventory, -1 if empty")
    gi.add_argument("--max-slots", type=int, default=-1, help="inventory slot cap, <=0 means no cap")
    gi.add_argument("--used-slots", type=int, default=0)
    gi.add_argument("--max-volume", type=float, default=-1.0, help="inventory volume cap, <=0 means no cap")
    gi.add_argument("--used-volume", type=float, default=0.0)
    gi.add_argument("--per-item-vol", type=float, default=0.0)
    gi.set_defaults(fn=cmd_give_item)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
