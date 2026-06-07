// Logs tab: tail any service log (secret-redacted server-side) + restart a
// single background service without bouncing the container.
//
//   GET  /api/logs/sources        -> source dropdown (fixed services + ue5-*)
//   GET  /api/logs?source=&tail=N -> redacted tail
//   POST /api/svc/restart {service} -> kill + relaunch one service
//
// Restart is allowlisted server-side (no postgres/mq-*/ue5-*); the buttons below
// mirror that set. Restarting admin-http drops this panel's connection briefly.

import { type Dispatch, type SetStateAction, useEffect, useRef, useState } from "react";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchLogSources,
  fetchLogs,
  restartService,
  type LogSource,
  type LogTailResp,
  type PublishResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

// Mirror of admin_logs.RESTARTABLE_SERVICES (the backend re-validates).
const RESTARTABLE = [
  "admin-http", "scheduler", "welcome-scanner", "market-bot",
  "mock-k8s", "director", "gateway", "text-router", "fls-stub",
];
const TAIL_OPTIONS = [100, 200, 500, 1000, 2000];

// Plain-language description of each restartable service (button tooltips +
// glossary) so a non-technical operator knows what they'd be bouncing.
const SERVICE_INFO: Record<string, string> = {
  "admin-http": "this admin web panel",
  "scheduler": "auto-restart + auto-backup jobs",
  "welcome-scanner": "grants the new-player kit on join",
  "market-bot": "NPC marketplace seeding + buy loop",
  "mock-k8s": "instance scaler (spawns / reaps maps)",
  "director": "routes players to the right instance",
  "gateway": "player connection gateway",
  "text-router": "in-game chat routing",
  "fls-stub": "Funcom Live Service auth shim",
};

export function LogsTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [sources, setSources] = useState<LogSource[]>([]);
  const [source, setSource] = useState("admin-http");
  const [tailN, setTailN] = useState(200);
  const [data, setData] = useState<LogTailResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(false);
  const [confirmSvc, setConfirmSvc] = useState<string | null>(null);
  const paneRef = useRef<HTMLDivElement>(null);

  async function loadSources() {
    const res = await fetchLogSources().catch(() => null);
    if (res && res.ok && typeof res.body === "object" && res.body) {
      const list = (res.body as { sources: LogSource[] }).sources || [];
      setSources(list);
      if (list.length && !list.some((s) => s.name === source)) setSource(list[0].name);
    }
  }

  async function load() {
    if (!source) return;
    setLoading(true);
    const res = await fetchLogs(source, tailN).catch(() => null);
    setLoading(false);
    if (res && res.ok && typeof res.body === "object" && res.body) {
      setData(res.body as LogTailResp);
      // stick to the bottom (newest) after render
      requestAnimationFrame(() => {
        if (paneRef.current) paneRef.current.scrollTop = paneRef.current.scrollHeight;
      });
    } else {
      pushToConsole(setConsoleEntries, `GET /api/logs ${source}`, "failed to load log", false);
    }
  }

  useEffect(() => {
    void loadSources();
  }, []);

  // Reload whenever the selected source / tail changes.
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, tailN]);

  // Auto-refresh poll.
  useEffect(() => {
    if (!auto) return;
    const t = setInterval(() => void load(), 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, source, tailN]);

  async function doRestart() {
    const svc = confirmSvc;
    setConfirmSvc(null);
    if (!svc) return;
    const res = await restartService(svc).catch(() => null);
    if (!res) {
      // admin-http self-restart drops the connection — that's expected.
      pushToConsole(setConsoleEntries, `svc-restart ${svc}`,
        svc === "admin-http" ? "panel reconnecting after admin-http restart…" : "request failed", svc === "admin-http");
    } else {
      const b = res.body;
      const ok = res.ok && typeof b === "object" && b !== null && (b as { ok?: boolean }).ok === true;
      pushToConsole(setConsoleEntries, `svc-restart ${svc}`, b as PublishResult, ok);
    }
    setTimeout(() => void load(), 2000);
  }

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header flex-wrap gap-2">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="font-semibold">Logs</h2>
            <select value={source} onChange={(e) => setSource(e.target.value)} className="input-field text-xs w-auto">
              {sources.map((s) => (
                <option key={s.name} value={s.name} disabled={!s.exists}>
                  {s.name}{s.exists ? "" : " (none yet)"}
                </option>
              ))}
            </select>
            <select value={tailN} onChange={(e) => setTailN(parseInt(e.target.value, 10) || 200)} className="input-field text-xs w-auto">
              {TAIL_OPTIONS.map((n) => <option key={n} value={n}>last {n}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-3">
            <label className="text-xs text-slate-400 flex items-center gap-1.5">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto 5s
            </label>
            <button className="btn-ghost text-xs" onClick={() => void load()} disabled={loading}>
              {loading ? "…" : "refresh"}
            </button>
          </div>
        </header>
        <div ref={paneRef} className="font-mono text-xs p-3 max-h-[600px] overflow-y-auto bg-slate-950/40">
          {!data && <div className="text-slate-500 italic">loading…</div>}
          {data && !data.exists && <div className="text-slate-500 italic">no log file for {data.source} yet</div>}
          {data && data.exists && data.lines.length === 0 && <div className="text-slate-500 italic">(empty)</div>}
          {data?.lines.map((ln, i) => (
            <pre key={i} className="whitespace-pre-wrap break-all text-slate-300 leading-tight">{ln || " "}</pre>
          ))}
        </div>
        {data && (
          <div className="px-3 pb-2 text-[10px] text-slate-500">
            {data.count} lines · {data.source}{auto ? " · auto 5s" : ""} · secrets redacted server-side
          </div>
        )}
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Service control</h2>
          <span className="text-xs text-slate-500">restart one service (no container bounce)</span>
        </header>
        <div className="p-4 flex flex-wrap gap-2">
          {RESTARTABLE.map((svc) => (
            <button key={svc} className="btn-ghost text-xs border border-slate-700"
              onClick={() => setConfirmSvc(svc)} title={SERVICE_INFO[svc] || svc}>
              ↻ {svc}
            </button>
          ))}
        </div>
        <div className="px-4 pb-2 text-[11px] text-slate-500 grid sm:grid-cols-2 gap-x-4 gap-y-0.5">
          {RESTARTABLE.map((svc) => (
            <div key={svc}><span className="font-mono text-slate-400">{svc}</span> — {SERVICE_INFO[svc] || "service"}</div>
          ))}
        </div>
        <p className="px-4 pb-4 text-xs text-slate-500">
          postgres, message brokers and UE5 maps are not individually restartable here — use a full server restart for those.
        </p>
      </div>

      <Confirm
        open={confirmSvc !== null}
        title={`Restart ${confirmSvc ?? ""}?`}
        message={`Interrupt in-flight work on ${confirmSvc ?? "this service"} and relaunch it.${
          confirmSvc === "admin-http" ? " This panel will briefly disconnect, then reconnect." : ""
        }`}
        confirmLabel="Restart"
        onConfirm={() => void doRestart()}
        onCancel={() => setConfirmSvc(null)}
      />
    </div>
  );
}
