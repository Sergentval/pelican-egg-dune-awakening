#!/usr/bin/env python3
"""Per-sietch settings overrides — heterogeneous sietches (PvP/PvE, names, economy).

A "sietch" = a Survival_1 partition (dimension_index>0). Each is its own UE5
process that reads its config from its own $HOME (UserEngine.ini / UserGame.ini).
So a sietch can have its OWN name + gameplay settings by giving it a per-instance
config dir: a fresh copy of the shared Phase-5 config with this sietch's overrides
(and display name) merged in. Done via the INI files (not -ini CLI), because UE5
mangles spaces in CLI cvar values — the INI file form handles spaces, exactly how
the global server name does.

Source of truth = a small per-sietch overrides JSON {schema_id: value}; the
materialize step (called by start-ue5.sh at spawn) renders it into the per-instance
UserEngine/UserGame.ini. Display name comes from the sietch label (DUNE_INSTANCE_
DISPLAY_NAME), merged as Bgd.ServerDisplayName.

Reuses admin_ini_merge.normalize_value/render_value/upsert_keyed so per-sietch
values follow the EXACT same rules as the global Settings tab. Only UE5-process
settings (file != "ondemand") are per-sietch-capable. Pure + I/O only (no DB/network).
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin_ini_merge  # noqa: E402  # type: ignore[import-not-found]

# UE5-process config files (overridable per instance) -> the target ini filename.
# UserOverrides is appended into UserGame each boot, so it merges into UserGame.ini.
_FILE_TO_INI = {"UserEngine": "UserEngine.ini", "UserGame": "UserGame.ini", "UserOverrides": "UserGame.ini"}


def _schema_path(base):
    return os.path.join(base, "data", "admin", "settings-schema.json")


def _overrides_dir(base):
    return os.path.join(base, "server", "state", "sietch-overrides")


def _json_path(base, pid):
    return os.path.join(_overrides_dir(base), f"{pid}.json")


def load_schema(base):
    try:
        with open(_schema_path(base), encoding="utf-8") as f:
            return json.load(f).get("settings", [])
    except (OSError, ValueError):
        return []


def is_per_sietch(st):
    """A setting is per-sietch-capable iff it lives in a UE5-process config a
    single instance reads (so a per-instance copy can override it). Repeated
    (UE array) settings are battlegroup-wide designations every instance must
    agree on — e.g. m_PvpEnabledPartitions (issue #106) — never per-sietch."""
    return st.get("file") in _FILE_TO_INI and not st.get("repeated")


def capable(base):
    """The catalogue of per-sietch-overridable settings (powers the editor)."""
    return [
        {k: st.get(k) for k in ("id", "label", "category", "section", "key", "type", "enum", "quoted", "default", "verified")}
        for st in load_schema(base)
        if is_per_sietch(st)
    ]


def validate_overrides(base, overrides):
    """Pure: split {id:value} into (clean, skipped). clean keeps only known
    per-sietch ids whose value normalizes for its type."""
    schema = {st["id"]: st for st in load_schema(base) if is_per_sietch(st)}
    clean, skipped = {}, []
    for sid, raw in (overrides or {}).items():
        st = schema.get(sid)
        if not st or admin_ini_merge.normalize_value(str(raw), st.get("type", "string"), st.get("enum")) is None:
            skipped.append(sid)
            continue
        clean[sid] = raw
    return clean, skipped


def get_overrides(base, pid):
    try:
        with open(_json_path(base, pid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_overrides(base, pid, overrides):
    """Persist this sietch's {id:value} overrides (source of truth). Invalid ids/
    values are dropped (reported). Empty -> remove the file (revert to defaults).
    The actual INI rendering happens in materialize() at spawn."""
    clean, skipped = validate_overrides(base, overrides)
    path = _json_path(base, pid)
    if not clean:
        try:
            os.remove(path)
        except OSError:
            pass
        return {"ok": True, "applied": [], "skipped": skipped, "cleared": True}
    os.makedirs(_overrides_dir(base), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return {"ok": True, "applied": list(clean), "skipped": skipped}


def materialize(base, pid, shared_dir, dest_dir, display_name=""):
    """Build a per-instance UE5 UserSettings dir for this sietch: a FRESH copy of
    the shared config with the sietch's overrides + display name merged into
    UserEngine.ini / UserGame.ini. Rebuilt each spawn so global changes propagate.
    Returns True on success."""
    if not os.path.isdir(shared_dir):
        return False
    # fresh copy of the shared config
    if os.path.islink(dest_dir) or os.path.isfile(dest_dir):
        os.remove(dest_dir)
    elif os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    shutil.copytree(shared_dir, dest_dir)

    schema = {st["id"]: st for st in load_schema(base) if is_per_sietch(st)}
    edits = {"UserEngine.ini": [], "UserGame.ini": []}
    if display_name and '"' not in display_name:
        edits["UserEngine.ini"].append(("ConsoleVariables", "Bgd.ServerDisplayName", f'"{display_name}"'))
    for sid, raw in get_overrides(base, pid).items():
        st = schema.get(sid)
        if not st or not st.get("section") or not st.get("key"):
            continue
        fname = _FILE_TO_INI.get(st["file"])
        norm = admin_ini_merge.normalize_value(str(raw), st.get("type", "string"), st.get("enum"))
        if not fname or norm is None:
            continue
        rendered = admin_ini_merge.render_value(norm, bool(st.get("quoted")))
        if rendered is None:
            continue
        edits.setdefault(fname, []).append((st["section"], st["key"], rendered))

    for fname, rows in edits.items():
        if not rows:
            continue
        path = os.path.join(dest_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        for section, key, rendered in rows:
            text = admin_ini_merge.upsert_keyed(text, section, key, rendered)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    return True


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    base = os.environ.get("DUNE_BASE_DIR", "/home/container")
    if cmd in ("capable", "get", "set", "clear") and len(argv) > 2 and os.path.isdir(argv[-1]):
        base = argv[-1]
    if cmd == "capable":
        print(json.dumps({"ok": True, "settings": capable(base)}))
        return 0
    if cmd == "get" and len(argv) > 2:
        print(json.dumps({"ok": True, "overrides": get_overrides(base, argv[2])}))
        return 0
    if cmd == "set" and len(argv) > 3:
        try:
            ov = json.loads(argv[3])
        except ValueError:
            ov = None
        if not isinstance(ov, dict):
            print(json.dumps({"ok": False, "error": "overrides must be a JSON object"}))
            return 1
        print(json.dumps(write_overrides(base, argv[2], ov)))
        return 0
    if cmd == "clear" and len(argv) > 2:
        print(json.dumps(write_overrides(base, argv[2], {})))
        return 0
    if cmd == "materialize" and len(argv) >= 5:
        name = argv[5] if len(argv) > 5 else ""
        ok = materialize(base, argv[2], argv[3], argv[4], name)
        print(json.dumps({"ok": ok}))
        return 0 if ok else 1
    print("usage: admin_sietch.py <capable|get <pid>|set <pid> <json>|clear <pid>|"
          "materialize <pid> <shared_dir> <dest_dir> [name]> [BASE]  (BASE via $DUNE_BASE_DIR for materialize)",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
