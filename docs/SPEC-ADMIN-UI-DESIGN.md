# SPEC — Admin UI visual + interaction overhaul (Claude Design "Dune Admin V2")

Status: **in progress** · Source: Claude Design handoff `Dune-Admin-Panel-V2`
(authored against this repo @ `de7070c`). Owner approved **full overhaul, phased**
(2026-06-08).

## Context

The IA redesign (PRs #62–#68) already landed the structure the design targets
(4 groups / 15 tabs / workspaces). The design handoff (`theme.css` + JSX
prototype + 6 chat transcripts) is therefore a **visual + interaction** layer,
not new IA. Per the handoff's MERGE-AUDIT, the prototype was built against our
exact `main`; our job is to apply its look and shell to the live React+Tailwind
SPA, reusing the existing tab components unchanged.

**Locked design decisions (from the chats — do not revisit):** two-tier floating
top-bar nav (NOT a sidebar — user explicitly rejected the sidebar); ⌘K command
palette; toast notifications + History "Command audit" instead of the persistent
bottom Output console; unified player-selection component everywhere; desert
theme + spice-orange accent; SVG line icons (no emoji); 3 swappable themes +
tweaks panel (accent/font/density/motion, persisted); custom StepperInput for all
number fields; springy buttons with 0→1 ripple + busy spinner; moderation buttons
color-coded by severity (Kick offline-gated); transform-only entrance animations;
responsive grids with no dead space; slider fill colors only the portion behind
the thumb.

## Design system (theme.css)

Fonts: **Saira Condensed** (display), **Hanken Grotesk** (body), **JetBrains
Mono** (mono). Colors via OKLCH CSS custom properties, 3 themes
(`.theme-desert` default / `.theme-night` / `.theme-sietch`). Tokens: surfaces,
text ramp, accent (+soft/line/glow), ok/warn/err, radii, shadows, density/motion
multipliers. Ambient `.app-bg` desert wash + grain.

## Phases (PR per phase — build `tsc && vite`, deploy server 30, verify, merge)

1. **Design-system foundation** _(this PR)_ — fonts; port the OKLCH theme tokens
   (3 themes) + base + `.app-bg` into `web/src/index.css`; remap the Tailwind
   `slate`→desert and `spice`→accent ramps (with `<alpha-value>` so `/opacity`
   still works) + fonts in `tailwind.config.js`; restyle the shared component
   classes (`card`, `card-header`, `btn-*`, `input-field`, `label`, `pill*`,
   `dot`, `chip`) to the theme.css versions. Restyles the whole app in place —
   the existing sidebar layout stays until phase 2.
2. **App shell** — replace the left sidebar with the floating **two-tier top
   bar** (brand · group tabs · target pill · ⌘K · log out; tier 2 = sub-sections
   of the browsed group, active underline, group dot). `app-wrap` max-width
   "breathing" layout.
3. **⌘K command palette** — fuzzy jump-to-section, recent-first, keyboard nav.
4. **Toasts + Command audit** — `ToastProvider` (running→done/failed, auto-
   dismiss, top-right stack); route the existing console-entry calls to toasts +
   the Events "Command audit"; retire the bottom `OutputConsole`.
5. **Tweaks panel + theming** — 3 themes + accent/font/density/motion, persisted
   to localStorage; `applyTweaks` sets the root theme class + CSS vars; gear in
   the top bar.
6. **Interaction primitives** — springy `Button` (variants + 0→1 ripple + busy
   spinner), `StepperInput` (all number fields), `xswitch` toggle; moderation
   button color-coding + offline-gated Kick.
7. **Unified player picker** — search + All/Online/Offline filter chips + avatar
   rows (Steam persona/status/position); modal on command tabs, inline table on
   Roster; the target pill opens it. Reused everywhere.
8. **Per-tab polish** — Overview server banner + single KPI strip (de-duped),
   stat tiles; Inventory containers (Equipment/Toolbar/Backpack/Wallet); Items
   tier+rarity filters + durability slider; instance/role badges parity; SVG
   icon set replacing emoji.

## Constraints

- Reuse existing tab components + backend wiring unchanged; this is presentation.
- Gate: `npm --prefix web run build` (`tsc --noEmit && vite build`). No eslint /
  test runner in `web/`.
- Deploy per phase: `tar | ssh docker cp` `data/web/dist` → `/home/container` on
  server 30 (5e0975e8); verify served bundle.
- Keep the live auto-refresh (Live toggle) and all existing behavior working.
