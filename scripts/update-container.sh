#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -d data ]]; then
  if docker container inspect sirius-pulse-v2-test >/dev/null 2>&1; then
    echo "检测到旧容器但缺少 ./data，拒绝更新以保护持久化数据。请先按部署指南完成迁移。" >&2
    exit 2
  fi
  install -d -m 700 -o 10001 -g 10001 data
fi

git pull --ff-only origin master
git submodule update --init --recursive
docker compose config -q
export SIRIUS_ENV_CACHE_KEY="$(sha256sum Dockerfile | awk '{print $1}')"
unset SIRIUS_ENV_CACHE_IMAGE
if docker image inspect sirius-pulse:latest >/dev/null 2>&1; then
  current_environment_key="$(docker image inspect --format '{{ index .Config.Labels \"org.sirius-pulse.environment-cache-key\" }}' sirius-pulse:latest)"
  current_lock_hash="$(docker run --rm --entrypoint sha256sum sirius-pulse:latest /app/uv.lock 2>/dev/null | awk '{print $1}' || true)"
  if [[ ( -z "$current_environment_key" || "$current_environment_key" == "<no value>" || "$current_environment_key" == "$SIRIUS_ENV_CACHE_KEY" ) \
    && "$(sha256sum uv.lock | awk '{print $1}')" == "$current_lock_hash" ]]; then
    export SIRIUS_ENV_CACHE_IMAGE=sirius-pulse:latest
  fi
fi
docker compose up -d --build --force-recreate --remove-orphans

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
