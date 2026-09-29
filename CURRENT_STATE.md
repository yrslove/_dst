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

LIVE_PROVEN on 2026-09-29: Farm 01's latest Master save contains exactly one vanilla
`researchlab` at `(112, -54)`, and a fresh canonical 1280x720 frame after normal load
showed it at the operating position. `Master/modoverrides.lua` is empty; the temporary
provisioning mod is not enabled. The saved entity record has no GUID field; the prior
live GUID was `127121`. Future gift work must reuse this machine and must not provision,
build, or search for another one.

The loaded Farm 01 screen currently shows the all-dead reset countdown. GameWorker is
DISABLED; no world reset was triggered. A fresh `IN_WORLD_IDLE` observation was not
reached during this persistence proof.

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
- The recent Farm 01 load showed the all-dead reset countdown rather than
  `IN_WORLD_IDLE`; resolve through existing death/reset recovery before gameplay.
- The first actionable gift frame has not yet been captured.
- In-world gift actions/contracts are not implemented.
- The production gift behavior remains future work; the gift loop is still pending.

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
  `67e957f3cd939eb48cb2c7bc60243cb106a7d6ad`. The post-recovery-change production
  world-entry acceptance completed; GameWorker was returned to DISABLED afterward.

The gift milestone is not complete from the gray-present observation alone. The next
concrete evidence is an enabled-present frame from Farm 01, followed by the real claim
sequence and fresh-state verification.

## Do not do now

- No multi-worker/orchestration work.
- No generic foundation hardening.
- No navigation/pathfinding.
- No farming/combat/survival AI.
- No repeated clean `MAIN_MENU` replay unless affected code changed.
- No GUI-console diagnostic as the normal gift-development path.
- No generic behavior-tree/planner framework.
- No full test suite after every small patch.

## Historical documentation

Dated stage, progress, handoff, and validation documents are historical evidence. They
are not current task authority unless the user explicitly names one. Use this document
for current status and blockers.
