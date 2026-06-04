// Per-sietch config editor (heterogeneous sietches: name + PvP/PvE, harvest, ...).
// A sietch = a Survival_1 dimension partition; each is its own UE5 process, so it
// can have its own name + any of the ~190 server-authoritative gameplay settings,
// merged into a per-instance UserEngine/UserGame.ini at spawn. Applying writes the
// overrides + restarts THAT sietch (settings are read at UE5 startup).

import { type Dispatch, type SetStateAction, useEffect, useMemo, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchSietchConfig,
  setSietchConfig,
  type SietchCapableSetting,
  type SietchConfigResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function boolish(type: string): "bool" | "cvarbool" | null {
  if (type === "bool") return "bool";
  if (type === "cvarbool") return "cvarbool";
  return null;
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
    return () => { live = false; };
  }, [partitionId]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = q
      ? settings.filter((s) => (s.label || s.id).toLowerCase().includes(q) || s.key.toLowerCase().includes(q) || (s.category || "").toLowerCase().includes(q))
      : settings;
    // overridden first, then by category/label
    return [...list].sort((a, b) => {
      const ao = a.id in overrides ? 0 : 1, bo = b.id in overrides ? 0 : 1;
      if (ao !== bo) return ao - bo;
      return (a.category || "").localeCompare(b.category || "") || (a.label || a.id).localeCompare(b.label || b.id);
    });
  }, [settings, overrides, filter]);

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
    if (ok) { onApplied(); onClose(); }
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

  const overrideCount = Object.keys(overrides).length;

  return (
    <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="card max-w-2xl w-full max-h-[85vh] flex flex-col">
        <header className="card-header">
          <div>
            <h3 className="font-semibold text-spice-300">Configure sietch #{partitionId}</h3>
            <p className="text-xs text-slate-500 mt-0.5">
              {overrideCount} override(s){players > 0 ? ` · ${players} player(s) online` : ""} · applying restarts this sietch
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={onClose} disabled={busy}>close</button>
        </header>

        <div className="p-4 space-y-3 overflow-y-auto">
          <div>
            <label className="label" htmlFor="sietch-name">Display name (server browser)</label>
            <input id="sietch-name" value={name} onChange={(e) => setName(e.target.value)}
              className="input-field w-full" placeholder="e.g. PvP Sietch" maxLength={48} />
          </div>
          <input value={filter} onChange={(e) => setFilter(e.target.value)} className="input-field w-full text-xs"
            placeholder={`filter ${settings.length} settings (e.g. pvp, harvest, sandworm)…`} />
          {loading && <div className="text-xs text-slate-500 italic">loading settings…</div>}
          <div className="border border-slate-800 rounded divide-y divide-slate-800 max-h-[40vh] overflow-y-auto">
            {filtered.slice(0, 200).map((s) => (
              <div key={s.id} className="flex items-center gap-2 px-2 py-1 text-xs">
                <input type="checkbox" checked={s.id in overrides} onChange={(e) => setOverride(s, e.target.checked)} title="override for this sietch" />
                <span className="flex-1 min-w-0">
                  <span className="text-slate-300">{s.label || s.id}</span>
                  {s.category && <span className="text-[10px] text-slate-600 ml-1">{s.category}</span>}
                  {s.verified === false && <span className="text-[10px] text-amber-500 ml-1" title="not yet game-effect-verified">?</span>}
                </span>
                {valueInput(s)}
              </div>
            ))}
            {!loading && filtered.length === 0 && <div className="px-2 py-2 text-xs text-slate-500 italic">no settings match</div>}
          </div>
        </div>

        <div className="px-4 py-3 border-t border-slate-800 flex items-center gap-2">
          <button className="btn-primary" disabled={busy || loading} onClick={() => void apply(false)}>
            {busy ? "applying…" : "Apply + restart sietch"}
          </button>
          <button className="btn-ghost" onClick={onClose} disabled={busy}>cancel</button>
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
