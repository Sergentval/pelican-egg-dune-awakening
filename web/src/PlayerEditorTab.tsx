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
  fetchProgressionPresets,
  playerWrite,
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

      {/* Danger zone */}
      <Section title="Danger zone" danger
        hint="Destructive, offline-gated writes. reset-spec wipes specialization tracks + keystones; account-delete is an irreversible full cascade.">
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
