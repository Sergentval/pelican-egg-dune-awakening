# Changelog

All notable changes to the Pelican egg for Dune: Awakening.

Dated sections, newest first. Entries reference the pull request (`#NN`) or
commit that shipped them. Sections up to and including 2026-08-20 were
reconstructed retroactively from the git and PR history on 2026-08-20; from
here on the log is maintained with each merge.

**How to pick up changes on an existing server** — two different levers:

- Changes to *scripts, panel, or runtime* → **Reinstall** the server in
  Pelican (world data under `server/state/` survives; `data/admin/` daemon
  configs are preserved).
- Changes to *egg variables or their validation rules* → **re-import the
  egg JSON** into the panel, then Reinstall. An imported egg is a copy; the
  panel never picks up new variables on its own.

## 2026-09-25 — The update's new story maps can start (Arrakeen Spaceport no longer hangs)

**Reinstall** to pick it up. The missing rows are added on the next boot.

- **"Connecting to Arrakeen Spaceport" never ended.** The September update added five story maps:

  | Partition | Map |
  |---|---|
  | 31 | `CB_Story_DestroyedZanovar` |
  | 32 | `CB_Story_OrbitalMonitor`, the Arrakeen Spaceport |
  | 33 | `CB_Arrakis_Story_Paranoid_PrayerRoom` |
  | 34 | `CB_Arrakis_Story_Glutton_DiningRoom` |
  | 35 | `CB_Arrakis_Generic_Sietch_Room` |

  A map cannot start without its `dune.world_partition` row, and `prestart.sh` seeds those rows from a hand-kept list that stopped at 30. Players could not progress the story past the Spaceport. The cause was found by a CubeCoders forum user (jonokeys) and passed on by @iamc0ke in #136.
- **Ids are Funcom's, not ours.** They are exactly the ids in `world-template.yaml`'s `worldPartitions`, which is also where mock-k8s reads each map's partition.
- **A silent conflict is now loud.** The seed is `ON CONFLICT DO NOTHING`. If a Funcom DB upgrade already put another map on one of these ids (`CB_SurvivalChallenge_Station_15` has been seen on 31), the map we meant to seed got no row and nothing said so. After seeding, `prestart.sh` now warns for every seeded map that has no row, and names the map holding its id: `world_partition: no row for <map> (template id N), id held by <other>`.
- **Verified on our test server.** All five rows were created on boot. A travel request for `CB_Story_OrbitalMonitor` started it on partition 32, and the Director saw it `ready:true` 85 s later.

## 2026-09-24 — The Deep Desert reshapes when the Coriolis cycle ends, not at the next restart

Issue #119. **Reinstall** to pick it up.

- **The storm timer hit zero and nothing happened until a restart.** The game
  applies a new Coriolis cycle only when a server **boots**. The seed and cycle
  dates are read at startup, and the map wipe runs in the database function
  `coriolis_update_seed`, which each server calls for its own map. Our test
  server's log of the 2026-06-02 cycle shows it: the storm ran all night,
  05:00 UTC passed with the Deep Desert up and nothing logged, and only the
  07:32 boot printed

  ```
  LogCoriolis: Display: Current Coriolis World Seed: 3
  LogCoriolis: Display: This Coriolis Cycle start date UTC: 2026.06.02-05.00.00
  ```

  On Funcom's side, the battlegroup operator restarts servers on a schedule
  (`restartSchedule`). mock-k8s replaces that operator, so nothing restarted
  the Deep Desert.
- **mock-k8s now restarts only the Deep Desert, every dimension of it, two
  minutes after the boundary.** The new `internal/coriolis` watcher reads the boundary from each
  instance's own boot line (`Next Coriolis Cycle start date UTC: …`), so it
  follows whatever cycle the game computes rather than a hard-coded "Tuesday
  05:00". It then recycles that instance: SIGTERM, which saves its state, and
  a fresh boot that applies the new cycle.
  - Every dimension is covered. Dimension 0 is a mock-k8s instance and is
    recycled in place. Dimensions 1..N (`DUNE_DD_DIMENSIONS`, default 3) are
    started outside mock-k8s by `spawn-dimension.sh`, so they are restarted
    through the same `admin-publish dimension-down` / `dimension-up` verbs the
    sietch controls use. A parked or downed dimension has no pidfile and is
    never touched.
  - Hagga, Arrakeen and the dungeons stay up.
  - It does not need the Pelican API variables the ⏰ scheduled restart does.
- **What it will never do.**
  - Start the replacement while the old server is still shutting down. The
    map is held "draining" until the process has exited, because two servers
    on one partition is the IGW index collision of the 1.5 crash loop.
  - Restart an instance that is still booting. Its log may still end with the
    previous boot's line.
  - Restart twice for the same boundary.
  - Restart several Deep Desert instances at once. The next one waits for the
    previous one to be back (bounded at 15 min).
  - Keep retrying a server that refuses to stop: it stays tracked, nothing
    starts beside it, and the next try comes 30 min later.
- **Stopping a server now waits for the server, not its launcher.** Found
  while testing this live: every UE5 pidfile holds the
  `sh DuneSandboxServer.sh` wrapper, which dies on SIGTERM at once, while the
  UE5 binary beside it enters PreShutdown and can stay there. A Deep Desert
  did for over ten minutes, still in the farm on partition 8.
  - `proc.Terminate` waited on the wrapper only. It returned at once, freed the
    port slot, never sent the SIGKILL, and left that server running untracked:
    the next Deep Desert start would have collided with it.
  - `admin-publish dimension-down` had the same wait, and so did the DD
    autoscaler and the sietch controls that call it.
  - Both now wait for the whole process group and SIGKILL it once the grace is
    spent. mock-k8s's grace goes from 15 s to 120 s, the
    `terminationGracePeriodSeconds` in Funcom's own world template; before
    this, the 15 s never actually applied to UE5.
  - A Director scale-down now also holds the map as draining until the old
    process is gone, so a quick scale-down/scale-up cannot put two servers on
    one partition.
- **Settings.**
  - `MOCK_K8S_CORIOLIS_INTERVAL`: poll interval, default `1m`; `off`
    disables the watcher.
  - `MOCK_K8S_CORIOLIS_DELAY`: delay after the boundary, default `2m`.
  - `/status` gains `instances.recycledTotal`.

## 2026-09-21 — Instances a player asked for are given back when nobody is on them

- **The watcher could only ever fill the budget.** Shipped that morning, it
  starts a map when a player asks to travel there — and nothing ever stopped one
  again. `MaxConcurrentInstances` is a budget over *every* tracked instance,
  always-warm maps included. The reporter keeps **six** maps warm against a
  budget of **eight**, so two on-demand slots existed on a world with thirty
  instanced maps. The first two dungeons anyone visited took them and never gave
  them back:

  ```
  traveldemand: at the instance cap, refusing to start a map a player asked for
    map=Story_ArtOfKanly live=8 max=8
  [16:16:50] group CB_Dungeon_OldCarthag (servers: [], num: 0)
  [16:16:50] group CB_Story_Hephaestus   (servers: [], num: 0)
  ```

  His players could enter one dungeon and no other, until the next restart.
- **`AutomaticStopDuration` is finally applied.** It has been in `ondemand.ini`
  since the file existed — parsed, logged at boot, and used by nobody, exactly
  like `MaxConcurrentInstances` before it. The new reaper in
  `internal/traveldemand` stops any on-demand map that has sat empty for that
  long (default 10m), freeing the slot. Poll interval
  `MOCK_K8S_REAP_INTERVAL` (default 30s, `off` disables).
- **What it will never do.** Touch an always-warm map — those are the operator's
  floor. Reap on an unreadable player count: a failed query means *unknown*, not
  *empty*, and reading it as empty would stop every instance on the server at
  once. Or evict a group: a player arriving resets the countdown, which then
  restarts from when they leave.
- **The occupancy signal is the one the autoscaler already trusts.** The new
  `internal/occupancy` runs `admin-publish farm-player-count`, which sums
  `dune.farm_state.connected_players` per map. Not `dune.actors` — that groups a
  character by their *persistent home map*, so a player visiting Arrakeen counts
  under the map they came from, and a drain guard reading it sees the hub as
  empty and evicts the visitor. That lesson was paid for once; it is not being
  paid for again.
- **The budget is stated out loud at boot.** Six warm maps against a budget of
  eight was unworkable before the watcher existed, silently. mock-k8s now logs
  `instance budget on_demand_slots=N`, and warns when N is two or fewer — or
  zero, which means no player can travel anywhere at all.
- ⚠️ **`ondemand.ini` is not hand-editable.** `apply-config.sh` regenerates it
  from the admin settings store on every boot, so an edit to the file is gone
  at the next restart. `MaxConcurrentInstances`, `AutomaticStopDuration` and
  `AlwaysWarmMaps` are all exposed in the admin panel — that is the lever that
  sticks.

## 2026-09-21 — A travel request now starts the mission instance it asks for

- **The missing half of instanced travel.** The Battlegroup Director routes a
  player to a `ClassicalInstancing` group only if a server for that group
  already exists, and it never creates the first one. Nothing on our side did
  either, so every mission and hub destination read, once a minute:

  ```
  Processing travel queue for ClassicalInstancing group CB_Story_BanditFortress01 (servers: [], num: 0)
  Travel request expired. MapName: CB_Story_BanditFortress01
  ```

  until `TravelRequestExpirationTimeSeconds` (300s) killed the request. That is
  the five-minute "In Queue" a reporter measured, and why his mission instances
  never launched even after the LIST fix in #130 — the Director could finally
  *see* the maps, but still had nothing to route to.
- **`mock-k8s` now watches the Director's own log.** The trigger was in there
  all along, one line before the dead queue:

  ```
  Received travel request for 1 player(s) to CB_Story_BanditFortress01 (instancingMode=ClassicalInstancing)
  ```

  The new `internal/traveldemand` package tails `logs/director.log` (2s by
  default, `MOCK_K8S_TRAVEL_WATCH_INTERVAL=off` disables it) and scales the
  demanded map to one replica. Only `ClassicalInstancing` — `Dimension` and
  `SingleServer` maps belong to AlwaysWarm and to `start-ue5-dimensions.sh`,
  and scaling those from here would fight their owners. Map names are matched
  against `^[A-Za-z0-9_]+$` before they are ever used to build a resource name
  or a process argument.
- **`MaxConcurrentInstances` finally means something.** Until now it was parsed,
  logged and ignored; it is what caps how many maps demand may start.
- **Every recipe is materialised at boot.** `Store.MaterializeAll` creates all
  35 `ServerSetScale` records at startup with `replicas 0` — records, not
  servers. The Director only scales what it can list, and on the reporter's
  server the only maps ever materialised were the two he had put in
  `DUNE_ALWAYS_WARM_MAPS`; everything else was a recipe nobody had asked for.
- **`Store.ScaleUpTo` replaces "get it, poke the spec, update it".** `Get`
  returns the object by value but its `Spec` is a map, so the copy *aliases* the
  stored one: writing into it changed the store outside the lock, and then
  `Update`'s change detection compared that map against itself, found nothing,
  and skipped `OnSpecChange`. The spawner only noticed on the next reconcile
  sweep — measured at **29 seconds** before the fix, versus the same second
  after it. `ScaleUpTo` does the read, the compare and the write under the one
  lock, and is a floor rather than an assignment: a second traveller to a live
  map changes nothing, and an always-warm map cannot be pulled down.
- **The watcher starts at the end of the log, not the beginning.**
  `director.log` is append-only across restarts and reaches tens of megabytes;
  it holds every travel request the server has ever served. A tailer starting at
  offset 0 replayed all of them on its first tick — on the test box a restart
  brought up a mission instance from a request eight minutes dead. It now primes
  to the file's end, while a log that does not exist yet is read from the start
  (the Director has not written it, so everything in it will be new). Truncation
  is still detected: `rotate-logs.sh` trims in place, and a tailer that kept its
  offset across that would go silently deaf.

  Verified live end to end: request injected at 07:36:16.459, UE5 launched the
  same second, and the Director listed the server in the group at 07:37:11 —
  `servers: [27 (bMq8JJBjSTaTxy+ovvgKfg)]` — against a 300s budget. The one step
  that cannot be tested from here is a real player completing the journey.

## 2026-09-20 — Update 1.5's custom-rules file is wired into the panel

- **1.5 shipped a settings file we never seeded.** The patch notes say it
  plainly — *"Added a new UserServerCustomSettings.ini file for self-hosted
  servers"* — and it was sitting in the depot next to `UserEngine.ini` and
  `UserGame.ini`, which `prestart.sh` has always seeded. Ours never mentioned
  it, so 45 knobs Funcom exposed were inert, and the new client-side
  "server settings" screen showed players Funcom's defaults rather than the
  operator's.
- `prestart.sh` seeds it like the other two (only when absent, so operator
  edits survive), and it is now a sink in **both** file maps — `apply-config.sh`
  and `admin-http.py` keep separate tables over the same files, and a setting
  missing from either is a control nobody can use. That is exactly how the
  first attempt failed: the panel answered
  `target INI sink 'UserServerCustomSettings' is missing`.
- **39 of the 45 keys are exposed**, in five new panel categories — combat and
  NPC multipliers, thirst/heat/stamina, crafting and gathering economy, XP and
  Intel gain, Landsraad multipliers, building piece limits and stability, death
  and sandworm loot rules. They need no egg re-import: like 176 of the existing
  202, they carry no env var and are written straight to the file by the panel.
- **The other 6 are deliberately left out.** `PVPMode`, `GatheringAmount`,
  `bAllowSandstorms`, `bAllowSandworms`, `bIsBuildingRestrictionsEnabled` and
  `FiefdomLimit` are the ones Funcom itself ships commented out, each annotated
  `/!\ already exposed in UserEngine.ini / UserGame.ini` — keys we already
  write through the old path. Exposing both would give an operator two controls
  that can disagree, and which one wins is an in-game question nobody has
  answered yet.
- `BuildingPieceLimitMultiplier` answers issue **#120** (remove the building
  limits), which was closed for want of a lever.
- Verified live end to end: the file is seeded at boot
  (`seeded admin file: UserSettings/UserServerCustomSettings.ini`), the panel
  lists 241 settings across the new categories, and writing three of them —
  a float, another float and a bool — lands them in the file in place, with
  Funcom's comments intact. Restored to defaults afterwards.

## 2026-09-20 — Travel to hubs and mission instances: the four-month workaround is gone

- **One word: annotation, not label.** Since May, mock-k8s has answered the
  Director's `LIST serversetscales` with an empty set, because a populated one
  threw `ArgumentNullException` inside `ListServerSetScales` and stopped the
  Director opening port 11717 — a startup failure that took the container with
  it. The cause, decompiled out of `BattlegroupUtils.dll`:

  ```csharp
  foreach (ServerSetScale item in val.Items)
      dictionary.Add(ModelExtensions.GetAnnotation(item, "igw.funcom.com/map-name"), item);
  ```

  `GetAnnotation`. We set that key as a **label** and never as an annotation,
  so it read null and `Dictionary.Add` refused it. The annotation is set now,
  in both the lazy-create path and `ensureUniformItem`; the label stays for
  anything selecting on it.
- **The empty list was never free.** It is why the Director could not find an
  instance to travel a player to, so every journey to a hub or a mission
  instance sat in the queue until `TravelRequestExpirationTimeSeconds` (300s)
  expired. That is the **five-minute "In Queue"** a reporter timed — not a cold
  boot: a cold mission instance measured **10 seconds** here, and the Director
  processes its queue every second.
- LIST now returns real items **by default**; `MOCK_K8S_LIST_ENABLE=0` is the
  escape hatch if a future build throws again. The launcher passes the variable
  through without inventing a default — a `0` there would have silently
  restored the old workaround.
- Verified live, the same experiment that failed an hour earlier: with the
  annotation set, `LIST returning real items … count=3` and
  `Battlegroup Director ready: success`, all warm maps up, **zero**
  `ArgumentNullException`, `RestartCount=0`. Tests: 5 new cases in
  `annotation_test.go`, and `TestList_EmptyByDefault` inverted into
  `TestList_PopulatedByDefault` plus a guard that the escape hatch still empties
  the list.

## 2026-09-20 — The scale control can start a map nobody has visited

- **The button refused the only case it existed for.** `SH_Arrakeen is not
  tracked by mock-k8s` — reported again right after a Reinstall. A map only has
  a ServerSetScale once something has asked for it (the always-warm pre-spawn,
  or the Director when a player first travels there), so the hub maps an
  operator wants to start by hand are exactly the ones that failed the test.
  The previous fix only reworded the refusal; the reporter's panel then
  truncated the longer sentence mid-word, which helped nobody.
- mock-k8s holds a lazy-create recipe for **every** map in the BattleGroup
  template (35 on a stock 1.5 world) and materialises one on the first by-name
  GET — the panel simply had no way to learn the canonical name to ask for,
  because `/status` only listed what already existed. It now also carries
  `knownMaps`, and the scale path does that GET before giving up. The button
  starts the map instead of explaining why it cannot.
- Verified live: `POST /api/instances/SH_Arrakeen/scale {"replicas":1}` →
  `{"ok":true,"previous":0}`, a UE5 instance on partition 3, and the panel
  showing `SH_Arrakeen 1/1 healthy` — the exact action that failed for the
  reporter. Go: 3 new cases in `known_maps_test.go` (recipes exposed, stable
  order, empty without a template); Python: 5 in `test_admin_instances.py`,
  including an older mock-k8s with no `knownMaps` field, which must degrade
  rather than raise mid-request.
- ⚠️ **Found while verifying, NOT fixed here:** scaling back to 0 leaves the
  UE5 process running. The spawner terminated pid 2282 and reported success,
  but the live server was pid 2297 — `DuneSandboxServer` forks, and the pidfile
  does not name the survivor. mock-k8s then frees the pool slot while the
  orphan still holds its ports, which is the same shape as the duplicate-
  partition crash of #124, at shutdown instead of startup. Filed as the next
  thing to look at.
## 2026-09-20 — `players` reads the right table again, and stops seeing double (#121)

- **The listing had stopped working entirely.** `admin players` answered
  `ERROR: column a.platform_id does not exist`. Funcom's migration
  `DA-19433_encrypt_platform_id.sql` encrypted that column at rest for GDPR —
  it is `encrypted_platform_id bytea` now — and its own header says where the
  readable copy went: *"expose a decrypted copy through the `accounts` view.
  All reads/lookups already go through that view."* Ours did not. The three
  stragglers (`players`, `resolve steam:<id>`, `player-state`) now read
  `dune.accounts`, like the rest of the script already did.
- **The duplicate players of issue #121 are gone.** An account can carry more
  than one `encrypted_player_state` row — a self-restore or an interrupted
  transfer leaves the previous one behind with dangling actor links — and a
  plain `LEFT JOIN` turned each into a second line in the panel. The husk keeps
  its last `online_status`, so it showed as permanently **Online**, and its
  character name is empty, so the UI printed the FLS id where the name goes:
  every player twice, once correctly and once as a never-leaving stranger named
  after their own id. `DISTINCT ON` now picks the live row per account, using
  the same definition of "husk" that `char_state_sweep` deletes by. The
  `online` filter is applied **after** that choice, so a stale row can no longer
  inflate the online list.
- The Server-persona `INSERT` picks its column at execution time from
  `information_schema`, so it works on a battlegroup that predates DA-19433 as
  well as one that has run it.
- Verified live, both shapes against the same row: with a synthetic husk
  injected, the old query returned 3 rows including
  `DE0BCCAA2501BF22 | (no name) | Online`, the new one returned 2 and
  `players online` returned 0. Husk removed afterwards; the database is back to
  its two original rows.
## 2026-09-20 — Logs are bounded (13 GB reclaimed on the test server)

- **Nothing ever trimmed `logs/`.** The test server held a **10.4 GB**
  `director.log` and a 1.3 GB `text-router.log`; a reporter's production server,
  8.9 GB. The Director writes that much at its *default* `level=info` — every
  ServerState and settings update is a full JSON payload, ~15 KB a line, on an
  **empty** server. Profiling 200 MB of it: DBG 72.6 MB, INF 38.8 MB, and
  87.5 MB of multi-line JSON.
- `scripts/rotate-logs.sh` (new) cuts any log over `DUNE_LOG_MAX_MB` (200) down
  to its last `DUNE_LOG_KEEP_MB` (50). `console.sh`'s supervisor runs it every
  `DUNE_LOG_ROTATE_INTERVAL` (300s), starting with the first tick, so a
  container inheriting a months-old `logs/` is bounded seconds after boot.
- **Truncation in place, not renaming.** Every service appends with `O_APPEND`
  (`launch_bg`), so a truncated file is picked up immediately; a renamed one
  would leave each daemon writing to an unlinked inode — the disk would never
  come back and the visible log would stay empty until the next restart. The
  tail is kept rather than the file deleted: the recent end is the half an
  operator needs, and a log that vanishes mid-incident is its own outage.
- **The log level is left alone on purpose.** `info` is what Funcom ships and
  what past incidents were diagnosed from; lowering it is the operator's call
  in `director_config.ini`. Bounding the file is not.
- Regression harness: `bash scripts/tests/test-rotate-logs.sh` (11 cases),
  including the open-writer property that rules out rename-based rotation, and
  a guard that sourcing the script does not steal the caller's log tag — it did,
  in the first draft, which would have mislabelled every later supervisor line.
  **Verified live: 13 GB → 836 MB on the test server**, marker line written into
  `director.log` (`9930 MB dropped, last 50 MB kept`) and the Director appending
  into it normally afterwards.

## 2026-09-19 — A mistyped Funcom token no longer restart-loops the boot

- **The first wall a new host hits looked exactly like a crash.** `console.sh`
  learned on 2026-09-17 to tell a crash from a setting the operator must
  change; the boot stages that run *before* it had not. `prestart.sh` called
  `die()` — exit 1 — for a Funcom token that is missing or will not decode,
  Wings read the non-zero exit as a crash, recreated the container, and the
  same unreadable token failed again. Every loop wiped the console line that
  said why.
- `lib.sh` gains the second verdict the boot was missing: **`die_config()`**,
  exiting `EX_CONFIG` (78, from sysexits.h), and **`run_boot_stage`**, which
  the entrypoint now runs all 21 stages through. On 78 the boot **holds** and
  keeps the explanation on screen; on any other code it exits exactly as
  before, because a transient failure genuinely should be retried.
- Four faults are now config faults: the missing token, the undecodable JWT,
  the missing extracted depot, and the unwritable K8s ServiceAccount mount.
  The Postgres and schema-load failures deliberately stay retryable — those
  are the kind a restart does fix.
- `hold_for_operator` moved from `console.sh` into `lib.sh`; the supervisor and
  the boot stages now share one implementation of the same verdict.
- The missing-token message stopped pointing at AMP: it now names
  `DUNE_JWT`, the panel's Startup tab, and where to get a token.
- Regression harness: `bash scripts/tests/test-boot-config-fault.sh` (14
  cases). Three go red when the hold is removed; the rest are the
  over-correction guards — an ordinary failure must *still* exit, or a server
  that would have recovered gets stranded. Verified live by pasting a
  truncated token into the test server: `[prestart] [ERROR] couldn't decode
  HostId from JWT` followed by the HELD banner, container still up at
  `RestartCount=0` 75s later, and a clean boot once the real token was put
  back.

## 2026-09-19 — Three ways the panel left an operator in the dark

All three were found while diagnosing the Deep Desert crash loop below. None of
them caused it; together they are why it had to be diagnosed from screenshots
by someone with the server's API keys, instead of by the operator.

- **The Logs tab showed an empty file for every UE5 instance.** The pattern
  guarding UE5 log names was `^ue5-[A-Za-z0-9_]+$` — no hyphen — while its own
  comment claimed it accepted `ue5-<Map>-<suffix>`. Real instance logs carry
  the pool slot (`ue5-DeepDesert_1-p2.log`) and, for travel partitions, the
  dimension (`ue5-DeepDesert_1-dim1-p101.log`), so every one of them was
  rejected at tail time *and* filtered out of the source list. What remained
  was the 0-byte placeholder `console.sh` touches per always-warm map: the tab
  answered `{"exists": true, "lines": []}` for a 492 MB log. Fixed, and
  `list_sources` now reports each source's `size` so a placeholder is
  distinguishable from an instance log without clicking it.
- **A map that kept failing to spawn went quiet.** mock-k8s counts consecutive
  failures and backs off (1m, 2m, 4m… capped at 15m), and `/api/status` has
  been forwarding `consecutiveFailures` and `nextRetry` all along — the Fleet
  health card just never rendered them, so Deep Desert read `failing 0/1` and
  then nothing. It now says how many spawns failed in a row, when the next
  attempt is due, and which log to read.
- **The scale control refused on-demand maps with a dead end.** `SH_Arrakeen
  not tracked by mock-k8s` reads like a panel fault; it actually means the map
  has no scale record yet because nothing has started it. The message now says
  so, and names the two ways to get one (add it to `DUNE_ALWAYS_WARM_MAPS`, or
  let a player travel there once).

## 2026-09-19 — A slow-booting map no longer gets a duplicate spawned on top of it

- **Deep Desert crash-looped until the spawner gave up on it.** A reporter's
  server, freshly updated to 1.5, showed `Deep Desert failing 0/1` and then
  nothing at all; players travelling anywhere off the warm maps sat in
  `In Queue` for ever. The UE5 log named the killer:

  ```
  LogServerIndices: Warning: Server B connected with desired index 8, which is
                    already assigned to A, assuming this server has shut down
  Fatal error: [S2sController.cpp:4497] Local partition is not found
  SIGSEGV: invalid attempt to write memory at address 0x3
  ```

  Two UE5 instances held partition 8 at once, so the newcomer evicted the
  incumbent, which fatal'd — producing a reap, another spawn, another
  collision, until `consecutiveFailures` hit 7 and mock-k8s parked the map at
  `desired=0`.
- **Where the duplicate came from, in our code.** `start-ue5.sh` backgrounds
  UE5 and blocks on its UDP-bind handshake before writing the pidfile, so the
  pidfile is the "I am up" signal. `capturePID` gave up waiting for it after a
  flat **20s** — while Deep Desert takes ~40s to get there (our own autoscaler
  config says as much). Giving up closed `pidReady`, which is exactly what
  makes `sweep()` classify an instance as a *phantom*: it reaped a UE5 that was
  still booting, released its port slot, and the next reconcile tick spawned a
  second one on the same partition. The first was never killed — nothing knew
  it existed any more.
- **The fix is to use the signal the spawner already had:** the launcher
  process. While `bash start-ue5.sh` is alive the instance is *starting*, so
  `capturePID` keeps polling however long the map takes. Once the launcher has
  exited it allows `pidWait` (20s) more for the pidfile to land, then declares
  the spawn failed; `pidHardCap` (10m) bounds the whole wait so a hung launcher
  cannot hold a slot for ever. Launcher identity is reuse-proofed with its
  `/proc` start-time, like every other pid in this package.
- Regression tests in `mock-k8s/internal/spawner/spawn_race_test.go` (4 cases).
  Two go red when the launcher check is removed; the other two are the
  over-correction guards — a genuinely failed spawn must still be reaped and
  its slot returned, and a hung launcher must still hit the cap. Verified live:
  the rebuilt binary boots all three warm maps and Deep Desert is still
  `desired:1 current:1 healthy` at 88s uptime, well past the old 20s window,
  with no `pidfile not seen` warning.
## 2026-09-17 — The console says when the game build has fallen behind

- **An out-of-date server used to announce itself through the wrong error, on
  the wrong machine.** The egg downloads the Funcom depot at *install* time
  only, so a deployment stays on the build it was installed with until someone
  reinstalls. When Update 1.5 landed (build `25351779`), a reporter's server
  stayed on `24653560` and the first symptom was players hitting
  **`M52 Outdated Client`** — a client-side code that points at the player's
  machine, not at the server that actually needed updating. The revision the
  server advertises to FLS (`2064155` against the client's `2111270`) is what
  gets refused, and neither number is visible anywhere an operator looks.
- `scripts/check-game-build.sh` (new) compares the installed build against the
  one Steam publishes and prints the difference, naming M52 so the symptom and
  the cause meet. Backgrounded from `pelican-entrypoint.sh`: it never blocks
  the boot, and a failed lookup prints nothing at all rather than training the
  operator to ignore a line that cries wolf. The verdict is also left in
  `server/state/build-check.json` for the panel to read without a second
  lookup.
- **It does not update anything, deliberately.** Pulling 5 GB of new binaries
  under a running server would swap the code out from under live players, and
  a boot-time download would make every restart a bet on Funcom's CDN.
  Reinstall stays the update, as the header of this file describes.
- The lookup is an HTTP call to a SteamCMD mirror, not the SteamCMD this egg
  ships: that binary is 32-bit and the runtime image carries no lib32 (the
  installer apt-installs `lib32gcc-s1`/`libcurl4:i386` into the *install*
  image only), so it fails there with `required file not found`. Valve
  publishes no first-party endpoint for a branch's build id. Set
  `DUNE_BUILD_CHECK_URL=off` to disable the call, or point it at another
  source; `APPID` in the URL is substituted.
- Regression harness: `bash scripts/tests/test-check-game-build.sh` (15 cases).
  Verified live in the container on both paths — silent on the up-to-date
  server, and the full warning against a fixture pinned to the reporter's
  `24653560`.

## 2026-09-17 — A rejected setting no longer restart-loops the server

- **A configuration fault is now told apart from a crash.** Funcom's Update 1.5
  (server build `25351779`, published 2026-09-17 12:00 UTC) tightened the
  display-name validation in FLS. A reporter's server then restart-looped every
  ~10m30 for eight hours: `GatewayDeclareFarmStatus` came back
  `INVALID_ARGUMENT / Invalid display name`, the gateway burned its ten retries
  (~9m25 of doubling backoff) and exited, the supervisor saw a dead critical
  service and exited 3, and Wings dutifully recreated the container — replaying
  the same rejected name forever. The panel showed only
  `[console] [WARN]     see logs/gateway.log`.
- `scripts/diagnose.sh` (new) reads the dead service's log tail and recognises
  the refusals that a restart cannot fix. `console.sh` prints the cause and the
  exact panel variable to change, then **holds** instead of exiting: the
  container stays up, the diagnosis stays on screen, and the operator fixes the
  setting and restarts deliberately. Faults with no known signature keep the
  previous behaviour (exit 3, let Wings recreate).
- The offending value lives in **`DUNE_WORLD_TITLE`**, not
  `DUNE_SERVER_DISPLAY_NAME` — that is the string the gateway advertises.
  The rule is enforced on Funcom's side and is not documented; no client-side
  validation is hard-coded here, so a future tightening surfaces as FLS's own
  message rather than as ours going stale. Probed against FLS on 2026-09-17:
  `[`, `]`, `|`, `&`, `:`, `/` and `,` are each accepted on their own.
- Regression harness: `bash scripts/tests/test-diagnose.sh` (9 cases, fixtures
  taken verbatim from the incident). The transient-failure cases are the ones
  that matter — a false "unrecoverable" would hold a server that had been about
  to recover.

## 2026-08-25 — Map bounds calibrated from the game itself (#116)

- **Deep Desert sector labels were off by one row.** The projection bounds were
  an uncalibrated round-number guess inherited from Icehunter/dune-admin
  (`-1300000..1200000`, and upstream is still broken — their #213 / #310).
  Replaced with the number the game reports about itself: the UE5 dedicated
  server prints its own world box at every boot under `LogDuneWorldPartitioner`
  — `Min=(X=-1270000 Y=-1270000) Max=(X=1168400 Y=1168400)`, identical across
  all four DD dimensions and every build we have logs for. Span 2 438 400 uu =
  24.384 km square = 8128 landscape quads at 300 uu/quad, exactly 3x
  Survival_1's 8128 quads at 100 uu/quad and sharing its -50 800 centre.
  Corroborated by `cdn.th.gl/dune-awakening/config/tiles.json`, which declares
  `deepdesert_1` at `[[-1270399,-1270399],[1167999,1167999]]` (0.016% of span
  away) and publishes the very image we ship — `web/public/deepdesert.webp` is
  byte-identical to its z=0 tile. Verified against the two live observations
  in the issue: `(516429, -1009962)` now reads **I7** (was H7) and
  `(1127612, 1077779)` reads **A9**, both as their reporter read them in game.
  One assumption remains explicit rather than buried: that the player-facing
  9x9 sector grid spans exactly this landscape box. The first observation sits
  only ~0.04 of a sector (~109 m) from the I/H line, so that is what a third
  ground-truth point near a row boundary would put to the test.
- **New `web/src/mapProjection.ts`** — the world<->image projection, the MAPS
  table and the sector math extracted out of `MapTab.tsx`/`DeepDesertGrid.tsx`
  into one React-free module, so it can be pinned by a test.
- **New `scripts/test_map_projection.py`** (16 cases) — compiles that module
  with the repo's own `tsc` and runs it under node, asserting the two ground
  truths, the grid orientation (I = north, A = the southern arrival row),
  round-trip inversion and the guards below. Joins the existing offline suite
  (`for t in scripts/test_*.py`) with no new toolchain; skips cleanly where
  `web/node_modules` or node is absent. It fails on the old bounds by
  construction, so the calibration cannot silently drift again.
- Live-map fixes found while auditing the same path:
  - Player dots no longer survive a map switch or a failed fetch. They were
    re-projected through the new map's bounds, so the Deep Desert calibration
    card could report DD sectors for Hagga coordinates — the exact readout the
    issue asks people to trust. Superseded responses are now discarded too.
  - The calibration card marks a row **clamped** when the coordinate fell
    outside the declared bounds. `worldToPct` pins strays to the edge, so an
    out-of-range player used to be indistinguishable from a genuine A9.
  - Teleport refuses a target who is not on the map being viewed. The
    `TeleportTo` payload carries no map field, so TPing a Hagga player to a
    Deep Desert location dropped them at those coords *inside Hagga*.
  - Click-to-pick refuses clicks that land off the image or before it decodes,
    instead of clamping them into a corner coordinate (an `<img>` with no
    intrinsic size reports height 0; the divide-by-zero silently produced
    `maxY`). Map images now declare their real 512x512 size.
  - Sector labelling at an exact grid line: `((1/3)*100)/100*9` is
    `2.999999999999999` and `((2/3)*100)/100*9` is `5.999999999999998` in
    IEEE754, which labelled the 3rd and 6th lines one cell west/north of where
    the SVG draws them. (`(1/3)*9` alone is exactly 3 — it is the round-trip
    through percent that loses the bit.)
  - Malformed POI / large-spice entries and non-finite coordinates are skipped
    rather than painted at the map's north-west corner.
- `admin_wickmaps.active_dd_seed` no longer discards Coriolis seed **0**:
  `exact or fallback` treated a legitimate 0 as absent, so 1 week in 12 drew
  the wrong one of the 12 fixed Deep Desert layouts.
- `admin_map.parse_markers` neutralises non-finite coordinates. `float()`
  accepts `nan`/`inf`, `json.dumps` then emits bare `NaN`/`Infinity`, and the
  SPA's `JSON.parse` rejected the whole payload — one bad row blanked the Live
  Map with no error shown. `int(float("inf"))` also raised an uncaught
  `OverflowError`.
- **Hagga Basin recalibrated from the same source.** Its box was a hand fit
  eyeballed over months by clicking landmarks (`-437871..350539 /
  -462011..376267`) — up to 24 000 uu out and, tellingly, not square while both
  the landscape box and the 512x512 image are. Replaced with the server's own
  `Survival_1` box, `-457200..355600` on both axes; th.gl's tile bounds for the
  map agree to 0.05% of span. **Live dots on Hagga shift by ~2-3% versus the
  previous build — that shift is the correction, not a regression.**
- **Arrakeen and Harko Village: the bounds were never the blocker, the images
  are.** The same log yields authoritative boxes for them too
  (`SH_Arrakeen Min=(X=-32765 Y=-21256) Max=(X=27235 Y=18744)`,
  `SH_HarkoVillage Min=(X=-99855 Y=-78118) Max=(X=100145 Y=121882)`), and the
  shipped guesses are wrong against both — Arrakeen's maxX by 10 235 with less
  than half the real Y range, Harko by an order of magnitude. But the shipped
  *images* are truncated crops, not renders of any box: `arrakeen.webp` fills
  its full width and only the top 57.8% of its height, with buildings sliced
  mid-shape at the cut, and `harko.webp` is a 319x320 block in the top-left of
  a 512x512 canvas. Neither content rect matches its world box's aspect
  (Arrakeen's art is 1.73 against a 1.50 box) or th.gl's tile layout, so there
  is no rectangle to project onto; dropping the real numbers in would move every
  dot to a *different* wrong place while looking authoritative. Upstream no
  longer ships these assets either. So they keep their old bounds and now carry
  an `uncalibrated` note that the Live Map shows as a banner — use those two
  maps to see *who* is on them, not *where*. Supplying a full-extent top-down
  render of either map is all that stands between this and finishing the job.
- Neither Arrakeen nor Harko draws a sector grid, so #116 itself does not depend
  on them.

## 2026-08-21 — Guild management (dune-admin port)

- New **Guilds** tab (Players group) + `guild-*` subcommands, ported from
  Icehunter/dune-admin #117 (MIT). Reads: all guilds with faction + member
  count, per-guild roster (canonical controller ids, roles, online status)
  and pending invites. Writes go through the game's own guild procs — they
  self-acquire the guild advisory lock and `pg_notify('guild_notify_channel')`
  so the running maps apply changes **live**: set description, transfer
  leadership (100 = the single leader slot; promoting demotes the sitting
  leader to 50), kick (refuses the leader loudly where the game proc would
  silently skip), disband, and admin-side guild creation (name uniqueness +
  per-player cap enforced by the game). Rename is the one lock-guarded
  UPDATE — no game proc or notify verb exists, so it shows in-game after the
  next restart. Every write is verified by re-read. Beyond dune-admin's
  surface: kick, disband and create are new.
- Fix (live-e2e-caught, same day): faction ids are data-driven —
  `guilds.guild_faction` is an FK into `dune.factions` (1=Atreides,
  2=Harkonnen, 3=None, 4=Smuggler), so guild-create resolves the neutral id
  from the table and the guild list joins it for the display name instead of
  hardcoding. Full live e2e: 14/14 (create → roster → describe → rename →
  four expected refusals → disband + DB cleanup).

## 2026-08-21 — Database tab + base owner resolution survives the DD wipe

- **Bug fix — Deep Desert bases showed "unclaimed".** The Bases owner column
  resolved the custodian through `permission_actor_rank.player_id → dune.actors
  → account`, but the Deep Desert weekly wipe rotates the player-controller
  actor, orphaning that middle link so a very-much-owned base read as
  unclaimed. Owner resolution now falls back to the **base actor's own
  `owner_account_id`** (which rides with the base across the wipe) when the rank
  path comes up empty — strictly additive, so it only ever replaces "unclaimed"
  with the real owner, never changes a base that already resolved.
- **New: Database tab (read-only).** The backend already exposed
  `POST /api/database/sql` (SELECT / WITH / EXPLAIN / SHOW only, capped at 200
  rows) but nothing in the UI surfaced it — so inspecting the game DB meant
  shelling into Postgres on the host. There's now a first-class query console
  in the panel, with one-click presets for base owners, Deep Desert
  coordinates, and the table list. Writes are rejected server-side.

## 2026-08-21 — Live Map shows player coordinates (self-service calibration)

- Player markers now expose their **raw world (x, y)** on hover, on every map —
  the data was always fetched to plot the dot, just never shown. On Deep Desert
  a new **Grid calibration** side-panel readout lists each live player with
  their raw coords and the sector the current projection lands them in. This
  closes a gap: grid calibration (#116) previously meant asking an operator to
  run a `db-sql` query, but the panel exposes no database tool — now the two
  calibration points (raw coords + real in-game sector) can be read straight
  from the panel with no SQL.

## 2026-08-21 — Item edit (quantity / quality)

- New `item-edit <item_id> [stack=N] [quality=N]` — the missing verb between
  `give-item` (INSERT) and `item-delete` (DELETE). Edits one stack's quantity
  and/or quality in place via a bounded direct `UPDATE dune.items` (no safe
  server proc exists), reusing `item-delete`'s ownership resolution and gating:
  player-carried items require the owner **offline**, world/base items require
  the **map down** (the running map caches inventory in memory and would clobber
  a live edit). Bounds: stack 1..1,000,000; quality capped at the highest tier
  the world already proves (floor 6) so an out-of-domain tier can't ghost the
  item. The write is re-read and verified before reporting success. Surfaced as
  an ✎ edit control on every item row in the **Player inventory** tab and in
  **base containers**; HTTP `POST /api/items/<item_id>/edit`.

## 2026-08-21 — Coriolis seed control

- The Deep Desert side panel gained a **Coriolis seed control**. It reads the
  effective `m_ForcedCoriolisWorldSeed` override and lets you pin one of the
  12 fixed layouts (or restore automatic weekly rotation) from a picker that
  previews each seed's POI composition — count, large-spice sectors and
  confidence — drawn from the same Wick Maps catalogue. The setting was
  already writable in the Settings tab as a bare integer; this turns it into
  an informed choice tied to the map. Applying writes through the validated
  settings path (no new endpoint) and **takes effect at the next Deep Desert
  regeneration** (cycle end / DB wipe), not on the running map; picking is a
  two-step (select → Apply) to prevent an accidental repin. Backend adds
  `admin_wickmaps.layouts_summary` (unit-tested) and enriches
  `GET /api/map/deepdesert-layout` with `forcedSeed`, `forcedSeedExplicit`
  and `summaries`.

## 2026-08-21 — Deep Desert sector map (Wick Maps)

- The Live Map's Deep Desert now draws a proper **9x9 sector grid** (A-I ×
  1-9) and overlays the week's points of interest — wrecks, caves, spice
  fields, testing stations, titanium, stravidium, taxi. The Deep Desert
  cycles through 12 fixed layouts by Coriolis seed; the panel detects the
  active seed and shows the matching POIs, so it self-updates each cycle
  with no re-rendering and no terrain image (nothing goes stale). Side
  panel shows the seed, confidence and a legend. Ported from DST
  (Apache-2.0) — see ATTRIBUTION.md. Grid bounds are being calibrated
  against real player positions (#116).

## 2026-08-21 — Panel UI updates no longer need a hard refresh

- `index.html` was served with no cache headers, so browsers applied
  heuristic freshness and kept pointing at the previous build's
  content-hashed JS — a reinstall silently served the old UI (this is
  why the #116 map fix looked undeployed). `index.html` is now
  `no-cache, must-revalidate`; hashed `/assets/` get an immutable
  long max-age. Every future frontend change lands on the next reload.

## 2026-08-21 — Live map: Deep Desert pins were vertically mirrored

- A player at the Deep Desert southern arrival zone rendered at the top
  of the live map (#116): world Y grows southward in this game and the
  DD map config was missing the vertical flip that Hagga Basin and
  Arrakeen already carry. Harko Village had the same latent flaw — both
  fixed; the click-to-teleport picker follows the same transform. DD
  bounds remain estimates: report any residual offset with a known
  in-game position and they'll be calibrated from it.

## 2026-08-21 — Player events + battlepass

- **Live player events** (🎯 sub-tab in Events & Diagnostics): zone races
  pay listed participants who reach a sphere (first in list order, one
  per tick); milestones pay every online player crossing a level or
  holding an achievement tag. Rewards are a shared spec (solaris, items,
  specialization XP) delivered in order with a claim ledger — each player
  is paid exactly once, partial failures resume without re-paying, and a
  daemon restart never re-announces past deeds.
- **Battlepass** (🎖 sub-tab): a 188-tier catalog (1,619 intel + 86
  schematic tiers, extracted from dune-admin's own generator) over
  levels, quests and exploration. Pre-existing progress baselines and is
  never paid; a tier earns only when the engine watches it happen
  (`award_past` opts out — set it BEFORE a player's first scan). Intel
  delivery is money-safe and waits for the player to be offline;
  demote/purge resets ship with the storm-safe semantics upstream
  documented the hard way.
- Both engines are OFF by default, run in one self-pacing daemon, touch
  the game only through the audited command layer, and were proven
  single-payer under 6-way process races. New grant subcommands:
  `award-intel` (clamped ≤2779, offline-gated) and `award-track-xp`
  (clamped ≤44,182).
- Ported from Icehunter/dune-admin's events + battlepass engines (MIT) —
  see ATTRIBUTION.md.

## 2026-08-21 — Deep Desert per-partition PvP

- **Per-instance PvP designation** via `DUNE_PVP_PARTITIONS` (e.g.
  `8,101,102,103` for the hot Deep Desert and the three tunnels): rendered
  as Funcom's own `+m_PvpEnabledPartitions=<id>` lines in the shared
  UserGame.ini — the syntax their template documents in a comment. In-game
  PvP labels and rules confirmed on all four partitions by the issue
  reporter, plus a full-day dual-Deep-Desert soak. Fixes #106. (#109)
- `DUNE_DD_PICKER_ROUTING` flips the DeepDesert_1 matchmaker rule from
  FirstOfGroup to HomeDimension so the in-game destination picker's choice
  actually routes (Survival_1 already ships HomeDimension — the "honour
  the choice" rule).
- Five verified QoL settings join the catalogue (reconnect grace ×2, ping
  system ×3); the settings engine gains repeated `+key=` handling and a
  drift sentinel on the matchmaker tuples.
- `DUNE_EGG_REF` is now a declared egg variable (default `main`) — it had
  always been consumed by the install script without being declared.
  **Upgrade: re-import the egg, then Reinstall.**

## 2026-08-20 — World reset, gated and reversible (C6)

- **Season resets without fear.** `world-reset-arm "RESET WORLD"` verifies
  zero players online, takes a verified database backup (optionally a
  per-character backup sweep too), and writes a durable marker — the world
  is untouched until the NEXT RESTART, which sets the current datadir
  aside (moved, never deleted) and boots a fresh, empty world under the
  same battlegroup identity, tokens, and config. 🌍 card in the Scheduler
  tab, chained to the restart-now flow.
- **Rollback is a swap**: `world-rollback-arm "ROLL BACK WORLD"` restores
  the preserved world at the next boot; progress on the fresh world is
  parked (`pg.rolled-back-<ts>`), not lost. Retention: 2 preserved
  pre-reset worlds, 1 rolled-back world.
- Every gate fails closed: confirmation phrases (re-validated
  server-side), zero-online check, backup re-verified at arm AND at boot,
  and any doubt boots the old world untouched. The boot hook can
  structurally never brick the boot.
- Proven live end-to-end on a real server: reset → fresh world (the
  wipe-guard's armed boot re-apply re-patched it automatically) →
  rollback → original world back, characters and permissions intact.
- Pairing with character backups: after a reset, a player joins the fresh
  world once, then `char-restore` brings their old character back.
- Ported from coastal-ms/DST-DuneServerTool's worldreset-2 (Apache-2.0),
  reshaped for this single-container stack — see ATTRIBUTION.md.

## 2026-08-20 — Base backup wipe-guard (C3.5)

- **Stored base backups can now survive the weekly Deep Desert reset.** A
  base backup is not a blob: the game keeps the actor rows in state
  `'BaseBackup'`, and Funcom's season cleanup deletes every actor whose
  state is not Travel/VehicleBackup/VehicleRecovery — `'BaseBackup'` is
  missing from that list, so allowing the backup tool in the Deep Desert
  fed stored backups to the wipe. The guard adds the one missing
  exclusion to the live cleanup function: anchored (refuses a function
  body it does not recognise), byte-preserving, idempotent, verified by
  re-reading the definition after every write. `base-guard-status` /
  `base-guard-apply` / `base-guard-revert`, plus a 🛡 card in the Bases
  tab. Commit `9a42b8d`.
- Optional **boot re-apply** (`data/admin/base-guard.json`, off by
  default): the guarded function is Funcom-owned and a game update can
  replace it, so the entrypoint re-patches right after `migrate-db` when
  armed. Never blocks the boot.
- The setting that makes this matter — **Base Backup Tool Allowed Maps**
  (`m_BaseBackupToolMapRestriction`) — joins the settings catalogue (196
  entries); add `DeepDesert` to it to let players use the backup tool
  there.
- Behaviour proven against Funcom's real cleanup function on a live
  server: with the guard, a `BaseBackup` actor survives the wipe; without
  it, it is deleted.
- Ported from coastal-ms/DST-DuneServerTool v13.3.0 BaseBackupGuard
  (Apache-2.0) — see ATTRIBUTION.md.

## 2026-08-20 — Base permission writes (C3.4)

- Edit base permissions from the panel and CLI: set/add a player's rank
  (1 = Owner, 2 = Co-Owner, 3 = Associate), remove a player, and a
  player picker limited to ids the game actually honours
  (`base-permission-set` / `base-permission-remove` /
  `base-permission-candidates`). Commit `2b891c6`.
- Transfer a base's ownership to a reserved **system custodian**
  (`base-transfer-custodian`): existing access is preserved, the outgoing
  Owner becomes Co-Owner, and the reserved Server persona (Red-Blink
  Care-Package-compatible tuple) is created on first use. Reversible.
- These writes apply **to the running map immediately** — they go through
  the game's own stored procedures, which notify live servers; no restart
  and no map-down gate. (Direct DML on `permission_actor_rank` is the trap:
  it skips the marker refresh + notify and the running map reverts it.)
- Server-enforced invariants: exactly one Owner (promote demotes the old
  Owner in the same transaction), the `m_MaxPermissionsPerActor` roster cap,
  claimed-base checks with friendly refusals.
- The permission roster read gains a `canonical` flag (marks rows the game
  ignores) and labels for the reserved Server/GM identities.
- Ported from Red-Blink's base permission editor (MIT) — see ATTRIBUTION.md.

## 2026-08-20 — Ecosystem port wave (six features in one day)

- **Per-character backup/restore** via the game's native transfer subsystem:
  `char-backup`, `char-restore`, retention pruning, and a pre-delete safety
  net on `account-delete`. Two upstream dune-admin bugs fixed in passing
  (respawn-uuid collision on self-restore; account-id reuse on import). (#110)
- **Connection doctor**: 11 read-only checks (advertised IP, ports,
  readiness, FLS heartbeat…) behind `GET /api/doctor` and a 🩺 card on the
  Overview tab. (#111)
- **Bases inventory + water management**: claimed-base list, per-type water
  levels (cisterns, windtraps, blood purifiers) and a refill that fails
  closed unless the base's map is fully stopped — a running map rewrites
  base state from memory on flush. (#112)
- **Player chat commands** (`!ping`, `!kit`): a second bounded queue on the
  game broker's `chat.intercept` exchange copies chat with zero
  interference; off by default, per-command opt-in, per-player cooldowns.
  DST v13.4 port. (#113)
- **Generator fuel**: per-device fuel levels (Oil / SpicedFuelCell / turbine
  lubricants, measured burn rates) and a transactional refill bounded by
  stack sizes and inventory slots, same map-down gate as water. (#114)
- **Base containers + permission roster (read)**: every stored item stack
  per base with delete, permission roster per base, and a hardened
  `item-delete` that distinguishes world inventories (map-down gate) from
  player-carried ones (offline gate). (#115)

## 2026-08-19 — Passwordless servers

- Clearing `DUNE_SERVER_PASSWORD` now yields a public, passwordless server
  instead of silently keeping the previous password. Requires re-importing
  the egg (new `nullable` rule on the variable). Fixes #107. (#108)

## 2026-08-13 → 2026-08-16 — Depot sync, panel hardening, HTTPS

- 2026-08-12 depot sync + the text-router failure behind #82. (#83)
- Admin panel screenshots in the README (#84); Grant-item durability preset
  clipping fix (#86).
- Stop one stalled connection wedging the whole admin panel — the
  single-thread `HTTPServer` issue #89. (#90)
- `panel restart` console command + notices when a non-critical service
  dies. (#91)
- Reverse-proxy correctness: trust `X-Forwarded-For` only from declared
  proxies (#93); stop `DUNE_ADMIN_UI_DOMAIN` implying TLS is in front (#94)
  and advertise the HTTPS domain instead of an unusable `http://IP:port`
  (#88).
- Scheduler fixes: failed auto-restart diagnosis without leaking the API
  key (#95), restart recorded/consumed at the right moment (#100, #101).
- Command audit persisted across restarts (SQLite under `server/state/`),
  read-only polling no longer evicts real actions. (#96)
- Panel-changed settings no longer revert on restart. (#97)
- Player Hard Cap exposed as an egg variable. (#98)
- Restart card with presets + quiet warning window. (#99)
- **HTTPS from the egg**: direct TLS termination, ACME DNS-01, three
  certificate backends. (#104, docs #105)

## 2026-06-13 — Importable eggs for both panels

- Egg JSON ships in two formats: Pelican `PLCN_v1` and Pterodactyl
  `PTDL_v2` (the previous `PLCN_v3` export was importable by neither).
  Closes #41. (#81)

## 2026-06-07 → 2026-06-09 — Admin UI redesign

- Information architecture: 22 tabs regrouped into 4 sections / 14
  workspaces (spec #62; phases #63–#68).
- Readability pass: friendly setting names, unified badges, live sync,
  grouped sietch settings. (#61)
- Global Live auto-refresh across tabs. (#69, #70)
- Design overhaul in 8 phases: desert theme, two-tier top bar, ⌘K command
  palette, toasts + audit, tweaks panel + theme switcher, custom steppers,
  unified player picker, SVG line icons. (#71–#78)
- Overview KPI de-dup, inventory containers, item tier/rarity +
  moderation color-coding. (#79, #80)

## 2026-06-05 → 2026-06-06 — Autoscaler + sietch parking

- Demand-based autoscaler for the on-demand maps: live-connection counting
  (never evicts hub visitors), travel-demand parsing, Deep-Desert-dimension
  load-proportional scaling, live pool view + Discord webhook alerts,
  fast-wake latency work (cold→warm in ~5 s). (#43–#44, #47–#49, #51–#55)
- Security: stop leaking the admin-ui password and DRY-RUN token to the
  console stream (#50); redact JSON-quoted secrets from the Logs tab (#42).
- Daemon configs moved to persistent `server/state/`, surviving reinstalls.
  (#45, #46)
- Sietch parking: park/unpark a Survival instance from the Instances tab,
  boot-skip filter, per-state badges, orphan `farm_state` auto-sweep.
  (#56–#60)

## 2026-06-03 → 2026-06-04 — Operations suite

- Unattended scheduler: auto-restart + auto-backup (#23); generic scheduled
  tasks — broadcasts + time-based instance scaling (#37).
- Live player map + inventory viewer. (#22)
- Player editor: faction tier, progression unlock/lock, current-state panel
  (solaris/XP/faction/journey), remove-faction. (#24, #26)
- Market bot: economy writer (NPC orders) + Market tab (#28), autonomous
  loop + d12 gamble-buy (#29).
- Spicefield economy controls (#27); Loot & Spice tab with curated loot
  controls (#36).
- Instance management: topology view, map spin-up/down/scale via mock-k8s,
  DeepDesert dimension spin down/up, player-chosen multi-Sietch instances,
  per-sietch heterogeneous config (PvP/PvE, names). (#31–#35)
- Logs tab + single-service restart (#30); tech unlock-all/lock-all with
  optional welcome broadcast (#38, #39); write routes no longer return 200
  on failure behind Cloudflare (#25); FLS reconcile crash that kept removed
  sietches in the in-game browser (#40).

## 2026-06-01 → 2026-06-02 — Admin panel foundations

- dune-admin ports (MIT, reimplemented against our stack): Database tab +
  player/character reads + PlayerGuard (#8); character writes — currency,
  rename, tags, char-XP, keystones, items, faction rep (#9); destructive
  writes — item-delete, reset-spec, account-delete (#10).
- Live server status grid (#13); settings/INI reconciliation with
  `GET/POST /api/settings` (#14); settings catalogue re-mapped to real
  cvars extracted from the server binary — 144 binary-verified knobs
  (#15–#17, #21).
- Welcome kits (#18); first SPA tabs — status, settings, welcome kits
  (#19); market pricing engine + read-only market view (#20).
- Boot fixes: deterministic `ExtractMapPartitions` (UE5 index-0 crash-loop,
  #12); build mock-k8s off `/tmp` to survive Wings' 100 MiB tmpfs (#11).

## 2026-05-31 — mock-k8s hardening

- SIGTERM handling, state persistence/restore, LIST bisection, security
  pass (#2); PID-reuse identity check + env override order (#6); ledger I/O
  safety, persist-generation guard, allowlist, deep omit (#4); self-healing
  reconcile loop + `/status` and `/metrics` endpoints (#7).

## 2026-05-26 → 2026-05-28 — Initial release

- Pelican egg + custom runtime image running Funcom's official self-host
  binaries on native Linux Docker — no Hyper-V, no K3s (commit `e374069`
  onward).
- Our own open-source **mock-k8s-go** replacing CubeCoders' closed binary:
  K8s API discovery, BattleGroup CR, ServerSetScale, UE5 spawn-on-demand.
  The egg runs entirely on MIT-licensed code.
- The eight boot-blockers found and fixed: SteamCMD 32-bit TLS, patchelf
  deps, volume ownership, `$STARTUP` eval, ServiceAccount mount under
  ReadonlyRootfs, `/mnt/server` symlink, stale initdb data, AMP anti-tamper.
- First admin commands over the game's RabbitMQ (broadcast, shutdown,
  kick…), console stdin listener, UE5-aware health check.
- Panel variables: server password + display name, then 22 tunable knobs
  via `apply-config.sh`.
- First real player connected and created a character on 2026-05-27.
