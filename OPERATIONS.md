# Operations

## Platform foundation status

Account health distinguishes container, agent, display, Steam, DST, and worker
mode/state. Bootstrap phase/version is visible with the runtime image/generation.
Remote view is disabled by default; xpra is the concrete unvalidated backend.

## Health

- /health/live: process is serving HTTP.
- /health/ready: DB reachable and Alembic at expected head.
- /api/v1/system/version: app/schema/agent protocol versions.
- /metrics: authenticated Prometheus text for active runtimes and job counts.

Dead Incus Nodes do not fail central readiness.

## Command handling

SETUP/START/STOP/RESTART/REBUILD/VERIFY respond 202 with job ID. Inspect /api/v1/jobs/{id}; do not assume request acceptance means completion. Jobs have bounded attempts and exponential retry. SETUP is the only unverified-start path; it reserves a normal slot for manual login and must be followed by a fresh agent readiness report and VERIFY.

An Idempotency-Key header deduplicates client retries without changing a later desired state. Independently, a partial unique index allows at most one active command of the same kind per runtime, and the executor permits only one leased/running lifecycle command per Account.

## Node actions

- DRAIN: no new starts; running instances continue.
- MAINTENANCE: requires no active runtime.
- EXIT MAINTENANCE: resumes ONLINE only when heartbeat/provider conditions permit.
- DISABLE: requires no active runtime.

## Stale conditions

Watchdog marks and emits diagnostics. It never destroys/rebuilds automatically. Investigate Node heartbeat, RuntimeAgent heartbeat, Steam/DST phase and provider state. Use bounded manual retry only after identifying cause.

## Remote view

VIEW is disabled by default. The mock provider proves token/session plumbing only.
The xpra adapter validates a short-lived capability behind the application's TLS
reverse proxy and never exposes a permanent URL; it still requires Linux validation.

## Audit/logs

Admin lifecycle actions are append-only through application APIs in audit_events. Events are operator-facing. Structured process logs carry request_id/job/entity IDs and redact sensitive key names.

## Worker operations

Worker mode and lifecycle are independent from runtime lifecycle. Use DISABLED during
setup, OBSERVE for recognition validation, and ACTIVE only for a short controlled run.
PAUSE and STOP release input; STOP does not stop DST. RESUME is always explicit. A
worker ERROR/NEEDS_ATTENTION does not authorize an Incus or DST restart.

Heartbeat shows plugin/version/config version, state/mode, last tick/action/observation,
error code, and restart count. Inspect bounded diagnostics only in account details and
the runtime diagnostic directory; do not enable continuous screenshots.

## Remote view operations

Configure `RUNTIME_VIEW_PROVIDER=xpra` only on the future Linux control-plane/Incus
host. VIEW_ONLY does not pause automation. CONTROL VIEW is interactive and waits for
PAUSED. Close sessions explicitly; expiry cleanup is a safety net. If the backend is
missing, treat `REMOTE_VIEW_BACKEND_UNAVAILABLE` as expected until real-node setup,
not as a passing mock result.
