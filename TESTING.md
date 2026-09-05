# Testing

Install pinned-range dependencies from requirements.txt, then:

~~~bash
pytest -q
ruff check .
python -m compileall -q app node_agent runtime_agent
~~~

The default suite covers state transitions, accounts/secrets, admin/CSRF, agent token isolation, protocol mismatch, durable jobs/retry/expiry, slot concurrency, lifecycle/rebuild, Node drain/maintenance/resources, watchdog, VIEW boundary and mock failure injection.

Migration round trip:

~~~bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
~~~

PostgreSQL must be tested separately with TEST_DATABASE_URL or a disposable CI service. The suite creates and drops a random `dst_test_*` schema; it never downgrades the supplied database. SQLite green tests do not validate PostgreSQL locking behavior.

Real Incus is opt-in and destructive only to its uniquely named test instance:

~~~bash
RUN_INCUS_TESTS=1 INCUS_TEST_IMAGE=images:alpine/3.20 pytest -q tests/providers/test_incus_real.py
~~~

Steam/DST are intentionally absent from CI. Run NODE_SETUP and RUNTIME_SETUP preflight/manual chain on the actual GPU host before production acceptance.
