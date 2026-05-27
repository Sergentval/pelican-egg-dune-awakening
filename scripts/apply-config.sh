#!/bin/bash
# Apply Pelican panel variables to Funcom's UE5 ini files.
#
# Mirrors the surface CubeCoders' AMP template exposes through its
# metaconfig automap. We support three target sinks:
#
#   - UserEngine.ini  → [ConsoleVariables] section (UE5 cvars)
#   - UserGame.ini    → [/Script/...] sections   (UE5 property classes)
#   - ondemand.ini    → flat key=value             (mock-k8s pool tuning)
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
import os
import re
import sys
import traceback

BASE = os.environ["DUNE_APPLY_BASE"]
USERENGINE = f"{BASE}/server/state/ue5-saved/UserSettings/UserEngine.ini"
USERGAME = f"{BASE}/server/state/ue5-saved/UserSettings/UserGame.ini"
ONDEMAND = f"{BASE}/server/state/ondemand.ini"

# (env_var, target_file, section_or_None, ini_key, quote_string_value)
# section=None ⇒ flat KVP (ondemand.ini style).
APPLY_MAP = [
    # --- World identity (was hand-rolled in entrypoint before this script) ---
    ("DUNE_SERVER_PASSWORD",             USERENGINE, "ConsoleVariables", "Bgd.ServerLoginPassword",                            True),
    ("DUNE_SERVER_DISPLAY_NAME",         USERENGINE, "ConsoleVariables", "Bgd.ServerDisplayName",                              True),

    # --- Resource yield (loot) ---
    ("DUNE_MINING_OUTPUT",               USERENGINE, "ConsoleVariables", "Dune.GlobalMiningOutputMultiplier",                  False),
    ("DUNE_VEHICLE_MINING_OUTPUT",       USERENGINE, "ConsoleVariables", "Dune.GlobalVehicleMiningOutputMultiplier",           False),
    ("DUNE_PVP_ZONE_RESOURCE_MULT",      USERENGINE, "ConsoleVariables", "SecurityZones.PvpResourceMultiplier",                False),

    # --- Vehicle behaviour ---
    ("DUNE_VEHICLE_DURABILITY_DAMAGE",   USERENGINE, "ConsoleVariables", "dw.VehicleDurabilityDamageMultiplier",               False),
    ("DUNE_SANDWORM_INVULN_ON_EXIT",     USERENGINE, "ConsoleVariables", "Vehicle.SandwormInvulnerabilitySecondsOnExit",       False),
    ("DUNE_SANDWORM_INVULN_ON_RESTART",  USERENGINE, "ConsoleVariables", "Vehicle.SandwormInvulnerabilitySecondsOnServerRestart", False),
    ("DUNE_SANDWORM_VEHICLE_COLLISION",  USERENGINE, "ConsoleVariables", "Vehicle.SandwormCollisionInteraction",               False),

    # --- World conditions / weather ---
    ("DUNE_SANDSTORMS_ENABLED",          USERENGINE, "ConsoleVariables", "Sandstorm.Enabled",                                  False),
    ("DUNE_SANDSTORM_TREASURE",          USERENGINE, "ConsoleVariables", "Sandstorm.Treasure.Enabled",                         False),
    ("DUNE_SANDWORMS_ENABLED",           USERENGINE, "ConsoleVariables", "sandworm.dune.Enabled",                              False),
    ("DUNE_SANDWORM_DANGER_ZONES",       USERENGINE, "ConsoleVariables", "Sandworm.SandwormDangerZonesEnabled",                False),

    # --- PvP / safety toggles (UserGame.ini UClass properties) ---
    ("DUNE_FORCE_PVP_EVERYWHERE",        USERGAME,   "/Script/DuneSandbox.PvpPveSettings",       "m_bShouldForceEnablePvpOnAllPartitions", False),
    ("DUNE_SECURITY_ZONES_ENABLED",      USERGAME,   "/Script/DuneSandbox.SecurityZonesSubsystem", "m_bAreSecurityZonesEnabled",          False),
    ("DUNE_CORIOLIS_AUTO_SPAWN",         USERGAME,   "/Script/DuneSandbox.SandStormConfig",      "m_bCoriolisAutoSpawnEnabled",            False),

    # --- Decay & building limits ---
    ("DUNE_ITEM_DETERIORATION_RATE",     USERGAME,   "/DeteriorationSystem.ItemDeteriorationConstants", "UpdateRateInSeconds",             False),
    ("DUNE_MAX_LANDCLAIM_SEGMENTS",      USERGAME,   "/Script/DuneSandbox.BuildingSettings",     "m_MaxNumLandclaimSegments",              False),
    ("DUNE_BLUEPRINT_MAX_EXTENSIONS",    USERGAME,   "/Script/DuneSandbox.BuildingSettings",     "m_BuildingBlueprintMaxExtensions",       False),
    ("DUNE_BASE_BACKUP_MAX_EXTENSIONS",  USERGAME,   "/Script/DuneSandbox.BuildingSettings",     "m_BaseBackupMaxExtensions",              False),
    ("DUNE_BUILDING_RESTRICTION_LIMITS", USERGAME,   "/Script/DuneSandbox.BuildingSettings",     "m_bBuildingRestrictionLimitsEnabled",    False),

    # --- On-demand server pool (mock-k8s consumes ondemand.ini, flat KVP) ---
    ("DUNE_MAX_CONCURRENT_INSTANCES",    ONDEMAND,   None,                                       "MaxConcurrentInstances",                 False),
    ("DUNE_AUTO_STOP_DURATION",          ONDEMAND,   None,                                       "AutomaticStopDuration",                  False),
    ("DUNE_ALWAYS_WARM_MAPS",            ONDEMAND,   None,                                       "AlwaysWarmMaps",                         False),
]


def log(msg):
    print(f"[apply-config] {msg}", flush=True)


def apply_keyed(path, section, key, value, quoted):
    """Update or insert key=value under [section] in an INI file.
    Returns True if the file was (re)written, False on skip/reject."""
    if not os.path.isfile(path):
        log(f"WARN target {path} missing — [{section}] {key} skipped")
        return False

    if quoted:
        if '"' in value:
            log(f"WARN value for {key} contains a double quote — UE5 won't parse this. Skipped.")
            return False
        rendered = f'"{value}"'
    else:
        rendered = value

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines(True)

    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    key_re = re.compile(rf"^[;\s]*{re.escape(key)}\s*=", re.IGNORECASE)

    out = []
    in_target = False
    section_seen = False
    key_done = False

    for line in lines:
        if section_re.match(line):
            section_seen = True
            in_target = True
            out.append(line)
            continue
        if any_section_re.match(line):
            if in_target and not key_done:
                # Section ran out of lines — inject before the next header.
                if out and out[-1].strip() == "":
                    out.insert(len(out) - 1, f"{key}={rendered}\n")
                else:
                    out.append(f"{key}={rendered}\n")
                key_done = True
            in_target = False
            out.append(line)
            continue
        if in_target and not key_done and key_re.match(line):
            out.append(f"{key}={rendered}\n")
            key_done = True
            continue
        out.append(line)

    if not section_seen:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n[{section}]\n{key}={rendered}\n")
    elif not key_done:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"{key}={rendered}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    log(f"  {os.path.basename(path)} [{section}] {key}={rendered}")
    return True


def apply_kvp(path, key, value):
    """Update or insert key=value in a flat key=value file (no sections).
    Returns True if the file was (re)written, False on skip."""
    if not os.path.isfile(path):
        log(f"WARN target {path} missing — {key} skipped")
        return False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines(True)

    key_re = re.compile(rf"^[;\s]*{re.escape(key)}\s*=", re.IGNORECASE)
    replaced = False
    out = []
    for line in lines:
        if not replaced and key_re.match(line):
            out.append(f"{key}={value}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    log(f"  {os.path.basename(path)} {key}={value}")
    return True


applied = 0
skipped_unset = 0
skipped_reject = 0
errored = 0
for entry in APPLY_MAP:
    env_name, path, section, key, quoted = entry
    raw = os.environ.get(env_name, "")
    if not raw:
        skipped_unset += 1
        continue
    try:
        if section is None:
            ok = apply_kvp(path, key, raw)
        else:
            ok = apply_keyed(path, section, key, raw, quoted)
        if ok:
            applied += 1
        else:
            skipped_reject += 1
    except Exception:
        errored += 1
        log(f"ERROR while applying {env_name} → {key}:")
        traceback.print_exc(file=sys.stdout)

log(f"pass complete: applied={applied} skipped(unset)={skipped_unset} skipped(rejected)={skipped_reject} errored={errored}")
PYEOF
