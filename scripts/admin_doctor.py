#!/usr/bin/env python3
"""Connection doctor — pure verdict logic over gathered connectivity facts.

The classic self-host failure mode is a server that BOOTS fine and looks
healthy in every panel, yet nobody can join or map-travel: the address
advertised to FLS is wrong/private, two maps share one UDP port, a port is
advertised that nothing listens on, or the Director stopped heartbeating.
Facts gathering (SQL over farm_state/world_partition, `ss`, the public-IP
lookup, the director.log tail) lives in admin-publish.sh `doctor` — the only
layer with those connections; this module turns the facts into typed checks:

    {"id", "status": ok|warn|error|skip, "summary", "detail", "hint"}

Read-only by design: the doctor DIAGNOSES; fixes stay explicit operator
actions. Mechanisms ported from coastal-ms/DST-DuneServerTool's P34
connection doctor (Apache-2.0) and Red-Blink/dune-awakening-selfhost-docker's
doctor.sh (MIT) — see ATTRIBUTION.md.
"""
import ipaddress
import json
import sys

HEARTBEAT_WARN_SECS = 120
HEARTBEAT_ERROR_SECS = 300


def is_private_ip(ip: str) -> bool:
    """True for RFC1918/loopback/link-local — an address the internet can't
    reach; advertising one to FLS makes the server unjoinable from outside."""
    try:
        return not ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _check(cid: str, status: str, summary: str, detail: str = "", hint: str = "") -> dict:
    return {"id": cid, "status": status, "summary": summary, "detail": detail, "hint": hint}


def analyze(facts: dict) -> "list[dict]":
    ext = (facts.get("external_ip") or "").strip()
    real = (facts.get("real_ip") or "").strip()
    farm = facts.get("farm") or []
    alive = [r for r in farm if r.get("alive")]
    partition_maps = set(facts.get("partition_maps") or [])
    udp_ports = set(facts.get("udp_ports") or [])
    hb = facts.get("heartbeat_epoch")
    now = facts.get("now_epoch") or 0
    checks: "list[dict]" = []

    # --- the advertised identity ------------------------------------------
    if ext:
        checks.append(_check("external-ip-set", "ok", f"External IP is set ({ext})"))
    else:
        checks.append(_check("external-ip-set", "error", "DUNE_EXTERNAL_IP is empty",
                             hint="Set the panel variable to your public IP — FLS advertises it to every client."))
    if ext and is_private_ip(ext):
        checks.append(_check("external-ip-public", "warn",
                             f"External IP {ext} is a private/loopback address",
                             hint="Clients outside your LAN cannot join. Use the public IP "
                                  "(or keep this deliberately for a LAN-only server)."))
    else:
        checks.append(_check("external-ip-public", "ok",
                             "External IP is a public address" if ext else "n/a"))
    if not real:
        checks.append(_check("external-vs-real", "skip",
                             "Public-IP lookup unavailable (no egress or lookup blocked)",
                             hint="Could not compare DUNE_EXTERNAL_IP against the real public IP."))
    elif ext and real != ext:
        checks.append(_check("external-vs-real", "error",
                             f"DUNE_EXTERNAL_IP ({ext}) differs from the real public IP ({real})",
                             hint="Your WAN IP changed (DHCP/CGNAT?). Update the variable and restart, "
                                  "or joins will time out while everything looks healthy."))
    else:
        checks.append(_check("external-vs-real", "ok", "Advertised IP matches the real public IP"))

    # --- what the maps actually advertise (DST P34's core finding) ---------
    if not farm:
        checks.append(_check("farm-registrations", "warn", "No farm_state rows at all",
                             hint="No map has registered yet — is the server still booting?"))
    else:
        checks.append(_check("farm-registrations", "ok", f"{len(farm)} map registration(s), {len(alive)} alive"))

    drift = [f"{r.get('map')}={r.get('game_addr')}" for r in alive
             if ext and (r.get("game_addr") or "") not in ("", ext)]
    if drift:
        checks.append(_check("advertised-addresses", "error",
                             f"{len(drift)} live map(s) advertise a non-external address",
                             detail=", ".join(sorted(drift)),
                             hint="Players are sent to that address and time out. Usually a stale "
                                  "registration — restart the server so every map re-registers."))
    else:
        checks.append(_check("advertised-addresses", "ok", "All live maps advertise the external IP"))

    igw_drift = [f"{r.get('map')}={r.get('igw_addr')}" for r in alive
                 if ext and (r.get("igw_addr") or "") not in ("", ext)]
    if igw_drift:
        checks.append(_check("igw-addresses", "error",
                             f"{len(igw_drift)} live map(s) advertise a non-external IGW address",
                             detail=", ".join(sorted(igw_drift)),
                             hint="The inter-world gateway is what map TRAVEL uses — a wrong address "
                                  "here hangs travel while direct joins still work."))
    else:
        checks.append(_check("igw-addresses", "ok", "All live maps advertise the external IGW address"))

    ports_seen: "dict[int, list[str]]" = {}
    for r in alive:
        p = r.get("game_port")
        if isinstance(p, int):
            ports_seen.setdefault(p, []).append(str(r.get("map")))
    collisions = {p: maps for p, maps in ports_seen.items() if len(maps) > 1}
    if collisions:
        detail = "; ".join(f"{p}: {', '.join(m)}" for p, m in sorted(collisions.items()))
        checks.append(_check("port-collisions", "error",
                             f"{len(collisions)} UDP port(s) shared by several live maps",
                             detail=detail,
                             hint="The whole-range-to-one-port misconfig (the in-game 2G2 error): "
                                  "every map needs its own UDP port forwarded."))
    else:
        checks.append(_check("port-collisions", "ok", "Every live map has its own UDP port"))

    if udp_ports:
        unbound = sorted({p for p in ports_seen if p not in udp_ports})
        if unbound:
            checks.append(_check("ports-bound", "error",
                                 f"{len(unbound)} advertised port(s) have no UDP listener",
                                 detail=", ".join(str(p) for p in unbound),
                                 hint="A map advertises a port nothing listens on — its process died "
                                      "or the registration is stale. Check the Instances tab."))
        else:
            checks.append(_check("ports-bound", "ok", "Every advertised port has a UDP listener"))
    else:
        checks.append(_check("ports-bound", "ok" if not ports_seen else "skip",
                             "No UDP listener inventory available" if ports_seen else "No live ports to check"))

    stuck = [str(r.get("map")) for r in farm if r.get("alive") and not r.get("ready")]
    if stuck:
        checks.append(_check("ready-stuck", "warn",
                             f"{len(stuck)} alive map(s) not ready: {', '.join(sorted(stuck))}",
                             hint="Normal during boot (UE5 startup takes minutes). If it persists, "
                                  "the READY handshake is stuck — restart that instance."))
    else:
        checks.append(_check("ready-stuck", "ok", "Every alive map reports ready"))

    orphan_maps = sorted({str(r.get("map")) for r in farm
                          if r.get("map") and r.get("map") not in partition_maps})
    if orphan_maps:
        checks.append(_check("farm-partition-coherence", "warn",
                             f"Registered map(s) with no world_partition row: {', '.join(orphan_maps)}",
                             hint="The Director cannot allocate a map with no partition row — "
                                  "players can see it but never load in."))
    else:
        checks.append(_check("farm-partition-coherence", "ok",
                             "Every registered map has a world_partition row"))

    # --- is anyone still talking to FLS ------------------------------------
    if hb is None:
        checks.append(_check("fls-heartbeat", "warn", "No server-state heartbeat found in the log tail",
                             hint="Either the server just booted (log rotated) or the Director stopped "
                                  "reporting — check logs/director.log."))
    else:
        age = max(0, int(now) - int(hb))
        if age > HEARTBEAT_ERROR_SECS:
            checks.append(_check("fls-heartbeat", "error",
                                 f"Last server-state heartbeat is {age}s old",
                                 hint="FLS de-registers silent servers — the world drops out of the "
                                      "in-game browser. Check director/gateway logs."))
        elif age > HEARTBEAT_WARN_SECS:
            checks.append(_check("fls-heartbeat", "warn", f"Last server-state heartbeat is {age}s old"))
        else:
            checks.append(_check("fls-heartbeat", "ok", f"Heartbeat {age}s ago"))

    return checks


def summarize(checks: "list[dict]") -> dict:
    s = {"ok": 0, "warn": 0, "error": 0, "skip": 0, "total": len(checks)}
    for c in checks:
        s[c["status"]] = s.get(c["status"], 0) + 1
    return s


def _main(argv: "list[str]") -> int:
    if len(argv) > 1 and argv[1] == "analyze":
        raw = sys.stdin.read()
        try:
            with open("/tmp/doctor-raw.json", "w", encoding="utf-8") as _f:
                _f.write(raw)
        except OSError:
            pass
        try:
            facts = json.loads(raw)
        except ValueError as exc:
            # Surface WHERE it broke — the facts line is machine-assembled,
            # so a parse error means a gathering bug worth pinpointing fast.
            at = getattr(exc, "pos", 0) or 0
            ctx = raw[max(0, at - 80):at + 40].replace("\n", "⏎")
            window = raw[max(0, at - 20):at + 20]
            print(json.dumps({"ok": False, "error": f"bad facts json: {exc}", "context": ctx,
                              "bytes": [hex(b) for b in window.encode("utf-8", "replace")]}))
            return 1
        checks = analyze(facts if isinstance(facts, dict) else {})
        print(json.dumps({"ok": True, "summary": summarize(checks), "checks": checks,
                          "facts": {k: facts.get(k) for k in
                                    ("external_ip", "real_ip", "heartbeat_epoch", "now_epoch")}}))
        return 0
    print(json.dumps({"ok": False, "error": "usage: analyze  (facts JSON on stdin)"}))
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
