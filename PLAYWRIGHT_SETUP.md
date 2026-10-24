# Установка HTML/CSS-генератора расписаний

Бот делает PNG с расписанием через Playwright/Chromium. ИИ API и платные ключи не нужны.

## После merge PR

На Ubuntu VPS:

```bash
cd /opt/rktm
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python -m py_compile app.py bot_flow.py schedule_browser.py
sudo systemctl restart rktm
sudo systemctl status rktm --no-pager -l
```

Если сервис не запустился:

```bash
sudo journalctl -u rktm -n 100 --no-pager
```

Браузер скачивается на VPS и требует свободного места. Для systemd с отдельным пользователем установку браузера надо выполнять от того же пользователя, от которого работает сервис (либо настроить PLAYWRIGHT_BROWSERS_PATH и установить туда). 

## Проверка

В Telegram: `/start` → роль → `📚 Выбрать расписание` → `📅 Моё расписание`. Длинные таблицы отправляются несколькими PNG.

Если в BotFather включён Main Mini App, его нужно отключить отдельно через @BotFather: Telegram может показывать кнопку независимо от reply-клавиатуры бота.

Примечание: изменения ещё не проверены на реальном VPS. Перед production-переходом протестировать на нескольких реальных расписаниях, особенно с объединёнными ячейками HTML-таблиц.
