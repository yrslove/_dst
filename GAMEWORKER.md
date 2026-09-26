# GameWorker

Status: code-complete foundation, `UNVALIDATED_ON_REAL_NODE`.

## Boundary

`DSTGameWorker` receives only `account_id`, `runtime_id`, runtime verification state,
and the canonical `DisplayEnvironment`. It has no Steam/email password, control-plane
or node bearer token, PostgreSQL connection, Incus API, inventory/trade logic, or
runtime lifecycle authority. It uses only runtime-scoped screen capture, visual
recognition, keyboard/mouse input, and process/readiness state supplied by the agent.

Every concrete worker, including `NoopGameWorker`, runs behind the same
`WorkerProcessHost` in a separate `multiprocessing` subprocess. Commands, replaceable
status, and terminal acknowledgements use separate bounded OS-local queues; no network
listener is created. Every spawn gets a monotonic worker generation and fresh queues.
Old-generation status/ACK payloads are rejected. A worker crash has a bounded,
exponentially backed-off independent restart budget and does not terminate DST or the
Runtime Agent.

## Modes and startup gate

- `DISABLED`: no capture or input; this is the default for every new runtime.
- `NOOP`: accepted as a configuration alias for the backward-compatible `DISABLED`
  wire value. The noop plugin supports only this mode.
- `OBSERVE`: capture and recognition run, `would_execute` is reported, and input is
  never emitted.
- `ACTIVE`: bounded input is permitted only after a fresh control-plane response says
  `runtime_verified=true`, the Runtime Agent has observed DST `READY`, the current
  worker generation has initialized successfully, and the worker is not paused,
  stopping, failed, or awaiting attention. Until every condition holds, configured
  ACTIVE reports and executes as safe OBSERVE.
- `REPLAY`: the core mode value and neutral observation boundary exist, but Stage 1
  reports `WORKER_MODE_UNAVAILABLE`; no replay engine is implemented yet.

`WORKER_AUTOSTART=0` is the default. Even a configured OBSERVE/ACTIVE worker is paused
after game readiness until an explicit resume. Configuration schema version is 1;
incompatible versions fail with `WORKER_CONFIG_VERSION_MISMATCH`.

## State machine

~~~text
DISABLED -> INITIALIZING -> WAITING_FOR_GAME -> OBSERVING
                                           -> READY / WAITING
                                           -> IDLE_ACTIVITY / NAVIGATING / INTERACTING
                                           -> RECOVERING -> OBSERVING
                                           -> NEEDS_ATTENTION
any running state -> PAUSED -> OBSERVING (explicit RESUME only)
any running state -> ERROR
any state -> SHUTTING_DOWN -> STOPPED
~~~

Transitions are centralized in `WorkerStateMachine` and retained in a bounded history.
Game readiness (`DST_PROCESS_RUNNING`, `DST_READY`) and worker readiness/activity are
separate signals.

`WorkerProcessHost` separately owns the process lifecycle:

~~~text
CREATED -> STARTING -> RUNNING
                     -> BACKOFF -> STARTING (bounded retries)
any live state -> STOPPING -> STOPPED
retry exhaustion / unkillable child -> FAILED
~~~

Calls to start, tick, command, stop, and shutdown are serialized. Explicit STOP
cancels backoff and forbids automatic restart; a later explicit RESUME may create a
new generation only after the prior stop has completed. Startup readiness, graceful
stop, terminate, and kill waits are bounded.

## Observation and vision

`X11ScreenCapture` captures only the root window of the runtime's canonical X display,
not the Node desktop. Each capture runs in a killable, timeout-bounded helper with a
sanitized graphical environment. It returns an immutable, size-bounded RGB `Frame`
carrying frame sequence, wall/monotonic timestamps, runtime/worker generation, source,
dimensions, and explicit coordinate space. The perception coordinator is strict
single-flight and retains at most one latest frame reference. Coordinates use
`Viewport`, `NormalizedPoint`, and `NormalizedRegion` in the 0.0..1.0 range.

`VisionDetector` uses OpenCV template matching with per-asset and default confidence
thresholds. Every result is `detected + confidence`. Templates are declared only in
`runtime_agent/gameworker/dst/assets/manifest.json` with ID, filename, expected
normalized region, threshold, and version. The repository intentionally contains no
fabricated DST screenshots. The registry rejects duplicate IDs, unsafe paths, missing
files, unsupported extensions, invalid thresholds, and dishonest verification types.
Until real screenshots are captured and reviewed, the empty production registry
reports `UNCONFIGURED` and perception-driven planning sends no input.

`GameObservation` records source-frame identity/timing, runtime and worker generation,
explicit `VALID`/`UNKNOWN`/`STALE`/`INVALID`/`UNAVAILABLE` validity, generic detections,
validated confidence, calibration/asset verification, screen hash, bounded
frame-change score, diagnostic flags, and a monotonic observation generation. It
implements the neutral Stage 1 `Observation` contract used by future live/replay
sources. Late, duplicate, cross-generation, expired, or pre-`GAME_READY` results are
discarded and do not replace the latest observation.

The Stage 3 flow is `CaptureSource -> Frame -> PerceptionEngine -> GameObservation ->
Planner -> Action -> Stage 2 safety gate`. The planner may return no action. It sees no
pixels and runs only for fresh, valid observations with verified assets and verified
calibration. In OBSERVE, any proposal receives terminal `SUPPRESSED`; synthetic tests
prove the input driver receives no gameplay operation.

## Input and actions

Stage 2 has one execution path: semantic `Action` -> generation/deadline validation ->
safety gate -> `ActionExecutor` -> `InputController` -> `InputDriver`. `Action` and
`ActionResult` are typed, pickle-safe, credential-free contracts. Every action has an
ID and exactly one terminal outcome; duplicate IDs return the original result without
repeating input. Gameplay actions require a short monotonic deadline and movement
requires an explicit bounded duration. No real DST movement duration is encoded here.

One bounded FIFO serializes normal gameplay actions per worker generation. Opposite
movement preempts safely. RELEASE_ALL/STOP, cancellation, GAME_LOST, deadman, and
shutdown bypass the FIFO, revoke input, release held state, and terminally resolve
queued/running actions. OBSERVE produces `SUPPRESSED`, not a false success. The Stage 1
effective ACTIVE gate remains authoritative.

`InputController` is the sole logical owner of pressed keys/buttons. The replaceable
`InputDriver` is only the low-level operation boundary: production uses bounded,
shell-free `XdotoolInputDriver`, while deterministic `FakeInputDriver` proves ordering,
failure, cancellation, and release behavior without X11. Failed key/button releases
do not stop later releases; uncertain operations are retried on the next release-all.
`DeadmanSafety` triggers only while input is held, latches input closed, and requires
an explicit reset.

`WORKER_ACTION_TIMEOUT` bounds action lifetime and duration (hard maximum five
seconds). `WORKER_ACTION_QUEUE_SIZE` bounds backlog, and
`WORKER_INPUT_SUBPROCESS_TIMEOUT` bounds every xdotool invocation. Action and key rate
limiters cap pathological loops.

## Activity, navigation, and recovery

`ActivityController.next_action(observation)` is separate from vision. It chooses no
action for unknown/unconfigured screens. The loop is observe -> select -> one short
action -> release/stop -> observe.

`NavigationController` provides short directional steps, timeout, cancellation, and
screen-change-based `StuckDetector`. Known-screen stuck recovery uses a bounded
alternate-action sequence. Unknown-screen recovery is intentionally stricter: release
input, observe again, then `NEEDS_ATTENTION`; it never presses random keys. Recovery
exhaustion is terminal until explicit operator action.

## Pause, VIEW, and emergency stop

The admin API exposes PAUSE, RESUME, STOP, and SET_MODE as durable runtime-bound
commands delivered over the authenticated heartbeat response. Interactive VIEW first
queues PAUSE. Its transport remains `CREATING` until a worker heartbeat confirms
PAUSED/DISABLED/STOPPED. Closing VIEW never resumes the worker automatically.
VIEW_ONLY does not revoke worker input ownership.

STOP WORKER is independent from STOP RUNTIME. An explicit RESUME may restart only the
worker subprocess within its configured mode; it does not restart Steam, DST, Incus,
or the runtime.

Control-plane command IDs are deduplicated by `WorkerBridge` and remain pending until
one terminal result is observed. A child crash resolves every pending ID as
`WORKER_CRASHED`; queue rejection is explicit. Safety revocations (`GAME_LOST`, PAUSE,
verification loss, safer mode, and STOP) are fail-closed: if the command queue is full,
the affected process generation is forcibly stopped rather than allowed to continue.

## Diagnostics and telemetry

Heartbeat fields include plugin/version/config version, mode/state, last tick/action/
observation, error code, restart count, and `would_execute`. Worker runs separately
track active seconds, pause seconds, actions, recoveries, and result. Error diagnostics
may contain one screenshot, observation JSON, and the last transitions. Rotation is
bounded by count and bytes; normal frames never enter that diagnostic ring. The
separately configured session recorder owns sampled production-frame persistence.

OpenCV, Pillow, and NumPy live in `requirements-runtime.txt`, not the control-plane
requirements. Defaults bound capture to 1280x720, capture/perception/planner deadlines,
frame/observation age, and low adaptive observation rates. Telemetry exposes capture
errors, last capture/frame age, perception latency, and last valid observation without
serializing frame pixels.

## Recording and offline replay

Stage 4 records an OBSERVE or ACTIVE session as a versioned directory containing an
atomically replaced `manifest.json`, append-only `events.jsonl`, and lossless PNG
payloads under `frames/`. The initial manifest is explicitly `INTERRUPTED`; only a
bounded graceful close may publish `COMPLETE`. Limit exhaustion publishes
`TRUNCATED`, and disk errors publish `FAILED` when the manifest remains writable.
The recording queue, duration, sampled frame count, bytes, event size, and shutdown
wait are all bounded. Queue pressure drops recording work rather than blocking the
worker, and every recorder is bound to one runtime/worker generation.

`ReplayCaptureSource` implements the same `CaptureSource -> Frame` contract as X11.
It validates the manifest/version, event order, generations, frame identities,
dimensions, declared sizes, PNG payloads, and root containment before replay. An
explicitly incomplete session may expose only its unambiguous valid prefix; unknown
versions and ambiguous corruption fail before perception. Replay remaps frames into
the current, separately identified replay generation while retaining recorded
generation provenance in bounded metadata.

`ReplayRunner` feeds that source through the ordinary `ObservePipeline` and current
perception/planner code. Fast mode advances recorded monotonic time without sleeping;
recorded-timing and programmatic STEP policies share a cancellable `ReplayClock`.
The replay action boundary is a dedicated input-free sink that always returns
`SUPPRESSED`; no `InputController` or `InputDriver` is constructed in REPLAY mode.
Recorded observations remain events for expected-vs-recomputed comparison and never
replace newly computed observations.

Heartbeat diagnostics now include bounded capture/perception/planner/cycle latency,
capture/perception/stale/suppression counters, recording state and queue counters,
replay progress, latest observation/proposal/result, and explicit input-safety state.
Raw image bytes, environment dumps, and credential-like metadata are excluded.

## Limitations

No visual template or movement timing is claimed valid. Real X11 key/button delivery,
XAUTHORITY, DST focus transitions, keyboard repeat, and hard process death while an X
key is held still require a Linux runtime. Parent-owned emergency key/button reset runs
after child death and before a replacement generation is spawned, but its physical X11
effect cannot be proven on this development host. Physical X11 capture, real DST
viewport/UI scale, templates, thresholds, and calibration remain unverified; therefore
perception-driven ACTIVE stays closed. Actual recording throughput, compression cost,
hard-kill filesystem behavior, and replay against a real DST dataset remain for the
real-host validation stage. This module contains no anti-detect,
fingerprint spoofing, enforcement bypass, injection, CAPTCHA, registration, Steam
Guard, proxy rotation, trade, market, transfer, or cashout automation.
