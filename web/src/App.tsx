import { useEffect, useState } from "react";
import {
  BroadcastTab,
  MaintenanceTab,
  SettingsTab,
  VehiclesTab,
} from "./tabs";
import { OverviewTab } from "./OverviewTab";
import { PlayersWorkspace } from "./PlayersWorkspace";
import { GiveItemsTab } from "./GiveItemsTab";
import { MapTab } from "./MapTab";
import { DatabaseTab } from "./DatabaseTab";
import { GuildsTab } from "./GuildsTab";
import { SchedulerTab } from "./SchedulerTab";
import { AutoscalerTab } from "./AutoscalerTab";
import { LootTab } from "./LootTab";
import { MarketTab } from "./MarketTab";
import { EventsTab } from "./EventsTab";
import { SpiceTab } from "./SpiceTab";
import { InstancesTab } from "./InstancesTab";
import { BasesTab } from "./BasesTab";
import { Login, PlayerPickerModal, type ConsoleEntry } from "./components";
import { ToastStack } from "./ToastStack";
import { api, logout as apiLogout, me, onUnauthorized, setToken } from "./api";
import { TargetPill, TargetProvider } from "./target";
import { LiveProvider, LiveToggle } from "./live";
import { CommandPalette, SearchIcon } from "./CommandPalette";
import { TweaksPanel, GearIcon } from "./TweaksPanel";
import { applyTweaks, loadTweaks, saveTweaks, type Tweaks } from "./tweaks";
import { GlobalRipple } from "./ui";
import { Icon } from "./icons";

type TabId =
  | "bases"
  | "guilds"
  | "overview"
  | "map"
  | "broadcast"
  | "players"
  | "give-items"
  | "loot"
  | "market"
  | "vehicles"
  | "settings"
  | "spice"
  | "maintenance"
  | "scheduler"
  | "autoscaler"
  | "instances"
  | "database"
  | "events";

type TabGroup = "fleet" | "players" | "economy" | "server";

interface TabDef {
  id: TabId;
  label: string;
  icon: string;
  group: TabGroup;
}

// Group names for the top-bar's first tier. Plain names (SVG icons land in
// phase 8); the per-tab emoji below are the second-tier section markers.
const GROUP_LABELS: Record<TabGroup, string> = {
  fleet: "Fleet",
  players: "Players",
  economy: "Items & Economy",
  server: "Server",
};
const GROUP_ORDER: TabGroup[] = ["fleet", "players", "economy", "server"];

const TABS: TabDef[] = [
  // Fleet — the live world: who/what is running and where.
  { id: "overview", label: "Overview", icon: "dashboard", group: "fleet" },
  { id: "map", label: "Live Map", icon: "map", group: "fleet" },
  { id: "instances", label: "Instances", icon: "layers", group: "fleet" },
  { id: "autoscaler", label: "Autoscaler", icon: "trend", group: "fleet" },
  // Players — everything that acts on a player (or player-spawned content).
  { id: "players", label: "Players", icon: "players", group: "players" },
  { id: "vehicles", label: "Vehicles", icon: "vehicles", group: "players" },
  { id: "bases", label: "Bases", icon: "layers", group: "players" },
  { id: "guilds", label: "Guilds", icon: "shield", group: "players" },
  // Items & Economy — giving items + the in-game economy.
  { id: "give-items", label: "Give Items", icon: "items", group: "economy" },
  { id: "market", label: "Market", icon: "coin", group: "economy" },
  { id: "loot", label: "Loot & Difficulty", icon: "dice", group: "economy" },
  { id: "spice", label: "Spice Economy", icon: "spice", group: "economy" },
  // Server — global settings, comms, and operations.
  { id: "settings", label: "Settings", icon: "settings", group: "server" },
  { id: "broadcast", label: "Send Message", icon: "broadcast", group: "server" },
  { id: "maintenance", label: "Shutdown & Restart", icon: "power", group: "server" },
  { id: "scheduler", label: "Scheduler", icon: "clock", group: "server" },
  { id: "database", label: "Database", icon: "terminal", group: "server" },
  { id: "events", label: "Events & Diagnostics", icon: "terminal", group: "server" },
];

export default function App() {
  const [authState, setAuthState] = useState<"loading" | "out" | "in">("loading");
  const [tab, setTab] = useState<TabId>("overview");
  // Which group's sub-sections the top bar is showing. Lets you browse a group's
  // tabs without navigating (the design's "browse without committing").
  const [selGroup, setSelGroup] = useState<TabGroup>("fleet");
  const [mode, setMode] = useState<"ui" | "internal" | "?">("?");
  const [entries, setEntries] = useState<ConsoleEntry[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [recent, setRecent] = useState<TabId[]>(() => {
    try { return (JSON.parse(localStorage.getItem("dune.nav.recent") || "[]") as TabId[]).slice(0, 5); } catch { return []; }
  });
  const [tweaks, setTweaks] = useState<Tweaks>(() => loadTweaks());
  const [tweaksOpen, setTweaksOpen] = useState(false);

  useEffect(() => {
    onUnauthorized(() => {
      setToken(""); // legacy localStorage cleanup
      setAuthState("out");
    });
    api("GET", "/api/healthz").then((res) => {
      if (res.ok && (res.body as { mode: "ui" | "internal" }).mode) {
        setMode((res.body as { mode: "ui" | "internal" }).mode);
      }
    });
    me().then((res) => setAuthState(res.ok ? "in" : "out"));
  }, []);

  // ⌘K / Ctrl+K toggles the command palette.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Apply + persist appearance tweaks (theme / accent / font / density / motion).
  useEffect(() => {
    applyTweaks(tweaks);
    saveTweaks(tweaks);
  }, [tweaks]);

  async function logout() {
    try {
      await apiLogout();
    } catch {
      // ignore — the UI transition below is the visible behaviour
    }
    setToken(""); // legacy localStorage cleanup
    setAuthState("out");
    setEntries([]);
  }

  // Navigate to a section and sync the browsed group to where we landed.
  function go(id: TabId) {
    setTab(id);
    const g = TABS.find((t) => t.id === id)?.group;
    if (g) setSelGroup(g);
    setRecent((prev) => {
      const next = [id, ...prev.filter((x) => x !== id)].slice(0, 5);
      try { localStorage.setItem("dune.nav.recent", JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
    setPaletteOpen(false);
  }

  if (authState === "loading") {
    return (
      <div className="min-h-screen flex items-center justify-center text-slate-500 text-sm">
        Loading…
      </div>
    );
  }

  if (authState === "out") {
    return <Login onAuthed={() => setAuthState("in")} />;
  }

  const curGroup: TabGroup = TABS.find((t) => t.id === tab)?.group ?? "fleet";
  const sectionTabs = TABS.filter((t) => t.group === selGroup);

  return (
    <LiveProvider>
    <TargetProvider>
    <div className="shell">
      <div className="app-wrap">
        {/* floating two-tier top bar */}
        <header className="appbar">
          <div className="appbar-top">
            <div className="brand">
              <div className="brand-mark">◈</div>
              <div className="col">
                <div className="brand-name">Dune Admin</div>
                <div className="brand-sub">Pelican egg · mode {mode}</div>
              </div>
            </div>
            <nav className="grp-tabs" aria-label="Groups">
              {GROUP_ORDER.map((g) => (
                <button
                  key={g}
                  className={"grp-tab" + (g === selGroup ? " is-selected" : "")}
                  onClick={() => setSelGroup(g)}
                >
                  {GROUP_LABELS[g]}
                  {g === curGroup && <span className="grp-dot" title="your current page is in this group" />}
                </button>
              ))}
            </nav>
            <div className="appbar-tools">
              <button className="topbar-search" onClick={() => setPaletteOpen(true)} title="Search / jump to a section (⌘K)">
                <SearchIcon />
                <span className="hidden sm:inline">Search</span>
                <span className="topbar-search-kbd">⌘K</span>
              </button>
              <TargetPill />
              <LiveToggle />
              <button className="btn-ghost text-xs" onClick={() => setTweaksOpen((o) => !o)} title="Appearance" aria-label="Appearance">
                <GearIcon />
              </button>
              <button onClick={logout} className="btn-ghost text-xs">Log out</button>
            </div>
          </div>
          <div className="appbar-sub">
            <div className="sub-tabs" key={selGroup}>
              {sectionTabs.map((t) => (
                <button
                  key={t.id}
                  className={"sub-tab" + (t.id === tab ? " is-active" : "")}
                  onClick={() => go(t.id)}
                >
                  <Icon name={t.icon} size={18} />
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </header>

        <main className="content">
          <div key={tab} className="page flex flex-col gap-[var(--gap)]">
            {tab === "overview" && <OverviewTab setConsoleEntries={setEntries} />}
            {tab === "map" && <MapTab setConsoleEntries={setEntries} />}
            {tab === "broadcast" && <BroadcastTab setConsoleEntries={setEntries} />}
            {tab === "players" && <PlayersWorkspace setConsoleEntries={setEntries} />}
            {tab === "bases" && <BasesTab setConsoleEntries={setEntries} />}
            {tab === "guilds" && <GuildsTab setConsoleEntries={setEntries} />}
            {tab === "give-items" && <GiveItemsTab setConsoleEntries={setEntries} />}
            {tab === "loot" && <LootTab setConsoleEntries={setEntries} />}
            {tab === "spice" && <SpiceTab setConsoleEntries={setEntries} />}
            {tab === "market" && <MarketTab setConsoleEntries={setEntries} />}
            {tab === "vehicles" && <VehiclesTab setConsoleEntries={setEntries} />}
            {tab === "settings" && <SettingsTab setConsoleEntries={setEntries} />}
            {tab === "maintenance" && <MaintenanceTab setConsoleEntries={setEntries} />}
            {tab === "scheduler" && <SchedulerTab setConsoleEntries={setEntries} />}
            {tab === "autoscaler" && <AutoscalerTab setConsoleEntries={setEntries} />}
            {tab === "events" && <EventsTab setConsoleEntries={setEntries} entries={entries} onClearSession={() => setEntries([])} />}
            {tab === "instances" && <InstancesTab setConsoleEntries={setEntries} />}
            {tab === "database" && <DatabaseTab setConsoleEntries={setEntries} />}
          </div>
        </main>

        <footer className="mt-8 pt-4 border-t border-slate-800 text-xs text-slate-500 flex flex-wrap items-center gap-3 justify-between">
          <span>
            Pelican egg admin · protocol via{" "}
            <a href="https://github.com/adainrivers/dune-dedicated-server-manager" className="text-spice-400 hover:underline" target="_blank" rel="noreferrer">
              adainrivers
            </a>{" "}
            (MIT) · item images + info from{" "}
            <a href="https://awakening.wiki" className="text-spice-400 hover:underline" target="_blank" rel="noreferrer">
              awakening.wiki
            </a>{" "}
            (community wiki, fair-use)
          </span>
          <a href="https://github.com/Sergentval/pelican-egg-dune-awakening" className="text-slate-400 hover:text-slate-200" target="_blank" rel="noreferrer">
            source
          </a>
        </footer>
      </div>
    </div>
    <CommandPalette
      open={paletteOpen}
      onClose={() => setPaletteOpen(false)}
      items={TABS}
      groupLabels={GROUP_LABELS}
      recent={recent}
      currentTab={tab}
      onGo={(id) => go(id as TabId)}
    />
    <ToastStack entries={entries} />
    <TweaksPanel open={tweaksOpen} onClose={() => setTweaksOpen(false)} tweaks={tweaks} onChange={setTweaks} />
    <GlobalRipple />
    <PlayerPickerModal />
    </TargetProvider>
    </LiveProvider>
  );
}
