# Admin command gaps — what Funcom hasn't shipped + what works partially

Companion to [`ADMIN-COMMANDS.md`](./ADMIN-COMMANDS.md) (full reference of
working commands). This doc catalogues:

1. **Missing commands** — server actions admins routinely want that
   Funcom did not expose via the AMQP `UDuneServerCommandSubsystem`
   (seabass) handler. We tested 35 plausible names against a live
   server on 2026-05-28; none matched.
2. **Partially-working commands** — handlers that publish successfully
   but the seabass implementation drops, ignores, or only half-applies
   the state change.
3. **Workarounds** — postgres-direct paths or composite item grants
   that achieve the intent through a different surface.

If you're a Funcom engineer reading this: the items in section 1 are
**candidates worth wiring into seabass**, since the underlying UE5
C++ methods already exist (`ADuneCharacter::HealPlayer`,
`ADuneCharacter::AwardXPByEventTag`, etc. — confirmed by binary
strings). Most are one new case in the `UDuneServerCommandSubsystem`
dispatch + the standard FLS auth check away from being live.

## How we know this list is exhaustive

The audit (2026-05-28) ran in two batches:

1. **Strings-mine** the UE5 dedicated-server binary
   (`DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping`)
   for PascalCase identifiers matching `<verb><Noun>` patterns
   plausible as ServerCommand names.
2. For each candidate, publish a minimal
   `{ServerCommand: "<name>", PlayerId: "<test-fls-id>"}` envelope
   via the working `heartbeats:notifications` exchange.
3. Grep each Sietch's UE5 log within 5 seconds of publish for
   either `Now running ServerCommand 'X'` (accepted) or
   `Deserialized message has unknown Server Command 'X'` (rejected).

All 35 candidates landed in the "unknown" bucket. The accepted set is
the 14-entry whitelist in
[`ADMIN-COMMANDS.md`](./ADMIN-COMMANDS.md).

---

## 1. Missing commands (no AMQP path exists today)

### Player health / life cycle

| Candidate name(s) tested | What it would do |
|---|---|
| `HealPlayer`, `RestoreHealth`, `SetCharacterHealth`, `SetPlayerHealth` | Restore a player's HP bar without consumables |
| `ReviveCharacter` | Revive a downed / dead player at their last position |
| `KillCharacter` | Kill a target (e.g. unstuck them from invalid state) |
| `Respawn` | Force-respawn at the player's bound bed / Sietch |
| `AddBuff` | Apply a temporary status effect / gameplay tag |

**Workaround**: hand the player a healing consumable and ask them to
use it. The egg's web UI ships a "Heal" kit (Healkits Mk1-Mk6 +
Bloodsacks) in the Kits tab.

```text
admin give me HealthPack_Channeled 5
admin give me Bloodsack_T6 3
```

### Hydration bar (player's own water level)

| Candidate name(s) tested | What it would do |
|---|---|
| `RestoreHydration`, `SetHydration`, `FullHydration` | Refill the player's own hydration bar |

Note: `UpdateAllWaterFillables` exists and works, but it only refills
**jerrycans and stills the player is carrying** — it does NOT touch
the player's own hydration meter.

**Workaround**: grant a drinkable item. The "Hydrate" preset kit in
the web UI does this.

```text
admin give me WaterPack_Consumable 5
admin give me Literjon 1
```

### Currency / Solari

| Candidate name(s) tested | What it would do |
|---|---|
| `AwardCurrency`, `AwardSolari`, `SetSolari` | Credit a player's Solari wallet directly |

**Workaround**: grant `SolarisCoin` items via `AddItemToInventory`.
Note that this drops coin **tokens** into inventory — they convert
to wallet balance on pickup but the UX is clumsy for bulk grants.

```text
admin give me SolarisCoin 100000        # 100k coin tokens
```

A more elegant fix would be a postgres-direct `UPDATE` on whatever
column stores the Solari balance. We have not yet located that
column — see the **Schema-survey-needed** appendix at the bottom.

### Faction / Landsraad reputation

| Candidate name(s) tested | What it would do |
|---|---|
| `AddFactionReputation`, `SetFaction` | Change a player's standing with Atreides / Harkonnen / Smugglers |

**Workaround**: grant the FactionReputation item FNames via
`AddItemToInventory`. The seabass `give` handler accepts these and
the in-game reputation system picks up the item as standing-grant
fuel:

```text
admin give me AtreidesReputation 1000
admin give me HarkonnenReputation 1000
admin give me SmugglersReputation 1000
```

The egg's web UI surfaces this as a dedicated "Give Landsraad
standing" form with House selector + quick amount buttons.

### Vehicle / actor cleanup

| Candidate name(s) tested | What it would do |
|---|---|
| `DespawnVehicle`, `DespawnActor`, `DeleteActor`, `RemoveVehicle`, `KillActor`, `DestroyActor`, `VehicleDespawn`, `RemoveSpawnedVehicle`, `KillVehicle`, `AdminDespawn` | Remove an abandoned / stuck spawned vehicle |

**Workaround**: postgres-direct delete on `dune.actors` with a
class-name whitelist for safety. FK cascades clean
`actor_state` / `inventories` / `base_backup_linked_actors`;
`overmap_players.vehicle_id` is `SET NULL` so the last driver keeps
their overmap row.

```sql
DELETE FROM dune.actors
WHERE id = <actor_id>
  AND class ILIKE ANY (ARRAY[
    '%BP_Sandbike%', '%BP_Buggy%', '%BP_Tank%',
    '%BP_SandCrawler%', '%BP_LightOrnithopter%',
    '%BP_MediumOrnithopter%', '%BP_TransportOrnithopter%',
    '%BP_TreadWheel%', '%BP_ContainerVehicle%'
  ]);
```

Already shipped as `admin vehicle-delete <actor_id>` in the egg.

### Items / schematics

| Candidate name(s) tested | What it would do |
|---|---|
| `AddSchematic`, `AddAllAvailableSchematics` | Unlock a single schematic or every schematic |
| `AddBasicInventoryToCharacter` | Restore a player's starter loadout |
| `AddItemsToInventory` (plural) | Batch grant multiple items in one call |
| `AddWeaponToInventory` | Specialised weapon grant (perhaps with proper Quality) |
| `AddCharacterUnlockedCustomizationAll` | Unlock every cosmetic skin / customisation |

**Workaround for schematics**: grant the schematic items directly:

```text
admin items "Schematic"                          # browse
admin give me B1C4_Unique_Dirk2_Schematic 1
```

**Workaround for batch grants**: loop `give` per item (the egg's
"Kits" tab handles this UX layer).

### Skills

| Candidate name(s) tested | What it would do |
|---|---|
| `SkillsUnlockAll` | Max every skill module in one call |
| `SkillsRespec`, `SkillsResetRespecTimer` | Trigger a respec / clear the cooldown |
| `SkillsToggleCheatProgression` | Toggle dev-test progression mode |
| `UnlockAllAbilities` | Unlock all ability nodes |
| `ResetCooldowns` | Clear all ability cooldowns |

**Workaround**: the working `SkillsSetModuleLevel` and
`SkillsSetUnspentSkillPoints` cover most of the use case. For
"unlock everything", you'd loop `admin skill me <module> <max>` per
module — tedious but possible. The egg's skills lookup
(`admin skills <category>`) lists the modules so you can script the
loop.

### Story / journey

| Candidate name(s) tested | What it would do |
|---|---|
| `CompleteStoryNodeByName`, `JourneyCompleteStoryNode` | Mark a story node complete |
| `ChangeSpiceAddictionStatus` | Cure or apply spice addiction debuff |
| `Wormify` | Force-trigger a sandworm encounter |

**No workaround** at this time — story-progression state lives in
`dune.player_state` columns we haven't fully mapped. See the
**Schema-survey-needed** appendix.

### Character state mods

| Candidate name(s) tested | What it would do |
|---|---|
| `AddCharacterStatModifierFloat` | Apply a temporary stat buff (speed, damage, etc.) |
| `AddCharacterGameplayTag` | Set/clear a gameplay tag on the character |
| `ServerSetCheats` | Toggle dev-test cheat flags |

**No workaround** — these are transient runtime state on the live UE5
character actor. Persisting them requires either Funcom adding the
ServerCommand OR finding the persistence column (likely doesn't
exist — most of these are non-persistent dev tools).

---

## 2. Partially-working commands

These reach the seabass handler successfully (`publish=ok` AND
`Now running ServerCommand` lines fire in the per-Sietch UE5 log)
but the in-game effect is incomplete, conditional, or absent.

### `AddItemToInventory` — Quality field hardcoded to 0

The payload accepts an `ItemName` + `Quantity` + `Durability`, but
the seabass handler **discards any `Quality` field** in favour of a
hardcoded `0`. Players who want a graded T1-T5 item end up with an
ungraded item.

| What the AMQP path does | What you actually want |
|---|---|
| Grant `AtreLMG5` quantity 1, Quality=0 (ungraded T5 LMG) | Grant `AtreLMG5` quantity 1, Quality=3 (graded T3 of T5 LMG) |

**Workaround (verified by adainrivers issue #12 reporter)**: skip
the AMQP path and `INSERT` directly into `dune.items` with the
`quality_level` column set:

```sql
INSERT INTO dune.items (
    inventory_id, stack_size, position_index, template_id,
    is_new, acquisition_time, stats, quality_level
) VALUES (
    <backpack_inventory_id>, <qty>, <free_slot>, '<ItemFName>',
    TRUE, EXTRACT(EPOCH FROM now())::int8, '{}'::jsonb, <0-5>
);
```

Where `backpack_inventory_id` is obtained by joining
`dune.player_state.player_pawn_id → dune.inventories.actor_id
WHERE inventory_type = 0`. The reporter confirmed values 0-5 work;
the game clamps higher values to 5.

Not yet shipped in the egg — would need a new `give-graded`
subcommand. Tracked in the issue #12 thread upstream.

### `AwardXP` — `Category` field required but value ignored

The seabass handler does `if payload.has("Category")` as a presence
check. **Without** the field: silent no-op (logs `Now running`,
applies nothing). **With** the field: always grants generic player
XP regardless of what the value says.

| Sent | Received | Applied |
|---|---|---|
| `{Experience: 1000}` | dispatch logged | NOTHING (silent no-op) |
| `{Experience: 1000, Category: "Combat"}` | dispatch logged | +1000 generic XP |
| `{Experience: 1000, Category: "Trading"}` | dispatch logged | +1000 generic XP (Trading ignored) |

Our wrapper auto-injects `Category: "Combat"` so operators never have
to think about it.

### `UpdateAllWaterFillables` — refills containers, not the bar

Refills jerrycans, stills, and similar carried containers. The
**player's own hydration meter** is unaffected. If the player is
about to dehydrate, this command alone won't save them — they have
to manually drink from a refilled container, OR consume a water item
directly.

Workaround: grant `WaterPack_Consumable` (drinks immediately) +
the fillable refill.

### `CheatScript` — accepted, never executes

```text
admin cheat me PlaytestSetupAdmin
```

The seabass handler logs `Now running ServerCommand 'CheatScript'`
but the script body is never resolved. Kept in our wrapper for
protocol parity in case a future EA patch wires it up.

### `ServerExec` — accepted, never executes

Same shape as `CheatScript`. The `Exec` field accepts arbitrary
strings; the handler logs the dispatch and discards the body.

### `Journey*` family — handlers fire, no state change

`JourneySetCheckpoint`, `JourneyCompleteStep`, etc. The handlers
exist in the seabass dispatch table (so they're not in the "unknown"
bucket) but they don't update any visible journey / story state.
adainrivers retired their UI exposure of these on 2026-05-26 after
live testing.

### `AwardXPByEventTag` — half-existent

The binary has `ADuneCharacter::AwardXPByEventTag` as a C++
UFUNCTION, but the seabass dispatch returns `unknown Server Command`.
So the implementation exists but no MQ handler is wired. A potential
"fix in one line" on Funcom's side.

---

## 3. The full negative-result list

For future contributors who want to retry these on new game versions —
each one was tested and returned `unknown Server Command` on EA build
as of 2026-05-28:

```
HealPlayer                  RestoreHealth               SetCharacterHealth
SetPlayerHealth             ReviveCharacter             KillCharacter
RestoreHydration            SetHydration                FullHydration
AwardCurrency               AwardSolari                 SetSolari
AddFactionReputation        SetFaction                  AddBuff
Respawn                     ResetCooldowns              UnlockAllAbilities
SkillsUnlockAll             SkillsRespec                SkillsResetRespecTimer
SkillsToggleCheatProgression AddSchematic               AddAllAvailableSchematics
AddBasicInventoryToCharacter AddItemsToInventory        AddWeaponToInventory
AddCharacterUnlockedCustomizationAll ChangeSpiceAddictionStatus
AddCharacterStatModifierFloat AddCharacterGameplayTag    Wormify
CompleteStoryNodeByName     JourneyCompleteStoryNode     ServerSetCheats
```

If any of these start being accepted on a new game patch, please
update this list and the [`ADMIN-COMMANDS.md`](./ADMIN-COMMANDS.md)
catalogue.

---

## Appendix — Schema survey still needed

Three workaround paths exist *in theory* via postgres direct write,
but we haven't located the columns:

1. **Player health bar** — UE5 has
   `DamageableActorComponent::m_CurrentMaxHealth` as a per-actor
   field, but we haven't found the persisted postgres column. May
   live in `dune.actor_state` (snapshot at logoff) or a sibling
   table. Worth a `\d+` on a live database to confirm.
2. **Player hydration meter** — same situation; Funcom must persist
   it somewhere for character resume across logout.
3. **Solari wallet balance** — likely a column on `dune.player_state`
   or `dune.encrypted_player_state`. Upstream's `queries.rs` reads
   only a subset of `player_state` columns; the wallet column is not
   yet referenced anywhere we've audited.

A focused schema survey command (run against the live container's
postgres) would resolve all three:

```bash
docker exec <container> bash -c '
  source /home/container/scripts/lib.sh /home/container
  "$EXTRACTED/postgres/usr/local/bin/psql" -h "$RUNTIME/postgresql" \
    -p 15432 -U dune -d dune -c "
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = '\''dune'\''
  AND (column_name ILIKE '\''%health%'\''
    OR column_name ILIKE '\''%hydration%'\''
    OR column_name ILIKE '\''%solari%'\''
    OR column_name ILIKE '\''%currency%'\''
    OR column_name ILIKE '\''%spice_addiction%'\''
    OR column_name ILIKE '\''%reputation%'\''
    OR column_name ILIKE '\''%journey%'\''
    OR column_name ILIKE '\''%story_node%'\'')
ORDER BY table_name, column_name;
"'
```

When the schema survey lands, this doc should grow a section listing
which fields are writable + which UPDATE statements safely persist
through a player relog.

## Attribution

Negative-result list, partial-command flags, and the protocol shape
all reverse-engineered with help from
[adainrivers/dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager)
(MIT). The 2026-05-28 35-candidate sweep was performed locally; the
14-entry accepted catalogue matches adainrivers'
`commands/specs.rs` byte-for-byte. See [`ATTRIBUTION.md`](../ATTRIBUTION.md).
