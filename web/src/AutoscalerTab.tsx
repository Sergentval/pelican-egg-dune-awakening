// Autoscaler tab: the demand-based instance autoscaler.
//
// Arms a background loop (autoscaler.json enabled) that each tick reads the live
// per-map online count + new director.log travel-demand and scales the 3 on-demand
// maps via mock-k8s ServerSetScale. Per map, the floor selects behaviour:
//   min_replicas >= 1  -> stay warm + load-scale up under population (instant joins)
//   min_replicas == 0  -> drain fully cold when idle + wake on inbound travel (~40s
//                         cold boot, but frees RAM)
// Anti-flap: an empty map must stay empty idle_drain_secs before draining and is
// immune to drain for demand_grace_secs after any inbound travel.
//
// Backed by GET /api/autoscaler and POST /api/autoscaler/{config,tick}.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import {
  type AutoscalerConfig,
  type AutoscalerMapCfg,
  type AutoscalerStatus,
  autoscalerConfig,
  autoscalerTick,
  fetchAutoscaler,
  type PublishResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

function logResult(setEntries: SetEntries, label: string, res: { ok: boolean; body: unknown } | null): boolean {
  if (!res) {
    pushToConsole(setEntries, label, "request failed (network error)", false);
    return false;
  }
  const b: unknown = res.body;
  const isObj = typeof b === "object" && b !== null;
  const ok = res.ok && isObj && "ok" in b && (b as { ok?: unknown }).ok === true;
  let msg: PublishResult | string;
  if (typeof b === "string") {
    msg = b;
  } else if (isObj && "error" in b && (b as { error?: string }).error) {
    msg = String((b as { error?: string }).error);
  } else if (isObj) {
    msg = JSON.stringify(b);
  } else {
    msg = String(b);
  }
  pushToConsole(setEntries, label, msg, ok);
  return ok;
}

const SCALE_MAX = 4; // mirrors admin_instances.SCALE_REPLICAS_MAX

const DEFAULTS: AutoscalerConfig = {
  enabled: false,
  scan_interval_secs: 60,
  idle_drain_secs: 300,
  demand_grace_secs: 120,
  players_per_instance: 30,
  webhook_url: "",
  maps: [],
};

export function AutoscalerTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [info, setInfo] = useState<AutoscalerStatus | null>(null);
  const [cfg, setCfg] = useState<AutoscalerConfig>(DEFAULTS);
  const [busy, setBusy] = useState("");

  async function load() {
    const r = await fetchAutoscaler().catch(() => null);
    if (r && r.ok && typeof r.body === "object" && r.body) {
      const s = r.body as AutoscalerStatus;
      setInfo(s);
      if (s.config) setCfg({ ...DEFAULTS, ...s.config, maps: s.config.maps ?? [] });
    }
  }

  // Poll refreshes only the live/status view — NOT the editable config — so a 15s
  // tick can't clobber edits the operator is mid-typing.
  async function refresh() {
    const r = await fetchAutoscaler().catch(() => null);
    if (r && r.ok && typeof r.body === "object" && r.body) setInfo(r.body as AutoscalerStatus);
  }

  useEffect(() => {
    void load();
    const t = setInterval(() => void refresh(), 15000);
    return () => clearInterval(t);
  }, []);

  async function saveConfig() {
    setBusy("config");
    const res = await autoscalerConfig(cfg).catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, cfg.enabled ? "autoscaler armed" : "autoscaler disarmed", res)) void load();
  }

  async function tickNow() {
    setBusy("tick");
    const res = await autoscalerTick().catch(() => null);
    setBusy("");
    if (logResult(setConsoleEntries, "autoscaler tick", res)) void load();
  }

  const anyBusy = busy !== "";
  const armed = info?.config?.enabled === true;

  function num(key: keyof Omit<AutoscalerConfig, "maps" | "enabled">, label: string, min: number, max: number) {
    return (
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-slate-400">{label}</span>
        <input
          type="number" min={min} max={max} step={1} value={cfg[key]}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10);
            setCfg({ ...cfg, [key]: Number.isNaN(v) ? min : Math.max(min, Math.min(max, v)) });
          }}
          className="input-field w-full font-mono"
        />
      </label>
    );
  }

  function updateMap(i: number, patch: Partial<AutoscalerMapCfg>) {
    setCfg({ ...cfg, maps: cfg.maps.map((m, idx) => (idx === i ? { ...m, ...patch } : m)) });
  }

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="font-semibold flex items-center gap-2">
              Autoscaler
              <span className={armed ? "pill-ok" : "pill-warn"}>{armed ? "armed" : "off"}</span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Demand-based scaling of the on-demand maps via mock-k8s. When armed, each tick
              ({cfg.scan_interval_secs}s) drains idle maps to their floor and wakes / load-scales them up.
              Never evicts a non-empty map; never scales down on an unreadable player count.
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={() => void load()} disabled={anyBusy}>refresh</button>
        </header>

        <div className="p-4 space-y-4">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={cfg.enabled}
              onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} />
            <span>Enable the autoscaler loop</span>
          </label>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {num("scan_interval_secs", "Tick interval (s)", 30, 3600)}
            {num("idle_drain_secs", "Idle drain (s)", 0, 86400)}
            {num("demand_grace_secs", "Demand grace (s)", 0, 86400)}
            {num("players_per_instance", "Players / instance", 1, 1000)}
          </div>

          <label className="flex flex-col gap-1 text-xs">
            <span className="text-slate-400">Discord webhook URL (optional — alerts on scale up/down/error)</span>
            <input type="text" value={cfg.webhook_url ?? ""} placeholder="https://discord.com/api/webhooks/…"
              onChange={(e) => setCfg({ ...cfg, webhook_url: e.target.value })}
              className="input-field w-full font-mono" />
          </label>

          {/* Per-map floor/ceiling. min=0 means the map drains fully cold + wakes on demand. */}
          <div className="space-y-2">
            <div className="text-xs uppercase tracking-wider text-slate-500">Managed maps</div>
            <div className="space-y-2">
              {cfg.maps.length === 0 && <p className="text-xs text-slate-500">No managed maps in config.</p>}
              {cfg.maps.map((m, i) => (
                <div key={m.map} className="flex flex-wrap items-end gap-3 border border-slate-800 rounded p-3">
                  <label className="flex items-center gap-2 text-sm min-w-[12rem]">
                    <input type="checkbox" checked={m.enabled}
                      onChange={(e) => updateMap(i, { enabled: e.target.checked })} />
                    <span className="font-mono">{m.map}</span>
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-slate-400">min (0 = cold)</span>
                    <input type="number" min={0} max={m.max_replicas} step={1} value={m.min_replicas}
                      onChange={(e) => {
                        const v = parseInt(e.target.value, 10);
                        updateMap(i, { min_replicas: Number.isNaN(v) ? 0 : Math.max(0, Math.min(m.max_replicas, v)) });
                      }}
                      className="input-field w-24 font-mono" />
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-slate-400">max</span>
                    <input type="number" min={1} max={SCALE_MAX} step={1} value={m.max_replicas}
                      onChange={(e) => {
                        const v = parseInt(e.target.value, 10);
                        const max = Number.isNaN(v) ? 1 : Math.max(1, Math.min(SCALE_MAX, v));
                        updateMap(i, { max_replicas: max, min_replicas: Math.min(m.min_replicas, max) });
                      }}
                      className="input-field w-24 font-mono" />
                  </label>
                  <span className="text-xs text-slate-500 self-center">
                    {m.min_replicas === 0 ? "cold wake/sleep" : `warm floor ${m.min_replicas}`}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-primary" disabled={anyBusy} onClick={() => void saveConfig()}>
              {busy === "config" ? "saving…" : cfg.enabled ? "Arm autoscaler" : "Save (disarmed)"}
            </button>
            <button className="btn-ghost border border-slate-700" disabled={anyBusy} onClick={() => void tickNow()}>
              {busy === "tick" ? "running…" : "Run one tick now"}
            </button>
            <span className="text-xs text-slate-500">“Run one tick now” scales once immediately, ignoring the enabled flag.</span>
          </div>
        </div>
      </div>

      {/* Live per-map occupancy + scale state (polls every 15s). */}
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Live</h2>
          <span className="text-xs text-slate-500">players from farm_state · refreshes ~15s</span>
        </header>
        <div className="p-4">
          {!info?.live || Object.keys(info.live).length === 0 ? (
            <p className="text-xs text-slate-500">No live data.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-slate-500 text-left">
                <tr><th className="pb-1">Map</th><th className="pb-1">Players</th><th className="pb-1">Desired</th><th className="pb-1">Current</th><th className="pb-1">Status</th></tr>
              </thead>
              <tbody className="font-mono">
                {Object.entries(info.live).map(([mp, lv]) => (
                  <tr key={mp} className="border-t border-slate-800/60">
                    <td className="py-1 pr-3">{mp}</td>
                    <td className={"py-1 pr-3 " + ((lv.players ?? 0) > 0 ? "text-spice-300" : "text-slate-500")}>{lv.players ?? "—"}</td>
                    <td className="py-1 pr-3">{lv.desired ?? "—"}</td>
                    <td className="py-1 pr-3">{lv.current ?? "—"}</td>
                    <td className="py-1 text-slate-400">{lv.status ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {info?.sources && (info.sources.mockK8s === false || info.sources.players === false) && (
            <p className="text-xs text-amber-400 mt-2">
              {info.sources.mockK8s === false && "mock-k8s unreachable. "}
              {info.sources.players === false && "player count unreadable."}
            </p>
          )}
        </div>
      </div>

      {/* Recent scale actions (drains / wakes / load steps). */}
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Recent actions</h2>
        </header>
        <div className="p-4">
          {!info?.runs || info.runs.length === 0 ? (
            <p className="text-xs text-slate-500">No scale actions recorded yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-slate-500 text-left">
                <tr><th className="pb-1">When</th><th className="pb-1">Map</th><th className="pb-1">Action</th><th className="pb-1">Detail</th></tr>
              </thead>
              <tbody className="font-mono">
                {info.runs.map((r, i) => (
                  <tr key={i} className="border-t border-slate-800/60">
                    <td className="py-1 pr-3 whitespace-nowrap text-slate-400">{r.at}</td>
                    <td className="py-1 pr-3">{r.map}</td>
                    <td className={"py-1 pr-3 " + (r.action === "error" ? "text-red-400" : "text-spice-300")}>{r.action}</td>
                    <td className="py-1 text-slate-400">{r.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
