// Clean stroke-based line icons (design phase 8) — replaces nav emoji.
// <Icon name="players" /> · inherits color via currentColor. Ported from the
// Claude Design handoff icons.jsx.

import type { CSSProperties, ReactNode } from "react";

const ICON_PATHS: Record<string, ReactNode> = {
  dashboard: <><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>,
  kits: <><path d="M3 8h18v3H3z" /><path d="M5 11v9h14v-9" /><path d="M12 8v12" /><path d="M12 8S10.5 3.5 8.5 3.5 6 6 8 8M12 8s1.5-4.5 3.5-4.5S18 6 16 8" /></>,
  broadcast: <><path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1Z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 6a8 8 0 0 1 0 12" /></>,
  players: <><circle cx="9" cy="8" r="3.2" /><path d="M3.5 19c.6-3 2.8-4.6 5.5-4.6S14 16 14.6 19" /><path d="M16 5.2a3 3 0 0 1 0 5.6" /><path d="M17.4 14.6c2 .5 3.4 1.9 3.9 4.4" /></>,
  items: <><path d="M12 2.5 21 7v10l-9 4.5L3 17V7Z" /><path d="M3 7l9 4.5L21 7" /><path d="M12 11.5V21.5" /></>,
  skills: <><path d="M12 3v4M12 17v4M3 12h4M17 12h4" /><path d="M12 8.5 13.4 11 12 13.5 10.6 11Z" /><circle cx="12" cy="12" r="0.6" fill="currentColor" /></>,
  vehicles: <><path d="M3 13l1.8-5a2 2 0 0 1 1.9-1.4h10.6A2 2 0 0 1 19.2 8L21 13" /><path d="M3 13h18v4a1 1 0 0 1-1 1h-1.5M3 13v4a1 1 0 0 0 1 1h1.5" /><circle cx="7" cy="18" r="1.8" /><circle cx="17" cy="18" r="1.8" /></>,
  movement: <><circle cx="12" cy="12" r="9" /><path d="M14.5 9.5 11 11l-1.5 3.5L13 13Z" /><path d="M12 3v2M12 19v2M3 12h2M19 12h2" /></>,
  maintenance: <><path d="M14.5 6.5a3.5 3.5 0 0 0-4.6 4.3l-6 6 1.8 1.8 6-6a3.5 3.5 0 0 0 4.3-4.6l-2 2-1.6-1.6Z" /><path d="m15 15 4 4" /></>,
  history: <><path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" /><path d="M3 4v4h4" /><path d="M12 8v4.5l3 2" /></>,
  refresh: <><path d="M20 11a8 8 0 1 0-1.8 6.3" /><path d="M20 5v5h-5" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-3.5-3.5" /></>,
  close: <><path d="m6 6 12 12M18 6 6 18" /></>,
  send: <><path d="M21 4 3 11l6 2.5L12 20l3-7Z" /><path d="m9 13.5 6-2.5" /></>,
  plus: <><path d="M12 5v14M5 12h14" /></>,
  target: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" /></>,
  logout: <><path d="M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3" /><path d="M16 8l4 4-4 4" /><path d="M20 12H9" /></>,
  check: <><path d="m5 12 4.5 4.5L19 7" /></>,
  alert: <><path d="M12 3 2.5 19h19Z" /><path d="M12 10v4M12 17h.01" /></>,
  clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  power: <><path d="M12 3v8" /><path d="M7 6.5a8 8 0 1 0 10 0" /></>,
  spice: <><path d="M12 2.5c3 4 5 6.5 5 10a5 5 0 0 1-10 0c0-3.5 2-6 5-10Z" /></>,
  map: <><path d="M9 3 3 5.5v15L9 18l6 3 6-2.5v-15L15 6 9 3Z" /><path d="M9 3v15M15 6v15" /></>,
  inventory: <><path d="M5 8h14l-1 11a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1L5 8Z" /><path d="M9 8V6a3 3 0 0 1 6 0v2" /><path d="M9.5 12h5" /></>,
  settings: <><circle cx="12" cy="12" r="3.2" /><path d="M12 2.5v2.6M12 18.9v2.6M21.5 12h-2.6M5.1 12H2.5M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8M18.7 18.7l-1.8-1.8M7.1 7.1 5.3 5.3" /></>,
  pin: <><path d="M12 21s7-6.3 7-11a7 7 0 0 0-14 0c0 4.7 7 11 7 11Z" /><circle cx="12" cy="10" r="2.5" /></>,
  coin: <><circle cx="12" cy="12" r="8" /><path d="M14.2 9.3a2.4 2.4 0 0 0-2.2-1.3c-1.4 0-2.4.8-2.4 1.9 0 1.2 1 1.7 2.4 2 1.5.3 2.6.9 2.6 2.1 0 1.2-1.1 2-2.6 2a2.5 2.5 0 0 1-2.3-1.3" /><path d="M12 6.5v1.2M12 16.3v1.2" /></>,
  flag: <><path d="M5 21V4" /><path d="M5 4h12l-2.2 3.4L17 11H5" /></>,
  user: <><circle cx="12" cy="8" r="3.5" /><path d="M5 20c.8-3.6 3.4-5.6 7-5.6s6.2 2 7 5.6" /></>,
  layers: <><path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z" /><path d="m3 12 9 4.5L21 12" /><path d="m3 16.5 9 4.5 9-4.5" /></>,
  terminal: <><rect x="3" y="4.5" width="18" height="15" rx="1.5" /><path d="m7 9 3 3-3 3M13 15h4" /></>,
  dice: <><rect x="3.5" y="3.5" width="17" height="17" rx="3" /><circle cx="8.5" cy="8.5" r="1.1" fill="currentColor" /><circle cx="15.5" cy="8.5" r="1.1" fill="currentColor" /><circle cx="12" cy="12" r="1.1" fill="currentColor" /><circle cx="8.5" cy="15.5" r="1.1" fill="currentColor" /><circle cx="15.5" cy="15.5" r="1.1" fill="currentColor" /></>,
  trend: <><path d="M3 17l5-5 3.5 3.5L20 7" /><path d="M15 7h5v5" /></>,
  shield: <><path d="M12 3 5 6v5c0 4.5 3 8 7 9.5 4-1.5 7-5 7-9.5V6Z" /></>,
  bolt: <><path d="M13 2 4 14h7l-1 8 9-12h-7Z" /></>,
  gauge: <><path d="M5 18a8 8 0 1 1 14 0" /><path d="M12 14l3.5-3.5" /><circle cx="12" cy="14" r="1.2" fill="currentColor" /></>,
};

export function Icon({ name, size = 20, stroke = 1.75, className = "", style }: {
  name: string;
  size?: number;
  stroke?: number;
  className?: string;
  style?: CSSProperties;
}) {
  const p = ICON_PATHS[name];
  if (!p) return null;
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={style}
      aria-hidden
    >
      {p}
    </svg>
  );
}
