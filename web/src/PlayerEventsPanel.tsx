// Player events (dune-admin port): admin-defined zone races + milestones
// that watch the game DB and pay the shared reward spec. Everything here
// is transport — types, gates and reward validation are server-side.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import {
  createPlayerEvent,
  fetchPlayerEventClaims,
  fetchPlayerEvents,
  playerEventAction,
  playerEventsConfig,
  type PlayerEvent,
  type PlayerEventClaim,
} from "./api";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;
type ConfirmSpec = { title: string; message: string; confirmLabel: string; onConfirm: () => void };

const EMPTY_FORM = {
  name: "", type: "milestone",
  signal: "level", threshold: 10, tag_name: "", award_past: false,
  map: "", x: 0, y: 0, z: 0, radius: 500, participants: "",
  reward: '{"currency": 1000}', announce_template: "{player} completed {event}!",
  poll_seconds: 7, jitter_seconds: 3,
};

export function PlayerEventsPanel({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [enabled, setEnabled] = useState(false);
  const [events, setEvents] = useState<PlayerEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [claimsFor, setClaimsFor] = useState<number | null>(null);
  const [claims, setClaims] = useState<PlayerEventClaim[]>([]);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  async function load() {
    setLoading(true);
    const res = await fetchPlayerEvents().catch(() => null);
    setLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "events" in b) {
      const data = b as { enabled: boolean; events: PlayerEvent[] };
      setEnabled(data.enabled);
      setEvents(data.events ?? []);
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(label: string, fn: () => Promise<{ ok: boolean; body: unknown } | null>) {
    setBusy(true);
    const res = await fn().catch(() => null);
    setBusy(false);
    const rb = res?.body as { ok?: boolean; error?: string } | undefined;
    const ok = Boolean(res?.ok && rb?.ok);
    pushToConsole(setConsoleEntries, label, rb?.error ?? (ok ? "ok" : "failed"), ok);
    void load();
    return ok;
  }

  function buildConfig(): string {
    if (form.type === "zone_race") {
      const participants = form.participants.split(",")
        .map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n) && n > 0);
      return JSON.stringify({
        map: form.map, x: Number(form.x), y: Number(form.y), z: Number(form.z),
        radius: Number(form.radius), participants,
      });
    }
    if (form.signal === "level") {
      return JSON.stringify({ signal: "level", threshold: Number(form.threshold),
                              award_past: form.award_past });
    }
    return JSON.stringify({ signal: "achievement_tag", tag_name: form.tag_name,
                            award_past: form.award_past });
  }

  async function submitCreate() {
    const ok = await run(`player-event create ${form.name}`, () =>
      createPlayerEvent({
        name: form.name, type: form.type, config: buildConfig(),
        reward: form.reward, announce_template: form.announce_template,
        poll_seconds: Number(form.poll_seconds), jitter_seconds: Number(form.jitter_seconds),
      }));
    if (ok) {
      setShowCreate(false);
      setForm({ ...EMPTY_FORM });
    }
  }

  async function showClaims(id: number) {
    setClaimsFor(id);
    setClaims([]);
    const res = await fetchPlayerEventClaims(id).catch(() => null);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "claims" in b) {
      setClaims((b as { claims: PlayerEventClaim[] }).claims ?? []);
    }
  }

  const input = "input-field text-xs";

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="card-title">🎯 Player events</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Zone races pay listed participants who reach a sphere; milestones pay every
              online player crossing a level or holding an achievement tag. Each player is
              paid ONCE per event (claim ledger); disabling stops watching, grants stay.
            </p>
          </div>
          <div className="flex gap-2 items-center">
            <label className="flex items-center gap-1.5 text-xs cursor-pointer">
              <input type="checkbox" checked={enabled} disabled={busy}
                onChange={(e) => void run(`player-events ${e.target.checked ? "on" : "off"}`,
                  () => playerEventsConfig(e.target.checked))} />
              engine enabled
            </label>
            <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
              {loading ? "…" : "reload"}
            </button>
            <button className="btn-primary text-xs" onClick={() => setShowCreate(!showCreate)}>
              {showCreate ? "close" : "+ new event"}
            </button>
          </div>
        </header>
        <div className="card-body space-y-3">
          {showCreate && (
            <div className="rounded border border-slate-800 p-3 space-y-2 text-xs">
              <div className="flex flex-wrap gap-2">
                <input className={input} placeholder="event name" value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })} />
                <select className={input} value={form.type}
                  onChange={(e) => setForm({ ...form, type: e.target.value })}>
                  <option value="milestone">milestone</option>
                  <option value="zone_race">zone race</option>
                </select>
                {form.type === "milestone" && (
                  <>
                    <select className={input} value={form.signal}
                      onChange={(e) => setForm({ ...form, signal: e.target.value })}>
                      <option value="level">level reached</option>
                      <option value="achievement_tag">has tag</option>
                    </select>
                    {form.signal === "level" ? (
                      <input className={input} type="number" min={1} max={200}
                        value={form.threshold} title="level threshold"
                        onChange={(e) => setForm({ ...form, threshold: Number(e.target.value) })} />
                    ) : (
                      <input className={input} placeholder="tag name (e.g. Exploration.POI.Arrakeen)"
                        value={form.tag_name}
                        onChange={(e) => setForm({ ...form, tag_name: e.target.value })} />
                    )}
                    <label className="flex items-center gap-1 cursor-pointer">
                      <input type="checkbox" checked={form.award_past}
                        onChange={(e) => setForm({ ...form, award_past: e.target.checked })} />
                      award past achievers
                    </label>
                  </>
                )}
                {form.type === "zone_race" && (
                  <>
                    <input className={input} placeholder="map (empty = any)" value={form.map}
                      onChange={(e) => setForm({ ...form, map: e.target.value })} />
                    {(["x", "y", "z", "radius"] as const).map((axis) => (
                      <input key={axis} className={input + " w-24"} type="number"
                        title={axis} placeholder={axis} value={form[axis]}
                        onChange={(e) => setForm({ ...form, [axis]: Number(e.target.value) })} />
                    ))}
                    <input className={input + " min-w-[16rem]"}
                      placeholder="participant account ids, comma-separated"
                      value={form.participants}
                      onChange={(e) => setForm({ ...form, participants: e.target.value })} />
                  </>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <input className={input + " min-w-[22rem] font-mono"}
                  placeholder='reward JSON — {"currency":N,"items":[…],"xp":[…]}'
                  value={form.reward}
                  onChange={(e) => setForm({ ...form, reward: e.target.value })} />
                <input className={input + " min-w-[18rem]"}
                  placeholder="announce template ({player} {event} {value})"
                  value={form.announce_template}
                  onChange={(e) => setForm({ ...form, announce_template: e.target.value })} />
                <button className="btn-primary text-xs" disabled={busy || !form.name}
                  onClick={() => void submitCreate()}>
                  Create (disabled until you arm it)
                </button>
              </div>
            </div>
          )}

          {events.length === 0 && !loading && (
            <p className="text-sm text-slate-500 italic">No events defined yet.</p>
          )}
          {events.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500">
                  <th className="pr-3 py-1">Event</th>
                  <th className="pr-3">Type</th>
                  <th className="pr-3">Paid</th>
                  <th className="pr-3">Pending</th>
                  <th className="pr-3">Armed</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id} className="border-t border-slate-800">
                    <td className="pr-3 py-1">{e.name}
                      <span className="text-slate-500"> #{e.id}</span></td>
                    <td className="pr-3">{e.type}</td>
                    <td className="pr-3 font-mono">{e.claims.granted ?? 0}</td>
                    <td className="pr-3 font-mono">
                      {(e.claims.pending ?? 0) + (e.claims.exhausted ?? 0) || ""}
                    </td>
                    <td className="pr-3">
                      <input type="checkbox" checked={!!e.enabled} disabled={busy}
                        onChange={(ev) => void run(
                          `player-event #${e.id} ${ev.target.checked ? "enable" : "disable"}`,
                          () => playerEventAction(e.id, "enable",
                                                  { enabled: ev.target.checked }))} />
                    </td>
                    <td className="text-right space-x-2">
                      <button className="btn-ghost text-xs" onClick={() => void showClaims(e.id)}>
                        claims
                      </button>
                      <button className="btn-ghost text-xs" disabled={busy}
                        onClick={() => setConfirm({
                          title: "Reset claims?",
                          message: `Forget who event "${e.name}" already paid — the next tick pays every current qualifier AGAIN. This is a re-arm, not a refund.`,
                          confirmLabel: "Reset",
                          onConfirm: () => void run(`player-event #${e.id} reset`,
                            () => playerEventAction(e.id, "reset")),
                        })}>
                        reset
                      </button>
                      <button className="btn-ghost text-xs text-red-300" disabled={busy}
                        onClick={() => setConfirm({
                          title: "Delete event?",
                          message: `Delete "${e.name}" and its claim history. Granted rewards stay with the players.`,
                          confirmLabel: "Delete",
                          onConfirm: () => void run(`player-event #${e.id} delete`,
                            () => playerEventAction(e.id, "delete")),
                        })}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {claimsFor !== null && (
            <div className="rounded border border-slate-800 p-2">
              <div className="flex justify-between items-center text-xs mb-1">
                <span className="text-slate-400">Claims for event #{claimsFor}</span>
                <button className="btn-ghost text-xs" onClick={() => setClaimsFor(null)}>close</button>
              </div>
              {claims.length === 0
                ? <p className="text-xs text-slate-500 italic">No claims yet.</p>
                : <table className="w-full text-xs">
                    <tbody>
                      {claims.map((c, i) => (
                        <tr key={i} className="border-t border-slate-900">
                          <td className="pr-3 py-0.5 font-mono">acct {c.account_id}</td>
                          <td className="pr-3">{c.status}</td>
                          <td className="pr-3">{c.attempts ? `${c.attempts} attempt(s)` : ""}</td>
                          <td className="text-slate-500 truncate max-w-[18rem]">{c.last_error}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>}
            </div>
          )}
        </div>
      </div>

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
