// Scheduled-task editor modal + summary helpers for the Scheduler tab.
//
// A task = {id, type, enabled, schedule, params} and mirrors admin_schedule.py's
// validate_task. Two types: "broadcast" (title/body/duration) and
// "scale-instance" (map/replicas/force). Schedule is "daily" (time + days) or
// "interval" (every_minutes). Validation here mirrors the backend; the server
// re-validates on save, so this is UX-only.

import { useState } from "react";
import {
  SCALABLE_MAPS,
  SCALE_REPLICAS_MAX,
  type ScheduledTask,
  type TaskSchedule,
} from "./api";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export function newTask(): ScheduledTask {
  return {
    id: "",
    type: "broadcast",
    enabled: true,
    schedule: { kind: "daily", time: "08:00", days: [...DAYS] },
    params: { title: "", body: "", duration: 30 },
  };
}

function defaultParams(type: ScheduledTask["type"]): ScheduledTask["params"] {
  return type === "broadcast"
    ? { title: "", body: "", duration: 30 }
    : { map: SCALABLE_MAPS[0], replicas: 1, force: false };
}

export function taskScheduleSummary(s: TaskSchedule): string {
  if (s.kind === "interval") return `every ${s.every_minutes ?? "?"}m`;
  const days = (s.days || []).length === 7 ? "daily" : (s.days || []).join(",");
  return `${s.time ?? "??:??"} UTC · ${days}`;
}

export function taskParamsSummary(t: ScheduledTask): string {
  if (t.type === "broadcast") return `“${t.params.title ?? ""}” (${t.params.duration ?? 30}s)`;
  return `${t.params.map ?? "?"} → ${t.params.replicas ?? "?"} replica(s)${t.params.force ? " · force" : ""}`;
}

export function TaskEditor({ initial, isNew, busy, onSave, onClose }: {
  initial: ScheduledTask;
  isNew: boolean;
  busy: boolean;
  onSave: (task: ScheduledTask) => void;
  onClose: () => void;
}) {
  const [t, setT] = useState<ScheduledTask>(() => JSON.parse(JSON.stringify(initial)) as ScheduledTask);
  const [err, setErr] = useState("");

  const setSched = (p: Partial<TaskSchedule>) => setT((c) => ({ ...c, schedule: { ...c.schedule, ...p } }));
  const setParam = (k: string, v: unknown) => setT((c) => ({ ...c, params: { ...c.params, [k]: v } }));

  function changeType(type: ScheduledTask["type"]) {
    setT((c) => ({ ...c, type, params: defaultParams(type) }));
  }

  function submit() {
    const id = t.id.trim();
    if (!ID_RE.test(id)) {
      setErr("id: lowercase letters/digits/-/_ , start alphanumeric, ≤64 chars");
      return;
    }
    if (t.schedule.kind === "daily") {
      if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(t.schedule.time || "")) { setErr("time must be HH:MM (00:00–23:59)"); return; }
      if (!(t.schedule.days || []).length) { setErr("pick at least one day"); return; }
    } else if (!(Number(t.schedule.every_minutes) >= 1)) {
      setErr("every_minutes must be ≥ 1");
      return;
    }
    if (t.type === "broadcast") {
      if (!String(t.params.title || "").trim() || !String(t.params.body || "").trim()) {
        setErr("broadcast needs a title and body");
        return;
      }
    } else {
      if (!SCALABLE_MAPS.includes(t.params.map as (typeof SCALABLE_MAPS)[number])) { setErr("pick a scalable map"); return; }
      const r = Number(t.params.replicas);
      if (!Number.isInteger(r) || r < 0 || r > SCALE_REPLICAS_MAX) { setErr(`replicas must be 0-${SCALE_REPLICAS_MAX}`); return; }
    }
    onSave({ ...t, id });
  }

  return (
    <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="card max-w-lg w-full max-h-[88vh] flex flex-col">
        <header className="card-header">
          <h3 className="font-semibold text-spice-300">{isNew ? "Add scheduled task" : `Edit task ${t.id}`}</h3>
          <button className="btn-ghost text-xs" onClick={onClose} disabled={busy}>close</button>
        </header>

        <div className="p-4 space-y-3 overflow-y-auto text-sm">
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="label">Task id</span>
              <input className="input-field w-full text-xs font-mono" value={t.id} disabled={!isNew}
                onChange={(e) => setT((c) => ({ ...c, id: e.target.value }))} placeholder="dd-reset-warning" />
            </label>
            <label className="block">
              <span className="label">Type</span>
              <select className="input-field w-full text-xs" value={t.type}
                onChange={(e) => changeType(e.target.value as ScheduledTask["type"])}>
                <option value="broadcast">broadcast (in-game message)</option>
                <option value="scale-instance">scale-instance (map replicas)</option>
              </select>
            </label>
          </div>

          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={t.enabled} onChange={(e) => setT((c) => ({ ...c, enabled: e.target.checked }))} />
            enabled
          </label>

          {/* Schedule */}
          <div className="rounded border border-slate-800 p-3 space-y-2">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-slate-400 w-20">Schedule</span>
              <select className="input-field text-xs" value={t.schedule.kind}
                onChange={(e) => setSched(e.target.value === "daily"
                  ? { kind: "daily", time: t.schedule.time || "08:00", days: t.schedule.days || [...DAYS] }
                  : { kind: "interval", every_minutes: t.schedule.every_minutes || 60 })}>
                <option value="daily">daily (time + days)</option>
                <option value="interval">interval (every N min)</option>
              </select>
            </div>
            {t.schedule.kind === "daily" ? (
              <>
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-slate-400 w-20">Time (UTC)</span>
                  <input className="input-field text-xs w-24" value={t.schedule.time || ""}
                    onChange={(e) => setSched({ time: e.target.value })} placeholder="18:00" />
                </div>
                <div className="text-xs">
                  <div className="text-slate-400 mb-1">Days</div>
                  <div className="flex flex-wrap gap-1">
                    {DAYS.map((d) => {
                      const on = (t.schedule.days || []).includes(d);
                      return (
                        <button key={d} type="button"
                          onClick={() => setSched({ days: on ? (t.schedule.days || []).filter((x) => x !== d) : [...(t.schedule.days || []), d] })}
                          className={"px-2 py-0.5 rounded text-[11px] " + (on ? "bg-spice-900/50 text-spice-200" : "text-slate-500 bg-slate-900")}>
                          {d}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </>
            ) : (
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-400 w-20">Every (min)</span>
                <input type="number" min={1} className="input-field text-xs w-24" value={t.schedule.every_minutes ?? 60}
                  onChange={(e) => setSched({ every_minutes: parseInt(e.target.value) || 1 })} />
              </div>
            )}
          </div>

          {/* Params */}
          <div className="rounded border border-slate-800 p-3 space-y-2">
            {t.type === "broadcast" ? (
              <>
                <label className="block">
                  <span className="label">Title</span>
                  <input className="input-field w-full text-xs" value={String(t.params.title || "")} maxLength={200}
                    onChange={(e) => setParam("title", e.target.value)} placeholder="Server notice" />
                </label>
                <label className="block">
                  <span className="label">Body</span>
                  <input className="input-field w-full text-xs" value={String(t.params.body || "")} maxLength={500}
                    onChange={(e) => setParam("body", e.target.value)} placeholder="Deep Desert resets in 1 hour" />
                </label>
                <label className="flex items-center gap-2 text-xs">
                  <span className="text-slate-400">Duration (s)</span>
                  <input type="number" min={1} className="input-field text-xs w-20" value={Number(t.params.duration ?? 30)}
                    onChange={(e) => setParam("duration", parseInt(e.target.value) || 1)} />
                </label>
              </>
            ) : (
              <>
                <label className="block">
                  <span className="label">Map</span>
                  <select className="input-field w-full text-xs" value={String(t.params.map || SCALABLE_MAPS[0])}
                    onChange={(e) => setParam("map", e.target.value)}>
                    {SCALABLE_MAPS.map((m) => <option key={m} value={m}>{m}</option>)}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-xs">
                  <span className="text-slate-400 w-20">Replicas</span>
                  <input type="number" min={0} max={SCALE_REPLICAS_MAX} className="input-field text-xs w-20"
                    value={Number(t.params.replicas ?? 0)} onChange={(e) => setParam("replicas", parseInt(e.target.value))} />
                  <span className="text-[10px] text-slate-500">0 = idle · max {SCALE_REPLICAS_MAX}</span>
                </label>
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={Boolean(t.params.force)} onChange={(e) => setParam("force", e.target.checked)} />
                  force (scale down even with players online — drops them)
                </label>
              </>
            )}
          </div>

          {err && <p className="text-xs text-red-400">{err}</p>}
        </div>

        <div className="px-4 py-3 border-t border-slate-800 flex items-center gap-2">
          <button className="btn-primary" disabled={busy} onClick={submit}>{busy ? "saving…" : (isNew ? "add task" : "save task")}</button>
          <button className="btn-ghost" onClick={onClose} disabled={busy}>cancel</button>
        </div>
      </div>
    </div>
  );
}
