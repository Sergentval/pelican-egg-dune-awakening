// Shared interaction primitives (design phase 6).
//  • StepperInput — number field with custom +/- chevrons (native spinners are
//    hidden globally in index.css), clamped to min/max.
//  • GlobalRipple — a single delegated click listener that spawns a contained
//    0→1 ripple on prominent buttons, so every action button feels alive
//    without touching each call site.

import { useEffect } from "react";

function Chevron() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m9 18 6-6-6-6" />
    </svg>
  );
}

export function StepperInput({
  value, onChange, min, max, step = 1, className = "",
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number;
  max?: number;
  step?: number;
  className?: string;
}) {
  const v = Number.isNaN(value) ? (min ?? 0) : value;
  const atMax = max != null && v >= max;
  const atMin = min != null && v <= min;
  function bump(d: number) {
    let next = v + d * step;
    if (min != null) next = Math.max(min, next);
    if (max != null) next = Math.min(max, next);
    onChange(next);
  }
  return (
    <div className="stepper">
      <input
        type="number"
        className={"input-field stepper-input " + className}
        value={Number.isNaN(value) ? "" : value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const n = e.target.value === "" ? NaN : Number(e.target.value);
          onChange(Number.isNaN(n) ? (min ?? 0) : n);
        }}
      />
      <div className="stepper-btns">
        <button type="button" className="stepper-btn up" tabIndex={-1} disabled={atMax} onClick={() => bump(1)} aria-label="Increase"><Chevron /></button>
        <button type="button" className="stepper-btn down" tabIndex={-1} disabled={atMin} onClick={() => bump(-1)} aria-label="Decrease"><Chevron /></button>
      </div>
    </div>
  );
}

const RIPPLE_SELECTOR = ".btn-primary, .btn-danger, .btn-warn, .btn-outline, .btn, .btn-danger-outline, .btn-warn-outline";

export function GlobalRipple() {
  useEffect(() => {
    function onClick(e: MouseEvent) {
      const el = (e.target as HTMLElement | null)?.closest(RIPPLE_SELECTOR) as HTMLElement | null;
      if (!el || el.hasAttribute("disabled")) return;
      const rect = el.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const r = document.createElement("span");
      r.className = "ripple";
      r.style.width = r.style.height = `${size}px`;
      r.style.left = `${e.clientX - rect.left - size / 2}px`;
      r.style.top = `${e.clientY - rect.top - size / 2}px`;
      el.appendChild(r);
      window.setTimeout(() => r.remove(), 650);
    }
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);
  return null;
}
