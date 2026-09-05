# GameWorker integration boundary

## Runtime platform foundation

`DSTGameWorker` is implemented but defaults to DISABLED; `NoopGameWorker` remains the
fallback. `GAME_READY` is an explicit agent readiness state and remains unvalidated
until the real-node checklist is run.

app/workers/contracts.py defines:

~~~python
prepare(context)
on_game_ready(context)
tick(context)
pause()
resume()
status()
shutdown()
~~~

The fallback app/workers/noop.py implementation always reports WORKER_IDLE and performs no keyboard/mouse input or CV. The concrete worker is documented in GAMEWORKER.md.

RuntimeAgent owns display/Steam/DST supervision and invokes the worker only after explicit GAME_READY. The worker must not receive Steam passwords, database access, Incus access, admin cookies or agent bearer tokens.

Correct data/control boundary:

~~~text
GameWorker plugin -> WorkerBridge -> RuntimeAgent heartbeat -> Control plane
~~~

Plugin selection has a versioned config, subprocess isolation, resource bounds, and a
verified-runtime gate. Merely observing a DST process never sets WORKER_ACTIVE.

## Implemented plugin boundary

`DSTGameWorker 0.1.0` now implements the contract described in `GAMEWORKER.md` and is
isolated in a subprocess. `NoopGameWorker` remains a selectable fallback. Safe defaults are
`worker_plugin=dst`, `mode=DISABLED`, and `autostart=false` for newly bootstrapped
runtimes. There are no validated visual assets yet, so OBSERVE reports UNCONFIGURED
instead of producing a false positive.
