#!/usr/bin/env python3
"""Player-triggered `!commands` from in-game chat — pure logic + the tick.

Mechanism (ported from coastal-ms/DST-DuneServerTool v13.4, Apache-2.0 — see
ATTRIBUTION.md; proven live by them 2026-08-04): the game broker carries a
TOPIC exchange `chat.intercept`, catch-all bound to Funcom's own
queue.intercept. Binding a SECOND bounded queue alongside yields a COPY of
every chat message and leaves Funcom's consumer untouched. admin-publish.sh
owns the broker plumbing (chat-queue-init / chat-drain / chat-queue-drop);
this module owns everything testable: envelope parsing, command extraction,
per-player+command cooldowns, config normalization, and the tick
orchestration that maps a command to an existing admin-publish action.

SAFETY POSTURE (upstream's, kept deliberately): OFF by default; per-command
opt-in; per-player cooldowns; commands only ever act on THE SENDER; the
copy-queue is bounded (max-length + TTL, drop-head) so a stopped drainer can
never pressure the broker; when disabled, the tick DROPS the queue so a
disabled panel is not quietly accumulating a copy of everything players type.
"""
import json
import os
import re
import subprocess
import sys
import time

CONFIG_PATH = "data/admin/chat-commands.json"
STATE_PATH = "server/state/admin/chat-commands-state.json"
COMMAND_RE = re.compile(r"^!([A-Za-z]{1,32})(?:\s+(.*))?$")
DRAIN_MAX = 25


def default_config() -> dict:
    return {
        "enabled": False,
        "cooldown_secs": 60,
        "commands": {
            # ping is the liveness probe — harmless, so it ships enabled and
            # activates the moment the MASTER switch turns on.
            "ping": {"enabled": True},
            # kit grants the configured item pack TO THE SENDER, via the same
            # give-item path the panel uses. Off until an operator both fills
            # the pack and flips it on.
            "kit": {"enabled": False, "items": []},
        },
    }


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def normalize_config(raw) -> dict:
    """Coerce an operator-edited config into the exact shape the tick trusts.
    Junk degrades to defaults rather than crashing the daemon."""
    cfg = default_config()
    src = raw if isinstance(raw, dict) else {}
    cfg["enabled"] = _as_bool(src.get("enabled", cfg["enabled"]))
    try:
        cd = int(src.get("cooldown_secs", cfg["cooldown_secs"]))
        cfg["cooldown_secs"] = cd if 1 <= cd <= 86400 else default_config()["cooldown_secs"]
    except (TypeError, ValueError):
        pass
    commands = src.get("commands") if isinstance(src.get("commands"), dict) else {}
    for name, spec in cfg["commands"].items():
        user = commands.get(name) if isinstance(commands.get(name), dict) else {}
        spec["enabled"] = _as_bool(user.get("enabled", spec["enabled"]))
        if "items" in spec:
            items = []
            for it in (user.get("items") if isinstance(user.get("items"), list) else []):
                if not isinstance(it, dict):
                    continue
                template = str(it.get("template") or "").strip()
                try:
                    qty = max(1, min(int(it.get("qty", 1)), 1000))
                except (TypeError, ValueError):
                    qty = 1
                if template:
                    items.append({"template": template, "qty": qty})
            spec["items"] = items
    return cfg


def parse_chat_message(raw: str) -> "dict | None":
    """Outer {Type: TextChat, content: "<inner JSON STRING>"} → {channel,
    sender, text}. Anything that isn't a well-formed TextChat is None — the
    copy-queue sees every message type players can generate."""
    try:
        outer = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(outer, dict) or outer.get("Type") != "TextChat":
        return None
    try:
        inner = json.loads(outer.get("content") or "")
    except ValueError:
        return None
    if not isinstance(inner, dict):
        return None
    message = inner.get("m_Message") if isinstance(inner.get("m_Message"), dict) else {}
    return {
        "channel": str(inner.get("m_ChannelType") or ""),
        "sender": str(inner.get("m_FuncomIdFrom") or ""),
        "text": str(message.get("m_UnlocalizedMessage") or ""),
    }


def command_of(text: str) -> "tuple[str, str] | None":
    """(command, args) for a `!command` chat line, else None. The charset is
    strict short ascii letters — everything else is just chat."""
    m = COMMAND_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").strip()


class CooldownLedger:
    """Per-(player, command) cooldown with a JSON-serializable state that
    prunes expired entries — the state file must not grow with every player
    who ever typed a command."""

    def __init__(self, cooldown_secs: int, state: "dict | None" = None):
        self.cooldown = max(1, int(cooldown_secs))
        self._last: "dict[str, float]" = {}
        if isinstance(state, dict):
            for k, v in state.items():
                try:
                    self._last[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        self._now = 0.0

    def allow(self, player: str, command: str, now: "float | None" = None) -> bool:
        ts = time.time() if now is None else float(now)
        self._now = max(self._now, ts)
        key = f"{player}|{command}"
        last = self._last.get(key)
        if last is not None and ts - last < self.cooldown:
            return False
        self._last[key] = ts
        return True

    def state(self) -> dict:
        cutoff = self._now - self.cooldown
        return {k: v for k, v in self._last.items() if v > cutoff}


# --------------------------------------------------------------------------
# Tick orchestration (I/O below this line; logic above stays pure).
# --------------------------------------------------------------------------

def _publish(base: str, argv: "list[str]", timeout: int = 30) -> "tuple[bool, str]":
    res = subprocess.run(["bash", os.path.join(base, "scripts", "admin-publish.sh"), *argv],
                         capture_output=True, text=True, timeout=timeout)
    out = (res.stdout or "") + (res.stderr or "")
    return res.returncode == 0, out[-500:]


def _load_json(path: str, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def _save_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def run_tick(base: str, log=print) -> int:
    """One scheduler tick: honor the master switch, drain the copy-queue, and
    execute at most DRAIN_MAX commands. Returns the number executed."""
    cfg = normalize_config(_load_json(os.path.join(base, CONFIG_PATH), {}))
    state_path = os.path.join(base, STATE_PATH)
    state = _load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}

    if not cfg["enabled"]:
        # A disabled feature must not keep a copy of all chat around: drop the
        # queue ONCE, remember we did, and go back to sleep.
        if state.get("queue_ready"):
            _publish(base, ["chat-queue-drop"])
            state["queue_ready"] = False
            _save_json(state_path, state)
        return 0

    if not state.get("queue_ready"):
        ok, out = _publish(base, ["chat-queue-init"])
        if not ok:
            log(f"[chat-commands] queue init failed: {out}")
            return 0
        state["queue_ready"] = True
        _save_json(state_path, state)

    ok, out = _publish(base, ["chat-drain", str(DRAIN_MAX)])
    if not ok:
        log(f"[chat-commands] drain failed: {out}")
        return 0

    import base64
    ledger = CooldownLedger(cfg["cooldown_secs"], state.get("cooldowns"))
    executed = 0
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("MSG:"):
            continue
        try:
            raw = base64.b64decode(line[4:]).decode("utf-8", "replace")
        except ValueError:
            continue
        msg = parse_chat_message(raw)
        if not msg or not msg["sender"]:
            continue
        cmd = command_of(msg["text"])
        if not cmd:
            continue
        name, _args = cmd
        spec = cfg["commands"].get(name)
        if not spec or not spec.get("enabled"):
            continue
        if not ledger.allow(msg["sender"], name):
            continue
        display = msg["sender"].split("#", 1)[0]
        if name == "ping":
            _publish(base, ["broadcast", "Chat commands", f"pong, {display}!", "5"])
            executed += 1
        elif name == "kit":
            fls = _resolve_funcom(base, msg["sender"])
            if not fls:
                log(f"[chat-commands] kit: could not resolve '{msg['sender']}' to an account")
                continue
            granted = 0
            for it in spec.get("items", []):
                gok, gout = _publish(base, ["give-item", fls, it["template"], str(it["qty"])])
                if gok:
                    granted += 1
                else:
                    log(f"[chat-commands] kit item {it['template']} failed: {gout}")
            _publish(base, ["broadcast", "Chat commands",
                            f"{display}: kit delivered ({granted} item type(s))", "8"])
            executed += 1
    state["cooldowns"] = ledger.state()
    _save_json(state_path, state)
    return executed


def _resolve_funcom(base: str, funcom_id: str) -> str:
    ok, out = _publish(base, ["resolve-funcom", funcom_id], timeout=15)
    if not ok:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("fls="):
            return line[4:].strip()
    return ""


def _main(argv: "list[str]") -> int:
    if len(argv) > 2 and argv[1] == "tick":
        n = run_tick(argv[2])
        print(json.dumps({"ok": True, "executed": n}))
        return 0
    print(json.dumps({"ok": False, "error": "usage: tick <BASE_DIR>"}))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
