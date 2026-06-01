"""Pure helpers for the live server status grid (Phase 4).

admin-http.py's GET /api/status builds a per-map grid by merging two sources:

  - mock-k8s /status            -> per-map instance/scale status
                                   (desired / current / status), over HTTPS:6443
  - admin-publish.sh server-status -> per-map live player counts, queried from
                                   dune.actors (admin-http holds no PG connection;
                                   all Postgres flows through admin-publish.sh)

These functions are pure (no I/O) so they are unit-testable; the HTTP route does
the network fetch + subprocess call and hands the raw data here. Nothing here
mutates its inputs.
"""


def parse_player_counts(csv_text):
    """Parse the `server-status` CSV ('map,players' with a header row) into a
    {map: int} dict. Tolerates a leading header, blank lines, and CRLF; rows
    whose count is not a non-negative integer are skipped (defensive against a
    psql error line leaking into the output)."""
    counts = {}
    for line in (csv_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Map names contain no commas; split off the trailing count field.
        parts = line.rsplit(",", 1)
        if len(parts) != 2:
            continue
        name, count = parts[0].strip(), parts[1].strip()
        if not name or name.lower() == "map" or not count.isdigit():
            continue  # header row or malformed/non-numeric count
        counts[name] = int(count)
    return counts


def merge_status(mock_status, player_counts):
    """Return a status grid: each mock-k8s map enriched with its live `players`
    count (0 when absent), plus top-level `totalServers` / `totalPlayers` and
    the carried-over uptime/reconcile/pool blocks.

    Any map that has players but no mock-k8s entry is still emitted (status
    "unknown") rather than silently dropped, so the grid never under-reports who
    is online. Inputs are not mutated.
    """
    mock = mock_status or {}
    counts = dict(player_counts or {})
    rows = []
    total_players = 0
    seen = set()

    for m in mock.get("maps") or []:
        name = m.get("map", "")
        seen.add(name)
        players = int(counts.get(name, 0))
        total_players += players
        row = dict(m)  # copy — don't mutate the caller's snapshot
        row["players"] = players
        rows.append(row)

    # Players on a map mock-k8s doesn't list (e.g. an on-demand instance the
    # snapshot missed) must not vanish from the grid.
    for name, players in counts.items():
        if name in seen:
            continue
        rows.append({
            "map": name,
            "players": int(players),
            "status": "unknown",
            "desired": 0,
            "current": 0,
        })
        total_players += int(players)

    return {
        "maps": rows,
        "totalServers": len(rows),
        "totalPlayers": total_players,
        "uptimeSeconds": mock.get("uptimeSeconds", 0),
        "reconcile": mock.get("reconcile", {}),
        "pool": mock.get("pool", {}),
    }
