# Архитектура

## Ownership

~~~text
Browser
  -> FastAPI control plane
       -> PostgreSQL (truth: desired state, jobs, leases, history)
       -> RuntimeProvider
            -> Incus CLI

Linux NODE
  -> Incus
  -> NodeAgent -> authenticated resource heartbeat
  -> Runtime containers
       -> RuntimeAgent -> authenticated process/readiness heartbeat
       -> isolated DSTGameWorker (Noop fallback)
~~~

Account не является container. У Account ровно одна active RuntimeInstance generation; partial unique index обеспечивает invariant. Rebuild деактивирует старую generation и создаёт новую, сохраняя Account/history.

## Transactional command path

~~~text
POST START + authenticated admin + CSRF
  -> transaction: lock account/runtime, desired_state=RUNNING, enqueue idempotent Job
  -> 202 Accepted
  -> JobExecutor claim via FOR UPDATE SKIP LOCKED
  -> transaction: lock Node, claim durable RuntimeLease, state=STARTING
  -> provider.start outside transaction
  -> transaction: state=RUNNING, Run/event
~~~

SQLite development/concurrency tests используют BEGIN IMMEDIATE. PostgreSQL production использует row locks/SKIP LOCKED. Node capacity считается как union runtime STARTING/RUNNING и unexpired active leases, поэтому одна runtime не считается дважды.

START timeout не освобождает slot мгновенно: provider мог фактически стартовать instance. Runtime помечается STALE, lease сохраняет capacity до reconciliation/expiry.

## Background responsibilities

- Scheduler создаёт недостающие jobs из desired state и учитывает enabled/drain/maintenance/online.
- JobExecutor выполняет одну infrastructure command и bounded retry.
- Reconciler только сравнивает DB state с provider; lifecycle actions не запускает.
- Watchdog определяет stale Node/runtime/process reports и не выполняет destructive recovery.

## State machines

Все transitions проходят через app/domain/state.py. DESTROYED — terminal. Account, Runtime и Node имеют отдельные graphs. Поля state/desired_state разделены.

## Agents

NodeAgent сообщает CPU/load/RAM/disk/GPU/VRAM, Incus availability и active runtime count. RuntimeAgent сообщает graphical session, Steam/DST process state, explicit readiness и worker state. Heartbeat использует entity-bound hash-only bearer credential и protocol_version=1.

Runtime process existence не означает readiness. Marker files должны создаваться проверенным launcher integration. GAME_READY разрешает только отдельную инициализацию worker; его mode/state остаются независимыми.

## Remote view

RuntimeViewProvider создаёт короткоживущую session. DB хранит только token hash и opaque backend session ID. Disabled provider — production-safe default; mock служит тестовым integration hook. Concrete xpra backend использует loopback-only Incus proxy и authenticated FastAPI HTTP/WebSocket adapter; real-node validation ещё не выполнена.

## Runtime bootstrap and leadership

`RuntimeBootstrapService` persists every phase from `RUNTIME_CREATED` through
`BOOTSTRAP_COMPLETE`. Retries resume after the last committed phase. Its agent
configuration is an explicit allow-list; the runtime token is encrypted at rest,
injected mode 0600, and omitted from logs, events, and diagnostics.

Scheduler and reconciler acquire a short database leadership lease with a fencing
token before global decisions. Executors remain independently concurrent through
durable job leases. Display, Steam and DST are separate agent dependencies;
container `RUNNING` is not application readiness. Linux behavior is
`UNVALIDATED_ON_REAL_NODE`.

## Scaling boundary

Один FastAPI deployment, одна PostgreSQL и фоновые workers достаточны. Job/lease locking допускает несколько executors, но reference systemd unit запускает один process. Kubernetes, Redis, Kafka и service mesh не используются.

## Runtime automation wave

The canonical runtime chain is now `DisplayManager -> SteamProcess -> DSTProcess ->
WorkerBridge -> isolated DSTGameWorker`. Steam/DST and xpra inherit the same canonical
`DisplayEnvironment`. Runtime phase remains GAME_READY while worker mode/state is a
separate heartbeat contract. Worker control is durable in PostgreSQL and delivered
only in authenticated runtime heartbeat responses.

Interactive VIEW and worker input are mutually exclusive. The view service queues
PAUSE and does not expose transport until PAUSED is reported. See `GAMEWORKER.md` and
`REMOTE_VIEW.md`. Linux behavior remains `UNVALIDATED_ON_REAL_NODE`.
