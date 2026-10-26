#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.demo ]; then
    echo 'Создайте .env.demo по .env.demo.example и задайте локальные секреты (см. README, раздел «Демо»).' >&2
    exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
    echo 'Сначала соберите фронтенд: cd frontend && npm run build' >&2
    exit 1
fi
docker compose --env-file .env.demo -f deploy/docker-compose.yml up -d "$@"
