// Bases tab: every claimed base (owner, map, piece/placeable counts) with a
// per-base water panel and a hard-gated refill. The refill subcommand FAILS
// CLOSED unless the base's map has zero live instances — a running map
// rewrites base state from memory on flush, silently undoing DB writes (the
// memory-flush rule). Ported from Red-Blink's bases feature (MIT).

import { type Dispatch, type SetStateAction, useEffect, useRef, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import {
  baseFuelRefill,
  baseGuardApply,
  baseGuardConfig,
  baseGuardRevert,
  basePermissionRemove,
  basePermissionSet,
  baseTransferCustodian,
  baseWaterRefill,
  deleteItem,
  fetchBaseContainers,
  fetchBaseFuel,
  fetchBaseGuard,
  fetchBasePermissions,
  fetchBases,
  fetchBaseWater,
  fetchPermissionCandidates,
  type BaseContainerItem,
  type BaseFuelRow,
  type BaseGuardState,
  type BasePermissionRow,
  type BaseRow,
  type BaseWaterRow,
  type PermissionCandidate,
  type PublishResult,
} from "./api";

const RANK_LABELS: Record<string, string> = { "1": "Owner", "2": "Co-Owner", "3": "Associate" };

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
  const [items, setItems] = useState<BaseContainerItem[]>([]);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [roster, setRoster] = useState<BasePermissionRow[]>([]);
  const [rosterLoading, setRosterLoading] = useState(false);
  const [candQ, setCandQ] = useState("");
  // null = no search ran yet (renders nothing); [] = a search found nobody.
  const [candidates, setCandidates] = useState<PermissionCandidate[] | null>(null);
  const [candLoading, setCandLoading] = useState(false);
  const [guard, setGuard] = useState<BaseGuardState | null>(null);
  const [guardLoading, setGuardLoading] = useState(false);
  // A fetch failure must not make the safety card vanish (the sibling
  // panels degrade to a visible empty state, review-aligned): keep the
  // last known state on screen and flag the staleness instead.
  const [guardError, setGuardError] = useState(false);
  // Selection token: a slow response for base A must never render under
  // base B's header after a quick re-selection (review-caught display race).
  const selectedIdRef = useRef<string>("");
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
    selectedIdRef.current = base.base_id;
    setWater([]);
    setFuel([]);
    setItems([]);
    setRoster([]);
    setCandidates(null);
    setCandQ("");
    setWaterLoading(true);
    const res = await fetchBaseWater(base.base_id).catch(() => null);
    if (selectedIdRef.current !== base.base_id) return; // superseded selection
    setWaterLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "water" in b) {
      setWater((b as { water: BaseWaterRow[] }).water ?? []);
    } else {
      setWater([]);
    }
    void loadFuel(base);
    void loadContainers(base);
    void loadRoster(base);
  }

  async function loadContainers(base: BaseRow) {
    setItemsLoading(true);
    const res = await fetchBaseContainers(base.base_id).catch(() => null);
    if (selectedIdRef.current !== base.base_id) return; // superseded selection
    setItemsLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "items" in b) {
      setItems((b as { items: BaseContainerItem[] }).items ?? []);
    } else {
      setItems([]);
    }
  }

  async function loadRoster(base: BaseRow) {
    setRosterLoading(true);
    const res = await fetchBasePermissions(base.base_id).catch(() => null);
    if (selectedIdRef.current !== base.base_id) return; // superseded selection
    setRosterLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "roster" in b) {
      setRoster((b as { roster: BasePermissionRow[] }).roster ?? []);
    } else {
      setRoster([]);
    }
  }

  // One funnel for the three permission mutations: run, log to console,
  // refresh the roster of the base the write targeted (not whatever is
  // selected by the time the response lands).
  async function runPermissionWrite(label: string, run: () => Promise<{ ok: boolean; body: unknown } | null>) {
    if (!selected) return;
    const base = selected;
    setBusy(true);
    const res = await run().catch(() => null);
    setBusy(false);
    const rb: unknown = res?.body;
    const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
      && "ok" in rb && (rb as { ok?: unknown }).ok === true;
    pushToConsole(setConsoleEntries, label, typeof rb === "string" ? rb : (rb as PublishResult), ok);
    void loadRoster(base);
  }

  async function searchCandidates() {
    setCandLoading(true);
    const res = await fetchPermissionCandidates(candQ.trim()).catch(() => null);
    setCandLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "candidates" in b) {
      setCandidates((b as { candidates: PermissionCandidate[] }).candidates ?? []);
    } else {
      setCandidates([]);
    }
  }

  async function loadGuard() {
    setGuardLoading(true);
    const res = await fetchBaseGuard().catch(() => null);
    setGuardLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "available" in b) {
      setGuard(b as BaseGuardState);
      setGuardError(false);
    } else {
      setGuardError(true);
    }
  }

  async function runGuardAction(label: string, run: () => Promise<{ ok: boolean; body: unknown } | null>) {
    setBusy(true);
    const res = await run().catch(() => null);
    setBusy(false);
    const rb: unknown = res?.body;
    const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
      && "ok" in rb && (rb as { ok?: unknown }).ok === true;
    pushToConsole(setConsoleEntries, label, typeof rb === "string" ? rb : (rb as PublishResult), ok);
    void loadGuard();
  }

  async function loadFuel(base: BaseRow) {
    setFuelLoading(true);
    const res = await fetchBaseFuel(base.base_id).catch(() => null);
    if (selectedIdRef.current !== base.base_id) return; // superseded selection
    setFuelLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "fuel" in b) {
      setFuel((b as { fuel: BaseFuelRow[] }).fuel ?? []);
    } else {
      setFuel([]);
    }
  }

  useEffect(() => { void load(); void loadGuard(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

      {(guardError || (guard !== null && guard.available)) && (
        <div className="card">
          <header className="card-header">
            <div>
              <h2 className="card-title">🛡 Base backup wipe-guard</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                The weekly Deep Desert reset deletes stored base backups (their actors sit in
                the database in state 'BaseBackup', which Funcom's cleanup does not spare).
                The guard adds the missing exclusion to the cleanup function; a game update
                can undo it, so arm the boot re-apply once you rely on it.
              </p>
            </div>
            <div className="flex gap-2 items-center">
              <button className="btn-ghost text-xs" onClick={() => void loadGuard()} disabled={guardLoading}>
                {guardLoading ? "…" : "reload"}
              </button>
              {guard?.applied ? (
                <button className="btn-ghost text-xs text-red-300" disabled={busy || !guard.function_found}
                  onClick={() => setConfirm({
                    title: "Remove the wipe-guard?",
                    message: "Restore Funcom's original cleanup behaviour: the next weekly Deep Desert reset will delete stored base backups again.",
                    confirmLabel: "Remove guard",
                    onConfirm: () => void runGuardAction("base-guard-revert", () => baseGuardRevert()),
                  })}>
                  Remove guard…
                </button>
              ) : (
                <button className="btn-primary text-xs" disabled={busy || !guard || !guard.function_found}
                  onClick={() => setConfirm({
                    title: "Apply the wipe-guard?",
                    message: "Adds one exclusion to Funcom's season-cleanup function so stored base backups survive the weekly Deep Desert reset. Anchored and verified by re-read; reversible.",
                    confirmLabel: "Apply guard",
                    onConfirm: () => void runGuardAction("base-guard-apply", () => baseGuardApply()),
                  })}>
                  Apply guard…
                </button>
              )}
            </div>
          </header>
          <div className="card-body">
            {guardError && (
              <p className="text-xs text-amber-400 mb-2">
                Could not load the guard state{guard ? " — showing the last known state" : ""}. Reload to retry.
              </p>
            )}
            {guard !== null && (
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs items-center">
                <span>
                  Status:{" "}
                  {!guard.function_found
                    ? <span className="text-red-300">cleanup function not found on this build</span>
                    : guard.applied
                      ? <span className="text-emerald-400">protected — backups survive the wipe</span>
                      : <span className="text-amber-400">not protected</span>}
                </span>
                <span className="font-mono">{guard.base_backups} stored backup{guard.base_backups === 1 ? "" : "s"}</span>
                <span className="font-mono">{guard.backup_state_actors} backup actors</span>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="checkbox" checked={guard.boot_reapply} disabled={busy}
                    onChange={(e) => void runGuardAction(`base-guard-config ${e.target.checked ? "on" : "off"}`,
                      () => baseGuardConfig(e.target.checked))} />
                  re-apply at every boot
                </label>
              </div>
            )}
          </div>
        </div>
      )}

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
            {!waterLoading && water.length > 0 && (
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
            {!fuelLoading && fuel.length > 0 && (
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

      {selected && (
        <div className="card">
          <header className="card-header">
            <div>
              <h2 className="card-title">📦 Containers — base #{selected.base_id}</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Everything stored in this base's placeables. Deleting a stack requires the base's
                map to be fully stopped (world inventories are cached by the running map).
              </p>
            </div>
            <button className="btn-ghost text-xs" onClick={() => selected && void loadContainers(selected)}
              disabled={itemsLoading}>
              {itemsLoading ? "…" : "reload"}
            </button>
          </header>
          <div className="card-body">
            {itemsLoading && <p className="text-xs text-slate-500">loading…</p>}
            {!itemsLoading && items.length === 0 && (
              <p className="text-sm text-slate-500 italic">No storage placeables at this base.</p>
            )}
            {!itemsLoading && items.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-slate-500">
                      <th className="pr-3 py-1">Container</th>
                      <th className="pr-3">Slot</th>
                      <th className="pr-3">Item</th>
                      <th className="pr-3">Qty</th>
                      <th className="pr-3">Quality</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((it, idx) => {
                      const firstOfContainer = idx === 0 || items[idx - 1].placeable_id !== it.placeable_id;
                      return (
                        <tr key={`${it.placeable_id}-${it.item_id || idx}`} className="border-t border-slate-800">
                          <td className="pr-3 py-1 font-mono">
                            {firstOfContainer ? `#${it.placeable_id} ${it.container_type.replace(/_placeable$/, "")}` : ""}
                          </td>
                          <td className="pr-3 font-mono">{it.item_id ? it.position_index : "—"}</td>
                          <td className="pr-3">{it.item_id ? it.template_id : <span className="text-slate-500 italic">empty</span>}</td>
                          <td className="pr-3 font-mono">{it.item_id ? it.stack_size : ""}</td>
                          <td className="pr-3 font-mono">{it.item_id && it.quality_level !== "0" ? `Q${it.quality_level}` : ""}</td>
                          <td className="text-right">
                            {it.item_id && (
                              <button className="btn-ghost text-xs text-red-300" disabled={busy}
                                onClick={() => setConfirm({
                                  title: "Delete item stack?",
                                  message: `Permanently delete ${it.stack_size}× ${it.template_id} (item #${it.item_id}) from container #${it.placeable_id}. The base's map must be fully stopped. Unrecoverable.`,
                                  confirmLabel: "Delete",
                                  onConfirm: async () => {
                                    setBusy(true);
                                    const res = await deleteItem(it.item_id).catch(() => null);
                                    setBusy(false);
                                    const rb: unknown = res?.body;
                                    const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
                                      && "ok" in rb && (rb as { ok?: unknown }).ok === true;
                                    pushToConsole(setConsoleEntries, `item-delete ${it.item_id}`,
                                      typeof rb === "string" ? rb : (rb as PublishResult), ok);
                                    if (selected) void loadContainers(selected);
                                  },
                                })}>
                                🗑
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {selected && (
        <div className="card">
          <header className="card-header">
            <div>
              <h2 className="card-title">🔑 Permissions — base #{selected.base_id}</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                1 = Owner · 2 = Co-Owner · 3 = Associate. Edits go through the game's own
                procedures and reach the running map immediately — no restart needed.
              </p>
            </div>
            <button className="btn-ghost text-xs" disabled={busy || rosterLoading || roster.length === 0}
              onClick={() => setConfirm({
                title: "Transfer to system custodian?",
                message: `Hand base #${selected.base_id} to the reserved Server identity. The current Owner is demoted to Co-Owner and keeps access; the Server persona is created on first use. Reversible by promoting a player back to Owner.`,
                confirmLabel: "Transfer",
                onConfirm: () => void runPermissionWrite(`base-transfer-custodian ${selected.base_id}`,
                  () => baseTransferCustodian(selected.base_id)),
              })}>
              Transfer to custodian…
            </button>
          </header>
          <div className="card-body space-y-3">
            {rosterLoading && <p className="text-xs text-slate-500">loading…</p>}
            {!rosterLoading && roster.length === 0 && (
              <p className="text-sm text-slate-500 italic">
                No permission holders — this base is unclaimed, so the game has nothing to
                attach permissions to. A player must claim or redeploy it first.
              </p>
            )}
            {!rosterLoading && roster.length > 0 && (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-slate-500">
                    <th className="pr-3 py-1">Rank</th>
                    <th className="pr-3">Character</th>
                    <th className="pr-3">FLS id</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {roster.map((r, i) => (
                    <tr key={`${r.player_id}-${i}`} className="border-t border-slate-800">
                      <td className="pr-3 py-1">
                        {RANK_LABELS[r.rank] ? (
                          <select className="input-field text-xs py-0.5" value={r.rank}
                            disabled={busy || r.canonical === "f"}
                            onChange={(e) => {
                              const nr = e.target.value;
                              if (nr === r.rank || !(nr === "1" || nr === "2" || nr === "3")) return;
                              const who = r.character || `#${r.player_id}`;
                              if (nr === "1") {
                                setConfirm({
                                  title: "Promote to Owner?",
                                  message: `Make ${who} the Owner of base #${selected.base_id}. The current Owner is demoted to Co-Owner in the same transaction.`,
                                  confirmLabel: "Promote",
                                  onConfirm: () => void runPermissionWrite(
                                    `base-permission-set ${selected.base_id} ${r.player_id} 1`,
                                    () => basePermissionSet(selected.base_id, r.player_id, 1)),
                                });
                              } else {
                                void runPermissionWrite(
                                  `base-permission-set ${selected.base_id} ${r.player_id} ${nr}`,
                                  () => basePermissionSet(selected.base_id, r.player_id, Number(nr) as 2 | 3));
                              }
                            }}>
                            <option value="1">1 — Owner</option>
                            <option value="2">2 — Co-Owner</option>
                            <option value="3">3 — Associate</option>
                          </select>
                        ) : (
                          <span className="font-mono">{r.rank}</span>
                        )}
                        {r.rank === "1" ? " 👑" : ""}
                      </td>
                      <td className="pr-3">
                        {r.character || <span className="text-slate-500 italic">deleted character (actor #{r.player_id})</span>}
                        {r.canonical === "f" && (
                          <span className="text-amber-400"
                            title="Not a player_controller_id — the game ignores this row">{" "}⚠</span>
                        )}
                      </td>
                      <td className="pr-3 font-mono">{r.fls_id}</td>
                      <td className="text-right">
                        {r.rank !== "1" && (
                          <button className="btn-ghost text-xs text-red-300" disabled={busy}
                            onClick={() => setConfirm({
                              title: "Remove permission?",
                              message: `Remove ${r.character || `#${r.player_id}`} (${RANK_LABELS[r.rank] ?? `rank ${r.rank}`}) from base #${selected.base_id}.`,
                              confirmLabel: "Remove",
                              onConfirm: () => void runPermissionWrite(
                                `base-permission-remove ${selected.base_id} ${r.player_id}`,
                                () => basePermissionRemove(selected.base_id, r.player_id)),
                            })}>
                            ✕
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {!rosterLoading && roster.length > 0 && (
              <div className="border-t border-slate-800 pt-3">
                <div className="flex gap-2 items-center">
                  <input className="input-field text-xs" placeholder="add player — name or controller id…"
                    value={candQ} onChange={(e) => setCandQ(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") void searchCandidates(); }} />
                  <button className="btn-ghost text-xs" onClick={() => void searchCandidates()}
                    disabled={candLoading}>
                    {candLoading ? "…" : "search"}
                  </button>
                </div>
                {candidates !== null && candidates.length === 0 && (
                  <p className="text-xs text-slate-500 italic mt-2">No matching players.</p>
                )}
                {candidates !== null && candidates.length > 0 && (
                  <table className="w-full text-xs mt-2">
                    <tbody>
                      {candidates.map((c) => {
                        const held = roster.some((r) => r.player_id === c.player_id);
                        return (
                          <tr key={c.player_id} className="border-t border-slate-800">
                            <td className="pr-3 py-1">{c.character}</td>
                            <td className="pr-3 font-mono">{c.fls_id}</td>
                            <td className="text-right">
                              {held ? (
                                <span className="text-slate-500 italic">already on roster</span>
                              ) : (
                                <button className="btn-ghost text-xs" disabled={busy}
                                  onClick={() => void runPermissionWrite(
                                    `base-permission-set ${selected.base_id} ${c.player_id} 3`,
                                    () => basePermissionSet(selected.base_id, c.player_id, 3))}>
                                  + add as Associate
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
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
