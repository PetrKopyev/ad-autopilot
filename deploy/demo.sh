#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.demo ]; then
    echo 'Создайте .env.demo по .env.demo.example и задайте локальные секреты (см. README, раздел «Демо»).' >&2
    exit 1
fi
domain=${APP_DOMAIN:-$(sed -n 's/^APP_DOMAIN=//p' .env.demo | tail -n 1 | tr -d "\"' \r")}
if [ "$domain" != localhost ]; then
    echo "Демо только локально: APP_DOMAIN должен быть localhost, сейчас «$domain»." >&2
    exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
    echo 'Сначала соберите фронтенд: cd frontend && npm run build' >&2
    exit 1
fi
dc() { docker compose -p autopilot-demo --env-file .env.demo -f deploy/docker-compose.yml "$@"; }
subnet=${APP_SUBNET:-$(sed -n 's/^APP_SUBNET=//p' .env.demo | tail -n 1 | tr -d "\"' \r")}
current=$(docker network inspect autopilot-demo_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null || true)
if [ -n "$current" ] && [ "$current" != "${subnet:-172.31.250.0/24}" ]; then
    # Сеть старого стенда с другой подсетью compose сам не пересоздаёт: убираем контейнеры и сеть, тома остаются.
    dc down
fi
# Старые api и worker не должны писать в базу, пока migrate меняет схему (DEPLOY.md, раздел 9).
dc stop api worker
dc up -d "$@"
