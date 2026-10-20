#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
command -v stat >/dev/null || { echo "GNU stat is required" >&2; exit 1; }
test -S /var/run/docker.sock || { echo "/var/run/docker.sock is required (Linux Docker)" >&2; exit 1; }

docker network inspect simplecode-stack >/dev/null 2>&1 || docker network create simplecode-stack >/dev/null
docker network inspect simplecode-sso >/dev/null 2>&1 || docker network create simplecode-sso >/dev/null

socket_gid="$(stat -c '%g' /var/run/docker.sock)"
if [ ! -f .env ]; then
  cp .env.example .env
  db_secret="$(openssl rand -hex 24)"
  oidc_secret="$(openssl rand -hex 24)"
  sed -i "s/^CODER_DB_PASSWORD=.*/CODER_DB_PASSWORD=${db_secret}/" .env
  sed -i "s/^CODER_OIDC_CLIENT_SECRET=.*/CODER_OIDC_CLIENT_SECRET=${oidc_secret}/" .env
  echo "wrote docker/coder/.env with fresh local secrets"
fi

if grep -q '^CODER_DOCKER_GID=' .env; then
  sed -i "s/^CODER_DOCKER_GID=.*/CODER_DOCKER_GID=${socket_gid}/" .env
else
  printf '\nCODER_DOCKER_GID=%s\n' "$socket_gid" >> .env
fi
