# Self-host admin surface — feedback for Funcom

A focused list of admin actions the self-host server-command surface
(`UDuneServerCommandSubsystem` over the `heartbeats:notifications`
AMQP exchange) does not currently expose, plus commands that publish
successfully but only half-apply state due to server-side hardcoded
fields. Intended as constructive feedback for the Dune: Awakening
team — every item below maps to a concrete one-or-two-line change in
the seabass dispatch table or the relevant command handler.

The full set of accepted commands today is 14
(`AddItemToInventory`, `ServiceBroadcast`, `KickPlayer`,
`CleanPlayerInventory`, `ResetProgression`,
`UpdateAllWaterFillables`, `AwardXP`, `SkillsSetModuleLevel`,
`SkillsSetUnspentSkillPoints`, `TeleportTo`, `TeleportToExact`,
`SpawnVehicleAt`, `CheatScript`, `ServerExec`). Everything below
either does not exist or does not behave the way self-host admins
would reasonably expect from the command name.

## How this list was built

For each candidate `ServerCommand` name, a minimal envelope was
published via the working `heartbeats:notifications` route. The
per-Sietch `LogDuneServerCommands` log was inspected within five
seconds for one of:

- `Now running ServerCommand '<name>'` → accepted
- `Deserialized message has unknown Server Command '<name>'` → rejected

35 plausible names were tested on the 2026-05-28 Early Access build;
the 14-entry accepted list above is the complete whitelist. Candidate
names came from `strings` over the UE5 dedicated-server binary
(`DuneSandbox/Binaries/Linux/DuneSandboxServer-Linux-Shipping`),
filtered for PascalCase `<verb><Noun>` patterns plausible as
admin actions.

The underlying C++ UFUNCTIONs for most of the missing commands are
already present in the binary — the dispatch wiring is the gap, not
the implementation.

---

# Section 1 — Commands that don't exist (please add)

## 1.1 Player health / life cycle

| Suggested `ServerCommand` | Existing C++ underpinning | What it would do |
|---|---|---|
| `HealPlayer` / `RestoreHealth` | `DamageableActorComponent::m_CurrentMaxHealth`, `ADuneCharacter` health getters | Restore a player's HP without consumables |
| `SetCharacterHealth` / `SetPlayerHealth` | same | Set HP to an exact value |
| `ReviveCharacter` | `ADuneCharacter::Revive` (UFUNCTION present) | Revive a downed/dead player at their last position |
| `KillCharacter` | `ADuneCharacter::KillCharacter` (UFUNCTION present) | Force-kill a target (e.g. unstuck from an invalid state) |
| `Respawn` | character respawn path | Force-respawn at the player's bound bed/Sietch |
| `AddBuff` | `UGameplayEffect` / Gameplay Ability System | Apply a temporary status effect or gameplay tag |

**Why this matters**: when a player is stuck (clipped into terrain,
ragdolled in an unrecoverable pose, or HP-drained but not dead),
admins currently have no surgical fix. The only available path is
"give them consumables and hope they can use them", which fails if
the player is in a state where they can't open the inventory wheel.

## 1.2 Player hydration meter

| Suggested `ServerCommand` | What it would do |
|---|---|
| `RestoreHydration` / `SetHydration` / `FullHydration` | Refill the player's own hydration meter directly |

The existing `UpdateAllWaterFillables` is **not** a substitute — it
refills jerrycans and stills the player is carrying, but the
player's own hydration bar is unaffected. A player about to die of
dehydration on a server-wide event start (e.g. operator just teleported
everyone to the Deep Desert for a PvP event) has no admin-recoverable
path other than killing them and hoping they respawn elsewhere.

## 1.3 Currency / Solari wallet

| Suggested `ServerCommand` | What it would do |
|---|---|
| `AwardCurrency` / `AwardSolari` / `SetSolari` | Credit or set a player's Solari wallet balance |

Today the only way to grant Solari via the admin surface is to drop
`SolarisCoin` item tokens into the player's inventory via
`AddItemToInventory`, then have them pick the tokens up — clumsy for
bulk grants and inconsistent UX for event prize pools. A proper
wallet-credit command would also avoid the inventory-slot pressure
that comes from dropping 100k+ coin tokens.

## 1.4 Faction / Landsraad reputation

| Suggested `ServerCommand` | What it would do |
|---|---|
| `AddFactionReputation` | Increment a player's standing with a specific House |
| `SetFaction` | Set the player's primary faction outright |

The current workaround is to grant `AtreidesReputation` /
`HarkonnenReputation` / `SmugglersReputation` items via the `give`
command, which works because the reputation system happens to
consume those items as standing-grant fuel. A direct command would
remove the indirection and let admins set absolute values rather
than incrementing.

## 1.5 Vehicle / actor cleanup

| Suggested `ServerCommand` | What it would do |
|---|---|
| `DespawnVehicle` / `RemoveVehicle` | Despawn an abandoned, stuck, or unrecoverable vehicle by actor id |
| `DespawnActor` / `DeleteActor` | More general actor cleanup (for placeables, dropped items, etc.) |
| `KillActor` / `DestroyActor` | Same intent — different verbs in case the chosen name conflicts internally |

**Why this matters**: `SpawnVehicleAt` exists and works, which means
admins regularly spawn vehicles. But there is no inverse. Vehicles
pile up over the lifetime of a server — failed event setups, players
who quit mid-drive, vehicles clipped into rock, etc. Currently the
only way to clean them up is to edit the `dune.actors` postgres
table directly, which is risky and not something every operator
can do safely.

## 1.6 Schematics / unlocks

| Suggested `ServerCommand` | What it would do |
|---|---|
| `AddSchematic` | Unlock a specific schematic by name |
| `AddAllAvailableSchematics` | Unlock every schematic appropriate for the player's progression |
| `AddCharacterUnlockedCustomizationAll` | Unlock cosmetics / customisation entries |

The current workaround is to grant `<Item>_Schematic` items via
`AddItemToInventory`, which works but requires admins to know every
schematic name. A bulk unlock command would help with character
testing, event setups, and "give the new player a fair starting
loadout" workflows.

## 1.7 Skills

| Suggested `ServerCommand` | What it would do |
|---|---|
| `SkillsUnlockAll` | Max every skill module in one call |
| `SkillsRespec` / `SkillsResetRespecTimer` | Trigger a player respec / clear the respec cooldown |
| `UnlockAllAbilities` | Unlock every ability node |
| `ResetCooldowns` | Clear all active ability cooldowns |
| `SkillsToggleCheatProgression` | Toggle dev-test progression mode |

`SkillsSetModuleLevel` and `SkillsSetUnspentSkillPoints` cover most
of the slot, but bulk operations require admins to loop over every
module — currently a 100+ command session for a full unlock.

## 1.8 Story / journey

| Suggested `ServerCommand` | What it would do |
|---|---|
| `CompleteStoryNodeByName` | Mark a specific story node complete |
| `ChangeSpiceAddictionStatus` | Cure or apply spice addiction debuff |
| `Wormify` | Force-trigger a sandworm encounter at a location |

`Journey*` family commands exist in the seabass dispatch table (see
Section 2), but the handlers don't actually change state. A working
`CompleteStoryNodeByName` would unstick players whose contracts are
broken (a recurring support issue) without database surgery.

## 1.9 Character state mods

| Suggested `ServerCommand` | What it would do |
|---|---|
| `AddCharacterStatModifierFloat` | Apply a temporary stat buff (movement speed, damage, etc.) |
| `AddCharacterGameplayTag` | Set or clear a gameplay tag on the character |
| `ServerSetCheats` | Toggle dev-test cheat flags |

These are useful for testing balance changes, running custom events,
or temporarily granting an effect for narrative purposes.

---

# Section 2 — Commands that publish but only partially work

## 2.1 `AddItemToInventory` — `Quality` is hardcoded to 0

**Symptom**: the payload accepts `ItemName` / `Quantity` / `Durability`
fields. There is no accepted `Quality` field, and the server-side
handler appears to hardcode the persisted `quality_level` to 0
regardless of what the payload contains.

**Result**: admins cannot grant graded T1-T5 items. A
`{ItemName: "AtreLMG5", Quality: 5}` payload still produces an
ungraded T5 weapon.

**What we'd suggest**: extend the handler to read an optional
`Quality` field (default 0 for back-compat), validate to the
documented 0-5 range, and pass it through to the `dune.items`
INSERT. The schema already has the `quality_level` column —
direct database edits with values 0-5 do work in-game and 6+
clamps to 5 (verified by a community report on adainrivers'
manager, issue #12).

## 2.2 `AwardXP` — `Category` field required but value ignored

**Symptom**: the seabass handler does a presence check on `Category`
before applying the XP grant. Without the field, the dispatch fires
(`LogDuneServerCommands: Now running ServerCommand 'AwardXP'`) but
the player receives no XP. With the field present, the player
receives generic XP regardless of which category value was sent.

| Payload | Dispatch logged? | XP applied? |
|---|---|---|
| `{Experience: 1000}` | yes | no (silent no-op) |
| `{Experience: 1000, Category: "Combat"}` | yes | yes, generic XP |
| `{Experience: 1000, Category: "Trading"}` | yes | yes, generic XP (Trading ignored) |

**What we'd suggest**: either route XP into the actual named category
(if that's the intent), or drop the `Category` field as a required
guard and document that AwardXP always grants generic XP.

## 2.3 `UpdateAllWaterFillables` — narrower than the name implies

**Symptom**: refills jerrycans, stills, and other carried fillable
containers. The player's own hydration meter is unaffected.

**What we'd suggest**: either rename to `RefillCarriedContainers`
(matches the actual behaviour), or extend to also restore the
hydration meter. A combined `Hydrate` command (refills containers AND
restores hydration bar to full) would cover the operator intent that
the current name implies.

## 2.4 `CheatScript` — accepted, never executes

**Symptom**: `LogDuneServerCommands: Now running ServerCommand 'CheatScript'`
fires on dispatch, but the named `[CheatScript.<name>]` block from
`DefaultGame.ini` is never resolved or executed.

**What we'd suggest**: either wire the handler to resolve and execute
the named script block, or remove the command from the accepted list
so operators get a clean rejection rather than a silent success.

## 2.5 `ServerExec` — accepted, never executes

**Symptom**: same shape as `CheatScript`. The `Exec` field accepts
arbitrary strings; the handler logs the dispatch but discards the
body. No console command is executed.

**What we'd suggest**: same as 2.4 — wire or remove. A working
`ServerExec` would be very useful (it covers the gap for most of
the missing commands in Section 1), but a non-working accepted
command is worse than a clean rejection because it hides the gap.

## 2.6 `Journey*` family — handlers fire, no state change

**Symptom**: `JourneySetCheckpoint`, `JourneyCompleteStep`, and
related commands appear in the seabass dispatch (so they're not in
the "unknown" bucket), but they don't update any visible journey or
story state. Affected community manager tools (adainrivers'
`dune-dedicated-server-manager`) retired these from their UI on
2026-05-26 after live-testing confirmed the no-op.

**What we'd suggest**: same wire-or-remove principle.

## 2.7 `AwardXPByEventTag` — half-implemented

**Symptom**: the binary has `ADuneCharacter::AwardXPByEventTag` as a
C++ UFUNCTION, but the seabass dispatch returns
`unknown Server Command 'AwardXPByEventTag'`. The implementation
exists; the dispatch entry is missing.

**What we'd suggest**: add the dispatch entry. Appears to be a
one-line fix on Funcom's side.

---

# Section 3 — Full negative-result list

For future game-version retests — each of these returned
`unknown Server Command` on the 2026-05-28 Early Access build. If any
start being accepted on a future patch, that's a signal Funcom has
expanded the dispatch table and this document should be updated.

```
HealPlayer                            RestoreHealth
SetCharacterHealth                    SetPlayerHealth
ReviveCharacter                       KillCharacter
RestoreHydration                      SetHydration
FullHydration                         AwardCurrency
AwardSolari                           SetSolari
AddFactionReputation                  SetFaction
AddBuff                               Respawn
ResetCooldowns                        UnlockAllAbilities
SkillsUnlockAll                       SkillsRespec
SkillsResetRespecTimer                SkillsToggleCheatProgression
AddSchematic                          AddAllAvailableSchematics
AddBasicInventoryToCharacter          AddItemsToInventory
AddWeaponToInventory                  AddCharacterUnlockedCustomizationAll
ChangeSpiceAddictionStatus            AddCharacterStatModifierFloat
AddCharacterGameplayTag               Wormify
CompleteStoryNodeByName               JourneyCompleteStoryNode
ServerSetCheats                       AwardXPByEventTag
```

---

# Why this matters for the community

Self-host server operators are running events, supporting players
who get stuck, and stewarding small-population community servers.
The commands above are not "cheat the official game" requests —
they're the operational toolkit any private-server admin team needs
to keep a community running smoothly. The biggest single ask in
Section 1 is the vehicle-cleanup family (1.5); the biggest fix in
Section 2 is the hardcoded `Quality=0` on `AddItemToInventory` (2.1),
since it blocks a feature class players can already see in the game
(graded gear) from being grantable.

Most of the suggested commands map to UFUNCTIONs the binary already
exports. The work on Funcom's side is primarily dispatch-table
wiring plus standard FLS auth checks — not new gameplay code.

If any of the partially-working commands are intentional design
choices that aren't documented externally, a public note (e.g. on a
self-host wiki page) would also be welcome — admins are currently
guessing at intent from binary strings.

## Reference

Protocol shape and the original 14-command accepted catalogue are
documented in the open-source admin tool from adainrivers
(`dune-dedicated-server-manager`, MIT licensed,
<https://github.com/adainrivers/dune-dedicated-server-manager>).
The 35-candidate sweep above expands on their work to confirm the
boundaries of the accepted whitelist.
