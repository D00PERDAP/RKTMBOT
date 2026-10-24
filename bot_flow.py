"""Telegram registration, public schedule browser, favorites and admin controls."""
import hashlib
import hmac
import logging
import os
import re
import secrets
from urllib.parse import urljoin

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from features import db, save_user, user

router = Router()
log = logging.getLogger(__name__)
TEACHER_CODE = os.getenv("TEACHER_ACCESS_CODE", "РКТМПРЕПОД2026")
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
SOURCES = {"s": "raspisan", "t": "Praspisan"}
LABELS = {"s": "🎓 Группы", "t": "👨‍🏫 Преподаватели"}


def setup():
    with db() as c:
        c.executescript("""
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
        CREATE TABLE IF NOT EXISTS bot_catalog_items(\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            source TEXT NOT NULL, filename TEXT NOT NULL, title TEXT NOT NULL,\n            UNIQUE(source,filename)\n        );\n        CREATE TABLE IF NOT EXISTS bot_pending(
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
    await message.answer(
        "👋 Добро пожаловать в РКТМ!\n"
        "Регистрация: роль → имя → пароль.\n"
        "После регистрации доступны ВСЕ расписания и избранное.",
        reply_markup=buttons([[("🎓 Студент", "reg:s"), ("👨‍🏫 Преподаватель", "reg:t")]])
    )


@router.message(CommandStart())
async def start(message: Message):
    uid = str(message.from_user.id)
    if not account(uid):
        await ask_role(message)
    else:
        await message.answer("🏠 РКТМ · Привет, "+account(uid)["display_name"]+"!", reply_markup=keyboard(uid))


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
        await message.answer(LABELS[source_key]+" · "+str(page+1)+"/"+str((len(items)+7)//8),
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


async def schedule_image(message, source, filename, title):
    from app import fetch_html, SCHEDULE_BASE
    from schedule_browser import render_schedule_pages
    if source not in SOURCES.values() or not re.fullmatch(r"[^/\\?#]+\.html?", filename, re.I):
        await message.answer("Некорректное расписание.")
        return
    try:
        html = await fetch_html(urljoin(SCHEDULE_BASE, filename))
        pages = await render_schedule_pages(title, html)
        for i, png in enumerate(pages, 1):
            await message.answer_photo(
                BufferedInputFile(png, filename="schedule-"+str(i)+".png"),
                caption=("📅 "+title+" · "+str(i)+"/"+str(len(pages))) if i==1 else None,
            )
    except Exception:
        log.exception("Schedule render failed")
        await message.answer("Не удалось создать карточки. Проверь, установлен ли Chromium на сервере.")


async def show_item(message, uid, source, filename, title):
    with db() as c:
        c.execute("INSERT INTO bot_catalog_items(source,filename,title) VALUES (?,?,?) ON CONFLICT(source,filename) DO UPDATE SET title=excluded.title",
                  (source,filename,title))
        item=c.execute("SELECT id FROM bot_catalog_items WHERE source=? AND filename=?",(source,filename)).fetchone()
        fav=c.execute("SELECT id FROM bot_favorites WHERE uid=? AND source=? AND filename=?",
                      (uid,source,filename)).fetchone()
    item_id=str(item["id"])
    favorite_button=("❌ Убрать из избранного","unfav:"+str(fav["id"])) if fav else ("⭐ В избранное","addid:"+item_id)
    await message.answer("📖 "+title+"\\nВыбери действие:", reply_markup=buttons([
        [("🖼 Посмотреть расписание","imgid:"+item_id)],
        [favorite_button],
        [("📌 Сделать моим","mineid:"+item_id)],
    ]))


async def show_favorites(message, uid):
    with db() as c:
        rows = c.execute("SELECT id,title FROM bot_favorites WHERE uid=? ORDER BY id DESC LIMIT 60", (uid,)).fetchall()
    if not rows:
        await message.answer("⭐ Избранное пусто. Открой любое расписание и нажми «В избранное».")
        return
    await message.answer("⭐ Избранное", reply_markup=buttons([[(r["title"][:48], "favorite:"+str(r["id"]))] for r in rows]))


@router.callback_query(F.data.startswith(("reg:", "cat:", "view:", "browse", "addid:", "unfav:", "favorite:", "imgid:", "mineid:")))
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
        await call.message.answer("🔎 Чьё расписание открыть?", reply_markup=catalog_keyboard())
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
            await call.message.answer("📌 Личное расписание: "+title,reply_markup=keyboard(uid))
        else:
            await schedule_image(call.message,source,filename,title)
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
            await call.message.answer("Удалено из избранного.")
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
    await message.answer("👤 "+a["display_name"]+"\nРоль: "+("Преподаватель" if get_role(uid)=="teacher" else "Студент")+
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
        await message.answer("Сначала выбери личное расписание в разделе «🔎 Все расписания».",reply_markup=catalog_keyboard())
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
    await message.answer("🔐 Админ-панель\n/addteacher ID\n/removeteacher ID\n/teachers")


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
    if text in ("📅 Моё расписание","📅 Расписание"):
        await personal(message)
    elif text in ("🔎 Все расписания","📚 Выбрать расписание"):
        if not account(uid):
            await ask_role(message)
        else:
            await message.answer("🔎 Выбери категорию:",reply_markup=catalog_keyboard())
    elif text=="⭐ Избранное":
        await favorites(message)
    elif text=="👤 Профиль":
        await profile(message)
    elif text=="🔐 Админ-панель":
        await admin(message)
    elif text=="🔔 Уведомления":
        await message.answer("🔔 /notify — включить, /mute — выключить.")
    elif text=="📰 Новости":
        from features import news_list
        items=news_list()[:3]
        await message.answer("\n\n".join(x["title"]+"\n"+x["body"][:350] for x in items) if items else "Новостей пока нет.")
    else:
        await message.answer("Выбери действие из меню.",reply_markup=keyboard(uid) if account(uid) else None)
