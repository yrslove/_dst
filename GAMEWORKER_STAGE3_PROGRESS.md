# GameWorker Stage 3 Progress

## Goal
Build a game-independent, production-shaped `CaptureSource -> Frame ->
PerceptionEngine -> Observation -> Planner -> Action -> SUPPRESSED` pipeline with
bounded memory, freshness/generation safety, explicit UNKNOWN semantics, and honest
asset/calibration verification gates.

## Current Status
COMPLETE

## Baseline
- Pre-change `python -m pytest -q -rs`: 116 passed, 2 infrastructure skips in
  44.58s.
- The working tree already contained the user's Stage 1/2 and platform changes; they
  were preserved.
- Pillow, OpenCV-headless, and NumPy were already declared in
  `requirements-runtime.txt`. The missing local OpenCV package was installed from
  that existing file for validation; dependency manifests were not changed.

## Final Architecture
`RuntimeAgent -> WorkerBridge -> WorkerProcessHost -> DSTGameWorker -> CaptureSource
-> immutable Frame -> PerceptionEngine -> GameObservation -> Planner -> Action ->
Stage 2 Safety Gate -> ActionExecutor -> InputController -> InputDriver`.

`ObservePipeline` is strict single-flight. It owns a capacity-one latest-frame slot,
generation/session validation, monotonic freshness checks, an ordered observation
store, planner dispatch, and the Stage 2 action boundary. No capture/perception queue,
recording buffer, or replay state was added.

## Completed
- Added canonical `CaptureSource`, deterministic bounded `FakeCaptureSource`, typed
  capture failures, and a capacity-one `LatestFrameSlot`.
- Replaced mutable/unversioned screenshots with immutable RGB-byte `Frame` objects
  carrying IDs, sequence, wall/monotonic timestamps, runtime identity/generation,
  worker generation, dimensions, coordinate space, source, and bounded metadata.
- Isolated X11/Pillow capture in a killable per-capture helper process with sanitized
  environment, bounded IPC payload, timeout, termination/kill cleanup, parent-death
  protection on Linux, and close/start race protection.
- Added explicit normalized/frame/viewport coordinate spaces, validated points and
  regions, offset `Viewport`, and versioned `CalibrationProfile` with verified state.
- Strengthened the existing `GameObservation` rather than creating a parallel world
  DTO. It now includes source provenance/timing, runtime/worker generations,
  observation generation, validity, freshness, detections, confidence, verification,
  calibration identity, latency, screen change, and bounded flags.
- Added generic validated `Detection`, canonical `ObservationValidity`, and a
  `PerceptionEngine` protocol. UNKNOWN/STALE/INVALID/UNAVAILABLE are first-class and
  never force a guess.
- Hardened `AssetRegistry`: schema/profile, unique IDs, relative root containment,
  PNG policy, missing-file detection, threshold/version/use semantics, strict boolean
  verification, and manifest/template/count size bounds.
- Kept the production manifest empty. No screenshots, fake DST assets, thresholds,
  or verified calibration were fabricated. Perception-driven planning requires both
  verified assets and matching verified calibration, so ACTIVE remains fail-closed.
- Added minimal `Planner`, `NullPlanner`, `FakePlanner`, and bounded immutable
  `ActionProposal`. The production placeholder policy is conservative and may return
  no action.
- Integrated the pipeline into `DSTGameWorker`, telemetry, configuration, env
  examples, and documentation without placing a perception dependency on the
  explicit/manual Stage 2 action path.
- Added operational health for capture errors, last capture, frame age, perception
  latency, and last valid observation. Raw pixels are never logged/reported/JSON
  serialized.
- `GAME_LOST`, pause, shutdown, and session changes invalidate observations and
  discard in-flight results. A new `GAME_READY` requires a strictly newer frame.
- Added persistent ordering high-water marks, exact observation/frame provenance,
  and nonblocking single-flight admission so late, duplicate, overlapping, or
  cross-generation results cannot replace current state.

## Bugs / Risks Found and Fixed
- X11 capture returned mutable PIL objects without IDs, generation, monotonic
  freshness, size bounds, timeout, or cancellable ownership.
- Thumbnail resolution reporting described the pre-thumbnail image.
- Viewport accepted invalid dimensions and could not represent an offset.
- Detection confidence and provenance were unvalidated; low confidence, stale data,
  missing assets, and backend errors were conflated.
- Duplicate template IDs overwrote silently and template paths were not contained.
- In-process ImageGrab could hang the worker. Capture now has a killable helper.
- `cv2.imread` failed on Unicode Windows paths. Template loading now uses contained
  bytes plus `cv2.imdecode`.
- Observation ordering originally lost its watermark on invalidation and could accept
  a delayed old result. High-water marks now survive session invalidation.
- UNKNOWN observations were initially at risk of being reported as stale because
  validity and temporal expiry were coupled in one convenience predicate. The
  coordinator now checks expiry separately before UNKNOWN gating.
- Capture close could race between helper construction and start. Helper start and
  active ownership publication are now atomic with respect to close.
- First worker preparation could apply `GAME_READY` twice. Initial and existing
  pipeline paths are now distinct.

## Previous-Stage Regression Found
Stage 1 created the isolated GameWorker process as daemonic. Python forbids a daemon
process from owning the Stage 3 capture helper. The worker process is now non-daemonic;
its existing bounded graceful/terminate/kill lifecycle remains authoritative. The
capture child additionally has Linux parent-death protection. Lifecycle and full-suite
tests pass after the change.

## Tests Added or Updated
- Added `tests/unit/test_gameworker_perception.py` with 29 executed cases covering
  Frame validation/immutability/freshness, coordinate conversion,
  fake/repeated/failing capture, latest-frame retention, X11 timeout cleanup,
  confidence/provenance validation, template registry safety, real OpenCV matching
  on synthetic fixtures, detector failure isolation, observation ordering, stale and
  generation gates, UNKNOWN/INVALID/failure behavior, perception/planner timeouts,
  capture health, single-flight, GAME_LOST/recovery, capture/perception/planner/shutdown
  cancellation, and exact end-to-end OBSERVE suppression.
- The OBSERVE invariant is exercised for movement, turning, interaction, cancel, and
  inventory proposals. Every result is terminal `SUPPRESSED`; `FakeInputDriver`
  receives zero gameplay events.

## Validation
- Targeted Stage 3: 29 passed.
- Stage 2/lifecycle + Stage 3 targeted: 69 passed.
- Full suite: 145 passed, 2 skipped in 50.35s.
- Expected skips: PostgreSQL requires `TEST_DATABASE_URL`; real Incus is opt-in on
  Linux.
- Migration regression: 1 passed in 0.60s.
- `python -m ruff check .`: passed.
- `python -m compileall -q app runtime_agent node_agent scripts migrations tests`:
  passed.
- `git diff --check`: passed; only repository-wide LF-to-CRLF advisory warnings were
  emitted by Git on this Windows checkout.

## Files Changed for Stage 3
- `.env.example`
- `deploy/env/runtime-agent.env.example`
- `GAMEWORKER.md`
- `GAMEWORKER_STAGE3_PROGRESS.md`
- `runtime_agent/gameworker/__init__.py`
- `runtime_agent/gameworker/activity.py`
- `runtime_agent/gameworker/base.py`
- `runtime_agent/gameworker/capture.py`
- `runtime_agent/gameworker/config.py`
- `runtime_agent/gameworker/dst/worker.py`
- `runtime_agent/gameworker/geometry.py`
- `runtime_agent/gameworker/perception.py`
- `runtime_agent/gameworker/process.py`
- `runtime_agent/gameworker/vision.py`
- `tests/unit/test_gameworker_perception.py`

## Known Limitations / Unverified Items
- Physical Linux X11 capture, DISPLAY/XAUTHORITY permissions, helper parent-death
  behavior, capture/perception latency, and CPU cost require a real runtime node.
- Actual DST fullscreen/window dimensions, viewport, UI scale, focus, image format,
  real template content, thresholds, and calibration remain unverified.
- The production registry intentionally has no verified DST assets and
  `WORKER_CALIBRATION_VERIFIED` defaults false. This blocks perception-driven ACTIVE
  while preserving the independently gated manual/non-perception Stage 2 path.
- In-process OpenCV operations are input-size/count bounded and deadline-checked, but
  their real-host runtime characteristics cannot be proven on this Windows host. No
  asynchronous perception/planner tasks are created, so timeout/session invalidation
  cannot leak background work.

## Stage 4 Boundary
No recording format, frame recorder, replay clock, session storage, or replay engine
was created. Those remain exclusively GameWorker Stage 4 work.

## Next Exact Action
Begin GameWorker Stage 4 — Recording / Replay / Diagnostics, using the stable Frame,
Observation, generation, and planner/action boundaries established here.

## Last Known Good State
145 passed, 2 expected infrastructure skips; migration 1 passed; Ruff, compileall,
and git diff check all pass.
