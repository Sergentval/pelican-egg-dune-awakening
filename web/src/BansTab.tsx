// Player bans (#118). The game has no ban command; the FLS stub refuses a
// banned player at every login and every travel, before they reach the world
// ("Player unauthorized to join server."). A player online at ban time is
// also kicked, so the ban takes effect at once.
import { useEffect, useMemo, useState } from "react";
import { type Ban, type PlayerRow, type PublishResult, banPlayer, fetchPlayers, liftBan, listBans, parsePlayerTable } from "./api";
import { Confirm } from "./components";

const DURATIONS: { label: string; secs: number | null }[] = [
  { label: "1 hour", secs: 3600 },
  { label: "1 day", secs: 86400 },
  { label: "7 days", secs: 7 * 86400 },
  { label: "30 days", secs: 30 * 86400 },
  { label: "Permanent", secs: null },
];

function when(iso: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? iso : t.toLocaleString();
}

function errorOf(body: unknown): string {
  if (body && typeof body === "object" && "error" in body) return String((body as { error?: string }).error ?? "error");
  return "request failed";
}

export function BansTab() {
  const [bans, setBans] = useState<Ban[]>([]);
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [filter, setFilter] = useState("");
  const [pick, setPick] = useState<PlayerRow | null>(null);
  const [duration, setDuration] = useState<number | null>(86400);
  const [reason, setReason] = useState("");
  const [kick, setKick] = useState(true);
  const [confirming, setConfirming] = useState<"ban" | Ban | null>(null);
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [b, p] = await Promise.all([listBans(), fetchPlayers("all")]);
    if (b.ok) setBans((b.body as { bans: Ban[] }).bans);
    if (p.ok) setPlayers(parsePlayerTable((p.body as PublishResult).stdout));
  }
  useEffect(() => { void refresh(); }, []);

  const banned = useMemo(() => new Set(bans.map((b) => b.fls_id)), [bans]);
  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return players
      .filter((p) => !q || (p.character ?? "").toLowerCase().includes(q) || p.fls_id.toLowerCase().includes(q))
      .slice(0, 50);
  }, [players, filter]);

  async function doBan() {
    if (!pick) return;
    setBusy(true);
    const res = await banPlayer({ fls_id: pick.fls_id, name: pick.character ?? "", reason: reason.trim(), duration_secs: duration, kick });
    setBusy(false);
    if (res.ok) {
      const label = pick.character ?? pick.fls_id;
      setStatus({ ok: true, text: `${label} is banned${duration === null ? " permanently" : ""}.${kick ? " Kick sent (no effect if they were offline)." : ""}` });
      setPick(null);
      setReason("");
    } else {
      setStatus({ ok: false, text: `Ban failed: ${errorOf(res.body)}` });
    }
    await refresh();
  }

  async function doLift(b: Ban) {
    setBusy(true);
    const res = await liftBan(b.fls_id);
    setBusy(false);
    setStatus(res.ok ? { ok: true, text: `Ban lifted for ${b.name || b.fls_id}.` } : { ok: false, text: `Could not lift: ${errorOf(res.body)}` });
    await refresh();
  }

  return (
    <div className="space-y-6 max-w-3xl">
      {status && (
        <div className={"card p-3 text-sm " + (status.ok ? "text-emerald-300" : "text-red-300")}>{status.text}</div>
      )}

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Ban a player</h2>
          <span className="text-xs text-slate-500">refused at every login and travel</span>
        </header>
        <div className="p-4 space-y-3">
          <div>
            <label className="label" htmlFor="ban-filter">Player</label>
            <input
              id="ban-filter"
              className="input-field w-full"
              placeholder="Search by character name or FLS id"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <div className="mt-2 max-h-56 overflow-y-auto border border-slate-800 rounded">
              {matches.length === 0 && <div className="p-2 text-xs text-slate-500">No player matches.</div>}
              {matches.map((p) => (
                <button
                  key={p.fls_id}
                  type="button"
                  disabled={banned.has(p.fls_id)}
                  onClick={() => setPick(p)}
                  className={
                    "w-full text-left px-3 py-1.5 text-sm flex justify-between gap-2 border-b border-slate-900 " +
                    (pick?.fls_id === p.fls_id ? "bg-slate-800 text-spice-200" : "hover:bg-slate-800/60") +
                    (banned.has(p.fls_id) ? " opacity-40 cursor-not-allowed" : "")
                  }
                >
                  <span>{p.character || <span className="font-mono text-slate-400">no character</span>}</span>
                  <span className="font-mono text-xs text-slate-500">
                    {p.fls_id}{p.online === "Online" ? " · online" : ""}{banned.has(p.fls_id) ? " · banned" : ""}
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="label">Duration</label>
            <div className="flex gap-1 flex-wrap">
              {DURATIONS.map((d) => (
                <button
                  key={d.label}
                  type="button"
                  onClick={() => setDuration(d.secs)}
                  className={"btn-ghost text-xs border border-slate-700 " + (duration === d.secs ? "bg-slate-800 text-spice-300" : "")}
                >
                  {d.label}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="label" htmlFor="ban-reason">Reason</label>
            <input
              id="ban-reason"
              className="input-field w-full"
              maxLength={500}
              placeholder="Shown in this list and the command history"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" checked={kick} onChange={(e) => setKick(e.target.checked)} />
            Kick them now if they are online
          </label>

          <div className="flex justify-end">
            <button type="button" className="btn-danger" disabled={!pick || busy} onClick={() => setConfirming("ban")}>
              Ban {pick ? pick.character || pick.fls_id : "…"}
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Banned players</h2>
          <span className="text-xs text-slate-500">{bans.length} active</span>
        </header>
        <div className="p-4">
          {bans.length === 0 ? (
            <p className="text-sm text-slate-500">Nobody is banned.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 text-left">
                <tr><th className="py-1">Player</th><th>Reason</th><th>Banned</th><th>Expires</th><th /></tr>
              </thead>
              <tbody>
                {bans.map((b) => (
                  <tr key={b.fls_id} className="border-t border-slate-800/50">
                    <td className="py-1.5">
                      <div className="text-slate-100">{b.name || "—"}</div>
                      <div className="text-[10px] font-mono text-slate-500">{b.fls_id}</div>
                    </td>
                    <td className="text-slate-300">{b.reason || <span className="text-slate-600">—</span>}</td>
                    <td className="text-xs text-slate-400">{when(b.banned_at)}</td>
                    <td className="text-xs text-slate-400">{b.expires_at ? when(b.expires_at) : "Permanent"}</td>
                    <td className="text-right">
                      <button type="button" className="btn-ghost text-xs border border-slate-700" disabled={busy} onClick={() => setConfirming(b)}>
                        Forgive
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <Confirm
        open={confirming === "ban"}
        title="Ban this player?"
        message={
          pick
            ? `${pick.character || pick.fls_id} will be refused at every login and travel${duration === null ? ", permanently" : ` for ${DURATIONS.find((d) => d.secs === duration)?.label ?? "the chosen time"}`}.`
            : ""
        }
        confirmLabel="Ban"
        onConfirm={() => { setConfirming(null); void doBan(); }}
        onCancel={() => setConfirming(null)}
      />
      <Confirm
        open={confirming !== null && confirming !== "ban"}
        title="Lift this ban?"
        message={confirming && confirming !== "ban" ? `${confirming.name || confirming.fls_id} will be able to join again.` : ""}
        confirmLabel="Forgive"
        onConfirm={() => { const b = confirming; setConfirming(null); if (b && b !== "ban") void doLift(b); }}
        onCancel={() => setConfirming(null)}
      />
    </div>
  );
}
