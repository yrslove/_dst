# Safe prepared-world profile

## Installed-build finding

Audit target: DST `747465`, Steam build `24700692`, the installed
`data/databundles/scripts.zip` (not an assumed upstream version).

- `shardindex.lua:SetServerShardData` reads a complete GAME-owned
  `leveldataoverride.lua`, then merges the USER partial `worldgenoverride.lua`.
  `../worldgenoverride.lua` is resolved inside the shard save context: the live
  hosted Master reads `Cluster_1/Master/worldgenoverride.lua`. The earlier
  cluster-root file was not read. `Not applying world gen overrides` is the
  fallback when no enabled, readable, valid override was obtained.
- This override is **not generation-only**. `networking.lua:StartDedicatedServer`
  calls `SetServerShardData` before loading an existing world, and
  `gamelogic.lua:DoInitGame` copies those shard options into
  `savedata.map.topology.overrides` (lines 843–849 in this build).
  `PopulateWorld` applies `WorldSettings_Overrides.Pre` and `.Post` on load.
- The ten canonical settings are supported runtime world settings. In particular,
  `worldsettings_overrides.lua` maps `day=onlyday` to day/dusk/night modifiers
  `3/0/0`, yielding clock segments `16/0/0`.
- `mainfunctions.lua:SaveGame` persists topology overrides and
  `world_network.persistdata.clock/seasons`; it also writes settings back to the
  shard index and USER override file. That file can legitimately become a complete
  settings list after a save. Runtime verification fingerprints owned settings,
  rather than treating DST's own serialization as configuration drift.

This supported path was confirmed live: the existing session
`EA6E12E4296C650B` loaded with all ten override markers, the clock visibly became
all-day, and normal UI disconnect saved snapshot `0000000004` containing the
canonical settings and clock `day=16,dusk=0,night=0`. No regeneration, console,
DST patch, or mod was used; the existing Science Machine remains intact.

## Provisioning and existing prepared-save restore

Keep worker/input ownership controlled. Back up the existing prepared Master before
migration. Place the canonical USER override in Master, load the prepared world
normally, observe actual application, and save through DST. Then, while the managed
runtime is STOPPED, attest and archive the existing prepared shard:

```sh
sudo .venv/bin/python -m scripts.diagnostics.safe_world_fixture \
  --instance dst-000001-g1 --application-log /protected/provisioning-client-log.txt \
  --archive /protected/prepared-safe-Master.tar.gz
```

The operation reuses the existing prepared-world guard (one Science Machine, no
fixture mods) and Master save layout. It never creates a world or rewrites save
contents. Attestation requires saved settings, persisted all-day clock, and actual
DST load/application markers. Manifest creation is idempotent; archives are never
overwritten. Full-container snapshots are unnecessary for this bounded shard
backup; Steam/authentication data are untouched.

Restore the canonical archive through the same guarded tool while STOPPED:

```sh
sudo .venv/bin/python -m scripts.diagnostics.safe_world_fixture \
  --instance dst-000001-g1 --restore --archive /protected/prepared-safe-Master.tar.gz \
  --rollback /protected/before-restore-Master.tar.gz
```

Restore validates the candidate before changing Master and preserves a rollback
archive. Ordinary starts reuse the current persisted, verified prepared world;
restore is an explicit maintenance/recovery operation, not daily regeneration.
Unsafe historical Incus snapshots remain available for diagnostics but are not
safe-profile fixtures. A missing/mismatched safe manifest or settings rejects
LongSession eligibility.

## Evidence contract

- `CONFIG_PRESENT`: deterministic USER configuration plus current PID/start ticks;
  insufficient for a LongSession.
- `WORLD_PROFILE_VERIFIED`: manifest profile version/hash/session identity matches
  the actual newest saved world; all ten persisted settings and clock segments
  match the canonical profile. Evidence includes settings fingerprint, manifest
  hash, save hash, and current runtime/process generation.
- `loaded_world_verified=true` additionally requires the current-process DST log
  header and the last loaded session's actual override markers. In-world
  LongSession checkpoints require this flag. Pre-entry admission uses the verified
  persisted fixture so normal world entry can run.

A correct override adjacent to an old unsafe save cannot produce the second or
third evidence level. New process identity invalidates old process evidence;
missing/wrong session, profile version, profile hash, or persisted settings rejects
fixture verification. Profile changes require explicit reprovisioning/attestation,
not automatic destructive regeneration. Save inspection never executes save Lua.

`world_behavior_verified` remains false: these proofs establish applied settings
and the observed all-day clock, not immunity to every possible world hazard or a
successful long unattended soak. The 75–90 minute soak is a separate next stage.
