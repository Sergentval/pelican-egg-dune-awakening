// Read-only Database tab. The backend already exposed POST /api/database/sql
// (read-only: SELECT / WITH / EXPLAIN / SHOW, capped at 200 rows) but nothing
// in the UI surfaced it — so calibrating the map or diagnosing a base meant
// telling operators to shell into Postgres on the host. This gives them a
// first-class query console in the panel instead. Writes are rejected server-
// side; this is inspection only.

import { useState, type Dispatch, type SetStateAction } from "react";
import { runSql, type DbSqlResult } from "./api";
import { pushToConsole, type ConsoleEntry } from "./components";

interface Preset {
  label: string;
  sql: string;
}

// Genuinely useful starting points — the two that come up most (who owns which
// base, and where things are in the Deep Desert for map calibration), plus a
// schema browser. All strictly read-only.
const PRESETS: Preset[] = [
  {
    label: "Bases + owners",
    sql: `SELECT b.id AS base_id, a.id AS actor_id, a.map,
       COUNT(DISTINCT bi.instance_id) AS pieces,
       a.owner_account_id,
       COALESCE(convert_from(ps.encrypted_character_name,'UTF8'),'') AS owner_via_account
FROM dune.buildings b
JOIN dune.building_instances bi ON bi.building_id = b.id
JOIN dune.actor_fgl_entities afe ON afe.entity_id = bi.owner_entity_id
JOIN dune.actors a ON a.id = afe.actor_id
LEFT JOIN dune.encrypted_player_state ps ON ps.account_id = a.owner_account_id
GROUP BY b.id, a.id, a.map, a.owner_account_id, ps.encrypted_character_name
ORDER BY pieces DESC;`,
  },
  {
    label: "Deep Desert coordinates",
    sql: `SELECT id,
       ((transform).location).x AS world_x,
       ((transform).location).y AS world_y,
       class
FROM dune.actors
WHERE map = 'DeepDesert'
ORDER BY id;`,
  },
  {
    label: "List tables",
    sql: `SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'dune'
ORDER BY table_name;`,
  },
];

export function DatabaseTab({ setConsoleEntries }: {
  setConsoleEntries: Dispatch<SetStateAction<ConsoleEntry[]>>;
}) {
  const [sql, setSql] = useState("");
  const [result, setResult] = useState<DbSqlResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    const q = sql.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    const res = await runSql(q);
    setBusy(false);
    if (res.ok) {
      const body = res.body as DbSqlResult;
      setResult(body);
      pushToConsole(setConsoleEntries, "db-sql", `${body.rows.length} row(s)${body.truncated ? " (truncated)" : ""}`, true);
    } else {
      const b = res.body as { error?: string; detail?: string };
      setResult(null);
      const msg = b.detail ? `${b.error}: ${b.detail}` : (b.error || "query failed");
      setError(msg);
      pushToConsole(setConsoleEntries, "db-sql", msg, false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Database</h2>
          <span className="text-xs text-slate-500">read-only · max 200 rows</span>
        </header>
        <div className="p-4 space-y-3">
          <p className="text-xs text-slate-500">
            Runs against the game database. Only <span className="font-mono text-slate-300">SELECT</span>,{" "}
            <span className="font-mono text-slate-300">WITH</span>, <span className="font-mono text-slate-300">EXPLAIN</span>{" "}
            and <span className="font-mono text-slate-300">SHOW</span> are accepted — writes are rejected by the server.
          </p>

          <div className="flex flex-wrap gap-2">
            {PRESETS.map((p) => (
              <button key={p.label} className="btn-ghost text-xs" onClick={() => setSql(p.sql)}>
                {p.label}
              </button>
            ))}
          </div>

          <textarea
            className="input-field w-full font-mono text-xs min-h-[8rem]"
            spellCheck={false}
            placeholder="SELECT ... FROM dune.actors LIMIT 20;"
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void run(); }}
          />

          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={() => void run()} disabled={busy || !sql.trim()}>
              {busy ? "running…" : "Run"}
            </button>
            <span className="text-[11px] text-slate-600">⌘/Ctrl + Enter</span>
          </div>

          {error && <p className="text-sm text-red-400 whitespace-pre-wrap break-words">{error}</p>}
        </div>
      </div>

      {result && (
        <div className="card">
          <header className="card-header">
            <h3 className="card-title">Result</h3>
            <span className="text-xs text-slate-500">
              {result.rows.length} row(s){result.truncated ? " · truncated at 200" : ""}
            </span>
          </header>
          <div className="card-body">
            {result.headers.length === 0 ? (
              <p className="text-sm text-slate-500 italic">No columns.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="tbl text-xs">
                  <thead>
                    <tr>
                      {result.headers.map((h) => (
                        <th key={h} className="text-left font-mono whitespace-nowrap pr-4">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {result.rows.map((r, ri) => (
                      <tr key={ri} className="border-t border-slate-800/70">
                        {r.map((c, ci) => (
                          <td key={ci} className="pr-4 py-0.5 whitespace-nowrap max-w-[24rem] truncate" title={c}>{c}</td>
                        ))}
                      </tr>
                    ))}
                    {result.rows.length === 0 && (
                      <tr><td className="py-1 text-slate-500 italic" colSpan={result.headers.length}>0 rows.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
