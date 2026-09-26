# Production deployment

## Control plane

Provision a dedicated Linux user, Python virtualenv, PostgreSQL database and /etc/dst-orchestrator/control-plane.env with mode 0600. Copy deploy/env/control-plane.env.example and replace every placeholder.

Required properties:

- ENVIRONMENT=production;
- PostgreSQL DATABASE_URL using psycopg;
- RUNTIME_PROVIDER=incus;
- CURRENT_IMAGE_VERIFIED=true only after the named Incus image passes real-node validation;
- externally generated Fernet key;
- long random admin password;
- HTTPS ORCHESTRATOR_PUBLIC_URL;
- DEBUG=false.

Install and verify:

~~~bash
python3 -m venv /opt/dst-orchestrator/.venv
/opt/dst-orchestrator/.venv/bin/pip install -r requirements.txt
/opt/dst-orchestrator/.venv/bin/alembic upgrade head
sudo install -m 0644 deploy/systemd/dst-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dst-orchestrator
curl --fail http://127.0.0.1:8080/health/ready
~~~

Reference unit uses one uvicorn process because background components are embedded.
PostgreSQL advisory/row locks and scheduler leadership protect lifecycle work, but the
production topology intentionally remains one control-plane process until a
multi-process deployment is validated end to end.

If `RUNTIME_XAUTHORITY` is configured, it must be an absolute path inside the runtime.
Bootstrap passes it to every graphical component and the generated default Xvfb
command uses the same file with `-auth`; provisioning that file/cookie remains an
image responsibility.

## TLS reverse proxy

Replace orchestrator.example.com and certificate paths in deploy/nginx/dst-orchestrator.conf. Validate with nginx -t before reload. Public port 8080 must remain firewalled; only nginx binds Internet-facing 443.

Agent routes should be private-network-only. If agents cross untrusted networks, introduce mTLS instead of expanding allow lists.

## Rollout

1. Back up DB/key/config.
2. Stop scheduler/executor service for schema changes requiring coordination.
3. Run alembic upgrade head.
4. Start service and check ready/version.
5. Canary one new image/runtime; do not mutate all persistent environments in place.

Rollback code only after checking migration compatibility. Database rollback is alembic downgrade -1; take a backup first.
