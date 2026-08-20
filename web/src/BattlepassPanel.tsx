// Battlepass (dune-admin port): the 188-tier progression pass. This panel
// is config + summary + the operator actions; the engine (state machine,
// grant ledger, offline-gated intel) is entirely server-side.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import {
  battlepassConfig,
  battlepassGrant,
  battlepassReseed,
  battlepassReset,
  battlepassTierUpdate,
  fetchBattlepass,
  fetchBattlepassTiers,
  type BattlepassSummary,
  type BattlepassTier,
} from "./api";
import { Confirm, pushToConsole, type ConsoleEntry } from "./components";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;
type ConfirmSpec = { title: string; message: string; confirmLabel: string; onConfirm: () => void };

export function BattlepassPanel({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [summary, setSummary] = useState<BattlepassSummary | null>(null);
  const [tiers, setTiers] = useState<BattlepassTier[] | null>(null);
  const [category, setCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [grantAccount, setGrantAccount] = useState("");
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  async function load() {
    const res = await fetchBattlepass().catch(() => null);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "config" in b) {
      setSummary(b as BattlepassSummary);
    }
  }

  async function loadTiers() {
    const res = await fetchBattlepassTiers().catch(() => null);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "tiers" in b) {
      setTiers((b as { tiers: BattlepassTier[] }).tiers ?? []);
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(label: string, fn: () => Promise<{ ok: boolean; body: unknown } | null>,
                     reloadTiers = false) {
    setBusy(true);
    const res = await fn().catch(() => null);
    setBusy(false);
    const rb = res?.body as { ok?: boolean; error?: string; detail?: string } | undefined;
    const ok = Boolean(res?.ok && rb?.ok);
    pushToConsole(setConsoleEntries, label,
      rb?.error ?? rb?.detail ?? (ok ? "ok" : "failed"), ok);
    void load();
    if (reloadTiers && tiers !== null) void loadTiers();
  }

  const cfg = summary?.config;
  const toggle = (key: "enabled" | "award_past" | "auto_grant", label: string) => (
    <label className="flex items-center gap-1.5 text-xs cursor-pointer" title={label}>
      <input type="checkbox" checked={cfg?.[key] ?? false} disabled={busy || !cfg}
        onChange={(e) => void run(`battlepass ${key}=${e.target.checked}`,
          () => battlepassConfig({ [key]: e.target.checked }))} />
      {key.replace("_", " ")}
    </label>
  );

  const categories = tiers ? [...new Set(tiers.map((t) => t.category))] : [];
  const visible = (tiers ?? []).filter((t) => !category || t.category === category);

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div>
            <h2 className="card-title">🎖 Battlepass</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Tiers earn when the engine WATCHES the progress happen (pre-existing progress
              is baseline, never paid — flip "award past" BEFORE a player's first scan to
              change that). Intel grants need the player offline; auto-grant retries until
              they log out.
            </p>
          </div>
          <div className="flex gap-3 items-center flex-wrap">
            {toggle("enabled", "master switch: run the scan at all")}
            {toggle("award_past", "first-scan progress earns instead of baselining — set BEFORE a player is ever scanned")}
            {toggle("auto_grant", "deliver earned tiers automatically (intel waits for the player to be offline)")}
            <button className="btn-ghost text-xs" onClick={() => void load()}>reload</button>
          </div>
        </header>
        <div className="card-body space-y-3 text-xs">
          {summary && (
            <div className="flex flex-wrap gap-x-6 gap-y-1">
              <span className="font-mono">{summary.tiers_enabled} tiers / {summary.intel_total} intel</span>
              <span>claims: <span className="font-mono">
                {Object.entries(summary.claims).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}
              </span></span>
              <span>delivery: <span className="font-mono">
                {Object.entries(summary.ledger).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}
              </span></span>
            </div>
          )}

          <div className="flex flex-wrap gap-2 items-center border-t border-slate-800 pt-2">
            <input className="input-field text-xs w-36" placeholder="account id"
              value={grantAccount} onChange={(e) => setGrantAccount(e.target.value)} />
            <button className="btn-ghost text-xs"
              disabled={busy || !/^\d+$/.test(grantAccount)}
              onClick={() => void run(`battlepass grant ${grantAccount}`,
                () => battlepassGrant(parseInt(grantAccount, 10)))}>
              Grant earned now
            </button>
            <button className="btn-ghost text-xs" disabled={busy}
              onClick={() => setConfirm({
                title: "Demote earned claims?",
                message: "Flip every EARNED claim to baseline: nothing left to deliver, nothing re-earns, granted history kept. The incident-recovery lever.",
                confirmLabel: "Demote",
                onConfirm: () => void run("battlepass reset demote",
                  () => battlepassReset("demote")),
              })}>
              Demote earned…
            </button>
            <button className="btn-ghost text-xs text-red-300" disabled={busy}
              onClick={() => setConfirm({
                title: "Purge claim history?",
                message: "Delete every claim AND its watch markers together — all progress re-baselines on the next scan (nobody gets re-paid). Granted delivery history is kept.",
                confirmLabel: "Purge",
                onConfirm: () => void run("battlepass reset purge",
                  () => battlepassReset("purge")),
              })}>
              Purge claims…
            </button>
            <button className="btn-ghost text-xs" disabled={busy}
              onClick={() => setConfirm({
                title: "Reseed the catalog?",
                message: "Replace every tier with the 188 defaults (operator edits lost). Claims survive — they re-attach by tier key.",
                confirmLabel: "Reseed",
                onConfirm: () => void run("battlepass reseed",
                  () => battlepassReseed(), true),
              })}>
              Reseed catalog…
            </button>
            <button className="btn-ghost text-xs"
              onClick={() => (tiers === null ? void loadTiers() : setTiers(null))}>
              {tiers === null ? "show tiers" : "hide tiers"}
            </button>
          </div>

          {tiers !== null && (
            <div className="space-y-2">
              <div className="flex gap-2 items-center">
                <select className="input-field text-xs py-0.5" value={category}
                  onChange={(e) => setCategory(e.target.value)}>
                  <option value="">all categories</option>
                  {categories.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <span className="text-slate-500">{visible.length} tier(s)</span>
              </div>
              <div className="overflow-x-auto max-h-96 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-slate-500">
                      <th className="pr-3 py-1">Tier</th>
                      <th className="pr-3">Signal</th>
                      <th className="pr-3">Intel</th>
                      <th className="pr-3">Items</th>
                      <th className="pr-3">On</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((t) => (
                      <tr key={t.id} className="border-t border-slate-900">
                        <td className="pr-3 py-0.5">{t.label}
                          <span className="text-slate-600 font-mono"> {t.tier_key}</span></td>
                        <td className="pr-3">{t.signal === "level"
                          ? `level ≥ ${t.threshold}` : t.signal_key}</td>
                        <td className="pr-3 font-mono">{t.intel}</td>
                        <td className="pr-3 font-mono">
                          {t.reward_items ? JSON.parse(t.reward_items).length : ""}
                        </td>
                        <td className="pr-3">
                          <input type="checkbox" checked={!!t.enabled} disabled={busy}
                            onChange={(e) => void run(
                              `battlepass tier #${t.id} ${e.target.checked ? "on" : "off"}`,
                              () => battlepassTierUpdate(t.id, { enabled: e.target.checked }),
                              true)} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
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
