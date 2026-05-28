// Every tab the SPA renders. Each tab is a React component that calls
// the corresponding admin-publish.sh subcommand via /admin/<sub>.

import { useEffect, useMemo, useState } from "react";
import {
  fetchHistory,
  fetchItems,
  fetchPlayers,
  fetchPos,
  fetchSkills,
  fetchVehicles,
  parsePlayerTable,
  parsePosOutput,
  publish,
  type HistoryResponse,
  type ItemRow,
  type PlayerRow,
  type PublishResult,
  type SkillRow,
  type VehicleClass,
} from "./api";
import {
  Confirm,
  PlayerPicker,
  pushToConsole,
  type ConsoleEntry,
} from "./components";

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
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    const res = await fetchPlayers("all");
    if (res.ok) setPlayers(parsePlayerTable((res.body as PublishResult).stdout));
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
                <th className="text-left px-4 py-2 font-medium">FLS id</th>
                <th className="text-left px-4 py-2 font-medium">Steam id</th>
                <th className="text-left px-4 py-2 font-medium">Life</th>
                <th className="text-left px-4 py-2 font-medium">Status</th>
                <th className="text-left px-4 py-2 font-medium">Last activity</th>
                <th className="text-left px-4 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {players.length === 0 && (
                <tr>
                  <td colSpan={6} className="text-center text-slate-500 py-8">
                    {loading ? "Loading…" : "No players seen yet"}
                  </td>
                </tr>
              )}
              {players.map((p) => (
                <tr key={p.fls_id} className="border-t border-slate-800/50">
                  <td className="px-4 py-2 font-mono">{p.fls_id}</td>
                  <td className="px-4 py-2 font-mono text-slate-400">{p.steam_id || "-"}</td>
                  <td className="px-4 py-2">{p.life}</td>
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
                        const res = await fetchPos(p.fls_id);
                        pushToConsole(
                          setConsoleEntries,
                          `pos ${p.fls_id}`,
                          res.body as PublishResult,
                          res.ok && (res.body as PublishResult).ok,
                        );
                      }}
                    >
                      pos
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
  const [playerId, setPlayerId] = useState("me");
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
      player_id: playerId,
      item: picked.id,
      qty,
      durability,
    }, `give ${playerId} ${picked.id} ×${qty}`);
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="card">
        <header className="card-header">
          <h2 className="font-semibold">Grant item</h2>
        </header>
        <form onSubmit={submit} className="p-4 space-y-4">
          <PlayerPicker value={playerId} onChange={setPlayerId} />
          <div>
            <label className="label">Item search</label>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="spice, crysknife, stilltent, solari…"
              className="input-field"
            />
            {picked && (
              <div className="mt-2 text-xs text-slate-300 flex items-center gap-2">
                Selected:
                <span className="font-mono text-spice-300">{picked.id}</span>
                <span className="text-slate-500">({picked.name || "?"})</span>
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
          {matches.map((it) => (
            <button
              type="button"
              key={it.id + it.source}
              onClick={() => setPicked(it)}
              className={
                "block w-full text-left px-4 py-2 text-xs border-b border-slate-800 hover:bg-slate-800 transition " +
                (picked?.id === it.id && picked?.source === it.source ? "bg-spice-900/30" : "")
              }
            >
              <div className="font-mono text-spice-300">{it.id}</div>
              <div className="text-slate-400 mt-0.5">
                {it.name} <span className="text-slate-600">— {it.category} / {it.source}</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---- Skills -----------------------------------------------------------

export function SkillsTab({ setConsoleEntries }: TabProps) {
  const [playerId, setPlayerId] = useState("me");
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
      player_id: playerId,
      module: picked.id,
      level,
    }, `skill ${playerId} ${picked.id} =${level}`);
  }

  async function submitPoints(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "points", { player_id: playerId, amount: unspent }, `points ${playerId} =${unspent}`);
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="space-y-6">
        <div className="card">
          <header className="card-header">
            <h2 className="font-semibold">Set skill module level</h2>
          </header>
          <form onSubmit={submitSkill} className="p-4 space-y-4">
            <PlayerPicker value={playerId} onChange={setPlayerId} />
            <div>
              <label className="label">Module</label>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="swordmaster, bene, voice, blade…"
                className="input-field"
              />
              {picked && (
                <div className="mt-2 text-xs text-slate-300 flex items-center gap-2">
                  Selected:
                  <span className="font-mono text-spice-300">{picked.id}</span>
                  <span className="text-slate-500">({picked.name || "?"})</span>
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
            <PlayerPicker value={playerId} onChange={setPlayerId} />
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
          {matches.map((s) => (
            <button
              type="button"
              key={s.id}
              onClick={() => setPicked(s)}
              className={
                "block w-full text-left px-4 py-2 text-xs border-b border-slate-800 hover:bg-slate-800 transition " +
                (picked?.id === s.id ? "bg-spice-900/30" : "")
              }
            >
              <div className="font-mono text-spice-300">{s.id}</div>
              <div className="text-slate-400 mt-0.5">
                {s.name}
                <span className="text-slate-600"> — {s.category} (max {s.maxLevel})</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---- Vehicles --------------------------------------------------------

export function VehiclesTab({ setConsoleEntries }: TabProps) {
  const [playerId, setPlayerId] = useState("me");
  const [vehicles, setVehicles] = useState<VehicleClass[]>([]);
  const [className, setClassName] = useState("");
  const [tplName, setTplName] = useState("");
  const [x, setX] = useState(0);
  const [y, setY] = useState(0);
  const [z, setZ] = useState(0);
  const [rotation, setRotation] = useState<number | "">("");
  const [persistent, setPersistent] = useState(1.0);
  const [posLoading, setPosLoading] = useState(false);

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
    setPosLoading(true);
    const res = await fetchPos(playerId);
    setPosLoading(false);
    pushToConsole(setConsoleEntries, `pos ${playerId}`, res.body as PublishResult, res.ok && (res.body as PublishResult).ok);
    if (res.ok) {
      const parsed = parsePosOutput((res.body as PublishResult).stdout);
      if (parsed) {
        setX(Math.round(parsed.x));
        setY(Math.round(parsed.y));
        setZ(Math.round(parsed.z));
      }
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!className || !tplName) return;
    const body: Record<string, unknown> = {
      player_id: playerId,
      class: className,
      x,
      y,
      z,
      template: tplName,
      persistent,
    };
    if (rotation !== "") body["rotation"] = rotation;
    await runAndLog(setConsoleEntries, "vehicle", body, `vehicle ${playerId} ${className}/${tplName}`);
  }

  return (
    <div className="card max-w-3xl">
      <header className="card-header">
        <h2 className="font-semibold">Spawn vehicle</h2>
        <span className="text-xs text-slate-500">{vehicles.length} classes available</span>
      </header>
      <form onSubmit={submit} className="p-4 space-y-4">
        <PlayerPicker value={playerId} onChange={setPlayerId} />

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="label" htmlFor="vh-class">Class</label>
            <select id="vh-class" value={className} onChange={(e) => setClassName(e.target.value)} className="input-field">
              <option value="" disabled>Select a vehicle…</option>
              {vehicles.map((v) => (
                <option key={v.id} value={v.id}>{v.id}</option>
              ))}
            </select>
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
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="label !mb-0">Position</span>
            <button type="button" className="btn-ghost text-xs" onClick={fetchAndFillPos} disabled={posLoading}>
              {posLoading ? "…" : `fill from ${playerId}`}
            </button>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <input type="number" placeholder="X" value={x} onChange={(e) => setX(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Y" value={y} onChange={(e) => setY(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Z" value={z} onChange={(e) => setZ(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
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
  const [playerId, setPlayerId] = useState("me");
  const [x, setX] = useState(0);
  const [y, setY] = useState(0);
  const [z, setZ] = useState(0);
  const [yaw, setYaw] = useState<number | "">("");
  const [mode, setMode] = useState<"teleport" | "tpsafe">("teleport");

  async function lookupPos() {
    const res = await fetchPos(playerId);
    pushToConsole(setConsoleEntries, `pos ${playerId}`, res.body as PublishResult, res.ok && (res.body as PublishResult).ok);
    if (res.ok) {
      const parsed = parsePosOutput((res.body as PublishResult).stdout);
      if (parsed) {
        setX(Math.round(parsed.x));
        setY(Math.round(parsed.y));
        setZ(Math.round(parsed.z));
      }
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { player_id: playerId, x, y, z };
    if (yaw !== "") body["yaw"] = yaw;
    await runAndLog(setConsoleEntries, mode, body, `${mode} ${playerId} → ${x},${y},${z}`);
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
        <PlayerPicker value={playerId} onChange={setPlayerId} />
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="label !mb-0">Destination</span>
            <button type="button" className="btn-ghost text-xs" onClick={lookupPos}>
              read {playerId}'s current pos
            </button>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <input type="number" placeholder="X" value={x} onChange={(e) => setX(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Y" value={y} onChange={(e) => setY(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
            <input type="number" placeholder="Z" value={z} onChange={(e) => setZ(parseFloat(e.target.value) || 0)} className="input-field font-mono" />
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
  // Shutdown
  const [shutType, setShutType] = useState("Restart");
  const [shutLead, setShutLead] = useState(600);
  const [shutFreq, setShutFreq] = useState(60);

  // XP
  const [xpPlayer, setXpPlayer] = useState("me");
  const [xpAmount, setXpAmount] = useState(5000);

  // Water
  const [waterPlayer, setWaterPlayer] = useState("me");
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
    await runAndLog(setConsoleEntries, "xp", { player_id: xpPlayer, amount: xpAmount }, `xp ${xpPlayer} +${xpAmount}`);
  }

  async function submitWater(e: React.FormEvent) {
    e.preventDefault();
    await runAndLog(setConsoleEntries, "water", { player_id: waterPlayer, amount: waterAmount }, `water ${waterPlayer}`);
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
          <PlayerPicker value={xpPlayer} onChange={setXpPlayer} />
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
          <PlayerPicker value={waterPlayer} onChange={setWaterPlayer} allowStar />
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
  const [playerId, setPlayerId] = useState("me");
  const [confirm, setConfirm] = useState<{ sub: string; label: string; warn: string } | null>(null);

  function attempt(sub: string, label: string, warn: string) {
    setConfirm({ sub, label, warn });
  }

  async function execute() {
    if (!confirm) return;
    const { sub, label } = confirm;
    setConfirm(null);
    await runAndLog(setConsoleEntries, sub, { player_id: playerId }, `${sub} ${playerId} — ${label}`);
  }

  return (
    <div className="card max-w-2xl">
      <header className="card-header">
        <h2 className="font-semibold">Player management</h2>
      </header>
      <div className="p-4 space-y-4">
        <PlayerPicker value={playerId} onChange={setPlayerId} allowStar />
        <div className="grid grid-cols-2 gap-3">
          <button
            className="btn-ghost border border-slate-700"
            onClick={() => attempt("kick", "Kick", `Disconnect ${playerId}. They can reconnect immediately.`)}
          >
            Kick
          </button>
          <button
            className="btn-danger"
            onClick={() => attempt("clean", "Clean inventory", `Wipes ${playerId}'s entire inventory. Unrecoverable.`)}
          >
            Clean inventory
          </button>
          <button
            className="btn-danger col-span-2"
            onClick={() => attempt("reset", "Reset progression", `Wipes ${playerId}'s XP, skill levels, and unspent points. Unrecoverable.`)}
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
