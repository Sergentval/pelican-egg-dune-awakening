// Scheduler tab: auto-restart + auto-backup config editor, manual "run now"
// triggers, existing-backup list, and run history. Backed by /api/schedule,
// /api/tasks/trigger/<task>, and /api/database/backups.

import { type Dispatch, type SetStateAction, useCallback, useEffect, useState } from "react";
import {
  fetchBackups,
  fetchSchedule,
  saveSchedule,
  triggerTask,
  triggerTaskById,
  type BackupTable,
  type TriggerResult,
  type ScheduleBackup,
  type ScheduleConfig,
  type ScheduledTask,
  type ScheduleResponse,
  type ScheduleRestart,
  type TaskRun,
} from "./api";
import { pushToConsole, type ConsoleEntry } from "./components";
import { TaskEditor, newTask, taskScheduleSummary, taskParamsSummary } from "./SchedulerTaskEditor";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export function SchedulerTab({ setConsoleEntries }: { setConsoleEntries: Dispatch<SetStateAction<ConsoleEntry[]>> }) {
  const [resp, setResp] = useState<ScheduleResponse | null>(null);
  const [cfg, setCfg] = useState<ScheduleConfig | null>(null);
  const [backups, setBackups] = useState<BackupTable | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ task: ScheduledTask; isNew: boolean } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const res = await fetchSchedule();
    setLoading(false);
    if (res.ok) {
      const b = res.body as ScheduleResponse;
      setResp(b);
      setCfg(JSON.parse(JSON.stringify(b.config)) as ScheduleConfig);
    }
    const bk = await fetchBackups();
    if (bk.ok) setBackups(bk.body as BackupTable);
  }, []);

  useEffect(() => { load(); }, [load]);

  const setR = (p: Partial<ScheduleRestart>) =>
    setCfg((c) => (c ? { ...c, restart: { ...c.restart, ...p } } : c));
  const setB = (p: Partial<ScheduleBackup>) =>
    setCfg((c) => (c ? { ...c, backup: { ...c.backup, ...p } } : c));

  async function persist(next: ScheduleConfig, label: string): Promise<boolean> {
    setSaving(true);
    const res = await saveSchedule(next);
    setSaving(false);
    const ok = res.ok && (res.body as { ok?: boolean }).ok !== false;
    pushToConsole(setConsoleEntries, label,
      ok ? "saved" : ((res.body as { error?: string }).error || "save failed"), ok);
    if (ok) load();
    return ok;
  }

  function save() {
    if (cfg) void persist(cfg, "save schedule");
  }

  // Task ops persist the full config (restart+backup+tasks) immediately so an
  // add/remove/toggle takes effect without a separate save click. cfg already
  // carries the saved restart/backup unless the user is mid-editing them.
  function saveTask(task: ScheduledTask, isNew: boolean) {
    if (!cfg) return;
    if (isNew && cfg.tasks.some((t) => t.id === task.id)) {
      pushToConsole(setConsoleEntries, "add task", `id "${task.id}" already exists`, false);
      return;
    }
    const tasks = isNew
      ? [...cfg.tasks, task]
      : cfg.tasks.map((t) => (t.id === task.id ? task : t));
    void persist({ ...cfg, tasks }, `${isNew ? "add" : "update"} task ${task.id}`).then((ok) => {
      if (ok) setEditor(null);
    });
  }

  function removeTask(id: string) {
    if (!cfg || !window.confirm(`Remove scheduled task "${id}"?`)) return;
    void persist({ ...cfg, tasks: cfg.tasks.filter((t) => t.id !== id) }, `remove task ${id}`);
  }

  function toggleTask(id: string) {
    if (!cfg) return;
    void persist({ ...cfg, tasks: cfg.tasks.map((t) => (t.id === id ? { ...t, enabled: !t.enabled } : t)) },
      `toggle task ${id}`);
  }

  function triggerMessage(body: unknown, ok: boolean): string {
    const b = (body ?? {}) as TriggerResult;
    return b.detail || b.error || (ok ? "done" : "failed");
  }

  async function runTaskNow(id: string) {
    setBusy(true);
    const res = await triggerTaskById(id);
    setBusy(false);
    const ok = res.ok && (res.body as TriggerResult).ok !== false;
    pushToConsole(setConsoleEntries, `run task ${id} now`, triggerMessage(res.body, ok), ok);
    load();
  }

  async function run(task: "backup" | "restart") {
    if (task === "restart" &&
        !window.confirm("Trigger a restart now? Players get the in-game countdown, then the server restarts.")) return;
    setBusy(true);
    const res = await triggerTask(task);
    setBusy(false);
    const ok = res.ok && (res.body as TriggerResult).ok !== false;
    pushToConsole(setConsoleEntries, `run ${task} now`, triggerMessage(res.body, ok), ok);
    load();
  }

  const r = cfg?.restart;
  const b = cfg?.backup;
  const runs: TaskRun[] = resp?.runs || [];
  const backupRows = backups?.rows || [];
  const restartConfigured = resp?.restart_configured ?? false;

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Scheduler</h2>
          <div className="flex gap-2">
            <button className="btn-ghost text-xs" onClick={load} disabled={loading}>{loading ? "…" : "reload"}</button>
            <button className="btn-primary text-xs" onClick={save} disabled={!cfg || saving}>{saving ? "saving…" : "save schedule"}</button>
          </div>
        </header>
        <p className="p-4 pb-0 text-xs text-slate-400">
          Unattended auto-restart + auto-backup. OFF by default. Times are UTC. Changes take effect on the next tick
          (~30s) after saving.
        </p>
        {!cfg && <p className="p-4 text-sm text-slate-500 italic">loading…</p>}

        {r && b && (
          <div className="p-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Restart */}
            <div className="rounded border border-slate-800 p-3 space-y-3">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input type="checkbox" checked={r.enabled} onChange={(e) => setR({ enabled: e.target.checked })} />
                Auto-restart
              </label>
              {!restartConfigured && (
                <p className="text-[11px] text-amber-400/90">
                  ⚠ Requires a Pelican client key: set <span className="font-mono">DUNE_PELICAN_URL</span>,{" "}
                  <span className="font-mono">DUNE_PELICAN_CLIENT_KEY</span> (ptlc_…), and{" "}
                  <span className="font-mono">DUNE_PELICAN_SERVER_ID</span> as panel variables. Without them the restart
                  task self-skips.
                </p>
              )}
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-400 w-28">Time (UTC HH:MM)</span>
                <input className="input-field text-xs w-24" value={r.time} onChange={(e) => setR({ time: e.target.value })} placeholder="08:00" />
              </div>
              <div className="text-xs">
                <div className="text-slate-400 mb-1">Days</div>
                <div className="flex flex-wrap gap-1">
                  {DAYS.map((d) => {
                    const on = r.days.includes(d);
                    return (
                      <button key={d} type="button"
                        onClick={() => setR({ days: on ? r.days.filter((x) => x !== d) : [...r.days, d] })}
                        className={"px-2 py-0.5 rounded text-[11px] " + (on ? "bg-spice-900/50 text-spice-200" : "text-slate-500 bg-slate-900")}>
                        {d}
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-400 w-28">Warn lead (s)</span>
                <input type="number" min={0} className="input-field text-xs w-24" value={r.warn_lead_secs}
                  onChange={(e) => setR({ warn_lead_secs: parseInt(e.target.value) || 0 })} />
              </div>
              <button className="btn-ghost text-xs border border-slate-700" onClick={() => run("restart")} disabled={busy || !restartConfigured}>
                run restart now
              </button>
            </div>

            {/* Backup */}
            <div className="rounded border border-slate-800 p-3 space-y-3">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input type="checkbox" checked={b.enabled} onChange={(e) => setB({ enabled: e.target.checked })} />
                Auto-backup (pg_dump of the dune DB)
              </label>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-400 w-28">Every (hours)</span>
                <input type="number" min={1} className="input-field text-xs w-24" value={b.every_hours}
                  onChange={(e) => setB({ every_hours: parseInt(e.target.value) || 1 })} />
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-slate-400 w-28">Keep (retention)</span>
                <input type="number" min={1} className="input-field text-xs w-24" value={b.retention}
                  onChange={(e) => setB({ retention: parseInt(e.target.value) || 1 })} />
              </div>
              <p className="text-[11px] text-slate-500">Dumps to <span className="font-mono text-slate-400">/home/container/backups</span> (downloadable in the panel file manager). Restore is CLI-only (destructive).</p>
              <button className="btn-ghost text-xs border border-slate-700" onClick={() => run("backup")} disabled={busy}>
                run backup now
              </button>
            </div>
          </div>
        )}
      </div>

      {/* scheduled tasks */}
      <div className="card">
        <header className="card-header">
          <div>
            <h3 className="font-semibold text-sm">Scheduled tasks</h3>
            <p className="text-[11px] text-slate-500 mt-0.5">
              Recurring broadcasts + time-based instance scaling. Apply on the next tick (~30s). Runs land in the history below.
            </p>
          </div>
          <button className="btn-primary text-xs" disabled={!cfg || saving}
            onClick={() => setEditor({ task: newTask(), isNew: true })}>+ add task</button>
        </header>
        <div className="divide-y divide-slate-900">
          {(cfg?.tasks || []).length === 0 && (
            <p className="px-4 py-3 text-sm text-slate-500 italic">
              No scheduled tasks. Add one to broadcast a message or scale a map on a schedule.
            </p>
          )}
          {(cfg?.tasks || []).map((task: ScheduledTask) => (
            <div key={task.id} className="px-4 py-2 flex items-center gap-3 text-xs">
              <button className={task.enabled ? "pill-ok" : "pill-err"} disabled={saving}
                onClick={() => toggleTask(task.id)} title="toggle enabled">
                {task.enabled ? "on" : "off"}
              </button>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-slate-200">{task.id}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">{task.type}</span>
                </div>
                <div className="text-[10px] text-slate-500 truncate">
                  {taskScheduleSummary(task.schedule)} · {taskParamsSummary(task)}
                </div>
              </div>
              <button className="btn-ghost text-xs border border-slate-700" disabled={busy}
                onClick={() => void runTaskNow(task.id)} title="run now (ignores schedule + enabled)">run now</button>
              <button className="btn-ghost text-xs" disabled={saving}
                onClick={() => setEditor({ task, isNew: false })}>edit</button>
              <button className="btn-ghost text-xs text-red-300" disabled={saving}
                onClick={() => removeTask(task.id)}>remove</button>
            </div>
          ))}
        </div>
      </div>

      {editor && (
        <TaskEditor initial={editor.task} isNew={editor.isNew} busy={saving}
          onSave={(task) => saveTask(task, editor.isNew)} onClose={() => setEditor(null)} />
      )}

      {/* existing backups */}
      <div className="card">
        <header className="card-header">
          <h3 className="font-semibold text-sm">Backups</h3>
          <span className="text-xs text-slate-500">{backupRows.length}</span>
        </header>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-slate-500 border-b border-slate-800">
              <tr><th className="py-1 px-3">File</th><th className="px-3">Size</th><th className="px-3">When (UTC)</th></tr>
            </thead>
            <tbody>
              {backupRows.length === 0 && <tr><td className="px-3 py-2 text-slate-500 italic" colSpan={3}>No backups yet.</td></tr>}
              {backupRows.map((row, i) => (
                <tr key={i} className="border-b border-slate-900">
                  <td className="py-1.5 px-3 font-mono">{row[0]}</td>
                  <td className="px-3 text-slate-400">{(Number(row[1]) / 1024 / 1024).toFixed(2)} MB</td>
                  <td className="px-3 text-slate-400">{(row[2] || "").replace("T", " ").replace("Z", "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* run history */}
      <div className="card">
        <header className="card-header">
          <h3 className="font-semibold text-sm">Run history</h3>
          <span className="text-xs text-slate-500">{runs.length}</span>
        </header>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-slate-500 border-b border-slate-800">
              <tr><th className="py-1 px-3">Task</th><th className="px-3">Status</th><th className="px-3">When (UTC)</th><th className="px-3">Detail</th></tr>
            </thead>
            <tbody>
              {runs.length === 0 && <tr><td className="px-3 py-2 text-slate-500 italic" colSpan={4}>No runs recorded yet.</td></tr>}
              {runs.map((run_, i) => (
                <tr key={i} className="border-b border-slate-900">
                  <td className="py-1.5 px-3 font-mono">{run_.task}</td>
                  <td className="px-3"><span className={run_.status === "ok" ? "pill-ok" : run_.status === "skipped" ? "pill-warn" : "pill-err"}>{run_.status}</span></td>
                  <td className="px-3 text-slate-400">{(run_.at || "").replace("T", " ").replace("Z", "")}</td>
                  <td className="px-3 text-slate-500 truncate max-w-[20rem]">{run_.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
