# DST Runtime Orchestrator

Control plane для управления persistent Steam/DST runtime на Linux + Incus. Архитектурная граница проекта:

~~~text
Account -> active RuntimeInstance generation -> Node -> RuntimeProvider -> Incus
~~~

Account — бизнес-сущность и переживает rebuild. RuntimeInstance — versioned инфраструктурная сущность. DSTGameWorker и fallback NoopGameWorker используют единый generation-bound subprocess lifecycle; gameplay безопасно отключён по умолчанию. Linux/input/vision validation ещё не выполнена.

## Реализовано

- SQLAlchemy 2, PostgreSQL production и SQLite test/development;
- Alembic migrations и health/live, health/ready;
- централизованные Account/Runtime/Node state machines;
- durable job queue с bounded retry, lease expiry и FOR UPDATE SKIP LOCKED на PostgreSQL;
- durable slot leases и атомарный hard cap Node;
- async command API: mutations возвращают 202 + job;
- Argon2id admin login, HttpOnly/SameSite session cookie, CSRF и login rate limit;
- Fernet secrets с production fail-fast;
- hash-only Node/runtime bearer credentials и protocol versioning;
- Node agent с CPU/RAM/disk/GPU/Incus heartbeat;
- runtime agent с graphical health, readiness markers и bounded process restarts;
- rebuild в generation + 1 без удаления Account и без переноса login session;
- drain/maintenance, watchdog, reconciler, scheduler и structured events/audit;
- short-lived remote VIEW provider boundary; публичный постоянный VNC URL отсутствует;
- authenticated dashboard для accounts/nodes/jobs/events;
- systemd/nginx, preflight и backup/restore foundation.

## Development

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
.\scripts\run_dev.ps1
~~~

Development defaults используют mock provider, SQLite и admin password 123. Они намеренно запрещены Settings.validate() в production.

Открыть http://127.0.0.1:8080, войти как admin / 123. Создание Account ставит PROVISION_RUNTIME в очередь. После provisioning lifecycle имеет явную ручную последовательность: `SETUP` запускает контейнер в рамках обычного slot lease, оператор завершает вход и проверяет Steam/DST, runtime agent отправляет свежий authenticated `GAME_READY` heartbeat, затем `VERIFY` фиксирует готовность. `START` до VERIFY отклоняется. После VERIFY runtime остаётся RUNNING до STOP; это намеренно не интерпретируется как готовность только по состоянию Incus.

## Production

Production требует PostgreSQL, Incus provider, external Fernet key, сильный admin password, DEBUG=false и HTTPS public URL. Пример environment: [control-plane.env.example](deploy/env/control-plane.env.example).

~~~bash
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
~~~

Nginx завершает TLS. Node/runtime agent endpoints следует держать в private network. Подробности: DEPLOYMENT.md, NODE_SETUP.md, RUNTIME_SETUP.md, OPERATIONS.md.

## API

Основной prefix: /api/v1.

- POST /auth/login, GET /auth/session, POST /auth/logout;
- /accounts, /accounts/{id}/{setup|start|stop|restart|verify|rebuild}, /accounts/{id}/runs;
- /jobs, /nodes, /events;
- POST /node-agent/heartbeat, POST /runtime-agent/heartbeat;
- /system/version;
- short-lived /runtimes/{id}/view-sessions.

Admin mutations требуют session cookie + X-CSRF-Token. Heartbeats требуют Authorization: Bearer <per-entity-token>.

## Проверка

~~~bash
pytest -q
ruff check .
alembic upgrade head
alembic downgrade -1
alembic upgrade head
~~~

Real Incus lifecycle tests opt-in: RUN_INCUS_TESTS=1 pytest -q tests/providers/test_incus_real.py.

Green mock/SQLite suite не доказывает работу физической цепочки Ubuntu → Incus → graphical session → Steam → DST → agent → STOP/START persistence. Используйте preflight и real-node runbook.
