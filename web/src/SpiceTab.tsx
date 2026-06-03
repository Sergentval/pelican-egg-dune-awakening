// Spice tab: server-wide spicefield economy controls. Shows each spicefield
// type (per map/dimension) with live active/primed-vs-cap counts, a spawn
// on/off toggle, and editable global caps. Backed by GET /api/spice +
// POST /api/spice/<id>/{spawning,caps}. These apply live (no offline gate) —
// they are server-economy levers, not per-player edits.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchSpice,
  setSpiceCaps,
  setSpiceSpawning,
  type PublishResult,
  type SpiceField,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function logResult(
  setEntries: SetEntries,
  label: string,
  res: { ok: boolean; body: unknown } | null,
): boolean {
  if (!res) {
    pushToConsole(setEntries, label, "request failed (network error)", false);
    return false;
  }
  const b: unknown = res.body;
  const isObj = typeof b === "object" && b !== null;
  const ok = res.ok && isObj && "ok" in b && (b as { ok?: unknown }).ok === true;
  const err = isObj && "error" in b ? (b as { error?: string }).error : undefined;
  pushToConsole(setEntries, label, typeof b === "string" ? b : (err ?? (b as PublishResult)), ok);
  return ok;
}

function SpiceRow({ f, busy, onToggle, onCaps }: {
  f: SpiceField;
  busy: boolean;
  onToggle: (id: number, active: boolean) => void;
  onCaps: (id: number, a: number, p: number) => void;
}) {
  const [a, setA] = useState(f.max_active);
  const [p, setP] = useState(f.max_primed);
  const atCap = f.max_active > 0 && f.active >= f.max_active;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 py-2 border-b border-slate-800 last:border-0 text-sm">
      <span className="w-20 font-medium">{f.field_type}</span>
      <span className="text-slate-500 text-xs w-12">dim {f.dimension}</span>
      <span className={"font-mono " + (atCap ? "text-amber-300" : "text-slate-300")} title="active / cap">
        act {f.active}/{f.max_active}
      </span>
      <span className="font-mono text-slate-400" title="primed / cap">prm {f.primed}/{f.max_primed}</span>
      <button
        className={f.spawning ? "pill-ok" : "pill-err"}
        onClick={() => onToggle(f.id, !f.spawning)}
        disabled={busy}
        title="toggle spawning"
      >
        {f.spawning ? "spawning ON" : "OFF"}
      </button>
      <div className="flex items-center gap-1 ml-auto">
        <label className="text-xs text-slate-500">cap a</label>
        <input type="number" min={0} value={a}
          onChange={(e) => setA(Math.max(0, parseInt(e.target.value || "0", 10)))}
          className="input-field w-16 text-xs font-mono" />
        <label className="text-xs text-slate-500">p</label>
        <input type="number" min={0} value={p}
          onChange={(e) => setP(Math.max(0, parseInt(e.target.value || "0", 10)))}
          className="input-field w-16 text-xs font-mono" />
        <button className="btn-ghost text-xs border border-slate-700" disabled={busy}
          onClick={() => onCaps(f.id, a, p)}>
          set caps
        </button>
      </div>
    </div>
  );
}

export function SpiceTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [fields, setFields] = useState<SpiceField[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    const res = await fetchSpice().catch(() => null);
    setLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "fields" in b) {
      setFields((b as { fields: SpiceField[] }).fields);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function onToggle(id: number, active: boolean) {
    setBusy(true);
    const res = await setSpiceSpawning(id, active).catch(() => null);
    setBusy(false);
    if (logResult(setConsoleEntries, `spice ${id} spawning=${active}`, res)) void load();
  }

  async function onCaps(id: number, maxActive: number, maxPrimed: number) {
    setBusy(true);
    const res = await setSpiceCaps(id, maxActive, maxPrimed).catch(() => null);
    setBusy(false);
    if (logResult(setConsoleEntries, `spice ${id} caps a=${maxActive} p=${maxPrimed}`, res)) void load();
  }

  const maps = [...new Set(fields.map((f) => f.map))].sort();

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold">Spicefield economy</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Toggle spice spawning and tune the global active/primed caps per field type. Applies live (server-wide). More spice in circulation = bigger economy.
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
            {loading ? "…" : "refresh"}
          </button>
        </header>
        <div className="p-4 space-y-5">
          {fields.length === 0 && (
            <p className="text-sm text-slate-500">{loading ? "Loading…" : "No spicefield data."}</p>
          )}
          {maps.map((map) => (
            <div key={map}>
              <h3 className="text-sm font-semibold text-spice-300 mb-1">{map}</h3>
              <div>
                {fields
                  .filter((f) => f.map === map)
                  .map((f) => (
                    <SpiceRow key={f.id} f={f} busy={busy} onToggle={onToggle} onCaps={onCaps} />
                  ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
