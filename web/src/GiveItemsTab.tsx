// Give Items workspace (IA phase 3) — one tab for the three "give a player
// items" flows, as sub-tabs. Each sub-tab renders the original, unchanged
// component so every control is preserved:
//   • Single item   — ad-hoc give one item (ItemsTab)
//   • Bundles       — give a pre-built kit (KitsTab)
//   • New-player kit — auto-grant on first join (WelcomeTab)

import { type Dispatch, type SetStateAction, useState } from "react";
import type { ConsoleEntry } from "./components";
import { ItemsTab, KitsTab, WelcomeTab } from "./tabs";
import { Icon } from "./icons";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

type Sub = "single" | "bundles" | "welcome";

const SUBS: { id: Sub; label: string; icon: string; hint: string }[] = [
  { id: "single", label: "Single item", icon: "items", hint: "Give one item to a player on demand" },
  { id: "bundles", label: "Bundles", icon: "kits", hint: "Give a pre-built kit (a bundle of items)" },
  { id: "welcome", label: "New-player kit", icon: "flag", hint: "Auto-grant a kit to players on first join" },
];

export function GiveItemsTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  const [sub, setSub] = useState<Sub>("single");
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

      {sub === "single" && <ItemsTab setConsoleEntries={setConsoleEntries} />}
      {sub === "bundles" && <KitsTab setConsoleEntries={setConsoleEntries} />}
      {sub === "welcome" && <WelcomeTab setConsoleEntries={setConsoleEntries} />}
    </div>
  );
}
