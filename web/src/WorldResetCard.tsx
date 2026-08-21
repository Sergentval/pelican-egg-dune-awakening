// World reset card (DST worldreset-2 port): arm a REVERSIBLE season reset.
// Nothing is destroyed from here — arming takes a verified backup and
// writes a durable marker; the next restart executes, setting the current
// datadir aside (moved, never deleted). Rollback is the reverse swap.
// Both phrases are re-validated server-side; this card is transport + a
// clear picture of what is armed.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import {
  armRestart,
  fetchWorldReset,
  worldResetArm,
  worldResetCancel,
  worldRollbackArm,
  type ArmRestartResult,
  type PublishResult,
  type WorldResetState,
} from "./api";
import { pushToConsole, type ConsoleEntry } from "./components";

const RESET_PHRASE = "RESET WORLD";
const ROLLBACK_PHRASE = "ROLL BACK WORLD";

export function WorldResetCard({ setConsoleEntries }: { setConsoleEntries: Dispatch<SetStateAction<ConsoleEntry[]>> }) {
  const [state, setState] = useState<WorldResetState | null>(null);
  const [stateError, setStateError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [withChars, setWithChars] = useState(true);
  const [rbPhrase, setRbPhrase] = useState("");
  const [rbTarget, setRbTarget] = useState("");

  async function load() {
    setLoading(true);
    const res = await fetchWorldReset().catch(() => null);
    setLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "preserved" in b) {
      setState(b as WorldResetState);
      setStateError(false);
    } else {
      setStateError(true);
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function runAction(label: string, run: () => Promise<{ ok: boolean; body: unknown } | null>) {
    setBusy(true);
    const res = await run().catch(() => null);
    setBusy(false);
    const rb: unknown = res?.body;
    const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
      && "ok" in rb && (rb as { ok?: unknown }).ok === true;
    pushToConsole(setConsoleEntries, label, typeof rb === "string" ? rb : (rb as PublishResult), ok);
    setPhrase("");
    setRbPhrase("");
    void load();
  }

  async function restartNow() {
    setBusy(true);
    const res = await armRestart(60, 60, 15).catch(() => null);
    setBusy(false);
    const rb = res?.body as ArmRestartResult | undefined;
    pushToConsole(setConsoleEntries, "restart-in 60",
      rb ? JSON.stringify(rb) : "failed", Boolean(res?.ok && rb?.ok));
  }

  const ts = (unix: number | undefined) =>
    unix ? new Date(unix * 1000).toLocaleString() : "";

  return (
    <div className="card border border-red-900/40">
      <header className="card-header">
        <div>
          <h2 className="card-title">🌍 World reset</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Season reset, reversible: arming takes a verified database backup and writes a
            durable marker — the NEXT RESTART sets the current world aside (never deleted)
            and boots a fresh one under the same identity and config. Roll back any time.
          </p>
        </div>
        <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
          {loading ? "…" : "reload"}
        </button>
      </header>
      <div className="card-body space-y-3 text-xs">
        {stateError && (
          <p className="text-amber-400">Could not load the world-reset state{state ? " — showing the last known state" : ""}. Reload to retry.</p>
        )}

        {state?.last_result && (
          <p className={state.last_result.ok ? "text-slate-400" : "text-red-300"}>
            Last boot action: {state.last_result.operation} — {state.last_result.ok ? "ok" : "FAILED"} —{" "}
            {state.last_result.detail}
            {state.last_result.preserved ? ` (kept: ${state.last_result.preserved})` : ""} · {ts(state.last_result.at)}
          </p>
        )}

        {state?.pending && (
          <div className="rounded border border-amber-700/40 p-2 space-y-1.5">
            <p className="text-amber-300 font-medium">
              ⚠ RESET ARMED — the next restart wipes to a fresh world.
            </p>
            <p className="text-slate-400">
              Backup {state.pending.backup_file} ({state.pending.backup_bytes} bytes),{" "}
              {state.pending.char_backups.length} character backup(s) · armed {ts(state.pending.requested_at)}
            </p>
            <div className="flex gap-2">
              <button className="btn-primary text-xs" disabled={busy} onClick={() => void restartNow()}>
                Restart now (60 s warning)
              </button>
              <button className="btn-ghost text-xs" disabled={busy}
                onClick={() => void runAction("world-reset-cancel", () => worldResetCancel())}>
                Disarm
              </button>
            </div>
          </div>
        )}

        {state?.rollback && (
          <div className="rounded border border-amber-700/40 p-2 space-y-1.5">
            <p className="text-amber-300 font-medium">
              ⚠ ROLLBACK ARMED — the next restart restores {state.rollback.restore_dir}.
            </p>
            <div className="flex gap-2">
              <button className="btn-primary text-xs" disabled={busy} onClick={() => void restartNow()}>
                Restart now (60 s warning)
              </button>
              <button className="btn-ghost text-xs" disabled={busy}
                onClick={() => void runAction("world-reset-cancel", () => worldResetCancel())}>
                Disarm
              </button>
            </div>
          </div>
        )}

        {state && !state.pending && !state.rollback && (
          <div className="space-y-2">
            <p className="text-slate-400">
              Online now: <span className="font-mono">{state.online_players ?? "?"}</span>
              {" — "}the server refuses to arm unless this is 0.
            </p>
            <div className="flex flex-wrap gap-2 items-center">
              <input className="input-field text-xs font-mono" placeholder={`type ${RESET_PHRASE}`}
                value={phrase} onChange={(e) => setPhrase(e.target.value)} />
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={withChars}
                  onChange={(e) => setWithChars(e.target.checked)} />
                back up every character first
              </label>
              <button className="btn-ghost text-xs text-red-300"
                disabled={busy || phrase !== RESET_PHRASE}
                onClick={() => void runAction(
                  `world-reset-arm${withChars ? " with-char-backups" : ""}`,
                  () => worldResetArm(phrase, withChars))}>
                Arm world reset
              </button>
            </div>
            {state.preserved.length > 0 && (
              <div className="flex flex-wrap gap-2 items-center border-t border-slate-800 pt-2">
                <select className="input-field text-xs font-mono py-0.5" value={rbTarget || state.preserved[0]}
                  onChange={(e) => setRbTarget(e.target.value)}>
                  {state.preserved.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <input className="input-field text-xs font-mono" placeholder={`type ${ROLLBACK_PHRASE}`}
                  value={rbPhrase} onChange={(e) => setRbPhrase(e.target.value)} />
                <button className="btn-ghost text-xs text-red-300"
                  disabled={busy || rbPhrase !== ROLLBACK_PHRASE}
                  onClick={() => void runAction(
                    `world-rollback-arm ${rbTarget || state.preserved[0]}`,
                    () => worldRollbackArm(rbPhrase, rbTarget || state.preserved[0]))}>
                  Arm rollback
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
