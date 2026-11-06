# Развёртывание

> **Начните с заявки на доступ к API Директа.** Пока Яндекс её не одобрит (от часа до трёх рабочих суток, в пик до семи дней, [SETUP-YANDEX.md](SETUP-YANDEX.md) §2), сервис не создаст кампании и не прочитает статистику, а вместо прогноза на экране «Ваш план» будет написано «Прогноз появится, когда Яндекс одобрит доступ». Подайте заявку в первый день, до поднятия сервера.

Сервис — пять контейнеров Docker Compose: `db` (PostgreSQL 16), `migrate` (разово применяет миграции), `api`, `worker`, `caddy` (HTTPS и статика интерфейса). Схема: [ARCHITECTURE.md](ARCHITECTURE.md). Демо без ключей и домена — в [README.md](../README.md).

## 1. Сервер

- VPS с Ubuntu 22.04 или 24.04, 2 ГБ памяти, 2 ГБ swap, 20 ГБ диска. Swap нужен и для сборки образа; включите его до первой сборки:

```sh
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

- Сервер должен быть в России: первичная запись и хранение персональных данных граждан РФ требуют этого (152-ФЗ, [PRIVACY.md](PRIVACY.md)).
- DNS: A-запись `<домен>` на IP сервера. Caddy сам получает сертификат Let's Encrypt, нужны открытые порты 80 и 443. AAAA-запись (IPv6) не заводите: сеть Compose работает по IPv4, и все IPv6-посетители делили бы одно ограничение частоты запросов.

```sh
sudo ufw allow 22/tcp    # только если подключаетесь по SSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

- Docker и Compose:

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # перелогиньтесь
sudo apt-get install -y git curl
```

- Node.js 20+ нужен только для сборки интерфейса ([NodeSource](https://github.com/nodesource/distributions) или `nvm`).

## 2. Код и интерфейс

```sh
git clone <адрес репозитория> ad-autopilot
cd ad-autopilot
(cd frontend && npm ci && npm run build)
```

Caddy раздаёт сборку из `frontend/dist`; без этой команды сайт откроется пустым.

## 3. Настройки (`.env`)

```sh
cp .env.example .env
chmod 600 .env
```

Не коммитьте `.env`. Запускайте Compose всегда с `--env-file .env` (из него подставляются `POSTGRES_*` и `APP_DOMAIN`). Секреты:

```sh
openssl rand -hex 24      # POSTGRES_PASSWORD (hex: пароль входит в DATABASE_URL)
openssl rand -hex 32      # SECRET_KEY
docker run --rm python:3.12-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"   # TOKEN_ENCRYPTION_KEY
```

| Переменная | Что это и где взять | Обязательна |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_DB` | Роль и база PostgreSQL, по умолчанию `autopilot` | да |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL | да |
| `DATABASE_URL` | Адрес базы; в `.env.example` собран из `POSTGRES_*` и хоста `db`, менять не нужно | да |
| `APP_BASE_URL` | Публичный адрес `https://<домен>` без слэша на конце; из него строятся redirect URI Яндекса и VK | да |
| `APP_DOMAIN` | Домен для Caddy, тот же, без `https://` (локально `localhost`) | да |
| `BIND_ADDR` | Адрес, на котором Caddy слушает 80 и 443; по умолчанию `0.0.0.0`. В `.env.demo` — `127.0.0.1` | нет |
| `CADDY_IP`, `APP_SUBNET` | Адрес Caddy и подсеть Compose, по умолчанию `172.31.250.10` и `172.31.250.0/24`. `api` доверяет `X-Forwarded-For` только от этого адреса. Менять только вместе, если подсеть занята | нет |
| `SECRET_KEY` | Подпись служебных значений, не короче 32 символов; заглушка из `.env.example` отклоняется. От него зависит ключ кампаний `ap-<хеш>-<id>`: смена на живом кабинете создаст дубли кампаний, сначала заархивируйте старые `ap-*`. При пустом `AI_OWNER_KEY_SECRET` его смена обнуляет пулы трат на ИИ | да |
| `TOKEN_ENCRYPTION_KEY` | Ключ Fernet для токенов кабинетов, ИНН и телефона. Потеряете — кабинеты придётся подключать заново; храните копию отдельно от копий базы | да |
| `PLATFORM_MODE` | `live` (по умолчанию) или `fake` (симулятор для демо) | нет |
| `DRY_RUN` | `false` (по умолчанию): автопилот применяет решения. `true` — режим наблюдения: решения автопилота (паузы, перенос бюджета, A/B, исправление отклонённых объявлений) только записываются как «предложено». Запуск из мастера, пересчёт недельных лимитов и продление кампании Директа, страж бюджета, действия владельца и откат применяются всегда | нет |
| `YANDEX_CLIENT_ID` | ID приложения на oauth.yandex.ru ([SETUP-YANDEX.md](SETUP-YANDEX.md)); без него войти нельзя | да |
| `YANDEX_CLIENT_SECRET` | Пароль того же приложения | рекомендуется |
| `DIRECT_API_URL`, `DIRECT_API_V501_URL` | Адреса API Директа, значения по умолчанию верны | нет |
| `DIRECT_SANDBOX` | `true`: обращения идут на `api-sandbox.direct.yandex.com`. По умолчанию `false` | нет |
| `DIRECT_AMOUNTS_INCLUDE_VAT` | `false` (по умолчанию): суммы стратегии Директа без НДС, лимит уходит как `лимит ÷ (1 + VAT_PERCENT/100)`. `true` — суммы API уже с НДС ([LIMITATIONS.md](LIMITATIONS.md)) | нет |
| `VAT_PERCENT` | Ставка НДС, по умолчанию 22 | нет |
| `VK_AMOUNTS_INCLUDE_VAT` | `false` (по умолчанию): лимиты VK уходят как есть. `true` — ставка НДС сверх лимита VK, лимит уходит как `лимит ÷ (1 + VAT_PERCENT/100)` | нет |
| `VK_CLIENT_ID`, `VK_CLIENT_SECRET` | Приложение VK Рекламы ([SETUP-VK.md](SETUP-VK.md)): вход через VK и обновление токенов. Без них VK подключается ключом API | нет |
| `VK_OAUTH_ENABLED` | `true` показывает «Войти через VK» (нужен `VK_CLIENT_ID`). По умолчанию `false`: VK включает `authorization_code` не всем приложениям. Закомментирован в `.env.example` | нет |
| `VK_PACKAGE_ID` | ID пакета формата VK для принудительного выбора; по умолчанию сервис выбирает пакет сам. Закомментирован | нет |
| `VK_PRICED_GOAL_SOURCE_ID`, `VK_PRICED_GOAL_NAME` | Запасная цель оптимизации VK на весь сервис; основной путь — «Настройки» → «Цель ВКонтакте» ([SETUP-VK.md](SETUP-VK.md)). Закомментированы | нет |
| `YC_FOLDER_ID`, `YC_API_KEY` | Каталог и API-ключ сервисного аккаунта Yandex Cloud ([SETUP-YANDEX.md](SETUP-YANDEX.md)). Без них (или при `PLATFORM_MODE=fake`) тексты и картинки шаблонные | да (боевой режим) |
| `YANDEXGPT_MODEL` | Модель текстов, по умолчанию `yandexgpt/latest` (учёт трат рассчитан на Pro 5.1, 0,8 ₽ за 1000 токенов) | нет |
| `YANDEXART_MODEL` | Модель картинок, по умолчанию `aliceai-image-art-3.0/latest`; цена в учёте — 2,23 ₽ (`ART_COST_KOP`). Права ключа — [SETUP-YANDEX.md](SETUP-YANDEX.md) | нет |
| `BUDGET_RESERVE_PCT` | Резерв бюджета, по умолчанию 8; учитывается при плане и запуске, перебалансировка и страж считают с 8% | нет |
| `AI_SPEND_ONBOARDING_LIMIT_RUB`, `AI_SPEND_WEEKLY_LIMIT_RUB` | Лимиты трат на нейросеть: подготовка плана (60 ₽) и недельный пул на владельца (25 ₽, по Яндекс ID) | нет |
| `AI_OWNER_KEY_SECRET` | Ключ HMAC псевдонимов владельцев в учёте трат на ИИ. Задайте случайную строку до первого боевого запуска и не меняйте; пусто — берётся из `SECRET_KEY` | нет |
| `TZ_DEFAULT` | Часовой пояс, `Europe/Moscow`; расписание воркера — по Москве | нет |
| `MEDIA_DIR` | Каталог картинок и роликов; в Compose задан `/data/media`, менять не нужно | нет |
| `OPERATOR_REQUISITES` | Реквизиты оператора для страницы «Данные и политика»; пусто — фраза о демонстрационной версии | нет |
| `DEMO_LOGIN` | `1` включает «Войти в демо», только вместе с `PLATFORM_MODE=fake`; на боевом сервере `0` | нет |

Картинки и ролики лежат в томе Compose `media` (`/data/media/<business_id>/`, права создаются сами). Он не входит в копию PostgreSQL из раздела 8: копируйте отдельно (`docker volume ls`, том `<проект>_media`).

### Память контейнеров

Пределы в `deploy/docker-compose.yml` рассчитаны на сервер 2 ГБ + 2 ГБ swap: `db`, `worker`, `migrate` — 512 МБ (640 МБ со swap), `api` — 1,5 ГБ (2 ГБ), `caddy` — 128 МБ. Контейнер, которому не хватило памяти, перезапускается сам (`restart: unless-stopped`), остальные не страдают. На сервере меньшего размера пересчитайте пределы.

## 4. Запуск

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
alias dc='docker compose --env-file .env -f deploy/docker-compose.yml'   # дальше dc = эта команда
```

`migrate` дожидается базы, выполняет `alembic upgrade head` и завершается; `api` и `worker` стартуют после его успеха. Если миграция упала: `dc logs migrate`.

## 5. Проверка

```sh
dc ps                                  # db, api, worker, caddy: running; migrate: exited (0)
curl https://<домен>/api/health        # {"ok":true,"db":true,"mode":"live"}
```

`db: false` — API не достучался до PostgreSQL. Откройте `https://<домен>`, отметьте согласие и нажмите «Войти с Яндекс ID»; при ошибке redirect URI см. [SETUP-YANDEX.md](SETUP-YANDEX.md). Воркер жив, если каждую минуту появляется запись `heartbeat`:

```sh
dc exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select kind, status, started_at from worker_runs order by id desc limit 10"'
```

## 6. Первый запуск: песочница → боевой кабинет

1. **Заявка на API Директа** ([SETUP-YANDEX.md](SETUP-YANDEX.md) §2) от Яндекс ID, который будет владеть кабинетом.
2. **Песочница (необязательно).** `DIRECT_SANDBOX=true` в `.env`, затем `dc up -d api worker`. Отдельной заявки не нужно. Если песочница не отвечает (03.11 отдавала 504), пропустите шаг. Пройдите мастер: создание кампаний, модерация и статистика идут в `api-sandbox.direct.yandex.com`, деньги не тратятся. В песочнице нет Метрики: счётчик и прогноз не подключатся.
3. **Боевой кабинет.** `DIRECT_SANDBOX=false`, перезапуск, в мастере «Подключить Директ» и Метрику. Первые дни можно держать `DRY_RUN=true`: автопилот только предлагает решения.

## 7. Логи

```sh
dc logs -f api worker caddy
```

Журнал `api` содержит метод, путь и код ответа без строки запроса (коды входа в него не попадают). Воркер пишет `Worker started` и `Worker job <шаг> failed for business <id>`; в `worker_runs` упавший шаг имеет статус `failed` и код `JOB_FAILED`. Все изменения в кабинетах, причины и результаты сверки — таблица `change_log` (экран «История»).

## 8. Резервные копии PostgreSQL

Ежедневная копия, хранение 14 дней (пути замените на свои):

```sh
sudo mkdir -p /srv/backups && sudo chmod 700 /srv/backups
```

```cron
15 3 * * * cd /home/<пользователь>/ad-autopilot && umask 077 && docker compose --env-file .env -f deploy/docker-compose.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > /srv/backups/autopilot-$(date +\%F).sql.gz && chmod 600 /srv/backups/autopilot-$(date +\%F).sql.gz
30 3 * * * find /srv/backups -name 'autopilot-*.sql.gz' -mtime +14 -delete
```

Копируйте файлы на другой сервер, а `.env` (особенно `TOKEN_ENCRYPTION_KEY`) храните отдельно: копия базы без ключа токены не раскрывает.

Восстановление в чистую базу (если таблицы уже есть, сначала пересоздайте базу `dropdb`/`createdb` в контейнере `db`):

```sh
dc stop api worker
gunzip -c /srv/backups/autopilot-ГГГГ-ММ-ДД.sql.gz | dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
dc up -d
```

## Оператор и ключи шифрования (152-ФЗ)

- Задайте `OPERATOR_REQUISITES` (наименование, ИНН/ОГРН, адрес, почта для обращений; публичная строка, без секретов): страница «Данные и политика» покажет её вместо фразы о демонстрационной версии. Плейсхолдеры в квадратных скобках в [PRIVACY.md](PRIVACY.md) заполняет юрист.
- `TOKEN_ENCRYPTION_KEY` (Fernet) не поддерживает ротацию: смена ключа делает токены нечитаемыми (кабинеты попросят войти заново). Для ротации перешифруйте таблицу `platform_accounts` скриптом «расшифровать старым — зашифровать новым» в окне обслуживания.

## 9. Обновление

1. Сделайте копию базы (раздел 8) и запомните версию:

   ```sh
   git rev-parse HEAD > /srv/backups/previous-version
   git branch --show-current
   ```

2. Получите код и соберите образ, пока старая версия работает (интерфейс пока не пересобирайте: Caddy раздаёт `frontend/dist` напрямую):

   ```sh
   git pull
   dc build
   ```

3. Остановите `api` и `worker`, соберите интерфейс, запустите новую версию:

   ```sh
   dc stop api worker
   (cd frontend && npm ci && npm run build)
   dc up -d
   ```

   Если в обновлении появилась сеть с фиксированной подсетью (`CADDY_IP`, `APP_SUBNET`), а сервис запускался до неё, перед `dc up -d` один раз выполните `dc down` (тома сохраняются). `dc up -d` сначала запускает `migrate`; `api` и `worker` стартуют после его успеха.

4. Если `migrate` упал (`dc logs migrate`), миграция откатилась целиком, `api` и `worker` не запущены. Вернитесь: `git checkout $(cat /srv/backups/previous-version)`, `(cd frontend && npm ci && npm run build)`, `dc up -d --build`. Не запускайте `dc start api worker` без отката кода. Когда причина исправлена, `git switch <ветка из шага 1>` и повторите с шага 2.

## 10. Откат версии

1. Копия базы (раздел 8) и `dc stop api worker`.
2. Если новая версия уже применила миграции, откатите схему образом текущей версии: `dc run --rm migrate alembic downgrade <ревизия прежней версии>` (её номер — последний файл в `backend/alembic/versions/` прежнего коммита). Что теряется, описано в docstring миграции.
3. `git checkout <тег или коммит>` (записан в `/srv/backups/previous-version`).
4. `(cd frontend && npm ci && npm run build)` и `dc up -d --build`.

Если откат схемы невозможен или данные повреждены, восстановите базу из копии (раздел 8).

## 11. Остановка

```sh
dc down            # данные в томах остаются
```

Не используйте `dc down -v`: он удалит том с базой.

## 12. Демо-стенд

Демо — отдельный проект `autopilot-demo` со своими томами и `.env.demo` (`PLATFORM_MODE=fake`, `DEMO_LOGIN=1`), запуск описан в [README.md](../README.md). Освободить места демо-пользователей сразу:

```sh
docker compose -p autopilot-demo --env-file .env.demo -f deploy/docker-compose.yml exec api python -m app.demo_reset
```

Команда работает только при `PLATFORM_MODE=fake` и `DEMO_LOGIN=1`. На боевом сервере демо не включайте.
