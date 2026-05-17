#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
exec .venv/bin/uvicorn chores.main:app --host 127.0.0.1 --port 8080
