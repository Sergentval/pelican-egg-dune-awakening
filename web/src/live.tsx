// Global "Live" auto-refresh — one switch for the whole panel.
//
// When Live is on (default), data tabs re-poll on their own cadence via
// useAutoRefresh; when off, nothing auto-polls and the per-tab refresh buttons
// are used manually. The choice is remembered in localStorage. There is no
// server push (no WebSocket/SSE) — this is coordinated client-side polling.

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

const STORAGE_KEY = "dune-admin-live";

interface LiveCtx {
  live: boolean;
  setLive: (v: boolean) => void;
}

const Ctx = createContext<LiveCtx>({ live: true, setLive: () => {} });

export function LiveProvider({ children }: { children: ReactNode }) {
  const [live, setLiveState] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(STORAGE_KEY);
      return v === null ? true : v === "1"; // default ON
    } catch {
      return true;
    }
  });
  const setLive = (v: boolean) => {
    setLiveState(v);
    try {
      localStorage.setItem(STORAGE_KEY, v ? "1" : "0");
    } catch {
      // ignore storage failures — the in-memory state still drives the UI
    }
  };
  return <Ctx.Provider value={{ live, setLive }}>{children}</Ctx.Provider>;
}

export function useLive(): LiveCtx {
  return useContext(Ctx);
}

// Call fn() every intervalMs while Live is on. Does NOT fire immediately —
// callers do their own initial load in a mount effect. fn is held in a ref so a
// changing closure (e.g. a new selected map/player) is picked up without
// resetting the timer.
export function useAutoRefresh(fn: () => void, intervalMs: number): void {
  const { live } = useLive();
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => fnRef.current(), intervalMs);
    return () => clearInterval(t);
  }, [live, intervalMs]);
}

// Header switch — toggles the global Live state.
export function LiveToggle() {
  const { live, setLive } = useLive();
  return (
    <button
      onClick={() => setLive(!live)}
      title={live ? "Live updates ON — data auto-refreshes" : "Live updates OFF — use each tab's refresh button"}
      className={
        "text-xs px-2 py-1 rounded flex items-center gap-1.5 border " +
        (live ? "border-emerald-900/60 text-emerald-300" : "border-slate-700 text-slate-500")
      }
    >
      <span className={live ? "animate-pulse" : ""} aria-hidden>●</span>
      {live ? "Live" : "Paused"}
    </button>
  );
}
