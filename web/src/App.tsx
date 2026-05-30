import { useEffect, useState } from "react";
import {
  BroadcastTab,
  Dashboard,
  HistoryTab,
  ItemsTab,
  KitsTab,
  MaintenanceTab,
  MovementTab,
  PlayersTab,
  SkillsTab,
  VehiclesTab,
} from "./tabs";
import { Login, OutputConsole, type ConsoleEntry } from "./components";
import { api, logout as apiLogout, me, onUnauthorized, setToken } from "./api";
import { TargetPill, TargetProvider } from "./target";

type TabId =
  | "dashboard"
  | "broadcast"
  | "players"
  | "kits"
  | "items"
  | "skills"
  | "vehicles"
  | "movement"
  | "maintenance"
  | "history";

interface TabDef {
  id: TabId;
  label: string;
  icon: string;
  group: "overview" | "commands" | "system";
}

const TABS: TabDef[] = [
  { id: "dashboard", label: "Dashboard", icon: "◆", group: "overview" },
  { id: "kits", label: "Kits", icon: "🎁", group: "commands" },
  { id: "broadcast", label: "Broadcast", icon: "📣", group: "commands" },
  { id: "players", label: "Players", icon: "👥", group: "commands" },
  { id: "items", label: "Items", icon: "📦", group: "commands" },
  { id: "skills", label: "Skills", icon: "✨", group: "commands" },
  { id: "vehicles", label: "Vehicles", icon: "🚗", group: "commands" },
  { id: "movement", label: "Movement", icon: "🧭", group: "commands" },
  { id: "maintenance", label: "Maintenance", icon: "🛠", group: "system" },
  { id: "history", label: "History", icon: "🗒", group: "system" },
];

export default function App() {
  const [authState, setAuthState] = useState<"loading" | "out" | "in">("loading");
  const [tab, setTab] = useState<TabId>("dashboard");
  const [mode, setMode] = useState<"ui" | "internal" | "?">("?");
  const [entries, setEntries] = useState<ConsoleEntry[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);

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
    // No localStorage check anymore — auth is determined entirely by
    // whether the HttpOnly session cookie is present and valid. /api/me
    // returns 200 + session info when the cookie is good; 401 otherwise.
    me().then((res) => setAuthState(res.ok ? "in" : "out"));
  }, []);

  async function logout() {
    // Tell the backend to revoke this session's jti and clear cookies.
    // Even if the request fails (e.g. the cookie is already dead) we
    // still flip the UI to the login screen.
    try {
      await apiLogout();
    } catch {
      // ignore — the UI transition below is the visible behaviour
    }
    setToken(""); // legacy localStorage cleanup
    setAuthState("out");
    setEntries([]);
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

  return (
    <TargetProvider>
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-slate-800 px-4 sm:px-6 py-3 flex items-center justify-between bg-slate-950/80 backdrop-blur sticky top-0 z-30 gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <button
            className="lg:hidden text-slate-400 hover:text-slate-100"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            aria-label="Toggle menu"
          >
            ☰
          </button>
          <span className="text-xl">🌀</span>
          <div className="hidden sm:block">
            <div className="text-spice-300 font-semibold leading-tight">Dune Admin</div>
            <div className="text-xs text-slate-500">Pelican egg</div>
          </div>
        </div>
        <div className="hidden md:block min-w-0 truncate">
          <TargetPill />
        </div>
        <div className="flex items-center gap-3">
          <span className={mode === "ui" ? "pill-ok" : "pill-warn"}>mode: {mode}</span>
          <button onClick={logout} className="btn-ghost text-xs">
            Log out
          </button>
        </div>
      </header>
      <div className="md:hidden border-b border-slate-800 px-4 py-2 bg-slate-950/60">
        <TargetPill />
      </div>

      <div className="flex-1 flex">
        {/* Sidebar */}
        <aside
          className={
            "w-60 border-r border-slate-800 bg-slate-950 shrink-0 " +
            "fixed inset-y-0 left-0 top-[57px] z-20 transform transition-transform lg:static lg:translate-x-0 " +
            (sidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0")
          }
        >
          <nav className="py-4 px-2 space-y-6">
            {(["overview", "commands", "system"] as const).map((group) => (
              <div key={group}>
                <div className="text-[10px] uppercase tracking-wider text-slate-500 px-3 mb-2">
                  {group}
                </div>
                <div className="space-y-0.5">
                  {TABS.filter((t) => t.group === group).map((t) => (
                    <button
                      key={t.id}
                      onClick={() => {
                        setTab(t.id);
                        setSidebarOpen(false);
                      }}
                      className={
                        "w-full text-left px-3 py-2 rounded text-sm flex items-center gap-3 transition " +
                        (tab === t.id ? "bg-spice-900/40 text-spice-200" : "text-slate-300 hover:bg-slate-800")
                      }
                    >
                      <span className="w-5 text-center">{t.icon}</span>
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </nav>
        </aside>

        {sidebarOpen && (
          <div className="fixed inset-0 bg-slate-950/50 z-10 lg:hidden" onClick={() => setSidebarOpen(false)} />
        )}

        <main className="flex-1 min-w-0 p-4 sm:p-6 space-y-6 overflow-x-auto">
          {tab === "dashboard" && <Dashboard setConsoleEntries={setEntries} />}
          {tab === "kits" && <KitsTab setConsoleEntries={setEntries} />}
          {tab === "broadcast" && <BroadcastTab setConsoleEntries={setEntries} />}
          {tab === "players" && <PlayersTab setConsoleEntries={setEntries} />}
          {tab === "items" && <ItemsTab setConsoleEntries={setEntries} />}
          {tab === "skills" && <SkillsTab setConsoleEntries={setEntries} />}
          {tab === "vehicles" && <VehiclesTab setConsoleEntries={setEntries} />}
          {tab === "movement" && <MovementTab setConsoleEntries={setEntries} />}
          {tab === "maintenance" && <MaintenanceTab setConsoleEntries={setEntries} />}
          {tab === "history" && <HistoryTab />}

          <OutputConsole entries={entries} onClear={() => setEntries([])} />
        </main>
      </div>

      <footer className="border-t border-slate-800 px-4 sm:px-6 py-3 text-xs text-slate-500 flex flex-wrap items-center gap-3 justify-between">
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
        <span>
          <a href="https://github.com/Sergentval/pelican-egg-dune-awakening" className="text-slate-400 hover:text-slate-200" target="_blank" rel="noreferrer">
            source
          </a>
        </span>
      </footer>
    </div>
    </TargetProvider>
  );
}
