# Runtime Agent compatibility entry point

agent/agent.py сохранён для старых launch scripts, но делегирует модульному runtime_agent.

Новый production entry point:

~~~bash
python -m runtime_agent.main
~~~

Runtime agent отвечает только за graphical session health, Steam/DST process supervision, explicit readiness markers, bounded restart/backoff, authenticated heartbeat и NoopGameWorker bridge.

Popen не считается readiness. Steam и DST становятся ready только при живом процессе и соответствующем launcher-created marker. WORKER_ACTIVE автоматически не выставляется.

Configuration: deploy/env/runtime-agent.env.example. Setup: RUNTIME_SETUP.md.

