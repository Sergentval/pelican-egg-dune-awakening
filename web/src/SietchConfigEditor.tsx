// Per-sietch config editor (heterogeneous sietches: name + PvP/PvE, harvest, ...).
// A sietch = a Survival_1 dimension partition; each is its own UE5 process, so it
// can have its own name + any of the ~190 server-authoritative gameplay settings,
// merged into a per-instance UserEngine/UserGame.ini at spawn. Applying writes the
// overrides + restarts THAT sietch (settings are read at UE5 startup), so after
// Apply we watch the partition cycle cold→starting→live before closing.

import { type Dispatch, type SetStateAction, useEffect, useMemo, useRef, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import { instanceState, type InstanceStateView } from "./mapNames";
import {
  fetchPartitions,
  fetchSietchConfig,
  setSietchConfig,
  type Partition,
  type SietchCapableSetting,
  type SietchConfigResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function boolish(type: string): "bool" | "cvarbool" | null {
  if (type === "bool") return "bool";
  if (type === "cvarbool") return "cvarbool";
  return null;
}

// Consolidate the ~30 raw schema categories into a handful of friendly groups.
const CATEGORY_GROUP: Record<string, string> = {
  "PvP": "Combat & PvP",
  "PvP / Combat": "Combat & PvP",
  "PvP / Safety": "Combat & PvP",
  "Combat": "Combat & PvP",
  "Harvest": "Harvesting & Economy",
  "Resource Yield": "Harvesting & Economy",
  "Economy": "Harvesting & Economy",
  "Spice": "Harvesting & Economy",
  "Crafting": "Harvesting & Economy",
  "Loot": "Loot",
  "Survival": "Survival & Difficulty",
  "Hazards": "Survival & Difficulty",
  "Respawn": "Survival & Difficulty",
  "Progression": "Survival & Difficulty",
  "World": "World & Building",
  "World Identity": "World & Building",
  "Building": "World & Building",
  "Decay & Building": "World & Building",
  "Shelter": "World & Building",
  "Inventory": "World & Building",
  "Player": "World & Building",
  "Sandworm": "Sandworm & Storms",
  "Sandworms": "Sandworm & Storms",
  "Storms": "Sandworm & Storms",
  "Storms & World": "Sandworm & Storms",
  "Vehicles": "Vehicles",
  "Faction": "Faction & Server",
  "Server": "Faction & Server",
  "Server Pool": "Faction & Server",
};
const GROUP_ORDER = [
  "Combat & PvP", "Harvesting & Economy", "Loot", "Survival & Difficulty",
  "Sandworm & Storms", "World & Building", "Vehicles", "Faction & Server", "Other",
];
function groupFor(category?: string): string {
  if (!category) return "Other";
  return CATEGORY_GROUP[category] ?? category;
}

// Presets are keyed on real setting ids. They only flip settings that actually
// exist in this server's sietch-capable schema (robust to schema drift), and the
// value is coerced to each setting's own type at apply time.
type PresetVal = boolean | string;
const PRESETS: Record<string, Record<string, PresetVal>> = {
  PvP: {
    force_pvp_everywhere: true,
    security_zones_enabled: false,
    disable_pvp_damage: false,
    security_force_enable_pvp: true,
  },
  PvE: {
    force_pvp_everywhere: false,
    security_zones_enabled: true,
    disable_pvp_damage: true,
    security_force_enable_pvp: false,
  },
  Hardcore: {
    force_pvp_everywhere: true,
    security_zones_enabled: false,
    near_death_mitigation: false,
    player_death_loot: true,
  },
};

function coerce(s: SietchCapableSetting, want: PresetVal): string {
  const bk = boolish(s.type);
  if (typeof want === "boolean") {
    if (bk === "cvarbool") return want ? "1" : "0";
    return want ? "true" : "false";
  }
  return String(want);
}

export function SietchConfigEditor({
  partitionId, label, players, setConsoleEntries, onClose, onApplied,
}: {
  partitionId: number;
  label: string;
  players: number;
  setConsoleEntries: SetEntries;
  onClose: () => void;
  onApplied: () => void;
}) {
  const [settings, setSettings] = useState<SietchCapableSetting[]>([]);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [name, setName] = useState(label);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmForce, setConfirmForce] = useState(false);
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [presetNote, setPresetNote] = useState("");
  // Post-apply restart watcher.
  const [restartPhase, setRestartPhase] = useState<null | "restarting" | "live" | "timeout">(null);
  const [restartState, setRestartState] = useState<InstanceStateView | null>(null);
  const watching = useRef(false);
  const inited = useRef(false);
  const closed = useRef(false);

  // Idempotent close — guards the double-onClose race between the 1200ms
  // auto-close timer and a manual Done/close click.
  function close() {
    if (closed.current) return;
    closed.current = true;
    watching.current = false;
    onClose();
  }

  useEffect(() => {
    let live = true;
    fetchSietchConfig(partitionId).then((res) => {
      if (!live) return;
      setLoading(false);
      if (res.ok && typeof res.body === "object" && res.body) {
        const b = res.body as { settings?: SietchCapableSetting[]; overrides?: Record<string, string> };
        setSettings(b.settings || []);
        setOverrides({ ...(b.overrides || {}) });
      }
    }).catch(() => setLoading(false));
    return () => { live = false; watching.current = false; };
  }, [partitionId]);

  // Open the groups that already have overrides, once, after first load.
  useEffect(() => {
    if (inited.current || settings.length === 0) return;
    inited.current = true;
    const open = new Set<string>();
    for (const s of settings) if (s.id in overrides) open.add(groupFor(s.category));
    setOpenGroups(open);
  }, [settings, overrides]);

  const q = filter.trim().toLowerCase();
  const matches = (s: SietchCapableSetting): boolean =>
    !q ||
    (s.label || s.id).toLowerCase().includes(q) ||
    s.key.toLowerCase().includes(q) ||
    (s.category || "").toLowerCase().includes(q) ||
    groupFor(s.category).toLowerCase().includes(q);

  // group -> settings (filtered), ordered groups, sorted rows (overridden first).
  const grouped = useMemo(() => {
    const groups = new Map<string, SietchCapableSetting[]>();
    for (const s of settings) {
      if (q && !matches(s)) continue;
      const g = groupFor(s.category);
      const arr = groups.get(g) || [];
      arr.push(s);
      groups.set(g, arr);
    }
    for (const arr of groups.values()) {
      arr.sort((a, b) => {
        const ao = a.id in overrides ? 0 : 1, bo = b.id in overrides ? 0 : 1;
        return ao - bo || (a.label || a.id).localeCompare(b.label || b.id);
      });
    }
    const order = [...groups.keys()].sort((a, b) =>
      ((GROUP_ORDER.indexOf(a) + 1) || 999) - ((GROUP_ORDER.indexOf(b) + 1) || 999) || a.localeCompare(b));
    return { groups, order };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings, overrides, filter]);

  function toggleGroup(g: string) {
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(g)) next.delete(g); else next.add(g);
      return next;
    });
  }

  function setOverride(s: SietchCapableSetting, on: boolean) {
    setOverrides((prev) => {
      const next = { ...prev };
      if (!on) { delete next[s.id]; return next; }
      const bk = boolish(s.type);
      next[s.id] = s.default ?? (bk === "cvarbool" ? "1" : bk === "bool" ? "true" : (s.enum?.[0] ?? ""));
      return next;
    });
  }
  function setValue(id: string, v: string) {
    setOverrides((prev) => ({ ...prev, [id]: v }));
  }

  function applyPreset(presetName: keyof typeof PRESETS) {
    const spec = PRESETS[presetName];
    const affected = new Set<string>();
    let count = 0;
    setOverrides((prev) => {
      const next = { ...prev };
      for (const s of settings) {
        if (!(s.id in spec)) continue;
        next[s.id] = coerce(s, spec[s.id]);
        affected.add(groupFor(s.category));
        count++;
      }
      return next;
    });
    setOpenGroups((prev) => new Set([...prev, ...affected]));
    setPresetNote(count > 0
      ? `${presetName} preset → ${count} setting(s) staged (review & Apply)`
      : `${presetName} preset: none of its settings are available here`);
  }

  function resetOverrides() {
    setOverrides({});
    setPresetNote("cleared all overrides — this sietch will inherit the global settings");
  }

  // After Apply restarts the sietch, poll the partition until it's live again.
  async function watchRestart() {
    setRestartPhase("restarting");
    setRestartState(null);
    watching.current = true;
    const deadline = Date.now() + 90000;
    const poll = async () => {
      if (!watching.current) return;
      const res = await fetchPartitions().catch(() => null);
      if (!watching.current) return; // closed/unmounted while the fetch was in flight
      if (res && res.ok && typeof res.body === "object" && res.body) {
        const list = (res.body as { partitions?: Partition[] }).partitions || [];
        const p = list.find((x) => x.partition_id === partitionId);
        if (p) {
          const sv = instanceState(p);
          setRestartState(sv);
          if (sv.state === "live") {
            setRestartPhase("live");
            watching.current = false;
            setTimeout(() => { close(); }, 1200);
            return;
          }
        }
      }
      if (Date.now() > deadline) { setRestartPhase("timeout"); watching.current = false; return; }
      setTimeout(() => void poll(), 3000);
    };
    void poll();
  }

  async function apply(force = false) {
    setBusy(true);
    const res = await setSietchConfig(partitionId, { name: name.trim() || undefined, overrides, force }).catch(() => null);
    setBusy(false);
    if (!res) { pushToConsole(setConsoleEntries, `sietch ${partitionId} config`, "request failed", false); return; }
    const b = res.body as SietchConfigResult;
    if (res.ok && b.requiresConfirmation) { setConfirmForce(true); return; }
    const ok = res.ok && b.ok === true;
    pushToConsole(setConsoleEntries, `sietch ${partitionId} config`,
      ok ? `applied ${(b.applied || []).length} override(s) + restart` : (b.error || "failed"), ok);
    if (ok) { onApplied(); void watchRestart(); }
  }

  function valueInput(s: SietchCapableSetting) {
    const on = s.id in overrides;
    const val = overrides[s.id] ?? "";
    const bk = boolish(s.type);
    if (!on) return <span className="text-[10px] text-slate-600 italic">inherits global{s.default ? ` (${s.default})` : ""}</span>;
    if (bk) {
      const truthy = val === "true" || val === "1" || val === "True";
      return (
        <label className="flex items-center gap-1 text-[10px] text-slate-300">
          <input type="checkbox" checked={truthy}
            onChange={(e) => setValue(s.id, bk === "cvarbool" ? (e.target.checked ? "1" : "0") : (e.target.checked ? "true" : "false"))} />
          {truthy ? "on" : "off"}
        </label>
      );
    }
    if (s.enum && s.enum.length) {
      return (
        <select className="input-field text-[10px] w-36 py-0" value={val} onChange={(e) => setValue(s.id, e.target.value)}>
          {s.enum.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    }
    const num = s.type === "float" || s.type === "int";
    return (
      <input type={num ? "number" : "text"} value={val} step={s.type === "float" ? "0.1" : "1"}
        onChange={(e) => setValue(s.id, e.target.value)} className="input-field text-[10px] w-36 py-0 font-mono" />
    );
  }

  function settingRow(s: SietchCapableSetting) {
    return (
      <div key={s.id} className="flex items-center gap-2 px-2 py-1 text-xs">
        <input type="checkbox" checked={s.id in overrides} onChange={(e) => setOverride(s, e.target.checked)} title="override for this sietch" />
        <span className="flex-1 min-w-0">
          <span className="text-slate-300">{s.label || s.id}</span>
          {s.verified === false && <span className="text-[10px] text-amber-500 ml-1" title="not yet game-effect-verified">?</span>}
        </span>
        {valueInput(s)}
      </div>
    );
  }

  const overrideCount = Object.keys(overrides).length;

  // ---- Restart-watch view (replaces the editor body once Apply fires) ----
  if (restartPhase) {
    const live = restartPhase === "live";
    const timed = restartPhase === "timeout";
    return (
      <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
        <div className="card max-w-md w-full p-6 text-center space-y-3">
          <h3 className="font-semibold text-spice-300">
            {live ? "Sietch back online ✓" : timed ? "Settings saved" : `Restarting sietch #${partitionId}…`}
          </h3>
          {!live && !timed && <div className="text-3xl animate-pulse">🌀</div>}
          {restartState && (
            <div className="flex items-center justify-center gap-2 text-xs">
              <span className={"w-2 h-2 rounded-full " + restartState.dot} />
              <span className={"px-1.5 rounded text-[10px] " + restartState.badge}>{restartState.label}</span>
            </div>
          )}
          <p className="text-sm text-slate-400">
            {live
              ? `"${name.trim() || label}" applied your settings and is accepting players again.`
              : timed
                ? "Settings written. No live instance came up within the window — they'll take effect the next time this sietch starts."
                : "Your settings were written; the sietch is rebooting to apply them (~40s for a cold UE5)."}
          </p>
          <button className="btn-primary w-full" onClick={close}>
            {live ? "Done" : "Close (keeps applying in the background)"}
          </button>
        </div>
      </div>
    );
  }

  // ---- Editor view -------------------------------------------------------
  return (
    <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="card max-w-2xl w-full max-h-[85vh] flex flex-col">
        <header className="card-header">
          <div>
            <h3 className="font-semibold text-spice-300">Configure sietch #{partitionId}</h3>
            <p className="text-xs text-slate-500 mt-0.5">
              {overrideCount} override{overrideCount === 1 ? "" : "s"}{players > 0 ? ` · ${players} player(s) online` : ""} · Apply restarts this sietch to take effect
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={close} disabled={busy}>close</button>
        </header>

        <div className="p-4 space-y-3 overflow-y-auto">
          <div>
            <label className="label" htmlFor="sietch-name">Display name (shown in the in-game server browser)</label>
            <input id="sietch-name" value={name} onChange={(e) => setName(e.target.value)}
              className="input-field w-full" placeholder="e.g. PvP Sietch" maxLength={48} />
          </div>

          {/* Quick presets — stage a set of overrides, then Apply. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] uppercase tracking-wide text-slate-500 mr-1">presets</span>
            <button className="btn-ghost text-xs border border-rose-900/60 text-rose-300" onClick={() => applyPreset("PvP")} title="Force PvP everywhere, drop security zones">⚔ PvP</button>
            <button className="btn-ghost text-xs border border-emerald-900/60 text-emerald-300" onClick={() => applyPreset("PvE")} title="Safe sietch: security zones on, PvP damage off">🛡 PvE</button>
            <button className="btn-ghost text-xs border border-amber-900/60 text-amber-300" onClick={() => applyPreset("Hardcore")} title="PvP on, less near-death mitigation, death drops loot">💀 Hardcore</button>
            <button className="btn-ghost text-xs border border-slate-700" onClick={resetOverrides} title="Clear all overrides — inherit the global settings">↺ Reset</button>
          </div>
          {presetNote && <p className="text-[11px] text-spice-300">{presetNote}</p>}

          <input value={filter} onChange={(e) => setFilter(e.target.value)} className="input-field w-full text-xs"
            placeholder={`filter ${settings.length} settings (e.g. pvp, harvest, sandworm)…`} />
          {loading && <div className="text-xs text-slate-500 italic">loading settings…</div>}

          <div className="space-y-2 max-h-[42vh] overflow-y-auto">
            {grouped.order.map((g) => {
              const list = grouped.groups.get(g) || [];
              const overCount = list.filter((s) => s.id in overrides).length;
              const open = q ? true : openGroups.has(g);
              return (
                <div key={g} className="border border-slate-800 rounded">
                  <button type="button" className="w-full flex items-center justify-between px-2 py-1.5 text-xs hover:bg-slate-800/40"
                    onClick={() => { if (!q) toggleGroup(g); }}>
                    <span className="flex items-center gap-2">
                      <span className="text-slate-400">{open ? "▾" : "▸"}</span>
                      <span className="text-slate-200 font-medium">{g}</span>
                      <span className="text-slate-600">{list.length}</span>
                    </span>
                    {overCount > 0 && <span className="px-1.5 rounded text-[10px] bg-spice-900/40 text-spice-300">{overCount} set</span>}
                  </button>
                  {open && <div className="divide-y divide-slate-800/60 border-t border-slate-800">{list.map(settingRow)}</div>}
                </div>
              );
            })}
            {!loading && grouped.order.length === 0 && <div className="px-2 py-2 text-xs text-slate-500 italic">no settings match</div>}
          </div>
        </div>

        <div className="px-4 py-3 border-t border-slate-800 flex items-center gap-2">
          <button className="btn-primary" disabled={busy || loading} onClick={() => void apply(false)}>
            {busy ? "applying…" : "Apply + restart sietch"}
          </button>
          <button className="btn-ghost" onClick={close} disabled={busy}>cancel</button>
          {confirmForce && (
            <span className="flex items-center gap-2 text-xs text-amber-300">
              {players} online — restart anyway?
              <button className="btn-ghost border border-red-900/60 text-red-300 text-xs" disabled={busy} onClick={() => void apply(true)}>yes, restart</button>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
