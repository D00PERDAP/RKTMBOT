"""Telegram registration, public schedule browser, favorites and admin controls."""
import hashlib
from html import escape
import hmac
import logging
import os
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, InputMediaPhoto, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from features import db, save_user, user

router = Router()
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow")


def today_schedule_day():
    return datetime.now(ZoneInfo(SCHEDULE_TIMEZONE)).weekday()
log = logging.getLogger(__name__)
TEACHER_CODE = os.getenv("TEACHER_ACCESS_CODE", "РКТМПРЕПОД2026")
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
SOURCES = {"s": "raspisan", "t": "Praspisan"}
LABELS = {"s": "🎓 Группы", "t": "👨‍🏫 Преподаватели"}


def setup():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS bot_screen_messages(chat_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_roles(uid TEXT PRIMARY KEY, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_states(uid TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approved_teachers(uid TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS bot_accounts(
            uid TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL, password_salt TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_favorites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL, source TEXT NOT NULL,
            filename TEXT NOT NULL, title TEXT NOT NULL,
            UNIQUE(uid,source,filename)
        );
        CREATE TABLE IF NOT EXISTS bot_catalog_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, filename TEXT NOT NULL, title TEXT NOT NULL,
            UNIQUE(source,filename)
        );
        CREATE TABLE IF NOT EXISTS bot_pending(
            uid TEXT PRIMARY KEY, role TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            password_salt TEXT NOT NULL DEFAULT ''
        );
        """)


def get_role(uid):
    with db() as c:
        row = c.execute("SELECT role FROM bot_roles WHERE uid=?", (str(uid),)).fetchone()
        return row["role"] if row else None


def account(uid):
    with db() as c:
        row = c.execute("SELECT display_name FROM bot_accounts WHERE uid=?", (str(uid),)).fetchone()
        return dict(row) if row else None


def state(uid):
    with db() as c:
        row = c.execute("SELECT state FROM bot_states WHERE uid=?", (str(uid),)).fetchone()
        return row["state"] if row else None


def set_state(uid, value):
    with db() as c:
        if value:
            c.execute("INSERT INTO bot_states(uid,state) VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET state=excluded.state", (str(uid),value))
        else:
            c.execute("DELETE FROM bot_states WHERE uid=?", (str(uid),))


async def show_screen(message, text, *, edit=False, **kwargs):
    """Keep one bot screen per chat, including after a process restart."""
    chat_id = message.chat.id
    if edit:
        try:
            edited = await message.edit_text(text, **kwargs)
            with db() as c:
                c.execute("INSERT INTO bot_screen_messages(chat_id,message_id) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
                          (str(chat_id), message.message_id))
            return edited
        except Exception as error:
            if "message is not modified" in str(error).lower():
                return message
            log.debug("Screen edit unavailable; replacing message", exc_info=True)
    with db() as c:
        old = c.execute("SELECT message_id FROM bot_screen_messages WHERE chat_id=?", (str(chat_id),)).fetchone()
    sent = await message.answer(text, **kwargs)
    if old and old["message_id"] != sent.message_id:
        try:
            await message.bot.delete_message(chat_id, old["message_id"])
        except Exception:
            log.debug("Old screen could not be deleted", exc_info=True)
    # Telegram reply keyboard belongs to a chat message. Deleting that message
    # later can make the persistent bottom keyboard disappear in some clients.
    # Keep keyboard-bearing messages outside the replaceable screen lifecycle.
    if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
        with db() as c:
            c.execute("DELETE FROM bot_screen_messages WHERE chat_id=?", (str(chat_id),))
        return sent
    with db() as c:
        c.execute("INSERT INTO bot_screen_messages(chat_id,message_id) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
                  (str(chat_id), sent.message_id))
    return sent


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=data) for title,data in row]
        for row in rows
    ])


def keyboard(uid):
    labels = [
        ["📅 Моё расписание"],
        ["🔄 Мои замены", "📆 Замены по дате"],
        ["🔎 Все расписания", "⭐ Избранное"],
        ["👤 Профиль", "🔔 Уведомления"],
        ["📰 Новости", "❔ Помощь"],
    ]
    if str(uid) in ADMIN_IDS:
        labels.append(["🔐 Админ-панель"])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=text) for text in row] for row in labels],
                               resize_keyboard=True, is_persistent=True)


def home_button():
    return [("🏠 Главное меню", "ui:home")]


def catalog_keyboard():
    return buttons([[("🎓 Расписания групп", "cat:s:0")],
                    [("👨‍🏫 Расписания преподавателей", "cat:t:0")], home_button()])


def profile_line(p):
    return ("Преподаватель: " if p["source"] == "Praspisan" else "Группа: ") + p["group_name"].rstrip(" ,")


async def show_home(message, uid=None):
    uid = str(uid if uid is not None else message.from_user.id)
    set_state(uid, None)
    p = user("telegram", uid)
    if not p or not p["filename"]:
        await ask_role(message)
        return
    status = "включены 🔔" if p["notifications"] else "выключены 🔕"
    await show_screen(message, f"📚 РКТМ · Главное меню\n\n{profile_line(p)}\n"
        f"Уведомления о заменах: {status}\n\nВыбери раздел кнопками внизу.", reply_markup=keyboard(uid))


async def show_settings(message, uid, edit=False):
    p = user("telegram", uid)
    if not p:
        await ask_role(message)
        return
    enabled = bool(p["notifications"])
    toggle = ("🔕 Выключить уведомления", "ui:notify:off") if enabled else ("🔔 Включить уведомления", "ui:notify:on")
    await show_screen(message, "🔔 Уведомления\n\n" + ("Сейчас включены." if enabled else "Сейчас выключены.")
        + "\n\nБот сообщает об изменениях расписания и заменах для твоего выбора.\n"
        "Расписание и замены доступны в меню при любом режиме уведомлений.",
        edit=edit, reply_markup=buttons([[toggle], [("👤 Профиль", "ui:profile")], home_button()]))


async def show_help(message, edit=False):
    await show_screen(message, "❔ Как пользоваться ботом\n\n"
        "1. Выбери свою группу или преподавателя.\n"
        "2. Открой «Моё расписание» — бот пришлёт карточки с занятиями.\n"
        "3. Переключай дни кнопками под расписанием.\n\n"
        "«Мои замены» показывает изменения для тебя, «Замены по дате» — все группы за день.\n"
        "В профиле можно изменить основной выбор, в уведомлениях — включить или выключить подписку.",
        edit=edit, reply_markup=buttons([[("🔎 Все расписания", "browse")], home_button()]))


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return digest.hex(), salt.hex()


async def ask_role(message):
    await show_screen(
        message, "📚 РКТМ · Расписание и замены\n\nВыбери, чьё расписание сохранить: своей группы или преподавателя.",
        reply_markup=buttons([[("🎓 Студент", "onboard:s"), ("👨‍🏫 Преподаватель", "onboard:t")]])
    )


@router.message(CommandStart())
async def start(message: Message):
    await show_home(message)


@router.callback_query(F.data.startswith(("onboard:", "onboardcat:", "onboardpick:", "onboardid:")))
async def onboarding(call: CallbackQuery):
    await call.answer()
    uid = str(call.from_user.id)
    data = call.data or ""
    if data.startswith("onboard:"):
        role = data.split(":")[1]
        if role not in SOURCES:
            return
        set_state(uid, "onboard:" + role)
        await onboarding_catalog(call.message, role, 0)
    elif data.startswith("onboardcat:"):
        _, role, page = data.split(":")
        if role in SOURCES and page.isdigit():
            await onboarding_catalog(call.message, role, int(page))
    elif data.startswith(("onboardpick:", "onboardid:")):
        selection = data.split(":")
        if len(selection) < 3 or selection[1] not in SOURCES:
            return
        role = selection[1]
        if state(uid) != "onboard:" + role:
            await ask_role(call.message)
            return
        try:
            if selection[0] == "onboardid":
                if len(selection) != 3 or not selection[2].isdigit():
                    return
                with db() as conn:
                    stored = conn.execute("SELECT * FROM bot_catalog_items WHERE id=? AND source=?",
                                          (int(selection[2]), SOURCES[role])).fetchone()
                if not stored:
                    raise ValueError("Selection missing")
                item = {"file":stored["filename"], "name":stored["title"]}
            else:
                _, _, page, index = selection
                if not page.isdigit() or not index.isdigit():
                    return
                item = await resolve_item(role, int(page), int(index))
        except (IndexError, KeyError, ValueError):
            await call.message.answer("Список изменился. Открой его заново и выбери расписание.")
            return
        # Existing Telegram account tables remain compatible with the old profile/menu.
        with db() as c:
            c.execute("INSERT INTO bot_roles(uid,role) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
                      (uid, "teacher" if role=="t" else "student"))
            if not account(uid):
                digest, salt = hash_password(secrets.token_urlsafe(32))
                c.execute("INSERT INTO bot_accounts(uid,display_name,password_hash,password_salt) VALUES(?,?,?,?)",
                          (uid, item["name"], digest, salt))
        save_user("telegram", uid, SOURCES[role], item["file"], item["name"], (user("telegram", uid) or {}).get("notifications", True))
        set_state(uid, None)
        await call.message.answer("✅ Расписание сохранено. Теперь можно открыть занятия и свои замены.", reply_markup=keyboard(uid))
        await schedule_image(call.message, SOURCES[role], item["file"], item["name"], edit=True)


def catalog_item_ids(source, items):
    ids = []
    with db() as conn:
        for item in items:
            conn.execute("INSERT INTO bot_catalog_items(source,filename,title) VALUES(?,?,?) ON CONFLICT(source,filename) DO UPDATE SET title=excluded.title", (source,item["file"],item["name"]))
            ids.append(conn.execute("SELECT id FROM bot_catalog_items WHERE source=? AND filename=?", (source,item["file"])).fetchone()[0])
    return ids


async def onboarding_catalog(message, role, page=0):
    from app import fetch_html, SCHEDULE_URLS, GroupParser
    parser = GroupParser()
    parser.feed(await fetch_html(SCHEDULE_URLS[SOURCES[role]]))
    items = parser.groups
    if not items:
        await show_screen(message, "Список пока пуст. Попробуй позже.")
        return
    page = min(max(0,page), (len(items)-1)//8)
    visible = items[page*8:page*8+8]
    ids = catalog_item_ids(SOURCES[role], visible)
    rows = [[(item["name"][:48], f"onboardid:{role}:{item_id}")] for item,item_id in zip(visible,ids)]
    nav = []
    if page:
        nav.append(("← Назад", f"onboardcat:{role}:{page-1}"))
    if (page+1)*8 < len(items):
        nav.append(("Далее →", f"onboardcat:{role}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("↩️ Изменить роль", "onboard:s" if role=="t" else "onboard:t")])
    rows.append(home_button())
    await show_screen(message,
                      ("🎓 Выбери свою группу" if role=="s" else "👨‍🏫 Выбери себя из списка преподавателей")
                      + f" · {page+1}/{(len(items)+7)//8}",
                      reply_markup=buttons(rows), edit=True)


@router.message(Command("register"))
async def register(message: Message):
    await ask_role(message)


async def show_catalog(message, source_key, page=0):
    from app import fetch_html, SCHEDULE_URLS, GroupParser
    if source_key not in SOURCES:
        return
    try:
        html = await fetch_html(SCHEDULE_URLS[SOURCES[source_key]])
        parser = GroupParser()
        parser.feed(html)
        items = parser.groups
        if not items:
            await message.answer("Список пока пуст. Попробуй позже.")
            return
        page = max(0, min(page, (len(items)-1)//8))
        visible = items[page*8:page*8+8]
        ids = catalog_item_ids(SOURCES[source_key], visible)
        rows = [[(item["name"][:48], f"viewid:{item_id}")] for item,item_id in zip(visible,ids)]
        nav = []
        if page:
            nav.append(("← Назад", "cat:"+source_key+":"+str(page-1)))
        if (page+1)*8 < len(items):
            nav.append(("Далее →", "cat:"+source_key+":"+str(page+1)))
        if nav:
            rows.append(nav)
        rows.append([("↩️ Все категории", "browse")])
        await show_screen(message,LABELS[source_key]+" · "+str(page+1)+"/"+str((len(items)+7)//8),
                             reply_markup=buttons(rows))
    except Exception:
        log.exception("Cannot fetch schedule catalog")
        await message.answer("Не удалось загрузить список. Попробуй позже.")


async def resolve_item(source_key, page, index):
    from app import fetch_html, SCHEDULE_URLS, GroupParser
    if source_key not in SOURCES or not (0 <= index < 8) or page < 0:
        raise ValueError("Invalid selection")
    html = await fetch_html(SCHEDULE_URLS[SOURCES[source_key]])
    parser = GroupParser()
    parser.feed(html)
    return parser.groups[page*8+index]


def day_keyboard(item_id, selected):
    from schedule_browser import DAY_SHORT
    rows = []
    for start in (0, 3):
        rows.append([(("• " if i == selected else "") + DAY_SHORT[i], f"day:{item_id}:{i}")
                     for i in range(start, start+3)])
    rows += [[("📅 Сегодня", f"today:{item_id}"), ("⭐ В избранное", f"addid:{item_id}")],
             [("🔄 Мои замены", "subs:mine"), ("🔎 Все расписания", "browse")], home_button()]
    return buttons(rows)


def schedule_text_parts(title, html, day=None):
    """Readable per-day schedule from the college's 12-lesson timetable."""
    from bs4 import BeautifulSoup
    from schedule_browser import DAYS, table_rows
    import re

    day_re = re.compile(r"^\s*(Пнд|Втр|Срд|Чтв|Птн|Сбт|Вск|Пн|Вт|Ср|Чт|Пт|Сб|Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье)\s*[,.: -]*(.*)$", re.I)
    day_ids = {"пнд":0,"втр":1,"срд":2,"чтв":3,"птн":4,"сбт":5,"вск":6,
               "пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,
               "понедельник":0,"вторник":1,"среда":2,"четверг":3,
               "пятница":4,"суббота":5,"воскресенье":6}
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    found = {i:[] for i in range(7)}
    times = {}
    for table in soup.find_all("table"):
        if table.find_parent("table"):
            continue
        rows = table_rows(table)
        active_day = None
        active_entry = None
        for row in rows:
            if not row:
                continue
            first = row[0].strip()
            if first.lower().startswith("время"):
                for i,value in enumerate(row[1:],1):
                    if re.search(r"\d{1,2}:\d{2}",value):
                        times[i] = value
                active_day = None
                active_entry = None
                continue
            match = day_re.match(first)
            if match:
                weekday = day_ids[match.group(1).lower()]
                date_label = match.group(2).strip()
                if active_day != weekday or active_entry is None or (date_label and date_label != active_entry[0]):
                    active_entry = (date_label, [])
                    found[weekday].append(active_entry)
                active_day = weekday
                cells = row[1:]
            elif active_day is not None and (not first or first in {"_", "-", "—"} or first.isdigit()):
                # Follow-on rows may contain additional lessons under a merged weekday cell.
                cells = row[1:] if not first else row
            else:
                active_day = None
                active_entry = None
                continue
            if active_entry is None:
                continue
            for slot, value in enumerate(cells, 1):
                value = " ".join(value.split()).strip()
                if not value or value in {"_", "-", "—", "–"}:
                    continue
                lesson = (slot, times.get(slot, ""), value)
                # Ignore repeats introduced by colspan/rowspan, but preserve
                # different lessons and additional rows for the same weekday.
                if not active_entry[1] or active_entry[1][-1] != lesson:
                    active_entry[1].append(lesson)
    if day is None:
        selected = list(range(6))
    else:
        selected = [day]
    chunks = []
    for weekday in selected:
        entries = found.get(weekday,[])
        heading = "📅 <b>" + escape(title) + " · " + DAYS[weekday] + "</b>"
        if not entries:
            chunks.append(heading + "\nРасписание на этот день не найдено в таблице.")
            continue
        for date_label, lessons in entries:
            text = heading
            if date_label:
                text += "\n📆 " + escape(date_label)
            if not lessons:
                text += "\nПар не указано."
            for slot, time_label, subject in lessons:
                line = "\n\n<b>" + str(slot) + "-й урок"
                if time_label:
                    line += " · " + escape(time_label)
                line += "</b>\n" + escape(subject)
                if len(text) + len(line) > 3500:
                    chunks.append(text)
                    text = heading + "\n(продолжение)"
                text += line
            chunks.append(text)
    return chunks or ["Расписание отсутствует."]


async def show_photo_pages(message, pages, markup, title, edit=False):
    """Photo limits are enforced by the renderer; navigation belongs to the last page."""
    with db() as conn:
        old = conn.execute("SELECT message_id FROM bot_screen_messages WHERE chat_id=?", (str(message.chat.id),)).fetchone()
    last = None
    for index, image in enumerate(pages):
        caption = f"📅 {title.rstrip(' ,')} · {index+1}/{len(pages)}"[:900]
        kwargs = {"reply_markup": markup} if index == len(pages)-1 else {}
        photo = BufferedInputFile(image, filename=f"schedule-{index+1}.png")
        if edit and index == 0:
            try:
                last = await message.edit_media(media=InputMediaPhoto(media=photo, caption=caption), **kwargs)
                continue
            except Exception as error:
                if "message is not modified" in str(error).lower():
                    last = message
                    continue
                log.debug("Cannot replace schedule photo; sending a new one", exc_info=True)
        last = await message.answer_photo(photo, caption=caption, **kwargs)
        if index == 0 and old:
            try:
                await message.bot.delete_message(message.chat.id, old["message_id"])
            except Exception:
                log.debug("Cannot remove old schedule screen", exc_info=True)
    with db() as conn:
        conn.execute("INSERT INTO bot_screen_messages VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
                     (str(message.chat.id), last.message_id))


async def schedule_image(message, source, filename, title, day=None, item_id=None, edit=False):
    from app import fetch_html, SCHEDULE_BASE
    from schedule_images import daily_images, plain
    from substitutions import personal_message, message_parts
    from datetime import timedelta
    if source not in SOURCES.values() or not re.fullmatch(r"[^/\\?#]+\.html?", filename, re.I):
        await message.answer("Не удалось открыть расписание. Выбери его заново в каталоге.", reply_markup=catalog_keyboard())
        return
    try:
        if item_id is None:
            with db() as conn:
                conn.execute("INSERT INTO bot_catalog_items(source,filename,title) VALUES(?,?,?) ON CONFLICT(source,filename) DO UPDATE SET title=excluded.title", (source,filename,title))
                item_id = conn.execute("SELECT id FROM bot_catalog_items WHERE source=? AND filename=?", (source,filename)).fetchone()[0]
        day = today_schedule_day() if day is None else day
        if day not in range(7):
            raise ValueError("Invalid weekday")
        if day == 6:
            parts = ["📅 " + escape(title) + " · Воскресенье\nСегодня выходной, уроков нет."]
        else:
            html = await fetch_html(urljoin(SCHEDULE_BASE, filename))
            parts = schedule_text_parts(title, html, day)
        today = datetime.now(ZoneInfo(SCHEDULE_TIMEZONE)).date()
        target = (today + timedelta(days=(day-today.weekday()) % 7)).isoformat()
        changes = personal_message({"source":source,"group_name":title}, target)
        markup = day_keyboard(item_id, day)
        try:
            pages = await daily_images(title, parts, changes)
            await show_photo_pages(message, pages, markup, title, edit=edit)
            return
        except Exception:
            log.exception("Schedule photo unavailable; falling back to complete text")
        content = "\n\n".join(plain(part) for part in parts)
        if changes:
            content += "\n\n" + changes
        chunks = list(message_parts("📋 Текстовая версия расписания\n\n" + content))
        for index, chunk in enumerate(chunks):
            if index == 0:
                await show_screen(message, chunk, edit=edit, reply_markup=markup if len(chunks)==1 else None)
            else:
                await message.answer(chunk, reply_markup=markup if index==len(chunks)-1 else None)
    except Exception:
        log.exception("Cannot load daily schedule")
        await message.answer("Сейчас не удалось загрузить расписание. Попробуй немного позже.", reply_markup=buttons([home_button()]))


async def show_item(message, uid, source, filename, title):
    with db() as c:
        c.execute("INSERT INTO bot_catalog_items(source,filename,title) VALUES (?,?,?) ON CONFLICT(source,filename) DO UPDATE SET title=excluded.title",
                  (source,filename,title))
        item=c.execute("SELECT id FROM bot_catalog_items WHERE source=? AND filename=?",(source,filename)).fetchone()
        fav=c.execute("SELECT id FROM bot_favorites WHERE uid=? AND source=? AND filename=?",
                      (uid,source,filename)).fetchone()
    item_id=str(item["id"])
    favorite_button=("❌ Убрать из избранного","unfav:"+str(fav["id"])) if fav else ("⭐ В избранное","addid:"+item_id)
    await show_screen(message,"📖 "+title+"\nВыбери действие:", reply_markup=buttons([
        [("📅 Открыть расписание","imgid:"+item_id)],
        [favorite_button],
        [("📌 Сделать моим","mineid:"+item_id)], home_button(),
    ]))


async def show_favorites(message, uid, page=0):
    with db() as conn:
        items = conn.execute("SELECT id,title FROM bot_favorites WHERE uid=? ORDER BY id DESC", (uid,)).fetchall()
    pages = max(1, (len(items)+9)//10)
    page = min(max(0,page), pages-1)
    rows = [[(item["title"][:48], f"favorite:{item['id']}")] for item in items[page*10:page*10+10]]
    nav = []
    if page:
        nav.append(("← Назад", f"ui:favorites:{page-1}"))
    if page+1 < pages:
        nav.append(("Далее →", f"ui:favorites:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [[("🔎 Все расписания", "browse")], home_button()]
    text = f"Выбери расписание для просмотра.\nСтраница {page+1} из {pages}" if items else "Здесь пока пусто. Открой расписание в каталоге и нажми «В избранное»."
    await show_screen(message, "⭐ Избранное\n\n"+text, reply_markup=buttons(rows))


@router.callback_query(F.data.startswith(("reg:", "cat:", "view:", "viewid:", "browse", "addid:", "unfav:", "favorite:", "imgid:", "mineid:", "day:", "all:", "today:")))
async def callback(call: CallbackQuery):
    await call.answer()
    uid = str(call.from_user.id)
    data = call.data or ""
    if data.startswith("reg:"):
        code = data[4:]
        if code not in SOURCES:
            return
        with db() as c:
            c.execute("INSERT INTO bot_pending(uid,role) VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role,display_name='',password_hash='',password_salt=''", (uid,"teacher" if code=="t" else "student"))
        set_state(uid, "reg_name")
        await call.message.answer("✍️ Напиши своё имя (от 2 до 60 символов).")
        return
    if not account(uid) and not user("telegram", uid):
        await call.message.answer("Сначала зарегистрируйся: /start")
        return
    if data == "browse":
        await show_screen(call.message,"🔎 Чьё расписание открыть?", reply_markup=catalog_keyboard(), edit=True)
    elif data.startswith("cat:"):
        parts=data.split(":")
        if len(parts)==3 and parts[2].isdigit():
            await show_catalog(call.message,parts[1],int(parts[2]))
    elif data.startswith("viewid:"):
        value = data.partition(":")[2]
        if not value.isdigit():
            return
        with db() as conn:
            item = conn.execute("SELECT * FROM bot_catalog_items WHERE id=?", (int(value),)).fetchone()
        if item:
            await show_item(call.message,uid,item["source"],item["filename"],item["title"])
        else:
            await show_screen(call.message, "Этот выбор устарел. Открой каталог ещё раз.", reply_markup=catalog_keyboard())
    elif data.startswith("view:"):
        parts=data.split(":")
        if len(parts)==4 and parts[2].isdigit() and parts[3].isdigit():
            try:
                item=await resolve_item(parts[1],int(parts[2]),int(parts[3]))
                await show_item(call.message,uid,SOURCES[parts[1]],item["file"],item["name"])
            except (IndexError,KeyError,ValueError):
                await call.message.answer("Выбор устарел. Открой список заново.")
    elif data.startswith(("addid:", "imgid:", "mineid:")):
        action,_,id_text=data.partition(":")
        if not id_text.isdigit():
            return
        with db() as c:
            item=c.execute("SELECT * FROM bot_catalog_items WHERE id=?",(int(id_text),)).fetchone()
        if not item:
            await call.message.answer("Открой расписание в каталоге заново.")
            return
        source,filename,title=item["source"],item["filename"],item["title"]
        if action=="addid":
            with db() as c:
                c.execute("INSERT OR IGNORE INTO bot_favorites(uid,source,filename,title) VALUES (?,?,?,?)",
                          (uid,source,filename,title))
            await call.message.answer("⭐ Добавлено: "+title)
        elif action=="mineid":
            save_user("telegram",uid,source,filename,title,(user("telegram",uid) or {}).get("notifications", True))
            with db() as conn:
                conn.execute("INSERT INTO bot_roles VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
                             (uid, "teacher" if source == "Praspisan" else "student"))
            await show_screen(call.message,"✅ Основное расписание сохранено\n\n"+title,reply_markup=keyboard(uid))
        else:
            await schedule_image(call.message,source,filename,title,edit=True)
    elif data.startswith(("day:", "all:", "today:")):
        parts=data.split(":")
        if len(parts)<2 or not parts[1].isdigit():
            return
        day=None
        if parts[0]=="day":
            if len(parts)!=3 or not parts[2].isdigit() or int(parts[2])>5:
                return
            day=int(parts[2])
        with db() as c:
            item=c.execute("SELECT * FROM bot_catalog_items WHERE id=?",(int(parts[1]),)).fetchone()
        if item:
            await schedule_image(call.message,item["source"],item["filename"],item["title"],day,item["id"],edit=True)
    elif data.startswith(("favorite:", "unfav:")):
        command,_,id_text=data.partition(":")
        if not id_text.isdigit():
            return
        with db() as c:
            row=c.execute("SELECT * FROM bot_favorites WHERE id=? AND uid=?",(int(id_text),uid)).fetchone()
            if row and command=="unfav":
                c.execute("DELETE FROM bot_favorites WHERE id=? AND uid=?",(int(id_text),uid))
        if not row:
            await call.message.answer("Элемент уже удалён.")
        elif command=="unfav":
            await show_screen(call.message,"Удалено из избранного.",edit=True)
        else:
            await show_item(call.message,uid,row["source"],row["filename"],row["title"])


async def complete_registration(message,uid):
    with db() as c:
        pending=c.execute("SELECT * FROM bot_pending WHERE uid=?",(uid,)).fetchone()
        if not pending or not pending["display_name"] or not pending["password_hash"]:
            await message.answer("Регистрация прервана. Начни с /start.")
            return
        c.execute("INSERT INTO bot_accounts(uid,display_name,password_hash,password_salt) VALUES (?,?,?,?) ON CONFLICT(uid) DO UPDATE SET display_name=excluded.display_name,password_hash=excluded.password_hash,password_salt=excluded.password_salt",
                  (uid,pending["display_name"],pending["password_hash"],pending["password_salt"]))
        c.execute("INSERT INTO bot_roles(uid,role) VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",(uid,pending["role"]))
        c.execute("DELETE FROM bot_pending WHERE uid=?",(uid,))
    set_state(uid,None)
    await message.answer("✅ Готово! Теперь можно смотреть расписания групп и преподавателей, сохранять избранное и выбирать личное.",reply_markup=keyboard(uid))


@router.message(Command("profile"))
async def profile(message: Message, uid=None, edit=False):
    uid = str(uid if uid is not None else message.from_user.id)
    p = user("telegram", uid)
    if not p:
        await ask_role(message)
        return
    role = "Преподаватель" if p["source"] == "Praspisan" else "Студент"
    status = "включены 🔔" if p["notifications"] else "выключены 🔕"
    await show_screen(message, f"👤 Мой профиль\n\nРоль: {role}\n{profile_line(p)}\nУведомления: {status}",
        edit=edit, reply_markup=buttons([[("🔎 Изменить основное расписание", "ui:register")],
            [("🔔 Настроить уведомления", "ui:settings")], [("⭐ Избранное", "ui:favorites")], home_button()]))


@router.message(Command("favorites"))
async def favorites(message: Message):
    uid=str(message.from_user.id)
    if not account(uid) and not user("telegram", uid):
        await ask_role(message)
    else:
        await show_favorites(message,uid)


@router.message(Command("schedule"))
async def personal(message: Message):
    uid=str(message.from_user.id)
    if not account(uid) and not user("telegram", uid):
        await ask_role(message)
        return
    p=user("telegram",uid)
    if not p or not p["filename"]:
        await show_screen(message,"Сначала выбери личное расписание в разделе «🔎 Все расписания».",reply_markup=catalog_keyboard())
        return
    await schedule_image(message,p["source"],p["filename"],p["group_name"])


def substitution_menu():
    return buttons([[("👤 Мои замены", "subs:mine")],
                    [("📆 Замены по дате", "subs:dates")],
                    [("🏠 Главное меню", "ui:home")]])


def substitution_dates(page=0):
    from substitutions import published_dates, display_date
    dates = published_dates()
    pages = max(1, (len(dates)+9)//10)
    page = min(max(page, 0), pages-1)
    rows = [[(f"📆 {display_date(day)}", f"subs:day:{day}")] for day in dates[page*10:page*10+10]]
    nav = []
    if page:
        nav.append(("← Назад", f"subs:dates:{page-1}"))
    if page+1 < pages:
        nav.append(("Далее →", f"subs:dates:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [[("🔄 Раздел замен", "subs:menu")], home_button()]
    return buttons(rows)


async def substitution_callback_content(message, uid, action, edit=False, page=0):
    from substitutions import personal_overview, published_dates, message_parts
    if action == "dates":
        text = "📆 Замены по дате\n\n" + ("Выбери опубликованный день. Здесь показаны замены всех групп." if published_dates() else "Опубликованных замен пока нет. Попробуй зайти позже.")
        await show_screen(message, text, edit=edit, reply_markup=substitution_dates(page))
    else:
        p = user("telegram", uid)
        content = "🔄 Мои замены\n\n" + profile_line(p) + "\n\n" + personal_overview(p) if p else "Выбери группу или преподавателя через /start, чтобы увидеть свои замены."
        chunks = list(message_parts(content))
        for index, chunk in enumerate(chunks):
            markup = substitution_menu() if index == len(chunks)-1 else None
            if index == 0:
                await show_screen(message, chunk, edit=edit, reply_markup=markup)
            else:
                await message.answer(chunk, reply_markup=markup)


@router.callback_query(F.data.startswith("subs:"))
async def substitution_callback(call: CallbackQuery):
    from substitutions import substitutions_on, format_day_changes, published_dates, message_parts
    await call.answer()
    uid = str(call.from_user.id)
    action = call.data
    if action == "subs:home":
        await show_home(call.message, uid)
    elif action == "subs:menu":
        await show_screen(call.message, "🔄 Замены\n\n«Мои замены» — изменения для тебя.\n«По дате» — все группы за опубликованный день.", edit=True, reply_markup=substitution_menu())
    elif action == "subs:mine":
        await substitution_callback_content(call.message, uid, "mine", edit=True)
    elif action == "subs:dates" or action.startswith("subs:dates:"):
        value = action.removeprefix("subs:dates:")
        await substitution_callback_content(call.message, uid, "dates", edit=True, page=int(value) if value.isdigit() else 0)
    elif action.startswith("subs:day:"):
        day = action.removeprefix("subs:day:")
        if day not in published_dates():
            await substitution_callback_content(call.message, uid, "dates", edit=True)
            return
        chunks = list(message_parts(format_day_changes(substitutions_on(day), day)))
        for index, chunk in enumerate(chunks):
            markup = substitution_dates() if index == len(chunks)-1 else None
            if index == 0:
                await show_screen(call.message, chunk, edit=True, reply_markup=markup)
            else:
                await call.message.answer(chunk, reply_markup=markup)


@router.message(Command("substitutions"))
async def personal_substitutions(message: Message):
    await show_screen(message, "🔄 Замены — выбери раздел:", reply_markup=substitution_menu())


@router.message(Command("id"))
async def telegram_id(message: Message):
    await message.answer("Telegram ID: "+str(message.from_user.id))


@router.message(Command("admin"))
async def admin(message: Message):
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Нет доступа.")
        return
    await show_screen(message,"🔐 Админ-панель\n/addteacher ID\n/removeteacher ID\n/teachers")


@router.message(Command("addteacher","removeteacher","teachers"))
async def admin_teachers(message: Message):
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Нет доступа.")
        return
    parts=(message.text or "").split()
    cmd=parts[0].split("@")[0]
    with db() as c:
        if cmd=="/teachers":
            rows=c.execute("SELECT uid FROM approved_teachers ORDER BY uid LIMIT 100").fetchall()
            await message.answer("Преподаватели:\n"+("\n".join(x["uid"] for x in rows) or "Пока нет"))
            return
        if len(parts)!=2 or not parts[1].isdigit():
            await message.answer("Укажи числовой Telegram ID.")
            return
        target=parts[1]
        if cmd=="/addteacher":
            c.execute("INSERT OR IGNORE INTO approved_teachers(uid) VALUES (?)",(target,))
        else:
            c.execute("DELETE FROM approved_teachers WHERE uid=?",(target,))
    await message.answer("Готово.")


@router.callback_query(F.data.startswith("ui:"))
async def interface_callback(call: CallbackQuery):
    await call.answer()
    uid = str(call.from_user.id)
    if call.data == "ui:home":
        await show_home(call.message, uid)
    elif call.data == "ui:profile":
        await profile(call.message, uid, edit=True)
    elif call.data == "ui:settings":
        await show_settings(call.message, uid, edit=True)
    elif call.data == "ui:register":
        await ask_role(call.message)
    elif call.data == "ui:favorites" or call.data.startswith("ui:favorites:"):
        value = call.data.removeprefix("ui:favorites:")
        await show_favorites(call.message, uid, int(value) if value.isdigit() else 0)
    elif call.data in ("ui:notify:on", "ui:notify:off"):
        with db() as conn:
            conn.execute("UPDATE users SET notifications=? WHERE platform='telegram' AND user_id=?",
                         (int(call.data == "ui:notify:on"), uid))
        await show_settings(call.message, uid, edit=True)


@router.message(Command("help"))
async def help_command(message: Message):
    await show_help(message)


@router.message(Command("notify", "mute"))
async def notification_setting(message: Message):
    uid = str(message.from_user.id)
    p = user("telegram", uid)
    if not p:
        await message.answer("Сначала зарегистрируйся через /start.")
        return
    enabled = message.text.split()[0].split("@")[0] == "/notify"
    with db() as conn:
        conn.execute("UPDATE users SET notifications=? WHERE platform='telegram' AND user_id=?",
                     (int(enabled), uid))
    await show_settings(message, uid)


@router.message(F.text)
async def text_handler(message: Message):
    uid=str(message.from_user.id)
    current=state(uid)
    text=(message.text or "").strip()
    if current=="reg_name":
        if not 2<=len(text)<=60 or text.startswith("/"):
            await message.answer("Имя должно содержать от 2 до 60 символов.")
            return
        with db() as c:
            c.execute("UPDATE bot_pending SET display_name=? WHERE uid=?",(text,uid))
        set_state(uid,"reg_password")
        await message.answer("🔒 Придумай пароль (минимум 8 символов).\nНе используй пароль от других сервисов. Сообщение с паролем будет удалено, если Telegram разрешит.")
        return
    if current=="reg_password":
        try:
            await message.delete()
        except Exception:
            pass
        if len(text)<8 or len(text)>128:
            await message.answer("Пароль должен быть от 8 до 128 символов. Попробуй ещё раз.")
            return
        digest,salt=hash_password(text)
        with db() as c:
            c.execute("UPDATE bot_pending SET password_hash=?,password_salt=? WHERE uid=?",(digest,salt,uid))
            row=c.execute("SELECT role FROM bot_pending WHERE uid=?",(uid,)).fetchone()
        if row and row["role"]=="teacher" and uid not in ADMIN_IDS:
            with db() as c:
                approved=c.execute("SELECT 1 FROM approved_teachers WHERE uid=?",(uid,)).fetchone()
            if not approved:
                set_state(uid,"reg_teacher_code")
                await message.answer("👨‍🏫 Введи код доступа преподавателя.")
                return
        await complete_registration(message,uid)
        return
    if current=="reg_teacher_code":
        try:
            await message.delete()
        except Exception:
            pass
        if hmac.compare_digest(text.encode(),TEACHER_CODE.encode()):
            await complete_registration(message,uid)
        else:
            await message.answer("Неверный код. Попробуй ещё раз или /register.")
        return
    if text in ("📅 Моё расписание", "📅 Расписание", "🔎 Все расписания", "📚 Выбрать расписание", "⭐ Избранное", "👤 Профиль", "🔐 Админ-панель", "🔔 Уведомления", "📰 Новости", "🔄 Замены", "🔄 Мои замены", "📆 Замены по дате", "❔ Помощь"):
        try:
            await message.delete()
        except Exception:
            pass
    if text in ("📅 Моё расписание","📅 Расписание"):
        await personal(message)
    elif text in ("🔎 Все расписания","📚 Выбрать расписание"):
        if not account(uid) and not user("telegram", uid):
            await ask_role(message)
        else:
            await show_screen(message,"🔎 Выбери категорию:",reply_markup=catalog_keyboard())
    elif text=="⭐ Избранное":
        await favorites(message)
    elif text=="👤 Профиль":
        await profile(message)
    elif text=="🔐 Админ-панель":
        await admin(message)
    elif text=="🔄 Замены":
        await personal_substitutions(message)
    elif text=="🔔 Уведомления":
        await show_settings(message, uid)
    elif text=="❔ Помощь":
        await show_help(message)
    elif text=="🔄 Мои замены":
        await substitution_callback_content(message, uid, "mine")
    elif text=="📆 Замены по дате":
        await substitution_callback_content(message, uid, "dates")
    elif text=="📰 Новости":
        from features import news_list
        from substitutions import message_parts
        items = news_list()[:3]
        content = "📰 Новости РКТМ\n\n" + ("\n\n".join(item["title"]+"\n"+item["body"] for item in items) if items else "Новых публикаций пока нет.")
        chunks = list(message_parts(content))
        for index, chunk in enumerate(chunks):
            markup = buttons([home_button()]) if index == len(chunks)-1 else None
            if index == 0:
                await show_screen(message, chunk, reply_markup=markup)
            else:
                await message.answer(chunk, reply_markup=markup)
    else:
        await message.answer("Выбери действие из меню.",reply_markup=keyboard(uid) if account(uid) else None)
