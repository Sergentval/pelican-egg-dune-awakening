// Every tab the SPA renders. Each tab is a React component that calls
// the corresponding admin-publish.sh subcommand via /admin/<sub>.

import { useEffect, useMemo, useState } from "react";
import {
  dgtSearch,
  fetchHistory,
  fetchItems,
  fetchPlayers,
  fetchSkills,
  fetchSteamInfo,
  fetchVehicles,
  itemCategoryStyle,
  parsePlayerTable,
  publish,
  skillCategoryStyle,
  vehicleIcon,
  type HistoryResponse,
  type ItemRow,
  type PlayerRow,
  type PublishResult,
  type SkillRow,
  type SteamPersona,
  type VehicleClass,
} from "./api";
import {
  Confirm,
  PlayerPicker,
  pushToConsole,
  type ConsoleEntry,
} from "./components";
import { useTarget } from "./target";

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
                              ? `${looked.map} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`
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

export function BroadcastTab({ setConsoleEntries }: TabProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [duration, setDuration] = useState(20);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "broadcast", { title, body, duration }, `broadcast "${title}"`);
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Server-wide broadcast</h2>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <div>
          <label className="label" htmlFor="bcast-title">Title</label>
          <input id="bcast-title" required value={title} onChange={(e) => setTitle(e.target.value)} className="input-field" placeholder="Maintenance" />
        </div>
        <div>
          <label className="label" htmlFor="bcast-body">Body</label>
          <textarea id="bcast-body" required value={body} onChange={(e) => setBody(e.target.value)} className="input-field min-h-[80px]" placeholder="Restart in 5 minutes — save your work" />
        </div>
        <div>
          <label className="label" htmlFor="bcast-dur">Display duration</label>
          <div className="flex items-center gap-2">
            <input id="bcast-dur" type="number" min={1} max={300} value={duration} onChange={(e) => setDuration(parseInt(e.target.value) || 20)} className="input-field w-32" />
            <span className="text-xs text-slate-500">seconds</span>
          </div>
        </div>
        <button type="submit" className="btn-primary">Send</button>
      </form>
    </div>
  );
}

// ---- Items ------------------------------------------------------------

export function ItemsTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ItemRow[]>([]);
  const [picked, setPicked] = useState<ItemRow | null>(null);
  const [qty, setQty] = useState(1);
  const [durability, setDurability] = useState(1.0);
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    if (!query.trim()) {
      setMatches([]);
      return;
    }
    const handle = setTimeout(async () => {
      setSearching(true);
      const res = await fetchItems(query, 40);
      if (res.ok) setMatches((res.body as { items: ItemRow[] }).items);
      setSearching(false);
    }, 200);
    return () => clearTimeout(handle);
  }, [query]);

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
    <div className="grid gap-6 md:grid-cols-2">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Grant item</h2>
        </header>
        <form onSubmit={submit} className="p-4 space-y-4">
          <PlayerPicker />
          <div>
            <label className="label">Item search</label>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="spice, crysknife, stilltent, solari…"
              className="input-field"
            />
            {picked && (
              <div className="mt-2 text-xs text-slate-300 flex items-center gap-2 flex-wrap">
                <span aria-hidden>{itemCategoryStyle(picked.category).icon}</span>
                <span className={`font-mono ${itemCategoryStyle(picked.category).color}`}>{picked.id}</span>
                <span className="text-slate-500">({picked.name || "?"})</span>
                <a
                  href={dgtSearch(picked.name || picked.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-slate-500 hover:text-spice-300"
                >
                  ↗
                </a>
                <button type="button" className="btn-ghost text-xs ml-auto" onClick={() => setPicked(null)}>
                  clear
                </button>
              </div>
            )}
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

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Results</h2>
          <span className="text-xs text-slate-500">{searching ? "searching…" : `${matches.length} match`}</span>
        </header>
        <div className="max-h-[600px] overflow-y-auto">
          {matches.length === 0 && (
            <div className="p-8 text-center text-slate-500 text-sm">
              Type to search 2558 items
            </div>
          )}
          {matches.map((it) => {
            const sty = itemCategoryStyle(it.category);
            const active = picked?.id === it.id && picked?.source === it.source;
            return (
              <div
                key={it.id + it.source}
                className={
                  "flex items-start gap-2 border-b border-slate-800 hover:bg-slate-800 transition " +
                  (active ? "bg-spice-900/30" : "")
                }
              >
                <button
                  type="button"
                  onClick={() => setPicked(it)}
                  className="flex-1 text-left px-4 py-2 text-xs min-w-0"
                >
                  <div className="flex items-center gap-1.5">
                    <span aria-hidden>{sty.icon}</span>
                    <span className={`font-mono ${sty.color}`}>{it.id}</span>
                  </div>
                  <div className="text-slate-400 mt-0.5 truncate">
                    {it.name} <span className="text-slate-600">— {it.category} / {it.source}</span>
                  </div>
                </button>
                <a
                  href={dgtSearch(it.name || it.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="self-center px-3 py-2 text-slate-500 hover:text-spice-300"
                  title="View on dune.gaming.tools"
                  onClick={(e) => e.stopPropagation()}
                >
                  ↗
                </a>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---- Skills -----------------------------------------------------------

export function SkillsTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<SkillRow[]>([]);
  const [picked, setPicked] = useState<SkillRow | null>(null);
  const [level, setLevel] = useState(1);
  const [unspent, setUnspent] = useState(50);

  useEffect(() => {
    const handle = setTimeout(async () => {
      const res = await fetchSkills(query, 60);
      if (res.ok) setMatches((res.body as { skills: SkillRow[] }).skills);
    }, 200);
    return () => clearTimeout(handle);
  }, [query]);

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
                    href={dgtSearch(picked.name || picked.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="text-slate-500 hover:text-spice-300"
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
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Modules</h2>
          <span className="text-xs text-slate-500">{matches.length} match</span>
        </header>
        <div className="max-h-[600px] overflow-y-auto">
          {matches.map((s) => {
            const sty = skillCategoryStyle(s.category);
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
                  </div>
                  <div className="text-slate-400 mt-0.5 truncate">
                    {s.name} <span className="text-slate-600">— {s.category} (max {s.maxLevel})</span>
                  </div>
                </button>
                <a
                  href={dgtSearch(s.name || s.id)}
                  target="_blank"
                  rel="noreferrer"
                  className="self-center px-3 py-2 text-slate-500 hover:text-spice-300"
                  title="View on dune.gaming.tools"
                  onClick={(e) => e.stopPropagation()}
                >
                  ↗
                </a>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---- Vehicles --------------------------------------------------------

export function VehiclesTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [vehicles, setVehicles] = useState<VehicleClass[]>([]);
  const [className, setClassName] = useState("");
  const [tplName, setTplName] = useState("");
  // Local X/Y/Z override — initializes from the shared target.pos but
  // the operator can edit before spawning. When `dirty` is false the
  // displayed value tracks target.pos so a position lookup in another
  // tab is visible here immediately.
  const [override, setOverride] = useState<{ x: number; y: number; z: number; dirty: boolean }>({ x: 0, y: 0, z: 0, dirty: false });
  const [rotation, setRotation] = useState<number | "">("");
  const [persistent, setPersistent] = useState(1.0);

  const x = override.dirty ? override.x : target.pos?.x !== undefined ? Math.round(target.pos.x) : 0;
  const y = override.dirty ? override.y : target.pos?.y !== undefined ? Math.round(target.pos.y) : 0;
  const z = override.dirty ? override.z : target.pos?.z !== undefined ? Math.round(target.pos.z) : 0;

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
        `${looked.map} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`,
        true,
      );
    } else {
      pushToConsole(setConsoleEntries, `pos ${target.playerId}`, target.posError || "lookup failed", false);
    }
  }

  function setXyz(axis: "x" | "y" | "z", v: number) {
    setOverride((prev) => ({
      x: axis === "x" ? v : prev.dirty ? prev.x : x,
      y: axis === "y" ? v : prev.dirty ? prev.y : y,
      z: axis === "z" ? v : prev.dirty ? prev.z : z,
      dirty: true,
    }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!className || !tplName) return;
    const body: Record<string, unknown> = {
      player_id: target.playerId,
      class: className,
      x,
      y,
      z,
      template: tplName,
      persistent,
    };
    if (rotation !== "") body["rotation"] = rotation;
    await runAndLog(setConsoleEntries, "vehicle", body, `vehicle ${target.playerId} ${className}/${tplName}`);
  }

  return (
    <div className="card max-w-3xl">
      <header className="card-header">
        <h2 className="font-semibold">Spawn vehicle</h2>
        <span className="text-xs text-slate-500">{vehicles.length} classes available</span>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <PlayerPicker />

        <div>
          <label className="label">Class</label>
          <div className="grid grid-cols-3 sm:grid-cols-5 gap-2 mb-2">
            {vehicles.map((v) => (
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
                <span className="text-2xl leading-none" aria-hidden>{vehicleIcon(v.id)}</span>
                <span className="truncate w-full text-center">{v.id}</span>
              </button>
            ))}
          </div>
          {className && (
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>
                {vehicles.find((v) => v.id === className)?.templates.length || 0} templates available
              </span>
              <a
                href={dgtSearch(className)}
                target="_blank"
                rel="noreferrer"
                className="text-slate-500 hover:text-spice-300"
              >
                view {className} on dune.gaming.tools ↗
              </a>
            </div>
          )}
        </div>
        <div>
          <label className="label" htmlFor="vh-tpl">Template</label>
          <select id="vh-tpl" value={tplName} onChange={(e) => setTplName(e.target.value)} className="input-field" disabled={currentTpls.length === 0}>
            {currentTpls.length === 0 && <option value="">Pick a class first</option>}
            {currentTpls.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
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
            <input type="number" placeholder="X" value={x} onChange={(e) => setXyz("x", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Y" value={y} onChange={(e) => setXyz("y", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Z" value={z} onChange={(e) => setXyz("z", parseFloat(e.target.value) || 0)} className="input-field font-mono" />
          </div>
          <p className="text-xs text-slate-500 mt-1">Tip: spawn drops at exact Z — for flying vehicles add ~200 above the ground.</p>
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

        <button type="submit" className="btn-primary" disabled={!className || !tplName}>Spawn</button>
      </form>
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
        `${looked.map} (${Math.round(looked.x)}, ${Math.round(looked.y)}, ${Math.round(looked.z)})`,
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
  const target = useTarget();
  const [shutType, setShutType] = useState("Restart");
  const [shutLead, setShutLead] = useState(600);
  const [shutFreq, setShutFreq] = useState(60);
  const [xpAmount, setXpAmount] = useState(5000);
  const [waterAmount, setWaterAmount] = useState(1_000_000);

  async function submitShutdown(e: React.FormEvent, cancel = false) {
    e.preventDefault();
    if (cancel) {
      await runAndLog(setConsoleEntries, "shutdown", { type: "cancel" }, "shutdown cancel");
    } else {
      await runAndLog(setConsoleEntries, "shutdown", { type: shutType, lead_secs: shutLead, freq_secs: shutFreq }, `shutdown ${shutType} in ${shutLead}s`);
    }
  }

  async function submitXp(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "xp", { player_id: target.playerId, amount: xpAmount }, `xp ${target.playerId} +${xpAmount}`);
  }

  async function submitWater(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "water", { player_id: target.playerId, amount: waterAmount }, `water ${target.playerId}`);
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Scheduled shutdown</h2>
        </header>
        <form onSubmit={(e) => submitShutdown(e)} className="p-4 space-y-4">
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

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Award XP</h2>
        </header>
        <form onSubmit={submitXp} className="p-4 space-y-4">
          <PlayerPicker />
          <div>
            <label className="label" htmlFor="xp-amt">Amount</label>
            <input id="xp-amt" type="number" min={1} value={xpAmount} onChange={(e) => setXpAmount(parseInt(e.target.value) || 0)} className="input-field" />
          </div>
          <button type="submit" className="btn-primary">Grant</button>
        </form>
      </div>

      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Refill water</h2>
        </header>
        <form onSubmit={submitWater} className="p-4 space-y-4">
          <PlayerPicker allowStar />
          <div>
            <label className="label" htmlFor="water-amt">Water amount</label>
            <input id="water-amt" type="number" min={1} value={waterAmount} onChange={(e) => setWaterAmount(parseInt(e.target.value) || 0)} className="input-field" />
          </div>
          <button type="submit" className="btn-primary">Refill</button>
        </form>
      </div>
    </div>
  );
}

// ---- Players (kick + clean + reset) -----------------------------------

export function PlayersTab({ setConsoleEntries }: TabProps) {
  const target = useTarget();
  const [confirm, setConfirm] = useState<{ sub: string; label: string; warn: string } | null>(null);

  function attempt(sub: string, label: string, warn: string) {
    setConfirm({ sub, label, warn });
  }

  async function execute() {
    if (!confirm) return;
    const { sub, label } = confirm;
    setConfirm(null);
    await runAndLog(setConsoleEntries, sub, { player_id: target.playerId }, `${sub} ${target.playerId} — ${label}`);
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Player management</h2>
      </header>
      <div className="p-4 space-y-4">
        <PlayerPicker allowStar />
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
