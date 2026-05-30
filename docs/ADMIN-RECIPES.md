# Admin recipes — copy-paste examples

Common admin workflows, organized by scenario. All snippets are
intended to be typed into the **Pelican panel Console tab** (the
`admin <subcommand>` prefix triggers `console.sh`'s stdin listener
which routes to `admin-publish.sh`).

For the full command reference + payload schemas, see
[`ADMIN-COMMANDS.md`](./ADMIN-COMMANDS.md). For the item catalogue
grouped by faction and tier (T1..T6 weapons, armor sets, augments,
B1C4 unique drops), see [`ADMIN-TIER-ITEMS.md`](./ADMIN-TIER-ITEMS.md).
For the protocol reverse-engineering, see the `dune-rmq-admin-protocol`
wiki note.

## Look up things first

You almost never want to guess. Run the lookup helpers before any
per-player command:

```text
admin players                 # who's online + their FLS ids
admin pos <player_id>         # current X/Y/Z + ready-to-paste commands
admin vehicles                # all 9 vehicle classes with templates
admin items <search>          # 2558 items, case-insensitive substring
admin skills <search>         # 145 skill modules
```

`admin pos me` is the easiest way to get coordinates for vehicle
spawning or teleport recipes — it outputs the player's current X/Y/Z
plus three pre-formatted `admin teleport` / `admin vehicle` lines
you can copy directly into the console. Example output:

```text
FLS:        DE0BCCAA2501BF22
Map:        HaggaBasin  (partition 1)
Position:   X=100454.19  Y=281732.57  Z=1966.59

Ready-to-paste commands:
  admin teleport me 100454 281733 1967
  admin vehicle  me Sandbike 100454 281733 1967 T3_Boost
  admin vehicle  me OrnithopterLight 100454 281733 2166 T6_Combat
```

`<search>` is matched against both the canonical FName id and the
display name. So `admin items spice` matches both `MelangeSpice`
(display "Spice Melange") and `ContractKirab2SpicePackage1` (display
"Sealed Spice Package").

## New player welcome kit

Help a fresh arrival who's struggling with the early-game grind. Give
them a starter loadout, full hydration, and some XP to spend.

```text
admin broadcast "Welcome!" "First-time grant from the admin team" 10
admin give me MelangeSpice 50
admin give me Stilltent 1
admin water me
admin xp me 5000
```

`me` resolves to the single currently-online account; for multi-player
servers use `admin players` to find the new arrival's FLS id and
substitute it.

## Stuck-player rescue

Player is clipped into terrain, fell through the world, or got stuck
in a vehicle. Two recovery paths:

```text
admin tpsafe me 101000 285000 4300       # nearest navigable XYZ near spawn
admin teleport me 101000 285000 4300 90  # exact XYZ if you have specific coords
```

If they're permanently broken (e.g. their character data is corrupt),
the nuclear option:

```text
admin reset me            # wipes XP+skills, keeps inventory+location
admin clean me            # wipes inventory; combine with reset for full restart
```

Both `reset` and `clean` are destructive. Warn the player first via
`admin broadcast`.

## Vehicle spawning

`admin vehicles` lists the nine classes shipping with Dune: Awakening
and the template variants each supports:

| ClassName | Templates |
|---|---|
| Sandbike | T1_ExtraSeat, T2_Inventory, T3_Boost, T4_Scanner, T5, T6 |
| Buggy | T3_Inventory, T4_Boost, T5_Mining, T6_Combat |
| Tank | T6_CombatFire, T6_CombatDart |
| Sandcrawler | T6_Harvesting |
| OrnithopterLight | T4_Inventory, T5_Boost, T6_Combat |
| OrnithopterMedium | T5_Inventory, T6_Combat |
| OrnithopterTransport | T6_Boost |
| TreadWheel | T4_Passenger, T5_Inventory, T6_Boost |
| ContainerVehicle | Container |

Spawn syntax:

```text
admin vehicle <player_id> <ClassName> <x> <y> <z> <TemplateName> [rotation] [persistent=1.0]
```

To get XYZ near the player, use `admin pos me` (or
`admin pos <fls_id>` for someone else). It prints the current
position **and** three pre-formatted vehicle/teleport commands ready
to paste. Real examples below use coordinates from a live HaggaBasin
session — substitute your own with `admin pos me` first:

```text
admin vehicle me Sandbike 101000 285000 4300 T3_Boost
admin vehicle me Buggy 101000 285000 4300 T6_Combat 90
admin vehicle me OrnithopterLight 101000 285000 4500 T6_Combat
admin vehicle me Sandcrawler 105000 290000 4300 T6_Harvesting 0 1.0
admin vehicle me Tank 101000 285000 4300 T6_CombatFire
```

The optional last argument is `Persistent`:
- `1.0` = vehicle persists across server restart (default)
- `0.0` = transient, despawns on restart or when the player leaves the area

Spawn somewhere the player can walk to — there's no "spawn at my feet"
shortcut, you need real coordinates. The seabass handler does NOT
ground-snap the vehicle; pick a `Z` slightly above terrain or the
vehicle may fall through.

## Inventory grants

`admin items <search>` finds the canonical FName. Then:

```text
admin give <player_id> <ItemFName> [quantity=1] [durability=1.0]
```

Recipes:

```text
admin give me MelangeSpice 1000         # 1000 raw spice
admin give me SolarisCoin 100000        # 100k Solari
admin give me Stilltent 1               # one stilltent
admin give me Crysknife 1               # one crysknife
admin give me Stillsuit_Choam_01_Top 1  # top half of starter stillsuit
```

Durability is 0.0–1.0 (1.0 = pristine). Some items ignore it (consumables).

For weapon augments (`T6_Augment_*`), `qty` lets you stockpile:

```text
admin items "T6_Augment"                # browse the catalogue
admin give me T6_Augment_Acuracy1 5
```

## Progression boosts

```text
admin xp me 25000                       # +25k generic player XP
admin points me 50                      # set unspent skill points to 50
admin skill me Skills.Ability.BattleCry 3  # max out Inspiration (Swordmaster)
```

Skill module ids use the **`Skills.Ability.<Name>`** or
**`Skills.Attribute.<Name>`** format — NOT the `Swordmaster_T1`
shorthand that adainrivers' specs.rs hints at (their hint is from an
older protocol version). Run `admin skills <category>` to browse:

```text
admin skills swordmaster                # Swordmaster track abilities
admin skills bene                       # Bene Gesserit abilities
admin skills mentat                     # Mentat track
admin skills attribute                  # All attribute-track modules
```

## Server-wide announcements

```text
admin broadcast "Maintenance" "Restart in 5 minutes — save your work" 30
admin broadcast "PvP enabled" "Deep Desert is now hostile zone" 60
admin broadcast "Event!" "Sandworm spawn party at 290k 280k in 10 min" 45
```

The third argument is per-pulse display duration (the banner stays
on-screen this long for each viewer). Default is 30 seconds.

## Scheduled restart with player warning

```text
admin shutdown Restart 600 60     # restart in 10 min, broadcast every 60s
admin shutdown Maintenance 1800   # maintenance in 30 min
admin shutdown Update 300 30      # patch restart in 5 min, 30s heartbeat
admin shutdown cancel             # abort the countdown
```

The `freq_secs` argument (default 60) controls how often the
client-visible countdown re-renders. Set lower for short leads
(30s for sub-5min) so players don't miss the warning.

## Moderation toolkit

```text
admin kick steam:76561198041278656            # boot one player by Steam ID
admin kick "*"                                # boot everyone (server-wide)
admin clean DE0BCCAA2501BF22                  # wipe a griefer's inventory
admin reset DE0BCCAA2501BF22                  # full progression wipe
```

The kick is immediate and ungraceful — there's no grace period or
"return to title" UX from the protocol side. Players just disconnect.

## Spice cache for an event prize pool

Build an "admin chest" via repeated grants. There's no native bulk-spawn,
so loop the give command (the wrapper handles each as a separate
publish):

```text
admin give me MelangeSpice 10000
admin give me SolarisCoin 1000000
admin give me Crysknife 5
admin give me Stillsuit_Choam_01_Top 5
admin give me Stillsuit_Choam_01_Mask 5
admin give me Stillsuit_Choam_01_Gloves 5
admin give me Stillsuit_Choam_01_Boots 5
```

Then drop the kit on the ground for a player to collect, or `tpsafe`
the recipient near you.

## Things that DON'T work (kept for protocol parity)

```text
admin exec UnlockAllSkills              # publishes ok, seabass ignores
admin cheat me PlaytestSetupAdmin       # same — handler logs but no state change
```

The handlers exist but Funcom's seabass implementation does not apply
state changes for these. Tracked upstream by adainrivers; we keep them
in the script in case a future EA patch fixes them.

Also doesn't work:

- **Character names** like `Sergentval` — encrypted at rest. Use FLS id, `me`, or `steam:<id>`.
- **`admin skill me Swordmaster_T1 5`** — the adainrivers' helper text in specs.rs is wrong; use the `Skills.Ability.X` / `Skills.Attribute.X` form.
- **Spawning a vehicle "at my feet"** — no spawn-at-self shortcut; pass real XYZ.
- **Mass `admin give "*" <item>`** — `*` works for `kick`/`clean`/`reset` but not for item grants (handler requires a concrete PlayerId).

## Attribution

Data tables (`data/admin/vehicles.json`, `items.json`,
`skill-modules.json`) bundled MIT-licensed from
[adainrivers/dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager).
Their reverse-engineering of the protocol + the catalogue values is
the canonical source; we ship copies for offline-friendly lookup
inside the container. See [`ATTRIBUTION.md`](../ATTRIBUTION.md).
