// Shared components: Login screen, output console, player picker.

import { useEffect, useRef, useState } from "react";
import type { PublishResult } from "./api";
import { fetchPlayers, login, parsePlayerTable, sanitizePlayerId, setToken } from "./api";
import { useTarget } from "./target";

// ---- Login -------------------------------------------------------------

export function Login({ onAuthed }: { onAuthed: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setPending(true);
    const res = await login(password);
    setPending(false);
    if (res.ok) {
      // Session is established via HttpOnly cookie that the backend set
      // on the /api/login response. Nothing for the SPA to stash — the
      // browser handles it. setToken("") is called once to clear any
      // pre-migration localStorage entry.
      setToken("");
      onAuthed();
    } else {
      setError(("body" in res && (res.body as { error?: string }).error) || "Login failed");
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 animate-fade-in">
      <form onSubmit={submit} className="w-full max-w-sm card p-6 shadow-2xl">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-2xl">🌀</span>
          <h1 className="text-xl font-semibold text-spice-300">Dune Admin</h1>
        </div>
        <p className="text-sm text-slate-400 mb-6">Pelican egg admin panel</p>
        <label className="label" htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="input-field font-mono"
        />
        {error && <p className="text-sm text-red-400 mt-2">{error}</p>}
        <button type="submit" className="btn-primary mt-4 w-full" disabled={pending || !password}>
          {pending ? "Signing in…" : "Sign in"}
        </button>
        <p className="text-xs text-slate-500 mt-4">
          Password is set in the Pelican egg variable <span className="font-mono text-slate-400">DUNE_ADMIN_UI_PASSWORD</span>. If you left it blank, prestart.sh logs an auto-generated value at boot.
        </p>
      </form>
    </div>
  );
}

// ---- OutputConsole -----------------------------------------------------

export interface ConsoleEntry {
  ts: number;
  label: string;
  body: string;
  ok: boolean;
}

const CONSOLE_LIMIT = 100;

interface ConsoleProps {
  entries: ConsoleEntry[];
  onClear: () => void;
}

export function OutputConsole({ entries, onClear }: ConsoleProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = 0;
  }, [entries.length]);

  return (
    <section className="card overflow-hidden">
      <header className="card-header">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold text-sm">Output</h2>
          <span className="text-xs text-slate-500">{entries.length} entries · most recent first</span>
        </div>
        <button className="btn-ghost text-xs" onClick={onClear} disabled={entries.length === 0}>
          clear
        </button>
      </header>
      <div ref={ref} className="font-mono text-xs p-3 space-y-2 max-h-72 overflow-y-auto">
        {entries.length === 0 && <div className="text-slate-500 italic">no commands run yet</div>}
        {entries.map((entry, idx) => (
          <div
            key={`${entry.ts}-${idx}`}
            className={
              "border-l-2 pl-3 py-1 " +
              (entry.ok ? "border-emerald-500/70" : "border-red-500/70")
            }
          >
            <div className="text-slate-500 text-[10px] flex items-center gap-2">
              <span>{new Date(entry.ts).toLocaleTimeString()}</span>
              <span className={entry.ok ? "pill-ok" : "pill-err"}>{entry.ok ? "ok" : "fail"}</span>
              <span className="text-slate-400">{entry.label}</span>
            </div>
            <pre className="whitespace-pre-wrap text-slate-300 text-xs mt-0.5">{entry.body || "(no output)"}</pre>
          </div>
        ))}
      </div>
    </section>
  );
}

export function pushToConsole(
  setEntries: React.Dispatch<React.SetStateAction<ConsoleEntry[]>>,
  label: string,
  result: PublishResult | string,
  ok = true,
): void {
  const body =
    typeof result === "string"
      ? result
      : (result.stdout || "") + (result.stderr ? "\n" + result.stderr : "");
  setEntries((prev) => {
    const next = [{ ts: Date.now(), label, body, ok }, ...prev];
    return next.slice(0, CONSOLE_LIMIT);
  });
}

// ---- PlayerPicker -----------------------------------------------------
//
// Reusable component for every per-player form. Combines a free-text
// input with a dropdown of known accounts. Default value is "me".

interface PlayerPickerProps {
  /** Controlled value. If omitted, the picker reads from useTarget(). */
  value?: string;
  /** Change handler. If omitted, the picker writes via useTarget(). */
  onChange?: (next: string) => void;
  allowStar?: boolean;
  /** Called after a player is picked (row/quick chip) — used to close the modal. */
  onPicked?: () => void;
}

// Procedural avatar — deterministic hue from the name, initials inside.
function avatarHue(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}
function initials(s: string): string {
  const parts = s.replace(/[^a-zA-Z0-9]/g, " ").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}
export function Avatar({ name, size = 30 }: { name: string; size?: number }) {
  const hue = avatarHue(name || "?");
  return (
    <span
      className="avatar"
      style={{ width: size, height: size, background: `linear-gradient(150deg, oklch(0.62 0.13 ${hue}), oklch(0.5 0.13 ${(hue + 40) % 360}))` }}
      aria-hidden
    >
      {initials(name || "?")}
    </span>
  );
}

type PlayerFilter = "all" | "online" | "offline";

interface KnownPlayer {
  fls_id: string;
  character: string | null;
  steam_id: string | null;
  online: boolean;
}

let cachedPlayers: KnownPlayer[] | null = null;

// Best-effort online status for a target playerId, from the last-fetched roster.
// null = unknown (no roster yet, or "me"/"*"); true/false when resolvable.
export function isPlayerOnline(playerId: string): boolean | null {
  if (!cachedPlayers) return null;
  const id = playerId.trim();
  let p: KnownPlayer | undefined;
  if (id.startsWith("name:")) {
    const n = id.slice(5).toLowerCase();
    p = cachedPlayers.find((x) => (x.character || "").toLowerCase() === n);
  } else if (id.startsWith("steam:")) {
    const s = id.slice(6);
    p = cachedPlayers.find((x) => x.steam_id === s);
  } else if (/^[0-9a-fA-F]{16}$/.test(id)) {
    p = cachedPlayers.find((x) => x.fls_id.toLowerCase() === id.toLowerCase());
  }
  return p ? p.online : null;
}

export function PlayerPicker({ value, onChange, allowStar = false, onPicked }: PlayerPickerProps) {
  const target = useTarget();
  // Default: drive the shared target. Tabs can still pass explicit
  // value/onChange to keep their own picker isolated if they want.
  const effectiveValue = value ?? target.playerId;
  const effectiveChange = onChange ?? target.setPlayerId;

  const [players, setPlayers] = useState<KnownPlayer[]>(cachedPlayers || []);
  const [refreshing, setRefreshing] = useState(false);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<PlayerFilter>("all");

  async function refresh() {
    setRefreshing(true);
    const res = await fetchPlayers("all");
    if (res.ok && (res.body as PublishResult).stdout) {
      const rows = parsePlayerTable((res.body as PublishResult).stdout).map((r) => ({
        fls_id: r.fls_id,
        character: r.character,
        steam_id: r.steam_id,
        online: r.online === "Online",
      }));
      cachedPlayers = rows;
      setPlayers(rows);
    }
    setRefreshing(false);
  }

  useEffect(() => {
    if (!cachedPlayers) refresh();
  }, []);

  function pick(v: string) {
    effectiveChange(v);
    onPicked?.();
  }

  const onlineCount = players.filter((p) => p.online).length;
  const query = q.trim().toLowerCase();
  const filtered = players.filter((p) => {
    if (filter === "online" && !p.online) return false;
    if (filter === "offline" && p.online) return false;
    if (!query) return true;
    return (p.character || "").toLowerCase().includes(query)
      || p.fls_id.toLowerCase().includes(query)
      || (p.steam_id || "").toLowerCase().includes(query);
  });

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={"chip text-xs" + (effectiveValue === "me" ? " is-active" : "")} onClick={() => pick("me")}>me</button>
        {allowStar && (
          <button type="button" className={"chip text-xs" + (effectiveValue === "*" ? " is-active" : "")} onClick={() => pick("*")}>all online</button>
        )}
        <input
          type="text" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="search name / Steam / id…"
          className="input-field text-xs flex-1 min-w-[150px]"
        />
        <button type="button" className="btn-ghost text-xs" onClick={refresh} disabled={refreshing} title="refresh players">{refreshing ? "…" : "↻"}</button>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {(["all", "online", "offline"] as const).map((f) => (
          <button key={f} type="button" className={"chip text-xs" + (filter === f ? " is-active" : "")} onClick={() => setFilter(f)}>
            {f[0].toUpperCase() + f.slice(1)}
            <span className="chip-count">{f === "all" ? players.length : f === "online" ? onlineCount : players.length - onlineCount}</span>
          </button>
        ))}
      </div>
      <div className="player-list max-h-64 overflow-y-auto">
        {filtered.length === 0 && (
          <div className="px-2 py-4 text-center text-xs text-slate-500">{players.length === 0 ? (refreshing ? "loading…" : "no players seen yet") : "no match"}</div>
        )}
        {filtered.map((p) => {
          const name = p.character || `${p.fls_id.slice(0, 8)}…`;
          const buttonValue = p.character ? `name:${p.character}` : p.fls_id;
          const active = effectiveValue === buttonValue;
          return (
            <button type="button" key={p.fls_id} className={"player-row" + (active ? " is-active" : "")} onClick={() => pick(buttonValue)} title={`FLS ${p.fls_id} · Steam ${p.steam_id || "?"}`}>
              <Avatar name={name} />
              <span className="flex-1 min-w-0 text-left">
                <span className="block truncate text-sm">{p.character ? name : <span className="font-mono">{name}</span>}</span>
                <span className="block truncate text-[10px] text-slate-500 font-mono">{p.steam_id || p.fls_id}</span>
              </span>
              <span className={p.online ? "pill-ok" : "pill-mute"}>{p.online ? "online" : "offline"}</span>
            </button>
          );
        })}
      </div>
      <input
        type="text"
        value={effectiveValue}
        maxLength={64}
        onChange={(e) => effectiveChange(e.target.value)}
        onBlur={(e) => {
          const cleaned = sanitizePlayerId(e.target.value);
          if (cleaned !== e.target.value) effectiveChange(cleaned);
        }}
        placeholder="me, *, name:Sergentval, steam:76561198..., or 16-hex FLS id"
        className="input-field font-mono text-xs"
      />
    </div>
  );
}

// Modal player selector — opened by the top-bar target pill; reuses PlayerPicker.
export function PlayerPickerModal() {
  const t = useTarget();
  if (!t.pickerOpen) return null;
  return (
    <div className="modal-scrim" onClick={() => t.setPickerOpen(false)}>
      <div className="modal" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3 className="card-title">Select target player</h3>
          <button className="btn-ghost text-xs" onClick={() => t.setPickerOpen(false)}>close</button>
        </div>
        <div className="modal-body">
          <PlayerPicker allowStar onPicked={() => t.setPickerOpen(false)} />
        </div>
      </div>
    </div>
  );
}

// ---- Confirm dialog for destructive commands --------------------------

interface ConfirmProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function Confirm({ open, title, message, confirmLabel, onConfirm, onCancel }: ConfirmProps) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 bg-slate-950/80 flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="card max-w-md w-full p-6">
        <h3 className="text-lg font-semibold text-red-300 mb-2">{title}</h3>
        <p className="text-sm text-slate-300 mb-6">{message}</p>
        <div className="flex justify-end gap-2">
          <button className="btn-ghost" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn-danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
