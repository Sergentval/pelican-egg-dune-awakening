// Players workspace (IA phase 7) — one tab for everything you do to a single
// player, as sub-tabs. Each sub-tab renders the original component unchanged,
// so every control is preserved:
//   • Roster    — who's online; kick / clean inventory / reset (PlayersTab)
//   • Character — currency, XP, faction, identity, keystones, danger ops (PlayerEditorTab)
//   • Skills    — grant skills + points + XP (SkillsTab)
//   • Inventory — view / delete a player's items (InventoryTab)
//   • Teleport  — move a player to a location or coordinates (MovementTab)
//
// All sub-tabs read the same shared target (TargetProvider), so the player you
// pick in one carries across the others. Vehicles stays its own tab.

import { type Dispatch, type SetStateAction, useState } from "react";
import type { ConsoleEntry } from "./components";
import { InventoryTab, MovementTab, PlayersTab, SkillsTab } from "./tabs";
import { PlayerEditorTab } from "./PlayerEditorTab";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

type Sub = "roster" | "character" | "skills" | "inventory" | "teleport";

const SUBS: { id: Sub; label: string; icon: string; hint: string }[] = [
  { id: "roster", label: "Roster", icon: "👥", hint: "Who's online; kick, clean inventory, reset progression" },
  { id: "character", label: "Character", icon: "🪪", hint: "Currency, XP, faction, identity, keystones, danger ops" },
  { id: "skills", label: "Skills", icon: "✨", hint: "Grant skills, spend skill points, award XP" },
  { id: "inventory", label: "Inventory", icon: "🎒", hint: "View and delete a player's items" },
  { id: "teleport", label: "Teleport", icon: "🧭", hint: "Move a player to a saved location or coordinates" },
];

export function PlayersWorkspace({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [sub, setSub] = useState<Sub>("roster");
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
              <span aria-hidden>{s.icon}</span>
              {s.label}
            </button>
          ))}
        </div>
        <p className="px-4 py-2 text-xs text-slate-500 border-t border-slate-800">
          {active.hint}. <span className="text-slate-400">The player you pick carries across all of these.</span>
        </p>
      </div>

      {sub === "roster" && <PlayersTab setConsoleEntries={setConsoleEntries} />}
      {sub === "character" && <PlayerEditorTab setConsoleEntries={setConsoleEntries} />}
      {sub === "skills" && <SkillsTab setConsoleEntries={setConsoleEntries} />}
      {sub === "inventory" && <InventoryTab setConsoleEntries={setConsoleEntries} />}
      {sub === "teleport" && <MovementTab setConsoleEntries={setConsoleEntries} />}
    </div>
  );
}
