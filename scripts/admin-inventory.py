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
  faction-rep --current N --delta N --faction ID
      Emits KV lines (new_rep, tier, tier_name, faction_name, capped) for the
      caller to set via dune.set_player_faction_reputation + rebuild the
      m_FactionDataArray jsonb component.
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


def cmd_faction_rep(args: argparse.Namespace) -> None:
    o = ap.faction_rep_outcome(args.current, args.delta, args.faction)
    _emit({
        "new_rep": o["new_rep"],
        "tier": o["tier"],
        "tier_name": o["tier_name"],
        "faction_name": o["faction_name"],
        "capped": 1 if o["capped"] else 0,
    })


def cmd_faction_tier(args: argparse.Namespace) -> None:
    o = ap.faction_tier_outcome(args.faction, args.tier)
    _emit({
        "rep": o["rep"],
        "tier": o["tier"],
        "tier_name": o["tier_name"],
        "faction_name": o["faction_name"],
    })


def cmd_progression_preset(args: argparse.Namespace) -> None:
    p = ap.progression_preset(args.id)
    if p is None:
        raise SystemExit(f"unknown preset: {args.id} (known: {','.join(ap.progression_preset_ids())})")
    _emit({
        "roots_array": ap.pg_text_array(p["nodes"]),
        "name": p["name"],
        "node_count": p["node_count"],
    })


def cmd_progression_all_roots(_args: argparse.Namespace) -> None:
    # Parallel arrays: pids[i] is the preset that owns roots[i]. Multi-root
    # presets repeat their id. Fed to unnest(:'pids',:'roots') in player-summary's
    # journey query so per-preset completion is one round-trip.
    pids: list[str] = []
    roots: list[str] = []
    for pid in ap.progression_preset_ids():
        for root in ap.progression_preset(pid)["nodes"]:
            pids.append(pid)
            roots.append(root)
    _emit({"pids": ap.pg_text_array(pids), "roots": ap.pg_text_array(roots)})


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

    fr = sub.add_parser("faction-rep", help="compute new faction rep + tier after a signed delta")
    fr.add_argument("--current", type=int, required=True)
    fr.add_argument("--delta", type=int, required=True)
    fr.add_argument("--faction", type=int, required=True, help="faction id (1=Atreides, 2=Harkonnen)")
    fr.set_defaults(fn=cmd_faction_rep)

    ft = sub.add_parser("faction-tier", help="compute target rep for a faction tier (0-20)")
    ft.add_argument("--faction", type=int, required=True, help="faction id (1=Atreides, 2=Harkonnen)")
    ft.add_argument("--tier", type=int, required=True, help="target tier 0-20")
    ft.set_defaults(fn=cmd_faction_tier)

    pp = sub.add_parser("progression-preset", help="emit a progression preset's root-node pg array + metadata")
    pp.add_argument("--id", required=True, help="preset id (skip_npe, act1_complete, unlock_all_lore, ...)")
    pp.set_defaults(fn=cmd_progression_preset)

    par = sub.add_parser("progression-all-roots", help="emit parallel pids[]/roots[] pg arrays for all presets")
    par.set_defaults(fn=cmd_progression_all_roots)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
