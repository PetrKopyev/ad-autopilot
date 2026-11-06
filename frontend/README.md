# Интерфейс

Веб-интерфейс сервиса «Автопилот рекламы»: React 18, TypeScript, Vite, React Router. Мастер запуска рекламы, «Ваш план», «Реклама», «Статистика», «История», «Клиенты», «Настройки», страница «Данные и политика» (`/policy`). Зависимости закреплены в `package.json`.

Запуск и проверки:

```sh
npm ci
npm run dev       # dev-сервер, /api проксируется на http://localhost:8000
npm run build
npm test          # vitest
npm run e2e       # Playwright против поднятого стенда, см. корневой README
```

Тексты находятся в `src/copy.ts`, токены и адаптивные стили — в `src/styles.css`, общие компоненты — в `src/components/index.ts`. Запросы к API идут через `api<T>('/api/...')`: только на текущий сайт, с cookie и заголовком `X-CSRFToken` для изменяющих запросов. Стенд целиком (демо и боевой) описан в [корневом README](../README.md) и [docs/DEPLOY.md](../docs/DEPLOY.md).
