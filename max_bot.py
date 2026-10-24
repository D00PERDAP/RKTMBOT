"""MAX messenger bot (development long polling). Telegram service stays separate.

Production deployments should switch to MAX webhooks per official guidance.
"""
import asyncio
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


def init_max():
    initialize()
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS max_profiles (
            uid TEXT PRIMARY KEY, source TEXT, filename TEXT, title TEXT
        );
        CREATE TABLE IF NOT EXISTS max_states (
            uid TEXT PRIMARY KEY, source TEXT, page INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS max_favorites (
            uid TEXT NOT NULL, source TEXT NOT NULL, filename TEXT NOT NULL,
            title TEXT NOT NULL, PRIMARY KEY(uid, source, filename)
        );
        """)


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


async def send(client, user_id, text):
    # MAX message size limit; split on line boundaries where possible.
    while text:
        chunk = text[:3500]
        if len(text) > 3500 and "\n" in chunk:
            chunk = chunk[:chunk.rfind("\n")] or chunk
        response = await client.post(
            "/messages", params={"user_id": int(user_id)},
            json={"text": chunk}, timeout=25,
        )
        response.raise_for_status()
        text = text[len(chunk):].lstrip("\n")


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
               + "\n/groups — группы, /teachers — преподаватели.")


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
            "INSERT INTO max_profiles(uid,source,filename,title) VALUES(?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET source=excluded.source,"
            "filename=excluded.filename,title=excluded.title",
            (uid, source, item["file"], item["name"]),
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
    await send(client, uid, text + "\n\n/day 0..5 — выбрать день; /today — сегодня; /save — в избранное.")


async def handle(client, uid, text):
    text = (text or "").strip()
    command = text.lower().split()[0] if text else ""
    if command in ("/start", "/help", "меню"):
        await send(client, uid, "РКТМ — расписание\n\n/groups — группы\n/teachers — преподаватели"
                   "\n/today — моё расписание сегодня\n/day 0 — понедельник (до 5 — суббота)"
                   "\n/save — сохранить в избранное\n/favorites — избранное"
                   "\n/news — новости\n/profile — профиль")
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
        await send(client, uid, "Твоё расписание: " + (p["title"] if p else "не выбрано"))
    elif command == "/save":
        p = profile(uid)
        if not p:
            await send(client, uid, "Сначала выбери расписание.")
        else:
            with db() as conn:
                conn.execute("INSERT OR IGNORE INTO max_favorites VALUES (?,?,?,?)",
                             (uid, p["source"], p["filename"], p["title"]))
            await send(client, uid, "Добавлено в избранное.")
    elif command == "/favorites":
        with db() as conn:
            items = conn.execute("SELECT title FROM max_favorites WHERE uid=? ORDER BY title", (uid,)).fetchall()
        await send(client, uid, "Избранное:\n" + ("\n".join(x["title"] for x in items) if items else "Пока пусто."))
    elif command == "/news":
        posts = news_list()[:5]
        await send(client, uid, "\n\n".join(x["title"] + "\n" + x["body"] for x in posts) if posts else "Новостей пока нет.")
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
                    else:
                        continue
                    if uid and uid != "None":
                        try:
                            await handle(client, uid, message)
                        except Exception:
                            log.exception("Failed to handle MAX message for user %s", uid)
                marker = data.get("marker", marker)
            except (httpx.HTTPError, ValueError):
                log.exception("MAX polling error, retrying")
                await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
