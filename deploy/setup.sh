#!/usr/bin/env bash
# Local setup: Postgres, Redis, Python packages, and the database schema.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for local Postgres and Redis."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  python3 - <<'PY'
from pathlib import Path
import secrets
path = Path(".env")
text = path.read_text()
text = text.replace(
    "SECRET_KEY=replace-with-a-long-random-string",
    "SECRET_KEY=" + secrets.token_urlsafe(32),
    1,
)
path.write_text(text)
PY
  echo "Created backend/.env. Set ALLOWED_EMAILS, GOOGLE_CLIENT_ID, and GOOGLE_CLIENT_SECRET before signing in."
fi

echo "Starting Postgres and Redis."
docker compose up -d

echo "Waiting for Postgres."
ready=0
for _ in $(seq 1 30); do
  if docker compose exec -T veyra-postgres pg_isready -U veyra -d veyra >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" != "1" ]; then
  echo "Postgres did not become ready."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
if [ "${VIRTUAL_ENV:-}" != "$ROOT/.venv" ]; then
  if [ -f .venv/scripts/activate ]; then
    source .venv/scripts/activate
  else
    source .venv/bin/activate
  fi
fi
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Applying database schema."
alembic upgrade head

echo "Backend is set up."
echo "API:    bash deploy/run.sh"
echo "Worker: bash deploy/worker.sh"
