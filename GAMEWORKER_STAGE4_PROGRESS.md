# GameWorker Stage 4 Progress

## Goal
Build bounded, crash-detectable recording, deterministic offline replay through the
existing Stage 3 pipeline, and bounded operational diagnostics without requiring a
live Don't Starve Together session.

## Current Status
COMPLETE

## Previous Stage Contracts
- Stage 1 provides runtime/worker generations, bounded IPC lifecycle, REPLAY enum
  foundation, immutable observations, and fail-closed mode transitions.
- Stage 2 provides canonical Action/ActionResult contracts, a bounded executor,
  explicit input safety gates, and deterministic fake input.
- Stage 3 provides immutable RGB Frame, CaptureSource, single-flight
  ObservePipeline, GameObservation/Detection provenance, calibration/assets state,
  planner boundary, freshness checks, and GAME_LOST invalidation.

## Baseline
- The repository is intentionally dirty with the user's prior Stage 1–3 and
  platform work; those changes are preserved.
- `python -m pytest -q -rs` -> 145 passed, 2 expected infrastructure skips in
  46.06 seconds.
- No recorder, recording session, replay source, JSONL helper, or atomic manifest
  utility currently exists.
- REPLAY is an enum value but `DSTGameWorker.prepare()` currently reports
  `WORKER_MODE_UNAVAILABLE`.

## Plan
- [x] Read Stage 1–3 checkpoints and audit the actual capture/perception/action,
  configuration, diagnostics, persistence, lifecycle, and tests.
- [x] Establish the pre-change full-suite baseline.
- [x] Implement a versioned RecordingSession and bounded asynchronous recorder.
- [x] Persist lossless frames plus structured lifecycle/observation/proposal/result
  events with atomic finalization and explicit incomplete states.
- [x] Implement strict, path-safe ReplayCaptureSource and deterministic replay clock.
- [x] Integrate recording and real REPLAY mode with the existing ObservePipeline.
- [x] Extend bounded diagnostics/telemetry and configuration.
- [x] Add recording, replay, corruption, safety, generation, failure, cancellation,
  determinism, and end-to-end tests.
- [x] Complete second-pass audit and full validation.

## Completed
- Initial repository/checkpoint audit and baseline validation.
- Added a v1 self-describing session format with an atomic manifest, JSONL causal
  event stream, separately persisted lossless PNG payloads, and UUID identity.
- Added a bounded asynchronous SessionRecorder with sampling, duration/frame/byte
  limits, non-blocking queue semantics, generation enforcement, disk-failure
  isolation, bounded close, and COMPLETE/INTERRUPTED/TRUNCATED/FAILED states.
- Added strict ReplayCaptureSource validation, path containment, lazy pixel loading,
  explicit replay-generation mapping, incomplete-prefix handling, ReplayClock fast/
  recorded/step policies, and cancellable replay.
- Added ReplayRunner over the existing ObservePipeline and a dedicated input-free
  ReplayActionSink that can only produce SUPPRESSED results.
- Integrated live recording hooks and REPLAY lifecycle into DSTGameWorker. Mode
  changes involving REPLAY tear down the old source before constructing the new one.
- Added bounded report diagnostics and counters for capture, perception, planner,
  observation cycle, recording, replay, suppression, and input safety.
- Added and documented the minimal Stage 4 configuration surface.
- Added streamed access to recorded events and per-frame recorded observations so
  regression code can compare expected/recorded data with current recomputation.
- Second-pass audit closed event ordering, dropped-frame causal references, replay
  provenance retention, initial empty event-stream creation, live/replay source
  replacement, direct-action replay suppression, raw-frame buffer accounting, and
  manifest counter/limit consistency.

## Current Work
Stage 4 implementation, second-pass audit, documentation, and validation are complete.

## Bugs / Risks Found
- REPLAY is not implemented and currently transitions to ERROR.
- Existing diagnostics write directly and synchronously with no reusable snapshot.
- Stage 3 pipeline does not expose capture/planner/cycle latency counters yet.
- VisionDetector directly reads wall/monotonic clocks, so deterministic replay needs
  a narrow clock seam or normalized replay results.
- A hard-killed child cannot finalize its manifest; the initial on-disk manifest
  must therefore be explicitly incomplete before any queued write begins.
- A sampled/dropped frame must also suppress later observation/proposal events that
  reference it, otherwise an otherwise healthy session becomes unreplayable.
- Switching an existing live worker into REPLAY (or back) must close and replace the
  capture/pipeline ownership graph; changing only the enum would reuse the old input.

## Decisions
- Treat current repository contents as source of truth and do not redesign Stage 1–3.
- Store lossless image payloads separately from bounded JSONL metadata.
- Write an explicit incomplete manifest first and atomically replace it only during
  state changes/finalization; a hard kill can never masquerade as COMPLETE.
- Use a bounded writer queue and prefer dropping recording frames over blocking the
  perception hot path.
- Remap replay frames into the current replay runtime/worker generation and preserve
  the recorded generations only as explicit provenance; never present replay as live.
- Keep recorded observations as expected/reference events while recomputing current
  observations from source frames through the Stage 3 pipeline.
- Validate all referenced images before replay begins, but decode pixels one frame at
  a time during capture.

## Tests
- Baseline: `python -m pytest -q -rs` -> 145 passed, 2 skipped in 46.06s.
- New Stage 4 module: 20 tests passed.
- Stage 1–4 critical targeted suite: 89 tests passed in 2.16s.
- Targeted Ruff and compile checks pass.
- Final Stage 4 module: 41 passed.
- Final Stage 1–4 critical targeted suite: 102 passed.
- Final full suite: 186 passed, 2 expected infrastructure skips in 49.71s.
- `python -m ruff check .` -> passed.
- `python -m compileall -q app runtime_agent node_agent scripts migrations tests` ->
  passed.
- `git diff --check` -> passed; Git emitted only repository-wide LF/CRLF advisory
  warnings.
- `python -m pytest -q tests/integration/test_migrations.py -rs` -> 1 passed in
  0.80s.
- `python -m alembic check` continues to report the pre-existing local SQLite
  INTEGER/BigInteger reflection drift on eight resource byte columns. Stage 4 changed
  no database model or migration.

## Files Changed
- `GAMEWORKER_STAGE4_PROGRESS.md`
- `.env.example`
- `deploy/env/runtime-agent.env.example`
- `GAMEWORKER.md`
- `runtime_agent/gameworker/__init__.py`
- `runtime_agent/gameworker/config.py`
- `runtime_agent/gameworker/dst/worker.py`
- `runtime_agent/gameworker/perception.py`
- `runtime_agent/gameworker/recording.py`
- `runtime_agent/gameworker/replay.py`
- `runtime_agent/gameworker/vision.py`
- `tests/unit/test_gameworker_lifecycle.py`
- `tests/unit/test_gameworker_recording.py`

## Remaining
- No remaining Stage 4 infrastructure work.
- Real Linux/X11 capture, actual disk throughput/compression, hard SIGKILL behavior,
  and a real DST recording dataset remain host/game validation items.

## Next Exact Action
Begin REAL DST VALIDATION / DATA COLLECTION: validate X11 capture and viewport, make
the first bounded OBSERVE recording, then replay that artifact offline before any
controlled ACTIVE validation.

## Last Known Good State
The full available suite passes 186 tests with 2 expected infrastructure skips;
repository-wide Ruff, compileall, diff whitespace validation, and fresh migration
regression pass. Only the documented, unrelated local SQLite Alembic type-reflection
drift remains.
