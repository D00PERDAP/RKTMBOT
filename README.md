# РКТМ NEXT — чистый проект

Единое приложение: **Telegram-бот + красное Mini App + FastAPI + SQLite**. Расписание читается с официального сайта `https://rktm.info/wp-content/uploads/HTML/` (индексы `raspisan.html`, `Praspisan.html`, `Araspisan.html` и связанные HTML-файлы). Список групп берётся с сайта, не зашит в код.

## Структура

- `app.py` — FastAPI, Telegram webhook, API расписания и команды бота
- `features.py` — SQLite, новости, обращения, подписки и отслеживание изменений
- `webapp/index.html` — красное мини-приложение
- `.env.example` — образец переменных окружения (не секреты)
- `requirements.txt` — зависимости
- `tests/` — локальные тесты

## Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

## Linux / Render

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Отредактируйте .env
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

На Render укажите **Build Command** `pip install -r requirements.txt`, **Start Command** `uvicorn app:app --host 0.0.0.0 --port $PORT`. Переменные окружения добавляйте в настройках сервиса, не загружайте `.env` в репозиторий. Для SQLite используйте постоянный диск с `DATABASE_PATH` внутри него; иначе данные могут исчезнуть при пересоздании сервиса.

`WEBAPP_URL` должен быть публичным HTTPS-адресом **этого же сервера**, без слеша на конце. Укажите `BOT_TOKEN` от BotFather и длинные случайные `WEBHOOK_SECRET` и `ADMIN_TOKEN`. После старта сервер автоматически зарегистрирует Telegram webhook. Не запускайте параллельно старого бота с тем же токеном.

Проверка: `/health`, `/api/groups/raspisan`, Telegram `/start`, кнопка «Открыть РКТМ NEXT». Профиль синхронизируется только при открытии Mini App из Telegram, поскольку проверяется подписанная Telegram `initData`.

### Безопасность

- Никогда не публикуйте `.env` и базу данных.
- Если прежний токен попадал в архив, перевыпустите его в BotFather.
- Для админ-API требуется заголовок `X-Admin-Token`.
- Перед боевым запуском проверьте отображение реальных расписаний, работу webhook и уведомлений.

### Границы версии

MAX **не подключён**: это Telegram-версия, подготовленная для последующего добавления второго адаптера. ИИ-ассистент и графическая админ-панель не реализованы. Новости и обращения поддерживаются через API. Уведомления работают только для пользователей, запустивших бота и включивших подписку. Не запускайте несколько копий сервера с одним монитором расписания.
