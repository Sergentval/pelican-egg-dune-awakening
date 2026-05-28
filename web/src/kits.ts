// Pre-defined item-grant bundles. The Funcom seabass handler has no
// direct heal / hydrate / set-health command (verified by exhaustive
// probing — see wiki/syntheses/dune-rmq-admin-protocol.md "Resolution
// Part 3"), so these compose AddItemToInventory calls to achieve the
// same UX. Stored as plain data; the SPA fires each line as a
// separate /admin/give call sequentially.

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
  lines: KitLine[];
  /** Hidden by default — show via "edit / advanced" toggle. */
  destructive?: boolean;
}

export const BUILT_IN_KITS: Kit[] = [
  {
    id: "heal-mk6",
    name: "Heal kit",
    emoji: "❤️‍🩹",
    blurb: "5× top-tier Healkit + 3× Massive Blood Sack — full health restore with backup.",
    lines: [
      { id: "HealthPack_Channeled_4", name: "Healkit Mk6", qty: 5 },
      { id: "Bloodsack_T6", name: "Massive Blood Sack", qty: 3 },
    ],
  },
  {
    id: "hydrate",
    name: "Hydrate",
    emoji: "💧",
    blurb: "Mk6 Hajra Literjon + 5 Cup of Water + 1 Stilltent — covers a long surface trip.",
    lines: [
      { id: "HighCapacityLiterjon_06", name: "Hajra Literjon Mk6", qty: 1 },
      { id: "WaterPack_Consumable", name: "Cup of Water", qty: 5 },
      { id: "Stilltent", name: "Stilltent", qty: 1 },
    ],
  },
  {
    id: "welcome",
    name: "Welcome kit",
    emoji: "🎁",
    blurb: "Starter set for a fresh arrival: water, basic heal, a Stilltent, 50 spice.",
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
    blurb: "All four melange consumables for a Bene-Gesserit-y evening.",
    lines: [
      { id: "SpiceAddictionConsumable_01", name: "Melange Spiced Food", qty: 5 },
      { id: "SpiceAddictionConsumable_02", name: "Melange Spiced Beer", qty: 5 },
      { id: "SpiceAddictionConsumable_03", name: "Melange Spiced Coffee", qty: 5 },
      { id: "SpiceAddictionConsumable_04", name: "Melange Spiced Wine", qty: 5 },
      { id: "SpiceAddictionConsumable_T6", name: "Melange Spiced Liquor", qty: 2 },
    ],
  },
  {
    id: "armor-swordmaster",
    name: "Swordmaster armor",
    emoji: "⚔️",
    blurb: "Full Swordmaster light armor set in one click.",
    lines: [{ id: "ArmorPack_Swordmaster", name: "Swordmaster Armor Pack", qty: 1 }],
  },
  {
    id: "armor-benegesserit",
    name: "Bene Gesserit armor",
    emoji: "✨",
    blurb: "Bene Gesserit armor set.",
    lines: [{ id: "ArmorPack_BeneGeserit", name: "Bene Gesserit Armor Pack", qty: 1 }],
  },
  {
    id: "armor-mentat",
    name: "Mentat armor",
    emoji: "🧠",
    blurb: "Mentat light armor set.",
    lines: [{ id: "ArmorPack_Mentat", name: "Mentat Armor Pack", qty: 1 }],
  },
  {
    id: "armor-trooper",
    name: "Trooper armor",
    emoji: "🪖",
    blurb: "Trooper armor set.",
    lines: [{ id: "ArmorPack_Trooper", name: "Trooper Armor Pack", qty: 1 }],
  },
  {
    id: "armor-planetologist",
    name: "Planetologist armor",
    emoji: "🌍",
    blurb: "Planetologist light armor set.",
    lines: [{ id: "ArmorPack_Planetologist", name: "Planetologist Armor Pack", qty: 1 }],
  },
  {
    id: "builder-basic",
    name: "Builder kit (basic)",
    emoji: "🏗",
    blurb: "Copper-tier base building materials pack.",
    lines: [{ id: "BasePack_Copper", name: "Basic Building Materials", qty: 1 }],
  },
  {
    id: "builder-std",
    name: "Builder kit (standard)",
    emoji: "🏗",
    blurb: "Iron-tier base building materials pack.",
    lines: [{ id: "BasePack_Iron", name: "Standard Building Materials", qty: 1 }],
  },
  {
    id: "builder-spec",
    name: "Builder kit (specialized)",
    emoji: "🏗",
    blurb: "Steel-tier base building materials pack.",
    lines: [{ id: "BasePack_Steel", name: "Specialized Building Materials", qty: 1 }],
  },
  {
    id: "spice-cache",
    name: "Spice cache",
    emoji: "💰",
    blurb: "1000 raw spice + 1 Solari coin (currency token).",
    lines: [
      { id: "MelangeSpice", name: "Spice Melange", qty: 1000 },
      { id: "SolarisCoin", name: "Solari Coin", qty: 1 },
    ],
  },
];

export interface CustomKit extends Kit {
  custom: true;
}

const CUSTOM_KEY = "dune-admin-custom-kits";

export function loadCustomKits(): CustomKit[] {
  try {
    const raw = localStorage.getItem(CUSTOM_KEY);
    return raw ? (JSON.parse(raw) as CustomKit[]) : [];
  } catch {
    return [];
  }
}

export function saveCustomKits(kits: CustomKit[]): void {
  localStorage.setItem(CUSTOM_KEY, JSON.stringify(kits));
}
