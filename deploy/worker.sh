#!/usr/bin/env bash
# Run the Celery worker that retries a failed dial.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x .venv/bin/celery ]; then
  echo "Run bash deploy/setup.sh first."
  exit 1
fi
if [ ! -f .env ]; then
  echo "backend/.env is missing. Run bash deploy/setup.sh first."
  exit 1
fi

# shellcheck disable=SC1091
if [ "${VIRTUAL_ENV:-}" != "$ROOT/.venv" ]; then
  if [ -f .venv/scripts/activate ]; then
    source .venv/scripts/activate
  else
    source .venv/bin/activate
  fi
fi
exec celery -A worker.celery_app:celery_app worker --beat --loglevel=INFO
