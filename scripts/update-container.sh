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
