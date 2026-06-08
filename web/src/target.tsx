// Shared "target" state — the player you're operating on plus their
// last-resolved position. Held at the App level via React Context so
// every command tab reads/writes the same value. Switching tabs no
// longer wipes the looked-up XYZ.

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { fetchPos, parsePosOutput, type PosInfo, type PublishResult } from "./api";

export interface Target {
  /** Free-text player id used by all forms. "me" / "name:X" / "steam:..." / 16-hex / "*". */
  playerId: string;
  /** Last-resolved position for the player. Null until a pos lookup runs. */
  pos: PosInfo | null;
  /** Whether a pos lookup is currently in flight. */
  posLoading: boolean;
  /** Last position-lookup error, if any. */
  posError: string | null;
}

interface TargetCtx extends Target {
  setPlayerId: (next: string) => void;
  /** Look up position for the current playerId (or pass an override). */
  lookupPos: (override?: string) => Promise<PosInfo | null>;
  /** Manually overwrite the position without hitting the API (used when a tab does its own pos call and wants to share the result). */
  setPos: (next: PosInfo | null) => void;
  /** Drop everything back to defaults. */
  reset: () => void;
  /** Whether the shared player-picker modal is open. */
  pickerOpen: boolean;
  setPickerOpen: (b: boolean) => void;
}

const Ctx = createContext<TargetCtx | null>(null);

export function TargetProvider({ children }: { children: ReactNode }) {
  const [playerId, setPlayerId] = useState<string>("me");
  const [pos, setPos] = useState<PosInfo | null>(null);
  const [posLoading, setPosLoading] = useState(false);
  const [posError, setPosError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  const lookupPos = useCallback(
    async (override?: string) => {
      const who = override ?? playerId;
      setPosLoading(true);
      setPosError(null);
      const res = await fetchPos(who);
      setPosLoading(false);
      if (!res.ok) {
        const err = (res.body as { error?: string }).error || "lookup failed";
        setPosError(err);
        return null;
      }
      const stdout = (res.body as PublishResult).stdout || "";
      const parsed = parsePosOutput(stdout, who);
      if (!parsed) {
        setPosError("could not parse pos output");
        return null;
      }
      setPos(parsed);
      return parsed;
    },
    [playerId],
  );

  const value = useMemo<TargetCtx>(
    () => ({
      playerId,
      pos,
      posLoading,
      posError,
      setPlayerId: (next) => {
        setPlayerId(next);
        // Player changed — drop the stale position so the UI doesn't
        // suggest spawning vehicles at the previous player's spot.
        if (next !== (pos?.source ?? "")) setPos(null);
        setPosError(null);
      },
      lookupPos,
      setPos,
      reset: () => {
        setPlayerId("me");
        setPos(null);
        setPosError(null);
      },
      pickerOpen,
      setPickerOpen,
    }),
    [playerId, pos, posLoading, posError, lookupPos, pickerOpen],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTarget(): TargetCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTarget must be used inside <TargetProvider>");
  return v;
}

// Sticky pill rendered in the App header so the active target is
// always visible no matter which tab you're on.
export function TargetPill() {
  const t = useTarget();
  const hasPos = t.pos !== null;
  return (
    <div
      className="target-pill target-pill-btn"
      role="button"
      tabIndex={0}
      title="Change target player"
      onClick={() => t.setPickerOpen(true)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); t.setPickerOpen(true); } }}
    >
      <span className="faint text-xs">target</span>
      <span className="t-mono text-spice-300 text-xs">{t.playerId}</span>
      {hasPos && (
        <span className="faint t-mono text-[11px]">· {t.pos!.map} ({Math.round(t.pos!.x)},{Math.round(t.pos!.y)})</span>
      )}
      {t.posLoading && <span className="faint italic text-xs">…</span>}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); void t.lookupPos(); }}
        className="text-slate-400 hover:text-slate-100"
        title="refresh position"
        disabled={t.posLoading}
      >
        ↻
      </button>
    </div>
  );
}
