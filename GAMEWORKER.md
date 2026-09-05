# GameWorker

Status: code-complete foundation, `UNVALIDATED_ON_REAL_NODE`.

## Boundary

`DSTGameWorker` receives only `account_id`, `runtime_id`, runtime verification state,
and the canonical `DisplayEnvironment`. It has no Steam/email password, control-plane
or node bearer token, PostgreSQL connection, Incus API, inventory/trade logic, or
runtime lifecycle authority. It uses only runtime-scoped screen capture, visual
recognition, keyboard/mouse input, and process/readiness state supplied by the agent.

The concrete worker runs in a separate `multiprocessing` subprocess. Commands and
reports use bounded OS-local queues; no network listener is created. A worker crash
has a bounded independent restart budget and does not terminate DST or Runtime Agent.
`NoopGameWorker` remains the fallback plugin.

## Modes and startup gate

- `DISABLED`: no capture or input; this is the default for every new runtime.
- `OBSERVE`: capture and recognition run, `would_execute` is reported, and input is
  never emitted.
- `ACTIVE`: bounded input is permitted only after a fresh control-plane response says
  `runtime_verified=true` and the Runtime Agent has observed DST `READY`.

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

## Observation and vision

`X11ScreenCapture` captures only the root window of the runtime's canonical X display,
not the Node desktop. Recognition frames are bounded by configured dimensions and
frequency. Coordinates use `Viewport`, `NormalizedPoint`, and `NormalizedRegion` in
the 0.0..1.0 range.

`VisionDetector` uses OpenCV template matching with per-asset and default confidence
thresholds. Every result is `detected + confidence`. Templates are declared only in
`runtime_agent/gameworker/dst/assets/manifest.json` with ID, filename, expected
normalized region, threshold, and version. The repository intentionally contains no
fabricated DST screenshots. Until real screenshots are captured and reviewed, the
registry reports `UNCONFIGURED`/`WORKER_ASSET_MISSING` and sends no input.

`GameObservation` records visibility/confidence, screen hash, bounded frame-change
score, and diagnostic flags. A static UI alone is not called frozen: freeze escalation
requires ACTIVE mode and recent worker activity.

## Input and actions

`InputController` uses `xdotool` on the canonical DISPLAY. `InputLease` allows one
component to own input. Action and key rate limiters cap buggy loops. `DeadmanSafety`
runs independently and releases all tracked keys/buttons if the worker stops touching
its timer. Pause, error, stop, process shutdown, runtime shutdown, and agent shutdown
all release input.

Only `GameActions` knows configurable bindings. High-level bounded operations include
move forward/backward, turn left/right, interact, cancel, and inventory-menu key. The
worker logs action names and outcomes, never mouse-pixel streams. No action duration
may exceed `WORKER_ACTION_TIMEOUT` (hard validation limit: five seconds).

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

## Diagnostics and telemetry

Heartbeat fields include plugin/version/config version, mode/state, last tick/action/
observation, error code, restart count, and `would_execute`. Worker runs separately
track active seconds, pause seconds, actions, recoveries, and result. Error diagnostics
may contain one screenshot, observation JSON, and the last transitions. Rotation is
bounded by count and bytes; normal frames are not written to disk.

OpenCV, Pillow, and NumPy live in `requirements-runtime.txt`, not the control-plane
requirements. Defaults bound capture to 1280x720 and low adaptive observation rates.

## Limitations

No visual template is claimed valid. X11 capture, xdotool, real DST window behavior,
scaling, bindings, frame thresholds, CPU use, and subprocess behavior must be validated
on one Linux runtime in OBSERVE before ACTIVE. This module contains no anti-detect,
fingerprint spoofing, enforcement bypass, injection, CAPTCHA, registration, Steam
Guard, proxy rotation, trade, market, transfer, or cashout automation.
