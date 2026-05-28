// Thin fetch wrapper + types. All endpoints share-origin with the SPA
// when served by admin-http.py, so no base URL is needed.

const TOKEN_KEY = "dune-admin-token";

export const getToken = (): string => localStorage.getItem(TOKEN_KEY) || "";
export const setToken = (token: string): void => {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
};

export interface ApiResponse<T = unknown> {
  ok: boolean;
  status: number;
  body: T | { error?: string; detail?: string; stderr?: string };
}

// Hook for any global on-401 redirect. Set by App.tsx.
let unauthorizedHandler: (() => void) | null = null;
export const onUnauthorized = (fn: () => void): void => {
  unauthorizedHandler = fn;
};

export async function api<T = unknown>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<ApiResponse<T>> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const tok = getToken();
  if (tok) headers["Authorization"] = "Bearer " + tok;
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = text;
  }
  // 401 on an authenticated route means the token is dead (expired or invalid).
  // /api/login itself returns 401 on bad password — handler doesn't fire there
  // because we don't carry a token to that call.
  if (res.status === 401 && tok && unauthorizedHandler && path !== "/api/login") {
    unauthorizedHandler();
  }
  return { ok: res.ok, status: res.status, body: parsed as T };
}

// ---- shared types ----

export interface PublishResult {
  ts: number;
  argv: string[];
  ok: boolean;
  exit_code: number;
  stdout: string;
  stderr: string;
}

export interface PlayerRow {
  fls_id: string;
  character: string | null;
  steam_id: string | null;
  platform_name: string;
  life: string;
  online: string;
  last_avatar_activity: string | null;
}

export interface SteamPersona {
  personaname?: string;
  profileurl?: string;
  avatar?: string;
  avatarmedium?: string;
  avatarfull?: string;
  realname?: string;
  personastate?: number;
  lastlogoff?: number;
}

export interface VehicleClass {
  id: string;
  actor_class: string;
  templates: string[];
}

export interface ItemRow {
  id: string;
  name?: string;
  category?: string;
  source?: string;
}

export interface SkillRow {
  id: string;
  name?: string;
  category?: string;
  maxLevel?: number;
}

export interface HistoryResponse {
  entries: PublishResult[];
  total: number;
}

// ---- convenience wrappers ----

export const login = (password: string): Promise<ApiResponse<{ token: string; expires_in: number; type: string }>> =>
  api("POST", "/api/login", { password });

export const me = () => api<{ authenticated: boolean; session: { exp: number; iat: number; sub: string } }>("GET", "/api/me");

export const fetchPlayers = (filter: "all" | "online" = "all") =>
  api<PublishResult>("GET", `/api/players?filter=${filter}`);

export const fetchPos = (playerId: string) => api<PublishResult>("GET", `/api/pos/${encodeURIComponent(playerId)}`);

export const fetchVehicles = () => api<{ vehicles: VehicleClass[] }>("GET", "/api/lookup/vehicles");

export interface ItemCategoryBucket {
  id: string;
  count: number;
}

export const fetchItems = (q: string, limit = 40, category = "") => {
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  if (category) qs.set("category", category);
  qs.set("limit", String(limit));
  return api<{ items: ItemRow[] }>("GET", `/api/lookup/items?${qs.toString()}`);
};

export const fetchItemCategories = () =>
  api<{ categories: ItemCategoryBucket[] }>("GET", "/api/lookup/item-categories");

/** True when the item id is a Unique blueprint variant — Funcom marks
 *  them with `_Unique_` (most categories) or `Unique` as a prefix on
 *  some weapon families (e.g. `UniqueAr1`, `UniqueSword_02`). Uniques
 *  get amber styling in the Items grid + Kits armor section. */
export function isUniqueItem(itemId: string): boolean {
  if (!itemId) return false;
  return /_Unique_|^Unique[A-Z0-9]/i.test(itemId);
}

/** Best-effort tier extraction from a Funcom FName.
 *
 *  Suffix conventions Funcom ships in DT_ItemTemplates:
 *    - `_T6` / `_T<N>`        explicit tier suffix
 *    - `_Mk<N>` / `Mk<N>`     mark number (most consumables)
 *    - trailing `_<N>` digit  0..6 tier on weapons (0=Artisan ... 5=Regis)
 *    - `_Unique_<name>...`   unique variants — return "Unique"
 *
 *  Returns a short label ("T6", "Mk5", "Unique", "—") suitable for a pill.
 */
export function detectTier(itemId: string): string {
  if (!itemId) return "";
  const t = itemId.match(/_T(\d+)(?:_|$)/i);
  if (t) return `T${t[1]}`;
  const mk = itemId.match(/Mk(\d+)$/i);
  if (mk) return `Mk${mk[1]}`;
  if (/_Unique_/i.test(itemId)) return "Unique";
  const trail = itemId.match(/_(\d+)$/);
  if (trail) {
    // 0..6 numeric tail commonly maps to T-tier in weapons + tools.
    return `T${trail[1]}`;
  }
  return "";
}

export const fetchSkills = (q: string, limit = 50, category = "") => {
  const qs = new URLSearchParams();
  if (q) qs.set("q", q);
  if (category) qs.set("category", category);
  qs.set("limit", String(limit));
  return api<{ skills: SkillRow[] }>("GET", `/api/lookup/skills?${qs.toString()}`);
};

export const fetchSkillCategories = () =>
  api<{ categories: ItemCategoryBucket[] }>("GET", "/api/lookup/skill-categories");

export interface ArmorPiece {
  id: string;
  name: string;
  slot: string;
}
export interface ArmorSet {
  base: string;
  pieces: ArmorPiece[];
}
export const fetchArmorSets = () =>
  api<{ sets: ArmorSet[] }>("GET", "/api/lookup/armor-sets");

export interface VehicleActor {
  id: number;
  className: string;
  classShort: string;
  map: string;
  partition: string;
  x: number;
  y: number;
  z: number;
}

export const fetchSpawnedVehicles = () =>
  api<PublishResult>("GET", "/api/vehicles/list");

export const deleteVehicleActor = (actorId: number) =>
  api<PublishResult>("POST", "/api/vehicles/delete", { actor_id: actorId });

/** Parse the `vehicle-list` psql stdout into structured rows. */
export function parseVehicleListOutput(stdout: string): VehicleActor[] {
  if (!stdout) return [];
  const lines = stdout.split(/\r?\n/);
  const dividerIdx = lines.findIndex((l) => /^\s*-+(\+-+)+\s*$/.test(l));
  if (dividerIdx <= 0) return [];
  const header = lines[dividerIdx - 1].split("|").map((c) => c.trim());
  const colIdx = (n: string) => header.findIndex((h) => h === n);
  const idCol = colIdx("id");
  const classCol = colIdx("class");
  const mapCol = colIdx("map");
  const partitionCol = colIdx("partition_id");
  const transformCol = colIdx("transform");
  const out: VehicleActor[] = [];
  for (let i = dividerIdx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^\(\d+ rows?\)/.test(line.trim())) break;
    if (!line.includes("|")) continue;
    const cells = line.split("|").map((c) => c.trim());
    if (cells.length < header.length) continue;
    const cls = cells[classCol] || "";
    const tform = cells[transformCol] || "";
    const m = tform.match(/\((-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)\)/);
    out.push({
      id: parseInt(cells[idCol] || "0", 10),
      className: cls,
      classShort: cls.split("/").pop()?.split(".")[0] || cls,
      map: cells[mapCol] || "",
      partition: cells[partitionCol] || "",
      x: m ? parseFloat(m[1]) : 0,
      y: m ? parseFloat(m[2]) : 0,
      z: m ? parseFloat(m[3]) : 0,
    });
  }
  return out;
}

/** Map a vehicle actor's `classShort` back to a friendly ClassName
 *  (Sandbike, Buggy, …) so we can reuse vehicleIcon/vehicleImageFilename. */
export function vehicleActorToClass(classShort: string): string {
  const s = classShort.toLowerCase();
  if (s.includes("sandbike")) return "Sandbike";
  if (s.includes("buggy")) return "Buggy";
  if (s.includes("tank")) return "Tank";
  if (s.includes("sandcrawler")) return "Sandcrawler";
  if (s.includes("lightornithopter")) return "OrnithopterLight";
  if (s.includes("mediumornithopter")) return "OrnithopterMedium";
  if (s.includes("transportornithopter")) return "OrnithopterTransport";
  if (s.includes("treadwheel")) return "TreadWheel";
  if (s.includes("containervehicle")) return "ContainerVehicle";
  return "";
}

/** Heuristic to give an armor set a readable label. We strip the
 *  shared `Combat_/Stillsuit_` prefixes and `_Unique_*` filler so the
 *  user sees "Swordmaster" or "AtreidesDeserterUnique01" instead of
 *  the raw `Combat_Heavy_Unique_Swordmaster`. */
export function armorSetLabel(set: ArmorSet): string {
  let base = set.base
    .replace(/^Combat_/, "")
    .replace(/^Stillsuit_/, "Stillsuit ")
    .replace(/^Insulated_Combat_/, "Insulated ")
    .replace(/^InsulatedCryo_Combat_/, "Cryo ")
    .replace(/^Social_/, "Social ")
    .replace(/^ExplorationSuit_/, "Exploration ");
  base = base.replace(/_/g, " ").trim();
  return base;
}

/** True when the set base contains the `Unique` marker — these are
 *  the rare/named one-off sets (Ginaz, Sisterhood, AtreidesDeserter,
 *  etc.) as opposed to the tiered CHOAM/native/social variants. */
export function isUniqueArmor(set: ArmorSet): boolean {
  return /Unique/i.test(set.base);
}

/** Detect the tier from an armor set base. Funcom mostly uses a
 *  trailing two-digit number (01-06) for tiered sets — that's the Mk
 *  level. Returns a short label ("T6" / "T3" / "") suitable for a
 *  small badge; empty string when the set is untierable (most
 *  Unique sets fall here). */
export function armorSetTier(set: ArmorSet): string {
  const m = set.base.match(/(?:_)(\d{1,2})$/);
  if (!m) return "";
  const n = parseInt(m[1], 10);
  if (n >= 1 && n <= 6) return `T${n}`;
  return "";
}

/** Detect the class / armor family from the set base for icon coloring. */
export function armorSetClass(set: ArmorSet): { icon: string; tag: string } {
  const b = set.base.toLowerCase();
  if (b.includes("swordmaster")) return { icon: "⚔️", tag: "Swordmaster" };
  if (b.includes("benegeserit") || b.includes("benegesserit")) return { icon: "✨", tag: "Bene Gesserit" };
  if (b.includes("mentat")) return { icon: "🧠", tag: "Mentat" };
  if (b.includes("trooper")) return { icon: "🪖", tag: "Trooper" };
  if (b.includes("planetologist")) return { icon: "🌍", tag: "Planetologist" };
  if (b.includes("stillsuit")) return { icon: "💧", tag: "Stillsuit" };
  if (b.includes("smuggler") || b.includes("smug_")) return { icon: "🎭", tag: "Smuggler" };
  if (b.includes("atreides")) return { icon: "🟢", tag: "Atreides" };
  if (b.includes("hark")) return { icon: "🔴", tag: "Harkonnen" };
  if (b.includes("choam")) return { icon: "🟡", tag: "Choam" };
  if (b.includes("nati") || b.includes("native")) return { icon: "🪨", tag: "Native" };
  if (b.includes("insulated") || b.includes("cryo")) return { icon: "❄️", tag: "Insulated" };
  if (b.includes("exploration")) return { icon: "🧭", tag: "Exploration" };
  if (b.includes("social")) return { icon: "👔", tag: "Social" };
  return { icon: "🛡️", tag: "Armor" };
}

/** Extract the type segment of a skill id (`Skills.<Type>.<Name>`).
 *  Returns the lowercase type tag — "ability", "attribute", "perk",
 *  "key", "spice", "science" — or empty when the id isn't a Skills.* form. */
export function detectSkillType(skillId: string): string {
  const m = skillId.match(/^Skills\.([A-Za-z]+)\./);
  return m ? m[1].toLowerCase() : "";
}

/** Skill type → small emoji badge for the chip. */
export function skillTypeIcon(type: string): string {
  switch (type) {
    case "ability": return "🌀";
    case "attribute": return "📈";
    case "perk": return "⭐";
    case "key": return "🔑";
    case "spice": return "🧂";
    case "science": return "🧪";
    default: return "•";
  }
}

export const fetchHistory = (limit = 50) =>
  api<HistoryResponse>("GET", `/api/history?limit=${limit}`);

export const fetchSteamInfo = (ids: string[]) =>
  api<{ enabled: boolean; players: Record<string, SteamPersona> }>(
    "GET",
    `/api/steam-info?ids=${ids.join(",")}`,
  );

// Generic publish — every per-command form just calls this.
export const publish = (sub: string, body: Record<string, unknown>) =>
  api<PublishResult>("POST", `/admin/${sub}`, body);

// Parse the `admin players` tabular stdout into rows (until we add a
// JSON endpoint for it). The CLI prints a psql ASCII table — split
// on the dashed separator, then strip pipes.
export function parsePlayerTable(stdout: string): PlayerRow[] {
  if (!stdout) return [];
  const lines = stdout.split(/\r?\n/);
  // Find the divider line (e.g. "----+----+----")
  const dividerIdx = lines.findIndex((l) => /^\s*-+(\+-+)+\s*$/.test(l));
  if (dividerIdx <= 0) return [];
  const header = lines[dividerIdx - 1].split("|").map((c) => c.trim());
  const out: PlayerRow[] = [];
  for (let i = dividerIdx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (/^\(\d+ rows?\)/.test(line.trim())) break;
    if (!line.includes("|")) continue;
    const cells = line.split("|").map((c) => c.trim());
    if (cells.length < header.length) continue;
    const row: Record<string, string> = {};
    header.forEach((h, idx) => {
      row[h] = cells[idx] || "";
    });
    out.push({
      fls_id: row["fls_id"] || "",
      character: row["character"] && row["character"] !== "-" ? row["character"] : null,
      steam_id: row["steam_id"] || null,
      platform_name: row["platform_name"] || "",
      life: row["life"] || "",
      online: row["online"] || "",
      last_avatar_activity: row["last_avatar_activity"] || null,
    });
  }
  return out;
}

// Parse the `admin pos` stdout into structured XYZ. The CLI prints
// human-friendly text + ready-to-paste commands; we just grab the
// Position line.
export interface PosInfo {
  flsId: string;
  map: string;
  partition: string;
  x: number;
  y: number;
  z: number;
  /** Player id used to look up this position (e.g. "me", "name:Sergentval"). */
  source: string;
  /** When the lookup completed. */
  ts: number;
}

/** Vehicle class → image filename hosted on awakening.wiki, or null if
 *  Funcom's wiki doesn't ship an icon for that class (we fall back to
 *  the emoji in vehicleIcon()). Filenames extracted from the live
 *  Vehicles category page on awakening.wiki. */
export function vehicleImageFilename(className: string): string | null {
  switch (className) {
    case "Sandbike": return "T_UI_IconVehCHSandBikeR_D.png";
    case "Buggy": return "T_UI_IconVehCHBuggyR_D.png";
    case "Tank": return "T_UI_IconVehCHTankR_D.png";
    case "Sandcrawler": return "T_UI_IconVehCHSandCrawlerR_D.png";
    case "OrnithopterLight": return "T_UI_IconVehCHOrniLightR_D.png";
    case "OrnithopterMedium": return "T_UI_IconVehCHOrniMediumR_D.png";
    case "OrnithopterTransport": return "T_UI_IconVehCHOrniTransportR_D.png";
    case "TreadWheel": return "T_UI_IconVehOETreadwheelR_D.png";
    // ContainerVehicle has no wiki icon — falls back to the emoji.
    default: return null;
  }
}

/** Vehicle class → emoji fallback used when no wiki icon is available. */
export function vehicleIcon(className: string): string {
  switch (className) {
    case "Sandbike": return "🏍️";
    case "Buggy": return "🛺";
    case "Tank": return "🚛";
    case "Sandcrawler": return "🚜";
    case "OrnithopterLight":
    case "OrnithopterMedium":
    case "OrnithopterTransport": return "🪶";
    case "TreadWheel": return "🛞";
    case "ContainerVehicle": return "📦";
    default: return "🚗";
  }
}

/** Item category → tailwind color class + small emoji prefix. */
export function itemCategoryStyle(category?: string): { color: string; icon: string } {
  switch ((category || "").toLowerCase()) {
    case "weapons": return { color: "text-red-300", icon: "⚔️" };
    case "clothing": return { color: "text-violet-300", icon: "👕" };
    case "resources": return { color: "text-emerald-300", icon: "🪨" };
    case "buildings":
    case "placeables": return { color: "text-amber-300", icon: "🏗️" };
    case "schematics": return { color: "text-sky-300", icon: "📜" };
    case "contracts": return { color: "text-blue-300", icon: "📋" };
    case "customizations": return { color: "text-pink-300", icon: "🎨" };
    default: return { color: "text-slate-300", icon: "•" };
  }
}

/** Skill category → tailwind color + emoji. Matches the in-game class banners. */
export function skillCategoryStyle(category?: string): { color: string; icon: string } {
  switch ((category || "").toLowerCase()) {
    case "benegesserit": return { color: "text-violet-300", icon: "✨" };
    case "swordmaster": return { color: "text-red-300", icon: "⚔️" };
    case "mentat": return { color: "text-amber-300", icon: "🧠" };
    case "trooper": return { color: "text-orange-300", icon: "🪖" };
    case "planetologist": return { color: "text-emerald-300", icon: "🌍" };
    case "duelist":
    case "fighter": return { color: "text-rose-300", icon: "🗡️" };
    default: return { color: "text-slate-300", icon: "•" };
  }
}

export function parsePosOutput(stdout: string, source: string): PosInfo | null {
  const flsMatch = stdout.match(/FLS:\s+(\S+)/);
  const mapMatch = stdout.match(/Map:\s+(\S+)\s+\(partition\s+(\S+)\)/);
  const posMatch = stdout.match(/Position:\s+X=(-?\d+\.?\d*)\s+Y=(-?\d+\.?\d*)\s+Z=(-?\d+\.?\d*)/);
  if (!flsMatch || !mapMatch || !posMatch) return null;
  return {
    flsId: flsMatch[1],
    map: mapMatch[1],
    partition: mapMatch[2],
    x: parseFloat(posMatch[1]),
    y: parseFloat(posMatch[2]),
    z: parseFloat(posMatch[3]),
    source,
    ts: Date.now(),
  };
}
