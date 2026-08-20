// Overview home (design phase 9) — a single de-duplicated landing page:
// server banner + one KPI strip (each stat exactly once) + players roster
// (left) + fleet health (right). Replaces the old StatusTab+Dashboard stack.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import type { ConsoleEntry } from "./components";
import { Avatar, pushToConsole } from "./components";
import { useTarget } from "./target";
import { useAutoRefresh } from "./live";
import { Icon } from "./icons";
import { MAP_ROLE_META, mapDisplayName, mapRole, mapStatusPill } from "./mapNames";
import {
  fetchDoctor,
  fetchPlayers,
  fetchStatus,
  parsePlayerTable,
  type DoctorCheck,
  type PlayerRow,
  type PublishResult,
  type StatusGrid,
} from "./api";

const DOCTOR_PILL: Record<DoctorCheck["status"], string> = {
  ok: "bg-emerald-900/40 text-emerald-300",
  warn: "bg-amber-900/40 text-amber-300",
  error: "bg-red-900/40 text-red-300",
  skip: "bg-slate-800 text-slate-400",
};

// On-demand connectivity diagnosis (it curls the public-IP service, so it is
// never polled). Catches the "boots fine, nobody can join" family: advertised
// IP drift, port collisions, stuck READY, silent FLS heartbeat.
function DoctorCard() {
  const [checks, setChecks] = useState<DoctorCheck[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function runDoctor() {
    setBusy(true);
    setErr("");
    const res = await fetchDoctor().catch(() => null);
    setBusy(false);
    const b: unknown = res?.body;
    const body = typeof b === "object" && b !== null ? (b as {
      ok?: boolean; checks?: DoctorCheck[]; error?: string; stderr?: string;
    }) : null;
    // The endpoint always answers HTTP 200 — the SEMANTIC ok and a non-empty
    // check list are what distinguish a diagnosis from a backend failure. An
    // empty list must never render as "all clear" on a diagnostic tool.
    if (res?.ok && body?.ok && (body.checks?.length ?? 0) > 0) {
      setChecks(body.checks ?? []);
    } else {
      setChecks(null);
      setErr(body?.error || body?.stderr || "diagnosis failed — see the console/logs");
    }
  }

  const bad = (checks ?? []).filter((c) => c.status === "warn" || c.status === "error").length;
  return (
    <div className="card">
      <header className="card-header">
        <div>
          <h2 className="card-title">🩺 Connection doctor</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Can players actually join? Advertised IP, ports, readiness, FLS heartbeat.
          </p>
        </div>
        <button className="btn-ghost text-xs" onClick={() => void runDoctor()} disabled={busy}>
          {busy ? "diagnosing…" : checks ? "re-run" : "run diagnosis"}
        </button>
      </header>
      <div className="card-body space-y-1.5">
        {err && <p className="text-xs text-red-300">{err}</p>}
        {!checks && !busy && !err && (
          <p className="text-xs text-slate-500 italic">Run on demand — not polled.</p>
        )}
        {checks && bad === 0 && (
          <p className="text-xs text-emerald-300">All clear — nothing between players and this server.</p>
        )}
        {(checks ?? []).map((c) => (
          <div key={c.id} className="py-1 border-b border-slate-800/60 last:border-0 text-xs">
            <div className="flex items-center gap-2">
              <span className={"px-1.5 rounded text-[10px] uppercase " + (DOCTOR_PILL[c.status] ?? DOCTOR_PILL.skip)}>{c.status}</span>
              <span className="text-slate-200">{c.summary}</span>
            </div>
            {c.detail && <p className="text-slate-400 mt-0.5 font-mono break-all">{c.detail}</p>}
            {c.hint && c.status !== "ok" && <p className="text-slate-500 mt-0.5">{c.hint}</p>}
          </div>
        ))}
      </div>
    </div>
  );
}

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function fmtUptime(secs: number): string {
  if (!secs || secs < 0) return "—";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  if (h >= 1) return `${h}h ${m}m`;
  return `${m}m`;
}
function relTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function OverviewTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const target = useTarget();
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    const [s, p] = await Promise.all([fetchStatus().catch(() => null), fetchPlayers("all").catch(() => null)]);
    setLoading(false);
    if (s && s.ok) setGrid(s.body as StatusGrid);
    if (p && p.ok) setPlayers(parsePlayerTable((p.body as PublishResult).stdout || ""));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void load(); }, []);
  useAutoRefresh(() => void load(), 15000);

  const online = players.filter((p) => p.online === "Online").length;
  const maps = grid?.maps ?? [];
  const live = maps.reduce((n, m) => n + (m.current ?? 0), 0);
  const wanted = maps.reduce((n, m) => n + (m.desired ?? 0), 0);

  const stats: { label: string; value: string; sub: string; icon: string; live?: boolean }[] = [
    { label: "Online", value: String(online), sub: `${players.length} tracked`, icon: "players", live: online > 0 },
    { label: "Instances", value: String(grid?.totalServers ?? "—"), sub: wanted ? `${live} live / ${wanted} wanted` : "running", icon: "layers" },
    { label: "Maps", value: String(maps.length || "—"), sub: "active worlds", icon: "map" },
    { label: "Uptime", value: fmtUptime(grid?.uptimeSeconds ?? 0), sub: "since boot", icon: "power" },
  ];

  function pickRow(p: PlayerRow) {
    target.setPlayerId(p.character ? `name:${p.character}` : p.fls_id);
  }
  async function lookup(p: PlayerRow) {
    const who = p.character ? `name:${p.character}` : p.fls_id;
    target.setPlayerId(who);
    const looked = await target.lookupPos(who);
    pushToConsole(setConsoleEntries, `pos ${who}`,
      looked ? `${mapDisplayName(looked.map)} (${Math.round(looked.x)}, ${Math.round(looked.y)})` : "pos lookup failed", !!looked);
  }

  return (
    <div className="space-y-[var(--gap)]">
      {/* server banner */}
      <div className="card card-accent-top">
        <div className="card-body flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="brand-mark" style={{ width: 46, height: 46, borderRadius: 13 }}><Icon name="spice" size={24} /></div>
            <div>
              <div className="flex items-center gap-2.5">
                <span className="t-display text-[22px]">Dune Server</span>
                <span className="pill-ok"><span className="dot dot-online" /> live</span>
              </div>
              <span className="faint text-[13px]">Pelican egg · {loading ? "refreshing…" : `${online} online`}{grid?.warning ? ` · ${grid.warning}` : ""}</span>
            </div>
          </div>
          <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>{loading ? "…" : "refresh"}</button>
        </div>
      </div>

      {/* KPI strip — each stat exactly once */}
      <div className="lay lay-dash-stats">
        {stats.map((s) => (
          <div className="stat" key={s.label}>
            <div className="flex items-center justify-between">
              <span className="stat-label">{s.label}</span>
              <Icon name={s.icon} size={18} style={{ color: "var(--accent)", opacity: 0.8 }} />
            </div>
            <div className="flex items-center gap-2">
              <span className="stat-num">{s.value}</span>
              {s.live && <span className="dot dot-online" />}
            </div>
            <span className="faint text-xs">{s.sub}</span>
          </div>
        ))}
      </div>

      {/* roster (left) + fleet health (right) */}
      <div className="lay lay-dash-main">
        <div className="card">
          <header className="card-header">
            <h2 className="card-title">Online players</h2>
            <span className="text-xs text-slate-500">{online} online · {players.length} tracked</span>
          </header>
          <div className="player-list max-h-[460px] overflow-y-auto m-2">
            {players.length === 0 && <div className="px-2 py-6 text-center text-xs text-slate-500">{loading ? "loading…" : "no players seen yet"}</div>}
            {players.map((p) => {
              const name = p.character || `${p.fls_id.slice(0, 8)}…`;
              const isOnline = p.online === "Online";
              const who = p.character ? `name:${p.character}` : p.fls_id;
              const active = target.playerId === who;
              return (
                <div key={p.fls_id} className={"player-row" + (active ? " is-active" : "")} onClick={() => pickRow(p)} role="button" tabIndex={0}>
                  <Avatar name={name} />
                  <span className="flex-1 min-w-0">
                    <span className="block truncate text-sm">{p.character ? name : <span className="font-mono">{name}</span>}</span>
                    <span className="block truncate text-[10px] text-slate-500 font-mono">{p.steam_id || p.fls_id}</span>
                  </span>
                  {p.last_avatar_activity && <span className="text-[10px] text-slate-500" title={new Date(p.last_avatar_activity).toLocaleString()}>{relTime(p.last_avatar_activity)}</span>}
                  <span className={isOnline ? "pill-ok" : "pill-mute"}>{isOnline ? "online" : "offline"}</span>
                  <button className="btn-ghost text-xs" onClick={(e) => { e.stopPropagation(); void lookup(p); }} title="set target + look up position">pos</button>
                </div>
              );
            })}
          </div>
        </div>

        <div className="card">
          <header className="card-header">
            <h2 className="card-title">Fleet health</h2>
            <span className="text-xs text-slate-500">{maps.length} maps</span>
          </header>
          <div className="card-body space-y-1.5">
            {grid?.sources && grid.sources.mockK8s === false && <p className="text-xs text-amber-300">mock-k8s unreachable — player counts only.</p>}
            {maps.length === 0 && <p className="text-xs text-slate-500 italic">{loading ? "loading…" : "no map data"}</p>}
            {[...maps]
              .sort((a, b) => MAP_ROLE_META[mapRole(a.map)].order - MAP_ROLE_META[mapRole(b.map)].order || mapDisplayName(a.map).localeCompare(mapDisplayName(b.map)))
              .map((m) => {
                const meta = MAP_ROLE_META[mapRole(m.map)];
                return (
                  <div key={m.map} className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1 border-b border-slate-800/60 last:border-0 text-xs">
                    <span aria-hidden>{meta.icon}</span>
                    <span className="text-slate-200">{mapDisplayName(m.map)}</span>
                    <span className={"px-1.5 rounded text-[10px] " + meta.chip}>{meta.title}</span>
                    <span className={mapStatusPill(m.status) + " ml-auto"}>{m.status || "?"}</span>
                    {(m.desired != null || m.current != null) && <span className="text-slate-400 font-mono">{m.current ?? 0}/{m.desired ?? 0}</span>}
                    {m.players > 0 && <span className="text-spice-300">{m.players}p</span>}
                  </div>
                );
              })}
          </div>
        </div>

        <DoctorCard />
      </div>
    </div>
  );
}
