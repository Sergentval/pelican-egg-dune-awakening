// Thin fetch wrapper + types. All endpoints share-origin with the SPA
// when served by admin-http.py, so no base URL is needed.
//
// Auth is via HttpOnly session cookie (dune_session) that the backend
// sets on /api/login. The browser auto-attaches it; JS cannot read it,
// so any future XSS cannot steal the credential. CSRF protection uses
// a paired non-HttpOnly cookie (dune_csrf) that we copy into the
// X-CSRF-Token header on mutating requests — the backend rejects a
// cookie-authenticated request that doesn't echo the cookie value back
// in the header, which a cross-origin attacker cannot read or forge.

// ---- input sanitisers --------------------------------------------------
//
// Defence-in-depth client-side validation. The backend already escapes
// everything via JSON encoding (no shell interpolation since Phase 2),
// so these are NOT the primary security layer — but they prevent
// obviously-malformed inputs from reaching the wire, give the operator
// instant feedback, and strip ASCII control characters that would log
// strangely or break the broadcast renderer.

// Strip control characters (NUL, BEL, BS, vertical-tab, FF, etc) and
// cap to `max` chars. Used for broadcast title/body and similar
// free-text fields. Newlines + tab are kept since multi-line broadcasts
// are legitimate.
export function sanitizeText(s: string, max: number): string {
  return s
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    .slice(0, max);
}

// Validate a Funcom FName-style identifier (used for item ids, vehicle
// class/template, skill modules). Letters, digits, underscore only,
// 1-64 chars. Returns "" on rejection so callers can show an error.
export function sanitizeIdent(s: string, max = 64): string {
  const trimmed = s.trim();
  if (!trimmed || trimmed.length > max) return "";
  return /^[A-Za-z0-9_]+$/.test(trimmed) ? trimmed : "";
}

// Validate a player-id input against the four documented forms:
//   *                          all online
//   me                         single online shortcut
//   name:<character>           up to 32 chars, common name characters
//   steam:<digits>             17-digit Steam id
//   <16-hex>                   FLS canonical
// Returns "" on rejection. Backend validates again; this just keeps
// shell-meta from leaving the SPA.
export function sanitizePlayerId(s: string): string {
  const v = s.trim();
  if (!v) return "";
  if (v === "*" || v === "me") return v;
  if (/^name:[A-Za-z0-9 _'.\-]{1,32}$/.test(v)) return v;
  if (/^steam:\d{17}$/.test(v)) return v;
  if (/^[0-9a-fA-F]{16}$/.test(v)) return v.toUpperCase();
  return "";
}

// Clamp + parse a numeric input. Returns `fallback` on NaN/missing,
// otherwise bounds to [min, max]. Used to prevent absurd values like
// "999999999999999999" from being submitted to backend.
export function clampNum(s: string, min: number, max: number, fallback: number): number {
  const n = parseInt(s, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function getCsrfToken(): string {
  // document.cookie is a string of "name=value; name=value; …".
  // Match the dune_csrf cookie regardless of position (start-of-string
  // or after "; ").
  const m = document.cookie.match(/(?:^|; )dune_csrf=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : "";
}

// Back-compat shims: older callers imported setToken/getToken. The SPA
// no longer stores any session token in JS — the cookie is authoritative.
// These are no-ops so we don't break the public surface mid-rollout.
export const getToken = (): string => "";
export const setToken = (_token: string): void => {
  // Old localStorage cleanup: drop any token left over from before this
  // migration so an attacker who steals the same key later finds nothing.
  try {
    localStorage.removeItem("dune-admin-token");
  } catch {
    // ignore quota / privacy-mode errors
  }
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
  // CSRF token attached on every mutating request. The browser sends
  // the matching cookie automatically; the backend cross-checks them.
  if (method !== "GET") {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const res = await fetch(path, {
    method,
    headers,
    // credentials: 'include' ensures the HttpOnly session cookie is
    // sent. With same-origin SPA + same-origin API this is the default,
    // but being explicit guards against future deployments that put
    // the API on a different origin.
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = text;
  }
  // 401 on an authenticated route means the cookie is dead (expired,
  // revoked, or never present). /api/login itself returns 401 on bad
  // password — skip the global handler there.
  if (res.status === 401 && unauthorizedHandler && path !== "/api/login") {
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

export const login = (
  password: string,
): Promise<ApiResponse<{ token: string; csrf: string; expires_in: number; type: string }>> =>
  api("POST", "/api/login", { password });

// Tells the backend to revoke the current session jti and clear cookies.
// Always treated as success on the client — even a 401 from a dead cookie
// is fine, we just transition the UI to the login screen either way.
export const logout = () => api("POST", "/api/logout");

export const me = () => api<{ authenticated: boolean; session: { exp: number; iat: number; sub: string } }>("GET", "/api/me");

export const fetchPlayers = (filter: "all" | "online" = "all") =>
  api<PublishResult>("GET", `/api/players?filter=${filter}`);

export const fetchPos = (playerId: string) => api<PublishResult>("GET", `/api/pos/${encodeURIComponent(playerId)}`);

// A CSV-derived table reply (headers + rows) as returned by the per-player
// read endpoints (inventory, state, tags). `available:false` means the
// underlying tables are missing on this build.
export interface PlayerTable {
  headers: string[];
  rows: string[][];
  truncated?: boolean;
  available?: boolean;
  detail?: string;
}

// Player inventory (dune.items for the target's character), enriched with a
// `name` column server-side. Each row carries item_id for targeted deletes.
export const fetchInventory = (playerId: string) =>
  api<PlayerTable>("GET", `/api/players/${encodeURIComponent(playerId)}/inventory`);

// DESTRUCTIVE: hard-delete one item stack by dune.items.id. Backend-gated by
// inventory kind: player-carried items reject while the owner is connected;
// WORLD items (base containers, vehicles) reject unless the map is fully
// stopped (the running map caches world inventories and rewrites on flush).
export const deleteItem = (itemId: string) =>
  api<PublishResult>("POST", `/api/items/${encodeURIComponent(itemId)}/delete`);

// ---- Live map (Phase 2-4) ----------------------------------------------
export interface MapMarker {
  id: string;
  name: string;
  online: boolean;
  partition: number;
  fls: string;
  x: number;
  y: number;
  z: number;
  kind: string;
}
export interface MapMarkersResp { ok: boolean; map: string; markers: MapMarker[]; }
export const fetchMapMarkers = (map: string) =>
  api<MapMarkersResp>("GET", `/api/map/markers?map=${encodeURIComponent(map)}`);

export interface MapLocation { name: string; map: string; x: number; y: number; z: number; }
export interface LocationsResp { ok: boolean; locations: MapLocation[]; }
export const fetchLocations = () => api<LocationsResp>("GET", "/api/map/locations");
export const addLocation = (location: MapLocation) =>
  api<LocationsResp>("POST", "/api/map/locations", { action: "add", location });
export const removeLocation = (name: string) =>
  api<LocationsResp>("POST", "/api/map/locations", { action: "remove", name });
export const teleportToLocation = (player: string, location: string) =>
  api<PublishResult>("POST", "/api/map/teleport", { player, location });

// ---- Scheduler: auto-restart + auto-backup ----------------------------
export interface ScheduleRestart {
  enabled: boolean;
  time: string;
  days: string[];
  warn_lead_secs: number;
  warn_freq_secs: number;
  catch_up_grace_secs: number;
}
export interface ScheduleBackup { enabled: boolean; every_hours: number; retention: number; }
// Generic scheduled tasks (broadcast, scale-instance) — mirror admin_schedule.py.
export interface TaskSchedule {
  kind: "daily" | "interval";
  time?: string;            // daily: "HH:MM" (UTC)
  days?: string[];          // daily: subset of mon..sun
  every_minutes?: number;   // interval
}
export interface BroadcastParams { title: string; body: string; duration: number }
export interface ScaleParams { map: string; replicas: number; force: boolean }
export interface ScheduledTask {
  id: string;
  type: "broadcast" | "scale-instance";
  enabled: boolean;
  schedule: TaskSchedule;
  params: Partial<BroadcastParams> & Partial<ScaleParams>;
}
export interface ScheduleConfig {
  restart: ScheduleRestart;
  backup: ScheduleBackup;
  tasks: ScheduledTask[];
}
// Mirror admin_instances.SCALABLE_MAPS / SCALE_REPLICAS_MAX (the backend re-validates).
export const SCALABLE_MAPS = ["DeepDesert_1", "SH_Arrakeen", "SH_HarkoVillage"] as const;
export const SCALE_REPLICAS_MAX = 4;
export interface TaskRun { task: string; status: string; detail: string; at: string; }
export interface ScheduleResponse {
  ok: boolean;
  config: ScheduleConfig;
  runs: TaskRun[];
  pending_restart: string | null;
  restart_configured: boolean;
}
export const fetchSchedule = () => api<ScheduleResponse>("GET", "/api/schedule");
export const saveSchedule = (config: ScheduleConfig) =>
  api<{ ok: boolean; config?: ScheduleConfig; error?: string }>("POST", "/api/schedule", config);
// run-backup/run-restart/run-task all return {ok, status?, detail?|error?} — NOT a
// PublishResult. Type it honestly so the UI can surface the detail/error message.
export interface TriggerResult { ok: boolean; status?: string; detail?: string; error?: string }
export const triggerTask = (task: "backup" | "restart") =>
  api<TriggerResult>("POST", `/api/tasks/trigger/${task}`);
export const triggerTaskById = (id: string) =>
  api<TriggerResult>("POST", `/api/tasks/trigger/${encodeURIComponent(id)}`);

// Ad-hoc restart. `admin shutdown` only banners players — it stops nothing —
// so this arms the scheduler's pending restart, which is what actually calls
// the panel's power API. warn_window_secs is how long BEFORE the restart the
// in-game countdown starts, so a restart hours out stays silent until then.
export interface ArmRestartResult { ok: boolean; restart_at?: string; warn_at?: string; error?: string }
export const armRestart = (delaySecs: number, warnWindowSecs: number, warnFreqSecs: number) =>
  api<ArmRestartResult>("POST", "/api/schedule/restart-in", {
    delay_secs: delaySecs, warn_window_secs: warnWindowSecs, warn_freq_secs: warnFreqSecs,
  });
export const cancelArmedRestart = () =>
  api<TriggerResult>("POST", "/api/schedule/cancel-restart");

export interface BackupTable { headers: string[]; rows: string[][] }
export const fetchBackups = () => api<BackupTable>("GET", "/api/database/backups");

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

// ---- Phase 4: server status grid ----

export interface StatusMapRow {
  map: string;
  status?: string;
  desired?: number;
  current?: number;
  players: number;
}
export interface StatusGrid {
  ok: boolean;
  maps: StatusMapRow[];
  totalServers: number;
  totalPlayers: number;
  uptimeSeconds: number;
  sources?: { mockK8s: boolean; playerCounts: boolean };
  warning?: string;
}
export const fetchStatus = () => api<StatusGrid>("GET", "/api/status");

// World-partition topology (Instances tab). dimension 0 = warm landing zone;
// dimension > 0 = per-player "sandstorm tunnel" partitions (e.g. DeepDesert 101/102/103).
export interface Partition {
  partition_id: number;
  map: string;
  dimension: number;
  label: string;
  blocked: boolean;
  server_id: string | null;
  game_port: number | null;
  ready: boolean;
  alive: boolean;
  players: number;
  parked?: boolean; // Survival_1 sietch intentionally paused (data kept), distinct from plain offline/cold
}
export interface PartitionsResp {
  ok: boolean;
  partitions: Partition[];
  error?: string;
}
export const fetchPartitions = () => api<PartitionsResp>("GET", "/api/partitions");

// Instance scale (Instances tab, phase 1) — drives mock-k8s ServerSetScale replicas.
export interface ScaleResult {
  ok: boolean;
  map?: string;
  replicas?: number;
  previous?: number;
  requiresConfirmation?: boolean;
  players?: number;
  message?: string;
  error?: string;
}
export const scaleInstance = (map: string, replicas: number, force = false) =>
  api<ScaleResult>("POST", `/api/instances/${encodeURIComponent(map)}/scale`, { replicas, force });

// Phase 2: spin a single dimension partition up (respawn) / down (offline).
export interface DimResult {
  ok: boolean;
  requiresConfirmation?: boolean;
  players?: number;
  partition?: number;
  message?: string;
  error?: string;
}
export const dimensionUp = (partitionId: number) =>
  api<DimResult>("POST", `/api/instances/dimension/${partitionId}/up`, {});
export const dimensionDown = (partitionId: number, force = false) =>
  api<DimResult>("POST", `/api/instances/dimension/${partitionId}/down`, { force });
// Repair the in-game browser: sweep orphan farm_state + resync the Director.
export const repairBrowser = () =>
  api<PublishResult>("POST", "/api/instances/repair", {});

// Multi-Sietch: add / remove a player-choosable Survival_1 sietch (a Survival_1
// dimension partition). Spin up/down of an existing sietch reuses dimensionUp/Down.
export const addSietch = (label?: string) =>
  api<DimResult>("POST", "/api/sietches", label && label.trim() ? { label: label.trim() } : {});
export const removeSietch = (partitionId: number, force = false) =>
  api<DimResult>("POST", `/api/sietches/${partitionId}/remove`, { force });
// Park = pause a sietch but KEEP all data + structures (survives reboot); unpark respawns
// it with data intact. Distinct from removeSietch (which deletes). Park is player-guarded.
export const parkSietch = (partitionId: number, force = false) =>
  api<DimResult>("POST", `/api/sietches/${partitionId}/park`, { force });
export const unparkSietch = (partitionId: number) =>
  api<DimResult>("POST", `/api/sietches/${partitionId}/unpark`, {});

// Per-sietch config (heterogeneous sietches: name + PvP/harvest/etc.).
export interface SietchCapableSetting {
  id: string;
  label?: string;
  category?: string;
  key: string;
  type: string;
  enum?: string[] | null;
  quoted?: boolean;
  default?: string;
  verified?: boolean;
}
export interface SietchConfigResp {
  ok: boolean;
  overrides: Record<string, string>;
  settings: SietchCapableSetting[];
}
export interface SietchConfigResult extends DimResult {
  applied?: string[];
  skipped?: string[];
  restarted?: boolean;
}
export const fetchSietchConfig = (partitionId: number) =>
  api<SietchConfigResp>("GET", `/api/sietches/${partitionId}/config`);
export const setSietchConfig = (
  partitionId: number,
  body: { name?: string; overrides?: Record<string, string>; force?: boolean },
) => api<SietchConfigResult>("POST", `/api/sietches/${partitionId}/config`, body);

// ---- Phase 5: server settings ----

export interface SettingItem {
  id: string;
  label: string;
  type: string;
  default: string | null;
  enum: string[] | null;
  value: string | null;
  isDefault: boolean;
  verified: boolean;
  section: string | null;
  key: string;
  clientGated: boolean;
  advanced: boolean;
}
export interface SettingsResponse {
  ok: boolean;
  count: number;
  categories: Record<string, SettingItem[]>;
  note?: string;
}
export interface SettingsSaveResult {
  ok: boolean;
  applied: string[];
  errors: { id: string; error: string }[];
  restartRequired: boolean;
}
export const fetchSettings = () => api<SettingsResponse>("GET", "/api/settings");
export const saveSettings = (settings: Record<string, string>) =>
  api<SettingsSaveResult>("POST", "/api/settings", { settings });

// ---- Phase 6: welcome kits ----

export interface WelcomeGrant {
  fls_id: string;
  package_version: string;
  account_id: number;
  character_name: string;
  status: string;
  granted_at: string;
  attempts: number;
  last_error: string;
  updated_at: string;
}
export interface WelcomeResponse {
  ok: boolean;
  config: {
    enabled: boolean;
    active_version: string;
    packages: string[];
    ledger: { granted: number; failed: number };
  };
  grants: WelcomeGrant[];
}
export interface WelcomeScanResult {
  ok: boolean;
  disabled?: boolean;
  granted?: number;
  failed?: number;
  skipped?: number;
  online?: number;
  version?: string;
  error?: string;
}
export const fetchWelcome = () => api<WelcomeResponse>("GET", "/api/welcome");
export const welcomeScan = () => api<WelcomeScanResult>("POST", "/api/welcome/scan");
export const welcomeRetryFailed = () => api<{ cleared: number }>("POST", "/api/welcome/retry-failed");

// ---- Player editor (dedicated DB-write routes) ------------------------
// These hit the per-player REST routes (POST /api/players/<id>/<action>),
// distinct from the generic publish() path. The body is a run_publish entry
// (PublishResult) on success, or {error} on a validation/auth failure.

export interface ProgressionPreset {
  id: string;
  name: string;
  description: string;
  node_count: number;
  nodes: string[];
}

export const fetchProgressionPresets = () =>
  api<{ ok: boolean; presets: ProgressionPreset[] }>("GET", "/api/progression/presets");

export interface FactionState {
  faction_id: number;
  name: string;
  rep: number;
  tier: number;
  tier_name: string;
}

export interface PlayerSummary {
  ok: boolean;
  solaris: number;
  xp: {
    xp: number;
    level: number;
    intel: number;
    maxXP: number;
    maxLevel: number;
    atCap: boolean;
    xpToNext: number;
    nextLevelAt: number;
  };
  faction: {
    alignment: number;
    alignment_name: string;
    atreides: FactionState;
    harkonnen: FactionState;
  };
  journey: { id: string; name: string; complete: number; total: number }[];
  error?: string;
  schema_gap?: boolean;
}

export const fetchPlayerSummary = (playerId: string) =>
  api<PlayerSummary>("GET", `/api/players/${encodeURIComponent(playerId)}/summary`);

// ---- Bases (Red-Blink port) ---------------------------------------------

export interface BaseRow {
  base_id: string;
  base_actor_id: string;
  map: string;
  pieces: string;
  placeables: string;
  owner: string;
}

export interface BaseWaterRow {
  water_type: string;
  devices: string;
  stored: string;
  capacity: string;
  blood_stored: string;
  blood_capacity: string;
}

export const fetchBases = (q = "") =>
  api<{ ok: boolean; bases: BaseRow[] }>(
    "GET", q ? `/api/bases?q=${encodeURIComponent(q)}` : "/api/bases");

export const fetchBaseWater = (baseId: string) =>
  api<{ ok: boolean; water: BaseWaterRow[] }>(
    "GET", `/api/bases/${encodeURIComponent(baseId)}/water`);

/** Server-side the refill FAILS CLOSED unless the base's map is fully
 * stopped; the UI confirms first, so force is always sent here. */
export const baseWaterRefill = (baseId: string) =>
  api<PublishResult & { error?: string }>(
    "POST", `/api/bases/${encodeURIComponent(baseId)}/water-refill`, { force: true });

export interface BaseFuelRow {
  placeable_id: string;
  generator: string;
  fuel: string;
  units: string;
  cap: string;
  percent: string;
  runtime_hours: string;
}

export const fetchBaseFuel = (baseId: string) =>
  api<{ ok: boolean; fuel: BaseFuelRow[] }>(
    "GET", `/api/bases/${encodeURIComponent(baseId)}/fuel`);

/** Same fail-closed map-down contract as the water refill. */
export const baseFuelRefill = (baseId: string) =>
  api<PublishResult & { error?: string }>(
    "POST", `/api/bases/${encodeURIComponent(baseId)}/fuel-refill`, { force: true });

export interface BaseContainerItem {
  placeable_id: string;
  container_type: string;
  inventory_id: string;
  slots: string;
  item_id: string;
  template_id: string;
  stack_size: string;
  quality_level: string;
  position_index: string;
}

export interface BasePermissionRow {
  rank: string;
  character: string;
  fls_id: string;
  player_id: string;
  /** "f" = this row names an actor the game ignores (not the account's
   *  player_controller_id) — shown, but rank edits are pointless on it. */
  canonical: string;
}

export interface PermissionCandidate {
  player_id: string;
  character: string;
  fls_id: string;
}

export const fetchBaseContainers = (baseId: string) =>
  api<{ ok: boolean; items: BaseContainerItem[] }>(
    "GET", `/api/bases/${encodeURIComponent(baseId)}/containers`);

export const fetchBasePermissions = (baseId: string) =>
  api<{ ok: boolean; roster: BasePermissionRow[] }>(
    "GET", `/api/bases/${encodeURIComponent(baseId)}/permissions`);

export const fetchPermissionCandidates = (q: string) =>
  api<{ ok: boolean; candidates: PermissionCandidate[] }>(
    "GET", `/api/bases/permission-candidates${q ? `?q=${encodeURIComponent(q)}` : ""}`);

/** Live write: the game's stored procedures notify the running map — no
 *  map-down gate, the change applies immediately. Promoting a new Owner
 *  demotes the current one to Co-Owner in the same transaction. */
export const basePermissionSet = (baseId: string, playerId: string, rank: 1 | 2 | 3) =>
  api<PublishResult & { error?: string }>(
    "POST", `/api/bases/${encodeURIComponent(baseId)}/permission-set`,
    { player_id: playerId, rank });

export const basePermissionRemove = (baseId: string, playerId: string) =>
  api<PublishResult & { error?: string }>(
    "POST", `/api/bases/${encodeURIComponent(baseId)}/permission-remove`,
    { player_id: playerId });

export const baseTransferCustodian = (baseId: string) =>
  api<PublishResult & { error?: string }>(
    "POST", `/api/bases/${encodeURIComponent(baseId)}/transfer-custodian`, {});

// ---- Base backup wipe-guard (DST port) ----------------------------------

export interface BaseGuardState {
  ok: boolean;
  available: boolean;
  function_found: boolean;
  applied: boolean;
  base_backups: number;
  backup_state_actors: number;
  boot_reapply: boolean;
}

export const fetchBaseGuard = () => api<BaseGuardState>("GET", "/api/base-guard");

export const baseGuardApply = () =>
  api<PublishResult & { error?: string }>("POST", "/api/base-guard/apply", {});

export const baseGuardRevert = () =>
  api<PublishResult & { error?: string }>("POST", "/api/base-guard/revert", {});

export const baseGuardConfig = (enabled: boolean) =>
  api<{ ok: boolean; boot_reapply: boolean }>("POST", "/api/base-guard/config", { enabled });

// ---- World reset (DST worldreset-2 port) --------------------------------

export interface WorldResetPending {
  backup_file: string;
  backup_bytes: number;
  char_backups: string[];
  requested_at: number;
}

export interface WorldRollbackPending {
  restore_dir: string;
  requested_at: number;
}

export interface WorldResetResult {
  operation: string;
  ok: boolean;
  detail: string;
  preserved: string;
  at: number;
}

export interface WorldResetState {
  ok: boolean;
  pending: WorldResetPending | null;
  rollback: WorldRollbackPending | null;
  last_result: WorldResetResult | null;
  preserved: string[];
  online_players: number | null;
}

export const fetchWorldReset = () => api<WorldResetState>("GET", "/api/world-reset");

/** Arms only — the phrase is re-validated server-side and the next boot
 *  executes. Long timeout territory: backup + optional character sweep. */
export const worldResetArm = (phrase: string, charBackups: boolean) =>
  api<PublishResult & { error?: string }>(
    "POST", "/api/world-reset/arm", { phrase, char_backups: charBackups });

export const worldResetCancel = () =>
  api<PublishResult & { error?: string }>("POST", "/api/world-reset/cancel", {});

export const worldRollbackArm = (phrase: string, target?: string) =>
  api<PublishResult & { error?: string }>(
    "POST", "/api/world-reset/rollback",
    target ? { phrase, target } : { phrase });

// ---- Deep Desert Wick Maps (DST port) ------------------------------------

export interface WickPoi {
  sector: string;   // e.g. "I5"
  subx: number;     // 1-4
  suby: number;     // 0-3
  type: string;     // wreck | cave | titanium | stravidium | testing-station | taxi-service | large-spice-field
}

export interface WickLegendEntry {
  type: string;
  label: string;
  count: number;
}

export interface WickLayout {
  seed: number;
  confidence?: string;
  reliability?: string;
  largeSpiceSectors?: string[];
  legend?: WickLegendEntry[];
  pois: WickPoi[];
}

export interface DeepDesertLayout {
  ok: boolean;
  available: boolean;
  reason?: string;
  seed: number | null;
  layout_available: boolean;
  layout: WickLayout | null;
}

export const fetchDeepDesertLayout = () =>
  api<DeepDesertLayout>("GET", "/api/map/deepdesert-layout");

// ---- Player events + battlepass (dune-admin port) ------------------------

export interface PlayerEvent {
  id: number;
  name: string;
  type: "zone_race" | "milestone";
  enabled: number;
  version: number;
  config_json: string;
  reward_json: string;
  announce_template: string;
  poll_seconds: number;
  jitter_seconds: number;
  claims: Record<string, number>;
}

export interface PlayerEventClaim {
  event_id: number;
  account_id: number;
  status: string;
  attempts: number;
  last_error: string;
  claimed_at: number;
}

export const fetchPlayerEvents = () =>
  api<{ ok: boolean; enabled: boolean; events: PlayerEvent[] }>(
    "GET", "/api/player-events");

export const fetchPlayerEventClaims = (id: number) =>
  api<{ ok: boolean; claims: PlayerEventClaim[] }>(
    "GET", `/api/player-events/${id}/claims`);

export const playerEventsConfig = (enabled: boolean) =>
  api<{ ok: boolean; error?: string }>(
    "POST", "/api/player-events/config", { enabled });

export interface PlayerEventInput {
  name: string;
  type: string;
  config: string;
  reward: string;
  announce_template: string;
  poll_seconds: number;
  jitter_seconds: number;
}

export const createPlayerEvent = (input: PlayerEventInput) =>
  api<{ ok: boolean; id?: number; error?: string }>(
    "POST", "/api/player-events", input);

export const playerEventAction = (id: number, action: "enable" | "delete" | "reset",
                                  body: Record<string, unknown> = {}) =>
  api<{ ok: boolean; error?: string }>(
    "POST", `/api/player-events/${id}/${action}`, body);

export interface BattlepassConfig {
  enabled: boolean;
  award_past: boolean;
  auto_grant: boolean;
  poll_seconds: number;
}

export interface BattlepassSummary {
  ok: boolean;
  config: BattlepassConfig;
  tiers_enabled: number;
  intel_total: number;
  claims: Record<string, number>;
  ledger: Record<string, number>;
}

export interface BattlepassTier {
  id: number;
  tier_key: string;
  category: string;
  label: string;
  signal: string;
  signal_key: string;
  threshold: number;
  intel: number;
  reward_items: string;
  enabled: number;
}

export const fetchBattlepass = () =>
  api<BattlepassSummary>("GET", "/api/battlepass");

export const fetchBattlepassTiers = () =>
  api<{ ok: boolean; tiers: BattlepassTier[] }>("GET", "/api/battlepass/tiers");

export const battlepassConfig = (cfg: Partial<BattlepassConfig>) =>
  api<{ ok: boolean; config?: BattlepassConfig; error?: string }>(
    "POST", "/api/battlepass/config", cfg);

export const battlepassTierUpdate = (id: number, fields: Record<string, unknown>) =>
  api<{ ok: boolean; error?: string }>(
    "POST", `/api/battlepass/tiers/${id}`, fields);

export const battlepassReseed = () =>
  api<{ ok: boolean; tiers?: number; error?: string }>(
    "POST", "/api/battlepass/reseed", {});

export const battlepassGrant = (accountId: number) =>
  api<{ ok: boolean; granted?: number; failed?: number; detail?: string; error?: string }>(
    "POST", "/api/battlepass/grant", { account_id: accountId });

export const battlepassReset = (mode: "demote" | "purge", accountId = 0) =>
  api<{ ok: boolean; changed?: number; error?: string }>(
    "POST", "/api/battlepass/reset", { mode, account_id: accountId });

// ---- Connection doctor --------------------------------------------------

export interface DoctorCheck {
  id: string;
  status: "ok" | "warn" | "error" | "skip";
  summary: string;
  detail: string;
  hint: string;
}

export const fetchDoctor = () =>
  api<{ ok: boolean; summary: Record<string, number>; checks: DoctorCheck[]; stderr?: string }>(
    "GET", "/api/doctor");

// ---- Character backups (native transfer subsystem) ---------------------

export interface CharBackup {
  file: string;
  fls: string;
  character_name: string;
  action: string;
  reason: string;
  patches_checksum: string;
  bytes: number;
  created_at: string;
}

export const fetchCharBackups = (playerId: string) =>
  api<{ ok: boolean; backups: CharBackup[] }>(
    "GET", `/api/players/${encodeURIComponent(playerId)}/char-backups`);

/** FULL REPLACE of the backup's character. The UI confirms first, so the
 * API-level force flag is always sent here. */
export const charBackupRestore = (file: string) =>
  api<PublishResult & { error?: string }>("POST", "/api/char-backups/restore", { file, force: true });

export const charBackupDelete = (file: string) =>
  api<PublishResult & { error?: string }>("POST", "/api/char-backups/delete", { file });

/** POST a player-editor write. `action` is the route suffix (e.g. "give-currency"). */
export function playerWrite(
  playerId: string,
  action: string,
  body?: Record<string, unknown>,
): Promise<ApiResponse<PublishResult & { error?: string }>> {
  return api<PublishResult & { error?: string }>(
    "POST",
    `/api/players/${encodeURIComponent(playerId)}/${action}`,
    body,
  );
}

// ---- Spicefield economy controls --------------------------------------

export interface SpiceField {
  id: number;
  field_type: string;
  map: string;
  dimension: number;
  spawning: boolean;
  active: number;
  max_active: number;
  primed: number;
  max_primed: number;
  weight: string;
}

export const fetchSpice = () =>
  api<{ ok: boolean; fields: SpiceField[]; error?: string }>("GET", "/api/spice");

export const setSpiceSpawning = (id: number, active: boolean) =>
  api<PublishResult & { error?: string }>("POST", `/api/spice/${id}/spawning`, { active });

export const setSpiceCaps = (id: number, maxActive: number, maxPrimed: number) =>
  api<PublishResult & { error?: string }>("POST", `/api/spice/${id}/caps`, {
    max_active: maxActive,
    max_primed: maxPrimed,
  });

// ---- Market bot (7b) ---------------------------------------------------

export interface MarketBotStatus {
  ok: boolean;
  bot_orders: number;
  npc_orders: number;
  player_orders: number;
  owner?: string;
  exchange?: string;
  access_point?: string;
  inventory?: string;
}

export interface MarketConfig {
  enabled?: boolean;
  max_listings?: number;
  buy_threshold?: number;
  gamble_die?: number;
  gamble_target?: number;
  max_buys_per_tick?: number;
  scan_interval_secs?: number;
  disabled_items?: string[];
}

export interface MarketInfo {
  ok: boolean;
  catalog?: { items?: number; vendor_priced?: number; fallback_priced?: number };
  config?: MarketConfig;
  orders?: unknown;
}

// 7b-3 gamble-buy tick result (manual trigger or autonomous loop).
export interface MarketBuyResult {
  ok: boolean;
  bought?: number;
  errors?: number;
  considered?: number;
  chosen?: number;
  posted?: number;
  skipped?: string;
  error?: string;
}

export const fetchMarket = () => api<MarketInfo>("GET", "/api/market");
export const fetchMarketBot = () => api<MarketBotStatus>("GET", "/api/market/bot");
export const marketPost = (limit: number) =>
  api<PublishResult & { error?: string }>("POST", "/api/market/post", { limit });
export const marketClear = () =>
  api<PublishResult & { error?: string }>("POST", "/api/market/clear", {});
export const marketBuy = () =>
  api<MarketBuyResult>("POST", "/api/market/buy", {});
export const marketConfig = (patch: Partial<MarketConfig>) =>
  api<{ ok: boolean; config?: MarketConfig; error?: string }>("POST", "/api/market/config", patch);

// ---- Autoscaler ---------------------------------------------------------
export interface AutoscalerMapCfg {
  map: string;
  enabled: boolean;
  min_replicas: number;
  max_replicas: number;
}
// DeepDesert sandstorm-DIMENSION pool autoscaling (separate from the mock-k8s `maps`:
// these are world_partition rows spawned/reaped via dimension-up/down). Survival_1
// sietches are player bases and are NEVER autoscaled — this only addresses DeepDesert_1.
export interface DeepDesertConfig {
  enabled: boolean;
  min_dims: number;
  max_dims: number;
  players_per_dim: number;
  idle_drain_secs: number;
  demand_grace_secs: number;
}
export interface AutoscalerConfig {
  enabled: boolean;
  scan_interval_secs: number;
  wake_poll_secs: number;
  idle_drain_secs: number;
  demand_grace_secs: number;
  players_per_instance: number;
  webhook_url?: string;
  maps: AutoscalerMapCfg[];
  deep_desert?: DeepDesertConfig;
}
export interface AutoscalerRun {
  map: string;
  action: string;
  detail: string;
  at: string;
}
export interface AutoscalerLive {
  desired: number | null;
  current: number | null;
  status: string | null;
  players: number | null;
}
// One live DeepDesert dimension (a world_partition row). players is the farm_state
// connected count, trusted only when online (offline reads 0, never a phantom).
export interface AutoscalerDimLive {
  partition_id: number;
  dimension_index: number;
  online: boolean;
  players: number;
  label: string;
}
// The DD-pool live view: per-dim rows + aggregate, with floor/ceiling the scaler
// enforces. readable=false means the count was unconfirmed (show "—", never act blind).
export interface DeepDesertLive {
  readable: boolean;
  enabled: boolean;
  dims: AutoscalerDimLive[];
  live: number;
  declared: number;
  players: number;
  min_dims: number;
  max_dims: number;
}
export interface AutoscalerStatus {
  ok: boolean;
  config?: AutoscalerConfig;
  runs?: AutoscalerRun[];
  state?: Record<string, { idle_since: string | null; last_demand: string | null }>;
  live?: Record<string, AutoscalerLive>;
  sources?: { mockK8s: boolean; players: boolean };
  deep_desert?: DeepDesertLive;
  log_offset?: number | null;
  error?: string;
}
export interface AutoscalerTickResult {
  ok: boolean;
  actions?: { map: string; action: string; ok: boolean; detail: string }[];
  error?: string;
}

export const fetchAutoscaler = () => api<AutoscalerStatus>("GET", "/api/autoscaler");
// The autoscaler set-config takes the FULL validated config (not a patch).
export const autoscalerConfig = (cfg: AutoscalerConfig) =>
  api<{ ok: boolean; config?: AutoscalerConfig; error?: string }>("POST", "/api/autoscaler/config", cfg);
export const autoscalerTick = () =>
  api<AutoscalerTickResult>("POST", "/api/autoscaler/tick", {});

// ---- Logs + service control --------------------------------------------
export interface LogSource {
  name: string;
  exists: boolean;
}
export interface LogSourcesResp {
  ok: boolean;
  sources: LogSource[];
}
export interface LogTailResp {
  ok: boolean;
  source: string;
  exists: boolean;
  lines: string[];
  count: number;
  tail: number;
}

export const fetchLogSources = () => api<LogSourcesResp>("GET", "/api/logs/sources");
export const fetchLogs = (source: string, tail = 200) =>
  api<LogTailResp>("GET", `/api/logs?source=${encodeURIComponent(source)}&tail=${tail}`);
export const restartService = (service: string) =>
  api<PublishResult & { error?: string }>("POST", "/api/svc/restart", { service });
