# Roadmap

## Current code boundary

Runtime bootstrap, versioned verified-image gating, provider exec/file injection,
separate Display/Steam/DST contracts, bounded restarts, and DB scheduler
leadership are implemented in code. Real Linux/Incus/Steam/DST validation is the
next wave; this repository does not claim production readiness.

## Implemented foundation

- SQLAlchemy/PostgreSQL/Alembic;
- durable jobs, attempts, leases and slot invariant;
- auth/CSRF/secrets/agent tokens;
- Node/runtime lifecycle, heartbeat, resources and watchdog;
- versioned runtime rebuild;
- audit/events/structured logs/metrics;
- systemd/nginx/preflight/backup;
- simple operations dashboard;
- adversarial mock tests.

## Required before production acceptance

1. Run the integration suite against an actual PostgreSQL service.
2. Validate Ubuntu + Incus storage/network/GPU on the target NODE.
3. Build and canary dst-base-v1 without reusable account credentials.
4. Validate graphical session, Steam, DST readiness markers and persistent manual login.
5. Exercise STOP/START/reboot and session persistence.
6. Validate the implemented authenticated xpra RuntimeViewProvider on one Linux Node; keep VIEW disabled until then.
7. Configure off-host encrypted backups and perform a restore drill.
8. Put production secrets in an OS secret facility or secrets manager.

## Later, based on measured need

- Node-local pull command execution and mTLS;
- Prometheus retention/Grafana alerts;
- calibrated RuntimeResourceProfile placement;
- assisted MOVE_RUNTIME export/restore workflow;
- PostgreSQL HA and multiple control-plane replicas;
- real GameWorker as an isolated plugin.

No Kubernetes, Kafka, Redis cluster, fingerprint spoofing, enforcement bypass or automatic account registration is planned in this control-plane repository.
