# Каркас интерфейса

React 18, TypeScript, Vite и React Router. Команды: `npm run dev`, `npm run build`, `npm run preview`. Зависимости перечислены с точными версиями в `package.json`.

Маршруты: `/`, `/login`, `/setup` и `/setup/1`–`/setup/5`, `/plan`, `/ads`, `/statistics`, `/history`, `/clients`, `/settings`, `/connect/vk`, `/platform-error`, `/policy`. Неизвестные адреса показывают страницу ошибки. Экраны пока служат заглушками; подключение кабинетов и запуск рекламы недоступны.

Тексты находятся в `src/copy.ts`, токены и адаптивные стили — в `src/styles.css`. Общие компоненты экспортируются из `src/components/index.ts`. `Money` и `BudgetRing` принимают суммы в целых копейках в пределах безопасного целого JavaScript; форматирование рублей не использует дробные вычисления.

`api<T>('/api/...', options)` отправляет JSON и cookie только на текущий сайт. Для изменяющих запросов читает cookie `csrftoken` и передаёт её в `X-CSRFToken`. Имена можно настроить через `createApiClient`. Пустой ответ возвращает `undefined`; ошибки имеют тип `ApiError` с понятным сообщением и HTTP-статусом. Передайте `signal`, чтобы отменить запрос. Vite проксирует `/api` на `http://localhost:8000`.
