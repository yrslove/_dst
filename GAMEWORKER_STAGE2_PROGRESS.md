# GameWorker Stage 2 Progress

## Goal
Build the production-shaped, game-independent execution path `Action -> Safety
Gate -> ActionExecutor -> InputDriver`, with bounded queues, generation safety,
terminal results, cancellation/preemption, and fail-safe input release.

## Current Status
COMPLETE

## Stage 1 Contract
- RuntimeAgent owns WorkerBridge, which owns one generation-aware
  WorkerProcessHost using fresh bounded command/ACK/status queues per spawn.
- Runtime and worker generations cross the process boundary; stale IPC and stale
  ACKs are rejected, and unresolved commands become `WORKER_CRASHED` on death.
- NOOP and DST share the same subprocess lifecycle; WorkerContext is frozen,
  credential-free, and identity changes are rejected in-place.
- Effective ACTIVE requires configured ACTIVE plus verification, game readiness,
  initialized resources, and a healthy non-paused state. GAME_LOST and safer-mode
  delivery fail closed even when the command queue is saturated.

## Baseline
- Existing worktree contains extensive modified/untracked Stage 1 and platform
  work, treated as user-owned baseline and preserved.
- `python -m pytest -q -rs` -> 98 passed, 2 skipped in 42.91s.
- Skips: PostgreSQL requires `TEST_DATABASE_URL`; real Incus is Linux opt-in.
- Existing local SQLite `alembic check` INTEGER/BigInteger drift is unrelated and
  will not be changed in Stage 2.
- Existing action/input path is synchronous `DSTGameWorker -> GameActions ->
  InputController -> xdotool`. It has local key/button tracking, a lease, rate
  limiters, and a deadman, but no canonical external action identity/generation
  contract, bounded action executor, terminal-result cache, or deterministic fake
  driver.

## Plan
- [x] Read the Stage 1 checkpoint and audit lifecycle/action/input ownership.
- [x] Run the full pre-change baseline.
- [x] Define pickle-safe Action and terminal ActionResult contracts.
- [x] Introduce one InputDriver boundary and deterministic FakeInputDriver while
  preserving the bounded xdotool adapter.
- [x] Implement bounded ActionExecutor with validation, safety gate, idempotency,
  deadlines, cancellation, opposite-movement preemption, and terminal results.
- [x] Integrate executor ownership into DSTGameWorker and all safety paths.
- [x] Strengthen parent-side crash cleanup and generation isolation.
- [x] Add focused action/input/safety/concurrency/process-death tests.
- [x] Perform second-pass audit and full validation.

## Completed
- Initial repository audit and baseline validation.
- Canonical Action includes identity, semantic type, runtime/worker generation,
  monotonic creation/deadline/order metadata, bounded duration, and immutable
  primitive parameters. Every accepted/rejected request gets one terminal result.
- ActionExecutor owns one bounded FIFO per worker generation. Duplicate reservation
  is atomic, completed results are cached through the maximum valid action lifetime,
  and an expired retry cannot execute after cache eviction.
- Opposite movement deterministically preempts; all input is serialized by one
  executor and one InputLease. Safety actions bypass the normal queue, latch input
  closed, cancel running/queued work, and release immediately.
- InputController is the sole logical owner. XdotoolInputDriver is bounded,
  shell-free, stdin/output-closed, and receives only sanitized graphical env.
  FakeInputDriver records deterministic events and simulated physical state.
- Failed releases do not prevent later releases. Logical held state is cleared,
  uncertain releases are retained for a later retry, and all keys/buttons are
  attempted independently.
- Deadman trips only with held input, revokes ownership, releases all, stays tripped
  until explicit reset, and has bounded shutdown.
- DSTGameWorker constructs exactly one executor with runtime/worker generation,
  synchronizes the Stage 1 effective ACTIVE gate into it, and shuts it down before
  releasing remaining resources. WorkerProcessHost still performs independent
  parent xdotool key/button release before replacement generation startup.

## Current Work
Stage 2 implementation, audit, documentation, and validation are complete.

## Bugs / Risks Found
- Existing `ActionResult` has no identity/generation/terminal enum and duplicates
  can execute input repeatedly.
- Existing `GameActions` has no bounded input queue or terminal-result guarantee.
- `InputController` directly owns xdotool calls, making deterministic safety tests
  and backend substitution unnecessarily difficult.
- Worker hard death cannot run child cleanup; the existing parent emergency release
  remains the independent recovery path. Its real X11 effect cannot be verified on
  this Windows host, but the release mechanism is independently unit-testable.
- Second-pass audit found and fixed a concurrent duplicate reservation race and the
  risk of a no-deadline action becoming executable after bounded dedupe eviction.
- Second-pass audit found an initialization leak when vision construction failed
  after the executor thread was created; partial cleanup now includes local actions.
- Executor shutdown now reports whether its owner thread actually stopped, and an
  ERROR-state resume refuses to create replacement resources after cleanup failure.

## Decisions
- Preserve semantic DST actions; platform key/mouse details remain below the
  controller/driver boundary.
- Keep one executor per worker generation. Its controller is the sole logical
  input-state owner; drivers execute low-level operations only.
- Use a bounded FIFO for normal actions and an out-of-band safety/cancel path so a
  full normal queue cannot starve GAME_LOST, shutdown, or release-all.
- Gameplay actions require a monotonic deadline no later than the configured action
  timeout after creation. Safety release actions may omit it and remain idempotent.
- Missing movement duration is rejected: Stage 2 does not invent DST calibration.

## Tests
- Baseline full suite: 98 passed, 2 skipped in 42.91s.
- Added 18 regression tests: 16 focused action/input/safety tests, one
  crash-release-before-respawn ordering test, and one partial-initialization
  executor-cleanup test.
- Combined action + Stage 1 lifecycle/runtime suite: 60 passed in 1.36s.
- Targeted Ruff and compileall: passed.
- Final `python -m pytest -q -rs` -> 116 passed, 2 skipped in 42.15s.
- Final `python -m ruff check .` -> passed.
- Final `python -m compileall -q app runtime_agent node_agent scripts migrations
  tests` -> passed.
- Final `git diff --check` -> passed; only existing Windows LF/CRLF conversion
  warnings were printed.
- `python -m pytest -q tests/integration/test_migrations.py -rs` -> 1 passed in
  0.57s.
- The previously documented local SQLite `alembic check` INTEGER/BigInteger drift
  remains unrelated and was not modified.

## Files Changed
- `GAMEWORKER_STAGE2_PROGRESS.md` (created).
- `runtime_agent/gameworker/actions.py`
- `runtime_agent/gameworker/input.py`
- `runtime_agent/gameworker/config.py`
- `runtime_agent/gameworker/dst/worker.py`
- `runtime_agent/gameworker/process.py`
- `runtime_agent/gameworker/__init__.py`
- `tests/unit/test_gameworker_actions.py` (created).
- `tests/unit/test_gameworker_lifecycle.py`
- `tests/unit/test_runtime_hardening.py`
- `GAMEWORKER.md`
- `.env.example`
- `deploy/env/runtime-agent.env.example`

## Remaining
No remaining Stage 2 code work. Real Linux/X11 behavior listed below must be
validated on the target host; Stage 3 is limited to Capture / Perception / OBSERVE.

## Next Exact Action
Begin GameWorker Stage 3 by defining the Capture / Perception / OBSERVE contract and
collecting reviewed real-host visual evidence without changing Stage 2 safety gates.

## Last Known Good State
Final repository state passes 116 tests with 2 infrastructure skips, repository-wide
Ruff, compileall, whitespace validation, and the fresh migration regression. The
only Stage 2 limitations are physical xdotool/XAUTHORITY/DST-focus/keyboard-repeat
and SIGKILL-with-X-key-held validation on a real Linux/X11 runtime. Parent emergency
release is code- and ordering-tested and always completes before replacement spawn.
