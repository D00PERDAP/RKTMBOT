"""MAX messenger bot (development long polling). Telegram service stays separate.

Production deployments should switch to MAX webhooks per official guidance.
"""
import asyncio
from datetime import datetime, timedelta
from collections import OrderedDict
import hashlib
from html import escape
from zoneinfo import ZoneInfo
from contextvars import ContextVar
import logging
import os
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
from app import GroupParser, SCHEDULE_BASE, SCHEDULE_URLS, fetch_html
from bot_flow import schedule_text_parts, today_schedule_day
from features import db, initialize, news_list, save_user, user

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
        # Preserve existing explicit subscriptions, including muted profiles.
        conn.execute("INSERT OR IGNORE INTO users(platform,user_id,source,filename,group_name,notifications) "
                     "SELECT 'max',uid,source,filename,title,1 FROM max_profiles WHERE filename IS NOT NULL")
    from substitutions import setup_store
    setup_store()



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
        [("📅 Моё расписание", "/today")],
        [("🔄 Мои замены", "/subsmine"), ("📆 Замены по дате", "/subsdates")],
        [("🔎 Выбрать расписание", "/browse")],
        [("⭐ Избранное", "/favorites"), ("👤 Профиль", "/profile")],
        [("🔔 Уведомления", "/settings"), ("📰 Новости", "/news")],
    ])


def back_home():
    return kb([[("🏠 Главное меню", "/start")]])


def profile_label(p):
    kind = "Преподаватель" if p["source"] == "Praspisan" else "Группа"
    return f"{kind}: {p['title'].rstrip(' ,')}"


def notification_enabled(uid):
    return bool((user("max", uid) or {}).get("notifications"))


async def show_home(client, uid):
    p = profile(uid)
    if not p:
        await ask_registration(client, uid)
        return
    status = "включены 🔔" if notification_enabled(uid) else "выключены 🔕"
    await send(client, uid,
               f"📚 РКТМ · Главное меню\n\n{profile_label(p)}\n"
               f"Уведомления о заменах: {status}\n\nЧто посмотреть?", home_buttons())


async def show_settings(client, uid, notice=""):
    if not profile(uid):
        await ask_registration(client, uid)
        return
    enabled = notification_enabled(uid)
    action = ("🔕 Выключить уведомления", "/mute") if enabled else ("🔔 Включить уведомления", "/notify")
    await send(client, uid, "🔔 Уведомления о заменах\n\n"
        + (notice + "\n\n" if notice else "")
        + ("Сейчас включены.\n" if enabled else "Сейчас выключены.\n")
        + "Бот сообщает о публикациях и исправлениях замен для твоей группы или преподавателя.\n\n"
        + "Расписание и замены доступны в меню при любом режиме уведомлений.",
        kb([[action], [("👤 Профиль", "/profile"), ("🏠 Главное меню", "/start")]]))


async def send(client, user_id, text, keyboard=None):
    """Edit the pressed menu in place; send normally for typed commands."""
    target = EDIT_TARGET.get()
    from substitutions import message_parts
    chunks = list(message_parts(text or " "))
    first = True
    for index, chunk in enumerate(chunks):
        payload = {"text": chunk, "attachments": [keyboard] if keyboard and index == len(chunks) - 1 else []}
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
        if response.json().get("success") is False:
            raise ValueError("MAX rejected message")
        first = False


_IMAGE_TOKENS = OrderedDict()


async def upload_image(client, image):
    key = hashlib.sha256(image).hexdigest()
    if key in _IMAGE_TOKENS:
        _IMAGE_TOKENS.move_to_end(key)
        return _IMAGE_TOKENS[key]
    response = await client.post("/uploads", params={"type":"image"})
    response.raise_for_status()
    upload_url = response.json()["url"]
    if not upload_url.startswith("https://"):
        raise ValueError("MAX returned an invalid upload URL")
    # The signed upload URL supplies its own credentials; do not forward the bot token.
    request = client.build_request("POST", upload_url,
        files={"data":("schedule.png", image, "image/png")}, timeout=45)
    request.headers.pop("Authorization", None)
    uploaded = await client.send(request)
    uploaded.raise_for_status()
    data = uploaded.json()
    token = data.get("token")
    if not token:
        token = next((photo.get("token") for photo in data.get("photos", {}).values()
                      if isinstance(photo,dict) and photo.get("token")), None)
    if not token:
        raise ValueError("MAX did not return an image token")
    _IMAGE_TOKENS[key] = token
    while len(_IMAGE_TOKENS) > 32:
        _IMAGE_TOKENS.popitem(last=False)
    return token


async def image_request(client, method, params, payload):
    for attempt in range(4):
        response = await client.request(method, "/messages", params=params, json=payload, timeout=25)
        data = response.json()
        if data.get("code") == "attachment.not.ready" and attempt < 3:
            await asyncio.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            _IMAGE_TOKENS.clear()
        response.raise_for_status()
        if data.get("success") is False or data.get("code") == "attachment.not.ready":
            _IMAGE_TOKENS.clear()
            raise ValueError("MAX could not send the image")
        return


async def send_photo_pages(client, uid, pages, keyboard, title):
    # Prepare every upload before editing the screen. Failed uploads fall back to text.
    tokens = [await upload_image(client, page) for page in pages]
    for index, token in enumerate(tokens):
        attachments = [{"type":"image", "payload":{"token":token}}]
        if index == len(tokens)-1:
            attachments.append(keyboard)
        payload = {"text":f"📅 {title} · {index+1}/{len(tokens)}"[:900], "attachments":attachments}
        target = EDIT_TARGET.get() if index == 0 else None
        if target:
            try:
                await image_request(client, "PUT", {"message_id":target}, {**payload,"notify":False})
                continue
            except (httpx.HTTPStatusError, ValueError):
                log.info("Cannot replace MAX photo screen; sending new photo")
        await image_request(client, "POST", {"user_id":int(uid)}, payload)


async def ask_registration(client, uid):
    rows = [[("🎓 Я студент", "/reg_student")], [("👨‍🏫 Я преподаватель", "/reg_teacher")]]
    if profile(uid):
        rows.append([("⬅️ Главное меню", "/start")])
    await send(client, uid, "📚 РКТМ · Расписание и замены\n\n"
        "Выбери, чьё расписание сохранить в профиле.\n"
        "Так бот сможет показывать твои занятия и присылать нужные замены.", kb(rows))


async def catalog(client, uid, source, page=0):
    parser = GroupParser()
    parser.feed(await fetch_html(SCHEDULE_URLS[source]))
    items = parser.groups
    title = "🎓 Выбери группу" if source == "raspisan" else "👨‍🏫 Выбери преподавателя"
    if not items:
        await send(client, uid, title + "\n\nНа сайте пока нет списка. Попробуй обновить его позже.",
            kb([[("🔄 Обновить список", "/groups" if source == "raspisan" else "/teachers")],
                [("⬅️ Назад", "/browse"), ("🏠 Главное меню", "/start")]]))
        return
    pages = (len(items) + 9) // 10
    page = min(max(page, 0), pages - 1)
    with db() as conn:
        conn.execute("INSERT INTO max_states(uid,source,page) VALUES(?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET source=excluded.source,page=excluded.page", (uid, source, page))
    rows = [[(item["name"][:60], f"/select {source} {item['file']}")]
            for item in items[page*10:page*10+10]]
    navigation = []
    if page:
        navigation.append(("← Назад", f"/catalog {source} {page-1}"))
    if page + 1 < pages:
        navigation.append(("Далее →", f"/catalog {source} {page+1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("⬅️ К категориям", "/browse"), ("🏠 Главное меню", "/start")])
    await send(client, uid, f"{title}\n\nСтраница {page+1} из {pages}\n"
        "Нажми на имя в списке ниже. Выбор сохранится в твоём профиле.\n"
        "Режим уведомлений при смене выбора сохранится.", kb(rows))


async def choose(client, uid, number=None, source=None, filename=None):
    if source is None:
        state = current_state(uid)
        if not state:
            await ask_registration(client, uid)
            return
        source = state["source"]
    if source not in SOURCES.values():
        await ask_registration(client, uid)
        return
    parser = GroupParser()
    parser.feed(await fetch_html(SCHEDULE_URLS[source]))
    if filename is not None:
        item = next((item for item in parser.groups if item["file"] == filename), None)
    else:
        state = current_state(uid)
        idx = state["page"] * 10 + (number or 0) - 1
        item = parser.groups[idx] if number and 1 <= number <= 10 and 0 <= idx < len(parser.groups) else None
    if not item:
        await send(client, uid, "Список изменился. Открой его заново и выбери расписание.",
                   kb([[("🔄 Открыть список", "/groups" if source == "raspisan" else "/teachers")],
                       [("🏠 Главное меню", "/start")]]))
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO max_profiles(uid,source,filename,title,role) VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET source=excluded.source,"
            "filename=excluded.filename,title=excluded.title,role=excluded.role",
            (uid, source, item["file"], item["name"], "teacher" if source=="Praspisan" else "student"),
        )
        conn.execute("INSERT INTO users(platform,user_id,source,filename,group_name,notifications) "
            "VALUES('max',?,?,?,?,1) ON CONFLICT(platform,user_id) DO UPDATE SET "
            "source=excluded.source,filename=excluded.filename,group_name=excluded.group_name",
            (str(uid), source, item["file"], item["name"]))
    await send(client, uid, "✅ Расписание сохранено\n\n" + profile_label({"source":source,"title":item["name"]})
        + "\n\nТеперь можно посмотреть занятия и свои замены.",
        kb([[("📅 Моё расписание", "/today")], [("🔄 Мои замены", "/subsmine")],
            [("👤 Профиль", "/profile"), ("🏠 Главное меню", "/start")]]))


async def show_schedule(client, uid, day=None, selected=None):
    p = selected or profile(uid)
    if not p or not p["filename"]:
        await ask_registration(client, uid)
        return
    if day is None:
        day = today_schedule_day()
    if day == 6:
        parts = [f"📅 {escape(p['title'])} · Воскресенье\nСегодня выходной, уроков нет."]
        text = "\n\n".join(clean_html(part) for part in parts)
    else:
        if day not in range(6):
            await send(client, uid, "День недели: от 0 (пн) до 5 (сб).")
            return
        schedule = await fetch_html(urljoin(SCHEDULE_BASE, p["filename"]))
        parts = schedule_text_parts(p["title"], schedule, day)
        text = "\n\n".join(clean_html(part) for part in parts)
    from substitutions import personal_message
    local_today = datetime.now(ZoneInfo(os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow"))).date()
    target_date = (local_today + timedelta(days=(day - local_today.weekday()) % 7)).isoformat()
    changes = personal_message(p, target_date)
    if changes:
        text += "\n\n" + changes
    command = f"/favday {p['source']} {p['filename']}" if selected else "/day"
    labels = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб")
    days = [(f"• {label}" if day == i else label, f"{command} {i}") for i, label in enumerate(labels)]
    rows = [days[:3], days[3:]]
    if selected:
        rows.append([("⬅️ Избранное", "/favorites"), ("🏠 Главное меню", "/start")])
    else:
        rows += [[("📅 Сегодня", "/today"), ("⭐ В избранное", "/save")],
                 [("🔄 Мои замены", "/subsmine"), ("🏠 Главное меню", "/start")]]
    if selected:
        text = "⭐ Избранное · Просмотр\n\n" + text
    try:
        from schedule_images import daily_images
        pages = await daily_images(p["title"], parts, changes)
        await send_photo_pages(client, uid, pages, kb(rows), p["title"])
    except Exception:
        log.exception("MAX schedule image unavailable; using text")
        await send(client, uid, "📋 Текстовая версия расписания\n\n" + text + "\n\nДругой день — кнопками ниже.", kb(rows))


async def handle(client, uid, text):
    text = (text or "").strip()
    command = text.lower().split()[0] if text else ""
    if command in ("/start", "меню"):
        await show_home(client, uid)
    elif command == "/help":
        await send(client, uid, "❔ Как пользоваться ботом\n\n"
            "1. Выбери свою группу или преподавателя.\n"
            "2. Открой «Моё расписание» и нужный день недели.\n"
            "3. В «Моих заменах» будут изменения, которые относятся к тебе.\n\n"
            "«Замены по дате» показывает все группы за опубликованный день.\n"
            "Управлять подпиской можно в разделе «Уведомления».",
            kb([[("🔎 Выбрать расписание", "/browse")], [("🏠 Главное меню", "/start")]]))
    elif command in ("/reg_student", "/reg_teacher"):
        await catalog(client, uid, "raspisan" if command=="/reg_student" else "Praspisan")
    elif command == "/register":
        await ask_registration(client, uid)
    elif command == "/browse":
        await send(client, uid, "🔎 Выбрать своё расписание\n\n"
            "Студентам — список групп. Преподавателям — список фамилий.\n"
            "Текущий выбор изменится только после нажатия на группу или имя.",
            kb([[("🎓 Группы", "/groups")], [("👨‍🏫 Преподаватели", "/teachers")],
                [("🏠 Главное меню", "/start")]]))
    elif command == "/select":
        parts = text.split()
        if len(parts) == 3 and parts[1] in SOURCES.values():
            await choose(client, uid, source=parts[1], filename=parts[2])
        else:
            await ask_registration(client, uid)
    elif command == "/catalog":
        parts = text.split()
        if len(parts) == 3 and parts[1] in SOURCES.values() and parts[2].isdigit():
            await catalog(client, uid, parts[1], int(parts[2]))
        else:
            await ask_registration(client, uid)
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
            await send(client, uid, "Сначала выбери группу или преподавателя в разделе «Выбрать расписание».")
    elif command in ("/today", "/schedule"):
        await show_schedule(client, uid)
    elif command == "/day":
        parts = text.split()
        if len(parts) != 2 or parts[1] not in [str(x) for x in range(6)]:
            await send(client, uid, "Используй /day 0 (пн) ... /day 5 (сб).")
        else:
            await show_schedule(client, uid, int(parts[1]))
    elif command == "/substitutions":
        await send(client, uid, "🔄 Замены в расписании\n\n«Мои замены» — только изменения для твоей группы или преподавателя.\n«По дате» — все опубликованные замены на выбранный день.",
                   kb([[("👤 Мои замены", "/subsmine")],
                       [("📆 Замены по дате", "/subsdates")],
                       [("🏠 Меню", "/start")]]))
    elif command == "/subsmine":
        from substitutions import personal_overview
        p = profile(uid)
        await send(client, uid, "🔄 Мои замены\n\n" + profile_label(p) + "\n\n" + personal_overview(p) if p else "Выбери группу или преподавателя, чтобы увидеть свои замены.",
                   kb([[("⬅️ Назад", "/substitutions")]]))
    elif command == "/subsdates":
        from substitutions import published_dates
        dates = published_dates()
        parts = text.split()
        page = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        pages = max(1, (len(dates) + 9) // 10)
        page = min(page, pages - 1)
        weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        rows = []
        for day in dates[page*10:page*10+10]:
            parsed = datetime.fromisoformat(day)
            rows.append([(f"📆 {parsed:%d.%m.%Y} · {weekdays[parsed.weekday()]}", f"/subsday {day}")])
        navigation = []
        if page:
            navigation.append(("← Назад", f"/subsdates {page-1}"))
        if page + 1 < pages:
            navigation.append(("Далее →", f"/subsdates {page+1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("🔄 Раздел замен", "/substitutions"), ("🏠 Главное меню", "/start")])
        prompt = f"Выбери опубликованную дату.\nСтраница {page+1} из {pages}" if dates else "Опубликованных замен пока нет. Попробуй зайти позже."
        await send(client, uid, "📆 Замены по дате\n\n" + prompt, kb(rows))
    elif command == "/subsday":
        from substitutions import published_dates, substitutions_on, format_day_changes
        parts = text.split()
        if len(parts) != 2 or parts[1] not in published_dates():
            await send(client, uid, "Выбери дату через «Замены по дате».")
            return
        await send(client, uid, format_day_changes(substitutions_on(parts[1]), parts[1]),
                   kb([[("⬅️ К датам", "/subsdates")]]))
    elif command == "/settings":
        await show_settings(client, uid)
    elif command in ("/notify", "/mute"):
        p = profile(uid)
        if not p:
            await ask_registration(client, uid)
            return
        save_user("max", uid, p["source"], p["filename"], p["title"], command == "/notify")
        await show_settings(client, uid, "✅ Настройка сохранена.")
    elif command == "/profile":
        p = profile(uid)
        if not p:
            await ask_registration(client, uid)
            return
        role = "Преподаватель" if p["source"] == "Praspisan" else "Студент"
        status = "включены 🔔" if notification_enabled(uid) else "выключены 🔕"
        await send(client, uid, f"👤 Мой профиль\n\nРоль: {role}\n{profile_label(p)}\n"
            f"Уведомления о заменах: {status}\n\nВыбор расписания можно изменить кнопкой ниже.",
            kb([[("🔎 Изменить расписание", "/register")], [("🔔 Настроить уведомления", "/settings")],
                [("🏠 Главное меню", "/start")]]))
    elif command == "/save":
        p = profile(uid)
        if not p:
            await send(client, uid, "Сначала выбери расписание.")
        else:
            with db() as conn:
                conn.execute("INSERT OR IGNORE INTO max_favorites VALUES (?,?,?,?)",
                             (uid, p["source"], p["filename"], p["title"]))
            await send(client, uid, "✅ Добавлено в избранное\n\n" + profile_label(p),
                       kb([[("⭐ Открыть избранное", "/favorites")], [("📅 Моё расписание", "/today"), ("🏠 Главное меню", "/start")]]))
    elif command == "/favorites":
        with db() as conn:
            items = conn.execute("SELECT source,filename,title FROM max_favorites WHERE uid=? ORDER BY title", (uid,)).fetchall()
        parts = text.split()
        pages = max(1, (len(items) + 9) // 10)
        page = min(int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0, pages - 1)
        rows = [[(f"⭐ {item['title']}"[:60], f"/favorite {item['source']} {item['filename']}")] for item in items[page*10:page*10+10]]
        navigation = []
        if page:
            navigation.append(("← Назад", f"/favorites {page-1}"))
        if page + 1 < pages:
            navigation.append(("Далее →", f"/favorites {page+1}"))
        if navigation:
            rows.append(navigation)
        rows += [[("📅 Моё расписание", "/today"), ("🏠 Главное меню", "/start")]]
        await send(client, uid, "⭐ Избранное\n\n" + ("Нажми на расписание, чтобы открыть его.\nТвой основной профиль останется прежним." if items else
            "Здесь пока пусто.\nОткрой своё расписание и нажми «В избранное»."), kb(rows))
    elif command in ("/favorite", "/favday"):
        parts = text.split()
        if (command == "/favorite" and len(parts) != 3) or (command == "/favday" and (len(parts) != 4 or parts[3] not in tuple(map(str, range(6))))):
            await send(client, uid, "Выбери расписание в избранном.", kb([[("⭐ Избранное", "/favorites")]]))
            return
        with db() as conn:
            item = conn.execute("SELECT source,filename,title FROM max_favorites WHERE uid=? AND source=? AND filename=?",
                                (str(uid),parts[1],parts[2])).fetchone()
        if item:
            await show_schedule(client, uid, int(parts[3]) if command == "/favday" else None, selected=dict(item))
        else:
            await send(client, uid, "Этого расписания уже нет в избранном.", kb([[("⭐ Открыть избранное", "/favorites")]]))
    elif command == "/news":
        posts = news_list()[:5]
        await send(client, uid, "📰 Новости РКТМ\n\n" + ("\n\n".join(x["title"] + "\n" + x["body"] for x in posts) if posts else "Новых публикаций пока нет."), home_buttons())
    elif text.isdigit():
        await choose(client, uid, int(text))
    else:
        await send(client, uid, "Не удалось распознать сообщение.\nВыбери раздел кнопками или открой помощь.",
                   kb([[("🏠 Главное меню", "/start"), ("❔ Помощь", "/help")]]))


async def poll():
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
                            try:
                                await send(client, uid, "Сейчас не удалось загрузить данные. Попробуй ещё раз немного позже.", back_home())
                            except Exception:
                                log.exception("Could not send MAX error screen")
                        finally:
                            EDIT_TARGET.reset(token)
                marker = data.get("marker", marker)
            except (httpx.HTTPError, ValueError):
                log.exception("MAX polling error, retrying")
                await asyncio.sleep(5)


async def run():
    from substitutions import monitor
    init_max()
    # When Telegram is configured the web service owns both platforms' notifications.
    task = asyncio.create_task(monitor()) if not os.getenv("BOT_TOKEN", "").strip() else None
    try:
        await poll()
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
