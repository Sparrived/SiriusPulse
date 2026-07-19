#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
git pull --ff-only origin master
git submodule update --init --recursive
docker compose up -d --build --force-recreate

for _ in {1..60}; do
  if curl -fsS http://127.0.0.1:8080/ >/dev/null \
    && curl -fsS http://127.0.0.1:18900/health >/dev/null; then
    docker compose ps
    exit 0
  fi
  sleep 2
done

docker compose ps
docker compose logs --tail=100
exit 1
