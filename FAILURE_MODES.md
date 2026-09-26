# Failure modes

## Bootstrap and leadership

Bootstrap commits every completed phase and retries from that phase. A failed
phase records `RUNTIME_BOOTSTRAP_FAILED`; no token appears in its message.
Scheduler leadership loss stops new global decisions immediately; existing
durable job leases remain valid and are not a global singleton.

## Control plane dies after enqueue

Job и desired state уже committed. После restart executor claim’ит PENDING/RETRY job. Integration test перезапускает app на той же DB и проверяет completion.

## Executor dies while leased

leased_until истекает. Следующий claim переводит abandoned attempt в ABANDONED и повторяет job, пока max_attempts не исчерпан.

## START response timeout

Actual instance state неизвестен. Runtime становится STALE, slot lease остаётся active и retry/reconciler инспектируют тот же external_id. Ensure/start идемпотентны; duplicate container не создаётся.

## Capacity contention

Node row lock + durable lease сериализуют claims. Тест 20 concurrent START при max=4 проверяет STARTING/RUNNING <= 4.

## Node heartbeat expires

Node -> OFFLINE, его STARTING/RUNNING runtimes -> STALE с NODE_OFFLINE. Runtime и Account не удаляются; destructive restart не выполняется.

## Runtime heartbeat expires

Runtime -> STALE, Account -> NEEDS_ATTENTION, error AGENT_STALE. Lease recovery выполняется только после expiry и отсутствия live executor claim.

## Steam/DST dies

Watchdog отличает STEAM_NOT_RUNNING и DST_NOT_RUNNING по agent phase. RuntimeAgent делает не более configured process restarts с exponential backoff, затем NEEDS_ATTENTION.

## Login/session lost

Runtime остаётся NEEDS_LOGIN. Rebuild не копирует credentials/session автоматически. Operator подключается через approved VIEW integration и запускает VERIFY после ручной проверки.

## PostgreSQL unavailable

Ready endpoint отвечает 503; live остаётся 200. Один dead Node не влияет на central readiness.

## Encryption key lost

Account secrets невосстановимы. DB backup бесполезен для secrets без отдельно защищённой копии Fernet key.

## Rebuild partially fails

Account и старая runtime history сохраняются. Новая generation получает ERROR/diagnostic. Automatic credential migration отсутствует.

## Worker failures

- Capture/display/input failures release all input and report a canonical worker error.
- Unknown or unconfigured screens never emit input; bounded observation recovery ends
  in NEEDS_ATTENTION.
- Stuck navigation uses short alternate actions and a bounded recovery count.
- Deadman timeout releases tracked keys/buttons independently of the main tick.
- Worker crash consumes only the worker restart budget. DST and Incus are untouched.
- DST/display disappearance pauses worker ownership; Runtime Agent remains supervisor.
- Every worker spawn receives fresh bounded IPC channels and a monotonic generation;
  stale status/ACK payloads are ignored and pending commands resolve as WORKER_CRASHED.
- Periodic status cannot evict terminal ACKs. ACK-channel saturation terminates the
  child so commands receive a deterministic crash outcome instead of disappearing.
- A full command queue cannot drop safety revocation: the current worker generation is
  force-stopped and any restart inherits paused/unverified/game-lost parent state.

## Remote view failures

Missing xpra/Incus/X socket returns REMOTE_VIEW_BACKEND_UNAVAILABLE and persists ERROR,
never ACTIVE. Interactive sessions remain CREATING until PAUSED is confirmed. Expired
sessions retain a durable cleanup state until the ephemeral proxy/xpra removal is
confirmed (including an idempotent already-absent result).
