// Live player map (Phase 2-4). Hand-rolled pan/zoom over a top-down map image
// (lifted from Icehunter/dune-admin, MIT) with player markers projected from
// dune.actors world coords, click-a-player to set the shared target, saved
// teleport locations with one-click TP, and a click-to-pick coordinate picker
// for new locations (e.g. a Hagga safe outpost). No map library dependency.

import {
  type Dispatch,
  type PointerEvent as ReactPointerEvent,
  type SetStateAction,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  addLocation,
  fetchLocations,
  fetchMapMarkers,
  removeLocation,
  teleportToLocation,
  type MapLocation,
  type MapMarker,
  type PublishResult,
} from "./api";
import { pushToConsole, type ConsoleEntry } from "./components";
import { useTarget } from "./target";
import { useAutoRefresh } from "./live";

interface MapCfg {
  key: string;
  label: string;
  image: string;
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
  flipX?: boolean;
  flipY?: boolean;
}

// Bounds + images lifted from dune-admin (MIT), validated against live coords.
const MAPS: MapCfg[] = [
  { key: "HaggaBasin", label: "Hagga Basin", image: "hagga-basin.webp", minX: -437871, maxX: 350539, minY: -462011, maxY: 376267, flipY: true },
  { key: "DeepDesert", label: "Deep Desert", image: "deepdesert.webp", minX: -1300000, maxX: 1200000, minY: -1300000, maxY: 1200000 },
  { key: "Arrakeen", label: "Arrakeen", image: "arrakeen.webp", minX: -32000, maxX: 17000, minY: -10000, maxY: 9500, flipY: true },
  { key: "HarkoVillage", label: "Harko Village", image: "harko.webp", minX: -5000, maxX: 14500, minY: -5500, maxY: 32000 },
];

const BASE_W = 760; // base image render width (px) before zoom
const POLL_MS = 4000;

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

// World coords -> percentage offsets within the map image (left%, top%).
function worldToPct(x: number, y: number, cfg: MapCfg): { left: number; top: number } {
  const normX = (x - cfg.minX) / (cfg.maxX - cfg.minX);
  const normY = (y - cfg.minY) / (cfg.maxY - cfg.minY);
  const fracX = clamp01(cfg.flipX ? 1 - normX : normX);
  const fracYup = clamp01(cfg.flipY ? 1 - normY : normY);
  return { left: fracX * 100, top: (1 - fracYup) * 100 };
}

// On-screen click -> world coords, using the transformed image bounding rect.
function pctToWorld(clientX: number, clientY: number, rect: DOMRect, cfg: MapCfg): { x: number; y: number } {
  const fracX = clamp01((clientX - rect.left) / rect.width);
  const fracYup = 1 - clamp01((clientY - rect.top) / rect.height);
  const rawX = cfg.flipX ? 1 - fracX : fracX;
  const rawY = cfg.flipY ? 1 - fracYup : fracYup;
  return {
    x: Math.round(rawX * (cfg.maxX - cfg.minX) + cfg.minX),
    y: Math.round(rawY * (cfg.maxY - cfg.minY) + cfg.minY),
  };
}

export function MapTab({ setConsoleEntries }: { setConsoleEntries: Dispatch<SetStateAction<ConsoleEntry[]>> }) {
  const target = useTarget();
  const [mapKey, setMapKey] = useState("HaggaBasin");
  const [markers, setMarkers] = useState<MapMarker[]>([]);
  const [locations, setLocations] = useState<MapLocation[]>([]);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [pickMode, setPickMode] = useState(false);
  const [pending, setPending] = useState<{ x: number; y: number } | null>(null);
  const [pendingName, setPendingName] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const viewportRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const drag = useRef({ active: false, x: 0, y: 0, moved: false });

  const cfg = MAPS.find((m) => m.key === mapKey) ?? MAPS[0];

  const loadMarkers = useCallback(async () => {
    const res = await fetchMapMarkers(mapKey);
    if (res.ok) setMarkers((res.body as { markers: MapMarker[] }).markers || []);
  }, [mapKey]);

  const loadLocations = useCallback(async () => {
    const res = await fetchLocations();
    if (res.ok) setLocations((res.body as { locations: MapLocation[] }).locations || []);
  }, []);

  useEffect(() => { loadMarkers(); }, [loadMarkers]);
  useAutoRefresh(loadMarkers, POLL_MS);
  useEffect(() => { loadLocations(); }, [loadLocations]);
  // reset view + picker when changing map
  useEffect(() => { setZoom(1); setPan({ x: 0, y: 0 }); setPending(null); }, [mapKey]);

  // Native non-passive wheel listener so zoom can preventDefault page scroll.
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = vp.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      setZoom((z) => {
        const next = Math.min(8, Math.max(0.3, z * factor));
        const k = next / z;
        setPan((p) => ({ x: cx - k * (cx - p.x), y: cy - k * (cy - p.y) }));
        return next;
      });
    };
    vp.addEventListener("wheel", onWheel, { passive: false });
    return () => vp.removeEventListener("wheel", onWheel);
  }, []);

  function onPointerDown(e: ReactPointerEvent) {
    drag.current = { active: true, x: e.clientX, y: e.clientY, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  }
  function onPointerMove(e: ReactPointerEvent) {
    if (!drag.current.active) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.current.moved = true;
    drag.current.x = e.clientX;
    drag.current.y = e.clientY;
    setPan((p) => ({ x: p.x + dx, y: p.y + dy }));
  }
  function onPointerUp(e: ReactPointerEvent) {
    const wasDrag = drag.current.moved;
    drag.current.active = false;
    if (!wasDrag && pickMode && innerRef.current) {
      const rect = innerRef.current.getBoundingClientRect();
      setPending(pctToWorld(e.clientX, e.clientY, rect, cfg));
      setPendingName("");
    }
  }

  async function savePending() {
    if (!pending || !pendingName.trim()) return;
    const res = await addLocation({ name: pendingName.trim(), map: mapKey, x: pending.x, y: pending.y, z: 0 });
    if (res.ok) {
      setLocations((res.body as { locations: MapLocation[] }).locations || []);
      pushToConsole(setConsoleEntries, `add location ${pendingName.trim()}`, `saved at (${pending.x}, ${pending.y}) on ${cfg.label}`, true);
      setPending(null);
      setPendingName("");
      setPickMode(false);
      setErr(null);
    } else {
      setErr((res.body as { error?: string }).error || "save failed");
    }
  }

  async function tpTo(loc: MapLocation) {
    if (!target.playerId) { setErr("pick a target player first (click a dot, or use the Players tab)"); return; }
    setErr(null);
    const res = await teleportToLocation(target.playerId, loc.name);
    const ok = res.ok && (res.body as PublishResult).ok;
    pushToConsole(setConsoleEntries, `tp ${target.playerId} -> ${loc.name}`, res.body as PublishResult, ok);
  }

  async function delLoc(name: string) {
    const res = await removeLocation(name);
    if (res.ok) setLocations((res.body as { locations: MapLocation[] }).locations || []);
  }

  const mapLocations = locations.filter((l) => l.map === mapKey);

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="font-semibold">Live map</h2>
            <span className="text-xs text-slate-500">{markers.length} players</span>
          </div>
          <div className="flex gap-1 flex-wrap">
            {MAPS.map((m) => (
              <button
                key={m.key}
                onClick={() => setMapKey(m.key)}
                className={"text-xs px-2 py-1 rounded " + (m.key === mapKey ? "bg-spice-900/40 text-spice-200" : "text-slate-400 hover:bg-slate-800")}
              >
                {m.label}
              </button>
            ))}
          </div>
        </header>
        <div className="p-3 flex items-center gap-3 flex-wrap text-xs">
          <button className="btn-ghost" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}>reset view</button>
          <button
            className={"btn-ghost " + (pickMode ? "text-spice-300 border border-spice-700" : "")}
            onClick={() => { setPickMode(!pickMode); setPending(null); }}
          >
            {pickMode ? "picking… click the map" : "+ add location"}
          </button>
          <span className="text-slate-500">target: <span className="font-mono text-spice-300">{target.playerId || "(none)"}</span></span>
          <span className="text-slate-600">scroll = zoom · drag = pan · click a player dot = set target</span>
        </div>
        <p className="px-3 pb-2 text-[11px] text-amber-400/80">
          ⓘ Dots are the <span className="font-medium">last saved position</span> — the game persists player coords on its
          save cadence, so a dot can lag the player's real location by tens of seconds (it jumps when the next save lands).
          Not a live feed.
        </p>
        {err && <p className="px-3 pb-2 text-xs text-red-400">{err}</p>}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_18rem] gap-4">
        <div
          ref={viewportRef}
          className={"relative overflow-hidden rounded border border-slate-800 bg-slate-950 h-[68vh] " + (pickMode ? "cursor-crosshair" : "cursor-grab")}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        >
          <div
            ref={innerRef}
            className="absolute top-0 left-0 origin-top-left"
            style={{ width: BASE_W, transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}
          >
            <img src={`/${cfg.image}`} alt={cfg.label} draggable={false} className="block w-full select-none pointer-events-none" />
            {markers.map((m) => {
              const p = worldToPct(m.x, m.y, cfg);
              return (
                <button
                  key={m.id}
                  title={`${m.name} (${m.online ? "online" : "offline"}) · partition ${m.partition}${m.fls ? "" : " · no linked account (cannot target)"}`}
                  onPointerDown={(e) => e.stopPropagation()}
                  onPointerUp={(e) => e.stopPropagation()}
                  onClick={(e) => { e.stopPropagation(); if (m.fls) target.setPlayerId(m.fls); }}
                  className={"absolute -translate-x-1/2 -translate-y-1/2 " + (m.fls ? "" : "opacity-50 cursor-not-allowed")}
                  style={{ left: `${p.left}%`, top: `${p.top}%` }}
                >
                  <span
                    className={"block rounded-full border border-black/60 " + (m.online ? "bg-emerald-400" : "bg-slate-500")}
                    style={{ width: 10 / zoom, height: 10 / zoom }}
                  />
                </button>
              );
            })}
            {mapLocations.map((l) => {
              const p = worldToPct(l.x, l.y, cfg);
              return (
                <div
                  key={l.name}
                  className="absolute -translate-x-1/2 -translate-y-1/2 pointer-events-none leading-none text-amber-300"
                  style={{ left: `${p.left}%`, top: `${p.top}%`, fontSize: 14 / zoom }}
                  title={l.name}
                >
                  ⌖
                </div>
              );
            })}
            {pending && (
              <div
                className="absolute -translate-x-1/2 -translate-y-1/2 pointer-events-none leading-none text-spice-300"
                style={{ ...(() => { const p = worldToPct(pending.x, pending.y, cfg); return { left: `${p.left}%`, top: `${p.top}%` }; })(), fontSize: 16 / zoom }}
              >
                ✛
              </div>
            )}
          </div>
        </div>

        <div className="space-y-3">
          {pending && (
            <div className="card p-3 space-y-2">
              <div className="text-xs text-slate-400">
                New location at <span className="font-mono text-slate-300">({pending.x}, {pending.y})</span> on {cfg.label}
              </div>
              <input
                className="input-field text-xs w-full"
                placeholder="name (e.g. Hagga safe outpost)"
                value={pendingName}
                onChange={(e) => setPendingName(e.target.value)}
              />
              <div className="flex gap-2">
                <button className="btn-primary text-xs" onClick={savePending} disabled={!pendingName.trim()}>save</button>
                <button className="btn-ghost text-xs" onClick={() => { setPending(null); setPendingName(""); }}>cancel</button>
              </div>
            </div>
          )}

          <div className="card">
            <header className="card-header">
              <h3 className="font-semibold text-sm">Locations</h3>
              <span className="text-xs text-slate-500">{mapLocations.length}</span>
            </header>
            <div className="divide-y divide-slate-900">
              {mapLocations.length === 0 && (
                <p className="p-3 text-xs text-slate-500 italic">
                  None on {cfg.label}. Hit “+ add location”, click the spot on the map (e.g. your Hagga safe outpost), and name it — then TP any targeted player there in one click.
                </p>
              )}
              {mapLocations.map((l) => (
                <div key={l.name} className="px-3 py-2 flex items-center gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm truncate">{l.name}</div>
                    <div className="text-[10px] text-slate-600 font-mono">{Math.round(l.x)}, {Math.round(l.y)}</div>
                  </div>
                  <button className="btn-primary text-xs" onClick={() => tpTo(l)} title="Teleport the target player here">TP</button>
                  <button className="btn-ghost text-xs text-red-300" onClick={() => delLoc(l.name)} title="Remove location">✕</button>
                </div>
              ))}
            </div>
          </div>

          <div className="card p-3">
            <h3 className="font-semibold text-sm mb-2">Players ({markers.length})</h3>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {markers.length === 0 && <p className="text-xs text-slate-500 italic">No players on {cfg.label}.</p>}
              {markers.map((m) => (
                <button
                  key={m.id}
                  onClick={() => { if (m.fls) target.setPlayerId(m.fls); }}
                  disabled={!m.fls}
                  title={m.fls ? "set as target" : "no linked account — cannot target"}
                  className={"w-full text-left text-xs flex items-center gap-2 px-1 py-0.5 rounded " + (m.fls ? "hover:bg-slate-800" : "opacity-50 cursor-not-allowed")}
                >
                  <span className={"w-2 h-2 rounded-full shrink-0 " + (m.online ? "bg-emerald-400" : "bg-slate-500")} />
                  <span className="truncate flex-1">{m.name}</span>
                  <span className="text-slate-600 font-mono">p{m.partition}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
