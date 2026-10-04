#!/usr/bin/env bash
# Run the Veyra API on http://127.0.0.1:8787
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x .venv/bin/uvicorn ]; then
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
exec uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
