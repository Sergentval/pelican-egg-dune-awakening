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
#   - Empty/unset env var ⇒ skip the row entirely (operator hand-edits survive).
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
    raw = os.environ.get(env_name, "")
    if not raw:
        skipped_unset += 1
        continue
    path = FILES.get(st.get("file"))
    if path is None:
        log(f"WARN unknown file sink {st.get('file')!r} for {env_name} — skipped")
        skipped_reject += 1
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

log(f"pass complete: applied={applied} skipped(unset)={skipped_unset} skipped(rejected)={skipped_reject} errored={errored}")
PYEOF
