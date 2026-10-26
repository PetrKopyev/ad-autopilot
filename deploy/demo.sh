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
docker compose -p autopilot-demo --env-file .env.demo -f deploy/docker-compose.yml up -d "$@"
