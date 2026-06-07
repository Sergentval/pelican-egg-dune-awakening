// Instances tab — operator topology + lifecycle control.
//
// Combines /api/status (mock-k8s per-map desired/current scale + live player
// counts) with /api/partitions (the world_partition topology joined to
// farm_state liveness). Maps are grouped by role: the operational maps
// (Overland hub, Hagga Basin sietches, the on-demand cities) get prominent
// cards; the ~20 rarely-touched story / dungeon / ecolab / overland maps fold
// into one collapsible section so the page stays readable.
//
// Controls: on-demand maps spin up/down/scale via mock-k8s ServerSetScale;
// sietches (Survival_1 dimensions) add / park / unpark / configure / remove;
// DeepDesert tunnels start / stop. Every state-changing action kicks a short
// "settle burst" of fast polling so the operator watches cold→starting→live.
//
// Friendly names, role chips, and the cold/starting/live/parked badge all come
// from ./mapNames — the single source of truth shared with the other tabs.

import { type Dispatch, type SetStateAction, useEffect, useRef, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import { SietchConfigEditor } from "./SietchConfigEditor";
import {
  MAP_ROLE_META,
  dimensionBadge,
  instanceState,
  mapDisplayName,
  mapRole,
  mapStatusPill,
} from "./mapNames";
import {
  addSietch,
  dimensionDown,
  dimensionUp,
  fetchPartitions,
  fetchStatus,
  parkSietch,
  removeSietch,
  repairBrowser,
  unparkSietch,
  scaleInstance,
  type DimResult,
  type Partition,
  type PublishResult,
  type ScaleResult,
  type StatusGrid,
  type StatusMapRow,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

// Mirror of admin-http SCALABLE_MAPS — the on-demand maps only (never the
// always-warm Survival_1 / Overmap). The backend re-validates.
const SCALABLE = new Set(["DeepDesert_1", "SH_Arrakeen", "SH_HarkoVillage"]);
const SCALE_MAX = 4;

// Settle-burst tuning: after an action, poll this often until nothing is
// transitional (or the window elapses) so state transitions are visible.
const BURST_INTERVAL_MS = 3000;
const BURST_WINDOW_MS = 90000;

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

// Small friendly-name + role-chip heading reused by every map card.
function MapTitle({ map }: { map: string }) {
  const meta = MAP_ROLE_META[mapRole(map)];
  return (
    <span className="flex items-center gap-2 flex-wrap">
      <span aria-hidden>{meta.icon}</span>
      <h3 className="font-semibold text-spice-300">{mapDisplayName(map)}</h3>
      <span className={"px-1.5 rounded text-[10px] " + meta.chip} title={meta.blurb}>{meta.title}</span>
      <span className="font-mono text-[10px] text-slate-600" title="raw engine map id">{map}</span>
    </span>
  );
}

export function InstancesTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [parts, setParts] = useState<Partition[]>([]);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(false);
  const [busy, setBusy] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [showSecondary, setShowSecondary] = useState(false);
  // Pending force-confirm after the player-online guard returns requiresConfirmation.
  const [confirm, setConfirm] = useState<null | { map: string; replicas: number; players: number }>(null);
  const [dimBusy, setDimBusy] = useState(0);
  const [dimConfirm, setDimConfirm] = useState<null | { partition: number; players: number }>(null);
  const [addingSietch, setAddingSietch] = useState(false);
  const [sietchConfirm, setSietchConfirm] = useState<null | { partition: number; players: number }>(null);
  const [parkConfirm, setParkConfirm] = useState<null | { partition: number; players: number }>(null);
  const [editSietch, setEditSietch] = useState<null | { pid: number; label: string; players: number }>(null);
  const [repairing, setRepairing] = useState(false);

  const burst = useRef<{ timer: ReturnType<typeof setInterval> | null; until: number }>({ timer: null, until: 0 });

  function stopBurst() {
    if (burst.current.timer) clearInterval(burst.current.timer);
    burst.current.timer = null;
    setSyncing(false);
  }

  // Fast-poll until nothing is mid-transition (e.g. a cold map booting to live)
  // or the window elapses — so the operator sees the state actually change.
  function startBurst() {
    burst.current.until = Date.now() + BURST_WINDOW_MS;
    setSyncing(true);
    if (burst.current.timer) return; // already bursting — window just extended
    burst.current.timer = setInterval(async () => {
      const fetched = await load();
      if (Date.now() > burst.current.until) { stopBurst(); return; }
      if (fetched === null) return; // transient fetch failure — keep watching, don't stop early
      if (!fetched.some((p) => instanceState(p).transitional)) stopBurst();
    }, BURST_INTERVAL_MS);
  }

  // Refresh now, then watch the transition settle.
  function refreshAndWatch() {
    void load();
    startBurst();
  }

  async function doRepair() {
    setRepairing(true);
    const res = await repairBrowser().catch(() => null);
    setRepairing(false);
    const ok = !!res && res.ok && (res.body as PublishResult)?.ok !== false;
    pushToConsole(setConsoleEntries, "repair browser", res ? (res.body as PublishResult) : "request failed", ok);
    if (ok) refreshAndWatch();
  }

  async function doAddSietch() {
    setAddingSietch(true);
    const res = await addSietch().catch(() => null);
    setAddingSietch(false);
    const ok = !!res && res.ok && (res.body as DimResult)?.ok === true;
    pushToConsole(setConsoleEntries, "add sietch",
      ok ? "new Hagga Basin sietch added (spawning…)" : ((res?.body as DimResult)?.error || "failed"), ok);
    if (ok) refreshAndWatch();
  }

  async function doRemoveSietch(partition: number, force = false) {
    setDimBusy(partition);
    const res = await removeSietch(partition, force).catch(() => null);
    setDimBusy(0);
    if (!res) {
      pushToConsole(setConsoleEntries, `remove sietch ${partition}`, "request failed", false);
      return;
    }
    const b = res.body as DimResult;
    if (res.ok && b.requiresConfirmation) {
      setSietchConfirm({ partition, players: b.players ?? 0 });
      return;
    }
    const ok = res.ok && b.ok === true;
    pushToConsole(setConsoleEntries, `remove sietch ${partition}`, ok ? "removed" : (b.error || "failed"), ok);
    if (ok) refreshAndWatch();
  }

  async function doPark(partition: number, force = false) {
    setDimBusy(partition);
    const res = await parkSietch(partition, force).catch(() => null);
    setDimBusy(0);
    if (!res) {
      pushToConsole(setConsoleEntries, `park sietch ${partition}`, "request failed", false);
      return;
    }
    const b = res.body as DimResult;
    if (res.ok && b.requiresConfirmation) {
      setParkConfirm({ partition, players: b.players ?? 0 });
      return;
    }
    const ok = res.ok && b.ok === true;
    pushToConsole(setConsoleEntries, `park sietch ${partition}`, ok ? "parked (data kept)" : (b.error || "failed"), ok);
    if (ok) refreshAndWatch();
  }

  async function doUnpark(partition: number) {
    setDimBusy(partition);
    const res = await unparkSietch(partition).catch(() => null);
    setDimBusy(0);
    const b = res?.body as DimResult | undefined;
    const ok = !!res && res.ok && b?.ok === true;
    pushToConsole(setConsoleEntries, `unpark sietch ${partition}`,
      ok ? "unparking (respawning…)" : (b?.error || "request failed"), ok);
    if (ok) refreshAndWatch();
  }

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
    if (ok) refreshAndWatch();
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
    pushToConsole(setConsoleEntries, `scale ${mapDisplayName(map)} → ${replicas}`,
      ok ? `replicas ${b.previous}→${b.replicas}` : (b.error || "failed"), ok);
    if (ok) refreshAndWatch();
  }

  // Returns the fetched partitions, or null on a failed fetch so the settle-burst
  // can tell "nothing transitional" apart from "couldn't read" and keep watching.
  async function load(): Promise<Partition[] | null> {
    setLoading(true);
    const [s, p] = await Promise.all([fetchStatus().catch(() => null), fetchPartitions().catch(() => null)]);
    setLoading(false);
    if (s && s.ok) setGrid(s.body as StatusGrid);
    let fetched: Partition[] | null = null;
    if (p && p.ok && typeof p.body === "object" && p.body) {
      const body = p.body as { ok: boolean; partitions?: Partition[]; error?: string };
      if (body.ok) {
        fetched = body.partitions || [];
        setParts(fetched);
      } else {
        pushToConsole(setConsoleEntries, "GET /api/partitions", body.error || "failed", false);
      }
    }
    if (fetched !== null) setLastSync(Date.now());
    return fetched;
  }

  useEffect(() => {
    void load();
    return () => stopBurst();
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  const allMaps = Array.from(new Set([...statusByMap.keys(), ...partsByMap.keys()]));
  const byRole = (a: string, b: string) =>
    MAP_ROLE_META[mapRole(a)].order - MAP_ROLE_META[mapRole(b)].order ||
    mapDisplayName(a).localeCompare(mapDisplayName(b));
  const operational = allMaps.filter((m) => !MAP_ROLE_META[mapRole(m)].secondary).sort(byRole);
  const secondary = allMaps.filter((m) => MAP_ROLE_META[mapRole(m)].secondary).sort(byRole);

  function partRow(p: Partition) {
    const sv = instanceState(p);
    const db = dimensionBadge(p.map, p.dimension);
    const isSietch = p.map === "Survival_1" && p.dimension > 0;
    const parked = !!p.parked;
    return (
      <div key={p.partition_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1 border-b border-slate-800 last:border-0 text-xs">
        <span className={"w-2 h-2 rounded-full shrink-0 " + sv.dot} title={sv.tip} />
        <span className="font-mono text-slate-400 w-14">#{p.partition_id}</span>
        <span className={"px-1.5 rounded text-[10px] " + (p.dimension === 0 ? "bg-sky-900/50 text-sky-300" : "bg-orange-900/50 text-orange-300")} title={db.tip}>
          {db.label}
        </span>
        {p.label && <span className="text-slate-300">{p.label}</span>}
        <span className={"px-1.5 rounded text-[10px] " + sv.badge} title={sv.tip}>{sv.label}</span>
        {p.game_port != null && <span className="font-mono text-slate-500" title="game port">:{p.game_port}</span>}
        {p.players > 0 && <span className="text-spice-300">{p.players} online</span>}
        {p.blocked && <span className="text-red-400 text-[10px]">blocked</span>}
        {p.dimension > 0 && (
          <span className="ml-auto flex items-center gap-1">
            {isSietch ? (
              <>
                {parked && (
                  <button className="btn-ghost text-[10px] border border-violet-900/60 text-violet-300 px-1.5 py-0"
                    disabled={dimBusy === p.partition_id} onClick={() => void doUnpark(p.partition_id)}
                    title="unpark: bring this sietch back online with its data">▶ unpark</button>
                )}
                {!parked && !p.server_id && (
                  <button className="btn-ghost text-[10px] border border-slate-700 px-1.5 py-0"
                    disabled={dimBusy === p.partition_id} onClick={() => void dimAct(p.partition_id, "up")}
                    title="start this cold sietch (bring it online, not parked)">↑ start</button>
                )}
                {!parked && (
                  <button className="btn-ghost text-[10px] border border-violet-900/60 text-violet-300 px-1.5 py-0"
                    disabled={dimBusy === p.partition_id} onClick={() => void doPark(p.partition_id)}
                    title="park: pause this sietch but KEEP all its data (survives reboot) — works whether it's live or cold">⏸ park</button>
                )}
                {!parked && (
                  <button className="btn-ghost text-[10px] border border-slate-700 px-1.5 py-0"
                    onClick={() => setEditSietch({ pid: p.partition_id, label: p.label, players: p.players })} title="configure this sietch (name, PvP, …)">⚙ configure</button>
                )}
                <button className="btn-ghost text-[10px] border border-red-900/60 text-red-400 px-1.5 py-0"
                  disabled={dimBusy === p.partition_id} onClick={() => void doRemoveSietch(p.partition_id)} title="remove this sietch (DELETES its data)">✕ remove</button>
              </>
            ) : (
              p.server_id
                ? <button className="btn-ghost text-[10px] border border-red-900/60 text-red-300 px-1.5 py-0"
                    disabled={dimBusy === p.partition_id} onClick={() => void dimAct(p.partition_id, "down")}>↓ offline</button>
                : <button className="btn-ghost text-[10px] border border-slate-700 px-1.5 py-0"
                    disabled={dimBusy === p.partition_id} onClick={() => void dimAct(p.partition_id, "up")}>↑ start</button>
            )}
            {dimBusy === p.partition_id && <span className="text-slate-500">…</span>}
          </span>
        )}
      </div>
    );
  }

  function mapCard(map: string) {
    const st = statusByMap.get(map);
    const mps = (partsByMap.get(map) || []).sort((a, b) => a.dimension - b.dimension || a.partition_id - b.partition_id);
    const dims = mps.filter((p) => p.dimension > 0);
    return (
      <div key={map} className="card">
        <header className="card-header flex-wrap gap-2">
          <MapTitle map={map} />
          <span className="flex items-center gap-2 flex-wrap">
            {st && <span className={mapStatusPill(st.status)}>{st.status || "unknown"}</span>}
            {st && (st.desired != null || st.current != null) && (
              <span className="text-xs text-slate-400 font-mono" title="current / desired instances">{st.current ?? 0}/{st.desired ?? 0} live</span>
            )}
            {dims.length > 0 && <span className="text-xs text-orange-300">{dims.length} {map === "Survival_1" ? "sietch" : "tunnel"}{dims.length > 1 ? "es" : ""}</span>}
            <span className="text-xs text-slate-400">{st?.players ?? 0} online</span>
          </span>
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
  }

  // Compact one-line summary for the collapsed story/dungeon maps.
  function secondaryRow(map: string) {
    const mps = partsByMap.get(map) || [];
    const st = statusByMap.get(map);
    const primary = mps.find((p) => p.dimension === 0) || mps[0];
    const sv = primary ? instanceState(primary) : instanceState({ server_id: null, ready: false });
    const players = mps.reduce((n, p) => n + (p.players || 0), 0) || st?.players || 0;
    const meta = MAP_ROLE_META[mapRole(map)];
    return (
      <div key={map} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5 px-1 border-b border-slate-800/60 last:border-0 text-xs">
        <span className={"w-2 h-2 rounded-full shrink-0 " + sv.dot} title={sv.tip} />
        <span className="text-slate-300 min-w-[10rem]">{mapDisplayName(map)}</span>
        <span className={"px-1.5 rounded text-[10px] " + meta.chip} title={meta.blurb}>{meta.title}</span>
        <span className={"px-1.5 rounded text-[10px] " + sv.badge} title={sv.tip}>{sv.label}</span>
        {players > 0 && <span className="text-spice-300">{players} online</span>}
        <span className="font-mono text-[10px] text-slate-600 ml-auto" title="raw engine map id">{map}</span>
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
            {syncing
              ? <span className="text-xs text-amber-300 flex items-center gap-1" title="watching the state change settle"><span className="animate-pulse">●</span> syncing…</span>
              : lastSync && <span className="text-xs text-slate-500" title="last refreshed">synced {new Date(lastSync).toLocaleTimeString()}</span>}
            <label className="text-xs text-slate-400 flex items-center gap-1.5">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto 15s
            </label>
            <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
              {loading ? "…" : "refresh"}
            </button>
            <button className="btn-ghost text-xs border border-amber-900/50 text-amber-300"
              onClick={() => void doRepair()} disabled={repairing || loading}
              title="Sweep orphan server registrations + resync the Director — fixes removed sietches/dimensions that linger in the in-game browser">
              {repairing ? "…" : "🔧 Repair browser"}
            </button>
          </div>
        </header>
        <div className="p-4 text-xs text-slate-500">
          Maps are grouped by role. <span className="text-sky-300">Warm</span> partitions are always-on landing zones;{" "}
          <span className="text-orange-300">tunnels / sietches</span> are per-player dimension partitions. State:{" "}
          <span className="text-emerald-400">live</span> · <span className="text-amber-400">starting</span> ·{" "}
          <span className="text-slate-400">cold</span> · <span className="text-violet-300">parked (paused, data kept)</span>.
          On-demand cities (Deep Desert, Arrakeen, Harko Village) have spin-up / shutdown / scale controls; acting with players online asks to confirm. After any action the view auto-syncs until the change settles.
        </div>
        <div className="px-4 pb-3 flex flex-wrap items-center gap-2">
          <button className="btn-primary text-xs" disabled={addingSietch} onClick={() => void doAddSietch()}>
            {addingSietch ? "adding…" : "➕ Add Sietch"}
          </button>
          <span className="text-xs text-slate-500">
            Spawns a new player-choosable Hagga Basin sietch. All sietches share the one Deep Desert / Arrakeen / Harko Village. Per sietch: ⏸ park (pause but keep all data, survives reboot) / ▶ unpark · ⚙ configure (name, PvP, …) · ✕ remove (deletes its data).
          </span>
        </div>
      </div>

      {operational.map(mapCard)}

      {secondary.length > 0 && (
        <div className="card">
          <header className="card-header cursor-pointer select-none" onClick={() => setShowSecondary((v) => !v)}>
            <div className="flex items-center gap-2">
              <span className="text-slate-400">{showSecondary ? "▾" : "▸"}</span>
              <h3 className="font-semibold">Story &amp; Dungeons</h3>
              <span className="text-xs text-slate-500">{secondary.length} maps · {secondary.reduce((n, m) => n + ((partsByMap.get(m) || []).reduce((a, p) => a + (p.players || 0), 0) || statusByMap.get(m)?.players || 0), 0)} online</span>
            </div>
            <span className="text-xs text-slate-500">{showSecondary ? "hide" : "show"}</span>
          </header>
          {showSecondary && (
            <div className="px-4 py-2">
              <p className="text-xs text-slate-500 mb-2">Story-mission, dungeon, ecolab and overland sub-instances. These are seeded on-demand by the game and aren't manually scaled here — listed for visibility.</p>
              {secondary.map(secondaryRow)}
            </div>
          )}
        </div>
      )}

      <Confirm
        open={confirm !== null}
        title={`${confirm?.players ?? 0} player(s) online on ${mapDisplayName(confirm?.map ?? "")}`}
        message={`Scaling ${mapDisplayName(confirm?.map ?? "this map")} to ${confirm?.replicas ?? 0} replica(s) will disconnect ${confirm?.players ?? 0} connected player(s). Proceed?`}
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

      <Confirm
        open={sietchConfirm !== null}
        title={`${sietchConfirm?.players ?? 0} player(s) in sietch ${sietchConfirm?.partition ?? ""}`}
        message={`Removing sietch ${sietchConfirm?.partition ?? ""} destroys it and disconnects ${sietchConfirm?.players ?? 0} player(s) currently in it. Proceed?`}
        confirmLabel="Remove sietch anyway"
        onConfirm={() => {
          const c = sietchConfirm;
          setSietchConfirm(null);
          if (c) void doRemoveSietch(c.partition, true);
        }}
        onCancel={() => setSietchConfirm(null)}
      />

      <Confirm
        open={parkConfirm !== null}
        title={`${parkConfirm?.players ?? 0} player(s) in sietch ${parkConfirm?.partition ?? ""}`}
        message={`Parking sietch ${parkConfirm?.partition ?? ""} disconnects ${parkConfirm?.players ?? 0} player(s) currently in it. Their builds and the sietch are KEPT (this only pauses it — unpark restores everything). Proceed?`}
        confirmLabel="Park sietch anyway"
        onConfirm={() => {
          const c = parkConfirm;
          setParkConfirm(null);
          if (c) void doPark(c.partition, true);
        }}
        onCancel={() => setParkConfirm(null)}
      />

      {editSietch && (
        <SietchConfigEditor
          partitionId={editSietch.pid}
          label={editSietch.label}
          players={editSietch.players}
          setConsoleEntries={setConsoleEntries}
          onClose={() => setEditSietch(null)}
          onApplied={() => refreshAndWatch()}
        />
      )}
    </div>
  );
}
