// Instances tab (Phase 0 — read-only topology view).
//
// Combines /api/status (mock-k8s per-map desired/current scale + live player
// counts) with /api/partitions (the world_partition topology joined to
// farm_state liveness) so an operator can see every map instance and every
// partition — warm (dimension 0) vs dimensional sandstorm-tunnel partitions
// (DeepDesert 101/102/103 etc.) — and which are actually live.
//
// Phase 1 will add map spin-up/down/scale here (drives mock-k8s ServerSetScale
// replicas); Phase 2 adds per-dimension control; Phase 3 adds Survival_1 shards.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchPartitions,
  fetchStatus,
  type Partition,
  type StatusGrid,
  type StatusMapRow,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function statusPill(status?: string): string {
  switch (status) {
    case "healthy": return "pill-ok";
    case "starting": return "pill-warn";
    case "failing": return "pill-err";
    default: return "pill-warn"; // idle / unknown
  }
}

export function InstancesTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [parts, setParts] = useState<Partition[]>([]);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(false);

  async function load() {
    setLoading(true);
    const [s, p] = await Promise.all([fetchStatus().catch(() => null), fetchPartitions().catch(() => null)]);
    setLoading(false);
    if (s && s.ok) setGrid(s.body as StatusGrid);
    if (p && p.ok && typeof p.body === "object" && p.body) {
      const body = p.body as { ok: boolean; partitions?: Partition[]; error?: string };
      if (body.ok) setParts(body.partitions || []);
      else pushToConsole(setConsoleEntries, "GET /api/partitions", body.error || "failed", false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => void load(), 15000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto]);

  // Union of maps seen in either source, so nothing is hidden.
  const statusByMap = new Map<string, StatusMapRow>();
  for (const m of grid?.maps || []) statusByMap.set(m.map, m);
  const partsByMap = new Map<string, Partition[]>();
  for (const p of parts) {
    const arr = partsByMap.get(p.map) || [];
    arr.push(p);
    partsByMap.set(p.map, arr);
  }
  const maps = Array.from(new Set([...statusByMap.keys(), ...partsByMap.keys()])).sort();

  function partRow(p: Partition) {
    const live = !!p.server_id && p.ready;
    return (
      <div key={p.partition_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1 border-b border-slate-800 last:border-0 text-xs">
        <span className={"w-2 h-2 rounded-full shrink-0 " + (live ? "bg-emerald-400" : p.server_id ? "bg-amber-400" : "bg-slate-600")} title={live ? "live" : p.server_id ? "registering" : "declared (no instance)"} />
        <span className="font-mono text-slate-400 w-14">#{p.partition_id}</span>
        <span className={"px-1.5 rounded text-[10px] " + (p.dimension === 0 ? "bg-sky-900/50 text-sky-300" : "bg-orange-900/50 text-orange-300")}>
          {p.dimension === 0 ? "warm" : `dim ${p.dimension}`}
        </span>
        {p.label && <span className="text-slate-400">{p.label}</span>}
        {p.game_port != null && <span className="font-mono text-slate-500">:{p.game_port}</span>}
        {p.players > 0 && <span className="text-spice-300">{p.players}p</span>}
        {p.blocked && <span className="text-red-400 text-[10px]">blocked</span>}
        {p.alive && !p.ready && <span className="text-amber-400 text-[10px]">not ready</span>}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold">Instances &amp; partitions</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              {grid ? `${grid.totalServers} servers · ${grid.totalPlayers} online · uptime ${Math.round((grid.uptimeSeconds || 0) / 60)}m` : "…"}
              {grid?.warning ? ` · ${grid.warning}` : ""}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <label className="text-xs text-slate-400 flex items-center gap-1.5">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto 15s
            </label>
            <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
              {loading ? "…" : "refresh"}
            </button>
          </div>
        </header>
        <div className="p-4 text-xs text-slate-500">
          Read-only topology. <span className="text-sky-300">warm</span> = always-on landing zone (dimension 0);{" "}
          <span className="text-orange-300">dim N</span> = per-player sandstorm-tunnel partitions. Dot:{" "}
          <span className="text-emerald-400">live</span> / <span className="text-amber-400">registering</span> /{" "}
          <span className="text-slate-500">declared</span>. Spin-up / shutdown controls land in the next phase.
        </div>
      </div>

      {maps.map((map) => {
        const st = statusByMap.get(map);
        const mps = (partsByMap.get(map) || []).sort((a, b) => a.dimension - b.dimension || a.partition_id - b.partition_id);
        const dims = mps.filter((p) => p.dimension > 0);
        return (
          <div key={map} className="card">
            <header className="card-header flex-wrap gap-2">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="font-semibold text-spice-300">{map}</h3>
                {st && <span className={statusPill(st.status)}>{st.status || "unknown"}</span>}
                {st && (st.desired != null || st.current != null) && (
                  <span className="text-xs text-slate-400 font-mono">{st.current ?? 0}/{st.desired ?? 0} replicas</span>
                )}
                {dims.length > 0 && <span className="text-xs text-orange-300">{dims.length} dim{dims.length > 1 ? "s" : ""}</span>}
              </div>
              <span className="text-xs text-slate-400">{st?.players ?? 0} online</span>
            </header>
            <div className="px-4 py-2">
              {mps.length === 0
                ? <div className="text-xs text-slate-500 italic py-1">no declared partitions</div>
                : mps.map(partRow)}
            </div>
          </div>
        );
      })}
    </div>
  );
}
