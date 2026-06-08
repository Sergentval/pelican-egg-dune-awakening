// Appearance / tweaks panel (design phase 5) — a top-right popover opened by the
// gear in the app bar. Live theme + accent + display font + density + motion.

import type { ReactNode } from "react";
import type { Tweaks } from "./tweaks";

export function GearIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

const THEMES = [
  { id: "desert", label: "Deep Desert" },
  { id: "night", label: "Night Spice" },
  { id: "sietch", label: "Sietch Stone" },
] as const;
const ACCENT_SWATCHES = [
  { id: "spice", label: "Spice", sw: "oklch(0.745 0.165 58)" },
  { id: "gold", label: "Gold", sw: "oklch(0.82 0.15 85)" },
  { id: "crimson", label: "Crimson", sw: "oklch(0.66 0.21 24)" },
  { id: "teal", label: "Teal", sw: "oklch(0.78 0.12 196)" },
] as const;
const FONTS = [
  { id: "saira", label: "Saira" },
  { id: "oswald", label: "Oswald" },
] as const;
const DENSITIES = [
  { id: "compact", label: "Compact" },
  { id: "cozy", label: "Cozy" },
  { id: "spacious", label: "Spacious" },
] as const;
const MOTIONS = [
  { id: "calm", label: "Calm" },
  { id: "lively", label: "Lively" },
  { id: "max", label: "Max" },
] as const;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="flex flex-wrap items-center gap-1.5">{children}</div>
    </div>
  );
}

export function TweaksPanel({
  open, onClose, tweaks, onChange,
}: {
  open: boolean;
  onClose: () => void;
  tweaks: Tweaks;
  onChange: (t: Tweaks) => void;
}) {
  if (!open) return null;
  function set<K extends keyof Tweaks>(k: K, v: Tweaks[K]) { onChange({ ...tweaks, [k]: v }); }
  function Chips<T extends string>(opts: readonly { id: T; label: string }[], cur: T, on: (v: T) => void) {
    return opts.map((o) => (
      <button key={o.id} className={"chip text-xs" + (cur === o.id ? " is-active" : "")} onClick={() => on(o.id)}>
        {o.label}
      </button>
    ));
  }
  return (
    <>
      <div className="fixed inset-0 z-[90]" onClick={onClose} />
      <div className="fixed top-[78px] right-3 z-[100] w-[320px] max-w-[calc(100vw-24px)] card p-4 space-y-4 pop-in">
        <div className="flex items-center justify-between">
          <h3 className="card-title">Appearance</h3>
          <button className="btn-ghost text-xs" onClick={onClose}>close</button>
        </div>
        <Row label="Theme">{Chips(THEMES, tweaks.theme, (v) => set("theme", v))}</Row>
        <Row label="Accent">
          {ACCENT_SWATCHES.map((o) => (
            <button
              key={o.id}
              onClick={() => set("accent", o.id)}
              title={o.label}
              aria-label={o.label}
              className={"w-7 h-7 rounded-full border-2 transition " + (tweaks.accent === o.id ? "border-slate-100 scale-110" : "border-transparent")}
              style={{ background: o.sw }}
            />
          ))}
        </Row>
        <Row label="Display font">{Chips(FONTS, tweaks.font, (v) => set("font", v))}</Row>
        <Row label="Density">{Chips(DENSITIES, tweaks.density, (v) => set("density", v))}</Row>
        <Row label="Motion">{Chips(MOTIONS, tweaks.motion, (v) => set("motion", v))}</Row>
      </div>
    </>
  );
}
