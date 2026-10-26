# Развёртывание

Сервис состоит из пяти контейнеров Docker Compose: `db` (PostgreSQL 16), `migrate` (разово применяет миграции), `api`, `worker` и `caddy` (HTTPS и статика интерфейса). Схема: [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Сервер

- VPS с Ubuntu 22.04 или 24.04, 2 ГБ памяти, 2 ГБ swap (сборка образа и интерфейса на 2 ГБ без swap может не пройти), 20 ГБ диска.
- Сервер в России: сервис хранит персональные данные граждан РФ, 152-ФЗ требует, чтобы первичная запись и хранение были на территории РФ ([PRIVACY.md](PRIVACY.md)). Подойдёт любой российский провайдер.
- Домен и DNS: A-запись `<домен>` на IP сервера. Caddy сам получает сертификат Let's Encrypt, для этого должны быть открыты порты 80 и 443 (в файрволе провайдера и в `ufw`), а DNS уже указывать на сервер.
- Установите Docker и плагин Compose:

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # перелогиньтесь
sudo apt-get install -y git curl
```

- Node.js 20 или новее нужен только для сборки интерфейса: [NodeSource](https://github.com/nodesource/distributions) или `nvm`.

## 2. Код и интерфейс

```sh
git clone <адрес репозитория> ad-autopilot
cd ad-autopilot
(cd frontend && npm ci && npm run build)
```

Caddy раздаёт готовую сборку из `frontend/dist`: без этой команды сайт откроется пустым.

## 3. Настройки (`.env`)

```sh
cp .env.example .env
chmod 600 .env
```

Файл читают все сервисы, Compose подставляет из него `POSTGRES_*` и `APP_DOMAIN`, поэтому запускайте всегда с `--env-file .env`. Не коммитьте `.env`.

Сгенерировать секреты:

```sh
openssl rand -hex 24      # POSTGRES_PASSWORD (hex, без спецсимволов: пароль входит в DATABASE_URL)
openssl rand -hex 32      # SECRET_KEY
docker run --rm python:3.12-slim sh -c "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"   # TOKEN_ENCRYPTION_KEY
```

| Переменная | Что это и где взять | Обязательна |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_DB` | Имя роли и базы PostgreSQL; любые, по умолчанию `autopilot` | да |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL, случайная строка | да |
| `DATABASE_URL` | Адрес базы для приложения; в `.env.example` уже собран из `POSTGRES_*` и хоста `db`, менять не нужно | да |
| `APP_BASE_URL` | Публичный адрес сервиса, `https://<домен>`, без слэша на конце. Из него строятся redirect URI Яндекса и VK | да |
| `APP_DOMAIN` | Домен для Caddy, тот же, что в `APP_BASE_URL`, без `https://`. Для локального запуска `localhost` | да |
| `SECRET_KEY` | Подпись служебных значений входа, не короче 32 символов; значение-заглушка из `.env.example` сервис отклоняет | да |
| `TOKEN_ENCRYPTION_KEY` | Ключ Fernet, которым шифруются токены кабинетов, ИНН и телефон. Потеряете ключ: сохранённые токены и данные для маркировки не прочитать, кабинеты придётся подключать заново. Храните копию отдельно от копий базы | да |
| `PLATFORM_MODE` | `live` (боевой, по умолчанию) или `fake` (симулятор площадок для демо, без внешних кабинетов) | нет |
| `DRY_RUN` | `true` (по умолчанию): автопилот только записывает «предложено» в историю и ничего не меняет в кабинетах. `false`: решения применяются. Запуск рекламы из мастера от `DRY_RUN` не зависит. Включайте `false`, когда готовы к реальным изменениям | нет |
| `YANDEX_CLIENT_ID` | ID приложения на oauth.yandex.ru ([SETUP-YANDEX.md](SETUP-YANDEX.md)). Без него войти в сервис нельзя | да |
| `YANDEX_CLIENT_SECRET` | Пароль того же приложения. Код обменивает коды входа и с PKCE без него, но у приложения Яндекса он есть: задайте | рекомендуется |
| `DIRECT_API_URL`, `DIRECT_API_V501_URL` | Адреса API Директа, значения по умолчанию верны. Менять не нужно | нет |
| `DIRECT_SANDBOX` | `true`: все обращения идут на `api-sandbox.direct.yandex.com`, хост подменяется сам. По умолчанию `false` | нет |
| `VK_CLIENT_ID`, `VK_CLIENT_SECRET` | Приложение VK Рекламы ([SETUP-VK.md](SETUP-VK.md)). Нужны для кнопки «Войти через VK» и для обновления токенов. Без них VK подключается только ключом API, который пользователь вводит сам | нет |
| `YC_FOLDER_ID`, `YC_API_KEY` | Каталог и API-ключ сервисного аккаунта Yandex Cloud для YandexGPT и YandexART ([SETUP-YANDEX.md](SETUP-YANDEX.md)). Без них (или при `PLATFORM_MODE=fake`) сервис подставляет заглушки вместо нейросети: тексты и картинки будут шаблонными | да (для боевого режима) |
| `YANDEXGPT_MODEL` | Имя модели, по умолчанию `yandexgpt/latest`. Стоимость токенов в учёте трат зашита в коде как для YandexGPT Pro 5.1 (0,8 ₽ за 1000 токенов) | нет |
| `BUDGET_RESERVE_PCT` | Резерв бюджета в процентах, по умолчанию 8. Учитывается при подготовке плана и запуске; ежедневная перебалансировка и страж считают с 8%, см. [LIMITATIONS.md](LIMITATIONS.md) | нет |
| `AI_SPEND_ONBOARDING_LIMIT_RUB`, `AI_SPEND_WEEKLY_LIMIT_RUB` | Лимиты расходов на нейросеть на один бизнес: на подготовку плана (по умолчанию 60 ₽) и в неделю (25 ₽) | нет |
| `TZ_DEFAULT` | Часовой пояс по умолчанию, `Europe/Moscow`. Расписание воркера считается по Москве | нет |
| `MEDIA_DIR` | Каталог для загруженных и сгенерированных картинок (в `.env.example` нет, значение по умолчанию `media`). См. предупреждение ниже | нет |

Предупреждение про `MEDIA_DIR`: в `deploy/docker-compose.yml` нет тома для картинок, а контейнеры работают от пользователя без прав записи в `/app`. Пока том не добавлен, загрузка фото и генерация картинок в контейнере не заработают с настройками по умолчанию. Это известный пробел сборки, он записан в [LIMITATIONS.md](LIMITATIONS.md). Перед боевым запуском нужно смонтировать общий для `api` и `worker` каталог с правами для пользователя с uid 10001 и указать его в `MEDIA_DIR`.

## 4. Запуск

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
```

Сервис `migrate` дожидается базы, выполняет `alembic upgrade head` и завершается; `api` и `worker` стартуют только после его успешного завершения. Если миграция упала, остальные сервисы не запустятся: смотрите `docker compose --env-file .env -f deploy/docker-compose.yml logs migrate`.

Для краткости дальше `dc` означает `docker compose --env-file .env -f deploy/docker-compose.yml`:

```sh
alias dc='docker compose --env-file .env -f deploy/docker-compose.yml'
```

## 5. Проверка

```sh
dc ps                                  # db, api, worker, caddy в состоянии running; migrate в exited (0)
curl https://<домен>/api/health        # {"ok":true,"db":true,"mode":"live"}
```

`db: false` значит, что API не достучался до PostgreSQL. `mode` показывает `PLATFORM_MODE`. Откройте `https://<домен>` и нажмите «Войти через Яндекс». Если Яндекс отвечает ошибкой redirect URI, проверьте [SETUP-YANDEX.md](SETUP-YANDEX.md).

Воркер жив, если в таблице `worker_runs` каждую минуту появляется запись `heartbeat`:

```sh
dc exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select kind, status, started_at from worker_runs order by id desc limit 10"'
```

## 6. Логи

```sh
dc logs -f api worker caddy
```

Воркер пишет `Worker started` при старте и `Worker job <шаг> failed for business <id>` при сбое шага. В `worker_runs` у упавшего шага статус `failed` и код `JOB_FAILED` (ответы площадок, чтобы не хранить чужие данные, туда не пишутся). Любое изменение в кабинетах, его причина и результат сверки лежат в таблице `change_log` и видны владельцу на экране «История».

## 7. Резервные копии PostgreSQL

Ежедневная копия, хранить 14 дней. Создайте каталог и добавьте в `crontab -e` строки (пути замените на свои):

```sh
mkdir -p /srv/backups
```

```cron
15 3 * * * cd /home/<пользователь>/ad-autopilot && docker compose --env-file .env -f deploy/docker-compose.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > /srv/backups/autopilot-$(date +\%F).sql.gz
30 3 * * * find /srv/backups -name 'autopilot-*.sql.gz' -mtime +14 -delete
```

Копируйте файлы на другой сервер или в объектное хранилище, а `.env` (особенно `TOKEN_ENCRYPTION_KEY`) храните отдельно от копий базы: копия базы без ключа токены не раскрывает.

Восстановление в чистую базу (после `up -d db`, когда схема ещё не создана; остановите `api` и `worker`):

```sh
dc stop api worker
gunzip -c /srv/backups/autopilot-ГГГГ-ММ-ДД.sql.gz | dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
dc up -d
```

Если в базе уже есть таблицы, сначала пересоздайте базу (`dropdb`/`createdb` внутри контейнера `db`).

## 8. Обновление

1. Сделайте копию базы (раздел 7) и запомните текущую версию — к ней возвращаются, если обновление не удалось:

   ```sh
   git rev-parse HEAD > /srv/backups/previous-version
   git branch --show-current
   ```

2. Получите код и соберите образ, пока старая версия ещё работает. Интерфейс пока не пересобирайте: Caddy раздаёт `frontend/dist` напрямую, и новый интерфейс заработал бы раньше нового API.

   ```sh
   git pull
   dc build
   ```

3. Остановите `api` и `worker`, соберите интерфейс и запустите новую версию:

   ```sh
   dc stop api worker
   (cd frontend && npm ci && npm run build)
   dc up -d
   ```

   `dc up -d` сначала запускает сервис `migrate` (`alembic upgrade head`), а `api` и `worker` стартуют только после его успешного завершения. Если их не остановить заранее, старые контейнеры работают до своего пересоздания и успевают писать в базу во время миграции: старый воркер выполняет шаги, старый API пишет в `worker_runs` через перемотку демо. Такие строки останутся без новых ключей идемпотентности.

4. Если `migrate` упал (`dc logs migrate`), миграция откатилась целиком и схема осталась прежней, а `api` и `worker` не запущены. Верните прежнюю версию: `git checkout $(cat /srv/backups/previous-version)`, `(cd frontend && npm ci && npm run build)` и `dc up -d --build`; её `migrate` ничего не изменит. Не запускайте `dc start api worker` без отката кода: `up` мог уже пересоздать контейнеры на новом образе. Когда причина исправлена, вернитесь на ветку (`git switch <ветка из шага 1>`) и повторите обновление с шага 2.

## 9. Откат версии

1. Сделайте копию базы (раздел 7) и остановите приложение: `dc stop api worker`.
2. Если новая версия уже применила миграции, откатите схему образом текущей версии: в нём есть файлы миграций, которых нет в старом коде. `dc run --rm migrate alembic downgrade <ревизия прежней версии>` (её номер — последний файл в `backend/alembic/versions/` прежнего коммита; можно и `downgrade -1` нужное число раз). Что теряется при откате, написано в docstring каждой миграции.
3. Вернитесь на прежний коммит или тег: `git checkout <тег или коммит>` (записан в `/srv/backups/previous-version` при обновлении, раздел 8).
4. Пересоберите интерфейс и образ и запустите заново: `(cd frontend && npm ci && npm run build)` и `dc up -d --build`. `migrate` старой версии увидит, что схема уже на её последней ревизии, и ничего не изменит.

Если откат схемы невозможен или данные повреждены, восстановите базу из копии (раздел 7).

## 10. Остановка

```sh
dc down            # контейнеры останавливаются, данные в томах остаются
```

Не используйте `dc down -v`: он удалит том с базой.
