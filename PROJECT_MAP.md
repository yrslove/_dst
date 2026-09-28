# Project Map

Navigation only. Current status and priorities are in [CURRENT_STATE.md](CURRENT_STATE.md).

| Subsystem | Purpose | Primary files / entry point | Tests |
|---|---|---|---|
| Control Plane runtime lifecycle | API startup, lifecycle executor and provider operations | `app/main.py`; `app/services/executor.py` | `tests/integration/test_jobs_runtime.py`, `test_failure_recovery.py` |
| Agent heartbeat / reconciliation / watchdog | Record liveness, converge desired runtime state, detect stale agents | `app/services/agents.py`, `reconciler.py`, `watchdog.py`; started in `app/main.py` | `tests/api/test_agents_nodes.py`, `tests/integration/test_reconciliation_hardening.py` |
| Remote View lifecycle | Create, authorize, proxy, and close temporary remote-view sessions | `app/services/views.py`; routes in `app/main.py` | `tests/api/test_views.py` |
| Runtime Agent | Own runtime process, heartbeat, and worker lifecycle | `runtime_agent/main.py` | `tests/unit/test_runtime_agent.py`, `test_gameworker_lifecycle.py` |
| ProcessSupervisor | Manage Steam/DST/display process lifecycle | `runtime_agent/process_supervisor.py`; process adapters in `runtime_agent/processes/` | `tests/unit/test_runtime_hardening.py`, `test_gameworker_lifecycle.py` |
| WorkerBridge / GameWorker | Bridge agent commands/results to the DST worker | `runtime_agent/worker_bridge.py`; `runtime_agent/gameworker/dst/worker.py` | `tests/unit/test_gameworker_lifecycle.py`, `test_gameworker_actions.py` |
| Perception / vision | Capture and classify game state, regions, and geometry | `runtime_agent/gameworker/perception.py`, `vision.py`, `capture.py`; worker observation path | `tests/unit/test_gameworker_perception.py` |
| ActivityController | Execute bounded activity-level operations | `runtime_agent/gameworker/activity.py`; `ActivityController` | `tests/unit/test_dst_behavior.py`, `test_gameworker_actions.py` |
| ActionExecutor / InputController / XpraInputDriver | Verify actions and send canonical bounded input | `runtime_agent/gameworker/actions.py`, `input.py`, `xpra_input.py` | `tests/unit/test_gameworker_actions.py`, `test_xpra_input.py` |
| Transition / action contracts | Define accepted state transitions and action semantics | `runtime_agent/gameworker/transitions.py`, `state.py`, `ipc.py`; DST worker command handling | `tests/unit/test_gameworker_transitions.py`, `test_state_machine.py` |
| Recording / replay | Bound diagnostic action/observation recordings and replay them | `runtime_agent/gameworker/recording.py`, `replay.py` | `tests/unit/test_gameworker_recording.py` |
| Diagnostics | Collect worker/runtime diagnostic state and reports | `runtime_agent/diagnostics.py`, `runtime_agent/gameworker/diagnostics.py` | `tests/unit/test_runtime_hardening.py`, `test_gameworker_lifecycle.py` |
| Config | Environment-backed Control Plane and Runtime Agent settings | `app/config.py`, `runtime_agent/config.py` | `tests/unit/test_config_and_secrets.py` |
| Tests | API, integration, provider, concurrency, and worker coverage | `tests/` | Grouped under `tests/api/`, `integration/`, `providers/`, `concurrency/`, `unit/`, `postgres/` |
