// Single source of truth for turning raw UE5/engine world identifiers — the
// dune.world_partition `map` column (e.g. "DeepDesert_1", "SH_Arrakeen",
// "CB_Dungeon_OldCarthag") — into operator-friendly names, roles, and a unified
// instance-state badge that every admin tab shares.
//
// Friendly names mirror the world_partition seed in scripts/prestart.sh and
// scripts/multi-sietch-config.sh. Keep this table in sync if that seed changes.

// ---- Roles -------------------------------------------------------------

export type MapRole =
  | "hub" // the shared open-world Overland map
  | "sietch" // Survival_1 — player-base sietches (Hagga Basin)
  | "on_demand" // SSS overflow cities scaled up/down by the autoscaler
  | "story" // story-mission instances
  | "dungeon" // dungeon instances
  | "ecolab" // ecological station instances
  | "overland" // overland sub-instances
  | "other"; // anything not yet categorised

export interface MapRoleMeta {
  /** Group / role label shown to operators. */
  title: string;
  /** Emoji marker for the group header. */
  icon: string;
  /** One-line "what is this" description for operators. */
  blurb: string;
  /** Sort weight — lower sorts earlier. Operational roles lead. */
  order: number;
  /** Tailwind classes for the small role chip. */
  chip: string;
  /** True ⇒ folds into the collapsed "Story & Dungeons" section. */
  secondary: boolean;
}

export const MAP_ROLE_META: Record<MapRole, MapRoleMeta> = {
  hub: { title: "Overland", icon: "🗺", blurb: "The shared open-world map every player roams.", order: 0, chip: "bg-sky-900/50 text-sky-300", secondary: false },
  sietch: { title: "Sietches", icon: "🏠", blurb: "Player-base sietches in Hagga Basin.", order: 1, chip: "bg-violet-900/50 text-violet-300", secondary: false },
  on_demand: { title: "On-demand city", icon: "🏙", blurb: "Overflow cities; the autoscaler-managed ones (Deep Desert, Arrakeen, Harko Village) have spin-up / scale controls.", order: 2, chip: "bg-orange-900/50 text-orange-300", secondary: false },
  story: { title: "Story", icon: "📖", blurb: "Story-mission instances.", order: 3, chip: "bg-indigo-900/50 text-indigo-300", secondary: true },
  dungeon: { title: "Dungeon", icon: "⚔", blurb: "Dungeon instances.", order: 4, chip: "bg-rose-900/50 text-rose-300", secondary: true },
  ecolab: { title: "Ecolab", icon: "🧪", blurb: "Ecological station instances.", order: 5, chip: "bg-teal-900/50 text-teal-300", secondary: true },
  overland: { title: "Overland sub-map", icon: "🏜", blurb: "Overland sub-instances.", order: 6, chip: "bg-amber-900/50 text-amber-300", secondary: true },
  other: { title: "Other", icon: "•", blurb: "Uncategorised instance.", order: 7, chip: "bg-slate-700 text-slate-300", secondary: true },
};

// On-demand overflow cities scaled via mock-k8s ServerSetScale.
const ON_DEMAND = new Set(["DeepDesert_1", "SH_Arrakeen", "SH_HarkoVillage", "SH_FallenLight"]);

export function mapRole(map: string): MapRole {
  if (map === "Overmap") return "hub";
  if (map === "Survival_1") return "sietch";
  if (ON_DEMAND.has(map)) return "on_demand";
  // Order matters: a CB_Story_Ecolab_* map is a story mission, not an ecolab.
  if (/story/i.test(map)) return "story";
  if (/dungeon/i.test(map)) return "dungeon";
  if (/ecolab/i.test(map)) return "ecolab";
  if (/overland/i.test(map)) return "overland";
  return "other";
}

// ---- Friendly names ----------------------------------------------------

// Raw map id -> friendly name. Mirrors the dune.world_partition seed
// (scripts/prestart.sh) plus SH_FallenLight (scripts/multi-sietch-config.sh).
export const MAP_NAMES: Record<string, string> = {
  Survival_1: "Hagga Basin",
  Overmap: "Overland",
  SH_Arrakeen: "Arrakeen",
  SH_HarkoVillage: "Harko Village",
  SH_FallenLight: "Fallen Light",
  DeepDesert_1: "Deep Desert",
  CB_Story_Hephaestus: "Hephaestus",
  CB_Story_Ecolab_Carthag: "Carthag Ecolab",
  CB_Story_WaterFatManor: "WaterFat Manor",
  Story_ProcesVerbal: "Proces Verbal",
  DLC_Story_LostHarvest_EcolabA: "Lost Harvest Ecolab A",
  DLC_Story_LostHarvest_EcolabB: "Lost Harvest Ecolab B",
  DLC_Story_LostHarvest_ForgottenLab: "Forgotten Lab",
  Story_ArtOfKanly: "Art of Kanly",
  CB_Dungeon_Hephaestus: "Hephaestus Dungeon",
  CB_Dungeon_OldCarthag: "Old Carthag",
  Story_Faction_Outpost_Atre: "Atreides Outpost",
  Story_Faction_Outpost_Hark: "Harkonnen Outpost",
  Story_HeighlinerDungeon: "Heighliner",
  CB_Ecolab_Bronze_Green_089: "Ecolab 089",
  CB_Ecolab_Bronze_Green_152: "Ecolab 152",
  CB_Ecolab_Bronze_Green_024: "Ecolab 024",
  CB_Ecolab_Bronze_Green_195: "Ecolab 195",
  CB_Ecolab_Bronze_Green_136: "Ecolab 136",
  CB_Overland_M_01: "Overland M01",
  CB_Overland_S_04: "Overland S04",
  CB_Overland_S_06: "Overland S06",
  CB_Overland_S_07: "Overland S07",
  CB_Story_BanditFortress01: "Bandit Fortress",
};

// Fallback humaniser for maps not in the table (future content). Strips engine
// + category prefixes, then turns separators into spaces.
function humanizeMap(map: string): string {
  let s = map.replace(/^(DLC_|CB_|SH_)/, "");
  s = s.replace(/^(Story_|Dungeon_|Ecolab_|Overland_|Faction_Outpost_)/, "");
  s = s.replace(/_/g, " ").replace(/\s+/g, " ").trim();
  return s || map;
}

export function mapDisplayName(map: string): string {
  if (!map) return "(unknown)";
  return MAP_NAMES[map] ?? humanizeMap(map);
}

// ---- Unified instance-state badge --------------------------------------
//
// One helper for the cold / starting / live / parked state of a partition,
// ported from InstancesTab.partRow so every tab renders it identically.

export type InstanceTone = "live" | "parked" | "starting" | "cold";

export interface InstanceStateView {
  state: InstanceTone;
  /** Badge text. */
  label: string;
  /** Tailwind classes for the pill. */
  badge: string;
  /** Tailwind classes for the status dot. */
  dot: string;
  /** Tooltip explaining the state in plain language. */
  tip: string;
  /** True while booting — drives the post-action settle-burst polling. */
  transitional: boolean;
}

export function instanceState(p: {
  parked?: boolean | null;
  ready?: boolean | null;
  server_id?: string | null;
}): InstanceStateView {
  if (p.parked) {
    return {
      state: "parked",
      label: "parked · data kept",
      badge: "bg-violet-900/50 text-violet-300",
      dot: "bg-violet-400",
      tip: "Parked — paused but all builds & data kept (survives reboot). Unpark to restore.",
      transitional: false,
    };
  }
  if (p.server_id && p.ready) {
    return {
      state: "live",
      label: "live",
      badge: "bg-emerald-900/50 text-emerald-300",
      dot: "bg-emerald-400",
      tip: "Live — running and accepting players.",
      transitional: false,
    };
  }
  if (p.server_id) {
    return {
      state: "starting",
      label: "starting",
      badge: "bg-amber-900/50 text-amber-300",
      dot: "bg-amber-400",
      tip: "Starting — registered and booting (not ready yet, ~40s for a cold UE5).",
      transitional: true,
    };
  }
  return {
    state: "cold",
    label: "cold",
    badge: "bg-slate-700 text-slate-300",
    dot: "bg-slate-600",
    tip: "Cold — declared but no instance running.",
    transitional: false,
  };
}

// mock-k8s map-level health (healthy / starting / failing / idle) — distinct
// from per-partition state above. Unifies the duplicated statusPill helpers.
export function mapStatusPill(status?: string): string {
  switch (status) {
    case "healthy":
      return "pill-ok";
    case "failing":
      return "pill-err";
    default:
      return "pill-warn"; // starting / idle / unknown
  }
}

// ---- Dimension labels --------------------------------------------------
//
// dim 0 = the always-warm landing zone; Survival_1 dim>0 = a player sietch;
// on-demand dim>0 = a per-player sandstorm tunnel.

export function dimensionBadge(map: string, dimension: number): { label: string; tip: string } {
  if (dimension === 0) return { label: "warm", tip: "Always-warm landing zone (dimension 0)." };
  if (map === "Survival_1") return { label: `sietch ${dimension}`, tip: "Player-choosable sietch (a Survival_1 dimension partition)." };
  return { label: `tunnel ${dimension}`, tip: "Per-player sandstorm-tunnel partition." };
}
