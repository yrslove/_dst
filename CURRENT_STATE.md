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

## Current blockers

- Control Plane `STALE` state can remain stale despite healthy Runtime Agent/DST heartbeats.
- The first actionable gift frame has not yet been captured.
- In-world gift actions/contracts are not implemented.
- Production behavior still relies too much on validation-flow concepts.

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
