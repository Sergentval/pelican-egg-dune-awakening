// ⌘K command palette (design phase 3) — instant jump-to-section. Opens on
// ⌘K / Ctrl+K or the top-bar search button. Recent-first when empty, fuzzy by
// label+group when typing; ↑/↓ to move, Enter to go, Esc to close.

import { useEffect, useMemo, useRef, useState } from "react";

export function SearchIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  );
}

export interface CmdItem {
  id: string;
  label: string;
  group: string;
  icon?: string;
}

// negative = no match; lower = better
function score(item: CmdItem, q: string): number {
  const label = item.label.toLowerCase();
  const group = item.group.toLowerCase();
  if (label.startsWith(q)) return 0;
  const li = label.indexOf(q);
  if (li >= 0) return 1 + li * 0.01;
  const gi = (label + " " + group).indexOf(q);
  if (gi >= 0) return 5 + gi * 0.01;
  return -1;
}

export function CommandPalette({
  open, onClose, items, groupLabels, recent, currentTab, onGo,
}: {
  open: boolean;
  onClose: () => void;
  items: CmdItem[];
  groupLabels: Record<string, string>;
  recent: string[];
  currentTab: string;
  onGo: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setSel(0);
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  const results = useMemo(() => {
    const query = q.trim().toLowerCase();
    if (!query) {
      const recentItems = recent
        .map((id) => items.find((i) => i.id === id))
        .filter((x): x is CmdItem => !!x);
      const rest = items.filter((i) => !recent.includes(i.id));
      return [...recentItems, ...rest].slice(0, 8);
    }
    return items
      .map((i) => ({ i, s: score(i, query) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => a.s - b.s)
      .map((x) => x.i)
      .slice(0, 8);
  }, [q, items, recent]);

  useEffect(() => {
    if (sel >= results.length) setSel(0);
  }, [results.length, sel]);

  if (!open) return null;

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(results.length - 1, s + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); const r = results[sel]; if (r) onGo(r.id); }
    else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  }

  return (
    <div className="cmdk-scrim" onClick={onClose}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <div className="cmdk-input-row">
          <SearchIcon size={18} />
          <input
            ref={inputRef}
            className="cmdk-input"
            placeholder="Jump to a section…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <span className="cmdk-esc">esc</span>
        </div>
        <div className="cmdk-list">
          {results.length === 0 && <div className="cmdk-empty">No section matches “{q}”.</div>}
          {results.map((r, idx) => (
            <button
              key={r.id}
              className={"cmdk-row" + (idx === sel ? " is-sel" : "")}
              onMouseEnter={() => setSel(idx)}
              onClick={() => onGo(r.id)}
            >
              <span className="cmdk-ico" aria-hidden>{r.icon}</span>
              <span className="cmdk-label">{r.label}</span>
              {r.id === currentTab && <span className="cmdk-current-tag">current</span>}
              <span className="cmdk-group">{groupLabels[r.group] ?? r.group}</span>
            </button>
          ))}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
