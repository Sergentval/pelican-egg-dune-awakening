// Pre-defined item-grant bundles. The Funcom seabass handler has no
// direct heal / hydrate / set-health command (verified by exhaustive
// probing — see wiki/syntheses/dune-rmq-admin-protocol.md "Resolution
// Part 3"), so these compose AddItemToInventory calls to achieve the
// same UX. Stored as plain data; the SPA fires each line as a
// separate /admin/give call sequentially.
//
// NOTE: `ArmorPack_<Class>` items in the item catalog are the *schematic
// permit* — they grant the crafting recipe, not the wearable armor.
// To give the actual armor we list each piece individually
// (Boots / Gloves / Helmet / Bottom / Top).

export interface KitLine {
  id: string;
  name: string;
  qty: number;
  durability?: number;
}

export interface Kit {
  id: string;
  name: string;
  emoji: string;
  blurb: string;
  /** Visual category in the Kits tab — groups built-ins. */
  group: "survival" | "armor" | "weapons" | "tools" | "loot";
  lines: KitLine[];
}

export const BUILT_IN_KITS: Kit[] = [
  // ── Survival ──────────────────────────────────────────────────────
  {
    id: "heal-mk6",
    name: "Heal kit",
    emoji: "❤️‍🩹",
    blurb: "Top-tier consumables to instantly restore health when used.",
    group: "survival",
    lines: [
      { id: "HealthPack_Channeled_4", name: "Healkit Mk6", qty: 5 },
      { id: "Bloodsack_T6", name: "Massive Blood Sack", qty: 3 },
      { id: "AntiRadiationPill", name: "Iodine Pill (Anti-Radiation)", qty: 5 },
    ],
  },
  {
    id: "hydrate",
    name: "Hydrate",
    emoji: "💧",
    blurb: "Long surface trip: top-tier Literjon + cup of water + Stilltent.",
    group: "survival",
    lines: [
      { id: "HighCapacityLiterjon_06", name: "Hajra Literjon Mk6", qty: 1 },
      { id: "WaterPack_Consumable", name: "Cup of Water", qty: 10 },
      { id: "Stilltent", name: "Stilltent", qty: 1 },
    ],
  },
  {
    id: "welcome",
    name: "Welcome kit",
    emoji: "🎁",
    blurb: "Fresh arrival starter: water + heal + Stilltent + 50 spice.",
    group: "survival",
    lines: [
      { id: "Literjon", name: "Literjon", qty: 1 },
      { id: "WaterPack_Consumable", name: "Cup of Water", qty: 5 },
      { id: "HealthPack_Channeled", name: "Healkit", qty: 3 },
      { id: "Stilltent", name: "Stilltent", qty: 1 },
      { id: "MelangeSpice", name: "Spice Melange", qty: 50 },
    ],
  },
  {
    id: "spice-night",
    name: "Spice consumables",
    emoji: "🧂",
    blurb: "All five melange consumables for a Bene-Gesserit-style evening.",
    group: "survival",
    lines: [
      { id: "SpiceAddictionConsumable_01", name: "Melange Spiced Food", qty: 3 },
      { id: "SpiceAddictionConsumable_02", name: "Melange Spiced Beer", qty: 3 },
      { id: "SpiceAddictionConsumable_03", name: "Melange Spiced Coffee", qty: 3 },
      { id: "SpiceAddictionConsumable_04", name: "Melange Spiced Wine", qty: 3 },
      { id: "SpiceAddictionConsumable_T6", name: "Melange Spiced Liquor", qty: 1 },
    ],
  },

  // ── Armor — generated dynamically from /api/lookup/armor-sets, see
  //          KitsTab. Removed from this static list to avoid drift.

  // ── Weapons ────────────────────────────────────────────────────────
  {
    id: "weapons-melee",
    name: "Melee bundle",
    emoji: "🗡️",
    blurb: "Crysknife + top-tier sword + Regis dirk.",
    group: "weapons",
    lines: [
      { id: "Crysknife_CR", name: "Crysknife", qty: 1 },
      { id: "CHOAMSword_3", name: "Regis Sword", qty: 1 },
      { id: "Dirk_3", name: "Regis Dirk", qty: 1 },
    ],
  },
  {
    id: "weapons-ranged",
    name: "Ranged bundle (top tier)",
    emoji: "🔫",
    blurb: "Regis pistol + SMG + scattergun + glasser flamethrower.",
    group: "weapons",
    lines: [
      { id: "HarkHeavyPistol5", name: "Regis Rafiq Snubnose", qty: 1 },
      { id: "AtreSmg6", name: "Regis Disruptor M11", qty: 1 },
      { id: "Scattergun_Prototype3", name: "Regis GRDA 44", qty: 1 },
      { id: "Flamethrower_Prototype_2", name: "Glasser Flamethrower", qty: 1 },
    ],
  },
  {
    id: "weapons-ranged-with-ammo",
    name: "Ranged + ammo loadout",
    emoji: "🎯",
    blurb: "Top-tier ranged weapons with matching ammo stockpile. Tweak the dart/rocket counts before granting.",
    group: "weapons",
    lines: [
      { id: "HarkHeavyPistol5", name: "Regis Rafiq Snubnose", qty: 1 },
      { id: "AtreSmg6", name: "Regis Disruptor M11", qty: 1 },
      { id: "Scattergun_Prototype3", name: "Regis GRDA 44", qty: 1 },
      { id: "Flamethrower_Prototype_2", name: "Glasser Flamethrower", qty: 1 },
      { id: "Ammo", name: "Light Darts", qty: 500 },
      { id: "HeavyAmmo", name: "Heavy Darts", qty: 200 },
      { id: "RocketAmmo", name: "Rocket", qty: 50 },
      { id: "InfantryRocketAmmo", name: "Missile", qty: 50 },
    ],
  },
  {
    id: "ammo-only",
    name: "Ammo restock",
    emoji: "📦",
    blurb: "Just ammo. Adjust counts to match what your players need.",
    group: "weapons",
    lines: [
      { id: "Ammo", name: "Light Darts", qty: 1000 },
      { id: "HeavyAmmo", name: "Heavy Darts", qty: 500 },
      { id: "RocketAmmo", name: "Rocket", qty: 100 },
      { id: "InfantryRocketAmmo", name: "Missile", qty: 100 },
    ],
  },
  {
    id: "weapons-stockpile",
    name: "Weapons stockpile",
    emoji: "💥",
    blurb: "One of every top-tier weapon — melee + ranged combined.",
    group: "weapons",
    lines: [
      { id: "Crysknife_CR", name: "Crysknife", qty: 1 },
      { id: "CHOAMSword_3", name: "Regis Sword", qty: 1 },
      { id: "Dirk_3", name: "Regis Dirk", qty: 1 },
      { id: "HarkHeavyPistol5", name: "Regis Rafiq Snubnose", qty: 1 },
      { id: "AtreSmg6", name: "Regis Disruptor M11", qty: 1 },
      { id: "Scattergun_Prototype3", name: "Regis GRDA 44", qty: 1 },
      { id: "Flamethrower_Prototype_2", name: "Glasser Flamethrower", qty: 1 },
    ],
  },

  // ── Tools ─────────────────────────────────────────────────────────
  {
    id: "tools-basic",
    name: "Basic toolkit",
    emoji: "🧰",
    blurb: "Cutteray Mk3 + Welding Torch Mk3 + Thumper + Binoculars.",
    group: "tools",
    lines: [
      { id: "MiningTool_2h_Standard", name: "Cutteray Mk3", qty: 1 },
      { id: "RepairTool3", name: "Welding Torch Mk3", qty: 1 },
      { id: "Thumper", name: "Thumper", qty: 1 },
      { id: "Binoculars_1", name: "Binoculars", qty: 1 },
    ],
  },
  {
    id: "tools-mining",
    name: "Mining toolkit (top tier)",
    emoji: "⛏️",
    blurb: "Cutteray Mk6 (Advanced) + Welding Mk5 + Omni Static Compactor.",
    group: "tools",
    lines: [
      { id: "MiningTool_2h_Advanced", name: "Cutteray Mk6", qty: 1 },
      { id: "MiningTool_2h_Light", name: "Cutteray Mk5", qty: 1 },
      { id: "RepairTool5", name: "Welding Torch Mk5", qty: 1 },
      { id: "StaticCompactorTier6", name: "Omni Static Compactor", qty: 1 },
    ],
  },
  {
    id: "tools-cutteray-all",
    name: "Cutteray collection",
    emoji: "⛏️",
    blurb: "Every Cutteray tier Funcom ships — Improvised through Mk6.",
    group: "tools",
    lines: [
      { id: "MiningTool_1h_Standard", name: "Improvised Cutteray", qty: 1 },
      { id: "MiningTool_1h_Heavy", name: "Cutteray Mk1", qty: 1 },
      { id: "MiningTool_1h_Light", name: "Cutteray Mk2", qty: 1 },
      { id: "MiningTool_2h_Standard", name: "Cutteray Mk3", qty: 1 },
      { id: "MiningTool_2h_Heavy", name: "Cutteray Mk4", qty: 1 },
      { id: "MiningTool_2h_Light", name: "Cutteray Mk5", qty: 1 },
      { id: "MiningTool_2h_Advanced", name: "Cutteray Mk6", qty: 1 },
    ],
  },
  {
    id: "tools-welding-all",
    name: "Welding torches (all tiers)",
    emoji: "🔥",
    blurb: "Welding Torch Mk1 / Mk3 / Mk5 — every tier in the catalogue.",
    group: "tools",
    lines: [
      { id: "RepairTool", name: "Welding Torch Mk1", qty: 1 },
      { id: "RepairTool3", name: "Welding Torch Mk3", qty: 1 },
      { id: "RepairTool5", name: "Welding Torch Mk5", qty: 1 },
    ],
  },
  {
    id: "tools-surveyor",
    name: "Surveyor kit",
    emoji: "🛰️",
    blurb: "Probe launcher + 20 probes + Thumper for spice-blow detection.",
    group: "tools",
    lines: [
      { id: "SurveyProbeLauncher", name: "Survey Probe Launcher", qty: 1 },
      { id: "SurveyProbe_1", name: "Survey Probe", qty: 20 },
      { id: "Thumper", name: "Thumper", qty: 1 },
    ],
  },
  {
    id: "tools-builder",
    name: "Builder repair kit",
    emoji: "🔧",
    blurb: "Welding Torch + Base + Vehicle reconstruction tools + Construction Tool.",
    group: "tools",
    lines: [
      { id: "RepairTool5", name: "Welding Torch Mk5", qty: 1 },
      { id: "BasicBuildingTool", name: "Construction Tool", qty: 1 },
      { id: "BaseBackupTool", name: "Base Reconstruction Tool", qty: 1 },
      { id: "VehicleBackupTool", name: "Vehicle Backup Tool", qty: 1 },
    ],
  },
  {
    id: "tools-shields-all",
    name: "Holtzman shields (all tiers)",
    emoji: "🛡️",
    blurb: "Every Holtzman personal shield Funcom ships — Mk2 / Mk3 / Mk5 + Adaptive unique.",
    group: "tools",
    lines: [
      { id: "HoltzmanShieldActiveDrain", name: "Holtzman Shield Mk2", qty: 1 },
      { id: "HoltzmanShieldActiveDrain2", name: "Holtzman Shield Mk3", qty: 1 },
      { id: "HoltzmanShieldActiveDrain3", name: "Holtzman Shield Mk5", qty: 1 },
      { id: "HoltzmanShieldActiveDrain_Unique_01", name: "Adaptive Holtzman Shield", qty: 1 },
    ],
  },
  {
    id: "tools-powerpacks-all",
    name: "Power packs (all tiers)",
    emoji: "🔋",
    blurb: "Improvised + Mk1-Mk6 power pack. Naming convention in Funcom's catalogue is jumbled — each one labelled by display name.",
    group: "tools",
    lines: [
      { id: "PowerPack", name: "Improvised Power Pack", qty: 1 },
      { id: "PowerPack5", name: "Power Pack Mk1", qty: 1 },
      { id: "PowerPack2", name: "Power Pack Mk2", qty: 1 },
      { id: "PowerPack6", name: "Power Pack Mk3", qty: 1 },
      { id: "PowerPack3", name: "Power Pack Mk4", qty: 1 },
      { id: "PowerPack7", name: "Power Pack Mk5", qty: 1 },
      { id: "PowerPack4", name: "Power Pack Mk6", qty: 1 },
    ],
  },
  {
    id: "tools-powerpacks-unique",
    name: "Unique power packs",
    emoji: "⚡",
    blurb: "Old Sparky Mk1-Mk4 + Young Sparky Mk5 + Accelerator + Purifier.",
    group: "tools",
    lines: [
      { id: "PowerPack_Unique_Regen_01", name: "Old Sparky Mk1", qty: 1 },
      { id: "PowerPack_Unique_Regen_02", name: "Old Sparky Mk2", qty: 1 },
      { id: "PowerPack_Unique_Regen_03", name: "Old Sparky Mk3", qty: 1 },
      { id: "PowerPack_Unique_Regen_04", name: "Old Sparky Mk4", qty: 1 },
      { id: "PowerPack_Unique_Regen_05", name: "Young Sparky Mk5", qty: 1 },
      { id: "PowerPack_Unique_Capacity_06", name: "Accelerator Power Pack", qty: 1 },
      { id: "PowerPack_Unique_PoisRadMitigation_06", name: "Purifier Power Pack", qty: 1 },
    ],
  },
  {
    id: "tools-glide-belts",
    name: "Glide belts (Emperor's Wings, all tiers)",
    emoji: "🪂",
    blurb: "The Emperor's Wings Mk1 through Mk6 — every glide stabilization belt tier.",
    group: "tools",
    lines: [
      { id: "GlidePartialStabilizationBelt", name: "The Emperor's Wings Mk1", qty: 1 },
      { id: "GlidePartialStabilizationBelt_02", name: "The Emperor's Wings Mk2", qty: 1 },
      { id: "GlidePartialStabilizationBelt_03", name: "The Emperor's Wings Mk3", qty: 1 },
      { id: "GlidePartialStabilizationBelt_04", name: "The Emperor's Wings Mk4", qty: 1 },
      { id: "GlidePartialStabilizationBelt_05", name: "The Emperor's Wings Mk5", qty: 1 },
      { id: "GlidePartialStabilizationBelt_06", name: "The Emperor's Wings Mk6", qty: 1 },
    ],
  },
  {
    id: "tools-stasis-belts",
    name: "Suspensor / stasis belts",
    emoji: "🧘",
    blurb: "Leap, Full, Planar, Responsive-Planar + unique Elohim-Class and Sentinel belts.",
    group: "tools",
    lines: [
      { id: "SuspensorBelt", name: "Leap Suspensor Belt", qty: 1 },
      { id: "FullSuspensorBelt", name: "Full Suspensor Belt", qty: 1 },
      { id: "PartialStabilizationBelt", name: "Planar Suspensor Belt", qty: 1 },
      { id: "FullStabilizationBelt", name: "Responsive Planar Suspensor Belt", qty: 1 },
      { id: "FullSuspensorBelt_Unique_Durability", name: "Elohim-Class Suspensor Belt", qty: 1 },
      { id: "FullStabilizationBelt_Unique_DmgReduction", name: "Sentinel Belt", qty: 1 },
    ],
  },
  {
    id: "tools-utility-mega",
    name: "Utility mega kit",
    emoji: "🧰",
    blurb: "Every utility tool the game ships: cutteray top-tier, welding, compactor, probes, staking, stilltent, binoculars, building+repair tools.",
    group: "tools",
    lines: [
      { id: "MiningTool_2h_Advanced", name: "Cutteray Mk6", qty: 1 },
      { id: "RepairTool5", name: "Welding Torch Mk5", qty: 1 },
      { id: "StaticCompactorTier6", name: "Omni Static Compactor", qty: 1 },
      { id: "StakingUnit", name: "Staking Unit", qty: 4 },
      { id: "StakingUnitVertical", name: "Vertical Staking Unit", qty: 4 },
      { id: "SurveyProbeLauncher", name: "Survey Probe Launcher", qty: 1 },
      { id: "SurveyProbe_1", name: "Survey Probe", qty: 20 },
      { id: "Thumper", name: "Thumper", qty: 1 },
      { id: "Stilltent", name: "Stilltent", qty: 1 },
      { id: "Binoculars_1", name: "Binoculars", qty: 1 },
      { id: "BasicBuildingTool", name: "Construction Tool", qty: 1 },
      { id: "BaseBackupTool", name: "Base Reconstruction Tool", qty: 1 },
      { id: "VehicleBackupTool", name: "Vehicle Backup Tool", qty: 1 },
    ],
  },

  // ── Loot / event drops ────────────────────────────────────────────
  {
    id: "builder-basic",
    name: "Builder kit (basic)",
    emoji: "🏗",
    blurb: "Copper-tier base building materials pack.",
    group: "loot",
    lines: [{ id: "BasePack_Copper", name: "Basic Building Materials", qty: 1 }],
  },
  {
    id: "builder-std",
    name: "Builder kit (standard)",
    emoji: "🏗",
    blurb: "Iron-tier base building materials pack.",
    group: "loot",
    lines: [{ id: "BasePack_Iron", name: "Standard Building Materials", qty: 1 }],
  },
  {
    id: "builder-spec",
    name: "Builder kit (specialized)",
    emoji: "🏗",
    blurb: "Steel-tier base building materials pack.",
    group: "loot",
    lines: [{ id: "BasePack_Steel", name: "Specialized Building Materials", qty: 1 }],
  },
  {
    id: "spice-cache",
    name: "Spice cache",
    emoji: "💰",
    blurb: "1000 raw spice + 5 Solari coins.",
    group: "loot",
    lines: [
      { id: "MelangeSpice", name: "Spice Melange", qty: 1000 },
      { id: "SolarisCoin", name: "Solari Coin", qty: 5 },
    ],
  },
  {
    id: "god-kit",
    name: "God kit",
    emoji: "👑",
    blurb: "Top-tier weapons + heal + hydrate + tools — everything in one click.",
    group: "loot",
    lines: [
      // Weapons
      { id: "Crysknife_CR", name: "Crysknife", qty: 1 },
      { id: "CHOAMSword_3", name: "Regis Sword", qty: 1 },
      { id: "HarkHeavyPistol5", name: "Regis Rafiq Snubnose", qty: 1 },
      { id: "AtreSmg6", name: "Regis Disruptor M11", qty: 1 },
      { id: "Scattergun_Prototype3", name: "Regis GRDA 44", qty: 1 },
      // Tools
      { id: "MiningTool_2h_Light", name: "Cutteray Mk5", qty: 1 },
      { id: "RepairTool5", name: "Welding Torch Mk5", qty: 1 },
      { id: "StaticCompactorTier6", name: "Omni Static Compactor", qty: 1 },
      // Survival
      { id: "HighCapacityLiterjon_06", name: "Hajra Literjon Mk6", qty: 1 },
      { id: "Stilltent", name: "Stilltent", qty: 1 },
      { id: "HealthPack_Channeled_4", name: "Healkit Mk6", qty: 10 },
      { id: "Bloodsack_T6", name: "Massive Blood Sack", qty: 5 },
      { id: "AntiRadiationPill", name: "Iodine Pill", qty: 10 },
      // Currency
      { id: "MelangeSpice", name: "Spice Melange", qty: 1000 },
      { id: "SolarisCoin", name: "Solari Coin", qty: 10 },
    ],
  },
];

/** Built-in kits indexed by group, in display order. */
export const KIT_GROUPS: Array<{ id: Kit["group"]; label: string; emoji: string }> = [
  { id: "survival", label: "Survival", emoji: "🌵" },
  { id: "armor", label: "Armor", emoji: "🛡️" },
  { id: "weapons", label: "Weapons", emoji: "⚔️" },
  { id: "tools", label: "Tools", emoji: "🧰" },
  { id: "loot", label: "Loot drops", emoji: "💰" },
];

export interface CustomKit extends Kit {
  custom: true;
}

const CUSTOM_KEY = "dune-admin-custom-kits";

export function loadCustomKits(): CustomKit[] {
  try {
    const raw = localStorage.getItem(CUSTOM_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as CustomKit[];
    // Back-compat: older saved kits don't have a `group` field.
    return parsed.map((k) => ({ ...k, group: k.group ?? "loot" }));
  } catch {
    return [];
  }
}

export function saveCustomKits(kits: CustomKit[]): void {
  localStorage.setItem(CUSTOM_KEY, JSON.stringify(kits));
}
