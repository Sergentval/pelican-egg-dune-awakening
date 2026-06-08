// Appearance tweaks (design phase 5) — live theme + accent/font/density/motion,
// persisted to localStorage. applyTweaks sets the root theme class + CSS vars so
// the whole var-based design system re-themes instantly.

export type Tweaks = {
  theme: "desert" | "night" | "sietch";
  accent: "spice" | "gold" | "crimson" | "teal";
  font: "saira" | "oswald";
  density: "compact" | "cozy" | "spacious";
  motion: "calm" | "lively" | "max";
};

export const DEFAULT_TWEAKS: Tweaks = {
  theme: "desert",
  accent: "spice",
  font: "saira",
  density: "cozy",
  motion: "lively",
};

const KEY = "dune.tweaks";

export function loadTweaks(): Tweaks {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}") as Partial<Tweaks>;
    return { ...DEFAULT_TWEAKS, ...raw };
  } catch {
    return DEFAULT_TWEAKS;
  }
}

export function saveTweaks(t: Tweaks): void {
  try { localStorage.setItem(KEY, JSON.stringify(t)); } catch { /* ignore */ }
}

const THEME_CLASS: Record<Tweaks["theme"], string> = {
  desert: "theme-desert",
  night: "theme-night",
  sietch: "theme-sietch",
};
const DENSITY: Record<Tweaks["density"], Record<string, string>> = {
  compact: { "--pad": "14px", "--gap": "14px", "--row-y": "7px" },
  cozy: { "--pad": "20px", "--gap": "22px", "--row-y": "11px" },
  spacious: { "--pad": "26px", "--gap": "30px", "--row-y": "15px" },
};
const MOTION: Record<Tweaks["motion"], Record<string, string>> = {
  calm: { "--press": "0.99", "--dur": "0.6" },
  lively: { "--press": "0.955", "--dur": "1" },
  max: { "--press": "0.92", "--dur": "1.4" },
};
const FONT: Record<Tweaks["font"], string> = {
  saira: '"Saira Condensed", "Oswald", system-ui, sans-serif',
  oswald: '"Oswald", "Saira Condensed", system-ui, sans-serif',
};
// null = use the active theme's own accent (the spice default)
export const ACCENTS: Record<Tweaks["accent"], { accent: string; accent2: string } | null> = {
  spice: null,
  gold: { accent: "oklch(0.82 0.15 85)", accent2: "oklch(0.70 0.16 66)" },
  crimson: { accent: "oklch(0.66 0.21 24)", accent2: "oklch(0.55 0.20 20)" },
  teal: { accent: "oklch(0.78 0.12 196)", accent2: "oklch(0.67 0.12 200)" },
};

export function applyTweaks(t: Tweaks): void {
  const root = document.documentElement;
  root.classList.remove("theme-desert", "theme-night", "theme-sietch");
  root.classList.add(THEME_CLASS[t.theme]);
  const set = (k: string, v: string) => root.style.setProperty(k, v);
  for (const [k, v] of Object.entries(DENSITY[t.density])) set(k, v);
  for (const [k, v] of Object.entries(MOTION[t.motion])) set(k, v);
  set("--font-display", FONT[t.font]);
  const a = ACCENTS[t.accent];
  if (a) {
    set("--accent", a.accent);
    set("--accent-2", a.accent2);
    set("--accent-soft", `color-mix(in oklch, ${a.accent} 16%, transparent)`);
    set("--accent-line", `color-mix(in oklch, ${a.accent} 45%, transparent)`);
    set("--glow", `color-mix(in oklch, ${a.accent} 55%, transparent)`);
    set("--accent-ink", "oklch(0.20 0.03 80)");
  } else {
    for (const k of ["--accent", "--accent-2", "--accent-soft", "--accent-line", "--glow", "--accent-ink"]) {
      root.style.removeProperty(k);
    }
  }
}
