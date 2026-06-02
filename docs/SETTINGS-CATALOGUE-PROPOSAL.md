# Dune server settings catalogue — proposal (Phase 1)
Curated from Funcom's own `DuneSandbox/Config/DefaultGame.ini` (2695 lines, ~140 sections) and **byte-verified against `DuneSandboxServer-Linux-Shipping`**: every key below exists as a real reflected config property in the shipping binary. Sourced default values are Funcom's stock defaults.
- **144 settings** across **20 categories** (48 high-value, 96 medium).
- **21** are struct/array values (advanced — panel renders a raw text field pre-filled with the default).
- All are **server-authoritative** (Funcom flags none of these as needing client-side application). The two known client-gated knobs — landclaim segments, building restrictions — already ship in the schema.
- Sink: `UserGame.ini [/Script/...]` (or the listed module section), applied on **server restart**. Ships `verified:false` until live-tested in-game.

| status | meaning |
|---|---|
| ★ | high operator value |
| ⚙ | struct/array (advanced raw-text) |

## Sandworm  (24, 5 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `EnableBuildingThreatGeneration` | `True` | bool | Enable Building Threat Generation | SandwormSettings |
| ★ | `ThreatScale` | `1.000000` | float | Threat Scale Multiplier | SandwormSettings |
| ★ | `m_EnableSandwormSystem` | `UseAllowList` | string | Enable Sandworm System | SandwormSettings |
| ★ | `m_bEnableDangerZones` | `True` | bool | Enable Sandworm Danger Zones | SandwormSettings |
| ★ | `m_bGiantWormSystemEnabled` | `True` | bool | Enable Giant Worm System | SandwormSettings |
|  | `AirborneThreatDecreasingValuePerSec` | `100.000000` | float | Airborne Threat Decay Per Second | SandwormSettings |
|  | `DefaultMaxThreatScore` | `5000.000000` | float | Default Max Threat Score | SandwormSettings |
|  | `MaxThreatInSafezone` | `0.000000` | float | Max Threat In Safezone | SandwormSettings |
|  | `PlayerShootingRecoilThreatFactor` | `1.000000` | float | Player Shooting Recoil Threat Factor | SandwormSettings |
|  | `RunningThreatPerSec` | `20.000000` | float | Running Threat Per Second | SandwormSettings |
|  | `ShieldingThreatPerSec` | `500.000000` | float | Shielding Threat Per Second | SandwormSettings |
|  | `SprintingThreatPerSec` | `20.000000` | float | Sprinting Threat Per Second | SandwormSettings |
|  | `ThreatDecreaseCooldownInSeconds` | `5.000000` | float | Threat Decrease Cooldown | SandwormSettings |
|  | `ThreatDecreasingValuePerSec` | `0.000000` | float | Threat Decay Per Second | SandwormSettings |
|  | `WWoRThreatPerSec` | `5.000000` | float | Walking-Without-Rhythm Threat Per Second | SandwormSettings |
|  | `WalkingThreatPerSec` | `15.000000` | float | Walking Threat Per Second | SandwormSettings |
|  | `m_GiantWormMinimumPlayersOnSpiceField` | `4` | int | Giant Worm Min Players On Field | SandwormSettings |
|  | `m_GiantWormMinimumSpiceAmountHarvested` | `50000.000000` | float | Giant Worm Min Spice Harvested | SandwormSettings |
|  | `m_GiantWormSpawningCooldown` | `7200.000000` | float | Giant Worm Spawn Cooldown | SandwormSettings |
|  | `m_MinDistanceBetweenSandworms` | `80000.000000` | float | Minimum Distance Between Sandworms | SandwormSettings |
| ⚙ | `m_SpawningAllowedBaseMapList` | `Two entries: HaggaBasin and DeepDesert` | array | Sandworm Spawning Allowed Maps | SandwormSettings |
|  | `m_SpiceBlobLifespan` | `420.000000` | float | Spice Threat Blob Lifespan | SandwormSettings |
| ⚙ | `m_ThreatGeneratedPerSpiceHarvestedMap` | `0.5 threat per harvest for all spice field sizes (L…` | struct | Threat Per Spice Harvested | SandwormSettings |
|  | `m_bEnableHibernation` | `True` | bool | Enable Sandworm Hibernation | SandwormSettings |

## World  (21, 6 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_CycleDurationInDays` | `7` | int | Coriolis Cycle Duration (Days) | CoriolisSubsystem |
| ★ | `m_DayLengthMinutes` | `30.000000` | float | Day Length (minutes) | TimeOfDaySettings |
| ★ | `m_bAreRandomEncountersEnabled` | `True` | bool | Random Encounters Enabled | EncountersSubsystem |
| ★ | `m_bIsDbWipeEnabled` | `True` | bool | Enable Deep Desert DB Wipe on Cycle End | CoriolisSubsystem |
| ★ | `m_bShouldRestartServerOnCycleEnd` | `True` | bool | Restart Server on Cycle End | CoriolisSubsystem |
| ★ | `m_bTimeOfDayEnabled` | `True` | bool | Enable Time of Day Cycle | TimeOfDaySettings |
|  | `m_AuroraProbability` | `25` | int | Aurora Probability (%) | TimeOfDaySettings |
|  | `m_CorpseLifespanInSeconds` | `120.000000` | float | NPC Corpse Lifespan (s) | DuneAISettings |
|  | `m_ForcedCoriolisWorldSeed` | `-1` | int | Forced Coriolis World Seed | CoriolisSubsystem |
| ⚙ | `m_Maps` | `Per-map feature toggles: Taxation, DeepDesertGamepl…` | struct | Per-Map Feature Toggles | MapFeatures |
|  | `m_RandomEncounterInstigationAroundPlayersDelayInSec` | `15.000000` | float | Random Encounter Delay Around Players (s) | EncountersSubsystem |
|  | `m_RandomEncounterInstigationOnWholeServerDelayInSec` | `60.000000` | float | Random Encounter Delay Server-wide (s) | EncountersSubsystem |
|  | `m_SandBuildupMultiplier` | `1.0` | float | Sand Buildup Multiplier | BiomeSettings |
| ⚙ | `m_SpawnTimeSettings` | `(m_TimeOfDayToSpawn=18.0, m_TimeOfDayToDespawn=6.0)` | struct | Patrol Ship Spawn/Despawn Time of Day | PatrolShipSubSystem |
|  | `m_StartTime` | `12.000000` | float | Start Time of Day | TimeOfDaySettings |
|  | `m_bAreEncounterAreaLimitsEnabled` | `True` | bool | Encounter Area Limits Enabled | EncountersSubsystem |
|  | `m_bAreEncounterNodesEnabled` | `True` | bool | Encounter Nodes Enabled | EncountersSubsystem |
|  | `m_bCoriolisTriggerShiftingSands` | `False` | bool | Coriolis Triggers Shifting Sands | SandStormConfig |
|  | `m_bIsRandomEncounterInstigationAroundPlayersEnabled` | `True` | bool | Random Encounters Around Players | EncountersSubsystem |
|  | `m_bIsRandomEncounterInstigationByAreaEnabled` | `True` | bool | Area-based Random Encounters | EncountersSubsystem |
|  | `m_bIsRandomEncounterInstigationOnWholeServerEnabled` | `True` | bool | Server-wide Random Encounters | EncountersSubsystem |

## Building  (17, 2 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★⚙ | `m_DefaultBuildingSystemModifiers` | `RefundPercentage=1.0, PlacementCostMultiplier=1.0` | struct | Default Building System Modifiers | BuildingSettings |
| ★ | `m_DefaultRepairCostMultiplier` | `0.500000` | float | Default Repair Cost Multiplier | BuildingSettings |
|  | `m_BaseBackupToolTimeRestrictionInSeconds` | `604800` | int | Base Backup Tool Cooldown | BuildingSettings |
|  | `m_BuildRange` | `2000.000000` | float | Build Range | BuildingSettings |
|  | `m_BuildingHeightLimitInM` | `980.000000` | float | Building Height Limit (m) | BuildingSettings |
|  | `m_FallbackDefaultBuildingHealth` | `2500.000000` | float | Default Building Health (Fallback) | BuildingSettings |
|  | `m_FallbackDefaultPlaceableHealth` | `400.000000` | float | Default Placeable Health (Fallback) | BuildingSettings |
|  | `m_LandclaimThresholdDistance` | `512.000000` | float | Landclaim Threshold Distance | BuildingSettings |
|  | `m_MaxPermissionsPerActor` | `32` | int | Max Permissions Per Actor | PermissionSettings |
|  | `m_PersistenceDelayDefault` | `15.000000` | float | Building Persistence Delay (Default) | BuildingSettings |
|  | `m_PickupTotalDurabilityPercentageReduction` | `0.050000` | float | Pickup Durability Reduction | BuildingSettings |
| ⚙ | `m_StakingUnitExtensionDefaultTimes` | `Array of escalating extension durations (60,120,240…` | array | Staking Unit Extension Times | BuildingSettings |
| ⚙ | `m_StakingUnitVerticalExtensionDefaultTimes` | `Array of escalating extension durations (60,120,240…` | array | Staking Unit Vertical Extension Times | BuildingSettings |
|  | `m_TimeToAutomaticallyCloseDoor` | `10` | int | Auto-Close Door Time | BuildingSettings |
|  | `m_bCanRemoveBuildablesWithNoOwner` | `True` | bool | Allow Removing Ownerless Buildables | BuildingSettings |
|  | `m_bEnableBuildingNearServerBorders` | `False` | bool | Allow Building Near Server Borders | BuildingSettings |
|  | `m_bMinBuildableDistanceFromServerBorder` | `1000.000000` | float | Min Build Distance From Server Border | BuildingSettings |

## Economy  (12, 7 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `SellOrderDailySolarisFee` | `20` | int | Exchange Sell Order Daily Fee (Solaris) | DuneExchangeSettings |
| ★ | `SellOrderPricePercentageFee` | `2.000000` | float | Exchange Sell Order Price Fee (%) | DuneExchangeSettings |
| ★⚙ | `m_CostMultiplierPerLandclaim` | `Array: 0.0, 0.5, 1.0, 1.5, 2.0, 2.5 (multiplier sca…` | array | Tax Cost Multiplier Per Landclaim | TaxationSettings |
| ★ | `m_PaymentItemPerHour` | `11.905000` | float | Payment Item (Solaris Coin) Per Hour | TaxationSettings |
| ★⚙ | `m_SolarisAmountToTagMapping` | `struct: Solaris reward amounts per tier/size tag (T…` | struct | Contract Solaris Rewards | ContractsSubsystem |
| ★ | `m_TaxationCycleLengthSeconds` | `1209600` | int | Taxation Cycle Length (s) | TaxationSettings |
| ★ | `m_bTaxationEnabled` | `False` | bool | Enable Taxation | TaxationSettings |
| ⚙ | `FillablePricesPer100Ml` | `Blood=2, Fuel=25, Water=20 (price per 100ml)` | struct | Fillable Prices Per 100ml | InventorySystemSettings |
|  | `MaxVendorCycleDuration` | `2419200` | int | Max Vendor Cycle Duration | InventorySystemSettings |
|  | `VendorBaselineDemand` | `0.050000` | float | Vendor Baseline Demand | InventorySystemSettings |
|  | `m_CostAmount` | `5000` | int | Character Recustomization Cost (Solaris) | CharacterRecustomizerSubsystem |
| ⚙ | `m_CostMultiplierPerVerticalExtension` | `Array: 0.0, 0.5, 1.0, 1.5, 2.0, 2.5 (multiplier sca…` | array | Tax Cost Multiplier Per Vertical Extension | TaxationSettings |

## Storms  (11, 5 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★⚙ | `m_LargeSandStormDamageConfig` | `Player=7, Building=7, Placeable=7, Vehicle=7` | struct | Large Sandstorm Damage | SandStormConfig |
| ★⚙ | `m_SmallSandStormDamageConfig` | `Player=5, Building=5, Placeable=5, Vehicle=5` | struct | Small Sandstorm Damage | SandStormConfig |
| ★ | `m_bAutoSpawnEnabled` | `True` | bool | Auto-Spawn Sandstorms | SandStormConfig |
| ★ | `m_bCoriolisDoesDamage` | `False` | bool | Coriolis Storms Deal Damage | SandStormConfig |
| ★ | `m_bMitigateAllSandstormDamage` | `False` | bool | Mitigate All Sandstorm Damage | BuildingSettings |
|  | `m_CoriolisHeavyDamage` | `5000.000000` | float | Coriolis Heavy Damage | SandStormConfig |
|  | `m_CoriolisLightDamage` | `5.000000` | float | Coriolis Light Damage | SandStormConfig |
|  | `m_CoriolisSpawnWarningsDurationInHours` | `6` | int | Coriolis Warning Duration (hours) | SandStormConfig |
|  | `m_CoriolisStage1DurationInSeconds` | `32400.000000` | float | Coriolis Stage 1 Duration (s) | SandStormConfig |
|  | `m_CoriolisStage2DurationInSeconds` | `3540.000000` | float | Coriolis Stage 2 Duration (s) | SandStormConfig |
|  | `m_bSandStormDebrisEnabled` | `True` | bool | Sandstorm Debris Enabled | SandStormConfig |

## Crafting  (8, 4 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_MaxArmorAugments` | `2` | int | Max Armor Augments | AugmentSettings |
| ★ | `m_MaxMeleeWeaponAugments` | `3` | int | Max Melee Weapon Augments | AugmentSettings |
| ★ | `m_MaxRangedWeaponAugments` | `3` | int | Max Ranged Weapon Augments | AugmentSettings |
| ★ | `m_RecyclerOutputWeight` | `0.250000` | float | Recycler Output Weight | CraftingSettings |
|  | `m_DefaultRequestsQueueLength` | `6` | int | Default Crafting Queue Length | CraftingSettings |
|  | `m_JackpotRollPercentage` | `0.950000` | float | Augment Jackpot Roll Threshold | AugmentSettings |
|  | `m_MinimumAugmentableItemQuality` | `0` | int | Minimum Augmentable Item Quality | AugmentSettings |
|  | `m_RepairCostWeight` | `0.500000` | float | Repair Cost Weight | CraftingSettings |

## Spice  (7, 2 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_NodeValueToSpiceResourceRatio` | `10.000000` | float | Node Value to Spice Resource Ratio | SpiceHarvestingSystem |
| ★ | `m_bSpawningActive` | `True` | bool | Spice Spawning Active | SpiceHarvestingSystem |
| ⚙ | `m_DefaultSystemSettings` | `Default spice field caps: Small=6/3, Medium=10/5, L…` | struct | Default Spice Field Caps | SpiceHarvestingSystem |
| ⚙ | `m_PerMapSystemSettings` | `Per-map spice field caps (MaxGloballyPrimed/MaxGlob…` | struct | Per-Map Spice Field Caps | SpiceHarvestingSystem |
|  | `m_PrimeRateInSeconds` | `30.000000` | float | Spice Bloom Prime Rate (seconds) | SpiceHarvestingSystem |
|  | `m_SpiceCollectorsAttackChance` | `0.000030` | float | Spice Collectors Attack Chance | EncountersSubsystem |
|  | `m_bPlayerMustWitnessBloom` | `False` | bool | Player Must Witness Spice Bloom | SpiceHarvestingSystem |

## Faction  (6, 3 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `bIsLandsraadEnabled` | `True` | bool | Enable Landsraad System | LandsraadSettings |
| ★ | `m_GuildCreationCost` | `1000` | int | Guild Creation Cost (Solaris) | GuildSettings |
| ★ | `m_MaxGuildMembersAllowed` | `32` | int | Max Guild Members | GuildSettings |
| ⚙ | `Data` | `Large struct: term retention 4 weeks, 3 decrees to …` | struct | Landsraad Configuration Data | LandsraadSettings |
|  | `m_FactionTierLock` | `2` | int | Faction Tier Lock | FactionSettings |
|  | `m_MaxGuildsAllowed` | `3` | int | Max Guilds Allowed | GuildSettings |

## Combat  (5, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ⚙ | `m_DefaultShieldDamageMitigation` | `Per-type shield mitigation: Dart/Energy/Explosive/H…` | struct | Default Shield Damage Mitigation | BuildingSettings |
|  | `m_MaxAttackDelayTime` | `5.000000` | float | Max NPC Attack Delay (s) | DuneAISettings |
|  | `m_MaxReinforcementSize` | `150.000000` | float | Max NPC Reinforcement Size | DuneAISettings |
|  | `m_MinAttackDelayTime` | `0.200000` | float | Min NPC Attack Delay (s) | DuneAISettings |
|  | `m_RandomDBNOChance` | `0.100000` | float | Random Down-But-Not-Out Chance | DuneAISettings |

## Loot  (5, 4 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `GlobalLootRightsBehaviour` | `PerPlayerChestAndNpcDrop` | string | Global Loot Rights Behaviour | LootSettings |
| ★ | `MaxLootDifficultyLevel` | `40` | int | Max Loot Difficulty Level | InventorySystemSettings |
| ★ | `MaxLootQualityLevel` | `5` | int | Max Loot Quality Level | InventorySystemSettings |
| ★⚙ | `m_PostSandwormDeathItemsGranted` | `1x WormTooth (durability 1.0)` | array | Post-Sandworm-Death Loot | SandwormSettings |
|  | `PerPlayerLootMinimumDespawnTimeAfterInteraction` | `30.000000` | float | Per-Player Loot Minimum Despawn Time After Interaction | InventorySystemSettings |

## Harvest  (4, 3 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_DewRefreshTime` | `12.000000` | float | Dew Harvest Refresh Time | DewHarvestSettings |
| ★ | `m_ResourceSpawnChance` | `1.0` | float | Resource Location Spawn Chance | ResourceLocationSystem |
| ★ | `m_bIsEnabled` | `True` | bool | Enable Resource Location System | ResourceLocationSystem |
|  | `m_FlourSandFieldsActivePercentage` | `1.0` | float | Flour Sand Fields Active Percentage | FlourSandSubsystem |

## PvP  (4, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
|  | `m_CriminalScoreLifeTimeInSec` | `600.000000` | float | Criminal Score Lifetime (s) | SecurityZonesSubsystem |
|  | `m_DefaultSecurityZoneType` | `NullSec` | string | Default Security Zone Type | SecurityZonesSubsystem |
|  | `m_OutlawCriminalScore` | `5` | int | Outlaw Criminal Score | SecurityZonesSubsystem |
|  | `m_OutlawFlagLifeTimeInSec` | `7200.000000` | float | Outlaw Flag Lifetime (s) | SecurityZonesSubsystem |

## Survival  (4, 2 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_bHydrationEnabled` | `True` | bool | Hydration System Enabled | HydrationSubsystem |
| ★ | `m_bIsSpiceAddictionEnabled` | `True` | bool | Enable Spice Addiction | SpiceAddictionSubsystem |
|  | `DecayedMaxDurabilityThreshold` | `0.200000` | float | Decayed Max Durability Threshold | InventorySystemSettings |
|  | `m_bIsSpiceVisionEnabled` | `True` | bool | Enable Spice Vision | SpiceAddictionSubsystem |

## Hazards  (3, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
|  | `m_DeathDelayDuration` | `3.000000` | float | Quicksand Death Delay (s) | HazardsSettings |
|  | `m_SandwormQuicksandSpeedModifier` | `0.250000` | float | Sandworm Quicksand Speed Modifier | HazardsSettings |
|  | `m_VehicleQuicksandDamage` | `10000.000000` | float | Vehicle Quicksand Damage | HazardsSettings |

## Progression  (3, 1 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `m_bIsEnabled` | `True` | bool | Contracts System Enabled | ContractsSubsystem |
|  | `m_MaxGlobalContractsNumberPerServer` | `10` | int | Max Global Contracts Per Server | ContractsSubsystem |
|  | `m_MinNumOfPlayersOnServerForContractSpawn` | `1` | int | Min Players For Contract Spawn | ContractsSubsystem |

## Respawn  (3, 2 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★⚙ | `m_DefaultRespawn` | `struct: m_TierRespawn empty, m_FallbackRespawnTimeM…` | struct | Default Respawn Timing | DuneAISettings |
| ★⚙ | `m_PVPRespawn` | `struct: m_TierRespawn empty, m_FallbackRespawnTimeM…` | struct | PvP Respawn Timing | DuneAISettings |
|  | `m_bCrossMapRespawnDropItems` | `True` | bool | Drop Items on Cross-Map Respawn | RespawnSettings |

## Inventory  (2, 2 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
| ★ | `PlayerInventoryStartingSize` | `35` | int | Player Inventory Starting Size | InventorySystemSettings |
| ★ | `PlayerInventoryStartingVolumeCapacity` | `175.000000` | float | Player Inventory Starting Volume Capacity | InventorySystemSettings |

## Shelter  (2, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
|  | `m_BuildingShelterThreshold` | `0.9` | float | Building Shelter Threshold | ShelterSettings |
|  | `m_PlaceableShelterThreshold` | `0.65` | float | Placeable Shelter Threshold | ShelterSettings |

## Vehicles  (2, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
|  | `Vehicle.CollisionDamageReductionFactor` | `0.010000` | float | Vehicle Collision Damage Reduction Factor | DuneVehicleSettings |
| ⚙ | `m_RecoveryPerVehicleClassCurrencyMultipliers` | `Per-vehicle-class Solaris recovery cost multipliers…` | struct | Vehicle Recovery Cost Multipliers | DuneVehicleSettings |

## Player  (1, 0 high)
| | key | default | type | label | section |
|---|---|---|---|---|---|
|  | `m_DefaultReconnectGracePeriodSeconds` | `300` | int | Default Reconnect Grace Period (s) | PlayerOnlineStateSettings |
