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

# A bare decimal written with a comma separator: "2,5", "-0,75", "+3,5".
# Deliberately narrow — exactly one comma with digits on both sides — so that
# comma-separated lists ("1,2,3") and grouped forms ("1.000,5") never match.
_DECIMAL_COMMA_RE = re.compile(r"^[+-]?\d+,\d+$")


def coerce_decimal_comma(raw: str, vtype: str) -> str:
    """Rewrite a comma decimal separator to a dot on float-typed settings.

    Reported in issue #82: on a German (or French, Spanish, …) system the
    operator naturally types `2,5` into the panel. UE5 reads INI floats with
    atof, which stops at the comma — the server runs 2 instead of 2.5 and
    never reports an error, so the setting looks applied but isn't.

    Only `float` is coerced: `intlist` values are legitimately comma-separated,
    and struct/array values carry their own commas. Anything that isn't a bare
    decimal passes through untouched so it fails validation downstream rather
    than being silently guessed at.
    """
    if vtype.lower() != "float":
        return raw
    stripped = raw.strip()
    if not _DECIMAL_COMMA_RE.match(stripped):
        return raw
    return stripped.replace(",", ".")


def render_value(raw: str, quoted: bool) -> str | None:
    """Render a value for an INI line. When quoted, wrap in double quotes;
    return None to REJECT a quoted value that itself contains a double quote
    (UE5 won't parse it). Unquoted values pass through unchanged.

    A BLANK value renders bare (`key=`) even for quoted settings: that is the
    form AMP's automap writes for a blank field ("Leave blank to disable" on
    the join password) and the form install.sh seeds into the UserEngine.ini
    template — `""` has never been observed against UE5's cvar parser.
    Issue #107."""
    if quoted:
        if '"' in raw:
            return None
        if raw == "":
            return ""
        return f'"{raw}"'
    return raw


def env_value_to_apply(environ, env_name: str, empty_ok: bool) -> str | None:
    """Value of one panel variable to apply at boot, or None to skip the row.

    Absent variable ⇒ None: a run without panel variables (TESTING.md Path A,
    plain docker) must leave hand-edited INIs alone. Present-but-empty ⇒ None
    too, UNLESS the setting declares empty_ok in settings-schema.json — for
    those the blank is an explicit operator choice (issue #107: a cleared
    join password means a public passwordless server), not "unconfigured".
    """
    raw = environ.get(env_name)
    if raw is None:
        return None
    if raw == "" and not empty_ok:
        return None
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


def upsert_repeated(text: str, section: str, key: str, values: "list[str]") -> str:
    """Replace every LIVE occurrence of UE array `key` under `[section]` with
    one `+key=value` line per element of `values`, in order, at the position
    of the first replaced line (or the end of the section).

    UE5 config arrays take one line per element: `+key=` appends, a bare
    `key=` resets to a single element, `!key=` clears — leaving any of those
    behind would fight the lines written here, so all live forms go. Commented
    `;+key=` lines (Funcom ships such examples in its template) are preserved.
    Empty `values` just removes the live lines; a missing section is created
    only when there is something to write. Issue #106."""
    lines = text.splitlines(keepends=True)
    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    live_re = re.compile(rf"^\s*[-+.!]?{re.escape(key)}\s*=", re.IGNORECASE)
    new_lines = [f"+{key}={v}\n" for v in values]

    out: list[str] = []
    in_target = False
    section_seen = False
    written = False

    for line in lines:
        if section_re.match(line):
            section_seen = True
            in_target = True
            out.append(line)
            continue
        if any_section_re.match(line):
            if in_target and not written and new_lines:
                # Section ran out of lines — inject before the next header,
                # keeping a trailing blank line as the separator.
                if out and out[-1].strip() == "":
                    for nl in new_lines:
                        out.insert(len(out) - 1, nl)
                else:
                    out.extend(new_lines)
                written = True
            in_target = False
            out.append(line)
            continue
        if in_target and live_re.match(line):
            if not written:
                out.extend(new_lines)
                written = True
            continue  # drop every live form of the key
        out.append(line)

    if not section_seen:
        if not new_lines:
            return text
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n[{section}]\n")
        out.extend(new_lines)
    elif in_target and not written and new_lines:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.extend(new_lines)

    return "".join(out)


def upsert_matched(text: str, section: str, key: str,
                   match_values: "list[str]", new_lines: "list[str]") -> str:
    """Replace the UE array entries of `key` under `[section]` whose VALUE is
    in `match_values` — whatever their prefix (`+`, `-`, `.`, `!` or bare) —
    with `new_lines` (COMPLETE prefixed lines, e.g. `-key=(...)`), inserted as
    one block at the first match, or at the end of the section.

    Unlike upsert_repeated, which owns every live line of its key, this only
    touches the listed values: other entries of the same UE array key (other
    maps' tuples, operator additions) survive. Empty `new_lines` just removes;
    a missing section is created only when there is something to write.
    Comments (`;`-prefixed) are preserved. Issue #106: the Deep Desert
    instance-picker routing swap (SelectionRule FirstOfGroup → HomeDimension)
    is one -/+ pair inside an array shared with every other map."""
    lines = text.splitlines(keepends=True)
    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    live_re = re.compile(rf"^\s*[-+.!]?{re.escape(key)}\s*=(.*)$", re.IGNORECASE)
    targets = set(match_values)
    block = [ln if ln.endswith("\n") else ln + "\n" for ln in new_lines]

    out: list[str] = []
    in_target = False
    section_seen = False
    written = False

    for line in lines:
        if section_re.match(line):
            section_seen = True
            in_target = True
            out.append(line)
            continue
        if any_section_re.match(line):
            if in_target and not written and block:
                if out and out[-1].strip() == "":
                    for nl in block:
                        out.insert(len(out) - 1, nl)
                else:
                    out.extend(block)
                written = True
            in_target = False
            out.append(line)
            continue
        if in_target:
            m = live_re.match(line)
            if m and m.group(1).strip() in targets:
                if not written:
                    out.extend(block)
                    written = True
                continue  # drop every matched entry
        out.append(line)

    if not section_seen:
        if not block:
            return text
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n[{section}]\n")
        out.extend(block)
    elif in_target and not written and block:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.extend(block)

    return "".join(out)


def read_repeated(text: str, section: str, key: str) -> "str | None":
    """Comma-joined values of the live `+key=`/`key=` lines under `[section]`,
    in file order, or None when no live line exists (commented `;+key=`
    examples don't count, and a `!key=ClearArray` line carries no value)."""
    section_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    live_re = re.compile(rf"^\s*[+.]?{re.escape(key)}\s*=(.*)$", re.IGNORECASE)
    in_target = False
    vals: list[str] = []
    for line in text.splitlines():
        if section_re.match(line):
            in_target = True
            continue
        if any_section_re.match(line):
            in_target = False
            continue
        if in_target:
            m = live_re.match(line)
            if m:
                vals.append(m.group(1).strip())
    return ",".join(vals) if vals else None


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
        return str(float(coerce_decimal_comma(s, "float")))
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
