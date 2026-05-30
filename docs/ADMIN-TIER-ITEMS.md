# Tier items — copy-paste catalogue

Practical reference for the `admin give` command grouped by item
family and tier. Every line is a real `admin <subcommand> [args]`
invocation you can paste straight into the **Pelican panel Console
tab** (or send via the admin web UI's Item Search).

For the full command surface see [`ADMIN-COMMANDS.md`](./ADMIN-COMMANDS.md);
for scenario walkthroughs see [`ADMIN-RECIPES.md`](./ADMIN-RECIPES.md).

> Tip: the catalogue below is a curated subset of the 2 558 entries in
> `data/admin/items.json`. To browse the full list inside the panel,
> run `admin items <search>` (case-insensitive substring match against
> id + display name). The admin web UI's Items tab also exposes the
> same dataset with category filters and tier auto-detection.

## How tiers are encoded in item ids

Funcom uses three overlapping naming conventions. The community wiki
sometimes shows "T1/T2/T3" labels in-game; the internal `ItemFName`
that `admin give` needs may use a different format:

| Pattern | Where it appears | Example |
|---|---|---|
| Numeric suffix `1..6` | Faction weapon families | `AtreLMG1` (T1) → `AtreLMG5` (T5) |
| `_T<n>_` infix | NPC / non-player weapons | `Rifle_Long_T3_NPC`, `Heavy_Flamethrower_T2_NPC` |
| `_T<n>` suffix | Augments, schematics | `T6_Augment_Acuracy1`, `Schematic_UniqueBattleRifle` |
| `01..06` set index | Armor sets | `Combat_Choam_Heavy01..06` |
| `Mk<n>` | Variants of one base item | `HighCapacityLiterjon` (`Hajra Literjon Mk1`) |
| Unique named | One-of-a-kind drops | `B1C4_Unique_Dirk2` (`Eel's Tooth`) |

Higher number = higher tier in nearly every family. Browse with
`admin items <faction>` (e.g. `admin items atre`, `admin items hark`,
`admin items smug`) to confirm the spelling before pasting.

## Currency, spice, fuel

```text
admin give me SolarisCoin   1000000     # 1M Solari (cap money)
admin give me MelangeSpice  10000       # raw spice
admin give me SpiceSand     500         # unprocessed spice sand
admin give me SpiceResidue  200         # spice residue
admin give me SpicedFuelCell 50         # vehicle / generator fuel
```

## Weapons by faction and tier

### Atreides — LMG family

```text
admin give me AtreLMG1 1        # Atreides LMG (T1)
admin give me AtreLMG2 1        # Atreides LMG MK I (T2)
admin give me AtreLMG3 1        # House Vulcan GAU-92 (T3)
admin give me AtreLMG4 1        # Adept Vulcan GAU-92 (T4)
admin give me AtreLMG5 1        # Regis Vulcan GAU-92 (T5)
```

### Atreides — SMG family

```text
admin give me AtreSmg2 1        # Standard Disruptor M11 (T2)
admin give me AtreSmg3 1        # Artisan Disruptor M11 (T3)
admin give me AtreSmg4 1        # House Disruptor M11 (T4)
admin give me AtreSmg5 1        # Adept Disruptor M11 (T5)
admin give me AtreSmg6 1        # Regis Disruptor M11 (T6)
```

### Harkonnen — `Hark<weapon><N>` follows the same pattern

```text
admin items hark                # browse Harkonnen weapon families
admin give me HarkPistol5 1     # T5 Harkonnen pistol (substitute family + tier)
```

### Smuggler — Shot / DMR / Spitdart families

```text
admin give me SmugShot3 1       # House Drillshot FK7 (T3)
admin give me SmugShot4 1       # Adept Drillshot FK7 (T4)
admin give me SmugShot5 1       # Regis Drillshot FK7 (T5)
admin give me SmugDmr3 1        # Artisan JABAL Spitdart (T3)
admin give me SmugDmr4 1        # House JABAL Spitdart (T4)
admin give me SmugDmr5 1        # Adept JABAL Spitdart (T5)
admin give me SmugDmr6 1        # Regis JABAL Spitdart (T6)
```

## Unique / legendary weapons (B1C4 contract drops)

Story-locked weapons. Granting them via admin bypasses the contract;
they work in-hand but may cause inventory weirdness if the player
hasn't unlocked the underlying schematic.

```text
admin give me B1C4_Unique_Dirk2          1   # Eel's Tooth
admin give me B1C4_Unique_DualBlades1    1   # Leech's Maw
admin give me B1C4_Unique_HarkAr2        1   # The Angry Adder
admin give me B1C4_Unique_HeavyPistol2   1   # Hell-Fury Pistol
admin give me B1C4_Unique_Kindjal2       1   # Serpent's Fang
admin give me B1C4_Unique_LMG2           1   # The Hateful Barker
admin give me B1C4_Unique_Rapier2        1   # Prescient Edge
admin give me B1C4_Unique_SMG2           1   # Spitting Cobra
admin give me B1C4_Unique_SmugDmr1       1   # Blind Fury
```

To grant the underlying schematic (legitimate unlock):

```text
admin give me B1C4_Unique_Dirk2_Schematic     1
admin give me B1C4_Unique_HarkAr2_Schematic   1
admin give me B1C4_Unique_LMG2_Schematic      1
```

## Melee — knives, swords, rapiers

```text
admin give me Crysknife_CR     1         # Crysknife (player-craftable variant)
admin give me UniqueSword_02   1         # Shock-sword
admin give me UniqueSword_03   1         # Spark-sword
admin give me UniqueSword_04   1         # Jolt-sword
admin give me UniqueSword_05   1         # Replica Pulse-sword
admin give me UniqueRapier_02  1         # Kharet Viper
admin give me UniqueRapier_03  1         # Halleck's Pick
admin give me UniqueDirk_02    1         # Denira's Gift
admin give me UniqueDirk_03    1         # Moisture Sealer
admin give me UniqueDirk_04    1         # Cauterizer
```

## Armor sets — five pieces each

Stillsuits and combat armor ship as five-piece sets: Boots, Bottom,
Gloves, Helmet, Top (plus Mask for stillsuits). The set base is
`<Family>_<Faction>_<Type><NN>` and each piece appends its slot.

### Choam stillsuits (early-game, hydration-focused)

```text
admin give me Stillsuit_Choam_01_Top    1   # Slaver Stillsuit Body
admin give me Stillsuit_Choam_01_Bottom 1
admin give me Stillsuit_Choam_01_Boots  1
admin give me Stillsuit_Choam_01_Gloves 1
admin give me Stillsuit_Choam_01_Mask   1
```

Variants `_02` (Kirab), `_04` (Native) use the same five-piece pattern.

### Choam Heavy combat armor (T1 set)

```text
admin give me Combat_Choam_Heavy01_Top    1
admin give me Combat_Choam_Heavy01_Bottom 1
admin give me Combat_Choam_Heavy01_Boots  1
admin give me Combat_Choam_Heavy01_Gloves 1
admin give me Combat_Choam_Heavy01_Helmet 1
```

Substitute `Heavy02` … `Heavy06` for higher-tier sets. The `04` set
has no `_Boots` piece — Funcom data gap, not a typo on our end.

### Other set families (same pattern)

```text
admin items "Combat_Choam_Light"      # browse light armor T1..T6
admin items "Combat_Choam_Scout"      # scout armor variants
admin items "Combat_Hark"             # Harkonnen combat sets
admin items "Combat_Atre"             # Atreides combat sets
```

## Consumables — food, water, medical

```text
admin give me Literjon              1    # standard water container
admin give me HighCapacityLiterjon  1    # Hajra Literjon Mk1 (larger)
admin give me Dew                   20   # raw morning dew (water)
admin give me HealthPack_Channeled  10   # Healkit
admin give me WaterPack_Consumable  20   # Cup of Water
admin give me AntiRadiationPill     30   # Iodine Pill
admin give me SaphoJuice            5    # Mentat focus drink
admin give me Bloodsack_01          5    # Small Blood Sack
```

Spice-addiction consumables (Bene Tleilax / vendor drops):

```text
admin give me SpiceAddictionConsumable_01 1   # Melange Spiced Food
admin give me SpiceAddictionConsumable_02 1   # Melange Spiced Beer
admin give me SpiceAddictionConsumable_03 1   # Melange Spiced Coffee
admin give me SpiceAddictionConsumable_04 1   # Melange Spiced Wine
```

## Survival gear

```text
admin give me Stilltent           1   # personal stilltent
admin give me Stilltent_Schematic 1   # the recipe (preferred — survives wipe)
admin water me                       # refill ALL water containers carried
```

## Schematics (unlock recipes the legit way)

Granting a schematic lets the player craft the item themselves — usually
the preferred path over directly handing them the gear. Browse with
`admin items <name>Schematic`.

```text
admin give me Flamethrower1Schematic              1
admin give me ChoamHeavyLasgunSchematic           1
admin give me MiningTool_1h_StandardSchematic     1
admin give me HealthPackSchematic                 1
admin give me Schematic_UniqueBattleRifle         1
```

## Augments and modules

Augments slot into weapons; `_T6_Augment_*` is the endgame tier:

```text
admin items "T6_Augment"                  # browse the catalogue
admin give me T6_Augment_Acuracy1 5       # +accuracy mod, qty 5
```

## Bundled "starter kit" example

Drop everything below to give a fresh player a full T2 loadout with
water + currency:

```text
admin give me SolarisCoin 50000
admin give me MelangeSpice 50
admin give me AtreSmg2 1
admin give me Crysknife_CR 1
admin give me Stillsuit_Choam_01_Top 1
admin give me Stillsuit_Choam_01_Bottom 1
admin give me Stillsuit_Choam_01_Boots 1
admin give me Stillsuit_Choam_01_Gloves 1
admin give me Stillsuit_Choam_01_Mask 1
admin give me HighCapacityLiterjon 1
admin give me HealthPack_Channeled 5
admin water me
admin xp me 5000
```

For an event prize pool with T6 gear:

```text
admin give me SolarisCoin 1000000
admin give me MelangeSpice 10000
admin give me AtreLMG5 1
admin give me AtreSmg6 1
admin give me SmugDmr6 1
admin give me UniqueSword_05 1
admin give me Combat_Choam_Heavy06_Top 1
admin give me Combat_Choam_Heavy06_Bottom 1
admin give me Combat_Choam_Heavy06_Boots 1
admin give me Combat_Choam_Heavy06_Gloves 1
admin give me Combat_Choam_Heavy06_Helmet 1
admin give me T6_Augment_Acuracy1 5
```

## Durability override

`admin give` accepts a fourth argument: `durability` ∈ `[0.0, 1.0]`,
default `1.0` (pristine). Use lower values for "found in the wild"
realism:

```text
admin give me AtreLMG5 1 0.65       # 65% durability LMG
admin give me Crysknife_CR 1 0.20   # heavily worn knife
```

Consumables and stack-only items ignore the durability arg.

## Caveats

- **Names with spaces or quotes**: wrap the title arg of `broadcast` /
  `give` (when the item name field is part of payload) in shell-style
  quotes. Internal ItemFNames never contain spaces.
- **Bulk grants for `*` (all online)** don't work for `give` — the
  seabass handler requires a concrete PlayerId. Loop per player.
- **Schematics vs. items**: granting `<X>_Schematic` makes the player
  able to craft `<X>`; granting `<X>` directly gives them the item
  bypassing the unlock requirements. Both paths work but a player who
  receives the item without the schematic can't re-craft it after a
  death-loss.
- **B1C4 uniques**: these are contract-locked story items. Giving them
  to a player who hasn't reached the relevant contract step works but
  may produce confusing quest-log entries later.

## Attribution

Item dataset (`data/admin/items.json`) bundled MIT-licensed from
[adainrivers/dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager).
The 2 558-row table is the canonical source of truth for `ItemFName`
values that Funcom's `AddItemToInventory` ServerCommand accepts. See
[`ATTRIBUTION.md`](../ATTRIBUTION.md).
