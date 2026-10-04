#!/usr/bin/env bash
# Prepare a NEW 4 GB instance. Refuses the old 2 GB box.
# This script does not allocate an Elastic IP. Attach that in AWS first, then set VEYRA_HOST.
set -euo pipefail

OLD_BOX="43.204.103.141"
HOST="${VEYRA_HOST:-}"
KEY="${VEYRA_SSH_KEY:-$HOME/.ssh/voice.pem}"

if [ -z "$HOST" ]; then
  echo "Set VEYRA_HOST to the new instance Elastic IP."
  echo "The old box at ${OLD_BOX} is not a valid target."
  echo "Attach the Elastic IP before running this. The address is billed while it is allocated."
  exit 2
fi

if [ "$HOST" = "$OLD_BOX" ]; then
  echo "Refusing to change the old 2 GB box at ${OLD_BOX}."
  exit 1
fi

echo "Target ${HOST}. Old box ${OLD_BOX} will not be contacted."
echo "1. Install Dograh on this host only. Recordings stay in its MinIO."
echo "2. Issue the certificate for the Elastic IP sslip.io name."
echo "3. Start veyra-postgres, veyra-redis, uvicorn on 127.0.0.1:8787, and one Celery worker."
echo "4. Point nginx /api at uvicorn. Leave Dograh's inbound server block in place."
echo "5. Register the Google redirect URI on that HTTPS host."
echo "6. Move the Vobiz answer URL only after a signed-in POST /api/calls returns a Dograh run id."

if [ "${1:-}" != "--apply" ]; then
  echo "Dry run. Pass --apply to SSH to ${HOST} and install the console containers."
  exit 0
fi

ssh -o ConnectTimeout=15 -i "$KEY" "root@${HOST}" "bash -s" <<'REMOTE'
set -euo pipefail
if [ "$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 || true)" = "43.204.103.141" ]; then
  echo "This machine is the old box. Stopping."
  exit 1
fi
docker volume create veyra-pg >/dev/null
docker rm -f veyra-postgres veyra-redis >/dev/null 2>&1 || true
docker run -d --name veyra-postgres --restart always \
  -e POSTGRES_USER=veyra -e POSTGRES_PASSWORD=veyra -e POSTGRES_DB=veyra \
  -v veyra-pg:/var/lib/postgresql/data -p 127.0.0.1:5432:5432 postgres:16
docker run -d --name veyra-redis --restart always -p 127.0.0.1:6379:6379 redis:7
echo "Postgres and Redis are on localhost. Copy the Veyra checkout to /opt/veyra and run alembic upgrade head, uvicorn, and celery there."
REMOTE
