# GameWorker Stage 1 Progress

## Goal
Harden the existing GameWorker core and lifecycle so RuntimeAgent, WorkerBridge,
WorkerProcessHost, and concrete workers have one bounded, deterministic,
generation-safe integration boundary for later Actions/Input/Safety work.

## Current Status
COMPLETE

## Baseline
- Before Stage 1 changes, `python -m pytest -q -rs` completed with 75 passed and
  2 infrastructure-dependent skips in 42.34 seconds.
- The worktree already contained extensive modified and untracked files from prior
  runtime/platform work. They are treated as user-owned baseline changes and will
  not be reverted.
- RuntimeAgent owns one WorkerBridge. WorkerBridge owns command deduplication and
  either an in-process NoopGameWorker or one WorkerProcessHost for the DST plugin.
  WorkerProcessHost owns the subprocess, bounded multiprocessing queues, restart
  count, pending IPC command IDs, and emergency parent-side input release.
- WorkerProcessHost creates a fresh pair of bounded queues for each subprocess
  spawn and fails pending commands when a child dies. The child serializes worker
  control and tick mutation with an RLock, while cancellation is signalled before
  taking that lock.
- DSTGameWorker already has a WorkerStateMachine and fail-closed ACTIVE checks.
  WorkerMode currently represents DISABLED, OBSERVE, and ACTIVE; there is no
  canonical REPLAY value yet. NoopGameWorker does not currently use the same state
  machine/process lifecycle as the DST plugin.
- WorkerContext is a frozen dataclass containing account/runtime IDs, a canonical
  DisplayEnvironment, verification state, state text, and free-form metadata. It
  is passed over the spawn boundary and is credential-free in current construction.
- Existing critical tests are concentrated in
  `tests/unit/test_runtime_hardening.py`; no dedicated GameWorker Stage 1 lifecycle
  test module exists yet.

## Plan
- [x] Audit the existing GameWorker ownership, protocol, IPC, modes, state machine,
  ACTIVE gate, NOOP path, and current regression tests.
- [x] Capture the pre-change full-suite baseline.
- [x] Define and enforce canonical command/result/process-generation contracts.
- [x] Close start/stop/restart, pending-ACK, queue-full, cancellation, and stale-IPC
  race paths with deterministic tests.
- [x] Align NOOP with the same core lifecycle without changing production defaults.
- [x] Add the minimal OBSERVE/REPLAY-compatible observation contract and mode
  validation without implementing Stage 3 or Stage 4 behavior.
- [x] Perform a second-pass lifecycle/serialization audit and full validation.

## Completed
- Repository audit: located construction and ownership in
  `runtime_agent/main.py`, `runtime_agent/worker_bridge.py`, and
  `runtime_agent/gameworker/process.py`; inspected the concrete DST and NOOP
  workers, WorkerContext/WorkerReport, WorkerConfig/WorkerMode, state machine,
  input safety primitives, and existing lifecycle regression tests.
- Baseline validation: established a reproducible pre-change test result.
- `runtime_agent/gameworker/ipc.py`: added pickle-safe, generation-bound command,
  status, and terminal-ACK DTOs plus canonical command/result names.
- `runtime_agent/gameworker/process.py`: separated replaceable status from terminal
  ACK traffic, added explicit worker generations, stale-envelope rejection,
  synchronized host state, bounded readiness/stop waits, deterministic crash
  outcomes, exponential restart backoff, stable-run budget reset, and an explicit
  process lifecycle state.
- Runtime generation now flows from `RuntimeAgentConfig` through environment and
  `RuntimeAgentSettings` into `WorkerContext`; worker subprocess names and report
  details include their generation.
- `WorkerBridge` now puts NOOP behind the same WorkerProcessHost lifecycle boundary
  as DST and serializes bridge state/command deduplication with a lock.
- Worker modes now include the REPLAY integration value and a backward-compatible
  NOOP alias for the existing DISABLED wire value. REPLAY deliberately fails closed
  as unavailable until the later replay stage.
- `Observation` is now a minimal structural core contract; the existing
  `GameObservation` implements timestamp, monotonic observation generation,
  availability, validity, confidence, and serialization without adding new CV.
- Configured ACTIVE is distinct from effective ACTIVE. DST input receives ACTIVE
  only while runtime verification, game readiness, initialized resources, and a
  non-paused/non-error worker state all hold; status otherwise reports safe OBSERVE
  and retains the requested mode in details.
- Queue-full delivery of GAME_LOST, PAUSE, verification revocation, or a safer mode
  now force-stops the current process generation and restarts it with parent-owned
  safe state, rather than allowing active behavior to continue.
- Documentation now records process/worker ownership, generation and ACK semantics,
  effective ACTIVE gating, NOOP alias/shared lifecycle, REPLAY availability, and
  fail-closed queue behavior. The runtime environment example includes the explicit
  runtime generation.
- `DSTGameWorker.shutdown()` now permits shutdown from INITIALIZING and cleans each
  action/deadman/input/capture resource independently. Cleanup failures remain
  observable as `WORKER_CLEANUP_FAILED` and cannot skip later cleanup steps.
- WorkerContext metadata is normalized to an immutable empty tuple and non-empty
  metadata is rejected at the IPC boundary. Host context updates reject changes to
  account/runtime/display/runtime-generation identity.

## Current Work
Stage 1 implementation and validation are complete. No code is currently in a
half-finished migration state.

## Bugs / Risks Found
- Process generation is implicit in queue replacement and is not present in IPC
  envelopes, so stale payload rejection is not an explicit invariant.
- ACK publication can evict an arbitrary report-queue item when full; that item can
  itself be a command ACK, contradicting the comment that ACKs are never evicted.
- WorkerProcessHost.start performs an unbounded `join()` on an already-dead handle;
  fake processes are harmless, but the lifecycle contract is not explicitly bounded.
- WorkerBridge uses a special in-process NOOP path rather than WorkerProcessHost,
  so NOOP does not exercise the same lifecycle core.
- The current mode representation lacks REPLAY and uses DISABLED rather than the
  requested canonical NOOP concept; compatibility requirements need careful
  resolution before changing values.
- Public command payloads are untyped dictionaries and validation is distributed
  across WorkerBridge and the child loop.
- The first targeted test run found one expected compatibility break: an existing
  test indexes queued commands as mappings. WorkerIPCCommand now provides read-only
  mapping-style access while remaining a typed dataclass; the fix awaits rerun.
- A command arriving after process death could previously spawn immediately through
  `command()` and bypass restart accounting/backoff; `_start_locked()` now routes
  all dead children through the canonical crash handler.
- Safety commands other than STOP could previously be dropped when the command
  queue was full. They now fail closed by terminating the affected generation.
- A configured ACTIVE worker reported ACTIVE before verification/game readiness,
  even though tick suppressed actions. Reports and GameActions now expose/use the
  effective gated mode instead.
- Direct DST shutdown from INITIALIZING raised an invalid-transition exception, and
  a cleanup exception could prevent later resources from closing. Both paths are
  now deterministic and covered by tests.

## Decisions
- Preserve the existing architecture and state machine; introduce only contracts
  required to make existing behavior explicit and safe.
- Preserve ProcessSupervisor separation: Steam/DST/Xvfb remain supervised there,
  while GameWorker keeps its purpose-built bounded WorkerProcessHost.
- Treat all pre-existing dirty-worktree changes as baseline/user-owned work.
- Use a dedicated bounded ACK queue rather than evicting status/ACK entries from a
  shared queue. ACK-channel saturation terminates the child so the parent converts
  every unresolved command to WORKER_CRASHED instead of losing it.
- Keep DISABLED as the control-plane wire value and expose NOOP as its enum alias;
  this avoids a protocol migration while making the canonical behavior explicit.

## Tests
- `python -m pytest -q -rs` -> 75 passed, 2 skipped in 42.34s.
  Skips: PostgreSQL integration needs TEST_DATABASE_URL; real Incus integration is
  opt-in on Linux.
- `python -m compileall -q runtime_agent app` after the first lifecycle change ->
  passed.
- Targeted compatibility rerun -> 19 passed.
- `python -m pytest -q tests/unit/test_gameworker_lifecycle.py
  tests/unit/test_runtime_hardening.py -rs` -> 34 passed in 0.16s.
- Targeted Ruff found one style-only nested-if issue after the latest safety change;
  it was patched; the next targeted run passed.
- Targeted lifecycle/runtime suite after real NOOP subprocess coverage -> 35 passed
  in 0.89s; targeted Ruff -> passed.
- Full `python -m pytest -q -rs` after implementation -> 94 passed, 2
  infrastructure-dependent skips in 41.98s.
- Latest targeted lifecycle/runtime suite after cleanup hardening -> 38 passed in
  0.83s; targeted Ruff -> passed.
- Final `python -m pytest -q -rs` -> 98 passed, 2 skipped in 42.96s.
- Final `python -m ruff check .` -> passed.
- Final `python -m compileall -q app runtime_agent node_agent scripts migrations
  tests` -> passed.
- Final `git diff --check` -> passed (Git emitted only existing LF/CRLF conversion
  warnings).
- `python -m alembic check` -> failed against the existing local SQLite database
  because SQLite reports eight resource byte columns as INTEGER while metadata uses
  BigInteger. Stage 1 changed no models or migrations; the dedicated fresh-database
  migration round-trip remains green.
- `python -m pytest -q tests/integration/test_migrations.py -rs` -> 1 passed in
  0.52s.

## Files Changed
- `GAMEWORKER_STAGE1_PROGRESS.md` (created for this stage).
- `runtime_agent/gameworker/ipc.py`
- `runtime_agent/gameworker/vision.py`
- `runtime_agent/gameworker/__init__.py`
- `runtime_agent/gameworker/process.py`
- `runtime_agent/gameworker/base.py`
- `runtime_agent/gameworker/config.py`
- `runtime_agent/gameworker/dst/worker.py`
- `runtime_agent/gameworker/noop.py`
- `runtime_agent/gameworker/state.py`
- `runtime_agent/worker_bridge.py`
- `runtime_agent/config.py`
- `runtime_agent/main.py`
- `app/runtime/bootstrap_models.py`
- `app/services/executor.py`
- `GAMEWORKER.md`
- `BOT_INTEGRATION.md`
- `FAILURE_MODES.md`
- `README.md`
- `deploy/env/runtime-agent.env.example`
- `tests/unit/test_gameworker_lifecycle.py`
- `tests/unit/test_runtime_hardening.py`

## Remaining
- No remaining GameWorker Stage 1 work.
- Stage 2 should define the external action contract, strengthen per-action
  cancellation/result semantics, verify input ownership and release-all behavior,
  and validate the bounded xdotool safety path on the intended Linux runtime.
- The unrelated local SQLite INTEGER/BigInteger Alembic comparison should be handled
  separately from GameWorker Stage 2, ideally with dialect-aware type comparison or
  validation against the production PostgreSQL database.

## Next Exact Action
Begin GameWorker Stage 2 by auditing `runtime_agent/gameworker/actions.py` and
`runtime_agent/gameworker/input.py` against the stable generation/cancellation
boundary, then specify action DTO and release-all invariants before implementation.

## Last Known Good State
The final state passes 98 tests with 2 infrastructure-dependent skips,
repository-wide Ruff, compileall, git diff whitespace validation, and a fresh
migration round-trip. Only `alembic check` against the existing local SQLite database
reports the unrelated INTEGER/BigInteger reflection drift documented above.
