// Coriolis seed control (feature "Coriolis seed control panel").
//
// The Deep Desert is one of 12 fixed layouts chosen by a Coriolis world seed
// (0-11) that normally rotates weekly. `m_ForcedCoriolisWorldSeed` in
// UserOverrides.ini pins one layout; -1 restores the weekly rotation. That
// setting is already writable through the generic Settings tab, but there it's
// a bare integer with no hint of what each seed contains. This control turns it
// into an informed choice: it shows the effective override, previews every
// layout's POI composition (from the same Wick Maps catalogue the grid draws),
// and writes the pick through the validated settings path.
//
// The change is a config write, not a live edit — it takes effect only when the
// Deep Desert is next regenerated (cycle end / DB wipe), never on the running
// map. The copy says so plainly, and a pick is a two-step (select → Apply) so a
// stray click can't repin the world.

import { useState } from "react";
import {
  setForcedCoriolisSeed,
  type DeepDesertLayout,
  type SettingsSaveResult,
  type WickSummary,
} from "./api";

const SEEDS = Array.from({ length: 12 }, (_, i) => i);

function iconSrc(type: string): string {
  return `/wickmaps/${type}.${type === "taxi-service" ? "png" : "svg"}`;
}

function SummaryPreview({ s }: { s: WickSummary | undefined }) {
  if (!s) {
    return <p className="text-slate-600">No catalogued POIs for this seed yet.</p>;
  }
  return (
    <div className="space-y-1">
      <div className="text-slate-400">
        {s.poiCount} POIs · {s.spiceSectors} large-spice sectors
        {s.confidence ? ` · ${s.confidence.toLowerCase()}` : ""}
      </div>
      {s.legend.length > 0 && (
        <div className="flex flex-wrap gap-x-2 gap-y-1 text-slate-400">
          {s.legend.map((l) => (
            <span key={l.type} className="inline-flex items-center gap-1">
              <img src={iconSrc(l.type)} alt="" className="w-3 h-3" />
              {l.label} {l.count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function DeepDesertSeedControl({ dd, onChanged }: {
  dd: DeepDesertLayout;
  onChanged: () => void;
}) {
  const forced = dd.forcedSeed ?? -1;
  const summaries = dd.summaries ?? [];
  const summaryFor = (seed: number) => summaries.find((s) => s.seed === seed);

  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function apply(seed: number) {
    setBusy(true);
    setMsg(null);
    try {
      const res = await setForcedCoriolisSeed(seed);
      const body = res.body as SettingsSaveResult;
      if (res.ok && body.applied?.length) {
        setMsg({
          ok: true,
          text: seed < 0
            ? "Rotation restored — a fresh layout each cycle."
            : `Forced to seed ${seed} — applies at the next Deep Desert regeneration.`,
        });
        setPending(null);
        onChanged();
      } else {
        const err = body.errors?.[0]?.error
          ?? (res.body as { error?: string }).error
          ?? "write rejected";
        setMsg({ ok: false, text: err });
      }
    } catch {
      setMsg({ ok: false, text: "request failed" });
    } finally {
      setBusy(false);
    }
  }

  const label = (seed: number) => (seed < 0 ? "Auto" : String(seed));

  return (
    <div className="space-y-2 border-t border-slate-700/60 pt-2">
      <div className="flex items-center justify-between">
        <span className="text-slate-400">Forced seed</span>
        <span className="font-mono">
          {forced < 0
            ? <span className="text-amber-400">automatic (weekly)</span>
            : <span className="text-spice-300">seed {forced}
                {dd.forcedSeedExplicit ? "" : " (default)"}</span>}
        </span>
      </div>

      <button
        type="button"
        className="text-[11px] text-slate-400 hover:text-slate-200"
        onClick={() => { setOpen((v) => !v); setPending(null); setMsg(null); }}
      >
        {open ? "▾ hide seed picker" : "▸ change forced seed"}
      </button>

      {open && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1">
            {[-1, ...SEEDS].map((seed) => {
              const isForced = seed === forced;
              const isPending = seed === pending;
              return (
                <button
                  key={seed}
                  type="button"
                  disabled={busy}
                  onClick={() => setPending(isPending ? null : seed)}
                  title={seed < 0 ? "Automatic weekly rotation" : `Seed ${seed}`}
                  className={[
                    "w-8 h-8 rounded font-mono text-[11px] border transition-colors",
                    isPending
                      ? "border-spice-400 bg-spice-500/25 text-spice-200"
                      : isForced
                        ? "border-spice-500/60 bg-spice-500/10 text-spice-300"
                        : "border-slate-700 bg-slate-800/50 text-slate-300 hover:border-slate-500",
                  ].join(" ")}
                >
                  {label(seed)}
                </button>
              );
            })}
          </div>

          {pending !== null && (
            <div className="card bg-slate-900/60 p-2 space-y-2 text-[11px]">
              <div className="font-medium text-slate-300">
                {pending < 0 ? "Automatic weekly rotation" : `Seed ${pending}`}
              </div>
              {pending >= 0 && <SummaryPreview s={summaryFor(pending)} />}
              <p className="text-[10px] text-slate-500 leading-tight">
                Applies at the next Deep Desert regeneration (cycle end / DB
                wipe) — not to the current map.
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busy || pending === forced}
                  className="btn-primary text-[11px] px-2 py-1 disabled:opacity-40"
                  onClick={() => apply(pending)}
                >
                  {busy ? "applying…" : pending === forced ? "already set" : "Apply"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  className="btn-ghost text-[11px] px-2 py-1"
                  onClick={() => setPending(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {msg && (
        <p className={`text-[11px] ${msg.ok ? "text-emerald-400" : "text-red-400"}`}>
          {msg.text}
        </p>
      )}
    </div>
  );
}
