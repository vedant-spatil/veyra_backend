#!/usr/bin/env bash
# Start the official prebuilt Dograh stack on this machine.
# Postgres and Redis stay on the compose network. The API listens on 127.0.0.1:8000.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
STACK="$REPO/local-dograh"
BACKEND_ENV="$REPO/backend/.env"
COMPOSE_URL="https://raw.githubusercontent.com/dograh-hq/dograh/main/docker-compose.yaml"

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

mkdir -p "$STACK"
cd "$STACK"

echo "Downloading the official Dograh compose file."
curl -fsSL -o docker-compose.yaml "$COMPOSE_URL"

cat > docker-compose.override.yaml <<'EOF'
# Veyra already publishes host ports 5432 and 6379.
# The official compose file also claims the container name "minio".
services:
  postgres:
    ports: !reset []
  redis:
    ports: !reset []
  minio:
    container_name: dograh-minio
  cloudflared:
    container_name: dograh-cloudflared
  api:
    ports: !override
      - "127.0.0.1:8000:8000"
EOF

generate_secret() {
  python3 -c 'import secrets; print(secrets.token_hex(32))'
}

dotenv_value() {
  local key=$1 line
  [ -f .env ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*)
        printf '%s\n' "${line#*=}"
        return 0
        ;;
    esac
  done < .env
  return 1
}

set_dotenv_value() {
  local key=$1 value=$2 tmp line updated=false
  tmp=".env.tmp.$$"
  if [ -f .env ]; then
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
    done < .env > "$tmp"
    if [ "$updated" != "true" ]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$value" > .env
  fi
}

ensure_secret() {
  local key=$1 current
  current="$(dotenv_value "$key" || true)"
  if [ -z "$current" ]; then
    set_dotenv_value "$key" "$(generate_secret)"
  fi
}

copy_from_root() {
  local key=$1 value="${2:-}"
  if [ -n "$value" ]; then
    set_dotenv_value "$key" "$value"
  fi
}

ensure_secret OSS_JWT_SECRET
ensure_secret POSTGRES_PASSWORD
ensure_secret REDIS_PASSWORD
ensure_secret MINIO_ROOT_PASSWORD
if [ -z "$(dotenv_value MINIO_ROOT_USER || true)" ]; then
  set_dotenv_value MINIO_ROOT_USER "dograh$(generate_secret | cut -c1-12)"
fi

set_dotenv_value DEPLOY_MODE prebuilt
set_dotenv_value FASTAPI_WORKERS 1
set_dotenv_value ENABLE_TELEMETRY "${ENABLE_TELEMETRY:-true}"
set_dotenv_value REGISTRY "${REGISTRY:-ghcr.io/dograh-hq}"

copy_from_root DOGRAH_EMAIL "${DOGRAH_EMAIL:-}"
copy_from_root DOGRAH_PASSWORD "${DOGRAH_PASSWORD:-}"
copy_from_root DOGRAH_API_KEY "${DOGRAH_API_KEY:-}"
copy_from_root DEEPGRAM_API_KEY "${DEEPGRAM_API_KEY:-}"
copy_from_root GROQ_API_KEY "${GROQ_API_KEY:-}"
copy_from_root GROQ_MODEL "${GROQ_MODEL:-}"
copy_from_root VOBIZ_AUTH_ID "${VOBIZ_AUTH_ID:-}"
copy_from_root VOBIZ_AUTH_TOKEN "${VOBIZ_AUTH_TOKEN:-}"
copy_from_root VOBIZ_NUMBER "${VOBIZ_NUMBER:-}"

if [ -f "$BACKEND_ENV" ]; then
  ENV_FILE="$BACKEND_ENV"
  set_dotenv_value() {
    local key=$1 value=$2 tmp line updated=false
    tmp="${ENV_FILE}.tmp.$$"
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
    done < "$ENV_FILE" > "$tmp"
    if [ "$updated" != "true" ]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
  }
  set_dotenv_value DOGRAH_BASE_URL "http://127.0.0.1:8000"
  set_dotenv_value DOGRAH_API_KEY "${DOGRAH_API_KEY:-}"
fi

echo "Starting Dograh. API http://127.0.0.1:8000  UI http://127.0.0.1:3010"
REGISTRY="${REGISTRY:-ghcr.io/dograh-hq}" ENABLE_TELEMETRY="${ENABLE_TELEMETRY:-true}" \
  docker compose --profile tunnel up -d --pull always

if [ -z "${DOGRAH_API_KEY:-}" ]; then
  echo "DOGRAH_API_KEY is empty in the repo root .env. Create an org key in the Dograh UI, then put it in .env and backend/.env."
fi
echo "Restart bash deploy/run.sh so the API reloads DOGRAH_BASE_URL."
