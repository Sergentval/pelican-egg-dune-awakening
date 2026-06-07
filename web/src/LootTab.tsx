// Loot & Difficulty tab: a curated control panel for everything loot-related.
//
//   • "General loot" — the loot-authoritative server settings (NPC drop
//     behaviour, player-death loot, double-difficulty loot, the loot
//     difficulty/quality caps, loot-rights behaviour, post-sandworm-death
//     items). These are schema-backed settings: they write to the server
//     INI and take effect on the next RESTART (same flow as the Settings
//     tab). Most are mapped-but-not-yet-game-verified — flagged "candidate".
//   (Spice economy moved to its own "Spice Economy" tab in IA phase 4.)
//
// There is intentionally NO loot-quantity multiplier here: the engine
// exposes loot as toggles + difficulty/quality caps, not a global "× loot"
// cvar (only mining/vehicle output have multipliers). Don't add a fake one.
//
// The per-setting input widget mirrors SettingsTab.renderInput in tabs.tsx.
// It is kept inline (not shared) to avoid editing the 134KB tabs.tsx for a
// ~30-line widget; if a third curated settings view appears, extract it.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { pushToConsole, type ConsoleEntry } from "./components";
import {
  fetchSettings,
  saveSettings,
  type SettingItem,
  type SettingsResponse,
  type SettingsSaveResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

const LOOT_CATEGORY = "Loot";

export function LootTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [items, setItems] = useState<SettingItem[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  async function refresh() {
    setLoading(true);
    const res = await fetchSettings();
    setLoading(false);
    if (res.ok) {
      const resp = res.body as SettingsResponse;
      setItems(resp.categories[LOOT_CATEGORY] ?? []);
      setEdits({});
    } else {
      pushToConsole(setConsoleEntries, "GET /api/settings", JSON.stringify(res.body), false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  const dirty = Object.keys(edits).length;

  async function apply() {
    if (!dirty) return;
    setSaving(true);
    const res = await saveSettings(edits);
    setSaving(false);
    const b = res.body as SettingsSaveResult;
    const ok = res.ok && b.ok;
    const lines = [`applied: ${(b.applied || []).join(", ") || "none"}`];
    if ((b.errors || []).length) lines.push("errors: " + b.errors.map((e) => `${e.id}: ${e.error}`).join("; "));
    if (b.restartRequired) lines.push("⚠ restart the server for changes to take effect");
    pushToConsole(setConsoleEntries, `loot settings apply (${dirty})`, lines.join("\n"), ok);
    void refresh();
  }

  function renderInput(s: SettingItem) {
    const cur = edits[s.id] ?? (s.value ?? "");
    const onChange = (v: string) => setEdits((p) => ({ ...p, [s.id]: v }));
    if (s.advanced) {
      // struct/array values (e.g. post-sandworm-death items) are written verbatim.
      return (
        <input
          className="input-field text-xs font-mono w-full"
          value={cur}
          placeholder={s.default || "single-line value; multi-line arrays: edit UserGame.ini directly"}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    }
    if (s.type === "bool" || s.type === "cvarbool") {
      return (
        <select className="input-field text-xs" value={cur} onChange={(e) => onChange(e.target.value)}>
          <option value="">(unset)</option>
          <option value="1">on</option>
          <option value="0">off</option>
        </select>
      );
    }
    if (s.type === "enum" && s.enum) {
      return (
        <select className="input-field text-xs" value={cur} onChange={(e) => onChange(e.target.value)}>
          <option value="">(unset)</option>
          {s.enum.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    }
    return (
      <input
        className="input-field text-xs font-mono"
        value={cur}
        inputMode={s.type === "int" || s.type === "float" ? "decimal" : "text"}
        placeholder={s.default ?? ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="card">
        <header className="card-header">
          <div className="flex items-center gap-2">
            <h2 className="font-semibold">Loot &amp; Difficulty</h2>
            <span className="text-xs text-slate-500">{items.length} settings</span>
          </div>
          <div className="flex gap-2">
            <button className="btn-ghost text-xs" onClick={() => void refresh()} disabled={loading}>
              {loading ? "…" : "reload"}
            </button>
            <button className="btn-primary text-xs" onClick={() => void apply()} disabled={!dirty || saving}>
              {saving ? "saving…" : `apply${dirty ? ` (${dirty})` : ""}`}
            </button>
          </div>
        </header>
        <p className="p-4 pb-2 text-xs text-slate-400">
          Loot drop behaviour, death-loot, and the difficulty/quality caps. Edits write to the server INI and take effect on the
          next <span className="font-mono text-slate-300">restart</span>. A{" "}
          <span className="pill-warn text-[10px]">candidate</span> tag means the mapping isn't game-verified yet;{" "}
          <span className="pill-warn text-[10px]">⚠ client-side</span> means clients clamp to their own default unless each player
          also edits their <span className="font-mono text-slate-300">Game.ini</span>. There is no loot-quantity multiplier — the
          engine only exposes toggles + caps.
        </p>
        {items.length === 0 ? (
          <p className="px-4 pb-4 text-sm text-slate-500 italic">{loading ? "loading…" : "no loot settings found"}</p>
        ) : (
          <div className="divide-y divide-slate-900">
            {items.map((s) => (
              <div key={s.id} className="px-4 py-2">
                <div className="flex items-center gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm flex items-center gap-2 flex-wrap">
                      {s.label}
                      {!s.verified && <span className="pill-warn text-[10px]">candidate</span>}
                      {!s.isDefault && <span className="pill-ok text-[10px]">set</span>}
                      {s.clientGated && (
                        <span
                          className="pill-warn text-[10px]"
                          title="The server applies this, but each client clamps to its own default unless the player also edits %localappdata%\DuneSandbox\Saved\Config\WindowsClient\Game.ini"
                        >
                          ⚠ client-side
                        </span>
                      )}
                      {s.advanced && <span className="text-[10px] text-amber-400/80">advanced</span>}
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono truncate">
                      {s.section ? `${s.section.split(".").pop()} · ` : ""}{s.key} · {s.type}
                      {s.default !== null ? ` · default ${s.default || '""'}` : ""}
                    </div>
                  </div>
                  {!s.advanced && <div className="w-40 shrink-0">{renderInput(s)}</div>}
                </div>
                {s.advanced && <div className="mt-1.5">{renderInput(s)}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
