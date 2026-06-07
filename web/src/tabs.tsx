// Every tab the SPA renders. Each tab is a React component that calls
// the corresponding admin-publish.sh subcommand via /admin/<sub>.

import { useEffect, useMemo, useState } from "react";
import { MAP_ROLE_META, mapDisplayName, mapRole, mapStatusPill } from "./mapNames";
import {
  armorSetClass,
  armorSetLabel,
  armorSetTier,
  clampNum,
  deleteItem,
  deleteVehicleActor,
  fetchInventory,
  detectSkillType,
  detectTier,
  isUniqueItem,
  fetchArmorSets,
  fetchHistory,
  fetchItemCategories,
  fetchItems,
  fetchPlayers,
  fetchSkillCategories,
  fetchSkills,
  fetchSpawnedVehicles,
  fetchSteamInfo,
  fetchVehicles,
  fetchStatus,
  fetchSettings,
  saveSettings,
  fetchWelcome,
  welcomeScan,
  welcomeRetryFailed,
  isUniqueArmor,
  itemCategoryStyle,
  parsePlayerTable,
  parseVehicleListOutput,
  publish,
  sanitizeIdent,
  sanitizeText,
  skillCategoryStyle,
  skillTypeIcon,
  vehicleActorToClass,
  vehicleIcon,
  type PlayerTable,
  vehicleImageFilename,
  type ArmorSet,
  type VehicleActor,
  type HistoryResponse,
  type ItemCategoryBucket,
  type ItemRow,
  type PlayerRow,
  type PublishResult,
  type SkillRow,
  type SteamPersona,
  type VehicleClass,
  type StatusGrid,
  type SettingItem,
  type SettingsResponse,
  type SettingsSaveResult,
  type WelcomeResponse,
  type WelcomeScanResult,
} from "./api";
import {
  awakeningImageUrl,
  awakeningPageUrl,
  awakeningSearch,
  lookupItemIdsBatch,
  wikiLinkFor,
  type WikiEntry,
} from "./awakening";
import {
  Confirm,
  PlayerPicker,
  pushToConsole,
  type ConsoleEntry,
} from "./components";
import { useTarget } from "./target";
import {
  BUILT_IN_KITS,
  KIT_GROUPS,
  loadCustomKits,
  saveCustomKits,
  type CustomKit,
  type Kit,
  type KitLine,
} from "./kits";

// Props shared by every tab — the parent passes setConsoleEntries so
// commands flow into the persistent output panel.
export interface TabProps {
  setConsoleEntries: React.Dispatch<React.SetStateAction<ConsoleEntry[]>>;
}

// Helper to run a publish + push into the console.
async function runAndLog(
  setConsoleEntries: TabProps["setConsoleEntries"],
  sub: string,
  body: Record<string, unknown>,
  label?: string,
): Promise<void> {
  const res = await publish(sub, body);
  const ok = res.ok && (res.body as PublishResult).ok;
  pushToConsole(
    setConsoleEntries,
    label || `${sub} ${JSON.stringify(body)}`,
    res.body as PublishResult,
    ok,
  );
}

// ---- Dashboard --------------------------------------------------------

export function Dashboard({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [steam, setSteam] = useState<Record<string, SteamPersona>>({});
  const [steamEnabled, setSteamEnabled] = useState<boolean | null>(null);

  async function refresh() {
    setLoading(true);
    const res = await fetchPlayers("all");
    let rows: PlayerRow[] = [];
    if (res.ok) {
      rows = parsePlayerTable((res.body as PublishResult).stdout);
      setPlayers(rows);
    }
    // Fetch Steam personas for any Steam id we know about.
    const steamIds = rows.map((r) => r.steam_id).filter((s): s is string => !!s && /^\d+$/.test(s));
    if (steamIds.length > 0) {
      const steamRes = await fetchSteamInfo(steamIds);
      if (steamRes.ok) {
        setSteam((steamRes.body as { players: Record<string, SteamPersona> }).players || {});
        setSteamEnabled((steamRes.body as { enabled: boolean }).enabled);
      }
    } else {
      setSteamEnabled(null);
    }
    setLoading(false);
  }

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 30000);
    return () => clearInterval(interval);
  }, []);

  const online = players.filter((p) => p.online === "Online").length;

  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="card lg:col-span-2">
        <header className="card-header">
          <h2 className="font-semibold">Players</h2>
          <div className="flex items-center gap-3">
            <span className="pill-ok">{online} online</span>
            <span className="text-xs text-slate-500">{players.length} total</span>
            <button className="btn-ghost text-xs" onClick={refresh} disabled={loading}>
              {loading ? "…" : "refresh"}
            </button>
          </div>
        </header>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-900/50 text-slate-400">
              <tr>
                <th className="text-left px-4 py-2 font-medium">Player</th>
                <th className="text-left px-4 py-2 font-medium">Steam</th>
                <th className="text-left px-4 py-2 font-medium">Status</th>
                <th className="text-left px-4 py-2 font-medium">Last activity</th>
                <th className="text-left px-4 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {players.length === 0 && (
                <tr>
                  <td colSpan={5} className="text-center text-slate-500 py-8">
                    {loading ? "Loading…" : "No players seen yet"}
                  </td>
                </tr>
              )}
              {players.map((p) => {
                const persona = p.steam_id ? steam[p.steam_id] : undefined;
                return (
                  <tr key={p.fls_id} className="border-t border-slate-800/50">
                    <td className="px-4 py-2">
                      <div className="text-slate-100">{p.character || <span className="font-mono text-slate-400">{p.fls_id.slice(0, 8)}…</span>}</div>
                      <div className="text-[10px] text-slate-500 font-mono">FLS {p.fls_id} · {p.life}</div>
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-2">
                        {persona?.avatar && (
                          <img src={persona.avatar} alt="" width={20} height={20} className="rounded" />
                        )}
                        <div>
                          {persona?.personaname ? (
                            <a
                              href={persona.profileurl}
                              target="_blank"
                              rel="noreferrer"
                              className="text-spice-300 hover:underline"
                            >
                              {persona.personaname}
                            </a>
                          ) : (
                            <span className="text-slate-500">{p.steam_id || "-"}</span>
                          )}
                          {persona?.personaname && (
                            <div className="text-[10px] text-slate-500 font-mono">{p.steam_id}</div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-2">
                      <span className={p.online === "Online" ? "pill-ok" : "pill"}>{p.online}</span>
                    </td>
                    <td className="px-4 py-2 text-slate-400">
                      {p.last_avatar_activity ? new Date(p.last_avatar_activity).toLocaleString() : "-"}
                    </td>
                    <td className="px-4 py-2">
                      <button
                        className="btn-ghost text-xs"
                        onClick={async () => {
                          const pid = p.character ? `name:${p.character}` : p.fls_id;
                          target.setPlayerId(pid);
                          const looked = await target.lookupPos(pid);
                          // Also surface in the per-session output log.
                          pushToConsole(
                            setConsoleEntries,
                            `pos ${pid}`,
                            looked
                              ? `${mapDisplayName(looked.map)} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`
                              : "pos lookup failed",
                            !!looked,
                          );
                        }}
                      >
                        pos
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {steamEnabled === false && players.some((p) => p.steam_id) && (
            <div className="px-4 py-2 text-xs text-slate-500 bg-slate-900/50 border-t border-slate-800">
              💡 Set <span className="font-mono text-slate-400">STEAM_API_KEY</span> in the Pelican egg variables to show Steam persona names and avatars here.
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Quick actions</h2>
        </header>
        <div className="p-4 space-y-2 text-sm">
          <button
            className="btn-ghost border border-slate-700 w-full text-left"
            onClick={() =>
              runAndLog(setConsoleEntries, "broadcast", {
                title: "Hello",
                body: "Server-side admin pipeline is live",
                duration: 12,
              }, 'broadcast "Hello" "..."')
            }
          >
            Send a hello broadcast
          </button>
          <p className="text-xs text-slate-500 mt-3 leading-relaxed">
            Pick a tab on the left for the full command. Every action lands in the Output console below the page.
          </p>
        </div>
      </div>
    </div>
  );
}

// ---- Broadcast --------------------------------------------------------

const BROADCAST_TITLE_MAX = 120;
const BROADCAST_BODY_MAX = 1000;

export function BroadcastTab({ setConsoleEntries }: TabProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [duration, setDuration] = useState(20);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    // Strip control chars one last time before submit — the on-change
    // sanitiser handles typing but a paste-then-submit could slip
    // through if the user paste-edits before we re-render.
    const safeTitle = sanitizeText(title, BROADCAST_TITLE_MAX);
    const safeBody = sanitizeText(body, BROADCAST_BODY_MAX);
    if (!safeTitle || !safeBody) return;
    await runAndLog(
      setConsoleEntries,
      "broadcast",
      { title: safeTitle, body: safeBody, duration },
      `broadcast "${safeTitle}"`,
    );
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Server-wide broadcast</h2>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <div>
          <label className="label" htmlFor="bcast-title">Title</label>
          <input
            id="bcast-title"
            required
            value={title}
            maxLength={BROADCAST_TITLE_MAX}
            onChange={(e) => setTitle(sanitizeText(e.target.value, BROADCAST_TITLE_MAX))}
            className="input-field"
            placeholder="Maintenance"
          />
        </div>
        <div>
          <label className="label" htmlFor="bcast-body">Body</label>
          <textarea
            id="bcast-body"
            required
            value={body}
            maxLength={BROADCAST_BODY_MAX}
            onChange={(e) => setBody(sanitizeText(e.target.value, BROADCAST_BODY_MAX))}
            className="input-field min-h-[80px]"
            placeholder="Restart in 5 minutes — save your work"
          />
          <p className="text-xs text-slate-500 mt-1">
            {body.length}/{BROADCAST_BODY_MAX}
          </p>
        </div>
        <div>
          <label className="label" htmlFor="bcast-dur">Display duration</label>
          <div className="flex items-center gap-2">
            <input
              id="bcast-dur"
              type="number"
              min={1}
              max={300}
              value={duration}
              onChange={(e) => setDuration(clampNum(e.target.value, 1, 300, 20))}
              className="input-field w-32"
            />
            <span className="text-xs text-slate-500">seconds</span>
          </div>
        </div>
        <button type="submit" className="btn-primary">Send</button>
      </form>
    </div>
  );
}

// ---- Items ------------------------------------------------------------

// Curated display order for category pills. Anything not in here falls
// to the end alphabetically.
const ITEM_CATEGORY_ORDER = [
  "weapons",
  "clothing",
  "consumables",
  "resources",
  "schematics",
  "buildings",
  "placeables",
  "customizations",
  "contracts",
];

export function ItemsTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>(""); // "" = all
  const [tierFilter, setTierFilter] = useState<string>(""); // "" = all
  const [uniqueFilter, setUniqueFilter] = useState<"" | "unique" | "standard">("");
  const [categories, setCategories] = useState<ItemCategoryBucket[]>([]);
  const [matches, setMatches] = useState<ItemRow[]>([]);
  const [wiki, setWiki] = useState<Record<string, WikiEntry | null>>({});
  const [picked, setPicked] = useState<ItemRow | null>(null);
  const [qty, setQty] = useState(1);
  const [durability, setDurability] = useState(1.0);
  const [searching, setSearching] = useState(false);

  // Load category list once.
  useEffect(() => {
    fetchItemCategories().then((res) => {
      if (res.ok) {
        const cats = (res.body as { categories: ItemCategoryBucket[] }).categories;
        // Sort by curated order, then by count desc.
        cats.sort((a, b) => {
          const ai = ITEM_CATEGORY_ORDER.indexOf(a.id);
          const bi = ITEM_CATEGORY_ORDER.indexOf(b.id);
          if (ai !== -1 && bi !== -1) return ai - bi;
          if (ai !== -1) return -1;
          if (bi !== -1) return 1;
          return b.count - a.count;
        });
        setCategories(cats);
      }
    });
  }, []);

  // Refetch matches when query or category changes.
  useEffect(() => {
    if (!query.trim() && !category) {
      setMatches([]);
      return;
    }
    const handle = setTimeout(async () => {
      setSearching(true);
      // When browsing a category give a bigger window — Funcom ships
      // ~290 weapons and ~280 garments so 500 covers both.
      const limit = category ? 500 : 40;
      const res = await fetchItems(query, limit, category);
      if (res.ok) {
        const list = (res.body as { items: ItemRow[] }).items;
        setMatches(list);
        lookupItemIdsBatch(list.map((it) => it.id)).then((map) =>
          setWiki((prev) => ({ ...prev, ...map })),
        );
      }
      setSearching(false);
    }, category && !query ? 0 : 200);
    return () => clearTimeout(handle);
  }, [query, category]);

  // Available tier badges in the current result set, for the tier-filter
  // chip row. Sorted naturally (T1 < T2 < … < T6 < Mk1 < … < Unique).
  const tiers = useMemo(() => {
    const seen = new Set<string>();
    for (const m of matches) {
      const t = detectTier(m.id);
      if (t) seen.add(t);
    }
    return Array.from(seen).sort();
  }, [matches]);

  const visible = useMemo(() => {
    let rows = matches;
    if (tierFilter) rows = rows.filter((m) => detectTier(m.id) === tierFilter);
    if (uniqueFilter === "unique") rows = rows.filter((m) => isUniqueItem(m.id));
    else if (uniqueFilter === "standard") rows = rows.filter((m) => !isUniqueItem(m.id));
    return rows;
  }, [matches, tierFilter, uniqueFilter]);

  // Whether the current view contains any Unique blueprints — controls
  // visibility of the Unique/Standard chip row.
  const hasUniques = useMemo(() => matches.some((m) => isUniqueItem(m.id)), [matches]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!picked) return;
    await runAndLog(setConsoleEntries, "give", {
      player_id: target.playerId,
      item: picked.id,
      qty,
      durability,
    }, `give ${target.playerId} ${picked.id} ×${qty}`);
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Grant item</h2>
        </header>
        <form onSubmit={submit} className="p-4 space-y-4">
          <PlayerPicker />
          <div>
            <label className="label">Search (any name or id)</label>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="spice, crysknife, stilltent, solari…"
              className="input-field"
            />
            {picked && (() => {
              const w = wiki[picked.id];
              const img = awakeningImageUrl(w?.image);
              return (
                <div className="mt-2 flex items-start gap-3 p-2 rounded bg-slate-800/50">
                  {img && (
                    <img
                      src={img}
                      alt=""
                      width={48}
                      height={48}
                      loading="lazy"
                      className="w-12 h-12 rounded bg-slate-900 object-contain border border-slate-700 shrink-0"
                    />
                  )}
                  <div className="min-w-0 flex-1 text-xs">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`font-mono ${itemCategoryStyle(picked.category).color}`}>{picked.id}</span>
                      <a
                        href={wikiLinkFor(w, picked.name || picked.id)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-slate-500 hover:text-spice-300"
                        title="View on awakening.wiki"
                      >
                        ↗
                      </a>
                      <button type="button" className="btn-ghost text-xs ml-auto" onClick={() => setPicked(null)}>
                        clear
                      </button>
                    </div>
                    <div className="text-slate-400">{w?.name || picked.name || "?"}</div>
                    {(w?.short_description || w?.long_description) && (
                      <div className="text-slate-500 mt-1 line-clamp-2">{w?.short_description || w?.long_description}</div>
                    )}
                  </div>
                </div>
              );
            })()}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="give-qty">Quantity</label>
              <input id="give-qty" type="number" min={1} value={qty} onChange={(e) => setQty(parseInt(e.target.value) || 1)} className="input-field" />
            </div>
            <div>
              <label className="label" htmlFor="give-dura">Durability</label>
              <input id="give-dura" type="number" min={0} max={1} step={0.05} value={durability} onChange={(e) => setDurability(parseFloat(e.target.value) || 1)} className="input-field" />
            </div>
          </div>
          <button type="submit" className="btn-primary" disabled={!picked}>Give</button>
        </form>
      </div>

      <div className="card flex flex-col min-h-0">
        <header className="card-header">
          <h2 className="font-semibold">Browse</h2>
          <span className="text-xs text-slate-500">
            {searching ? "searching…" : `${visible.length} of ${matches.length}`}
          </span>
        </header>

        {/* Category pill row */}
        <div className="px-4 pt-3 pb-2 border-b border-slate-800 space-y-2">
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => { setCategory(""); setTierFilter(""); }}
              className={
                "px-2 py-1 rounded-full border text-xs transition " +
                (category === ""
                  ? "border-spice-500 bg-spice-900/40 text-spice-200"
                  : "border-slate-700 text-slate-300 hover:bg-slate-800")
              }
            >
              All
            </button>
            {categories.map((c) => {
              const sty = itemCategoryStyle(c.id);
              return (
                <button
                  type="button"
                  key={c.id}
                  onClick={() => { setCategory(c.id); setTierFilter(""); }}
                  className={
                    "px-2 py-1 rounded-full border text-xs flex items-center gap-1.5 transition " +
                    (category === c.id
                      ? "border-spice-500 bg-spice-900/40 text-spice-200"
                      : "border-slate-700 text-slate-300 hover:bg-slate-800")
                  }
                >
                  <span aria-hidden>{sty.icon}</span>
                  <span className="capitalize">{c.id}</span>
                  <span className="text-slate-500 text-[10px]">{c.count}</span>
                </button>
              );
            })}
          </div>

          {/* Tier chips — only when a category is picked + multiple tiers in view */}
          {tiers.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wider text-slate-500">Tier</span>
              <button
                type="button"
                onClick={() => setTierFilter("")}
                className={
                  "px-2 py-0.5 rounded text-xs border transition " +
                  (tierFilter === ""
                    ? "border-slate-500 bg-slate-800 text-slate-100"
                    : "border-slate-800 text-slate-500 hover:bg-slate-800")
                }
              >
                all
              </button>
              {tiers.map((t) => (
                <button
                  type="button"
                  key={t}
                  onClick={() => setTierFilter(t === tierFilter ? "" : t)}
                  className={
                    "px-2 py-0.5 rounded text-xs border font-mono transition " +
                    (tierFilter === t
                      ? "border-spice-500 bg-spice-900/40 text-spice-300"
                      : "border-slate-800 text-slate-400 hover:bg-slate-800")
                  }
                >
                  {t}
                </button>
              ))}
            </div>
          )}

          {/* Unique vs Standard filter — only appears when the result
              set has uniques (e.g. browsing weapons / consumables). */}
          {hasUniques && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wider text-slate-500">Rarity</span>
              <button
                type="button"
                onClick={() => setUniqueFilter("")}
                className={
                  "px-2 py-0.5 rounded text-xs border transition " +
                  (uniqueFilter === ""
                    ? "border-slate-500 bg-slate-800 text-slate-100"
                    : "border-slate-800 text-slate-500 hover:bg-slate-800")
                }
              >
                all
              </button>
              <button
                type="button"
                onClick={() => setUniqueFilter(uniqueFilter === "unique" ? "" : "unique")}
                className={
                  "px-2 py-0.5 rounded text-xs border flex items-center gap-1 transition " +
                  (uniqueFilter === "unique"
                    ? "border-amber-500 bg-amber-900/40 text-amber-200"
                    : "border-slate-800 text-amber-300/70 hover:bg-amber-900/30")
                }
              >
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden />
                Unique
              </button>
              <button
                type="button"
                onClick={() => setUniqueFilter(uniqueFilter === "standard" ? "" : "standard")}
                className={
                  "px-2 py-0.5 rounded text-xs border transition " +
                  (uniqueFilter === "standard"
                    ? "border-slate-500 bg-slate-800 text-slate-100"
                    : "border-slate-800 text-slate-500 hover:bg-slate-800")
                }
              >
                Standard
              </button>
            </div>
          )}
        </div>

        <div className="flex-1 max-h-[600px] overflow-y-auto">
          {visible.length === 0 && (
            <div className="p-8 text-center text-slate-500 text-sm">
              {category
                ? "No items match the current category + tier filter."
                : "Pick a category above, or type to search 2558 items."}
            </div>
          )}
          {/* Grid view when browsing a category; list view for free-text */}
          {category && visible.length > 0 ? (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2 p-3">
              {visible.slice(0, 200).map((it) => {
                const sty = itemCategoryStyle(it.category);
                const w = wiki[it.id];
                const img = awakeningImageUrl(w?.image);
                const tier = detectTier(it.id);
                const unique = isUniqueItem(it.id);
                const active = picked?.id === it.id && picked?.source === it.source;
                const borderClass = active
                  ? "border-spice-500 bg-spice-900/30"
                  : unique
                    ? "border-amber-700/60 bg-amber-950/10 hover:border-amber-400/70"
                    : "border-slate-800 hover:border-spice-500/50 hover:bg-slate-800/50";
                const nameColor = unique ? "text-amber-200" : sty.color;
                const tierBadgeClass = unique
                  ? "bg-amber-900/80 border-amber-700 text-amber-200"
                  : "bg-slate-800/90 border-slate-700 text-spice-300";
                return (
                  <div
                    key={it.id + it.source}
                    className={"rounded border p-2 text-xs transition relative " + borderClass}
                  >
                    <button
                      type="button"
                      onClick={() => setPicked(it)}
                      className="w-full text-left flex flex-col items-center gap-1"
                    >
                      <div className="relative w-16 h-16 flex items-center justify-center">
                        {img ? (
                          <img
                            src={img}
                            alt=""
                            width={56}
                            height={56}
                            loading="lazy"
                            className="w-14 h-14 object-contain"
                          />
                        ) : (
                          <span className="text-3xl" aria-hidden>{sty.icon}</span>
                        )}
                        {tier && (
                          <span className={"absolute -bottom-1 -right-1 text-[9px] font-mono px-1 py-0.5 rounded border " + tierBadgeClass}>
                            {tier}
                          </span>
                        )}
                        {unique && (
                          <span
                            className="absolute -top-1 -left-1 w-2.5 h-2.5 rounded-full bg-amber-400 border border-amber-200/70 shadow shadow-amber-500/30"
                            title="Unique blueprint"
                            aria-label="Unique"
                          />
                        )}
                      </div>
                      <div className="text-center w-full">
                        <div className={`truncate ${nameColor}`}>{w?.name || it.name || it.id}</div>
                        <div className="text-[10px] text-slate-500 font-mono truncate">{it.id}</div>
                      </div>
                    </button>
                    <a
                      href={wikiLinkFor(w, it.name || it.id)}
                      target="_blank"
                      rel="noreferrer"
                      className="absolute top-1 right-1 text-slate-500 hover:text-spice-300 text-[10px]"
                      title="View on awakening.wiki"
                    >
                      ↗
                    </a>
                  </div>
                );
              })}
              {visible.length > 200 && (
                <div className="col-span-full text-center text-xs text-slate-500 py-3">
                  Showing first 200 — narrow the tier filter or search to see more.
                </div>
              )}
            </div>
          ) : (
            visible.map((it) => {
            const sty = itemCategoryStyle(it.category);
            const active = picked?.id === it.id && picked?.source === it.source;
            const w = wiki[it.id];
            const img = awakeningImageUrl(w?.image);
            return (
              <div
                key={it.id + it.source}
                className={
                  "flex items-center gap-2 border-b border-slate-800 hover:bg-slate-800 transition px-2 " +
                  (active ? "bg-spice-900/30" : "")
                }
              >
                <div className="w-8 h-8 shrink-0 flex items-center justify-center">
                  {img ? (
                    <img
                      src={img}
                      alt=""
                      width={32}
                      height={32}
                      loading="lazy"
                      className="w-8 h-8 rounded bg-slate-950 object-contain border border-slate-800"
                    />
                  ) : (
                    <span className="text-lg" aria-hidden>{sty.icon}</span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setPicked(it)}
                  className="flex-1 text-left py-2 text-xs min-w-0"
                >
                  <div className={`font-mono ${sty.color} truncate`}>{it.id}</div>
                  <div className="text-slate-400 mt-0.5 truncate">
                    {w?.name || it.name} <span className="text-slate-600">— {it.category} / {it.source}</span>
                  </div>
                </button>
                <a
                  href={wikiLinkFor(w, it.name || it.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="self-center px-3 py-2 text-slate-500 hover:text-spice-300"
                  title="View on awakening.wiki"
                  onClick={(e) => e.stopPropagation()}
                >
                  ↗
                </a>
              </div>
            );
            })
          )}
        </div>
      </div>
    </div>
  );
}

// ---- Skills -----------------------------------------------------------

// Curated display order for skill class pills. Anything not listed
// falls to the end alphabetically.
const SKILL_CATEGORY_ORDER = [
  "Swordmaster",
  "Trooper",
  "Mentat",
  "BeneGesserit",
  "Planetologist",
  "Hidden",
];

const SKILL_TYPES = [
  { id: "ability", label: "Abilities" },
  { id: "attribute", label: "Attributes" },
  { id: "perk", label: "Perks" },
  { id: "key", label: "Key skills" },
  { id: "spice", label: "Spice" },
  { id: "science", label: "Science" },
];

export function SkillsTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>(""); // "" = all classes
  const [typeFilter, setTypeFilter] = useState<string>(""); // "" = all types
  const [categories, setCategories] = useState<ItemCategoryBucket[]>([]);
  const [matches, setMatches] = useState<SkillRow[]>([]);
  const [picked, setPicked] = useState<SkillRow | null>(null);
  const [level, setLevel] = useState(1);
  const [unspent, setUnspent] = useState(50);
  const [xpAmount, setXpAmount] = useState(5000);

  // Class pills loaded once.
  useEffect(() => {
    fetchSkillCategories().then((res) => {
      if (res.ok) {
        const cats = (res.body as { categories: ItemCategoryBucket[] }).categories;
        cats.sort((a, b) => {
          const ai = SKILL_CATEGORY_ORDER.indexOf(a.id);
          const bi = SKILL_CATEGORY_ORDER.indexOf(b.id);
          if (ai !== -1 && bi !== -1) return ai - bi;
          if (ai !== -1) return -1;
          if (bi !== -1) return 1;
          return b.count - a.count;
        });
        setCategories(cats);
      }
    });
  }, []);

  // Refetch when query / category changes.
  useEffect(() => {
    if (!query.trim() && !category) {
      setMatches([]);
      return;
    }
    const handle = setTimeout(async () => {
      const limit = category ? 200 : 60;
      const res = await fetchSkills(query, limit, category);
      if (res.ok) setMatches((res.body as { skills: SkillRow[] }).skills);
    }, category && !query ? 0 : 200);
    return () => clearTimeout(handle);
  }, [query, category]);

  // Available type chips for the current result set.
  const availableTypes = useMemo(() => {
    const seen = new Set<string>();
    for (const m of matches) {
      const t = detectSkillType(m.id);
      if (t) seen.add(t);
    }
    return SKILL_TYPES.filter((t) => seen.has(t.id));
  }, [matches]);

  const visible = useMemo(
    () => (typeFilter ? matches.filter((m) => detectSkillType(m.id) === typeFilter) : matches),
    [matches, typeFilter],
  );

  async function submitSkill(e: React.FormEvent) {
    e.preventDefault();
    if (!picked) return;
    await runAndLog(setConsoleEntries, "skill", {
      player_id: target.playerId,
      module: picked.id,
      level,
    }, `skill ${target.playerId} ${picked.id} =${level}`);
  }

  async function submitPoints(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "points", { player_id: target.playerId, amount: unspent }, `points ${target.playerId} =${unspent}`);
  }

  async function submitXp(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "xp", { player_id: target.playerId, amount: xpAmount }, `xp ${target.playerId} +${xpAmount}`);
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="space-y-6">
        <div className="card">
          <header className="card-header">
            <h2 className="font-semibold">Set skill module level</h2>
          </header>
          <form onSubmit={submitSkill} className="p-4 space-y-4">
            <PlayerPicker />
            <div>
              <label className="label">Module</label>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="swordmaster, bene, voice, blade…"
                className="input-field"
              />
              {picked && (
                <div className="mt-2 text-xs text-slate-300 flex items-center gap-2 flex-wrap">
                  <span aria-hidden>{skillCategoryStyle(picked.category).icon}</span>
                  <span className={`font-mono ${skillCategoryStyle(picked.category).color}`}>{picked.id}</span>
                  <span className="text-slate-500">({picked.name || "?"})</span>
                  <a
                    href={awakeningSearch(picked.name || picked.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="text-slate-500 hover:text-spice-300"
                    title="View on awakening.wiki"
                  >
                    ↗
                  </a>
                  <button type="button" className="btn-ghost text-xs ml-auto" onClick={() => setPicked(null)}>
                    clear
                  </button>
                </div>
              )}
            </div>
            <div>
              <label className="label" htmlFor="skill-level">Level</label>
              <input id="skill-level" type="number" min={0} max={picked?.maxLevel || 5} value={level} onChange={(e) => setLevel(parseInt(e.target.value) || 0)} className="input-field" />
              {picked?.maxLevel && <p className="text-xs text-slate-500 mt-1">max {picked.maxLevel}</p>}
            </div>
            <button type="submit" className="btn-primary" disabled={!picked}>Grant</button>
          </form>
        </div>

        <div className="card">
          <header className="card-header">
            <h2 className="font-semibold">Unspent skill points</h2>
          </header>
          <form onSubmit={submitPoints} className="p-4 space-y-4">
            <PlayerPicker />
            <div>
              <label className="label" htmlFor="unspent-amt">Amount</label>
              <input id="unspent-amt" type="number" min={0} value={unspent} onChange={(e) => setUnspent(parseInt(e.target.value) || 0)} className="input-field" />
            </div>
            <button type="submit" className="btn-primary">Set</button>
          </form>
        </div>

        <div className="card">
          <header className="card-header">
            <h2 className="font-semibold">Award XP</h2>
          </header>
          <form onSubmit={submitXp} className="p-4 space-y-4">
            <PlayerPicker />
            <div>
              <label className="label" htmlFor="xp-amt">Amount</label>
              <div className="flex items-center gap-2">
                <input id="xp-amt" type="number" min={1} value={xpAmount} onChange={(e) => setXpAmount(parseInt(e.target.value) || 0)} className="input-field flex-1" />
                <div className="flex gap-1">
                  {[1000, 5000, 25000].map((n) => (
                    <button key={n} type="button" onClick={() => setXpAmount(n)} className={"btn-ghost text-xs border border-slate-700 " + (xpAmount === n ? "bg-slate-800 text-spice-300" : "")}>
                      {n.toLocaleString()}
                    </button>
                  ))}
                </div>
              </div>
              <p className="text-xs text-slate-500 mt-1">Category="Combat" is auto-injected (Funcom's handler no-ops without it).</p>
            </div>
            <button type="submit" className="btn-primary">Grant</button>
          </form>
        </div>
      </div>

      <div className="card flex flex-col min-h-0">
        <header className="card-header">
          <h2 className="font-semibold">Browse modules</h2>
          <span className="text-xs text-slate-500">
            {visible.length} of {matches.length}
          </span>
        </header>

        {/* Class pill row */}
        <div className="px-4 pt-3 pb-2 border-b border-slate-800 space-y-2">
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => { setCategory(""); setTypeFilter(""); }}
              className={
                "px-2 py-1 rounded-full border text-xs transition " +
                (category === ""
                  ? "border-spice-500 bg-spice-900/40 text-spice-200"
                  : "border-slate-700 text-slate-300 hover:bg-slate-800")
              }
            >
              All
            </button>
            {categories.map((c) => {
              const sty = skillCategoryStyle(c.id);
              return (
                <button
                  type="button"
                  key={c.id}
                  onClick={() => { setCategory(c.id); setTypeFilter(""); }}
                  className={
                    "px-2 py-1 rounded-full border text-xs flex items-center gap-1.5 transition " +
                    (category === c.id
                      ? "border-spice-500 bg-spice-900/40 text-spice-200"
                      : "border-slate-700 text-slate-300 hover:bg-slate-800")
                  }
                >
                  <span aria-hidden>{sty.icon}</span>
                  <span>{c.id}</span>
                  <span className="text-slate-500 text-[10px]">{c.count}</span>
                </button>
              );
            })}
          </div>

          {/* Type filter — Ability / Attribute / Perk / Key / Spice / Science */}
          {availableTypes.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wider text-slate-500">Type</span>
              <button
                type="button"
                onClick={() => setTypeFilter("")}
                className={
                  "px-2 py-0.5 rounded text-xs border transition " +
                  (typeFilter === ""
                    ? "border-slate-500 bg-slate-800 text-slate-100"
                    : "border-slate-800 text-slate-500 hover:bg-slate-800")
                }
              >
                all
              </button>
              {availableTypes.map((t) => (
                <button
                  type="button"
                  key={t.id}
                  onClick={() => setTypeFilter(t.id === typeFilter ? "" : t.id)}
                  className={
                    "px-2 py-0.5 rounded text-xs border flex items-center gap-1 transition " +
                    (typeFilter === t.id
                      ? "border-spice-500 bg-spice-900/40 text-spice-300"
                      : "border-slate-800 text-slate-400 hover:bg-slate-800")
                  }
                >
                  <span aria-hidden>{skillTypeIcon(t.id)}</span>
                  {t.label}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex-1 max-h-[600px] overflow-y-auto">
          {visible.length === 0 && (
            <div className="p-8 text-center text-slate-500 text-sm">
              {category
                ? "No modules match the current class + type filter."
                : "Pick a class above, or type to search 145 modules."}
            </div>
          )}

          {/* Grid view when browsing a class; list view when free-text searching */}
          {category && visible.length > 0 ? (
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-2 p-3">
              {visible.map((s) => {
                const sty = skillCategoryStyle(s.category);
                const type = detectSkillType(s.id);
                const active = picked?.id === s.id;
                return (
                  <div
                    key={s.id}
                    className={
                      "rounded border p-2 text-xs transition relative " +
                      (active
                        ? "border-spice-500 bg-spice-900/30"
                        : "border-slate-800 hover:border-spice-500/50 hover:bg-slate-800/50")
                    }
                  >
                    <button
                      type="button"
                      onClick={() => setPicked(s)}
                      className="w-full text-left"
                    >
                      <div className="flex items-center gap-1.5 mb-1">
                        <span className="text-lg" aria-hidden>{sty.icon}</span>
                        <span className={`font-semibold ${sty.color} truncate flex-1`}>{s.name || s.id}</span>
                      </div>
                      <div className="flex items-center gap-1 flex-wrap text-[10px]">
                        {type && (
                          <span className="px-1.5 py-0.5 rounded bg-slate-800/80 border border-slate-700 text-slate-300 flex items-center gap-1">
                            <span aria-hidden>{skillTypeIcon(type)}</span>
                            {type}
                          </span>
                        )}
                        <span className="px-1.5 py-0.5 rounded bg-slate-800/80 border border-slate-700 text-slate-400">
                          max {s.maxLevel ?? "?"}
                        </span>
                      </div>
                      <div className="text-slate-500 font-mono text-[10px] mt-1 truncate">{s.id}</div>
                    </button>
                    <a
                      href={awakeningSearch(s.name || s.id)}
                      target="_blank"
                      rel="noreferrer"
                      className="absolute top-1 right-1 text-slate-500 hover:text-spice-300 text-[10px]"
                      title="View on awakening.wiki"
                    >
                      ↗
                    </a>
                  </div>
                );
              })}
            </div>
          ) : (
            visible.map((s) => {
              const sty = skillCategoryStyle(s.category);
              const type = detectSkillType(s.id);
              const active = picked?.id === s.id;
              return (
                <div
                  key={s.id}
                  className={
                    "flex items-start gap-2 border-b border-slate-800 hover:bg-slate-800 transition " +
                    (active ? "bg-spice-900/30" : "")
                  }
                >
                  <button
                    type="button"
                    onClick={() => setPicked(s)}
                    className="flex-1 text-left px-4 py-2 text-xs min-w-0"
                  >
                    <div className="flex items-center gap-1.5">
                      <span aria-hidden>{sty.icon}</span>
                      <span className={`font-mono ${sty.color}`}>{s.id}</span>
                      {type && <span className="text-slate-500 text-[10px]">{skillTypeIcon(type)}</span>}
                    </div>
                    <div className="text-slate-400 mt-0.5 truncate">
                      {s.name} <span className="text-slate-600">— {s.category} (max {s.maxLevel})</span>
                    </div>
                  </button>
                  <a
                    href={awakeningSearch(s.name || s.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="self-center px-3 py-2 text-slate-500 hover:text-spice-300"
                    title="View on awakening.wiki"
                    onClick={(e) => e.stopPropagation()}
                  >
                    ↗
                  </a>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

// ---- Vehicles --------------------------------------------------------

// Vehicle factions accepted by the seabass handler. Free-text per
// adainrivers' spec, but these five cover the in-game choices.
const FACTIONS = ["", "Atreides", "Harkonnen", "Choam", "Smuggler", "Spacing Guild"] as const;

interface VehiclePreset {
  id: string; // local random id
  name: string;
  class: string;
  template: string;
  faction: string;
  persistent: number;
  zOffset: number;
}
const PRESETS_KEY = "dune-admin-vehicle-presets";

function loadPresets(): VehiclePreset[] {
  try {
    const raw = localStorage.getItem(PRESETS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}
function savePresets(presets: VehiclePreset[]): void {
  localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
}

export function VehiclesTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [vehicles, setVehicles] = useState<VehicleClass[]>([]);
  const [className, setClassName] = useState("");
  const [tplName, setTplName] = useState("");
  const [tplCustom, setTplCustom] = useState(""); // free-text override
  const [useCustomTpl, setUseCustomTpl] = useState(false);
  // Local X/Y/Z override — initializes from the shared target.pos but
  // the operator can edit before spawning.
  const [override, setOverride] = useState<{ x: number; y: number; z: number; dirty: boolean }>({ x: 0, y: 0, z: 0, dirty: false });
  const [rotation, setRotation] = useState<number | "">("");
  const [persistent, setPersistent] = useState(1.0);
  const [faction, setFaction] = useState("");
  const [zOffset, setZOffset] = useState(0);
  const [spawnCount, setSpawnCount] = useState(1);
  const [presets, setPresets] = useState<VehiclePreset[]>(loadPresets);
  const [presetNameDraft, setPresetNameDraft] = useState("");

  const baseX = override.dirty ? override.x : target.pos?.x !== undefined ? Math.round(target.pos.x) : 0;
  const baseY = override.dirty ? override.y : target.pos?.y !== undefined ? Math.round(target.pos.y) : 0;
  const baseZ = override.dirty ? override.z : target.pos?.z !== undefined ? Math.round(target.pos.z) : 0;
  const effectiveTpl = useCustomTpl ? tplCustom.trim() : tplName;

  useEffect(() => {
    fetchVehicles().then((res) => {
      if (res.ok) setVehicles((res.body as { vehicles: VehicleClass[] }).vehicles);
    });
  }, []);

  const currentTpls = useMemo(() => vehicles.find((v) => v.id === className)?.templates || [], [vehicles, className]);

  // Auto-pick first template when class changes.
  useEffect(() => {
    if (currentTpls.length && !currentTpls.includes(tplName)) setTplName(currentTpls[0]);
  }, [currentTpls, tplName]);

  async function fetchAndFillPos() {
    const looked = await target.lookupPos();
    if (looked) {
      setOverride({ x: 0, y: 0, z: 0, dirty: false });
      pushToConsole(
        setConsoleEntries,
        `pos ${target.playerId}`,
        `${mapDisplayName(looked.map)} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`,
        true,
      );
    } else {
      pushToConsole(setConsoleEntries, `pos ${target.playerId}`, target.posError || "lookup failed", false);
    }
  }

  function setXyz(axis: "x" | "y" | "z", v: number) {
    setOverride((prev) => ({
      x: axis === "x" ? v : prev.dirty ? prev.x : baseX,
      y: axis === "y" ? v : prev.dirty ? prev.y : baseY,
      z: axis === "z" ? v : prev.dirty ? prev.z : baseZ,
      dirty: true,
    }));
  }

  function applyPreset(p: VehiclePreset) {
    setClassName(p.class);
    setTplName(p.template);
    setUseCustomTpl(false);
    setFaction(p.faction);
    setPersistent(p.persistent);
    setZOffset(p.zOffset);
  }

  function saveCurrentAsPreset() {
    const name = presetNameDraft.trim();
    if (!name || !className || !effectiveTpl) return;
    const next: VehiclePreset[] = [
      ...presets.filter((p) => p.name !== name),
      {
        id: Math.random().toString(36).slice(2),
        name,
        class: className,
        template: effectiveTpl,
        faction,
        persistent,
        zOffset,
      },
    ];
    setPresets(next);
    savePresets(next);
    setPresetNameDraft("");
  }

  function deletePreset(id: string) {
    const next = presets.filter((p) => p.id !== id);
    setPresets(next);
    savePresets(next);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!className || !effectiveTpl) return;
    // Spawn 1..N copies. Multiples stagger Z by 200 so flying vehicles
    // don't all spawn inside each other.
    const count = Math.max(1, Math.min(10, spawnCount));
    for (let i = 0; i < count; i++) {
      const body: Record<string, unknown> = {
        player_id: target.playerId,
        class: className,
        x: baseX,
        y: baseY,
        z: baseZ + zOffset + (count > 1 ? i * 200 : 0),
        template: effectiveTpl,
        persistent,
      };
      if (rotation !== "") body["rotation"] = rotation;
      if (faction) body["faction"] = faction;
      const label =
        `vehicle ${target.playerId} ${className}/${effectiveTpl}` +
        (faction ? ` faction=${faction}` : "") +
        (count > 1 ? ` (${i + 1}/${count})` : "");
      // eslint-disable-next-line no-await-in-loop
      await runAndLog(setConsoleEntries, "vehicle", body, label);
    }
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="card">
      <header className="card-header">
        <h2 className="font-semibold">Spawn vehicle</h2>
        <span className="text-xs text-slate-500">{vehicles.length} classes available</span>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <PlayerPicker />

        <div>
          <label className="label">Class</label>
          <div className="grid grid-cols-3 sm:grid-cols-5 gap-2 mb-2">
            {vehicles.map((v) => {
              const imgFile = vehicleImageFilename(v.id);
              const imgUrl = awakeningImageUrl(imgFile);
              return (
                <button
                  type="button"
                  key={v.id}
                  onClick={() => setClassName(v.id)}
                  className={
                    "flex flex-col items-center gap-1 px-2 py-3 rounded border text-xs transition " +
                    (className === v.id
                      ? "border-spice-500 bg-spice-900/40 text-spice-100"
                      : "border-slate-700 hover:border-slate-500 text-slate-300")
                  }
                >
                  <div className="w-14 h-14 flex items-center justify-center">
                    {imgUrl ? (
                      <img
                        src={imgUrl}
                        alt=""
                        width={56}
                        height={56}
                        loading="lazy"
                        className="w-14 h-14 object-contain"
                      />
                    ) : (
                      <span className="text-3xl leading-none" aria-hidden>{vehicleIcon(v.id)}</span>
                    )}
                  </div>
                  <span className="truncate w-full text-center">{v.id}</span>
                </button>
              );
            })}
          </div>
          {className && (
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>
                {vehicles.find((v) => v.id === className)?.templates.length || 0} templates available
              </span>
              <a
                href={awakeningPageUrl(className)}
                target="_blank"
                rel="noreferrer"
                className="text-slate-500 hover:text-spice-300"
              >
                view {className} on awakening.wiki ↗
              </a>
            </div>
          )}
        </div>
        <div>
          <div className="flex items-center justify-between mb-1.5 gap-2 flex-wrap">
            <label className="label !mb-0" htmlFor="vh-tpl">Template</label>
            <label className="text-xs text-slate-400 flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={useCustomTpl}
                onChange={(e) => setUseCustomTpl(e.target.checked)}
                className="accent-spice-500"
              />
              custom (free-text)
            </label>
          </div>
          {useCustomTpl ? (
            <input
              id="vh-tpl-custom"
              type="text"
              value={tplCustom}
              onChange={(e) => setTplCustom(e.target.value)}
              placeholder="e.g. T6_Combat, T4_Inventory, or a new id Funcom shipped"
              className="input-field font-mono"
            />
          ) : (
            <select id="vh-tpl" value={tplName} onChange={(e) => setTplName(e.target.value)} className="input-field" disabled={currentTpls.length === 0}>
              {currentTpls.length === 0 && <option value="">Pick a class first</option>}
              {currentTpls.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="vh-faction">Faction</label>
            <select id="vh-faction" value={faction} onChange={(e) => setFaction(e.target.value)} className="input-field">
              {FACTIONS.map((f) => (
                <option key={f || "default"} value={f}>{f || "Default (CHOAM)"}</option>
              ))}
            </select>
            <p className="text-xs text-slate-500 mt-1">Overrides the vehicle skin; blank = ship default.</p>
          </div>
          <div>
            <label className="label" htmlFor="vh-count">Spawn count</label>
            <div className="flex items-center gap-2">
              <input id="vh-count" type="number" min={1} max={10} value={spawnCount} onChange={(e) => setSpawnCount(parseInt(e.target.value) || 1)} className="input-field w-24" />
              <div className="flex gap-1">
                {[1, 3, 5].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setSpawnCount(n)}
                    className={
                      "btn-ghost text-xs border border-slate-700 " +
                      (spawnCount === n ? "bg-slate-800 text-spice-300" : "")
                    }
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>
            <p className="text-xs text-slate-500 mt-1">Multiple copies stagger Z by +200 each to avoid clipping.</p>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5 gap-2 flex-wrap">
            <span className="label !mb-0">Position</span>
            <div className="flex items-center gap-2">
              {target.pos && !override.dirty && (
                <span className="text-xs text-slate-500">
                  from <span className="font-mono text-slate-400">{target.pos.source}</span> @ {target.pos.map}
                </span>
              )}
              {override.dirty && (
                <button type="button" className="btn-ghost text-xs" onClick={() => setOverride({ x: 0, y: 0, z: 0, dirty: false })}>
                  reset to target
                </button>
              )}
              <button type="button" className="btn-ghost text-xs" onClick={fetchAndFillPos} disabled={target.posLoading}>
                {target.posLoading ? "…" : `lookup ${target.playerId}`}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <input type="number" placeholder="X" value={baseX} onChange={(e) => setXyz("x", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Y" value={baseY} onChange={(e) => setXyz("y", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Z" value={baseZ} onChange={(e) => setXyz("z", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
          </div>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <span className="text-xs text-slate-500">Z offset:</span>
            <input
              type="number"
              value={zOffset}
              onChange={(e) => setZOffset(parseFloat(e.target.value) || 0)}
              className="input-field font-mono w-24"
            />
            {[0, 100, 200, 500].map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => setZOffset(n)}
                className={
                  "btn-ghost text-xs border border-slate-700 " +
                  (zOffset === n ? "bg-slate-800 text-spice-300" : "")
                }
              >
                +{n}
              </button>
            ))}
            <span className="text-xs text-slate-500">
              spawn Z = {baseZ + zOffset}
            </span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="vh-rot">Rotation (yaw, optional)</label>
            <input id="vh-rot" type="number" value={rotation} onChange={(e) => setRotation(e.target.value === "" ? "" : parseFloat(e.target.value))} className="input-field" placeholder="auto" />
          </div>
          <div>
            <label className="label" htmlFor="vh-pst">Persistent</label>
            <select id="vh-pst" value={persistent} onChange={(e) => setPersistent(parseFloat(e.target.value))} className="input-field">
              <option value={1.0}>1.0 — survives restart</option>
              <option value={0.0}>0.0 — transient</option>
            </select>
          </div>
        </div>

        <div className="border border-slate-800 rounded p-3 space-y-2 bg-slate-950/40">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="label !mb-0">Presets</span>
            <span className="text-xs text-slate-500">{presets.length} saved locally</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {presets.length === 0 && (
              <span className="text-xs text-slate-500 italic">No presets yet — fill the form, then name + save below.</span>
            )}
            {presets.map((p) => (
              <div key={p.id} className="flex items-center gap-1 border border-slate-700 rounded">
                <button
                  type="button"
                  className="px-2 py-1 text-xs hover:bg-slate-800"
                  onClick={() => applyPreset(p)}
                  title={`${p.class} / ${p.template}${p.faction ? ` · ${p.faction}` : ""} · z+${p.zOffset}`}
                >
                  <span className="text-spice-300">{p.name}</span>
                </button>
                <button
                  type="button"
                  className="px-2 py-1 text-xs text-slate-500 hover:text-red-400"
                  onClick={() => deletePreset(p.id)}
                  title="delete preset"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={presetNameDraft}
              onChange={(e) => setPresetNameDraft(e.target.value)}
              placeholder="preset name (e.g. ‘scout thopter’)"
              className="input-field text-xs flex-1"
            />
            <button
              type="button"
              className="btn-ghost border border-slate-700 text-xs"
              onClick={saveCurrentAsPreset}
              disabled={!presetNameDraft.trim() || !className || !effectiveTpl}
            >
              save current
            </button>
          </div>
        </div>

        <button type="submit" className="btn-primary" disabled={!className || !effectiveTpl}>
          Spawn{spawnCount > 1 ? ` ×${spawnCount}` : ""}
        </button>
      </form>
      </div>

      <SpawnedVehiclesPanel setConsoleEntries={setConsoleEntries} />
    </div>
  );
}

// ---- Manage spawned vehicles (DB-direct delete) ------------------------
//
// Funcom doesn't expose a DespawnVehicle ServerCommand (verified by
// the audit) — we go directly at `dune.actors`. The DELETE cascades
// through every FK we audited; the live game eventually resyncs the
// player's view (immediate for nearby vehicles, on next respawn for
// further ones).
function SpawnedVehiclesPanel({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [vehicles, setVehicles] = useState<VehicleActor[]>([]);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);

  async function refresh() {
    setLoading(true);
    const res = await fetchSpawnedVehicles();
    if (res.ok) {
      const stdout = (res.body as PublishResult).stdout || "";
      setVehicles(parseVehicleListOutput(stdout));
    }
    setLoading(false);
  }

  useEffect(() => { refresh(); }, []);

  async function doDelete(actorId: number) {
    setConfirmId(null);
    setDeleting(actorId);
    const res = await deleteVehicleActor(actorId);
    const ok = res.ok && (res.body as PublishResult).ok;
    pushToConsole(setConsoleEntries, `vehicle-delete actor=${actorId}`, res.body as PublishResult, ok);
    setDeleting(null);
    if (ok) refresh();
  }

  // Distance from the shared target.pos (when known) so the user can
  // sort visually by proximity to themselves.
  const withDistance = useMemo(() => {
    if (!target.pos) return vehicles.map((v) => ({ v, dist: null as number | null }));
    return vehicles
      .map((v) => {
        const dx = v.x - target.pos!.x;
        const dy = v.y - target.pos!.y;
        const dz = v.z - target.pos!.z;
        return { v, dist: Math.sqrt(dx * dx + dy * dy + dz * dz) };
      })
      .sort((a, b) => (a.dist ?? Infinity) - (b.dist ?? Infinity));
  }, [vehicles, target.pos]);

  const confirmEntry = confirmId !== null ? vehicles.find((v) => v.id === confirmId) : null;

  return (
    <div className="card">
      <header className="card-header">
        <h2 className="font-semibold">Spawned vehicles</h2>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">{vehicles.length} active</span>
          <button className="btn-ghost text-xs" onClick={refresh} disabled={loading}>
            {loading ? "…" : "refresh"}
          </button>
        </div>
      </header>
      <div className="p-4 space-y-3">
        <div className="text-xs text-amber-200 bg-amber-950/40 border border-amber-800/60 rounded px-3 py-2 space-y-1">
          <div className="font-semibold">⚠ Delete clears the DB row, but UE5 caches the actor in memory.</div>
          <div className="text-amber-300/80">
            Funcom doesn't ship a DespawnVehicle ServerCommand (35 candidates probed, all rejected).
            The vehicle stays visible in-game until the Sietch resyncs.
            To actually see the despawn now, choose one of:
          </div>
          <ul className="list-disc list-inside text-amber-300/80 marker:text-amber-500 space-y-0.5 pl-1">
            <li>The affected player logs out + back in</li>
            <li>The player moves far enough away that the vehicle leaves their relevance bubble, then returns</li>
            <li>Restart the server (Pelican panel <span className="font-mono">Power → Restart</span>) — guaranteed clean state on next boot</li>
          </ul>
        </div>
        <p className="text-xs text-slate-400">
          Source of truth: <span className="font-mono text-slate-300">dune.actors</span>. Deleting a row cascades through every FK (inventory, state, attachments) so no orphans are left behind.
          {target.pos && <span className="text-spice-300/80"> Sorted by distance from {target.playerId}.</span>}
        </p>
        {withDistance.length === 0 && (
          <div className="text-sm text-slate-500 italic py-4 text-center">
            {loading ? "Loading…" : "No vehicles currently spawned."}
          </div>
        )}
        <ul className="space-y-1.5">
          {withDistance.map(({ v, dist }) => {
            const cls = vehicleActorToClass(v.classShort);
            const imgUrl = awakeningImageUrl(vehicleImageFilename(cls));
            return (
              <li key={v.id} className="flex items-center gap-3 p-2 rounded border border-slate-800 bg-slate-900/50">
                <div className="w-10 h-10 shrink-0 flex items-center justify-center">
                  {imgUrl ? (
                    <img src={imgUrl} alt="" width={40} height={40} loading="lazy" className="w-10 h-10 object-contain" />
                  ) : (
                    <span className="text-2xl">{vehicleIcon(cls || "")}</span>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-slate-200 truncate">
                    {cls || v.classShort} <span className="text-xs text-slate-500 font-mono">#{v.id}</span>
                  </div>
                  <div className="text-[10px] text-slate-500 font-mono">
                    {mapDisplayName(v.map)} · p{v.partition} · X={Math.round(v.x)} Y={Math.round(v.y)} Z={Math.round(v.z)}
                    {dist !== null && (
                      <span className="ml-2 text-spice-300/70">{Math.round(dist).toLocaleString()} from {target.playerId}</span>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn-danger text-xs"
                  onClick={() => setConfirmId(v.id)}
                  disabled={deleting !== null}
                >
                  {deleting === v.id ? "deleting…" : "delete"}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <Confirm
        open={confirmId !== null}
        title="Delete vehicle?"
        message={
          confirmEntry
            ? `Permanently remove ${vehicleActorToClass(confirmEntry.classShort) || confirmEntry.classShort} #${confirmEntry.id} from ${mapDisplayName(confirmEntry.map)}. Removes the postgres row + cascades the vehicle's inventory and state. UE5 keeps it visible in-game until the Sietch resyncs — relog or server-restart to see the despawn. Unrecoverable.`
            : ""
        }
        confirmLabel="Delete row"
        onConfirm={() => confirmId !== null && doDelete(confirmId)}
        onCancel={() => setConfirmId(null)}
      />
    </div>
  );
}

// ---- Movement (teleport + tpsafe + position lookup) --------------------

export function MovementTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [override, setOverride] = useState<{ x: number; y: number; z: number; dirty: boolean }>({ x: 0, y: 0, z: 0, dirty: false });
  const [yaw, setYaw] = useState<number | "">("");
  const [mode, setMode] = useState<"teleport" | "tpsafe">("teleport");

  const x = override.dirty ? override.x : target.pos?.x !== undefined ? Math.round(target.pos.x) : 0;
  const y = override.dirty ? override.y : target.pos?.y !== undefined ? Math.round(target.pos.y) : 0;
  const z = override.dirty ? override.z : target.pos?.z !== undefined ? Math.round(target.pos.z) : 0;

  function setXyz(axis: "x" | "y" | "z", v: number) {
    setOverride((prev) => ({
      x: axis === "x" ? v : prev.dirty ? prev.x : x,
      y: axis === "y" ? v : prev.dirty ? prev.y : y,
      z: axis === "z" ? v : prev.dirty ? prev.z : z,
      dirty: true,
    }));
  }

  async function lookupPos() {
    const looked = await target.lookupPos();
    if (looked) {
      setOverride({ x: 0, y: 0, z: 0, dirty: false });
      pushToConsole(
        setConsoleEntries,
        `pos ${target.playerId}`,
        `${mapDisplayName(looked.map)} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`,
        true,
      );
    } else {
      pushToConsole(setConsoleEntries, `pos ${target.playerId}`, target.posError || "lookup failed", false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { player_id: target.playerId, x, y, z };
    if (yaw !== "") body["yaw"] = yaw;
    await runAndLog(setConsoleEntries, mode, body, `${mode} ${target.playerId} → ${x},${y},${z}`);
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Teleport</h2>
        <div className="flex gap-1">
          <button
            type="button"
            className={`btn-ghost text-xs ${mode === "teleport" ? "bg-slate-800 text-spice-300" : ""}`}
            onClick={() => setMode("teleport")}
          >
            exact
          </button>
          <button
            type="button"
            className={`btn-ghost text-xs ${mode === "tpsafe" ? "bg-slate-800 text-spice-300" : ""}`}
            onClick={() => setMode("tpsafe")}
          >
            safe snap
          </button>
        </div>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <PlayerPicker />
        <div>
          <div className="flex items-center justify-between mb-1.5 gap-2 flex-wrap">
            <span className="label !mb-0">Destination</span>
            <div className="flex items-center gap-2">
              {target.pos && !override.dirty && (
                <span className="text-xs text-slate-500">
                  from <span className="font-mono text-slate-400">{target.pos.source}</span> @ {target.pos.map}
                </span>
              )}
              {override.dirty && (
                <button type="button" className="btn-ghost text-xs" onClick={() => setOverride({ x: 0, y: 0, z: 0, dirty: false })}>
                  reset to target
                </button>
              )}
              <button type="button" className="btn-ghost text-xs" onClick={lookupPos} disabled={target.posLoading}>
                {target.posLoading ? "…" : `lookup ${target.playerId}`}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <input type="number" placeholder="X" value={x} onChange={(e) => setXyz("x", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Y" value={y} onChange={(e) => setXyz("y", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Z" value={z} onChange={(e) => setXyz("z", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
          </div>
        </div>
        <div>
          <label className="label" htmlFor="tp-yaw">Yaw (optional)</label>
          <input id="tp-yaw" type="number" value={yaw} onChange={(e) => setYaw(e.target.value === "" ? "" : parseFloat(e.target.value))} className="input-field" placeholder="(keep current)" />
        </div>
        <button type="submit" className="btn-primary">
          {mode === "teleport" ? "Teleport (exact)" : "Teleport (safe snap)"}
        </button>
      </form>
    </div>
  );
}

// ---- Maintenance (shutdown + xp + water) -------------------------------

export function MaintenanceTab({ setConsoleEntries }: TabProps) {
  const [shutType, setShutType] = useState("Restart");
  const [shutLead, setShutLead] = useState(600);
  const [shutFreq, setShutFreq] = useState(60);

  async function submitShutdown(e: React.FormEvent, cancel = false) {
    e.preventDefault();
    if (cancel) {
      await runAndLog(setConsoleEntries, "shutdown", { type: "cancel" }, "shutdown cancel");
    } else {
      await runAndLog(setConsoleEntries, "shutdown", { type: shutType, lead_secs: shutLead, freq_secs: shutFreq }, `shutdown ${shutType} in ${shutLead}s`);
    }
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Scheduled shutdown</h2>
        <span className="text-xs text-slate-500">sysadmin · affects all players</span>
      </header>
      <form onSubmit={(e) => submitShutdown(e)} className="p-4 space-y-4">
        <p className="text-xs text-slate-400">
          Broadcasts a countdown to every Sietch, then triggers a server-wide shutdown of the chosen type.
          Use <span className="font-mono text-slate-300">Cancel pending</span> to abort an in-flight countdown.
        </p>
        <div>
          <label className="label" htmlFor="shut-type">Type</label>
          <select id="shut-type" value={shutType} onChange={(e) => setShutType(e.target.value)} className="input-field">
            <option>Restart</option>
            <option>Maintenance</option>
            <option>Update</option>
          </select>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="shut-lead">Lead time (s)</label>
            <input id="shut-lead" type="number" min={30} value={shutLead} onChange={(e) => setShutLead(parseInt(e.target.value) || 60)} className="input-field" />
          </div>
          <div>
            <label className="label" htmlFor="shut-freq">Re-broadcast every (s)</label>
            <input id="shut-freq" type="number" min={5} value={shutFreq} onChange={(e) => setShutFreq(parseInt(e.target.value) || 60)} className="input-field" />
          </div>
        </div>
        <div className="flex gap-2">
          <button type="submit" className="btn-primary">Schedule</button>
          <button type="button" className="btn-ghost border border-slate-700" onClick={(e) => submitShutdown(e, true)}>Cancel pending</button>
        </div>
      </form>
    </div>
  );
}

// ---- Players (kick + clean + reset) -----------------------------------

export function PlayersTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [confirm, setConfirm] = useState<{ sub: string; label: string; warn: string } | null>(null);
  const [waterAmount, setWaterAmount] = useState(1_000_000);
  const [solariAmount, setSolariAmount] = useState(1000);
  // Landsraad standing — granted by giving House-specific reputation
  // items (one item = +1 standing point with that house). Funcom
  // doesn't expose a direct "AdjustHouseStanding" ServerCommand, so we
  // route through AddItemToInventory the same way Solari does.
  type HouseReputationId =
    | "AtreidesReputation"
    | "HarkonnenReputation"
    | "SmugglersReputation";
  const HOUSES: Array<{ id: HouseReputationId; label: string; emoji: string }> = [
    { id: "AtreidesReputation",  label: "House Atreides",  emoji: "🦅" },
    { id: "HarkonnenReputation", label: "House Harkonnen", emoji: "⚔️" },
    { id: "SmugglersReputation", label: "Smugglers",        emoji: "🥷" },
  ];
  const [landsraadHouse, setLandsraadHouse] = useState<HouseReputationId>("AtreidesReputation");
  const [landsraadAmount, setLandsraadAmount] = useState(100);

  function attempt(sub: string, label: string, warn: string) {
    setConfirm({ sub, label, warn });
  }

  async function execute() {
    if (!confirm) return;
    const { sub, label } = confirm;
    setConfirm(null);
    await runAndLog(setConsoleEntries, sub, { player_id: target.playerId }, `${sub} ${target.playerId} — ${label}`);
  }

  async function submitWater(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(
      setConsoleEntries,
      "water",
      { player_id: target.playerId, amount: waterAmount },
      `water ${target.playerId} ${waterAmount}`,
    );
  }

  async function submitSolari(e: React.FormEvent) {
    e.preventDefault();
    const amount = Math.max(1, solariAmount);
    await runAndLog(
      setConsoleEntries,
      "give",
      { player_id: target.playerId, item: "SolarisCoin", qty: amount },
      `💰 Solari ${target.playerId} +${amount.toLocaleString()}`,
    );
  }

  async function submitLandsraad(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    const amount = Math.max(1, landsraadAmount);
    const house = HOUSES.find((h) => h.id === landsraadHouse);
    const label = house ? `${house.emoji} ${house.label}` : landsraadHouse;
    await runAndLog(
      setConsoleEntries,
      "give",
      { player_id: target.playerId, item: landsraadHouse, qty: amount },
      `🏛️ Landsraad ${label} ${target.playerId} +${amount.toLocaleString()}`,
    );
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Target player</h2>
        </header>
        <div className="p-4">
          <PlayerPicker allowStar />
        </div>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Give Solari</h2>
          <span className="text-xs text-slate-500">currency · non-destructive</span>
        </header>
        <form onSubmit={submitSolari} className="p-4 space-y-3">
          <p className="text-xs text-slate-400">
            Grants <span className="font-mono text-slate-300">SolarisCoin</span> × N via <span className="font-mono text-slate-300">AddItemToInventory</span> —
            Funcom doesn't expose a direct wallet-credit command, so we drop coin items into the player's inventory. Each coin counts as 1 Solari toward their wallet.
          </p>
          <div>
            <label className="label" htmlFor="pl-solari">Amount</label>
            <div className="flex items-center gap-2 flex-wrap">
              <input
                id="pl-solari"
                type="number"
                min={1}
                value={solariAmount}
                onChange={(e) => setSolariAmount(parseInt(e.target.value) || 0)}
                className="input-field w-32 font-mono"
              />
              <div className="flex gap-1">
                {[100, 1_000, 10_000, 100_000, 1_000_000].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setSolariAmount(n)}
                    className={
                      "btn-ghost text-xs border border-slate-700 " +
                      (solariAmount === n ? "bg-slate-800 text-spice-300" : "")
                    }
                  >
                    {n >= 1_000_000 ? `${n / 1_000_000}M` : n >= 1_000 ? `${n / 1_000}k` : n}
                  </button>
                ))}
              </div>
              <button type="submit" className="btn-primary ml-auto">Give 💰</button>
            </div>
          </div>
        </form>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Give Landsraad standing</h2>
          <span className="text-xs text-slate-500">House reputation · non-destructive</span>
        </header>
        <form onSubmit={submitLandsraad} className="p-4 space-y-3">
          <p className="text-xs text-slate-400">
            Grants one of the House Reputation items via <span className="font-mono text-slate-300">AddItemToInventory</span>.
            Each item = +1 standing point with that House (same item-based mechanism as Solari).
            Items: <span className="font-mono text-slate-300">AtreidesReputation</span>, <span className="font-mono text-slate-300">HarkonnenReputation</span>, <span className="font-mono text-slate-300">SmugglersReputation</span>.
          </p>
          <div>
            <label className="label">House</label>
            <div className="flex gap-2 flex-wrap">
              {HOUSES.map((h) => (
                <button
                  key={h.id}
                  type="button"
                  onClick={() => setLandsraadHouse(h.id)}
                  className={
                    "btn-ghost text-sm border border-slate-700 " +
                    (landsraadHouse === h.id ? "bg-slate-800 text-spice-300" : "")
                  }
                >
                  {h.emoji} {h.label}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="label" htmlFor="pl-landsraad">Amount</label>
            <div className="flex items-center gap-2 flex-wrap">
              <input
                id="pl-landsraad"
                type="number"
                min={1}
                value={landsraadAmount}
                onChange={(e) => setLandsraadAmount(parseInt(e.target.value) || 0)}
                className="input-field w-32 font-mono"
              />
              <div className="flex gap-1">
                {[10, 100, 1_000, 10_000, 100_000].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setLandsraadAmount(n)}
                    className={
                      "btn-ghost text-xs border border-slate-700 " +
                      (landsraadAmount === n ? "bg-slate-800 text-spice-300" : "")
                    }
                  >
                    {n >= 1_000 ? `${n / 1_000}k` : n}
                  </button>
                ))}
              </div>
              <button type="submit" className="btn-primary ml-auto">Give 🏛️</button>
            </div>
          </div>
        </form>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Refill water containers</h2>
          <span className="text-xs text-slate-500">non-destructive</span>
        </header>
        <form onSubmit={submitWater} className="p-4 space-y-3">
          <p className="text-xs text-slate-400">
            Tops up every fillable water container (jerrycans, stills, Literjons, etc.) the player carries.
            <span className="text-slate-500"> Doesn't directly hydrate the character — for that, give them a Cup of Water via the Kits tab.</span>
          </p>
          <div>
            <label className="label" htmlFor="pl-water">Water amount</label>
            <div className="flex items-center gap-2">
              <input
                id="pl-water"
                type="number"
                min={1}
                value={waterAmount}
                onChange={(e) => setWaterAmount(parseInt(e.target.value) || 0)}
                className="input-field w-40 font-mono"
              />
              <button type="submit" className="btn-primary">Refill</button>
            </div>
          </div>
        </form>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Moderation</h2>
          <span className="text-xs text-slate-500">destructive — confirm dialog gates these</span>
        </header>
        <div className="p-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <button
              className="btn-ghost border border-slate-700"
              onClick={() => attempt("kick", "Kick", `Disconnect ${target.playerId}. They can reconnect immediately.`)}
            >
              Kick
            </button>
            <button
              className="btn-danger"
              onClick={() => attempt("clean", "Clean inventory", `Wipes ${target.playerId}'s entire inventory. Unrecoverable.`)}
            >
              Clean inventory
            </button>
            <button
              className="btn-danger col-span-2"
              onClick={() => attempt("reset", "Reset progression", `Wipes ${target.playerId}'s XP, skill levels, and unspent points. Unrecoverable.`)}
            >
              Reset progression
            </button>
          </div>
        </div>
      </div>

      <Confirm
        open={confirm !== null}
        title={confirm?.label || ""}
        message={confirm?.warn || ""}
        confirmLabel={confirm?.label || ""}
        onConfirm={execute}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

// ---- Kits -------------------------------------------------------------
//
// One-click bundled item grants. Funcom's seabass handler has no direct
// "heal player" / "set hydration" command (verified exhaustively, see
// the protocol wiki note), so these compose AddItemToInventory calls
// to achieve the same end result via consumables.

// Build a Kit object from an ArmorSet returned by /api/lookup/armor-sets.
// The Kit interface only carries name+emoji+blurb so we encode the
// unique flag + tier in the blurb prefix; the renderer fishes them
// back via the original ArmorSet object stored on a parallel map.
function kitFromArmorSet(set: ArmorSet): Kit {
  const cls = armorSetClass(set);
  const tier = armorSetTier(set);
  const unique = isUniqueArmor(set);
  const tierBit = tier ? `${tier} · ` : "";
  return {
    id: `armor-${set.base}`,
    name: armorSetLabel(set),
    emoji: cls.icon,
    blurb: `${unique ? "Unique · " : ""}${tierBit}${cls.tag} · ${set.pieces.length} pieces`,
    group: "armor",
    lines: set.pieces.map((p) => ({ id: p.id, name: p.name, qty: 1 })),
  };
}

export function KitsTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [custom, setCustom] = useState<CustomKit[]>(loadCustomKits);
  const [armorSets, setArmorSets] = useState<ArmorSet[]>([]);
  const [draftName, setDraftName] = useState("");
  const [draftItemId, setDraftItemId] = useState("");
  const [draftQty, setDraftQty] = useState(1);
  const [draftLines, setDraftLines] = useState<KitLine[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [wiki, setWiki] = useState<Record<string, WikiEntry | null>>({});

  // Modal-state for per-line customize. When set, a full-screen overlay
  // opens with the kit's lines as a checklist + qty overrides.
  const [editingKit, setEditingKit] = useState<Kit | null>(null);
  const [overrides, setOverrides] = useState<Record<string, { include: boolean; qty: number }>>({});

  // Collapsed-state per group + custom-kits section. Persisted so the
  // operator's accordion preferences survive page reloads.
  const COLLAPSED_KEY = "dune-admin-kit-collapsed-groups";
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(COLLAPSED_KEY);
      return raw ? new Set<string>(JSON.parse(raw)) : new Set<string>();
    } catch {
      return new Set<string>();
    }
  });
  function toggleCollapsed(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(Array.from(next)));
      return next;
    });
  }

  // Armor kits are generated from items.json server-side — one kit per
  // 3+ piece wearable set (57 sets in catalog) — so they stay fresh
  // when Funcom ships a new armor without us touching the SPA source.
  useEffect(() => {
    fetchArmorSets().then((res) => {
      if (res.ok) setArmorSets((res.body as { sets: ArmorSet[] }).sets);
    });
  }, []);

  const armorKits: Kit[] = useMemo(() => armorSets.map(kitFromArmorSet), [armorSets]);
  // Side-table so the Armor renderer can fish back the original
  // ArmorSet (for tier/unique badging) without re-parsing the kit id.
  const armorSetByKitId: Map<string, ArmorSet> = useMemo(() => {
    const m = new Map<string, ArmorSet>();
    armorSets.forEach((s) => m.set(`armor-${s.base}`, s));
    return m;
  }, [armorSets]);
  const builtInKits: Kit[] = useMemo(() => [...BUILT_IN_KITS, ...armorKits], [armorKits]);

  // Batch-fetch wiki entries for every line across built-in + custom +
  // dynamic-armor kits. Re-runs when armor data lands; cached after that.
  useEffect(() => {
    const allIds = new Set<string>();
    for (const k of builtInKits) k.lines.forEach((l) => allIds.add(l.id));
    for (const k of custom) k.lines.forEach((l) => allIds.add(l.id));
    if (allIds.size === 0) return;
    lookupItemIdsBatch(Array.from(allIds)).then((map) =>
      setWiki((prev) => ({ ...prev, ...map })),
    );
  }, [armorKits.length, custom.length]);

  // Close modal on Esc.
  useEffect(() => {
    if (!editingKit) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setEditingKit(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editingKit]);

  // When user opens a kit for customization, seed per-line overrides —
  // every line included, qty defaults to the kit's authored qty.
  function openCustomize(kit: Kit | null) {
    if (!kit) {
      setEditingKit(null);
      setOverrides({});
      return;
    }
    const next: Record<string, { include: boolean; qty: number }> = {};
    kit.lines.forEach((l, i) => {
      next[`${kit.id}#${i}`] = { include: true, qty: l.qty };
    });
    setOverrides(next);
    setEditingKit(kit);
  }

  async function runKit(kit: Kit, useOverrides = false) {
    setRunning(kit.id);
    try {
      const lines = useOverrides
        ? kit.lines
            .map((l, i) => {
              const o = overrides[`${kit.id}#${i}`];
              return o && o.include ? { ...l, qty: Math.max(1, o.qty) } : null;
            })
            .filter((x): x is KitLine => x !== null)
        : kit.lines;
      for (const line of lines) {
        const body: Record<string, unknown> = {
          player_id: target.playerId,
          item: line.id,
          qty: line.qty,
        };
        if (line.durability !== undefined) body["durability"] = line.durability;
        // eslint-disable-next-line no-await-in-loop
        await runAndLog(
          setConsoleEntries,
          "give",
          body,
          `${kit.emoji} ${kit.name}: ${line.name} ×${line.qty}`,
        );
      }
    } finally {
      setRunning(null);
    }
  }

  function toggleAll(kit: Kit, include: boolean) {
    setOverrides((prev) => {
      const next = { ...prev };
      kit.lines.forEach((_, i) => {
        const k = `${kit.id}#${i}`;
        if (next[k]) next[k] = { ...next[k], include };
      });
      return next;
    });
  }

  function setLineQty(kit: Kit, idx: number, qty: number) {
    const k = `${kit.id}#${idx}`;
    setOverrides((prev) => ({ ...prev, [k]: { ...(prev[k] || { include: true, qty: 1 }), qty } }));
  }

  function setLineInclude(kit: Kit, idx: number, include: boolean) {
    const k = `${kit.id}#${idx}`;
    setOverrides((prev) => ({ ...prev, [k]: { ...(prev[k] || { include, qty: 1 }), include } }));
  }

  function addDraftLine() {
    // Funcom item FNames match [A-Za-z0-9_]+; reject anything that
    // doesn't so a paste of "; rm -rf /" can't enter the draft list.
    // The backend already JSON-escapes the value, but client-side
    // rejection gives the operator immediate feedback instead of a
    // silent backend error.
    const id = sanitizeIdent(draftItemId);
    if (!id) return;
    const qty = clampNum(String(draftQty), 1, 9999, 1);
    setDraftLines((prev) => [...prev, { id, name: id, qty }]);
    setDraftItemId("");
    setDraftQty(1);
  }

  function saveDraft() {
    const name = draftName.trim();
    if (!name || draftLines.length === 0) return;
    const k: CustomKit = {
      id: `c-${Math.random().toString(36).slice(2, 9)}`,
      name,
      emoji: "📦",
      blurb: `${draftLines.length} item${draftLines.length === 1 ? "" : "s"} (custom)`,
      group: "loot",
      lines: draftLines,
      custom: true,
    };
    const next = [...custom, k];
    setCustom(next);
    saveCustomKits(next);
    setDraftName("");
    setDraftLines([]);
  }

  function deleteCustom(id: string) {
    const next = custom.filter((k) => k.id !== id);
    setCustom(next);
    saveCustomKits(next);
  }

  return (
    <div className="space-y-6">
      <div className="card max-w-3xl">
        <header className="card-header">
          <h2 className="font-semibold">Target player</h2>
        </header>
        <div className="p-4">
          <PlayerPicker />
          <p className="text-xs text-slate-500 mt-2">
            Each kit fires multiple <span className="font-mono text-slate-400">/admin/give</span> calls back-to-back at the player above.
            Funcom doesn't ship a direct heal / set-hydration command — these bundles use the in-game consumables instead.
          </p>
        </div>
      </div>

      {KIT_GROUPS.map((g) => {
        const groupKits = builtInKits.filter((k) => k.group === g.id);
        if (groupKits.length === 0) return null;
        const isCollapsed = collapsed.has(g.id);

        // Armor needs sub-sections: Unique vs Standard, with different
        // colour treatments + tier badges. Everything else uses the
        // plain grid.
        const isArmor = g.id === "armor";
        const armorSplit = isArmor
          ? {
              unique: groupKits.filter((k) => {
                const s = armorSetByKitId.get(k.id);
                return s && isUniqueArmor(s);
              }),
              standard: groupKits.filter((k) => {
                const s = armorSetByKitId.get(k.id);
                return s && !isUniqueArmor(s);
              }),
            }
          : null;

        const renderKitCard = (kit: Kit, opts: { unique?: boolean } = {}) => {
          const s = armorSetByKitId.get(kit.id);
          const tier = s ? armorSetTier(s) : "";
          const cardBorder = opts.unique
            ? "border-amber-700/60 hover:border-amber-400/70 bg-amber-950/10"
            : "border-slate-800 hover:border-spice-500/50 hover:bg-slate-800/50";
          return (
            <div key={kit.id} className={`rounded-lg border transition relative ${cardBorder}`}>
              {tier && (
                <span
                  className={
                    "absolute top-2 right-2 text-[10px] font-mono px-1.5 py-0.5 rounded border " +
                    (opts.unique
                      ? "bg-amber-900/70 text-amber-200 border-amber-700"
                      : "bg-slate-900 text-spice-300 border-slate-700")
                  }
                >
                  {tier}
                </span>
              )}
              <div className="p-3">
                <div className="flex items-center gap-2 mb-1 pr-10">
                  <span className="text-xl" aria-hidden>{kit.emoji}</span>
                  <span className={`font-semibold truncate ${opts.unique ? "text-amber-200" : "text-spice-300"}`}>{kit.name}</span>
                  {running === kit.id && <span className="text-xs text-slate-500 ml-auto">running…</span>}
                </div>
                <div className="text-xs text-slate-400 mb-2">{kit.blurb}</div>
                <ul className="text-xs text-slate-500 space-y-1">
                  {kit.lines.slice(0, 5).map((l, i) => {
                    const w = wiki[l.id];
                    const img = awakeningImageUrl(w?.image);
                    return (
                      <li key={i} className="flex items-center gap-1.5 truncate">
                        {img ? (
                          <img src={img} alt="" width={16} height={16} loading="lazy" className="w-4 h-4 shrink-0 rounded-sm bg-slate-950 object-contain border border-slate-800" />
                        ) : (
                          <span className="w-4 h-4 shrink-0" />
                        )}
                        <span className="text-slate-400">×{l.qty}</span>
                        <span className="truncate">{w?.name || l.name}</span>
                      </li>
                    );
                  })}
                  {kit.lines.length > 5 && (
                    <li className="text-slate-600 italic">…+{kit.lines.length - 5} more</li>
                  )}
                </ul>
              </div>
              <div className="px-3 pb-3 flex gap-2">
                <button
                  type="button"
                  onClick={() => runKit(kit, false)}
                  disabled={running !== null}
                  className="btn-primary text-xs flex-1"
                >
                  Grant all
                </button>
                <button
                  type="button"
                  onClick={() => openCustomize(kit)}
                  className="btn-ghost border border-slate-700 text-xs"
                  title="Open customize editor"
                >
                  ⚙ customize
                </button>
              </div>
            </div>
          );
        };

        return (
          <div key={g.id} className="card">
            <button
              type="button"
              onClick={() => toggleCollapsed(g.id)}
              className="card-header w-full hover:bg-slate-800/40 transition cursor-pointer text-left"
              aria-expanded={!isCollapsed}
            >
              <h2 className="font-semibold flex items-center gap-2">
                <span className="text-slate-500 transition-transform inline-block w-3" style={{ transform: isCollapsed ? "rotate(-90deg)" : "rotate(0deg)" }}>▾</span>
                <span aria-hidden>{g.emoji}</span> {g.label}
              </h2>
              <span className="text-xs text-slate-500">
                {isArmor && armorSplit
                  ? `${armorSplit.unique.length} unique · ${armorSplit.standard.length} standard`
                  : `${groupKits.length} bundles`}
              </span>
            </button>
            {!isCollapsed && (
              isArmor && armorSplit ? (
                <div className="p-4 space-y-5">
                  {armorSplit.unique.length > 0 && (
                    <div>
                      <h3 className="text-xs uppercase tracking-wider text-amber-300/80 mb-2 flex items-center gap-2">
                        <span className="w-2 h-2 rounded-full bg-amber-400" aria-hidden />
                        Unique armor
                        <span className="text-slate-500 normal-case">({armorSplit.unique.length})</span>
                      </h3>
                      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {armorSplit.unique.map((kit) => renderKitCard(kit, { unique: true }))}
                      </div>
                    </div>
                  )}
                  {armorSplit.standard.length > 0 && (
                    <div>
                      <h3 className="text-xs uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-2">
                        <span className="w-2 h-2 rounded-full bg-slate-500" aria-hidden />
                        Standard armor
                        <span className="text-slate-500 normal-case">({armorSplit.standard.length})</span>
                      </h3>
                      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                        {armorSplit.standard.map((kit) => renderKitCard(kit))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
                  {groupKits.map((kit) => renderKitCard(kit))}
                </div>
              )
            )}
          </div>
        );
      })}

      {/* ── Modal: per-line customize editor ─────────────────────── */}
      {editingKit && (
        <div
          className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4 animate-fade-in"
          onClick={() => openCustomize(null)}
        >
          <div
            className="card max-w-2xl w-full max-h-[90vh] flex flex-col border-spice-700"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="card-header">
              <h2 className="font-semibold flex items-center gap-2 min-w-0">
                <span aria-hidden>{editingKit.emoji}</span>
                <span className="truncate">{editingKit.name}</span>
              </h2>
              <div className="flex items-center gap-2 shrink-0">
                <button type="button" className="btn-ghost text-xs border border-slate-700" onClick={() => toggleAll(editingKit, true)}>
                  check all
                </button>
                <button type="button" className="btn-ghost text-xs border border-slate-700" onClick={() => toggleAll(editingKit, false)}>
                  uncheck all
                </button>
                <button
                  type="button"
                  className="btn-ghost text-xs text-slate-400 hover:text-slate-100"
                  onClick={() => openCustomize(null)}
                  title="Close (Esc)"
                >
                  ✕
                </button>
              </div>
            </header>
            <div className="p-4 overflow-y-auto flex-1">
              <p className="text-xs text-slate-400 mb-3">
                Uncheck a row to skip it. Adjust the qty to override the bundled default.
                Only checked rows fire when you hit <span className="text-spice-300">Grant selected</span>.
              </p>
              <ul className="space-y-1.5">
                {editingKit.lines.map((l, i) => {
                  const w = wiki[l.id];
                  const img = awakeningImageUrl(w?.image);
                  const o = overrides[`${editingKit.id}#${i}`] || { include: true, qty: l.qty };
                  return (
                    <li
                      key={i}
                      className={
                        "flex items-center gap-3 p-2 rounded border transition " +
                        (o.include
                          ? "border-slate-700 bg-slate-900/50"
                          : "border-slate-800 bg-slate-950/50 opacity-50")
                      }
                    >
                      <input
                        type="checkbox"
                        checked={o.include}
                        onChange={(e) => setLineInclude(editingKit, i, e.target.checked)}
                        className="accent-spice-500 shrink-0"
                      />
                      <div className="w-8 h-8 shrink-0 flex items-center justify-center">
                        {img ? (
                          <img src={img} alt="" width={32} height={32} loading="lazy" className="w-8 h-8 object-contain" />
                        ) : (
                          <span className="text-slate-600">·</span>
                        )}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="text-slate-200 text-sm truncate">{w?.name || l.name}</div>
                        <div className="text-[10px] text-slate-500 font-mono truncate">{l.id}</div>
                      </div>
                      <input
                        type="number"
                        min={1}
                        value={o.qty}
                        onChange={(e) => setLineQty(editingKit, i, parseInt(e.target.value) || 1)}
                        disabled={!o.include}
                        className="input-field w-20 text-xs font-mono shrink-0"
                      />
                    </li>
                  );
                })}
              </ul>
            </div>
            <div className="px-4 py-3 border-t border-slate-800 flex items-center gap-2">
              <span className="text-xs text-slate-500 mr-auto">
                {Object.values(overrides).filter((o) => o.include).length} of {editingKit.lines.length} selected · target <span className="font-mono text-slate-300">{target.playerId}</span>
              </span>
              <button
                type="button"
                onClick={() => openCustomize(null)}
                className="btn-ghost border border-slate-700"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={async () => { await runKit(editingKit, true); openCustomize(null); }}
                disabled={running !== null || Object.values(overrides).every((o) => !o.include)}
                className="btn-primary"
              >
                {running === editingKit.id ? "granting…" : "Grant selected"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <button
          type="button"
          onClick={() => toggleCollapsed("custom")}
          className="card-header w-full hover:bg-slate-800/40 transition cursor-pointer text-left"
          aria-expanded={!collapsed.has("custom")}
        >
          <h2 className="font-semibold flex items-center gap-2">
            <span className="text-slate-500 transition-transform inline-block w-3" style={{ transform: collapsed.has("custom") ? "rotate(-90deg)" : "rotate(0deg)" }}>▾</span>
            Your custom kits
          </h2>
          <span className="text-xs text-slate-500">{custom.length} saved locally</span>
        </button>
        {!collapsed.has("custom") && (
        <div className="p-4 space-y-4">
          {custom.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {custom.map((k) => (
                <div key={k.id} className="p-3 rounded-lg border border-slate-800 group">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xl" aria-hidden>{k.emoji}</span>
                    <span className="font-semibold text-spice-300">{k.name}</span>
                    <button
                      type="button"
                      className="ml-auto text-slate-500 hover:text-red-400 text-xs opacity-0 group-hover:opacity-100 transition"
                      onClick={() => deleteCustom(k.id)}
                      title="delete custom kit"
                    >
                      ×
                    </button>
                  </div>
                  <ul className="text-xs text-slate-500 space-y-1 mb-2">
                    {k.lines.map((l, i) => {
                      const w = wiki[l.id];
                      const img = awakeningImageUrl(w?.image);
                      return (
                        <li key={i} className="flex items-center gap-1.5 truncate">
                          {img ? (
                            <img src={img} alt="" width={16} height={16} loading="lazy" className="w-4 h-4 shrink-0 rounded-sm bg-slate-950 object-contain border border-slate-800" />
                          ) : (
                            <span className="w-4 h-4 shrink-0" />
                          )}
                          <span className="text-slate-400">×{l.qty}</span>
                          <span className="truncate font-mono">{l.id}</span>
                        </li>
                      );
                    })}
                  </ul>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => runKit(k)}
                      disabled={running !== null}
                      className="btn-primary flex-1 text-xs"
                    >
                      Grant to {target.playerId}
                    </button>
                    <button
                      type="button"
                      onClick={() => openCustomize(k)}
                      className="btn-ghost border border-slate-700 text-xs"
                      title="Open customize editor"
                    >
                      ⚙
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="border border-dashed border-slate-700 rounded-lg p-4 space-y-3">
            <div className="text-sm font-semibold text-slate-300">Build a custom kit</div>
            <div className="grid grid-cols-3 gap-2">
              <input
                type="text"
                value={draftItemId}
                onChange={(e) => setDraftItemId(e.target.value)}
                placeholder="ItemFName (e.g. MelangeSpice)"
                className="input-field font-mono col-span-2 text-xs"
              />
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  value={draftQty}
                  onChange={(e) => setDraftQty(parseInt(e.target.value) || 1)}
                  className="input-field font-mono text-xs flex-1"
                />
                <button
                  type="button"
                  onClick={addDraftLine}
                  disabled={!draftItemId.trim()}
                  className="btn-ghost border border-slate-700 text-xs"
                >
                  + add
                </button>
              </div>
            </div>
            {draftLines.length > 0 && (
              <ul className="text-xs text-slate-400 space-y-1">
                {draftLines.map((l, i) => (
                  <li key={i} className="flex items-center gap-2 font-mono">
                    <span className="text-slate-500">×{l.qty}</span>
                    <span className="text-spice-300">{l.id}</span>
                    <button
                      type="button"
                      className="ml-auto text-slate-600 hover:text-red-400"
                      onClick={() => setDraftLines((prev) => prev.filter((_, idx) => idx !== i))}
                    >
                      remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                placeholder="kit name (e.g. ‘event drop’)"
                className="input-field flex-1 text-xs"
              />
              <button
                type="button"
                onClick={saveDraft}
                disabled={!draftName.trim() || draftLines.length === 0}
                className="btn-ghost border border-slate-700 text-xs"
              >
                save kit
              </button>
            </div>
            <p className="text-xs text-slate-500">
              Use the <span className="font-mono">Items</span> tab to search 2558 item FNames if you don't have one handy.
            </p>
          </div>
        </div>
        )}
      </div>
    </div>
  );
}

// ---- History ----------------------------------------------------------

export function HistoryTab() {
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    const res = await fetchHistory(100);
    if (res.ok) setHistory(res.body as HistoryResponse);
    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  return (
    <div className="card">
      <header className="card-header">
        <h2 className="font-semibold">Server-side history</h2>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">
            {history ? `${history.entries.length} of ${history.total}` : "-"}
          </span>
          <button className="btn-ghost text-xs" onClick={load} disabled={loading}>
            {loading ? "…" : "refresh"}
          </button>
        </div>
      </header>
      <div className="max-h-[700px] overflow-y-auto font-mono text-xs">
        {(history?.entries || []).slice().reverse().map((entry, idx) => (
          <div key={`${entry.ts}-${idx}`} className="border-b border-slate-800 px-4 py-2">
            <div className="flex items-center gap-2 text-slate-400 text-[10px]">
              <span>{new Date(entry.ts * 1000).toLocaleString()}</span>
              <span className={entry.ok ? "pill-ok" : "pill-err"}>{entry.ok ? "ok" : "fail"}</span>
              <span className="text-slate-300">{entry.argv.join(" ")}</span>
            </div>
            {(entry.stdout || entry.stderr) && (
              <pre className="whitespace-pre-wrap text-slate-300 mt-1 text-xs">
                {(entry.stdout || "") + (entry.stderr ? "\n" + entry.stderr : "")}
              </pre>
            )}
          </div>
        ))}
        {!loading && history?.entries.length === 0 && (
          <div className="p-8 text-center text-slate-500">No history yet</div>
        )}
      </div>
    </div>
  );
}

// ---- Status (Phase 4: live server grid) -------------------------------

export function StatusTab({ setConsoleEntries }: TabProps) {
  const [grid, setGrid] = useState<StatusGrid | null>(null);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    const res = await fetchStatus();
    setLoading(false);
    if (res.ok) setGrid(res.body as StatusGrid);
    else pushToConsole(setConsoleEntries, "GET /api/status", JSON.stringify(res.body), false);
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="card">
      <header className="card-header">
        <div className="flex items-center gap-2">
          <h2 className="font-semibold">Server status</h2>
          {grid && (
            <span className="text-xs text-slate-500">
              {grid.totalServers} servers · {grid.totalPlayers} online
            </span>
          )}
        </div>
        <button className="btn-ghost text-xs" onClick={refresh} disabled={loading}>
          {loading ? "…" : "refresh"}
        </button>
      </header>
      <div className="p-4 space-y-3">
        {grid?.sources && !grid.sources.mockK8s && (
          <p className="text-xs text-amber-300">mock-k8s status unreachable — showing player counts only.</p>
        )}
        {!grid && <p className="text-sm text-slate-500 italic">loading…</p>}
        {grid && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-slate-500 border-b border-slate-800">
                <tr>
                  <th className="py-1 pr-4">Map / Sietch</th>
                  <th className="pr-4">Status</th>
                  <th className="pr-4">Replicas</th>
                  <th className="pr-4">Players</th>
                </tr>
              </thead>
              <tbody>
                {[...grid.maps]
                  .sort((a, b) =>
                    MAP_ROLE_META[mapRole(a.map)].order - MAP_ROLE_META[mapRole(b.map)].order ||
                    mapDisplayName(a.map).localeCompare(mapDisplayName(b.map)))
                  .map((m) => {
                    const meta = MAP_ROLE_META[mapRole(m.map)];
                    return (
                      <tr key={m.map} className="border-b border-slate-900">
                        <td className="py-1.5 pr-4">
                          <span className="flex items-center gap-2 flex-wrap">
                            <span aria-hidden>{meta.icon}</span>
                            <span className="text-slate-200">{mapDisplayName(m.map)}</span>
                            <span className={"px-1.5 rounded text-[10px] " + meta.chip} title={meta.blurb}>{meta.title}</span>
                            <span className="font-mono text-[10px] text-slate-600" title="raw engine map id">{m.map}</span>
                          </span>
                        </td>
                        <td className="pr-4"><span className={mapStatusPill(m.status)}>{m.status || "?"}</span></td>
                        <td className="pr-4 text-slate-400">{m.current ?? 0}/{m.desired ?? 0}</td>
                        <td className="pr-4">
                          {m.players > 0
                            ? <span className="text-emerald-300 font-semibold">{m.players}</span>
                            : <span className="text-slate-500">0</span>}
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        )}
        {grid && (
          <p className="text-xs text-slate-500">
            uptime {Math.floor(grid.uptimeSeconds / 60)}m · auto-refresh 15s
          </p>
        )}
      </div>
    </div>
  );
}

// ---- Inventory (Phase 1: view + per-item delete) ----------------------

// dune.inventories.inventory_type -> human container label. Values + meaning
// lifted from Icehunter/dune-admin (repairGearInventoryTypes {0,1,14,15,27,30}
// = "backpack, equipment, emote wheel, equipped weapons, action wheel, bank")
// and confirmed against live character data.
const INV_TYPE_LABELS: Record<string, string> = {
  "0": "Backpack",
  "1": "Equipped (armor & modules)",
  "15": "Hotbar (held tools / weapons)",
  "27": "Action wheel",
  "14": "Emote wheel",
  "30": "Bank",
};
const INV_TYPE_ORDER = ["0", "15", "1", "27", "14", "30"];
function invTypeLabel(t: string): string {
  return INV_TYPE_LABELS[t] || `Other (type ${t})`;
}

export function InventoryTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [table, setTable] = useState<PlayerTable | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ id: string; name: string } | null>(null);

  async function load() {
    if (!target.playerId) return;
    setLoading(true);
    setError(null);
    const res = await fetchInventory(target.playerId);
    setLoading(false);
    if (res.ok) {
      setTable(res.body as PlayerTable);
    } else {
      setTable(null);
      setError((res.body as { error?: string }).error || "inventory load failed");
    }
  }

  async function doDelete(id: string, name: string) {
    const res = await deleteItem(id);
    const ok = res.ok && (res.body as PublishResult).ok;
    pushToConsole(setConsoleEntries, `item-delete #${id} (${name})`, res.body as PublishResult, ok);
    setConfirm(null);
    if (ok) load();
  }

  const headers = table?.headers ?? [];
  const rows = table?.rows ?? [];
  const cell = (row: string[], h: string) => {
    const i = headers.indexOf(h);
    return i >= 0 && i < row.length ? row[i] : "";
  };

  // Group item stacks by their container (inventory_type).
  const groups = new Map<string, string[][]>();
  for (const r of rows) {
    const t = cell(r, "inv_type");
    const arr = groups.get(t) ?? [];
    arr.push(r);
    groups.set(t, arr);
  }
  const orderedTypes = [
    ...INV_TYPE_ORDER.filter((t) => groups.has(t)),
    ...[...groups.keys()].filter((t) => !INV_TYPE_ORDER.includes(t)).sort(),
  ];

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Player inventory</h2>
          <button className="btn-ghost text-xs" onClick={load} disabled={loading}>
            {loading ? "…" : "load"}
          </button>
        </header>
        <div className="p-4 space-y-3">
          <PlayerPicker />
          <p className="text-xs text-slate-500">
            Reads <span className="font-mono text-slate-300">dune.items</span> for the target character. Deleting a
            stack is <span className="text-red-300">permanent</span> and requires the player to be{" "}
            <span className="font-mono text-slate-300">offline</span> — the server rejects deletes on a connected
            character.
          </p>
          {error && <p className="text-sm text-red-400">{error}</p>}
          {table && table.available === false && (
            <p className="text-sm text-amber-400">unavailable: {table.detail || "inventory tables missing"}</p>
          )}
        </div>
      </div>

      {table && rows.length > 0 && (
        <div className="space-y-3">
          <div className="text-xs text-slate-500 font-mono">{rows.length} item stacks · {target.playerId}</div>
          {orderedTypes.map((t) => {
            const items = groups.get(t) ?? [];
            return (
              <div key={t} className="card">
                <header className="card-header">
                  <h3 className="font-semibold text-sm">{invTypeLabel(t)}</h3>
                  <span className="text-xs text-slate-500">{items.length}</span>
                </header>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="text-left text-slate-500 border-b border-slate-800">
                      <tr>
                        <th className="py-1 px-3">Item</th>
                        <th className="px-3">Qty</th>
                        <th className="px-3">Quality</th>
                        <th className="px-3">Durability</th>
                        <th className="px-3">Slot</th>
                        <th className="px-3"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((r) => {
                        const id = cell(r, "item_id");
                        const tmpl = cell(r, "template_id");
                        const name = cell(r, "name") || tmpl;
                        const dura = cell(r, "durability");
                        const maxd = cell(r, "max_durability");
                        return (
                          <tr key={id} className="border-b border-slate-900">
                            <td className="py-1.5 px-3">
                              <div className="text-slate-200">{name}</div>
                              <div className="text-[10px] text-slate-600 font-mono">{tmpl} · #{id}</div>
                            </td>
                            <td className="px-3 font-mono">{cell(r, "stack_size")}</td>
                            <td className="px-3">{cell(r, "quality")}</td>
                            <td className="px-3 font-mono text-slate-400">
                              {dura}
                              {maxd && maxd !== "N/A" ? ` / ${maxd}` : ""}
                            </td>
                            <td className="px-3 font-mono text-slate-500">{cell(r, "slot")}</td>
                            <td className="px-3 text-right">
                              <button
                                className="btn-ghost text-xs text-red-300 hover:text-red-200"
                                onClick={() => setConfirm({ id, name })}
                              >
                                delete
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {table && rows.length === 0 && !loading && (
        <p className="text-sm text-slate-500 italic">
          No item stacks (empty inventory, or the character has no offline-readable items).
        </p>
      )}

      <Confirm
        open={confirm !== null}
        title="Delete item stack?"
        message={
          confirm
            ? `Permanently delete "${confirm.name}" (item #${confirm.id}) from ${target.playerId}'s inventory. The player must be offline. Unrecoverable.`
            : ""
        }
        confirmLabel="Delete"
        onConfirm={() => confirm && doDelete(confirm.id, confirm.name)}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

// ---- Settings (Phase 5: server config) --------------------------------

export function SettingsTab({ setConsoleEntries }: TabProps) {
  const [resp, setResp] = useState<SettingsResponse | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  async function refresh() {
    setLoading(true);
    const res = await fetchSettings();
    setLoading(false);
    if (res.ok) {
      setResp(res.body as SettingsResponse);
      setEdits({});
    } else {
      pushToConsole(setConsoleEntries, "GET /api/settings", JSON.stringify(res.body), false);
    }
  }

  useEffect(() => { refresh(); }, []);

  const dirty = Object.keys(edits).length;

  async function apply() {
    if (!dirty) return;
    setSaving(true);
    const res = await saveSettings(edits);
    setSaving(false);
    const b = res.body as SettingsSaveResult;
    const ok = res.ok && b.ok;
    const lines = [`applied: ${(b.applied || []).join(", ") || "none"}`];
    if ((b.errors || []).length) lines.push("errors: " + b.errors.map((e) => `${e.id}: ${e.error}`).join("; "));
    if (b.restartRequired) lines.push("⚠ restart the server for changes to take effect");
    pushToConsole(setConsoleEntries, `settings apply (${dirty})`, lines.join("\n"), ok);
    refresh();
  }

  function renderInput(s: SettingItem) {
    const cur = edits[s.id] ?? (s.value ?? "");
    if (s.advanced) {
      // struct/array values are written verbatim — one full-width raw field.
      return (
        <input
          className="input-field text-xs font-mono w-full"
          value={cur}
          placeholder={s.default || "single-line value; multi-line arrays: edit UserGame.ini directly"}
          onChange={(e) => setEdits((p) => ({ ...p, [s.id]: e.target.value }))}
        />
      );
    }
    if (s.type === "bool" || s.type === "cvarbool") {
      return (
        <select className="input-field text-xs" value={cur} onChange={(e) => setEdits((p) => ({ ...p, [s.id]: e.target.value }))}>
          <option value="">(unset)</option>
          <option value="1">on</option>
          <option value="0">off</option>
        </select>
      );
    }
    if (s.type === "enum" && s.enum) {
      return (
        <select className="input-field text-xs" value={cur} onChange={(e) => setEdits((p) => ({ ...p, [s.id]: e.target.value }))}>
          <option value="">(unset)</option>
          {s.enum.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      );
    }
    return (
      <input
        className="input-field text-xs font-mono"
        value={cur}
        inputMode={s.type === "int" || s.type === "float" ? "decimal" : "text"}
        placeholder={s.default ?? ""}
        onChange={(e) => setEdits((p) => ({ ...p, [s.id]: e.target.value }))}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <div className="flex items-center gap-2">
            <h2 className="font-semibold">Server settings</h2>
            {resp && <span className="text-xs text-slate-500">{resp.count} settings</span>}
          </div>
          <div className="flex gap-2">
            <button className="btn-ghost text-xs" onClick={refresh} disabled={loading}>{loading ? "…" : "reload"}</button>
            <button className="btn-primary text-xs" onClick={apply} disabled={!dirty || saving}>
              {saving ? "saving…" : `apply${dirty ? ` (${dirty})` : ""}`}
            </button>
          </div>
        </header>
        <p className="p-4 text-xs text-slate-400">
          Edit a value and hit apply — it writes to the server INI and takes effect on the next{" "}
          <span className="font-mono text-slate-300">restart</span>. A{" "}
          <span className="pill-warn text-[10px]">candidate</span> tag means the mapping isn't game-verified yet;{" "}
          <span className="pill-warn text-[10px]">⚠ client-side</span> means clients clamp to their own default unless each
          player also edits their <span className="font-mono text-slate-300">Game.ini</span>;{" "}
          <span className="text-amber-400/80">advanced</span> values are written verbatim.
        </p>
      </div>
      {!resp && <p className="text-sm text-slate-500 italic">loading…</p>}
      {resp && Object.entries(resp.categories).sort(([a], [b]) => a.localeCompare(b)).map(([cat, items]) => (
        <div key={cat} className="card">
          <header className="card-header">
            <h3 className="font-semibold text-sm">{cat}</h3>
            <span className="text-xs text-slate-500">{items.length}</span>
          </header>
          <div className="divide-y divide-slate-900">
            {items.map((s) => (
              <div key={s.id} className="px-4 py-2">
                <div className="flex items-center gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm flex items-center gap-2 flex-wrap">
                      {s.label}
                      {!s.verified && <span className="pill-warn text-[10px]">candidate</span>}
                      {!s.isDefault && <span className="pill-ok text-[10px]">set</span>}
                      {s.clientGated && (
                        <span
                          className="pill-warn text-[10px]"
                          title="The server applies this, but each client clamps to its own default unless the player also edits %localappdata%\DuneSandbox\Saved\Config\WindowsClient\Game.ini"
                        >
                          ⚠ client-side
                        </span>
                      )}
                      {s.advanced && <span className="text-[10px] text-amber-400/80">advanced</span>}
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono truncate">
                      {s.section ? `${s.section.split(".").pop()} · ` : ""}{s.key} · {s.type}
                      {s.default !== null ? ` · default ${s.default || '""'}` : ""}
                    </div>
                  </div>
                  {!s.advanced && <div className="w-40 shrink-0">{renderInput(s)}</div>}
                </div>
                {s.advanced && <div className="mt-1.5">{renderInput(s)}</div>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---- Welcome kits (Phase 6) -------------------------------------------

export function WelcomeTab({ setConsoleEntries }: TabProps) {
  const [resp, setResp] = useState<WelcomeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setLoading(true);
    const res = await fetchWelcome();
    setLoading(false);
    if (res.ok) setResp(res.body as WelcomeResponse);
    else pushToConsole(setConsoleEntries, "GET /api/welcome", JSON.stringify(res.body), false);
  }

  useEffect(() => { refresh(); }, []);

  async function scan() {
    setBusy(true);
    const res = await welcomeScan();
    setBusy(false);
    const b = res.body as WelcomeScanResult;
    const msg = b.disabled
      ? "welcome kit is disabled — nothing granted"
      : b.error
        ? `error: ${b.error}`
        : `granted ${b.granted ?? 0} · failed ${b.failed ?? 0} · skipped ${b.skipped ?? 0} (online ${b.online ?? 0})`;
    pushToConsole(setConsoleEntries, "welcome scan", msg, res.ok && (b.ok ?? false));
    refresh();
  }

  async function retry() {
    setBusy(true);
    const res = await welcomeRetryFailed();
    setBusy(false);
    pushToConsole(setConsoleEntries, "welcome retry-failed", `cleared ${(res.body as { cleared: number }).cleared ?? 0} failed rows`, res.ok);
    refresh();
  }

  const cfg = resp?.config;

  return (
    <div className="space-y-4">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Welcome kits</h2>
          <div className="flex gap-2">
            <button className="btn-ghost text-xs" onClick={refresh} disabled={loading}>{loading ? "…" : "refresh"}</button>
            <button className="btn-ghost text-xs border border-slate-700" onClick={retry} disabled={busy}>retry failed</button>
            <button className="btn-primary text-xs" onClick={scan} disabled={busy}>{busy ? "…" : "run scan now"}</button>
          </div>
        </header>
        <div className="p-4 space-y-2 text-sm">
          {cfg ? (
            <div className="flex flex-wrap gap-4">
              <span>status: <span className={cfg.enabled ? "pill-ok" : "pill-warn"}>{cfg.enabled ? "enabled" : "disabled"}</span></span>
              <span className="text-slate-400">active: <span className="font-mono text-slate-200">{cfg.active_version || "—"}</span></span>
              <span className="text-slate-400">packages: <span className="font-mono">{cfg.packages.join(", ") || "—"}</span></span>
              <span className="text-slate-400">granted: <span className="text-emerald-300">{cfg.ledger.granted}</span></span>
              <span className="text-slate-400">failed: <span className={cfg.ledger.failed ? "text-red-300" : "text-slate-300"}>{cfg.ledger.failed}</span></span>
            </div>
          ) : <p className="text-slate-500 italic">loading…</p>}
          <p className="text-xs text-slate-500">
            Edit <span className="font-mono text-slate-400">data/admin/welcome-kit.json</span> to define items + enable. The scanner grants the active
            package once per player per version (bump the version to re-grant). In-panel kit editing is a future addition.
          </p>
        </div>
      </div>
      {resp && resp.grants.length > 0 && (
        <div className="card">
          <header className="card-header">
            <h3 className="font-semibold text-sm">Recent grants</h3>
            <span className="text-xs text-slate-500">{resp.grants.length}</span>
          </header>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-slate-500 border-b border-slate-800">
                <tr>
                  <th className="py-1 px-3">Player</th>
                  <th className="px-3">Version</th>
                  <th className="px-3">Status</th>
                  <th className="px-3">When</th>
                  <th className="px-3">Note</th>
                </tr>
              </thead>
              <tbody>
                {resp.grants.map((g, i) => (
                  <tr key={`${g.fls_id}-${g.package_version}-${i}`} className="border-b border-slate-900">
                    <td className="py-1.5 px-3 font-mono">{g.character_name || `${g.fls_id.slice(0, 8)}…`}</td>
                    <td className="px-3 font-mono">{g.package_version}</td>
                    <td className="px-3"><span className={g.status === "granted" ? "pill-ok" : "pill-err"}>{g.status}</span></td>
                    <td className="px-3 text-slate-400">{(g.updated_at || "").replace("T", " ").replace("Z", "")}</td>
                    <td className="px-3 text-slate-500 truncate max-w-[16rem]">{g.last_error || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
