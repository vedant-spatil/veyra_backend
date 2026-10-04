#!/usr/bin/env bash
# Start Dograh on Veyra's Postgres and Redis.
# Dograh tables live in schema dograh inside database veyra. Redis uses database 1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
ENV_FILE="$ROOT/.dograh.env"
BACKEND_ENV="$ROOT/.env"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required."
  exit 1
fi
if [ ! -f "$REPO/.env" ]; then
  echo "Repo root .env is missing."
  exit 1
fi

# shellcheck disable=SC1091
set -a
. "$REPO/.env"
set +a

generate_secret() {
  python3 -c 'import secrets; print(secrets.token_hex(32))'
}

dotenv_value() {
  local file=$1 key=$2 line
  [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*)
        printf '%s\n' "${line#*=}"
        return 0
        ;;
    esac
  done < "$file"
  return 1
}

set_dotenv_value() {
  local file=$1 key=$2 value=$3 tmp line updated=false
  tmp="${file}.tmp.$$"
  if [ -f "$file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "$key"=*)
          printf '%s=%s\n' "$key" "$value"
          updated=true
          ;;
        *)
          printf '%s\n' "$line"
          ;;
      esac
    done < "$file" > "$tmp"
    if [ "$updated" != "true" ]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" > "$file"
  fi
}

ensure_secret() {
  local key=$1 current
  current="$(dotenv_value "$ENV_FILE" "$key" || true)"
  if [ -z "$current" ]; then
    set_dotenv_value "$ENV_FILE" "$key" "$(generate_secret)"
  fi
}

copy_if_set() {
  local key=$1 value="${2:-}"
  if [ -n "$value" ]; then
    set_dotenv_value "$ENV_FILE" "$key" "$value"
  fi
}

touch "$ENV_FILE"
ensure_secret OSS_JWT_SECRET
if [ -z "$(dotenv_value "$ENV_FILE" MINIO_ROOT_USER || true)" ]; then
  set_dotenv_value "$ENV_FILE" MINIO_ROOT_USER "dograh$(generate_secret | cut -c1-12)"
fi
ensure_secret MINIO_ROOT_PASSWORD
set_dotenv_value "$ENV_FILE" FASTAPI_WORKERS 1
set_dotenv_value "$ENV_FILE" ENABLE_TELEMETRY false
set_dotenv_value "$ENV_FILE" REGISTRY "${REGISTRY:-ghcr.io/dograh-hq}"

copy_if_set DOGRAH_EMAIL "${DOGRAH_EMAIL:-}"
copy_if_set DOGRAH_PASSWORD "${DOGRAH_PASSWORD:-}"
copy_if_set DOGRAH_API_KEY "${DOGRAH_API_KEY:-}"
copy_if_set DEEPGRAM_API_KEY "${DEEPGRAM_API_KEY:-}"
copy_if_set GROQ_API_KEY "${GROQ_API_KEY:-}"
copy_if_set GROQ_MODEL "${GROQ_MODEL:-}"
copy_if_set VOBIZ_AUTH_ID "${VOBIZ_AUTH_ID:-}"
copy_if_set VOBIZ_AUTH_TOKEN "${VOBIZ_AUTH_TOKEN:-}"
copy_if_set VOBIZ_NUMBER "${VOBIZ_NUMBER:-}"

if [ -f "$BACKEND_ENV" ]; then
  set_dotenv_value "$BACKEND_ENV" DOGRAH_BASE_URL "http://127.0.0.1:8000"
  set_dotenv_value "$BACKEND_ENV" DOGRAH_API_KEY "${DOGRAH_API_KEY:-}"
  if [ -n "${VOBIZ_AUTH_ID:-}" ]; then
    set_dotenv_value "$BACKEND_ENV" VOBIZ_AUTH_ID "${VOBIZ_AUTH_ID}"
  fi
  if [ -n "${VOBIZ_AUTH_TOKEN:-}" ]; then
    set_dotenv_value "$BACKEND_ENV" VOBIZ_AUTH_TOKEN "${VOBIZ_AUTH_TOKEN}"
  fi
fi

echo "Stopping the extra local-dograh Postgres and Redis."
docker rm -f local-dograh-postgres-1 local-dograh-redis-1 local-dograh-api-1 local-dograh-ui-1 dograh-minio dograh-cloudflared >/dev/null 2>&1 || true

cd "$ROOT"
# shellcheck disable=SC1091
set -a
. "$ENV_FILE"
set +a

echo "Starting the shared Postgres and Redis."
REGISTRY="${REGISTRY:-ghcr.io/dograh-hq}" docker compose up -d veyra-postgres veyra-redis
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if docker compose exec -T veyra-postgres pg_isready -U veyra -d veyra >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T veyra-postgres psql -U veyra -d veyra -v ON_ERROR_STOP=1 < "$ROOT/deploy/dograh-veyra.sql"
if [ "$(docker compose exec -T veyra-postgres psql -U veyra -d veyra -tAc "SELECT 1 FROM pg_database WHERE datname = 'dograh'" | tr -d '[:space:]')" != "1" ]; then
  docker compose exec -T veyra-postgres psql -U veyra -d veyra -v ON_ERROR_STOP=1 -c "CREATE DATABASE dograh OWNER dograh_app"
fi
docker compose exec -T veyra-postgres psql -U veyra -d dograh -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector"
docker compose exec -T veyra-postgres psql -U veyra -d dograh -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO dograh_app"

echo "Starting Dograh. API http://127.0.0.1:8000  UI http://127.0.0.1:3010"
REGISTRY="${REGISTRY:-ghcr.io/dograh-hq}" docker compose --profile tunnel up -d

if [ -z "${DOGRAH_API_KEY:-}" ]; then
  echo "DOGRAH_API_KEY is empty in the repo root .env. Create an org key in the Dograh UI, then put it in .env and backend/.env."
fi
echo "Dograh webhook URL: http://host.docker.internal:8787/api/billing/hooks/dograh"
echo "Restart bash deploy/run.sh so the API reloads DOGRAH_BASE_URL."
