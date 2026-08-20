// Bases tab: every claimed base (owner, map, piece/placeable counts) with a
// per-base water panel and a hard-gated refill. The refill subcommand FAILS
// CLOSED unless the base's map has zero live instances — a running map
// rewrites base state from memory on flush, silently undoing DB writes (the
// memory-flush rule). Ported from Red-Blink's bases feature (MIT).

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import {
  baseFuelRefill,
  baseWaterRefill,
  fetchBaseFuel,
  fetchBases,
  fetchBaseWater,
  type BaseFuelRow,
  type BaseRow,
  type BaseWaterRow,
  type PublishResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;
type ConfirmSpec = { title: string; message: string; confirmLabel: string; onConfirm: () => void };

export function BasesTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [bases, setBases] = useState<BaseRow[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<BaseRow | null>(null);
  const [water, setWater] = useState<BaseWaterRow[]>([]);
  const [waterLoading, setWaterLoading] = useState(false);
  const [fuel, setFuel] = useState<BaseFuelRow[]>([]);
  const [fuelLoading, setFuelLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  async function load() {
    setLoading(true);
    const res = await fetchBases(q).catch(() => null);
    setLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "bases" in b) {
      setBases((b as { bases: BaseRow[] }).bases ?? []);
    } else {
      setBases([]);
      pushToConsole(setConsoleEntries, "GET /api/bases", "failed to list bases", false);
    }
  }

  async function loadWater(base: BaseRow) {
    setSelected(base);
    setWaterLoading(true);
    const res = await fetchBaseWater(base.base_id).catch(() => null);
    setWaterLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "water" in b) {
      setWater((b as { water: BaseWaterRow[] }).water ?? []);
    } else {
      setWater([]);
    }
    void loadFuel(base);
  }

  async function loadFuel(base: BaseRow) {
    setFuelLoading(true);
    const res = await fetchBaseFuel(base.base_id).catch(() => null);
    setFuelLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "fuel" in b) {
      setFuel((b as { fuel: BaseFuelRow[] }).fuel ?? []);
    } else {
      setFuel([]);
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const pct = (stored: string, capacity: string): string => {
    const s = Number(stored);
    const c = Number(capacity);
    if (!c || Number.isNaN(s)) return "—";
    return `${Math.round((s / c) * 100)}%`;
  };

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="card-title">🏠 Bases</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Claimed bases with owner + build size. Select one to inspect its water storage.
            </p>
          </div>
          <div className="flex gap-2">
            <input className="input-field text-xs" placeholder="owner or map…" value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void load(); }} />
            <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
              {loading ? "…" : "search"}
            </button>
          </div>
        </header>
        <div className="card-body">
          {bases.length === 0 && !loading && (
            <p className="text-sm text-slate-500 italic">No claimed bases on this world (or none matching).</p>
          )}
          {bases.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-slate-500">
                    <th className="pr-3 py-1">Base</th>
                    <th className="pr-3">Owner</th>
                    <th className="pr-3">Map</th>
                    <th className="pr-3">Pieces</th>
                    <th className="pr-3">Placeables</th>
                  </tr>
                </thead>
                <tbody>
                  {bases.map((b) => (
                    <tr key={b.base_id}
                      className={"border-t border-slate-800 cursor-pointer hover:bg-slate-800/40"
                        + (selected?.base_id === b.base_id ? " bg-slate-800/60" : "")}
                      onClick={() => void loadWater(b)}>
                      <td className="pr-3 py-1 font-mono">#{b.base_id}</td>
                      <td className="pr-3">{b.owner || <span className="text-slate-500 italic">unclaimed</span>}</td>
                      <td className="pr-3">{b.map}</td>
                      <td className="pr-3">{b.pieces}</td>
                      <td className="pr-3">{b.placeables}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {selected && (
        <div className="card">
          <header className="card-header">
            <div>
              <h2 className="card-title">💧 Water — base #{selected.base_id}{selected.owner ? ` (${selected.owner})` : ""}</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Refill requires the base's map to be fully stopped — a running map rewrites
                base state from memory and would undo the write. Blood is never granted.
              </p>
            </div>
            <button className="btn-primary text-xs" disabled={busy || waterLoading}
              onClick={() => setConfirm({
                title: "Refill water storage?",
                message: `Fill every water device of base #${selected.base_id} to capacity. The server refuses unless the base's map has zero live instances.`,
                confirmLabel: "Refill",
                onConfirm: async () => {
                  setBusy(true);
                  const res = await baseWaterRefill(selected.base_id).catch(() => null);
                  setBusy(false);
                  const rb: unknown = res?.body;
                  const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
                    && "ok" in rb && (rb as { ok?: unknown }).ok === true;
                  pushToConsole(setConsoleEntries, `base-water-refill ${selected.base_id}`,
                    typeof rb === "string" ? rb : (rb as PublishResult), ok);
                  void loadWater(selected);
                },
              })}>
              {busy ? "…" : "Refill…"}
            </button>
          </header>
          <div className="card-body">
            {waterLoading && <p className="text-xs text-slate-500">loading…</p>}
            {!waterLoading && water.length === 0 && (
              <p className="text-sm text-slate-500 italic">No water devices at this base.</p>
            )}
            {water.length > 0 && (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-slate-500">
                    <th className="pr-3 py-1">Type</th>
                    <th className="pr-3">Devices</th>
                    <th className="pr-3">Stored</th>
                    <th className="pr-3">Capacity</th>
                    <th className="pr-3">Fill</th>
                    <th className="pr-3">Blood</th>
                  </tr>
                </thead>
                <tbody>
                  {water.map((w) => (
                    <tr key={w.water_type} className="border-t border-slate-800">
                      <td className="pr-3 py-1">{w.water_type}</td>
                      <td className="pr-3">{w.devices}</td>
                      <td className="pr-3 font-mono">{w.stored}</td>
                      <td className="pr-3 font-mono">{w.capacity}</td>
                      <td className="pr-3">{pct(w.stored, w.capacity)}</td>
                      <td className="pr-3 font-mono">
                        {Number(w.blood_capacity) > 0 ? `${w.blood_stored}/${w.blood_capacity}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {selected && (
        <div className="card">
          <header className="card-header">
            <div>
              <h2 className="card-title">⚡ Generators — base #{selected.base_id}</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Fuel lives as item stacks inside each device. Refill tops every device to its cap
                (Oil ×499, SpicedFuelCell ×499, lubricants ×499) and requires the base's map to be
                fully stopped.
              </p>
            </div>
            <button className="btn-primary text-xs" disabled={busy || fuelLoading || fuel.length === 0}
              onClick={() => setConfirm({
                title: "Refill generators?",
                message: `Top every generator/turbine of base #${selected.base_id} up to its fuel cap. The server refuses unless the base's map has zero live instances.`,
                confirmLabel: "Refill fuel",
                onConfirm: async () => {
                  setBusy(true);
                  const res = await baseFuelRefill(selected.base_id).catch(() => null);
                  setBusy(false);
                  const rb: unknown = res?.body;
                  const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
                    && "ok" in rb && (rb as { ok?: unknown }).ok === true;
                  pushToConsole(setConsoleEntries, `base-fuel-refill ${selected.base_id}`,
                    typeof rb === "string" ? rb : (rb as PublishResult), ok);
                  void loadFuel(selected);
                },
              })}>
              {busy ? "…" : "Refill fuel…"}
            </button>
          </header>
          <div className="card-body">
            {fuelLoading && <p className="text-xs text-slate-500">loading…</p>}
            {!fuelLoading && fuel.length === 0 && (
              <p className="text-sm text-slate-500 italic">No generators or wind turbines at this base.</p>
            )}
            {fuel.length > 0 && (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-slate-500">
                    <th className="pr-3 py-1">Device</th>
                    <th className="pr-3">Type</th>
                    <th className="pr-3">Fuel</th>
                    <th className="pr-3">Units</th>
                    <th className="pr-3">Fill</th>
                    <th className="pr-3">Runtime</th>
                  </tr>
                </thead>
                <tbody>
                  {fuel.map((f) => (
                    <tr key={f.placeable_id} className="border-t border-slate-800">
                      <td className="pr-3 py-1 font-mono">#{f.placeable_id}</td>
                      <td className="pr-3">{f.generator}</td>
                      <td className="pr-3">{f.fuel}</td>
                      <td className="pr-3 font-mono">{f.units}/{f.cap}</td>
                      <td className="pr-3">{f.percent}%</td>
                      <td className="pr-3">{f.runtime_hours} h</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      <Confirm
        open={confirm !== null}
        title={confirm?.title ?? ""}
        message={confirm?.message ?? ""}
        confirmLabel={confirm?.confirmLabel ?? ""}
        onConfirm={() => { confirm?.onConfirm(); setConfirm(null); }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}
