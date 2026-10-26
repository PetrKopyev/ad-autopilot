# Автопилот рекламы

Сервис, который сам ведёт рекламу малого бизнеса (маникюрный салон, йога-студия и похожие) в Яндекс Директе и VK Рекламе. Владелец описывает бизнес, подключает свои рекламные кабинеты и задаёт месячный бюджет. Дальше сервис сам создаёт кампании, останавливает неэффективные объявления, добавляет денег эффективным, проверяет пограничные объявления A/B-тестами и показывает расход, обращения и цену клиента простым языком. Каждое изменение записывается в историю с понятной причиной, и его можно откатить. Все решения принимает код по понятным правилам (см. [RULES.md](docs/RULES.md)); нейросеть YandexGPT/YandexART только пишет тексты и рисует картинки. Бюджет защищён четырьмя слоями, расход не должен превысить заданный.

## Быстрый старт: демо без ключей

Потребуется Node.js 20 или новее.

```sh
cd frontend
npm ci
VITE_MOCK=1 npm run dev
```

Откройте адрес, который напечатает Vite (обычно http://localhost:5173). Интерфейс работает на заготовленных данных, без сервера, базы и ключей: вход, мастер запуска, статистика, объявления, история с откатом, клиенты, настройки.

Демо с настоящим бэкендом и симулятором площадок (`PLATFORM_MODE=fake`; внешние кабинеты, YandexGPT и YandexART не нужны) запускается тем же Docker Compose, что и боевой сервис. Вход в сервис всегда идёт через Яндекс ID, поэтому нужно приложение Яндекса только для входа (права «логин» и «почта»), см. [SETUP-YANDEX.md](docs/SETUP-YANDEX.md). Ключи Директа, VK и Yandex Cloud не нужны.

```sh
cp .env.example .env     # заполните POSTGRES_PASSWORD, SECRET_KEY, TOKEN_ENCRYPTION_KEY, YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET
                         # и поставьте PLATFORM_MODE=fake, DRY_RUN=false
(cd frontend && npm ci && npm run build)
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
curl -k https://localhost/api/health
```

Redirect URI приложения Яндекса для локального демо: `https://localhost/api/auth/yandex/callback` (принимает ли форма Яндекса адрес `localhost`, проверьте при создании приложения; иначе разверните демо на сервере с доменом). Подробности про переменные и боевой запуск: [docs/DEPLOY.md](docs/DEPLOY.md). Состояние симулятора живёт в памяти процесса `api`: после его перезапуска демо-запуск нужно создать заново.

## Документы

| Документ | О чём |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Развёртывание на VPS, `.env`, обновление, резервные копии, откат версии |
| [docs/SETUP-YANDEX.md](docs/SETUP-YANDEX.md) | Приложение Яндекса, доступ к API Директа, песочница, Yandex Cloud |
| [docs/SETUP-VK.md](docs/SETUP-VK.md) | Приложение VK Рекламы, доступ к API, вход через VK или ключ |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Компоненты, поток изменения, защита бюджета, расписание воркера |
| [docs/RULES.md](docs/RULES.md) | Правила автопилота и пороги простым языком |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Что проверено только на симуляторе и известные ограничения |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Черновик политики обработки персональных данных (152-ФЗ) |
| [docs/RESEARCH.md](docs/RESEARCH.md) | Исследование рынка и площадок |
| [docs/internal/PLAN.md](docs/internal/PLAN.md) | Внутренний план и обоснования |

## Тесты

Бэкенд (Python 3.12 или новее):

```sh
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest -m "not db"
```

Тесты с базой (`-m db`) нужен PostgreSQL. Укажите его в `TEST_DATABASE_URL` с драйвером psycopg (не asyncpg) и сначала примените миграции: тесты, кроме миграционного, схему сами не создают.

```sh
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/autopilot_test'
DATABASE_URL="$TEST_DATABASE_URL" APP_BASE_URL=https://localhost SECRET_KEY=0123456789abcdef0123456789abcdef \
  TOKEN_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  alembic upgrade head
pytest -m db
```

Фронтенд:

```sh
cd frontend
npm ci
npm run build                 # проверка типов и сборка в frontend/dist
VITE_MOCK=1 npm run build     # сборка с заготовленными данными, как в демо
```

## Устройство репозитория

```
backend/    FastAPI, воркер, миграции (alembic), тесты
frontend/   React + TypeScript + Vite
deploy/     docker-compose.yml, Caddyfile
docs/       документация
.env.example  все настройки сервиса
```
