#!/usr/bin/env python3
"""Shared reward spec for the player-events and battlepass engines.

One spec shape, ported from dune-admin's rewardSpec (MIT):

    {"currency": 5000,
     "items": [{"template": "...", "qty": 1, "quality": 0}, ...],
     "xp":    [{"track": "Combat", "amount": 10000}, ...]}

Delivery is strictly ordered currency → items (array order) → xp (array
order), aborting at the first failure. Upstream resumed a partial grant by
PARSING the stored error string ("grant item \"X\": …" was a load-bearing
data format); the port stores a structured cursor {"stage", "index"}
instead — the cursor names the step that FAILED, so a retry replays from
that step onward and never re-pays anything before it. Anything
unrecognisable as a cursor fails safe to a full replay, matching
upstream's fallback.

Unknown keys in a spec are refused loudly: upstream's UI wrote
reward.faction_scrip which no engine ever read — a silent no-op we choose
to surface as a config error instead.
"""
from __future__ import annotations

import json

STAGES = ("currency", "items", "xp")


def parse_spec(text: str | None) -> dict:
    """Validated canonical spec. Empty/None = announce-only (no rewards)."""
    if not text or not text.strip():
        return {"currency": 0, "items": [], "xp": []}
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"reward spec is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("reward spec must be a JSON object")
    unknown = set(raw) - {"currency", "items", "xp"}
    if unknown:
        raise ValueError(f"reward spec has unsupported key(s): {sorted(unknown)}"
                         " (supported: currency, items, xp)")
    currency = raw.get("currency", 0)
    if not isinstance(currency, int) or isinstance(currency, bool) or currency < 0:
        raise ValueError("reward.currency must be a non-negative integer")
    items = raw.get("items", [])
    if not isinstance(items, list):
        raise ValueError("reward.items must be a list")
    canon_items = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("template"), str) \
                or not it["template"]:
            raise ValueError(f"reward.items[{i}] needs a non-empty template")
        qty = it.get("qty", 1)
        quality = it.get("quality", 0)
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            raise ValueError(f"reward.items[{i}].qty must be a positive integer")
        if not isinstance(quality, int) or isinstance(quality, bool) or quality < 0:
            raise ValueError(f"reward.items[{i}].quality must be >= 0")
        canon_items.append({"template": it["template"], "qty": qty,
                            "quality": quality})
    xp = raw.get("xp", [])
    if not isinstance(xp, list):
        raise ValueError("reward.xp must be a list")
    canon_xp = []
    for i, tr in enumerate(xp):
        if not isinstance(tr, dict) or not isinstance(tr.get("track"), str) \
                or not tr["track"]:
            raise ValueError(f"reward.xp[{i}] needs a non-empty track")
        amount = tr.get("amount", 0)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError(f"reward.xp[{i}].amount must be >= 0")
        canon_xp.append({"track": tr["track"], "amount": amount})
    return {"currency": currency, "items": canon_items, "xp": canon_xp}


def is_empty(spec: dict) -> bool:
    return not spec["currency"] and not spec["items"] and not spec["xp"]


def steps(spec: dict) -> list[dict]:
    """The ordered delivery plan. Each step: {stage, index, payload}."""
    out = []
    if spec["currency"]:
        out.append({"stage": "currency", "index": 0,
                    "payload": {"amount": spec["currency"]}})
    for i, it in enumerate(spec["items"]):
        out.append({"stage": "items", "index": i, "payload": dict(it)})
    for i, tr in enumerate(spec["xp"]):
        out.append({"stage": "xp", "index": i, "payload": dict(tr)})
    return out


def cursor_of(step: dict) -> str:
    return json.dumps({"stage": step["stage"], "index": step["index"]})


def _parse_cursor(cursor: str | None) -> tuple[str, int] | None:
    if not cursor:
        return None
    try:
        raw = json.loads(cursor)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("stage") not in STAGES:
        return None
    index = raw.get("index", 0)
    if not isinstance(index, int) or index < 0:
        return None
    return (str(raw["stage"]), index)


def remaining(spec: dict, cursor: str | None) -> list[dict]:
    """Steps still owed given a stored cursor (the step that FAILED last
    time, inclusive). Unparseable cursor ⇒ full replay (fail safe)."""
    plan = steps(spec)
    parsed = _parse_cursor(cursor)
    if parsed is None:
        return plan
    stage, index = parsed
    order = {s: n for n, s in enumerate(STAGES)}
    return [s for s in plan
            if (order[s["stage"]], s["index"]) >= (order[stage], index)]


def deliver(spec: dict, runner, cursor: str | None = None) -> dict:
    """Drive `runner(step) -> error_str_or_empty` over the remaining plan.
    Returns {ok, cursor, error}: on failure the cursor names the failed
    step so the caller persists it for the retry."""
    for step in remaining(spec, cursor):
        err = runner(step)
        if err:
            return {"ok": False, "cursor": cursor_of(step),
                    "error": f"{step['stage']}[{step['index']}]: {err}"}
    return {"ok": True, "cursor": "", "error": ""}
