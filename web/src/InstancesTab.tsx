// Instances tab (Phase 0 — read-only topology view).
//
// Combines /api/status (mock-k8s per-map desired/current scale + live player
// counts) with /api/partitions (the world_partition topology joined to
// farm_state liveness) so an operator can see every map instance and every
// partition — warm (dimension 0) vs dimensional sandstorm-tunnel partitions
// (DeepDesert 101/102/103 etc.) — and which are actually live.
//
// Phase 1 (this file): map spin-up/down/scale via mock-k8s ServerSetScale
// replicas, with a player-online confirm guard. Phase 2 adds per-dimension
// control; Phase 3 adds Survival_1 shards.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import {
  dimensionDown,
  dimensionUp,
  fetchPartitions,
  fetchStatus,
  scaleInstance,
  type DimResult,
  type Partition,
  type ScaleResult,
  type StatusGrid,
  type StatusMapRow,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

// Mirror of admin-http SCALABLE_MAPS — the on-demand maps only (never the
// always-warm Survival_1 / Overmap). The backend re-validates.
const SCALABLE = new Set(["DeepDesert_1", "SH_Arrakeen", "SH_HarkoVillage"]);
const SCALE_MAX = 4;

function statusPill(status?: string): string {
  switch (status) {
    case "healthy": return "pill-ok";
    case "starting": return "pill-warn";
    case "failing": return "pill-err";
    default: return "pill-warn"; // idle / unknown
  }
}

// Per-map replicas input + Apply (its own state so each card is independent).
function ScaleControl({ current, busy, onApply }: { current: number; busy: boolean; onApply: (n: number) => void }) {
  const [n, setN] = useState(current);
  useEffect(() => { setN(current); }, [current]);
  return (
    <span className="flex items-center gap-1">
      <input
        type="number" min={0} max={SCALE_MAX} value={n}
        onChange={(e) => setN(Math.max(0, Math.min(SCALE_MAX, parseInt(e.target.value || "0", 10))))}
        className="input-field w-14 text-xs font-mono"
      />
      <button className="btn-ghost text-xs border border-slate-700" disabled={busy || n === current} onClick={() => onApply(n)}>
        apply
      </button>
    </span>
  );
}

export function InstancesTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [parts, setParts] = useState<Partition[]>([]);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(false);
  const [busy, setBusy] = useState("");
  // Pending force-confirm after the player-online guard returns requiresConfirmation.
  const [confirm, setConfirm] = useState<null | { map: string; replicas: number; players: number }>(null);
  const [dimBusy, setDimBusy] = useState(0);
  const [dimConfirm, setDimConfirm] = useState<null | { partition: number; players: number }>(null);

  async function dimAct(partition: number, action: "up" | "down", force = false) {
    setDimBusy(partition);
    const res = await (action === "up" ? dimensionUp(partition) : dimensionDown(partition, force)).catch(() => null);
    setDimBusy(0);
    if (!res) {
      pushToConsole(setConsoleEntries, `dimension ${action} ${partition}`, "request failed", false);
      return;
    }
    const b = res.body as DimResult;
    if (res.ok && b.requiresConfirmation) {
      setDimConfirm({ partition, players: b.players ?? 0 });
      return;
    }
    const ok = res.ok && b.ok === true;
    pushToConsole(setConsoleEntries, `dimension ${action} ${partition}`,
      ok ? (action === "up" ? "spawning… (poll for live)" : "offline") : (b.error || "failed"), ok);
    if (ok) void load();
  }

  async function scale(map: string, replicas: number, force = false) {
    setBusy(map);
    const res = await scaleInstance(map, replicas, force).catch(() => null);
    setBusy("");
    if (!res) {
      pushToConsole(setConsoleEntries, `scale ${map}`, "request failed", false);
      return;
    }
    const b = res.body as ScaleResult;
    if (res.ok && b.requiresConfirmation) {
      setConfirm({ map, replicas, players: b.players ?? 0 });
      return;
    }
    const ok = res.ok && b.ok === true;
    pushToConsole(setConsoleEntries, `scale ${map} → ${replicas}`,
      ok ? `replicas ${b.previous}→${b.replicas}` : (b.error || "failed"), ok);
    if (ok) void load();
  }

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
        {p.dimension > 0 && (
          <span className="ml-auto flex items-center gap-1">
            {p.server_id
              ? <button className="btn-ghost text-[10px] border border-red-900/60 text-red-300 px-1.5 py-0"
                  disabled={dimBusy === p.partition_id} onClick={() => void dimAct(p.partition_id, "down")}>↓ offline</button>
              : <button className="btn-ghost text-[10px] border border-slate-700 px-1.5 py-0"
                  disabled={dimBusy === p.partition_id} onClick={() => void dimAct(p.partition_id, "up")}>↑ start</button>}
            {dimBusy === p.partition_id && <span className="text-slate-500">…</span>}
          </span>
        )}
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
          <span className="text-slate-500">declared</span>. On-demand maps (Deep Desert, Arrakeen, Harko) have spin-up / shutdown / scale controls; scaling down with players online asks to confirm.
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
            {SCALABLE.has(map) && (
              <div className="px-4 pb-3 pt-2 flex flex-wrap items-center gap-2 border-t border-slate-800">
                <span className="text-[10px] uppercase tracking-wide text-slate-500 mr-1">scale</span>
                <button className="btn-ghost text-xs border border-slate-700"
                  disabled={busy === map || (st?.desired ?? 0) >= 1}
                  onClick={() => void scale(map, 1)}>▶ start</button>
                <button className="btn-ghost text-xs border border-red-900/60 text-red-300"
                  disabled={busy === map || (st?.desired ?? 0) === 0}
                  onClick={() => void scale(map, 0)}>■ stop</button>
                <ScaleControl current={st?.desired ?? 0} busy={busy === map} onApply={(n) => void scale(map, n)} />
                {busy === map && <span className="text-xs text-slate-500">working…</span>}
              </div>
            )}
          </div>
        );
      })}

      <Confirm
        open={confirm !== null}
        title={`${confirm?.players ?? 0} player(s) online on ${confirm?.map ?? ""}`}
        message={`Scaling ${confirm?.map ?? "this map"} to ${confirm?.replicas ?? 0} replica(s) will disconnect ${confirm?.players ?? 0} connected player(s). Proceed?`}
        confirmLabel="Scale anyway"
        onConfirm={() => {
          const c = confirm;
          setConfirm(null);
          if (c) void scale(c.map, c.replicas, true);
        }}
        onCancel={() => setConfirm(null)}
      />

      <Confirm
        open={dimConfirm !== null}
        title={`${dimConfirm?.players ?? 0} player(s) on partition ${dimConfirm?.partition ?? ""}`}
        message={`Taking dimension partition ${dimConfirm?.partition ?? ""} offline will disconnect ${dimConfirm?.players ?? 0} player(s) in that tunnel. Proceed?`}
        confirmLabel="Take offline anyway"
        onConfirm={() => {
          const c = dimConfirm;
          setDimConfirm(null);
          if (c) void dimAct(c.partition, "down", true);
        }}
        onCancel={() => setDimConfirm(null)}
      />
    </div>
  );
}
