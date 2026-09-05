#!/usr/bin/env bash
set -euo pipefail
uvicorn_args=(app.main:app --host 127.0.0.1 --port 8080 --reload)
if [[ -f .env ]]; then uvicorn_args+=(--env-file .env); fi
exec python -m uvicorn "${uvicorn_args[@]}"
