# Автопилот рекламы
Монорепозиторий сервиса автоматизации рекламы для малого бизнеса.
Архитектура и задачи: [план проекта](docs/internal/PLAN.md).

Локальный запуск с Docker Compose:
```sh
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d
```
Перед запуском заполните `.env` по комментариям; проверка API: `https://localhost/api/health`.
