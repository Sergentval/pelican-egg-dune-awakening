// Events & Diagnostics workspace (IA phase 4) — one tab for the two
// after-the-fact views, as sub-tabs. Each renders the original component
// unchanged:
//   • Service logs   — live per-service log tail + restart (LogsTab)
//   • Command audit  — log of every admin action run from this panel (HistoryTab)

import { type Dispatch, type SetStateAction, useState } from "react";
import type { ConsoleEntry } from "./components";
import { LogsTab } from "./LogsTab";
import { HistoryTab } from "./tabs";
import { Icon } from "./icons";
import { PlayerEventsPanel } from "./PlayerEventsPanel";
import { BattlepassPanel } from "./BattlepassPanel";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

type Sub = "live" | "battlepass" | "logs" | "audit";

const SUBS: { id: Sub; label: string; icon: string; hint: string }[] = [
  { id: "live", label: "Player events", icon: "target", hint: "Zone races + milestone events that watch the world and pay rewards" },
  { id: "battlepass", label: "Battlepass", icon: "skills", hint: "188-tier progression pass over levels, quests and exploration" },
  { id: "logs", label: "Service logs", icon: "terminal", hint: "Live per-service log tail + restart a single service" },
  { id: "audit", label: "Command audit", icon: "history", hint: "Every admin action run from this panel (who ran what)" },
];

export function EventsTab({ setConsoleEntries, entries, onClearSession }: { setConsoleEntries: SetEntries; entries?: ConsoleEntry[]; onClearSession?: () => void }) {
  const [sub, setSub] = useState<Sub>("logs");
  const active = SUBS.find((s) => s.id === sub) ?? SUBS[0];
  return (
    <div className="space-y-4">
      <div className="card">
        <div className="px-3 pt-3 flex flex-wrap gap-1">
          {SUBS.map((s) => (
            <button
              key={s.id}
              onClick={() => setSub(s.id)}
              title={s.hint}
              className={
                "text-sm px-3 py-1.5 rounded-t flex items-center gap-2 border-b-2 transition " +
                (sub === s.id
                  ? "border-spice-500 text-spice-200"
                  : "border-transparent text-slate-400 hover:bg-slate-800")
              }
            >
              <Icon name={s.icon} size={18} />
              {s.label}
            </button>
          ))}
        </div>
        <p className="px-4 py-2 text-xs text-slate-500 border-t border-slate-800">{active.hint}.</p>
      </div>

      {sub === "live" && <PlayerEventsPanel setConsoleEntries={setConsoleEntries} />}
      {sub === "battlepass" && <BattlepassPanel setConsoleEntries={setConsoleEntries} />}
      {sub === "logs" && <LogsTab setConsoleEntries={setConsoleEntries} />}
      {sub === "audit" && <HistoryTab sessionEntries={entries} onClearSession={onClearSession} />}
    </div>
  );
}
