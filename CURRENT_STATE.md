# Current State

## Current business goal

**One-account target:**

enter account/world

-> handle actionable gift

-> perform only required playtime/activity

-> handle next gift

-> repeat safely

## Live-proven — do not revalidate by default

The following paths/capabilities have already been proven against the live runtime:

- Control Plane -> Runtime Agent -> GameWorker -> canonical input -> DST -> fresh
  verification -> ACK/result.
- `MAIN_MENU -> Host Game -> Farm 01 -> Survivor Select -> Wilson/Wardrobe -> IN_WORLD_IDLE`.
- Host Game hover/focus recovery.
- Selected/hovered survivor portrait handling.
- Bounded movement with gameplay-region verification.
- Canonical `PAUSE_WORLD` and `RESUME_WORLD`.
- Death/reset recognition.
- Xpra canonical input path and bounded reconnect handling.
- Runtime Agent SIGHUP token/adoption handoff.
- Steam/Klei persistence through managed recovery.
- Control Plane `STALE -> RUNNING` recovery after a healthy authenticated `GAME_READY`
  heartbeat while the configured GameWorker remains `DISABLED`.

Do not rerun these acceptance paths merely because a new task starts. Revalidate a path
only when code directly affecting it changed or new evidence indicates regression.

## Current milestone

**FIRST REAL IN-WORLD GIFT CLAIM**

Known gift evidence:

- A gray wrapped-present means a gift is pending but not actionable.
- A nearby valid Science Machine / Alchemy Engine enables gift access.
- The gray-present ROI exists; it identifies the pending state, not an enabled actionable gift.
- An enabled-present live fixture is still required to establish the actionable visual state.
- Required real path: `enabled gift -> Opening -> You Received -> Use Later -> fresh IN_WORLD`.
- The claim is complete only after the fresh observation confirms return to `IN_WORLD`.

## Current environment direction

Farm 01 should become a durable prepared single-account world. Prefer a persistent safe
world, a vanilla Science Machine near the operating position, and minimal required
gameplay. Avoid building navigation/crafting/survival AI merely to recreate
infrastructure that can persist in the world.

LIVE_PROVEN on 2026-09-29: Farm 01 was rebuilt after the previous prepared save was
replaced by a DST world reset. The latest restored Master save contains exactly one
vanilla `researchlab` at `(374, 196)`; its live provisioning GUID was `128894`.
After restoring the prepared snapshot, a fresh canonical 1280x720 `IN_WORLD_IDLE`
frame showed alive Wilson and the machine beside the Florid Postern. GameWorker was
returned to DISABLED. A further clean managed STOP/START again loaded the same world;
a bounded canonical rightward input visibly moved Wilson while the machine remained
in view. `Master/modoverrides.lua` is empty; neither temporary fixture
mod is enabled. The rollback point is Incus snapshot
`dst-000001-g1/PREPARED_ALIVE_PRE_DEATH_20260929_1315`, also preserved as
`~/.local/state/dst-backups/Farm_01_PREPARED_ALIVE_PRE_DEATH_20260929_1315_save.tar.gz`.
Future gift work must reuse this machine and must not provision or build another one.

The controlled death validation observed alive, death, reset countdown, and a new
post-reset Master session with zero Science Machines. Its compact evidence archive is
`~/.local/state/dst-backups/Farm_01_RESET_OCCURRED_20260929_1333_evidence.tar.gz`.
The prepared snapshot was then restored through the managed STOP/START lifecycle.

## Stage 1 — verified daily gift claim

The worker now reports an explicit `DAILY_GIFT_CONFIRMED` result only when a verified
`CLICK_REWARD_OPEN` action is followed by a fresh `REWARD_RESULT` observation containing
the verified “You Received” title and close anchor. `GIFT_AVAILABLE`,
`GIFT_INTERACTION_STARTED`, `GIFT_UI_OPEN`, and `GIFT_UI_CLOSED` remain separate states.
The bounded worker telemetry includes the confirmation's evidence frame, sequence, and
action ID. Fixture-backed tests cover the positive result, available/banner-only,
open-in-progress, dismissal, and duplicate-result cases. This offline proof covers the
recorded daily login reward flow; it does not prove an in-world Science Machine gift
claim. No reward modal is currently available for live validation.

Preflight on 2026-09-29 found a launch configuration mismatch: Control Plane
`RUNTIME_AUTO_LAUNCH_STEAM=false` and `RUNTIME_AUTO_LAUNCH_DST=false`, while guest
Runtime Agent `AUTO_LAUNCH_STEAM=1` and `AUTO_LAUNCH_DST=1`. These settings were left
unchanged; no lifecycle operation was performed. The guest worker configuration is
`WORKER_MODE=DISABLED`, `WORKER_AUTOSTART=0`. A fresh read-only 1280x720 X11 capture showed
the game at Survivor Select, with chat reporting that `sociopath` was killed by
Darkness; no player was verified alive in-world and no gameplay input was sent.
Rollback remains available at Incus snapshot
`dst-000001-g1/PREPARED_ALIVE_PRE_DEATH_20260929_1315` and host archive
`~/.local/state/dst-backups/Farm_01_PREPARED_ALIVE_PRE_DEATH_20260929_1315_save.tar.gz`
(1,122,046 bytes, present at preflight).

## Stage 2 — durable gameplay task and reward persistence

`DAILY_GIFT_CONFIRMED` now enters the existing Control Plane runtime-heartbeat
transaction and is stored as a durable `GameplayTask` result, linked to the owning
account, runtime, and current WorkerRun. Available, interaction-started, UI-open,
UI-closed, and confirmed results remain distinct; only a structurally valid
`DAILY_GIFT_CONFIRMED` payload can complete a task. Confirmation identity is scoped to
account and task kind and derived from the worker action ID plus evidence frame ID;
it does not assume a calendar-day reward cycle. Database uniqueness protects against
duplicate delivery, and the same transaction writes the successful task and claim.

Migration `0007_gameplay_task_results` adds `gameplay_tasks`, its account/runtime/
WorkerRun links, success/status constraints, and active-task/claim uniqueness. At the
end of Stage 2, the populated validation database remained at
`0006_worker_remote_view`; a temporary backup clone successfully upgraded to 0007,
downgraded to 0006, and upgraded again. No PostgreSQL integration database was
configured for that validation.

Stage 2 persistence is implemented and offline-verified. Stage 1 real live gift
confirmation remains `STAGE_1_LIVE_VALIDATION_PENDING`; no claimable gift was consumed
or fabricated. Runtime/player state was not freshly inspected during Stage 2, and no
live gameplay or lifecycle action was performed. The prepared rollback snapshot and
archive recorded above were not modified by this stage; their presence was not
rechecked during Stage 2.

## Stage 3 — single account daily task executor

The deployed Control Plane service uses `.data/linux-validation.env`; its active
database is `.data/linux_validation.sqlite3`. It is now migrated through
`0010_long_session` (including the gameplay result migrations). Production PostgreSQL
migration remains untested.

For managed runtime startup, Control Plane `RUNTIME_AUTO_LAUNCH_STEAM` and
`RUNTIME_AUTO_LAUNCH_DST` in `.data/linux-validation.env` are authoritative. Runtime
Bootstrap renders those settings into the guest Runtime Agent environment on managed
start/rebuild; the guest copy is generated configuration. Bootstrap version 2 forces
the next start to apply the reconciled configuration. Both settings are true for the
normal managed launch path.

The existing scheduler invokes the durable `DAILY_GIFT_CLAIM` job. The executor
reconciles STOPPED/STARTING/RUNNING runtime states, requests fresh GameWorker
OBSERVE perception after `GAME_READY`, then uses the existing worker command path.
Only Stage 2's persisted `DAILY_GIFT_CONFIRMED` result completes a task. Normal
no-reward is represented as `NO_REWARD_AVAILABLE`; other unsafe or unreconciled
conditions remain inspectable `NEEDS_ATTENTION` results. Worker disable/input release
and owned-runtime stop run through cleanup paths.

Focused Stage 1, Stage 2, runtime/job, migration, and Stage 3 tests passed after the
report-shape and SQLite UTC timestamp checks were added. The synthetic lifecycle
covers managed STOPPED -> STARTING -> GAME_READY -> fresh observation -> ACTIVE ->
confirmed result -> worker disabled -> managed STOPPED. PostgreSQL migration is not
covered here.

Bounded live validation used a fresh OBSERVE-only worker command. It produced
`CHARACTER_SELECTION` at 0.979 confidence. The screen was not accepted as a safe
initial task state because the prior fresh capture contained the death-by-Darkness
chat message. No ACTIVE command or gameplay input was sent. Worker mode ended
`DISABLED`, held inputs false; runtime remained in its pre-existing running state.
There was no actionable reward evidence, no `DAILY_GIFT_CONFIRMED`, and Stage 1 live
claim validation remains pending. Rollback resources remain preserved.

## Current blockers

- The production fixed-coordinate path
  `MAIN_MENU -> IN_WORLD_IDLE` is LIVE_PROVEN on deployed revision
  `67e957f3cd939eb48cb2c7bc60243cb106a7d6ad`. A fresh stable `MAIN_MENU` at
  2026-09-29 09:34:33 UTC reached worker-verified `IN_WORLD_IDLE` at 09:37:03.9 UTC
  (2m30.9s); two independent fresh `IN_WORLD_IDLE` observations followed at
  09:37:45.5 and 09:37:46.6 UTC. No Control Plane VERIFY or worker reactivation was
  required during entry. No action timeout occurred, so local late-transition recovery
  was not naturally exercised. Per-state stable-state-to-action timings were not
  retained in the Control Plane status history. The largest adjacent action-status
  interval was 47.3s, including an unclassified game transition; its non-Loading idle
  portion cannot be isolated.
- Fixed menu click locations must continue to come from
  `dst-1280x720-linux-v1`; do not return to dynamic anchor localization for these
  controls unless the supported display profile changes.
- The first actionable gift frame has not yet been captured.
- Stage 1 live status remains `STAGE_1_LIVE_VALIDATION_PENDING`: a live actionable
  reward must still be claimed and confirmed.
- Safe world profile ownership and current-process configuration evidence are now
  implemented and passed a 10-minute live session on 2026-09-29. The short run proves
  managed world entry, fresh config/process evidence, 51 in-world checkpoints, and
  clean stop; it does not prove the settings' gameplay effects. Do not start the
  75–90 minute soak until the profile's effect on the existing world is separately
  established.
- Active Control Plane SQLite schema is `0010_long_session`; PostgreSQL migration
  has not been tested.
- The in-world path still needs recorded evidence of (1) an enabled present beside the
  Science Machine, (2) the post-`INTERACT` reward UI showing `You Received` and
  `Use Later`, and (3) a fresh `IN_WORLD` frame after `Use Later`. Existing reward fixtures
  are from the daily login modal and cannot prove that in-world path.

## Completed worker semantics

- An ACTIVE GameWorker performs deterministic world entry with
  `validation_flow=false`; the legacy validation route remains test-only.
- Reusable gameplay primitives are independent of validation-flow orchestration; the
  validation flag enables only the legacy one-shot test route.
- Recoverable worker intervention no longer rewrites configured ACTIVE intent to
  DISABLED. Unsafe states suppress action execution until observation or recovery is
  verified.
- Mods Disabled confirmation is supported by fixed production UI handling; the
  deployed confirmation action cleared the live modal and its fresh verification
  observed the transition.
- The canonical host-to-guest application deployment path is implemented and
  LIVE_PROVEN; the guest reports revision
  `f723a9ee5090529240e3775d68a426abfa2a6aff`. The post-recovery-change production
  world-entry acceptance completed; GameWorker was returned to DISABLED afterward.

The gift milestone is not complete from the gray-present observation alone. The next
concrete evidence is an enabled-present frame from Farm 01, followed by the real claim
sequence and fresh-state verification.

## Stage 3B safe world-entry reconciliation (2026-09-29)

GameWorker now classifies a survivor-selection screen from verified title, player-list,
and Wilson-target anchors, and uses the observed Wilson portrait / loadout button anchors
for canonical selection actions. Action verification still requires fresh observations;
loading is intermediate and only stable fresh `IN_WORLD_IDLE` completes entry. The daily
executor admits character selection only when those structural anchors are verified.
Partial screens, DEAD, reset-pending, and unknown states remain non-actionable.

The bounded live preflight found the runtime already RUNNING and worker DISABLED with no
held inputs. A fresh screen was the normal Survivor Select UI, with an on-screen death
message. Read-only save inspection found the active Master save on a different session
from `PREPARED_ALIVE_PRE_DEATH_20260929_1315`; the prepared archive contains the expected
Science Machine fixture and the active save does not. The rollback snapshot/archive still
exist and were not restored or consumed. No survivor selection, world start, reward action,
or new daily task was attempted. The pre-existing `NEEDS_ATTENTION` task remains durable;
the worker remains disabled and the inherited runtime remains running.

Stage 3B focused regression command passed 158 tests across daily executor, gameplay
persistence, perception/actions/transitions, Stage 1 behavior, runtime/jobs, and
migrations. Ruff lint and `git diff --check` pass. Ruff format check identifies existing
format drift in touched historical files; those files were not wholesale reformatted to
avoid unrelated churn. Live world-entry/claim validation remains blocked until the
prepared world is safely available again; no automatic rollback was performed.

## Stage 3C controlled prepared-world restore (2026-09-29)

The current post-death container was preserved as Incus snapshot
`STAGE3C_CURRENT_POST_DEATH_20260929_1847`, with a separate compact save archive
`Farm_01_STAGE3C_CURRENT_POST_DEATH_20260929_1847_save.tar.gz`. Its active Master
session was `AC6D72B5B368FA58` with no Science Machine record. The prepared snapshot
`PREPARED_ALIVE_PRE_DEATH_20260929_1315` and its independent archive remained intact.
After managed STOP and worker/input quiescence, the existing runbook snapshot restore
passed the read-only pre-death guard: Master session `EA6E12E4296C650B`, save
`0000000003`, one `researchlab` at `(374, 196)`, and empty mod overrides.

Managed START restored process readiness. Bootstrap version 3 now retains the verified
`dst-1280x720-linux-v1` worker calibration profile on managed starts. The canonical
runtime deployment restored Stage 3B worker code after the older container snapshot
reverted it. Fresh GameWorker observations followed canonical world-entry actions from
`MAIN_MENU` through the saved-world list and Loading to valid `IN_WORLD_IDLE` with a
visible player. The same prepared save/fixture remained on disk after entry and after
the daily task. The worker ended DISABLED with no held inputs; the inherited runtime
remains RUNNING and healthy.

The previous GameplayTask 1 remains `NEEDS_ATTENTION / WORKER_OBSERVATION_UNSAFE`.
Migration 0009 treats that state as a terminal attempt, allowing a distinct task 2.
The production DAILY_GIFT_CLAIM job 70 ran through the executor and persisted task 2
as `NEEDS_ATTENTION / NO_CLAIMABLE_REWARD_UNVERIFIED`: the worker observed a stable
in-world state but its gift telemetry stayed `UNKNOWN`, so reward unavailability was
not asserted. No `DAILY_GIFT_CONFIRMED` occurred; Stage 1 live validation remains
pending. Stage 3C restore and live world entry are proven; the next gift work needs a
real actionable reward or a verified unavailable-reward signal.

## Stage 3D — direct HUD gift availability (2026-09-29)

Status: `PASS_ACTIVE_SAMPLE_PENDING`. The user-verified top-left gift HUD icon's
gray/inactive state now supplies a production unavailable-reward signal. This
supersedes the previous absence of verified unavailability evidence.

`VisionDetector` reuses `world_present_banner` within calibrated normalized ROI
`(0.115, 0, 0.2, 0.14)` for `dst-1280x720-linux-v1`. On fresh valid in-world frames,
template identity plus masked icon chroma establishes `NO_REWARD_AVAILABLE`.
Missing/ambiguous/stale evidence stays `GIFT_AVAILABILITY_UNKNOWN`. One canonical
hover is available for uncertain identity; its localized UI response is secondary
evidence, never gift availability or claim confirmation. Hover transport/response
are covered by focused tests; hover was unnecessary and was not exercised live.
No trustworthy active/colored reference exists: `ACTIVE_ICON_LIVE_SAMPLE_PENDING`.
The active branch requires an independently verified reference and remains unproven.

The runtime advanced to a real death countdown during implementation, before any
Stage 3D gameplay input. A full current-state snapshot failed with ENOSPC and Incus
removed its partial copy. Managed STOP job 71 failed while snapshotting held Incus
in FROZEN state. The current saves were then preserved in verified compact archive
`Farm_01_STAGE3D_CURRENT_PRE_RESTORE_20260929_1941_save.tar.gz` (SHA-256
`7c5602fbef92848678f17ecf4284fd9fe12794f1b10cf83aac5dc63aad984297`).
Managed STOP job 72 succeeded; the existing runbook restored the untouched
`PREPARED_ALIVE_PRE_DEATH_20260929_1315` snapshot once while STOPPED. The pre-death
guard independently verified matching save bytes, one machine at `(374, 196)`,
and disabled temporary mods. Managed START job 73 and canonical runtime deployment
revision `f48c31f16b8ff24b7a4076d192b8a7684a29ed9e` restored healthy GAME_READY.
No automatic recovery policy was added.

Fresh canonical world entry reached living `IN_WORLD_IDLE`; the live icon bounds
were `(168, 10, 226, 68)`, identity confidence `0.9999367`, chroma p95 `13`, and
colored fraction `0`. Production DAILY_GIFT_CLAIM job 74 created distinct task 3
and durably persisted `NO_REWARD_AVAILABLE` with frame `r1-w1-f2` observed at
`2026-09-29T19:47:14.548776+00:00`. Tasks 1 and 2 retain their original
NEEDS_ATTENTION history. No DAILY_GIFT_CONFIRMED occurred; Stage 1 live validation
remains pending. The combined focused Stage 1/2/3/3B/detector suite passed 155 tests.

Final managed STOP job 75 succeeded after task cleanup, preserving the alive baseline
instead of leaving it running unattended. Runtime 1 / `dst-000001-g1` is STOPPED;
worker mode DISABLED, process state STOPPED, held_inputs=false. Final read-only save
verification still finds Master `EA6E12E4296C650B`, save `0000000003`, exactly one
Science Machine at `(374, 196)`. Both original rollback snapshots and their archives,
plus the Stage 3D compact current-state archive, remain available. Stage 4 was not run.

## Stage 4A — unattended-session preflight (2026-09-29)

Historical preflight; Stage 4A.1 below records the later session and the implemented
long-session task and storage guard. The safe-profile concern remains current.

Status: `BLOCKED / NOT_READY_FOR_LONG_SOAK`. No runtime start, gameplay input,
snapshot restore, world setting change, new session task, or live soak was performed.
The active/colored gift sample is not this blocker.

Read-only verification still finds prepared Master `EA6E12E4296C650B`, save
`0000000003`, exactly one Science Machine at `(374, 196)`, and no temporary mods.
The installed legitimate world settings expose nonlethal darkness, hunger, and
temperature damage. The prepared world's `leveldataoverride.lua` still uses
`darkness="default"`, `hunger="default"`, `temperaturedamage="default"`, default
hostile/environmental threats, and normal day/night progression.

Reward-safe environmental simplification is NOT established. The installed
`scripts/components/giftreceiver.lua` obtains gift count through native
`TheInventory:GetClientGiftCount`; it does not expose playtime eligibility or prove
that changing world settings preserves that eligibility. No project evidence or
authoritative documentation inspected establishes that exact requirement. The
availability of legitimate nonlethal settings alone is insufficient proof.

The alternative minimal survival policy also lacks a usable night-light capability:
prepared player saves contain none of torch, lantern, minerhat, or lighter. The world
has no campfire/firepit/lantern/nightlight record. Its sole torch record is at
`(-149.54, -277.61)` with fuel `8`, far from the prepared operating position, and
does not establish a sustained light source. Existing canonical actions and
navigation/recovery primitives do not provide verified light acquisition,
equipping, replenishment, or crafting. Pausing cannot substitute for a proven
unpaused reward session. Starting an idle soak would repeat the known Darkness risk.
No speculative survival economy or unused session framework was added.

At that preflight, storage was approximately 6.6 GiB free; Incus uses the `dir` driver on the same
host filesystem. Live Klei data is 14 MiB; host journals are 203 MiB and guest journals
461 MiB. Recording is DISABLED with zero frames/events; existing per-recording defaults
are 15 minutes, 1800 frames, 512 MiB, queue size 8. No explicit journald size override
was found on the host. All three compact save archives passed gzip integrity checks;
the prepared and Stage 3C rollback snapshots were retained. No large snapshot was
created. A future session must enforce a minimum 2 GiB free-space guard on the host/
Incus filesystem and bound aggregate diagnostics/log retention; those guards were
not yet implemented at that preflight.

At that preflight, runtime 1 was STOPPED and the worker DISABLED. Stage 4A.1 below
records the later session and current blocker. No 6–10 hour soak was started.

## Stage 4A.1 — safe profile and first unattended soak (2026-09-29)

Status: `BLOCKED before the 75-minute soak`. The long-session task, storage guard,
and bounded monitoring are implemented, and the first production session completed.
However, the safe built-in profile did not persist through actual world entry, so the
run cannot validate survival under that profile and the second soak must wait.

The original prepared config was backed up at
`~/.local/state/dst-backups/STAGE4A1_original_config_20260929.tar.gz`; its original
Master `leveldataoverride.lua` SHA-256 is
`47057a5fbad74a41fc7c863c511093bf63ad6d78fa4399689430482399726944`. The managed
bootstrap applied the experimental profile before launch, but after the game entered
the world the file hash returned to that exact original value and the hazard settings
were `default`. The attempted values were day `onlyday`, hunger and temperature
damage `nonlethal`, winter/spring/summer `noseason`, hounds/shadow creatures/weather/
lightning `never`. This is concrete config persistence/validation evidence; it is not
a reward eligibility finding.

Production LONG_SESSION task 4 / job 78 completed with `TIME_BUDGET_REACHED`. Fresh
monitoring ran from `2026-09-29T20:26:52.892900Z` to
`2026-09-29T20:36:58.741434Z` (about 10 minutes), with 51 fresh in-world checkpoints,
zero recoveries, no manual gameplay intervention, and the gift icon continuously
classified `NO_REWARD_AVAILABLE`. The player remained `IN_WORLD_IDLE` at the final
fresh checkpoint. This is a real operational idle soak, but not a safe-profile soak.

After cleanup, runtime 1 / `dst-000001-g1` is STOPPED, GameWorker is DISABLED,
`held_inputs=false`, and Job 78 is SUCCEEDED. Read-only post-stop save verification
found Master `EA6E12E4296C650B`, save `0000000003`, exactly one Science Machine at
`(374, 196)`, and no temporary mods. The prepared rollback snapshot/archive, compact
Stage 3D save archive, and original config archive remain available. Host filesystem
free space was about 7.05 GB at the final checkpoint; the 2 GiB guard is implemented.

The 75–90 minute session was not started because live safe-setting persistence failed.
Reward eligibility remains `SAFE_WORLD_REWARD_ELIGIBILITY_NOT_YET_OBSERVED`; the gray
icon does not establish that safe settings disable rewards. No 6–10 hour soak ran.

Implementation and focused/regression tests are in the checkpoint. The requested
targeted suite passed 194 tests. Ruff passed; `git diff --check` passed. Format check
passed for 21 changed Python files; seven files retain formatting drift and were
left untouched. The safe-profile ownership and evidence blocker was closed in Stage
4A.2 below; reward eligibility and longer survival validation remain separate.

## Stage 4A.2 — safe world config ownership and live evidence (2026-09-29)

The installed DST build is `747465` (Steam build ID `24700692`). Its
`scripts/tools/generate_worldgenoverride.lua` writes `worldgenoverride.lua` with
`override_enabled` and partial `overrides`; `scripts/map/customize.lua` exposes the
safe profile's keys and values. The prepared world's `Master/leveldataoverride.lua`
is a complete saved level definition and is no longer the safe-profile ownership
layer.

The canonical safe profile now renders deterministically to the cluster-level
`worldgenoverride.lua`. Runtime Agent reconciliation runs in the existing DST
`ProcessSupervisor.before_start` path on every launch/restart and is idempotent.
Post-ready evidence carries desired/applied SHA-256, account/runtime ID and generation,
cluster path, DST PID/start ticks, process generation, and verification timestamp.
Runtime Agent refreshes that evidence only after checking the current file and process
identity; the Control Plane checks it against the current runtime before admitting
worker activation. `LongSession` also rejects missing, stale, mismatched, or unverified
evidence throughout monitoring. The evidence explicitly records that world behavior
is not verified.

Live validation deployed revision `db938d55ab4f3f1a66fb9ae33e3907c0c2ce512d`, then
used managed STOP/START. Fresh guest and Control Plane hashes matched
`c6a17368c19966a901c778b70a8b4b9b28ba71615034f4a183543c83908c1b83` for runtime 1,
generation 1, DST PID 891/start ticks 5149068. `LONG_SESSION` entered the prepared
world and completed its 600-second monitoring budget with 51 checkpoints and zero
recoveries. It finished `TIME_BUDGET_REACHED`; GameWorker ended DISABLED with no held
inputs, and managed STOP returned runtime 1 to STOPPED. The post-stop
`worldgenoverride.lua` hash still matched. This proves configuration-file/process
evidence and short unattended operation, not that DST applied each setting to the
existing save or that those settings guarantee survival/reward eligibility.

Focused tests passed (26); Ruff and `git diff --check` passed. The work is committed
and pushed to `main` in `43edc8b` and deployment fix `db938d5`.

## Safe-profile factual application correction (2026-09-29)

The requested 5400-second soak stopped after 16m04s/80 checkpoints: dusk occurred
and DST logged `Not applying world gen overrides`, despite file/process evidence.
No death/reset or unexpected input was observed. This invalidated admission based
only on configuration-file verification.

Installed-build audit and live provisioning confirmed the supported correction:
USER overrides belong in `Cluster_1/Master/worldgenoverride.lua` for this hosted
shard; DST also applies these world settings when loading an existing save.
The same prepared session `EA6E12E4296C650B` was loaded with all ten actual override
markers and saved normally as `0000000004`. Persisted settings match the canonical
profile; persisted clock is day=16/dusk=0/night=0, matching the visible all-day HUD.
No regeneration/mod/console or new input path was needed.

The prepared Master now has a versioned safe manifest and a protected rollback/
restore archive. Runtime evidence distinguishes CONFIG_PRESENT from
WORLD_PROFILE_VERIFIED and current loaded-world application. LongSession requires
persisted safe settings/fixture identity and current-process load evidence at
in-world checkpoints. Old unsafe snapshots cannot satisfy that contract.
See [the installed-build finding and provisioning contract](docs/SAFE_WORLD_PROFILE.md).
Deployed-code short validation is recorded separately in the stage's live artifacts;
the 75–90 minute soak remains pending and must not start automatically.

## Do not do now

- No multi-worker/orchestration work.
- No generic foundation hardening.
- No navigation/pathfinding.
- No farming/combat/survival AI.
- No repeated clean `MAIN_MENU` replay unless affected code changed.
- No GUI-console diagnostic as the normal gift-development path.
- No generic behavior-tree/planner framework.
- No full test suite after every small patch.

## Stage 4B — installed-build reward mechanics audit (2026-09-30)

Build-specific audit: DST `747465` / Steam build `24700692`. The bundled Lua clearly
separates lobby daily gifts from in-world playtime gifts. Lobby login waits for
inventory download / `CHECK_DAILY_GIFT` and presents `GetDailyGiftItem()` plus
unopened entitlements in `ThankYouPopup`. In-world gifts use native
`TheInventory:GetClientGiftCount`, replicated `hasgift`, and a separate
`hasgiftmachine` state.

The previous assumption that a gray present means `NO_REWARD_AVAILABLE` is disproven
by the installed client flow. `GiftItemToast` is displayed when a gift count exists,
but it is click-disabled until `builder.lua` finds a visible nearby `giftmachine`
while the player's inventory is open. Science Machine and Alchemy Engine provide
that tag. The retained live gray-present frame is therefore evidence of a pending
gift without an enabled machine context; the 48m06s segment does not establish a
no-gift AFK interval. Native account eligibility/timer logic is not in the scripts
bundle. The current source/runtime findings and bounded next experiment are in
[REWARD_MECHANICS_AUDIT_20260930.md](docs/REWARD_MECHANICS_AUDIT_20260930.md).

This checkpoint adds a separate `inworld_gift_state` telemetry field and reports
gray visible present as `IN_WORLD_GIFT_PENDING`. One live canonical `Tab`/
`OPEN_INVENTORY` action was fresh-screen-change verified, but the toast stayed gray.
Build source shows this key opens `CONTROL_OPEN_INVENTORY`, not the crafting menu
needed for the giftmachine context. Production ACTIVE mode now proposes one bounded
canonical `OPEN_CRAFTING_MENU` action on `b` (`CONTROL_OPEN_CRAFTING`) for the next
live check. The production gift click remains unproven and must not be treated as the
daily `LOGIN_REWARD_AVAILABLE` modal. Actual `GiftItemPopUp` result, claim
confirmation, daily-vs-weekly durable progression, account weekly count, AFK
eligibility, and this account's next-due/reset timestamps remain open.

Runtime health evidence at 2026-09-30 13:50 UTC: DST and Steam running, worker ACTIVE,
no held inputs, zero world-interaction actions before this stage, and item-server
HealthCheck returned `OK`. After deployment, the worker opened the canonical
inventory once (0.52s; fresh UI change verified); the toast remained gray. The agent
adoption preserved Xvfb/Steam/DST process identities. The currently pending `b` /
`OPEN_CRAFTING_MENU` fix is offline tested but not yet deployed or exercised live.
HealthCheck is service health only; it does not prove authenticated item-server
eligibility or counted playtime.

## Historical documentation

Dated stage, progress, handoff, and validation documents are historical evidence. They
are not current task authority unless the user explicitly names one. Use this document
for current status and blockers.
