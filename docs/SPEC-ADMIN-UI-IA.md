# SPEC — Admin UI Information Architecture redesign

Status: **proposed** · Scope: full redesign (22 → 14 tabs) · Owner-approved direction
(2026-06-07). Builds on the readability pass (`web/src/mapNames.ts`, PR #61).

## Context / problem

The admin SPA (`web/src`) has grown to **22 flat tabs in 3 groups** —
`overview` (4) · `commands` (**12 — a junk drawer**) · `system` (6). A
5-agent IA audit confirmed three structural problems:

1. **Overlap / duplication.** Dashboard, Status, Instances, and Autoscaler all
   re-render server + player counts. Status is pure read-only telemetry already
   present (with more detail) inside Instances.
2. **Scattered mental models.** Five tabs implement "pick a target player → act"
   (Players, Player Editor, Skills, Inventory, Movement). Three implement "give
   items" (Items, Kits, Welcome). Spice tuning is *hidden* — rendered inside Loot.
3. **Jargon / cognitive load.** "Replicas" (k8s), "FLS", raw ISO timestamps,
   "Browse", "Market bot", "safe snap", inventory enum codes, and status pills
   with no tooltips.

**Goal:** cleaner, more approachable for **non-technical operators**, while
**keeping maximum control + information**. Not a visual/CSS redesign (handled
separately with Claude Design). Every consolidation uses **sub-tabs / accordions**
— no control or data is removed, only the number of top-level entry points.

## Target IA — 22 → 14 tabs, 4 groups

| Group | Tab | Origin |
|---|---|---|
| **🌍 Fleet** | **Overview** (home) | merge **Dashboard + Status** |
| | Instances | keep (Status grid folds in as a compact "Fleet health" strip) |
| | Autoscaler | keep |
| | Live Map | keep |
| **👥 Players** | **Players** (one workspace) | merge **Players + Player Editor + Skills + Inventory + Movement** → sub-tabs **Roster · Character · Skills · Inventory · Teleport**, shared target picker pinned on top |
| **🎁 Items & Economy** | **Give Items** | merge **Items + Kits + Welcome** → sub-tabs **Single item · Bundles · New-player kit** |
| | Market | keep (relabel "Catalog seeding") |
| | Loot & Difficulty | **Loot** with Spice removed |
| | **Spice Economy** | **extracted** out of Loot into its own tab |
| **🛠 Server** | Settings | **Core Rules / Advanced** sub-tabs |
| | Send Message | rename of **Broadcast** |
| | Shutdown & Restart | rename of **Maintenance** |
| | Scheduler | keep (restructure into clearer sections) |
| | **Events & Diagnostics** | merge **Logs + History** → sub-tabs **Command audit · Service logs** |

Everyday-mod tasks (who's online, manage a player, give items, send a message)
become 2 clicks; power-admin density (190 settings, partitions, cron, cvars) is
tucked behind sub-tabs / Advanced — present, never lost.

## Merge specs (control-preserving)

- **Overview ← Dashboard + Status.** New read-only home: fleet-health summary
  (the Status grid, relabeled) + online-player roster (Dashboard table, with
  location + relative last-activity) + a quick "Send message" form. Status and
  Dashboard top-level tabs removed; Instances keeps its own detail.
- **Players workspace ← Players + Player Editor + Skills + Inventory + Movement.**
  One tab, persistent `TargetProvider` picker on top, sub-tabs: **Roster**
  (online/offline list, kick/ban/moderation), **Character** (currency, XP,
  faction, identity, keystones, tech, danger ops), **Skills**, **Inventory**
  (view/delete stacks), **Teleport** (named/coord teleport). Each sub-tab is the
  existing component, unchanged in capability.
- **Give Items ← Items + Kits + Welcome.** Sub-tabs: **Single item** (ad-hoc
  give), **Bundles** (kits + custom-kit editor), **New-player kit** (welcome
  auto-grant config + scan/retry).
- **Events & Diagnostics ← Logs + History.** Sub-tabs: **Command audit**
  (History, searchable) · **Service logs** (Logs viewer + per-service restart +
  service glossary). Scheduler keeps its own run-history.
- **Spice extracted from Loot.** New top-level **Spice Economy** tab; Loot becomes
  **Loot & Difficulty** (loot rules only, grouped by theme).
- **Settings → Core Rules / Advanced.** Sub-tabs: **Core Rules** = ~20–30 most
  common toggles (game mode, PvP/PvE, harvest, spice yield, …) front and center;
  **Advanced** = the remaining ~160 settings (current full list). No setting
  dropped; backend unchanged.

## Legibility / label pass (cross-cutting, independent of merges)

| Where | Before → After |
|---|---|
| Status / Instances | "Replicas" → **"Instances (running / wanted)"**; tooltips on Cold/Starting/Ready |
| Dashboard / roster | "FLS" → **"Player ID"** (tooltip); ISO timestamp → **"2h ago"** |
| Inventory | container enums → **Backpack / Worn gear / Bank / …** (extend existing `INV_TYPE_LABELS`) |
| Items | "Browse" → **"Item catalog"**; "Rarity" → **"Blueprint type"** |
| Market | "Market bot" → **"Catalog seeding"**; simplify gamble-buy copy |
| Spice | "a" / "p" → **"Active cap" / "Primed cap"** |
| Movement | "safe snap" → **"Safe landing (snaps to walkable floor)"** |
| Broadcast | header → **"Send server message"**; "duration" → **"How long to show (s)"** + char counter |
| Maintenance | expand Restart / Maintenance / Update type explanations |
| Logs | add a one-line **service glossary** (admin-http, scheduler, market-bot, …) |
| Autoscaler | **"unsaved changes"** indicator before Save |

## Phasing (PR per phase, each independently shippable)

1. **Spec** (this doc). ← *current PR*
2. **Nav regroup + label pass** — `App.tsx` re-clustered into the 4 groups +
   global renames + the cross-cutting label/tooltip fixes. Tabs still standalone,
   just regrouped + de-jargoned. (May split into 2 PRs to respect ≤5 files/phase.)
3. **Give Items workspace** (Items + Kits + Welcome).
4. **Events & Diagnostics** (Logs + History) + **Spice extraction** + **Loot**
   rename.
5. **Overview** (Dashboard + Status).
6. **Settings** Core / Advanced split.
7. **Players workspace** (the big lift: 5 tabs → sub-tabs).

## Constraints & conventions

- **Keep all controls + info** — merges are sub-tabs/accordions; nothing removed.
- **No visual/CSS redesign** — structure, naming, and grouping only.
- Reuse the shared `TargetProvider`/`TargetPill` (target.tsx), `OutputConsole`,
  `Confirm`, `PlayerPicker` (components.tsx), and `mapNames.ts`.
- `tabs.tsx` (~3.3k lines) shrinks as merged components move into focused
  workspace files (`PlayersWorkspace.tsx`, `GiveItemsTab.tsx`, `EventsTab.tsx`,
  `OverviewTab.tsx`).
- **Gate:** `npm --prefix web run build` (`tsc --noEmit && vite build`) clean.
  No eslint / JS test runner configured for `web/` — tsc is the type gate
  (strict + noUnusedLocals + noUnusedParameters).
- **Deploy** (per phase, on request): build → `tar | ssh docker cp -` the
  `data/web/dist` into `/home/container/data/web/dist` on server 30; reach the UI
  via the container netns (port 8090). `data/web/dist` is git-tracked.
- **Per phase:** branch, build, verify, PR `--merge` + delete branch. No
  Co-Authored-By trailer.
