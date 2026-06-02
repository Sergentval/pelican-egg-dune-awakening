"""Pure INI reconciliation engine (Phase 5).

The upsert algorithms here are a faithful, text-based extraction of
apply-config.sh's apply_keyed / apply_kvp so that script can delegate to this
module (and PUT /api/settings can reuse it) without changing behavior:

  - empty/unset value  -> caller skips the row (operator hand-edits survive)
  - section missing     -> append a fresh `[section]` and the key/value
  - key present         -> rewrite in place under the matching section
                           (case-insensitive; a commented `;key=` counts)
  - key missing         -> append the key at the end of the section

All functions are pure (operate on and return strings); file I/O lives in thin
wrappers in the callers. render_value handles the quoted-string rendering used
by string cvars; normalize_value coerces a typed value for the API write path.
"""
import re

_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}


def render_value(raw: str, quoted: bool) -> str | None:
    """Render a value for an INI line. When quoted, wrap in double quotes;
    return None to REJECT a quoted value that itself contains a double quote
    (UE5 won't parse it). Unquoted values pass through unchanged."""
    if quoted:
        if '"' in raw:
            return None
        return f'"{raw}"'
    return raw


def upsert_keyed(text: str, section: str, key: str, rendered: str) -> str:
    """Return `text` with `key=rendered` set under `[section]`. Mirrors
    apply-config.sh apply_keyed exactly."""
    lines = text.splitlines(keepends=True)
    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    key_re = re.compile(rf"^[;\s]*{re.escape(key)}\s*=", re.IGNORECASE)

    out: list[str] = []
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
                # Section ran out of lines — inject before the next header,
                # keeping a trailing blank line as the separator.
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

    return "".join(out)


def upsert_flat(text: str, key: str, value: str) -> str:
    """Return `text` with `key=value` set in a flat (sectionless) KVP file.
    Mirrors apply-config.sh apply_kvp exactly."""
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^[;\s]*{re.escape(key)}\s*=", re.IGNORECASE)
    out: list[str] = []
    replaced = False
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
    return "".join(out)


def read_keyed(text: str, section: str, key: str) -> str | None:
    """Return the current value of `key` under `[section]` (the text after '='),
    or None if the key isn't set there. A commented `;key=` line counts as unset
    (the API reports such a setting at its default)."""
    lines = text.splitlines()
    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$", re.IGNORECASE)
    in_target = False
    for line in lines:
        if section_re.match(line):
            in_target = True
            continue
        if any_section_re.match(line):
            in_target = False
            continue
        if in_target:
            m = key_re.match(line)
            if m:
                return m.group(1).strip()
    return None


def read_flat(text: str, key: str) -> str | None:
    """Return the current value of `key` in a flat (sectionless) KVP file, or
    None if unset. A commented line counts as unset."""
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$", re.IGNORECASE)
    for line in text.splitlines():
        m = key_re.match(line)
        if m:
            return m.group(1).strip()
    return None


def normalize_value(raw, vtype: str, enum: list[str] | None = None) -> str:
    """Coerce/validate a typed value for the API write path, returning the INI
    string form. Raises ValueError on an invalid value so the route can 400.

    Types: bool -> True/False ; int ; float ; enum (membership) ; intlist
    (comma-separated ints) ; string (passthrough). The boot path keeps using
    render_value directly, so this stricter coercion is API-only.
    """
    s = ("" if raw is None else str(raw)).strip()
    t = vtype.lower()
    if t == "bool":
        low = s.lower()
        if low in _BOOL_TRUE:
            return "True"
        if low in _BOOL_FALSE:
            return "False"
        raise ValueError(f"invalid bool: {raw!r}")
    if t == "cvarbool":
        # Console variables take 1/0, not True/False — 1/0 is accepted for every
        # bool/int cvar, so it's the unambiguous form for [ConsoleVariables].
        low = s.lower()
        if low in _BOOL_TRUE:
            return "1"
        if low in _BOOL_FALSE:
            return "0"
        raise ValueError(f"invalid cvarbool: {raw!r}")
    if t == "int":
        return str(int(s))
    if t == "float":
        return str(float(s))
    if t == "enum":
        if enum and s in enum:
            return s
        raise ValueError(f"invalid enum value {raw!r}; allowed: {enum}")
    if t == "intlist":
        if not s:
            return ""
        return ",".join(str(int(p.strip())) for p in s.split(","))
    if t == "string":
        return s
    if t in ("struct", "array"):
        # Advanced UClass values (e.g. damage configs, tax-multiplier arrays):
        # written verbatim, no numeric coercion. INI is line-based, so a value
        # must fit one key=value line — reject newlines and empties. Genuinely
        # multi-line arrays (several +key= lines) are edited in UserGame.ini.
        if "\n" in s or "\r" in s:
            raise ValueError(f"{vtype} value must be a single line; edit UserGame.ini directly for multi-line arrays")
        if not s:
            raise ValueError(f"{vtype} value is empty; set it verbatim from DefaultGame.ini")
        return s
    raise ValueError(f"unknown setting type: {vtype}")
