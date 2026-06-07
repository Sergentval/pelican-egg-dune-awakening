// Overview home (IA phase 5) — the read-only landing page. Combines the
// fleet-health summary (StatusTab) with the online-player roster (Dashboard),
// both rendered unchanged so all their info is preserved. This replaces the
// separate Dashboard and Status tabs.

import { type Dispatch, type SetStateAction } from "react";
import type { ConsoleEntry } from "./components";
import { Dashboard, StatusTab } from "./tabs";

type SetEntries = Dispatch<SetStateAction<ConsoleEntry[]>>;

export function OverviewTab({ setConsoleEntries }: { setConsoleEntries: SetEntries }) {
  return (
    <div className="space-y-6">
      <StatusTab setConsoleEntries={setConsoleEntries} />
      <Dashboard setConsoleEntries={setConsoleEntries} />
    </div>
  );
}
