// Live-map projection: world (Unreal cm) <-> image %, plus the Deep Desert
// 9x9 sector grid math. Deliberately React-free and dependency-free so the
// numbers below can be pinned by a test the repo already runs
// (scripts/test_map_projection.py compiles THIS file with the repo's own tsc
// and asserts against it — see issue #116).
//
// Unit: 1 world unit = 1 cm (Unreal default). This game's world Y grows
// SOUTHWARD and every map image is north-up, hence flipY on every map.

export interface MapCfg {
  key: string;
  label: string;
  image: string;
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
  flipX?: boolean;
  flipY?: boolean;
  /** Set when the shipped image is NOT a faithful full render of the bounds
   *  below, so a dot's position on it cannot be trusted. The UI says so rather
   *  than letting an operator read a precise-looking position off a wrong map. */
  uncalibrated?: string;
}

// Bounds + images lifted from dune-admin (MIT). Provenance differs per map —
// see each entry; do NOT assume they are all equally trustworthy.
export const MAPS: MapCfg[] = [
  // Hagga Basin — GAME-AUTHORITATIVE, same source as Deep Desert below:
  //   Survival_1: Min=(X=-457200.000 Y=-457200.006) Max=(X=355600.000 Y=355600.006)
  // Replaces a hand-fit that had been eyeballed over months by clicking
  // landmarks (-437871..350539 / -462011..376267). That fit was up to 24 000 uu
  // out and, tellingly, was not square while both the landscape box and the
  // 512x512 image are. th.gl's tile bounds for this map are
  // [[-457599,-457599],[355199,355199]] — 0.05% of span from the server's own
  // number, so image extent and world box agree. Expect live dots to shift by
  // ~2-3% versus the old build; that shift is the correction.
  { key: "HaggaBasin", label: "Hagga Basin", image: "hagga-basin.webp", minX: -457200, maxX: 355600, minY: -457200, maxY: 355600, flipY: true },

  // Deep Desert — GAME-AUTHORITATIVE, not fitted. The UE5 dedicated server
  // prints its own world box at every boot, and it is identical across all
  // four DD dimensions and every build we have logs for:
  //
  //   $ grep -a "Setting partition definition" logs/ue5-DeepDesert_1-*.log
  //   LogDuneWorldPartitioner: Log: Setting partition definition to Map:
  //   /Game/Dune/Maps/Arrakis/DeepDesert_1/DeepDesert_1.DeepDesert_1,
  //   Box2D array: bIsValid=true, Min=(X=-1270000.000 Y=-1270000.000),
  //   Max=(X=1168400.000 Y=1168400.000), Label: Deep Desert (db id: 8, dim.: 0)
  //
  // (verified on Dreamworld build/revision 1973075). Span 2 438 400 uu =
  // 24.384 km square = 8128 landscape quads at 300 uu/quad, exactly 3x
  // Survival_1's 8128 quads at 100 uu/quad and sharing its -50 800 centre —
  // derived geometry, not padding. One sector = 2 438 400 / 9 = 270 933.33 uu
  // (2.709 km); 9x9 sectors = 594.6 km^2, ~3% above the 576 km^2 the community
  // commonly cites — corroboration-shaped, but not a match, so it is the quad
  // geometry and th.gl below that carry the argument, not this figure.
  //
  // Corroborated by cdn.th.gl/dune-awakening/config/tiles.json, which declares
  // deepdesert_1 bounds [[-1270399,-1270399],[1167999,1167999]] and publishes
  // the very image we ship (web/public/deepdesert.webp is byte-identical to
  // its z=0 tile, md5 9de6994df41e6aa397aad64adf58f5d0) — 0.016% of span from
  // the server's own number, i.e. the same box.
  //
  // Cross-checked against the two live observations in issue #116 (@iamc0ke),
  // each read off the in-game map — see scripts/test_map_projection.py:
  //   (516429, -1009962) -> I7   [the old -1300000/1200000 guess said H7]
  //   (1127612, 1077779) -> A9
  // Residual assumption, stated plainly: that the player-facing 9x9 grid spans
  // exactly this landscape box. The first point sits only ~0.04 sector (~109 m)
  // from the I/H line, so that assumption is what a third ground-truth point
  // near a row boundary would test.
  { key: "DeepDesert", label: "Deep Desert", image: "deepdesert.webp", minX: -1270000, maxX: 1168400, minY: -1270000, maxY: 1168400, flipY: true },

  // Arrakeen / Harko Village: inherited from dune-admin, NEVER validated
  // against a live coordinate, and demonstrably wrong. The SAME grep quoted
  // above yields an authoritative box for these two as well — one distinct
  // value each, across every log we have:
  //
  //   SH_Arrakeen:     Min=(X=-32765.000 Y=-21256.000) Max=(X=27235.000  Y=18744.000)
  //   SH_HarkoVillage: Min=(X=-99855.015 Y=-78117.655) Max=(X=100144.985 Y=121882.345)
  //
  // versus the inherited guesses below: Arrakeen's maxX is off by 10 235 and
  // its Y range is less than half the real one; Harko is off by an order of
  // magnitude on both axes. Note also that the game's boxes for Survival_1,
  // DeepDesert_1 and SH_HarkoVillage are square, while these two entries are
  // not — a non-square entry in this table is itself a smell.
  //
  // BUT the bounds are not the blocker for these two — THE IMAGES ARE. Both
  // shipped assets are truncated crops, not full renders of any box:
  //   arrakeen.webp: content fills x[0,511] but only y[0,295] of 512 (57.8%),
  //     pure black below, and buildings are sliced mid-shape at the cut —
  //     the bottom of the map is simply missing, not empty desert.
  //   harko.webp:    content is a 319x320 block anchored at the canvas's
  //     top-left; the remaining 38% on each axis is pure black.
  // Neither content rect matches its world box's aspect (Arrakeen art is 1.73,
  // its box 1.50) nor th.gl's tile layout for these maps, so there is no
  // rectangle to map the world box onto. Swapping the numbers in would move
  // every dot to a *different* wrong place while looking authoritative, which
  // is worse than the honest state. Upstream no longer ships these assets, so
  // there is nothing to re-pull.
  //
  // Left projecting as before and flagged `uncalibrated` so the UI says a dot
  // here cannot be trusted. To finish the job someone needs a full-extent
  // top-down render of each map; then the boxes above drop straight in and the
  // flag comes off. Neither map draws a sector grid, so #116 does not depend
  // on this. Tracked, not hidden.
  { key: "Arrakeen", label: "Arrakeen", image: "arrakeen.webp", minX: -32000, maxX: 17000, minY: -10000, maxY: 9500, flipY: true,
    uncalibrated: "the shipped map image is cut off below 58% of its height, so dot positions on it are approximate" },
  { key: "HarkoVillage", label: "Harko Village", image: "harko.webp", minX: -5000, maxX: 14500, minY: -5500, maxY: 32000, flipY: true,
    uncalibrated: "the shipped map image covers only the top-left 62% of its canvas, so dot positions on it are approximate" },
];

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

export interface Projected {
  left: number;   // % across the image, 0 = west edge
  top: number;    // % down the image, 0 = north edge
  inBounds: boolean; // false = the raw coord fell outside minX..maxX / minY..maxY
                     // and left/top were clamped to the edge. The label the grid
                     // then reports is an edge sector, NOT a real reading.
}

// World coords -> position on the map image. Returns null for non-finite input
// so a NaN/Infinity coordinate is skipped rather than painted at the image's
// top-left corner (a bare `left: NaN%` is dropped by the CSSOM and falls back
// to 0px, which looks exactly like a genuine north-west marker).
export function worldToPct(x: number, y: number, cfg: MapCfg): Projected | null {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const normX = (x - cfg.minX) / (cfg.maxX - cfg.minX);
  const normY = (y - cfg.minY) / (cfg.maxY - cfg.minY);
  if (!Number.isFinite(normX) || !Number.isFinite(normY)) return null; // degenerate cfg
  const inBounds = normX >= 0 && normX <= 1 && normY >= 0 && normY <= 1;
  const fracX = clamp01(cfg.flipX ? 1 - normX : normX);
  const fracYup = clamp01(cfg.flipY ? 1 - normY : normY);
  return { left: fracX * 100, top: (1 - fracYup) * 100, inBounds };
}

// Just the fields we need off a DOMRect — keeps this module DOM-free.
export interface RectLike {
  left: number;
  top: number;
  width: number;
  height: number;
}

// Click position -> world coords. The exact inverse of worldToPct for any
// in-bounds point. Returns null instead of guessing when:
//   - the element has no layout yet (an <img> that hasn't decoded reports
//     height 0, which used to divide by zero and silently yield maxY), or
//   - the click landed outside the image (the pointer handler sits on the
//     larger viewport, and clamping turned every mis-click into a corner).
export function pctToWorld(clientX: number, clientY: number, rect: RectLike, cfg: MapCfg): { x: number; y: number } | null {
  if (!(rect.width > 0) || !(rect.height > 0)) return null;
  const rx = (clientX - rect.left) / rect.width;
  const ry = (clientY - rect.top) / rect.height;
  if (!(rx >= 0 && rx <= 1 && ry >= 0 && ry <= 1)) return null;
  const fracYup = 1 - ry;
  const rawX = cfg.flipX ? 1 - rx : rx;
  const rawY = cfg.flipY ? 1 - fracYup : fracYup;
  return {
    x: Math.round(rawX * (cfg.maxX - cfg.minX) + cfg.minX),
    y: Math.round(rawY * (cfg.maxY - cfg.minY) + cfg.minY),
  };
}

// ---- Deep Desert 9x9 sector grid --------------------------------------
// I..A top to bottom (I = north, A = the southern arrival row), 1..9 left to
// right. Each sector is split into a 4x4 sub-grid (subx 1-4, suby 0-3) —
// matches how the ported DST POI data is tagged (DST WickMaps.tsx).
export const DD_ROWS = ["I", "H", "G", "F", "E", "D", "C", "B", "A"];
export const DD_N = 9;

// floor() of a percentage into one of DD_N cells. The epsilon is not cosmetic —
// the round-trip through percent is what loses the bit. In IEEE754:
//   ((1/3)*100)/100*9 === 2.999999999999999   (floor -> 2, want 3)
//   ((2/3)*100)/100*9 === 5.999999999999998   (floor -> 5, want 6)
// so a point sitting exactly on the 3rd or 6th grid line used to be labelled one
// cell west (or north) of the line the SVG actually draws. Note (1/3)*9 alone is
// exactly 3 — it is the *100 /100 detour that introduces the error, so do not
// "simplify" this away. 1e-9 of a cell is ~0.0003 uu.
function cellIndex(pct: number): number {
  return Math.floor((pct / 100) * DD_N + 1e-9);
}

// Which sector a point at (left%, top%) falls in — e.g. "D4" — or null if it is
// outside the grid. Same row order the grid draws, so a live player dot and
// this label always agree. Used by the Live Map's calibration readout (#116).
export function sectorForPct(left: number, top: number): string | null {
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
  if (left < 0 || left > 100 || top < 0 || top > 100) return null;
  const col = Math.min(DD_N, Math.max(1, cellIndex(left) + 1));
  const ri = Math.min(DD_N - 1, Math.max(0, cellIndex(top)));
  return `${DD_ROWS[ri]}${col}`;
}

// Convenience for tests / tooling: world coords straight to a sector label,
// or null when the point is off this map (no clamping, unlike the UI path).
export function sectorForWorld(x: number, y: number, cfg: MapCfg): string | null {
  const p = worldToPct(x, y, cfg);
  if (!p || !p.inBounds) return null;
  return sectorForPct(p.left, p.top);
}
