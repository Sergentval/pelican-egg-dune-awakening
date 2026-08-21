#!/bin/bash
# Apply Pelican panel variables to Funcom's UE5 ini files.
#
# Mirrors the surface CubeCoders' AMP template exposes through its
# metaconfig automap. Three target sinks:
#
#   - UserEngine.ini  → [ConsoleVariables] section (UE5 cvars)
#   - UserGame.ini    → [/Script/...] sections   (UE5 property classes)
#   - ondemand.ini    → flat key=value             (mock-k8s pool tuning)
#
# The setting catalogue lives in data/admin/settings-schema.json (the single
# source of truth shared with admin-http's GET/PUT /api/settings). Only entries
# that carry an `env` (a panel variable) are applied here at boot; the rest are
# managed exclusively through the admin API. The actual INI upsert is delegated
# to scripts/admin_ini_merge.py (unit-tested; byte-identical to this script's
# previous inline implementation).
#
# Behaviour:
#   - Unset env var        ⇒ skip the row entirely (operator hand-edits survive).
#   - Empty env var        ⇒ same, unless the setting declares empty_ok in the
#                            schema — then the blank is applied (issue #107:
#                            a cleared join password means a public server).
#   - Section missing      ⇒ append a fresh `[section]` and the key/value.
#   - Key present          ⇒ rewrite in place under the matching section.
#   - Key missing          ⇒ append the key at the end of the section.
#
# Called from pelican-entrypoint.sh AFTER prestart.sh has seeded the
# Funcom-stock templates.

set -euo pipefail

BASE="${1:-${DUNE_BASE_DIR:-/home/container}}"
export DUNE_APPLY_BASE="$BASE"

exec python3 - <<'PYEOF'
import json
import os
import sys
import traceback

BASE = os.environ["DUNE_APPLY_BASE"]
sys.path.insert(0, f"{BASE}/scripts")
import admin_ini_merge as ini  # faithful, unit-tested INI upsert engine

FILES = {
    "UserEngine":    f"{BASE}/server/state/ue5-saved/UserSettings/UserEngine.ini",
    "UserGame":      f"{BASE}/server/state/ue5-saved/UserSettings/UserGame.ini",
    "UserOverrides": f"{BASE}/server/state/UserOverrides.ini",
    "ondemand":      f"{BASE}/server/state/ondemand.ini",
}
SCHEMA = f"{BASE}/data/admin/settings-schema.json"


def log(msg):
    print(f"[apply-config] {msg}", flush=True)


def apply_repeated(path, section, key, vals):
    """UE array key (issue #106): one +key= line per element, every previous
    live line replaced. Empty vals removes the lines (reverts to defaults)."""
    if not os.path.isfile(path):
        log(f"WARN target {path} missing — [{section}] {key} skipped")
        return False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    new = ini.upsert_repeated(text, section, key, vals)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    log(f"  {os.path.basename(path)} [{section}] {key} = {','.join(vals) if vals else '<cleared>'} ({len(vals)} +line(s))")
    return True


def apply_one(path, section, key, rendered):
    """Read → upsert (flat if section is None, else section-scoped) → write.
    Returns True if (re)written, False on a missing target file."""
    if not os.path.isfile(path):
        where = f"[{section}] " if section else ""
        log(f"WARN target {path} missing — {where}{key} skipped")
        return False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    new = ini.upsert_flat(text, key, rendered) if section is None \
        else ini.upsert_keyed(text, section, key, rendered)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    where = f"[{section}] " if section else ""
    log(f"  {os.path.basename(path)} {where}{key}={rendered}")
    return True


try:
    with open(SCHEMA, encoding="utf-8") as f:
        settings = json.load(f)["settings"]
except Exception:
    log(f"ERROR cannot load settings schema {SCHEMA}; no panel settings applied")
    traceback.print_exc(file=sys.stdout)
    settings = []

applied = 0
skipped_unset = 0
skipped_reject = 0
errored = 0
for st in settings:
    env_name = st.get("env")
    if not env_name:
        continue  # API-managed only (no panel variable) — not applied at boot
    # Skip unset variables so hand-edits survive a panel-less run; a
    # present-but-EMPTY variable is applied when the setting declares
    # empty_ok (issue #107: blank join password = public server).
    raw = ini.env_value_to_apply(os.environ, env_name, bool(st.get("empty_ok")))
    if raw is None:
        skipped_unset += 1
        continue
    path = FILES.get(st.get("file"))
    if path is None:
        log(f"WARN unknown file sink {st.get('file')!r} for {env_name} — skipped")
        skipped_reject += 1
        continue
    if st.get("repeated"):
        # UE array key: validate the id list ("8,101") then write one +key=
        # line per element via the engine (issue #106).
        try:
            norm = ini.normalize_value(raw, st.get("type", "intlist"), st.get("enum"))
        except ValueError as exc:
            log(f"WARN {env_name}: {exc}. Skipped.")
            skipped_reject += 1
            continue
        vals = [p for p in norm.split(",") if p]
        # Ecosystem-verified interplay (DST v12.19.8, Red-Blink, dapdsm all
        # guard this): the per-partition list is IGNORED by the game while
        # m_bShouldForceEnablePvpOnAllPartitions is True.
        if vals and os.environ.get("DUNE_FORCE_PVP_EVERYWHERE", "").strip().lower() in ("true", "1"):
            log(f"WARN {env_name} is set but DUNE_FORCE_PVP_EVERYWHERE=True — "
                "the game ignores the per-partition list while the global force "
                "is on. Set Force PvP Everywhere to False to use the list.")
        try:
            if apply_repeated(path, st["section"], st["key"], vals):
                applied += 1
            else:
                skipped_reject += 1
        except Exception:
            errored += 1
            log(f"ERROR while applying {env_name} → {st['key']}:")
            traceback.print_exc(file=sys.stdout)
        continue
    # Issue #82: a panel variable typed on a German/French system arrives as
    # "2,5". UE5's atof stops at the comma and applies 2 without complaining,
    # so the operator sees a setting that looks applied but silently isn't.
    value = ini.coerce_decimal_comma(raw, st.get("type", ""))
    if value != raw:
        log(f"  {env_name}: decimal separator normalized {raw!r} -> {value!r} "
            f"(UE5 requires a dot)")
    rendered = ini.render_value(value, st.get("quoted", False))
    if rendered is None:
        log(f"WARN value for {st['key']} contains a double quote — UE5 won't parse this. Skipped.")
        skipped_reject += 1
        continue
    try:
        if apply_one(path, st.get("section"), st["key"], rendered):
            applied += 1
        else:
            skipped_reject += 1
    except Exception:
        errored += 1
        log(f"ERROR while applying {env_name} → {st['key']}:")
        traceback.print_exc(file=sys.stdout)

# ----------------------------------------------------------------------
# Deep Desert instance-picker routing (issue #106). The engine default for
# DeepDesert_1 is SelectionRule="FirstOfGroup", under which the client's
# instance pick is NOT honored — the Director routes to the first instance
# of the group. Survival_1 ships "HomeDimension" by default, which is why
# multi-sietch picking works out of the box. True swaps DD to HomeDimension
# via a -/+ pair on the shared m_BattlegroupsAllMapSettings array (only the
# DeepDesert_1 tuple is touched); False removes the pair (engine default
# returns); unset/empty leaves the file alone (hand edits survive).
# Tuples verified byte-identical to this depot's DefaultGame.ini.
# ----------------------------------------------------------------------
MM_SECTION = "/Script/DuneSandbox.MatchmakerEventsSettings"
MM_KEY = "m_BattlegroupsAllMapSettings"
MM_FIRST = '(MapName="DeepDesert_1",MapSettings=(SelectionRule="FirstOfGroup",MaxPlayerCapacity=100,IsStartingMap=False))'
MM_HOME = '(MapName="DeepDesert_1",MapSettings=(SelectionRule="HomeDimension",MaxPlayerCapacity=100,IsStartingMap=False))'

routing = os.environ.get("DUNE_DD_PICKER_ROUTING", "").strip().lower()
if routing:
    ug_path = FILES["UserGame"]
    if routing in ("true", "1", "false", "0"):
        enable = routing in ("true", "1")
        pair = [f"-{MM_KEY}={MM_FIRST}", f"+{MM_KEY}={MM_HOME}"] if enable else []
        # Same failure contract as every other write in this pass: an I/O or
        # engine error must skip THIS feature, never abort the boot (the
        # entrypoint runs under set -e).
        try:
            if os.path.isfile(ug_path):
                with open(ug_path, "r", encoding="utf-8", errors="replace") as f:
                    ug_text = f.read()
                ug_new = ini.upsert_matched(ug_text, MM_SECTION, MM_KEY, [MM_FIRST, MM_HOME], pair)
                with open(ug_path, "w", encoding="utf-8") as f:
                    f.write(ug_new)
                log(f"  UserGame.ini [{MM_SECTION}] DeepDesert_1 SelectionRule = "
                    + ("HomeDimension (picker choice honored)" if enable else "engine default (FirstOfGroup)"))
                if enable:
                    # Drift sentinel: exactly our -/+ pair should carry the DD
                    # tuple. More live lines means a format-drifted duplicate
                    # (e.g. a game patch changed MaxPlayerCapacity) that the
                    # exact-value matcher could not remove.
                    live = sum(1 for l in ug_new.splitlines()
                               if l.lstrip().lower().startswith(("+" + MM_KEY.lower(), "-" + MM_KEY.lower(),
                                                                 MM_KEY.lower(), "." + MM_KEY.lower()))
                               and 'MapName="DeepDesert_1"' in l)
                    if live != 2:
                        log(f"WARN {live} live DeepDesert_1 routing tuple(s) found where 2 were expected — "
                            "a format-drifted duplicate may need hand cleanup in UserGame.ini "
                            f"[{MM_SECTION}].")
            else:
                log(f"WARN target {ug_path} missing — DD picker routing skipped")
        except Exception:
            errored += 1
            log("ERROR while applying DUNE_DD_PICKER_ROUTING:")
            traceback.print_exc(file=sys.stdout)
    else:
        log(f"WARN DUNE_DD_PICKER_ROUTING={routing!r} not understood (True/False) — skipped")
    if routing in ("false", "0") and os.environ.get("DUNE_PVP_PARTITIONS", "").strip():
        log("WARN DUNE_PVP_PARTITIONS is set but picker routing is False — "
            "players may not be routed to the instance they pick.")

log(f"pass complete: applied={applied} skipped(unset)={skipped_unset} skipped(rejected)={skipped_reject} errored={errored}")
PYEOF
