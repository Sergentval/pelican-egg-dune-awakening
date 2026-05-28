// awakening.wiki integration.
//
// The community wiki at https://awakening.wiki/ exposes a public REST API
// at https://api.awakening.wiki/ (CORS open, no key needed). Images are
// served from media.awakening.wiki via MediaWiki's Special:FilePath
// redirector, so we don't have to compute the md5 hash bucket ourselves.
//
// Why this exists: dune.gaming.tools is Cloudflare-gated and refuses
// hotlinks. awakening.wiki is friendly to programmatic access — the
// images load directly into <img> tags from any origin.

const API_BASE = "https://api.awakening.wiki";
const WIKI_BASE = "https://awakening.wiki";

/** Build the image URL for a wiki File. Browser follows a 301 to the
 *  canonical media.awakening.wiki hash-bucket path. */
export function awakeningImageUrl(filename: string | null | undefined): string | null {
  if (!filename) return null;
  return `${WIKI_BASE}/Special:FilePath/${encodeURIComponent(filename)}`;
}

/** Open a wiki page by title (humans search this). */
export function awakeningPageUrl(title: string): string {
  // MediaWiki replaces spaces with underscores; URL-encode the rest.
  const t = title.replace(/\s+/g, "_");
  return `${WIKI_BASE}/${encodeURIComponent(t)}`;
}

/** Fall back to the wiki search when we don't have an exact page slug. */
export function awakeningSearch(query: string): string {
  return `${WIKI_BASE}/Special:Search?search=${encodeURIComponent(query)}&go=Go`;
}

// ──────────────────────────────────────────────────────────────────────
// API
// ──────────────────────────────────────────────────────────────────────

export interface WikiEntry {
  item_id?: string;
  name?: string;
  image?: string;
  long_description?: string;
  short_description?: string;
  added?: string;
  market_price?: string | number;
  max_stack?: string | number;
  volume?: string | number;
  /** Internal — set by us during batch enrichment. */
  _category?: string;
}

// In-memory cache keyed by item_id. Each entry is the WikiEntry object
// or `null` if we already tried and it's not in the wiki.
const cache = new Map<string, WikiEntry | null>();
// Pending lookups so concurrent callers share one fetch round-trip.
const pending = new Map<string, Promise<WikiEntry | null>>();

const CATEGORIES = ["items", "weapons", "tools", "garments", "consumables", "ammo", "resources", "contract_items"];

/** Get a wiki entry for a single item_id. Returns null if not found.
 *  Cached for the session. */
export async function lookupItemId(itemId: string): Promise<WikiEntry | null> {
  if (cache.has(itemId)) return cache.get(itemId) ?? null;
  if (pending.has(itemId)) return pending.get(itemId) ?? null;
  const work = (async () => {
    // Probe each category until we get a hit. We expect 1-2 calls
    // typical — items has 2274 entries so it's the most-likely first hit.
    for (const cat of CATEGORIES) {
      try {
        const url = `${API_BASE}/${cat}?where=(item_id,eq,${encodeURIComponent(itemId)})&limit=1`;
        const res = await fetch(url, { mode: "cors" });
        if (!res.ok) continue;
        const json = (await res.json()) as { list?: WikiEntry[] };
        const row = (json.list || [])[0];
        if (row) {
          row._category = cat;
          cache.set(itemId, row);
          return row;
        }
      } catch {
        // network/abort — try next
      }
    }
    cache.set(itemId, null);
    return null;
  })();
  pending.set(itemId, work);
  try {
    return await work;
  } finally {
    pending.delete(itemId);
  }
}

/** Batch lookup: hit each category once with an `in` filter for any
 *  uncached id, then merge. Cheaper than N round-trips. */
export async function lookupItemIdsBatch(itemIds: string[]): Promise<Record<string, WikiEntry | null>> {
  const out: Record<string, WikiEntry | null> = {};
  const todo: string[] = [];
  for (const id of itemIds) {
    if (!id) continue;
    if (cache.has(id)) {
      out[id] = cache.get(id) ?? null;
    } else {
      todo.push(id);
    }
  }
  if (todo.length === 0) return out;

  // Hit each category once with a batched `in` filter.
  const remaining = new Set(todo);
  for (const cat of CATEGORIES) {
    if (remaining.size === 0) break;
    const ids = Array.from(remaining);
    // Cap URL length — chunk if needed.
    for (let i = 0; i < ids.length; i += 50) {
      const chunk = ids.slice(i, i + 50);
      const where = `(item_id,in,${chunk.map((id) => encodeURIComponent(id)).join(",")})`;
      const url = `${API_BASE}/${cat}?where=${where}&limit=100`;
      try {
        const res = await fetch(url, { mode: "cors" });
        if (!res.ok) continue;
        const json = (await res.json()) as { list?: WikiEntry[] };
        for (const row of json.list || []) {
          if (!row.item_id) continue;
          row._category = cat;
          cache.set(row.item_id, row);
          out[row.item_id] = row;
          remaining.delete(row.item_id);
        }
      } catch {
        // ignore
      }
    }
  }

  // Anything left genuinely isn't in the wiki — cache the miss so we
  // don't re-probe across renders.
  for (const id of remaining) {
    cache.set(id, null);
    out[id] = null;
  }
  return out;
}

/** Best-effort page URL for a wiki entry. Uses the human name when we
 *  have one; otherwise falls back to a search by item_id. */
export function wikiLinkFor(entry: WikiEntry | null | undefined, fallbackId?: string): string {
  if (entry?.name) return awakeningPageUrl(entry.name);
  if (entry?.item_id) return awakeningSearch(entry.item_id);
  if (fallbackId) return awakeningSearch(fallbackId);
  return WIKI_BASE;
}
