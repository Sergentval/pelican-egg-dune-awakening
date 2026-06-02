# Spec: Live Player Map + Inventory Viewer

Status: PROPOSED (planning). Phased; no phase exceeds 5 files. Reuses existing
backend wherever possible. Reference code/assets lifted from Icehunter/dune-admin
(MIT) - add to ATTRIBUTION.md.

## Goals (from user)

1. A web **map interface** showing live player positions, mapgenie.io-style
   (pan/zoom over a real top-down Hagga/Deep Desert map).
2. A **TP shortcut** to teleport a player to a safe outpost in Hagga Basin
   (plus other saved locations, operator-defined).
3. **View a player's inventory** and **delete specific items**.

## What already exists (reuse - do NOT rebuild)

Verified live on server 30 (2026-06-02):

- **Positions:** `dune.actors.transform` is a composite; `((transform).location).x/y/z`,
  plus `map` (text, value is literally `HaggaBasin` / `Arrakeen` / `HarkoVillage` /
  `DeepDesert`) and `partition_id`. Sample Hagga pawn returned `(101221, 279328, 2628)`.
  `dune.player_state.player_pawn_id = actors.id` links a pawn to a player.
- **Single-player position:** `GET /api/pos/<player>` -> `admin-publish.sh pos`.
- **Teleport:** `tpsafe` (snap to safe) + `teleport` (exact), via RMQ TeleportTo /
  TeleportToExact. CLI + `POST /admin/tpsafe` / `POST /admin/teleport`
  (admin-http build_argv line 315). **This is the TP-shortcut primitive.**
- **Inventory read:** `GET /api/players/<id>/inventory` -> `inventory-list` returns
  `item_id, template_id, stack_size, quality, durability, max_durability, slot`,
  keyed on the player-character pawn (`dune_pc_actor_id`). Works offline.
- **Item delete:** `POST /api/items/<item_id>/delete` -> `item-delete` (offline-gated,
  existence-checked). **The inventory viewer's delete button is already wired.**
- **Item names:** `data/admin/item-data.json` (1658 items, name lookup), loadable via
  `admin_market.load_catalog`.

So: **inventory viewer + delete is backend-complete (frontend-only)**, and the map
reuses positions + teleport + needs only a batch endpoint + a locations store + UI.

## Reference to lift (Icehunter/dune-admin, MIT)

- `cmd/dune-admin/db.go::cmdFetchMapMarkers` - the exact batch position SQL
  (players via `actors`+`player_state`; vehicles via vehicle-class actors).
- `cmd/dune-admin/location_store.go` - SQLite `map_locations(name,x,y,z,sort,...)`
  with seed-if-empty (mirrors our welcome-ledger pattern).
- `web/src/tabs/LiveMapTab.tsx` - Leaflet `CRS.Simple` + `ImageOverlay` + per-map
  world-bound projection + markers.
- `web/src/tabs/PlayersTab/modals/MapCoordPickerModal.tsx` - click-map-to-pick coords.
- **Map assets** `web/public/{hagga-basin.webp, hagga-basin.png, deepdesert.webp,
  arrakeen.webp, harko.webp, map-icons.webp}` + `map-data/*-spawns.json`.
- **World-bound constants** (world units -> image), already empirically derived:
  - `HaggaBasin`: minX -437871, maxX 350539, minY -462011, maxY 376267, flipY
  - `DeepDesert`: minX -1300000, maxX 1200000, minY -1300000, maxY 1200000
  - `Arrakeen`: minX -32000, maxX 17000, minY -10000, maxY 9500, flipY
  - `HarkoVillage`: minX -5000, maxX 14500, minY -5500, maxY 32000
  (our live Hagga sample `101221,279328` lands inside the Hagga box - validates.)

mapgenie note: mapgenie's tiles/POI DB are copyrighted and cannot be scraped. We can
deliver a mapgenie-*style* experience (pan/zoom real top-down map + live markers +
click-to-TP) using dune-admin's MIT map image. A literal mapgenie clone (their tiles)
is out; higher-res tiles would be a separate asset-sourcing effort.

---

## Phase 1 - Inventory viewer + delete (frontend-only; ship first)

Backend is done. Smallest change, immediate value, and a feature neither DST nor
ddsm exposes (puts us ahead).

Files (3):
- `scripts/admin-http.py` - in `_handle_player_inventory`, enrich each row with
  `name` from `admin_market.load_catalog` (mirror the `char-xp` enrichment at
  line ~1349). No new endpoint.
- `web/src/api.ts` - `fetchInventory(id)` (`GET /api/players/<id>/inventory`),
  `deleteItem(itemId)` (`POST /api/items/<id>/delete`); typed `InventoryItem`.
- `web/src/tabs.tsx` - in `PlayersTab`, an inventory panel/modal per selected player:
  table of name / qty / quality / durability / slot, with a per-row Delete button.
  Delete is destructive + offline-gated: when the player is online, render items
  read-only with a "player must be offline to delete" note; on success, refetch.

Verify: `tsc --noEmit` + `vite build`; live-check on the dev char (inventory-list
already returns its items). No new Python tests (enrichment is thin); optionally a
catalog-name-join unit check.

## Phase 2 - Batch map-markers endpoint (backend)

Files (4):
- `scripts/admin_map.py` (new) - pure helpers: `MAP_KEYS`, `validate_map_key`,
  and a `parse_markers(csv)` -> list of `{id,name,kind,online,partition,fls,x,y,z}`.
- `scripts/admin-publish.sh` - new `map-markers <map>` subcommand: lift dune-admin's
  SQL (players: `actors` JOIN `player_state` JOIN `accounts`; vehicles: vehicle-class
  `actors` with `transform`), filtered `WHERE map = :'map'`, CSV out. Read-only.
- `scripts/admin-http.py` - `GET /api/map/markers?map=HaggaBasin` -> `run_publish`,
  validate map key (400 on bad input), 200 with markers.
- `scripts/test_admin_map.py` (new) - map-key validation + `parse_markers` table parse.

Verify: full `unittest` suite green.

## Phase 3 - Locations store + TP shortcuts (backend)

Files (4):
- `data/admin/map-locations.json` (new) - seed catalog. Seed a **"Hagga safe outpost"**
  entry (coords captured via the Phase 4 picker, or a known landmark; see caveat) plus
  a couple of obvious hubs. Shape `{map,name,x,y,z,sort}`.
- `scripts/admin_locations.py` (new) - load/list/upsert/remove against the JSON (or a
  SQLite store mirroring `admin_welcome.py` if we want concurrency safety).
- `scripts/admin-publish.sh` - `tp-to-location <player> <location-name>`: resolve the
  location -> call the existing `tpsafe` path with its x/y/z. (No new TP logic.)
- `scripts/admin-http.py` - `GET/POST /api/map/locations` (list/add/remove) +
  `POST /api/map/teleport` `{player, location}` -> `tp-to-location`.

Caveat: there is no outpost actor in `dune.actors` to auto-derive Hagga coords from
(confirmed: 0 outpost/spawn/landmark classes), so the exact "safe outpost" coordinate
is captured via the coord-picker (Phase 4) or seeded from known community coords. The
`map` key for Hagga is `HaggaBasin`.

## Phase 4 - Map tab frontend

Files (4-5):
- `web/public/` - add `hagga-basin.webp` (+ `deepdesert.webp`, etc. as maps come
  online), lifted from dune-admin (MIT, attribute).
- `web/src/MapTab.tsx` (new) - Leaflet `CRS.Simple` + `ImageOverlay` (lift the bounds
  constants + world<->pixel projection), poll `/api/map/markers?map=` every few
  seconds, render player markers (+ vehicles later). Click a marker -> action menu
  reusing existing teleport endpoints. A **Locations sidebar** lists saved locations
  with a one-click "TP selected player here" (the Hagga-safe-outpost shortcut) and a
  **coord-picker** (click map -> save a new location, e.g. capture "Hagga safe outpost").
- `web/src/api.ts` - `fetchMapMarkers(map)`, `fetchLocations()`, `saveLocation()`,
  `teleportToLocation(player, name)`.
- `web/src/App.tsx` - register the Map tab.
- Dependency: add `react-leaflet` + `leaflet` (matches dune-admin; gives pan/zoom for
  free). Alternative: hand-rolled `<canvas>` (no dep, more code) - recommend
  react-leaflet unless bundle size is a concern.

Verify: `tsc --noEmit` + `vite build`; live-check markers move as the dev char moves;
TP-to-Hagga-outpost shortcut teleports the char.

## Phase 5 - Polish / vehicles / PR

- Vehicle markers (vehicle-class actors with `transform`), marker filters (players /
  vehicles / locations), online-only toggle.
- ATTRIBUTION.md: dune-admin (Icehunter, MIT) for map assets + LiveMap/location code.
- Memory + wiki update; PR + merge.

## Risks / caveats

- **Asset licensing:** dune-admin assets are MIT - lift with attribution (do NOT use
  mapgenie tiles). Cleanest legal path.
- **Offline-gated delete:** inventory delete needs the player offline; the UI must
  surface this (read-only items + note while online).
- **Map keys / DeepDesert:** only HaggaBasin/Arrakeen/HarkoVillage exist on the dev
  server now; DeepDesert markers need DD spun up to test.
- **react-leaflet bundle weight:** acceptable; flag if the SPA must stay tiny.
- **Poll load:** `map-markers` is one batched query per poll; keep cadence tunable to
  bound `dune.actors` load (dune-admin polls every few seconds).
- **Coordinate bounds** are dune-admin's empirical values; re-confirm against live
  pawn samples per map before trusting click-to-coord precision.

## Recommended order

Phase 1 (inventory viewer - backend done, fast, unique) -> Phase 2 (markers) ->
Phase 3 (locations + Hagga TP) -> Phase 4 (map UI) -> Phase 5 (polish/PR).
