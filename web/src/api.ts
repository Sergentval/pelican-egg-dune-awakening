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

export const fetchItems = (q: string, limit = 40) =>
  api<{ items: ItemRow[] }>("GET", `/api/lookup/items?q=${encodeURIComponent(q)}&limit=${limit}`);

export const fetchSkills = (q: string, limit = 50) =>
  api<{ skills: SkillRow[] }>("GET", `/api/lookup/skills?q=${encodeURIComponent(q)}&limit=${limit}`);

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
