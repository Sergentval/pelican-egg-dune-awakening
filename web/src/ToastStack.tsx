// Toast notifications (design phase 4) — replaces the persistent bottom Output
// console. Every action still calls pushToConsole (so the session log feeds the
// Events → Command audit), and each NEW entry pops a transient toast top-right
// that auto-dismisses (ok faster than fail) with a countdown bar + manual close.

import { useEffect, useRef, useState } from "react";
import type { ConsoleEntry } from "./components";

const OK_MS = 4200;
const FAIL_MS = 6500;
const MAX = 4;

interface ActiveToast { key: string; label: string; body: string; ok: boolean; }

function keyOf(e: ConsoleEntry): string { return `${e.ts}-${e.label}`; }

export function ToastStack({ entries }: { entries: ConsoleEntry[] }) {
  const [toasts, setToasts] = useState<ActiveToast[]>([]);
  const seen = useRef<Set<string>>(new Set());
  const inited = useRef(false);

  useEffect(() => {
    // First run: mark whatever's already there as seen — don't toast on mount.
    if (!inited.current) {
      inited.current = true;
      for (const e of entries) seen.current.add(keyOf(e));
      return;
    }
    const fresh = entries.filter((e) => !seen.current.has(keyOf(e)));
    if (fresh.length === 0) return;
    for (const e of fresh) seen.current.add(keyOf(e));
    setToasts((prev) =>
      [...fresh.map((e) => ({ key: keyOf(e), label: e.label, body: e.body, ok: e.ok })), ...prev].slice(0, MAX),
    );
  }, [entries]);

  function dismiss(key: string) {
    setToasts((prev) => prev.filter((t) => t.key !== key));
  }

  if (toasts.length === 0) return null;

  return (
    <div className="toast-stack" role="region" aria-label="Notifications">
      {toasts.map((t) => <Toast key={t.key} t={t} onDismiss={() => dismiss(t.key)} />)}
    </div>
  );
}

function Toast({ t, onDismiss }: { t: ActiveToast; onDismiss: () => void }) {
  const ms = t.ok ? OK_MS : FAIL_MS;
  useEffect(() => {
    const timer = setTimeout(onDismiss, ms);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className={"toast " + (t.ok ? "toast-ok" : "toast-fail")}>
      <div className="toast-icon" aria-hidden>{t.ok ? "✓" : "✕"}</div>
      <div className="toast-body">
        <div className="toast-title">
          {t.ok ? "Done" : "Failed"}
          <span className="toast-sub">{t.label}</span>
        </div>
        {t.body && <div className="toast-detail">{t.body}</div>}
      </div>
      <button className="toast-close" onClick={onDismiss} aria-label="Dismiss">✕</button>
      <span className={"toast-bar" + (t.ok ? "" : " fail")} style={{ animationDuration: `${ms}ms` }} />
    </div>
  );
}
