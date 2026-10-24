"""MAX messenger bot (development long polling). Telegram service stays separate.

Production deployments should switch to MAX webhooks per official guidance.
"""
import asyncio
from contextvars import ContextVar
import html
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
from app import GroupParser, SCHEDULE_BASE, SCHEDULE_URLS, fetch_html
from bot_flow import schedule_text_parts, today_schedule_day
from features import db, initialize, news_list

API = "https://platform-api2.max.ru"
TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
SOURCES = {"groups": "raspisan", "teachers": "Praspisan"}
log = logging.getLogger("rktm.max")
EDIT_TARGET = ContextVar("max_edit_target", default=None)


def init_max():
    initialize()
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS max_profiles (
            uid TEXT PRIMARY KEY, source TEXT, filename TEXT, title TEXT, role TEXT
        );
        CREATE TABLE IF NOT EXISTS max_states (
            uid TEXT PRIMARY KEY, source TEXT, page INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS max_favorites (
            uid TEXT NOT NULL, source TEXT NOT NULL, filename TEXT NOT NULL,
            title TEXT NOT NULL, PRIMARY KEY(uid, source, filename)
        );
        """)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(max_profiles)")}
        if "role" not in cols:
            conn.execute("ALTER TABLE max_profiles ADD COLUMN role TEXT")


def clean_html(text):
    return BeautifulSoup(text, "html.parser").get_text("\n", strip=True)


def profile(uid):
    with db() as conn:
        row = conn.execute("SELECT * FROM max_profiles WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None


def current_state(uid):
    with db() as conn:
        row = conn.execute("SELECT * FROM max_states WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None


def kb(rows):
    return {"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": title, "payload": action} for title, action in row]
        for row in rows
    ]}}


def home_buttons():
    return kb([
        [("📅 Моё расписание", "/today"), ("🔎 Все расписания", "/browse")],
        [("⭐ Избранное", "/favorites"), ("👤 Профиль", "/profile")],
        [("📰 Новости", "/news"), ("🏠 Меню", "/start")],
    ])


async def send(client, user_id, text, keyboard=None):
    """Edit the pressed menu in place; send normally for typed commands."""
    target = EDIT_TARGET.get()
    remaining = text or " "
    first = True
    while remaining:
        chunk = remaining[:3500]
        if len(remaining) > 3500 and "\\n" in chunk:
            chunk = chunk[:chunk.rfind("\\n")] or chunk
        remaining = remaining[len(chunk):].lstrip("\\n")
        payload = {"text": chunk, "attachments": [keyboard] if keyboard and not remaining else []}
        if target and first:
            try:
                response = await client.put(
                    "/messages", params={"message_id": target},
                    json={**payload, "notify": False}, timeout=25,
                )
                response.raise_for_status()
                result = response.json()
                if result.get("success") is False:
                    raise ValueError(result.get("message", "MAX edit failed"))
                first = False
                continue
            except (httpx.HTTPError, ValueError):
                log.exception("Cannot edit MAX menu; sending new message")
        response = await client.post(
            "/messages", params={"user_id": int(user_id)},
            json=payload, timeout=25,
        )
        response.raise_for_status()
        first = False


async def ask_registration(client, uid):
    await send(client, uid, "👋 Добро пожаловать в РКТМ!\\n\\nКто ты?",
               kb([[("🎓 Студент", "/reg_student"), ("👨‍🏫 Преподаватель", "/reg_teacher")]]))


async def catalog(client, uid, source, page=0):
    html_text = await fetch_html(SCHEDULE_URLS[source])
    parser = GroupParser()
    parser.feed(html_text)
    items = parser.groups
    if not items:
        await send(client, uid, "Список расписаний пока пуст.")
        return
    page = min(max(page, 0), (len(items)-1)//10)
    with db() as conn:
        conn.execute(
            "INSERT INTO max_states(uid,source,page) VALUES(?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET source=excluded.source,page=excluded.page",
            (uid, source, page),
        )
    rows = [f"{i+1}. {item['name']}" for i, item in enumerate(items[page*10:page*10+10])]
    title = "Группы" if source == "raspisan" else "Преподаватели"
    await send(client, uid, f"{title} — страница {page+1}/{(len(items)+9)//10}\n"
               + "\n".join(rows)
               + "\n\nОтправь номер для выбора, /next или /prev для страниц."
               + "\nВыбери расписание кнопкой:",
               kb([[(f"{page*10+i+1}. {item['name']}"[:60], f"/pick {i+1}")] for i,item in enumerate(items[page*10:page*10+10])]
                  + [[("◀️", "/prev"), ("▶️", "/next")],
                     [("🎓 Группы", "/groups"), ("👨‍🏫 Преподаватели", "/teachers")],
                     [("🏠 Главное меню", "/start")]]))


async def choose(client, uid, number):
    state = current_state(uid)
    if not state:
        await send(client, uid, "Сначала выбери /groups или /teachers.")
        return
    source = state["source"]
    page = state["page"]
    parser = GroupParser()
    parser.feed(await fetch_html(SCHEDULE_URLS[source]))
    idx = page * 10 + number - 1
    if not 1 <= number <= 10 or idx >= len(parser.groups):
        await send(client, uid, "Такого номера нет на этой странице.")
        return
    item = parser.groups[idx]
    with db() as conn:
        conn.execute(
            "INSERT INTO max_profiles(uid,source,filename,title,role) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET source=excluded.source,"
            "filename=excluded.filename,title=excluded.title,role=excluded.role",
            (uid, source, item["file"], item["name"], "teacher" if source=="Praspisan" else "student"),
        )
    await show_schedule(client, uid)


async def show_schedule(client, uid, day=None):
    p = profile(uid)
    if not p or not p["filename"]:
        await send(client, uid, "Расписание не выбрано. Открой /groups или /teachers.")
        return
    if day is None:
        day = today_schedule_day()
    if day == 6:
        text = f"{p['title']} — воскресенье\nСегодня выходной, уроков нет."
    else:
        if day not in range(6):
            await send(client, uid, "День недели: от 0 (пн) до 5 (сб).")
            return
        schedule = await fetch_html(urljoin(SCHEDULE_BASE, p["filename"]))
        text = "\n\n".join(clean_html(part) for part in schedule_text_parts(p["title"], schedule, day))
    from substitutions import substitutions_for, format_group_changes
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    local_today = datetime.now(ZoneInfo(os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow"))).date()
    target_date = (local_today + timedelta(days=(day - local_today.weekday()) % 7)).isoformat()
    changes = substitutions_for(p["title"], target_date) if p["source"] == "raspisan" else []
    if changes:
        text += "\n\n" + format_group_changes(changes, p["title"], target_date)
    await send(client, uid, text + "\n\nВыбери день:",
               kb([[("Пн", "/day 0"), ("Вт", "/day 1"), ("Ср", "/day 2")],
                   [("Чт", "/day 3"), ("Пт", "/day 4"), ("Сб", "/day 5")],
                   [("📅 Сегодня", "/today"), ("⭐ Сохранить", "/save")],
                   [("🔎 Все расписания", "/browse"), ("🏠 Меню", "/start")]]))


async def handle(client, uid, text):
    text = (text or "").strip()
    command = text.lower().split()[0] if text else ""
    if command in ("/start", "/help", "меню") and not profile(uid):
        await ask_registration(client, uid)
    elif command in ("/reg_student", "/reg_teacher"):
        await catalog(client, uid, "raspisan" if command=="/reg_student" else "Praspisan")
    elif command in ("/start", "/help", "меню"):
        await send(client, uid, "📚 РКТМ | Главное меню\n\nВыбери нужный раздел 👇",
                   home_buttons())
    elif command == "/register":
        await ask_registration(client, uid)
    elif command == "/browse":
        await send(client, uid, "🔎 Все расписания\\nВыбери категорию:",
                   kb([[("🎓 Группы", "/groups")],
                       [("👨‍🏫 Преподаватели", "/teachers")],
                       [("🏠 Меню", "/start")]]))
    elif command == "/pick":
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            await choose(client, uid, int(parts[1]))
        else:
            await send(client, uid, "Выбери расписание кнопкой.")
    elif command == "/groups":
        await catalog(client, uid, SOURCES["groups"])
    elif command == "/teachers":
        await catalog(client, uid, SOURCES["teachers"])
    elif command in ("/next", "/prev"):
        s = current_state(uid)
        if s:
            await catalog(client, uid, s["source"], s["page"] + (1 if command == "/next" else -1))
        else:
            await send(client, uid, "Сначала открой /groups или /teachers.")
    elif command in ("/today", "/schedule"):
        await show_schedule(client, uid)
    elif command == "/day":
        parts = text.split()
        if len(parts) != 2 or parts[1] not in [str(x) for x in range(6)]:
            await send(client, uid, "Используй /day 0 (пн) ... /day 5 (сб).")
        else:
            await show_schedule(client, uid, int(parts[1]))
    elif command == "/profile":
        p = profile(uid)
        await send(client, uid, "👤 Профиль\\nРоль: " + ("Преподаватель" if p and p.get("role")=="teacher" else "Студент") + "\\nТвоё расписание: " + (p["title"] if p else "не выбрано"),
                   kb([[("🔄 Изменить группу / преподавателя", "/register")],
                       [("🏠 Меню", "/start")]]))
    elif command == "/save":
        p = profile(uid)
        if not p:
            await send(client, uid, "Сначала выбери расписание.")
        else:
            with db() as conn:
                conn.execute("INSERT OR IGNORE INTO max_favorites VALUES (?,?,?,?)",
                             (uid, p["source"], p["filename"], p["title"]))
            await send(client, uid, "⭐ Добавлено в избранное.", home_buttons())
    elif command == "/favorites":
        with db() as conn:
            items = conn.execute("SELECT title FROM max_favorites WHERE uid=? ORDER BY title", (uid,)).fetchall()
        await send(client, uid, "⭐ Избранное:\n" + ("\n".join(x["title"] for x in items) if items else "Пока пусто."), home_buttons())
    elif command == "/news":
        posts = news_list()[:5]
        await send(client, uid, "\n\n".join(x["title"] + "\n" + x["body"] for x in posts) if posts else "Новостей пока нет.", home_buttons())
    elif text.isdigit():
        await choose(client, uid, int(text))
    else:
        await send(client, uid, "Неизвестная команда. Отправь /help.")


async def run():
    if not TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в .env")
    init_max()
    marker = None
    async with httpx.AsyncClient(base_url=API, headers={"Authorization": TOKEN}, timeout=50) as client:
        while True:
            try:
                params = {"timeout": 30, "limit": 100}
                if marker is not None:
                    params["marker"] = marker
                response = await client.get("/updates", params=params, timeout=45)
                response.raise_for_status()
                data = response.json()
                for update in data.get("updates", []):
                    kind = update.get("update_type")
                    if kind == "bot_started":
                        uid = str((update.get("user") or {}).get("user_id", ""))
                        message = "/start"
                    elif kind == "message_created":
                        msg = update.get("message") or {}
                        uid = str((msg.get("sender") or {}).get("user_id", ""))
                        message = (msg.get("body") or {}).get("text", "")
                    elif kind == "message_callback":
                        callback = update.get("callback") or {}
                        uid = str((callback.get("user") or {}).get("user_id", ""))
                        message = callback.get("payload", "")
                        edit_mid = ((update.get("message") or {}).get("body") or {}).get("mid")
                        callback_id = callback.get("callback_id")
                        if callback_id:
                            try:
                                ack = await client.post("/answers", params={"callback_id": callback_id},
                                                        json={}, timeout=15)
                                ack.raise_for_status()
                            except httpx.HTTPError:
                                log.exception("Callback acknowledgement failed")
                    else:
                        continue
                    if uid and uid != "None":
                        token = EDIT_TARGET.set(edit_mid if kind == "message_callback" else None)
                        try:
                            await handle(client, uid, message)
                        except Exception:
                            log.exception("Failed to handle MAX message for user %s", uid)
                        finally:
                            EDIT_TARGET.reset(token)
                marker = data.get("marker", marker)
            except (httpx.HTTPError, ValueError):
                log.exception("MAX polling error, retrying")
                await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
