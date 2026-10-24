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
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
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
        except Exception:
            log.debug("Screen edit unavailable; replacing message", exc_info=True)
    with db() as c:
        old = c.execute("SELECT message_id FROM bot_screen_messages WHERE chat_id=?", (str(chat_id),)).fetchone()
    if old:
        try:
            await message.bot.delete_message(chat_id, old["message_id"])
        except Exception:
            log.debug("Old screen could not be deleted", exc_info=True)
    sent = await message.answer(text, **kwargs)
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
    rows = [
        [KeyboardButton(text="📅 Моё расписание"), KeyboardButton(text="🔎 Все расписания")],
        [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="📰 Новости")],
    ]
    if str(uid) in ADMIN_IDS:
        rows.append([KeyboardButton(text="🔐 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def catalog_keyboard():
    return buttons([[("🎓 Расписания групп", "cat:s:0")], [("👨‍🏫 Расписания преподавателей", "cat:t:0")]])


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return digest.hex(), salt.hex()


async def ask_role(message):
    await show_screen(
        message, "👋 Добро пожаловать в РКТМ!\\n\\nВыбери, кто ты:",
        reply_markup=buttons([[("🎓 Студент", "onboard:s"), ("👨‍🏫 Преподаватель", "onboard:t")]])
    )


@router.message(CommandStart())
async def start(message: Message):
    uid = str(message.from_user.id)
    p = user("telegram", uid)
    if not p or not p["filename"]:
        await ask_role(message)
    else:
        await schedule_image(message, p["source"], p["filename"], p["group_name"])
        await message.answer("🏠 Меню Telegram доступно снизу 👇", reply_markup=keyboard(uid))


@router.callback_query(F.data.startswith(("onboard:", "onboardcat:", "onboardpick:")))
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
    elif data.startswith("onboardpick:"):
        _, role, page, index = data.split(":")
        if role not in SOURCES or not page.isdigit() or not index.isdigit():
            return
        if state(uid) != "onboard:" + role:
            await ask_role(call.message)
            return
        try:
            item = await resolve_item(role, int(page), int(index))
        except (IndexError, KeyError, ValueError):
            await call.message.answer("Список изменился. Выбери заново.")
            return
        # Existing Telegram account tables remain compatible with the old profile/menu.
        with db() as c:
            c.execute("INSERT INTO bot_roles(uid,role) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",
                      (uid, "teacher" if role=="t" else "student"))
            if not account(uid):
                digest, salt = hash_password(secrets.token_urlsafe(32))
                c.execute("INSERT INTO bot_accounts(uid,display_name,password_hash,password_salt) VALUES(?,?,?,?)",
                          (uid, item["name"], digest, salt))
        save_user("telegram", uid, SOURCES[role], item["file"], item["name"], True)
        set_state(uid, None)
        await schedule_image(call.message, SOURCES[role], item["file"], item["name"], edit=True)
        await call.message.answer("✅ Регистрация завершена! Кнопки главного меню находятся снизу 👇", reply_markup=keyboard(uid))


async def onboarding_catalog(message, role, page=0):
    from app import fetch_html, SCHEDULE_URLS, GroupParser
    parser = GroupParser()
    parser.feed(await fetch_html(SCHEDULE_URLS[SOURCES[role]]))
    items = parser.groups
    if not items:
        await show_screen(message, "Список пока пуст. Попробуй позже.")
        return
    page = min(max(0,page), (len(items)-1)//8)
    rows = [[(item["name"][:48], f"onboardpick:{role}:{page}:{i}")]
            for i,item in enumerate(items[page*8:page*8+8])]
    nav = []
    if page:
        nav.append(("◀️", f"onboardcat:{role}:{page-1}"))
    if (page+1)*8 < len(items):
        nav.append(("▶️", f"onboardcat:{role}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("↩️ Изменить роль", "onboard:s" if role=="t" else "onboard:t")])
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
        rows = [[(item["name"][:48], "view:"+source_key+":"+str(page)+":"+str(i))]
                for i,item in enumerate(items[page*8:page*8+8])]
        nav = []
        if page:
            nav.append(("◀️", "cat:"+source_key+":"+str(page-1)))
        if (page+1)*8 < len(items):
            nav.append(("▶️", "cat:"+source_key+":"+str(page+1)))
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


def day_keyboard(item_id,selected):
    from schedule_browser import DAY_SHORT
    rows=[]
    for start in (0,3):
        rows.append([(("● " if i==selected else "")+DAY_SHORT[i],"day:"+str(item_id)+":"+str(i)) for i in range(start,start+3)])
    rows.append([("📅 Сегодня","today:"+str(item_id))])
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


async def schedule_image(message, source, filename, title, day=None, item_id=None, edit=False):
    from app import fetch_html, SCHEDULE_BASE
    if source not in SOURCES.values() or not re.fullmatch(r"[^/\\?#]+\.html?", filename, re.I):
        await message.answer("Некорректное расписание.")
        return
    try:
        if item_id is None:
            with db() as c:
                c.execute("INSERT INTO bot_catalog_items(source,filename,title) VALUES (?,?,?) ON CONFLICT(source,filename) DO UPDATE SET title=excluded.title",(source,filename,title))
                item=c.execute("SELECT id FROM bot_catalog_items WHERE source=? AND filename=?",(source,filename)).fetchone()
                item_id=item["id"]
        if day is None:
            day = today_schedule_day()
        if day == 6:
            parts = ["📅 <b>" + escape(title) + " · Воскресенье</b>\n☀️ Сегодня выходной, уроков нет."]
        else:
            html = await fetch_html(urljoin(SCHEDULE_BASE, filename))
            parts = schedule_text_parts(title, html, day)
        from substitutions import substitutions_for, format_group_changes
        from datetime import timedelta
        local_today = datetime.now(ZoneInfo(SCHEDULE_TIMEZONE)).date()
        target_date = (local_today + timedelta(days=(day - local_today.weekday()) % 7)).isoformat()
        changes = substitutions_for(title, target_date) if source == "raspisan" else []
        if changes:
            parts.append("<b>🔄 Замены</b>\n" + escape(format_group_changes(changes, title, target_date)))
        content = "\n\n".join(parts)
        if len(content) > 4000:
            content = parts[0] if len(parts[0]) <= 4000 else parts[0][:3500] + "…"
            content += "\n\n⚠️ Расписание слишком длинное для одного сообщения."
        markup = day_keyboard(item_id, day if day < 6 else -1)
        await show_screen(message, content, edit=edit, parse_mode="HTML", reply_markup=markup)
    except Exception:
        log.exception("Schedule text failed")
        await message.answer("Не удалось загрузить расписание. Попробуй позже.")


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
        [("📋 Посмотреть расписание","imgid:"+item_id)],
        [favorite_button],
        [("📌 Сделать моим","mineid:"+item_id)],
    ]))


async def show_favorites(message, uid):
    with db() as c:
        rows = c.execute("SELECT id,title FROM bot_favorites WHERE uid=? ORDER BY id DESC LIMIT 60", (uid,)).fetchall()
    if not rows:
        await show_screen(message,"⭐ Избранное пусто. Открой любое расписание и нажми «В избранное».")
        return
    await show_screen(message,"⭐ Избранное", reply_markup=buttons([[(r["title"][:48], "favorite:"+str(r["id"]))] for r in rows]))


@router.callback_query(F.data.startswith(("reg:", "cat:", "view:", "browse", "addid:", "unfav:", "favorite:", "imgid:", "mineid:", "day:", "all:", "today:")))
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
    if not account(uid):
        await call.message.answer("Сначала зарегистрируйся: /start")
        return
    if data == "browse":
        await show_screen(call.message,"🔎 Чьё расписание открыть?", reply_markup=catalog_keyboard(), edit=True)
    elif data.startswith("cat:"):
        parts=data.split(":")
        if len(parts)==3 and parts[2].isdigit():
            await show_catalog(call.message,parts[1],int(parts[2]))
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
            save_user("telegram",uid,source,filename,title,True)
            await show_screen(call.message,"📌 Личное расписание: "+title,reply_markup=keyboard(uid))
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
async def profile(message: Message):
    uid=str(message.from_user.id)
    a=account(uid)
    p=user("telegram",uid)
    if not a:
        await ask_role(message)
        return
    await show_screen(message,"👤 "+a["display_name"]+"\nРоль: "+("Преподаватель" if get_role(uid)=="teacher" else "Студент")+
                         "\n📌 Личное: "+(p["group_name"] if p and p["filename"] else "не выбрано")+
                         "\n⭐ Избранное: /favorites\n🔄 Перерегистрироваться: /register",reply_markup=keyboard(uid))


@router.message(Command("favorites"))
async def favorites(message: Message):
    uid=str(message.from_user.id)
    if not account(uid):
        await ask_role(message)
    else:
        await show_favorites(message,uid)


@router.message(Command("schedule"))
async def personal(message: Message):
    uid=str(message.from_user.id)
    if not account(uid):
        await ask_role(message)
        return
    p=user("telegram",uid)
    if not p or not p["filename"]:
        await show_screen(message,"Сначала выбери личное расписание в разделе «🔎 Все расписания».",reply_markup=catalog_keyboard())
        return
    await schedule_image(message,p["source"],p["filename"],p["group_name"])


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
    if text in ("📅 Моё расписание", "📅 Расписание", "🔎 Все расписания", "📚 Выбрать расписание", "⭐ Избранное", "👤 Профиль", "🔐 Админ-панель", "🔔 Уведомления", "📰 Новости"):
        try:
            await message.delete()
        except Exception:
            pass
    if text in ("📅 Моё расписание","📅 Расписание"):
        await personal(message)
    elif text in ("🔎 Все расписания","📚 Выбрать расписание"):
        if not account(uid):
            await ask_role(message)
        else:
            await show_screen(message,"🔎 Выбери категорию:",reply_markup=catalog_keyboard())
    elif text=="⭐ Избранное":
        await favorites(message)
    elif text=="👤 Профиль":
        await profile(message)
    elif text=="🔐 Админ-панель":
        await admin(message)
    elif text=="🔔 Уведомления":
        await show_screen(message,"🔔 /notify — включить, /mute — выключить.")
    elif text=="📰 Новости":
        from features import news_list
        items=news_list()[:3]
        await show_screen(message,"\n\n".join(x["title"]+"\n"+x["body"][:350] for x in items) if items else "Новостей пока нет.")
    else:
        await message.answer("Выбери действие из меню.",reply_markup=keyboard(uid) if account(uid) else None)
