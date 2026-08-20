// Player Editor tab: a point-and-click surface over the dedicated per-player
// DB-write routes (POST /api/players/<id>/<action>) that previously had no UI —
// currency, character XP, keystones, rename, tags, faction rep + tier,
// journey-progression presets, and the offline-gated destructive writes
// (reset-spec, account-delete). Distinct from the Skills tab, which drives the
// generic RMQ publish path. The server enforces the offline gate per write.

import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { Confirm, PlayerPicker, pushToConsole, type ConsoleEntry } from "./components";
import { useTarget } from "./target";
import {
  charBackupDelete,
  charBackupRestore,
  fetchCharBackups,
  fetchPlayerSummary,
  fetchProgressionPresets,
  playerWrite,
  type CharBackup,
  type PlayerSummary,
  type ProgressionPreset,
  type PublishResult,
} from "./api";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;
type ConfirmSpec = { title: string; message: string; confirmLabel: string; onConfirm: () => void };

function Section({ title, hint, danger, children }: {
  title: string;
  hint?: string;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={"card" + (danger ? " border-red-900/60" : "")}>
      <header className="card-header">
        <div>
          <h2 className={"font-semibold" + (danger ? " text-red-300" : "")}>{title}</h2>
          {hint && <p className="text-xs text-slate-500 mt-0.5">{hint}</p>}
        </div>
      </header>
      <div className="p-4 space-y-4">{children}</div>
    </div>
  );
}

const csvToList = (s: string): string[] => s.split(",").map((t) => t.trim()).filter(Boolean);

export function PlayerEditorTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const target = useTarget();
  const pid = target.playerId;

  const [busy, setBusy] = useState("");
  const [currency, setCurrency] = useState(1000);
  const [xp, setXp] = useState(5000);
  const [name, setName] = useState("");
  const [tagsAdd, setTagsAdd] = useState("");
  const [tagsRemove, setTagsRemove] = useState("");
  const [faction, setFaction] = useState<"atreides" | "harkonnen">("atreides");
  const [rep, setRep] = useState(500);
  const [tier, setTier] = useState(5);
  const [presets, setPresets] = useState<ProgressionPreset[]>([]);
  const [preset, setPreset] = useState("");
  const [delConfirm, setDelConfirm] = useState("");
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [summary, setSummary] = useState<PlayerSummary | null>(null);
  const [sumLoading, setSumLoading] = useState(false);
  const [backups, setBackups] = useState<CharBackup[]>([]);
  const [bkLoading, setBkLoading] = useState(false);

  useEffect(() => {
    fetchProgressionPresets().then((res) => {
      const body: unknown = res.body;
      if (res.ok && typeof body === "object" && body !== null && "presets" in body) {
        const list = (body as { presets: ProgressionPreset[] }).presets;
        if (list.length) {
          setPresets(list);
          setPreset(list[0].id);
        }
      }
    });
  }, []);

  async function loadSummary() {
    if (!pid) {
      setSummary(null);
      return;
    }
    setSumLoading(true);
    const res = await fetchPlayerSummary(pid).catch(() => null);
    setSumLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "ok" in b && (b as PlayerSummary).ok) {
      setSummary(b as PlayerSummary);
    } else {
      setSummary(null);
    }
  }

  async function loadBackups() {
    if (!pid) {
      setBackups([]);
      return;
    }
    setBkLoading(true);
    const res = await fetchCharBackups(pid).catch(() => null);
    setBkLoading(false);
    const b: unknown = res?.body;
    if (res?.ok && typeof b === "object" && b !== null && "backups" in b) {
      setBackups((b as { backups: CharBackup[] }).backups ?? []);
    } else {
      setBackups([]);
    }
  }

  // Reload the current-state panel whenever the target player changes.
  useEffect(() => {
    void loadSummary();
    void loadBackups();
  }, [pid]); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(action: string, body: Record<string, unknown> | undefined, label: string) {
    if (!pid) return;
    setBusy(action);
    const res = await playerWrite(pid, action, body).catch(() => null);
    setBusy("");
    if (!res) {
      pushToConsole(setConsoleEntries, label, "request failed (network error)", false);
      return;
    }
    // The body may be the run_publish entry (object), an {error} object, or a
    // non-JSON string (e.g. a proxy/Cloudflare HTML error page) — never assume a
    // shape, or `"x" in body` throws on a string and the click silently dies.
    const b: unknown = res.body;
    const isObj = typeof b === "object" && b !== null;
    const ok = res.ok && isObj && "ok" in b && (b as { ok?: unknown }).ok === true;
    const errMsg = isObj && "error" in b ? (b as { error?: string }).error : undefined;
    const out: PublishResult | string =
      typeof b === "string" ? b : (errMsg ?? (b as PublishResult));
    pushToConsole(setConsoleEntries, label, out, ok);
    if (ok) void loadSummary(); // reflect the write in the current-state panel
  }

  const anyBusy = busy !== "" || !pid;
  const selectedPreset = presets.find((p) => p.id === preset);

  return (
    <div className="space-y-6">
      <Section
        title="Target player"
        hint="Pick who to edit. Most writes below require the character to be OFFLINE — the server refuses (and reports it) when they're online."
      >
        <PlayerPicker />
      </Section>

      <Section title="Current state" hint="Live values for the selected player; refreshes after each successful edit.">
        {!summary ? (
          <p className="text-sm text-slate-500">
            {sumLoading
              ? "Loading…"
              : pid
                ? "No data — pick a valid player (or the character has no record yet)."
                : "Pick a player above."}
          </p>
        ) : (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap gap-x-6 gap-y-1">
              <span><span className="text-slate-500">Solaris</span> <span className="font-mono text-spice-300">{summary.solaris.toLocaleString()}</span></span>
              <span><span className="text-slate-500">Level</span> <span className="font-mono">{summary.xp.level}</span><span className="text-slate-500">/{summary.xp.maxLevel}</span></span>
              <span><span className="text-slate-500">XP</span> <span className="font-mono">{summary.xp.xp.toLocaleString()}</span><span className="text-slate-500">/{summary.xp.maxXP.toLocaleString()}</span></span>
              <span><span className="text-slate-500">To next level</span> <span className="font-mono">{summary.xp.atCap ? "MAX" : summary.xp.xpToNext.toLocaleString()}</span></span>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1">
              <span>
                <span className="text-slate-500">Faction</span>{" "}
                {summary.faction.alignment
                  ? <span className="text-spice-300">{summary.faction.alignment_name}</span>
                  : <span className="text-slate-400">None (unaligned)</span>}
              </span>
              <span className="text-slate-400">Atreides <span className="font-mono">{summary.faction.atreides.rep}</span> (t{summary.faction.atreides.tier} {summary.faction.atreides.tier_name})</span>
              <span className="text-slate-400">Harkonnen <span className="font-mono">{summary.faction.harkonnen.rep}</span> (t{summary.faction.harkonnen.tier} {summary.faction.harkonnen.tier_name})</span>
            </div>
            <div>
              <span className="text-slate-500">Journey progression</span>
              <div className="flex flex-wrap gap-1.5 mt-1">
                {summary.journey.map((j) => {
                  const done = j.total > 0 && j.complete >= j.total;
                  const cls = done
                    ? "border-emerald-700 text-emerald-300"
                    : j.complete > 0
                      ? "border-amber-700 text-amber-300"
                      : "border-slate-700 text-slate-500";
                  return (
                    <span key={j.id} className={"text-xs px-1.5 py-0.5 rounded border " + cls} title={j.name}>
                      {j.name}: {j.complete}/{j.total}
                    </span>
                  );
                })}
              </div>
            </div>
            <button className="btn-ghost text-xs border border-slate-700" onClick={() => void loadSummary()} disabled={sumLoading}>
              {sumLoading ? "…" : "refresh"}
            </button>
          </div>
        )}
      </Section>

      <div className="grid gap-6 md:grid-cols-2">
        {/* Currency & progression */}
        <Section title="Currency & progression">
          <div>
            <label className="label" htmlFor="pe-cur">Solaris (signed delta)</label>
            <div className="flex gap-2">
              <input id="pe-cur" type="number" value={currency}
                onChange={(e) => setCurrency(parseInt(e.target.value || "0", 10))}
                className="input-field font-mono" />
              <button className="btn-primary whitespace-nowrap" disabled={anyBusy}
                onClick={() => run("give-currency", { amount: currency }, `give-currency ${pid} ${currency}`)}>
                {busy === "give-currency" ? "…" : "Apply"}
              </button>
            </div>
          </div>
          <div>
            <label className="label" htmlFor="pe-xp">Character XP (signed delta)</label>
            <div className="flex gap-2">
              <input id="pe-xp" type="number" value={xp}
                onChange={(e) => setXp(parseInt(e.target.value || "0", 10))}
                className="input-field font-mono" />
              <button className="btn-primary whitespace-nowrap" disabled={anyBusy}
                onClick={() => run("award-char-xp", { amount: xp }, `award-char-xp ${pid} ${xp}`)}>
                {busy === "award-char-xp" ? "…" : "Award"}
              </button>
            </div>
          </div>
          <div>
            <label className="label">Keystones</label>
            <button className="btn-ghost border border-slate-700 w-full" disabled={anyBusy}
              onClick={() => run("grant-keystones", undefined, `grant-keystones ${pid}`)}>
              {busy === "grant-keystones" ? "…" : "Grant all 205 keystones"}
            </button>
          </div>
          <div>
            <label className="label">Tech tree (discovered recipes, offline)</label>
            <div className="flex gap-2">
              <button className="btn-ghost border border-slate-700 flex-1" disabled={anyBusy}
                onClick={() => run("tech-unlock", { mode: "unlock-all" }, `tech-unlock ${pid} unlock-all`)}>
                {busy === "tech-unlock" ? "…" : "Unlock all tech"}
              </button>
              <button className="btn-ghost border border-slate-700 flex-1" disabled={anyBusy}
                onClick={() => run("tech-unlock", { mode: "lock-all" }, `tech-unlock ${pid} lock-all`)}>
                {busy === "tech-unlock" ? "…" : "Lock all tech"}
              </button>
            </div>
          </div>
        </Section>

        {/* Identity */}
        <Section title="Identity">
          <div>
            <label className="label" htmlFor="pe-name">Rename character</label>
            <div className="flex gap-2">
              <input id="pe-name" value={name} maxLength={64}
                onChange={(e) => setName(e.target.value)} placeholder="new name"
                className="input-field" />
              <button className="btn-primary whitespace-nowrap" disabled={anyBusy || !name.trim()}
                onClick={() => run("rename", { name }, `rename ${pid} ${name}`)}>
                {busy === "rename" ? "…" : "Rename"}
              </button>
            </div>
          </div>
          <div>
            <label className="label" htmlFor="pe-tadd">Tags to add (comma-separated)</label>
            <input id="pe-tadd" value={tagsAdd} onChange={(e) => setTagsAdd(e.target.value)}
              placeholder="Tag.One, Tag.Two" className="input-field font-mono text-xs" />
          </div>
          <div>
            <label className="label" htmlFor="pe-trem">Tags to remove (comma-separated)</label>
            <input id="pe-trem" value={tagsRemove} onChange={(e) => setTagsRemove(e.target.value)}
              placeholder="Tag.Three" className="input-field font-mono text-xs" />
          </div>
          <button className="btn-primary w-full" disabled={anyBusy || (!tagsAdd.trim() && !tagsRemove.trim())}
            onClick={() => run("tags", { add: csvToList(tagsAdd), remove: csvToList(tagsRemove) }, `tags ${pid}`)}>
            {busy === "tags" ? "…" : "Apply tag changes"}
          </button>
        </Section>

        {/* Faction */}
        <Section title="Faction (Great House)" hint="rep adds a signed delta; set-tier aligns the house and sets the tier directly.">
          <div>
            <label className="label" htmlFor="pe-fac">House</label>
            <select id="pe-fac" value={faction} onChange={(e) => setFaction(e.target.value as "atreides" | "harkonnen")}
              className="input-field">
              <option value="atreides">Atreides</option>
              <option value="harkonnen">Harkonnen</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pe-rep">Reputation (signed delta)</label>
            <div className="flex gap-2">
              <input id="pe-rep" type="number" value={rep}
                onChange={(e) => setRep(parseInt(e.target.value || "0", 10))}
                className="input-field font-mono" />
              <button className="btn-primary whitespace-nowrap" disabled={anyBusy}
                onClick={() => run("faction-rep", { faction, amount: rep }, `faction-rep ${pid} ${faction} ${rep}`)}>
                {busy === "faction-rep" ? "…" : "Add rep"}
              </button>
            </div>
          </div>
          <div>
            <label className="label" htmlFor="pe-tier">Set tier (0–20, aligns the house)</label>
            <div className="flex gap-2">
              <input id="pe-tier" type="number" min={0} max={20} value={tier}
                onChange={(e) => setTier(Math.max(0, Math.min(20, parseInt(e.target.value || "0", 10))))}
                className="input-field font-mono" />
              <button className="btn-primary whitespace-nowrap" disabled={anyBusy}
                onClick={() => run("set-faction-tier", { faction, tier }, `set-faction-tier ${pid} ${faction} t${tier}`)}>
                {busy === "set-faction-tier" ? "…" : "Set tier"}
              </button>
            </div>
          </div>
          <button className="btn-ghost border border-red-900/60 text-red-300 w-full" disabled={anyBusy}
            onClick={() => setConfirm({
              title: "Remove faction?",
              message: `Remove ${pid} from their Great House entirely — alignment → None, both houses' reputation cleared. Offline-gated; reversible by setting a tier again.`,
              confirmLabel: "Remove faction",
              onConfirm: () => run("remove-faction", undefined, `remove-faction ${pid}`),
            })}>
            {busy === "remove-faction" ? "…" : "Remove faction (unalign)"}
          </button>
        </Section>

        {/* Journey progression */}
        <Section title="Journey progression" hint="Apply (unlock) or reverse (lock) a preset bundle of journey nodes. Takes effect on next login.">
          <div>
            <label className="label" htmlFor="pe-preset">Preset</label>
            <select id="pe-preset" value={preset} onChange={(e) => setPreset(e.target.value)} className="input-field">
              {presets.map((p) => (
                <option key={p.id} value={p.id}>{p.name} (~{p.node_count} nodes)</option>
              ))}
            </select>
            {selectedPreset && <p className="text-xs text-slate-500 mt-1">{selectedPreset.description}</p>}
          </div>
          <div className="flex gap-2">
            <button className="btn-primary flex-1" disabled={anyBusy || !preset}
              onClick={() => run("progression-unlock", { preset }, `progression-unlock ${pid} ${preset}`)}>
              {busy === "progression-unlock" ? "…" : "Unlock"}
            </button>
            <button className="btn-ghost border border-slate-700 flex-1" disabled={anyBusy || !preset}
              onClick={() => run("progression-lock", { preset }, `progression-lock ${pid} ${preset}`)}>
              {busy === "progression-lock" ? "…" : "Lock"}
            </button>
          </div>
        </Section>
      </div>

      {/* Character backups (native transfer subsystem) */}
      <Section title="Character backups"
        hint="Full-character snapshots via the game's own transfer subsystem (offline-gated). A restore fully REPLACES the character's current state; a backup from an older game patch is refused.">
        <div className="flex items-center gap-2">
          <button className="btn-primary" disabled={anyBusy}
            onClick={async () => {
              await run("char-backup", { reason: "manual via player-editor" }, `char-backup ${pid}`);
              void loadBackups();
            }}>
            {busy === "char-backup" ? "…" : "Backup now"}
          </button>
          <button className="btn-ghost text-xs" onClick={() => void loadBackups()} disabled={bkLoading || !pid}>
            {bkLoading ? "…" : "reload"}
          </button>
        </div>
        {backups.length === 0 && !bkLoading && (
          <p className="text-sm text-slate-500 italic">No backups yet for this player.</p>
        )}
        {backups.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500">
                  <th className="pr-3 py-1">Taken</th>
                  <th className="pr-3">Character</th>
                  <th className="pr-3">Origin</th>
                  <th className="pr-3">Size</th>
                  <th className="pr-3">Game patch</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {backups.map((b) => (
                  <tr key={b.file} className="border-t border-slate-800">
                    <td className="pr-3 py-1 font-mono whitespace-nowrap">{b.created_at || "-"}</td>
                    <td className="pr-3">{b.character_name || "-"}</td>
                    <td className="pr-3">{b.action}{b.reason ? ` — ${b.reason}` : ""}</td>
                    <td className="pr-3 whitespace-nowrap">{(b.bytes / 1024).toFixed(0)} KB</td>
                    <td className="pr-3 font-mono">{b.patches_checksum ? b.patches_checksum.slice(0, 8) : "-"}</td>
                    <td className="whitespace-nowrap text-right">
                      <button className="btn-ghost text-xs border border-amber-900/60 text-amber-300 mr-2"
                        disabled={anyBusy}
                        onClick={() => setConfirm({
                          title: "Restore character?",
                          message: `Restore ${b.character_name || b.fls} from ${b.created_at}. This REPLACES the character's entire current state (the player must be offline). Anything done since this backup is lost.`,
                          confirmLabel: "Restore",
                          onConfirm: async () => {
                            const res = await charBackupRestore(b.file).catch(() => null);
                            const rb: unknown = res?.body;
                            const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
                              && "ok" in rb && (rb as { ok?: unknown }).ok === true;
                            pushToConsole(setConsoleEntries, `char-restore ${b.file}`,
                              typeof rb === "string" ? rb : (rb as PublishResult), ok);
                            void loadSummary();
                            void loadBackups();
                          },
                        })}>
                        Restore…
                      </button>
                      <button className="btn-ghost text-xs text-red-300" disabled={anyBusy}
                        onClick={() => setConfirm({
                          title: "Delete this backup?",
                          message: `Permanently delete backup ${b.file}. The character itself is not touched.`,
                          confirmLabel: "Delete backup",
                          onConfirm: async () => {
                            const res = await charBackupDelete(b.file).catch(() => null);
                            const rb: unknown = res?.body;
                            const ok = Boolean(res?.ok) && typeof rb === "object" && rb !== null
                              && "ok" in rb && (rb as { ok?: unknown }).ok === true;
                            pushToConsole(setConsoleEntries, `char-backup-delete ${b.file}`,
                              typeof rb === "string" ? rb : (rb as PublishResult), ok);
                            void loadBackups();
                          },
                        })}>
                        🗑
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {/* Danger zone */}
      <Section title="Danger zone" danger
        hint="Destructive, offline-gated writes. reset-spec wipes specialization tracks + keystones; account-delete takes a verified character backup first, then runs the irreversible full cascade.">
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="label">Reset specialization</label>
            <button className="btn-danger w-full" disabled={anyBusy}
              onClick={() => setConfirm({
                title: "Reset specialization?",
                message: `Wipe ALL specialization tracks + purchased keystones for ${pid}. The character must be offline. This cannot be undone (the game reconciles on login).`,
                confirmLabel: "Reset spec",
                onConfirm: () => run("reset-spec", undefined, `reset-spec ${pid}`),
              })}>
              {busy === "reset-spec" ? "…" : "Reset spec…"}
            </button>
          </div>
          <div>
            <label className="label" htmlFor="pe-del">Delete account (type the exact FLS id to confirm)</label>
            <div className="flex gap-2">
              <input id="pe-del" value={delConfirm} onChange={(e) => setDelConfirm(e.target.value)}
                placeholder="16-hex FLS id" className="input-field font-mono text-xs" />
              <button className="btn-danger whitespace-nowrap" disabled={anyBusy || !delConfirm.trim()}
                onClick={() => setConfirm({
                  title: "Delete account — IRREVERSIBLE",
                  message: `Permanently delete the account/character + full cascade for ${pid}. The server only proceeds if this confirm equals the resolved FLS id AND the character is offline.`,
                  confirmLabel: "Delete forever",
                  onConfirm: () => run("account-delete", { confirm: delConfirm.trim(), reason: "deleted via player-editor" }, `account-delete ${pid}`),
                })}>
                {busy === "account-delete" ? "…" : "Delete…"}
              </button>
            </div>
          </div>
        </div>
      </Section>

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
