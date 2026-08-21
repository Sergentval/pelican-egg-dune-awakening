// Deep Desert sector grid + POI overlay (DST "Wick Maps" port). The Deep
// Desert cycles through 12 fixed layouts (Coriolis seed 0-11); the backend
// detects the active seed and hands us the matching pre-collected POIs.
// We draw the 9x9 sector grid (A-I × 1-9) ourselves and plot the POIs on
// the same %-coordinate space the live player dots use, so no terrain
// image is needed. POI data + icons ported from coastal-ms/DST-DuneServerTool
// (Apache-2.0). Sits inside MapTab's scaled/panned inner div.

import type { WickLayout, WickPoi } from "./api";

// I..A top to bottom, 1..9 left to right, each sector split 4x4 (subx 1-4,
// suby 0-3) — matches how the source data is tagged (DST WickMaps.tsx).
const ROWS = ["I", "H", "G", "F", "E", "D", "C", "B", "A"];
const N = 9;

const ICON: Record<string, string> = {
  wreck: "/wickmaps/wreck.svg",
  cave: "/wickmaps/cave.svg",
  titanium: "/wickmaps/titanium.svg",
  stravidium: "/wickmaps/stravidium.svg",
  "testing-station": "/wickmaps/testing-station.svg",
  "taxi-service": "/wickmaps/taxi-service.png",
  "large-spice-field": "/wickmaps/large-spice-field.svg",
};

// Which 9x9 sector a point at (left%, top%) falls in — e.g. "D4" — or null if
// it's outside the grid. Same row order the grid draws (I..A top→bottom, cols
// 1..9 left→right), so a live player dot and this label always agree. Used by
// the Live Map's calibration readout (issue #116).
export function sectorForPct(left: number, top: number): string | null {
  if (left < 0 || left > 100 || top < 0 || top > 100) return null;
  const col = Math.min(N, Math.max(1, Math.floor((left / 100) * N) + 1));
  const ri = Math.min(N - 1, Math.max(0, Math.floor((top / 100) * N)));
  return `${ROWS[ri]}${col}`;
}

// POI centre in % of the map image, mirroring DST's pixel math.
function poiPct(p: WickPoi): { left: number; top: number } | null {
  const row = p.sector[0];
  const col = Number(p.sector.slice(1));
  const ri = ROWS.indexOf(row);
  if (ri < 0 || !Number.isFinite(col) || col < 1 || col > N) return null;
  const sx = Math.min(4, Math.max(1, p.subx));
  const sy = Math.min(3, Math.max(0, p.suby));
  const left = ((col - 1) + (sx - 0.5) / 4) / N * 100;
  const top = (ri + (sy + 0.5) / 4) / N * 100;
  return { left, top };
}

export function DeepDesertGrid({ layout, spiceSectors, zoom }: {
  layout: WickLayout | null;
  spiceSectors: string[];
  zoom: number;
}) {
  const lines = Array.from({ length: N + 1 }, (_, i) => (i / N) * 100);
  const cells = Array.from({ length: N }, (_, i) => i);
  const iconPx = 20 / zoom;

  return (
    <div className="absolute inset-0 pointer-events-none">
      {/* highlight large-spice sectors */}
      {spiceSectors.map((s) => {
        const row = s[0];
        const col = Number(s.slice(1));
        const ri = ROWS.indexOf(row);
        if (ri < 0 || !Number.isFinite(col)) return null;
        return (
          <div key={`sp-${s}`} className="absolute bg-amber-400/10 border border-amber-400/25"
            style={{ left: `${((col - 1) / N) * 100}%`, top: `${(ri / N) * 100}%`,
                     width: `${100 / N}%`, height: `${100 / N}%` }} />
        );
      })}

      {/* grid lines */}
      <svg className="absolute inset-0 w-full h-full" preserveAspectRatio="none" viewBox="0 0 100 100">
        {lines.map((v, i) => (
          <g key={i}>
            <line x1={v} y1={0} x2={v} y2={100} stroke="rgba(0,0,0,0.35)" strokeWidth={0.15} />
            <line x1={0} y1={v} x2={100} y2={v} stroke="rgba(0,0,0,0.35)" strokeWidth={0.15} />
          </g>
        ))}
      </svg>

      {/* sector labels (top edge cols, left edge rows) */}
      {cells.map((i) => (
        <div key={`cl-${i}`} className="absolute font-mono text-black/55 -translate-x-1/2"
          style={{ left: `${((i + 0.5) / N) * 100}%`, top: 1, fontSize: 9 / zoom }}>
          {i + 1}
        </div>
      ))}
      {cells.map((i) => (
        <div key={`rl-${i}`} className="absolute font-mono text-black/55 -translate-y-1/2"
          style={{ top: `${((i + 0.5) / N) * 100}%`, left: 1, fontSize: 9 / zoom }}>
          {ROWS[i]}
        </div>
      ))}

      {/* POIs */}
      {(layout?.pois ?? []).map((p, idx) => {
        const pos = poiPct(p);
        if (!pos) return null;
        const src = ICON[p.type];
        return (
          <div key={`poi-${idx}`}
            className="absolute -translate-x-1/2 -translate-y-1/2"
            style={{ left: `${pos.left}%`, top: `${pos.top}%` }}
            title={`${p.type} · ${p.sector}`}>
            {src
              ? <img src={src} alt={p.type} draggable={false}
                  style={{ width: iconPx, height: iconPx }}
                  className="select-none drop-shadow-[0_1px_1px_rgba(0,0,0,0.8)]" />
              : <span className="block rounded-full bg-fuchsia-400 border border-black/60"
                  style={{ width: iconPx * 0.5, height: iconPx * 0.5 }} />}
          </div>
        );
      })}
    </div>
  );
}
