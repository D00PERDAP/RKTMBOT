"""Bot-only registration, schedule images and Telegram admin commands."""
import os
import io
import re
import hmac
import textwrap
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from features import db, save_user, user

router = Router()
TEACHER_CODE = os.getenv("TEACHER_ACCESS_CODE", "РКТМПРЕПОД2026")
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}

def setup():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS bot_roles (uid TEXT PRIMARY KEY, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_states (uid TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approved_teachers (uid TEXT PRIMARY KEY);
        """)

def role(uid):
    with db() as c:
        row=c.execute("SELECT role FROM bot_roles WHERE uid=?", (str(uid),)).fetchone()
        return row["role"] if row else None

def state(uid):
    with db() as c:
        row=c.execute("SELECT state FROM bot_states WHERE uid=?", (str(uid),)).fetchone()
        return row["state"] if row else None

def set_state(uid, value):
    with db() as c:
        if value:
            c.execute("INSERT INTO bot_states VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET state=excluded.state",(str(uid),value))
        else:
            c.execute("DELETE FROM bot_states WHERE uid=?",(str(uid),))

def set_role(uid, value):
    with db() as c:
        c.execute("INSERT INTO bot_roles VALUES (?,?) ON CONFLICT(uid) DO UPDATE SET role=excluded.role",(str(uid),value))
    set_state(uid,None)

def approved(uid):
    with db() as c:
        return c.execute("SELECT 1 FROM approved_teachers WHERE uid=?",(str(uid),)).fetchone() is not None

def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows
    ])

def bottom_menu(uid):
    rows = [
        [KeyboardButton(text="📅 Моё расписание")],
        [KeyboardButton(text="📚 Выбрать расписание"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="📰 Новости")],
    ]
    if str(uid) in ADMIN_IDS:
        rows.append([KeyboardButton(text="🔐 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True,is_persistent=True)

def menu():
    return buttons([
        [("🖼 Моё расписание","b:schedule")],
        [("📚 Выбрать расписание","b:choose")],
        [("👤 Профиль","b:profile"),("📰 Новости","b:news")],
    ])

@router.message(CommandStart())
async def start(message:Message):
    setup()
    uid=str(message.from_user.id)
    if not role(uid):
        await message.answer("🇷🇺 РКТМ — расписание\nВыбери, кто ты:",reply_markup=buttons([
            [("🎓 Студент","b:student"),("👨‍🏫 Преподаватель","b:teacher")]
        ]))
    else:
        await message.answer("РКТМ · Главное меню",reply_markup=bottom_menu(uid))

@router.callback_query(F.data.startswith("b:"))
async def actions(call:CallbackQuery):
    await call.answer()
    uid=str(call.from_user.id)
    action=call.data[2:]
    if action=="student":
        set_role(uid,"student")
        await call.message.answer("🎓 Регистрация завершена.",reply_markup=bottom_menu(uid))
    elif action=="teacher":
        if approved(uid):
            set_role(uid,"teacher")
            await call.message.answer("👨‍🏫 Доступ преподавателя подтверждён.",reply_markup=bottom_menu(uid))
        else:
            set_state(uid,"teacher_code")
            await call.message.answer("Отправь кодовое слово преподавателя одним сообщением.")
    elif action=="choose":
        if not role(uid):
            await call.message.answer("Сначала зарегистрируйся: /start")
            return
        await choose(call.message,uid)
    elif action=="schedule":
        await send_schedule(call.message,uid)
    elif action=="profile":
        profile=user("telegram",uid)
        await call.message.answer(
            "👤 Роль: "+("Преподаватель" if role(uid)=="teacher" else "Студент" if role(uid)=="student" else "Не зарегистрирован")
            +"\nРасписание: "+(profile["group_name"] if profile and profile["filename"] else "не выбрано")
            +"\nСменить роль: /startrole",reply_markup=bottom_menu(uid))
    elif action=="news":
        from features import news_list
        items=news_list()[:3]
        await call.message.answer("\n\n".join(x["title"]+"\n"+x["body"][:350] for x in items) if items else "Новостей пока нет.")
    elif action.startswith("page:"):
        await choose(call.message,uid,int(action.split(":")[1]))
    elif action.startswith("pick:"):
        _,page,idx=action.split(":")
        await pick(call.message,uid,int(page),int(idx))

@router.message(Command("startrole"))
async def change_role(message:Message):
    await message.answer("Выбери роль:",reply_markup=buttons([[("🎓 Студент","b:student"),("👨‍🏫 Преподаватель","b:teacher")]]))

async def index_items(uid):
    from app import fetch_html, SCHEDULE_URLS, GroupParser
    source="Praspisan" if role(uid)=="teacher" else "raspisan"
    html=await fetch_html(SCHEDULE_URLS[source])
    parser=GroupParser();parser.feed(html)
    return source,parser.groups

async def choose(message,uid,page=0):
    try:
        source,items=await index_items(uid)
        if not items:
            await message.answer("Список пока недоступен.")
            return
        page=max(0,min(page,(len(items)-1)//8))
        rows=[[(item["name"][:48],"b:pick:"+str(page)+":"+str(i))] for i,item in enumerate(items[page*8:page*8+8])]
        nav=[]
        if page>0:nav.append(("◀️", "b:page:"+str(page-1)))
        if (page+1)*8<len(items):nav.append(("▶️","b:page:"+str(page+1)))
        if nav:rows.append(nav)
        await message.answer("Выбери "+("преподавателя" if source=="Praspisan" else "группу")+":",reply_markup=buttons(rows))
    except Exception:
        await message.answer("Не удалось загрузить список расписаний. Попробуй позже.")

async def pick(message,uid,page,idx):
    try:
        source,items=await index_items(uid)
        pos=page*8+idx
        if idx<0 or idx>=8 or pos>=len(items):raise ValueError()
        item=items[pos]
        save_user("telegram",uid,source,item["file"],item["name"],True)
        await message.answer("✅ Сохранено: "+item["name"],reply_markup=bottom_menu(uid))
        await send_schedule(message,uid)
    except Exception:
        await message.answer("Не удалось сохранить выбор. Попробуй ещё раз.")

from schedule_cards import render_schedule

async def send_schedule(message,uid):
    profile=user("telegram",uid)
    if not role(uid):
        await message.answer("Сначала зарегистрируйся: /start")
        return
    if not profile or not profile["filename"]:
        await message.answer("Сначала выбери расписание.",reply_markup=buttons([[("Выбрать","b:choose")]]))
        return
    if profile["source"]!=("Praspisan" if role(uid)=="teacher" else "raspisan"):
        await message.answer("Для новой роли выбери своё расписание.",reply_markup=buttons([[("Выбрать","b:choose")]]))
        return
    from app import fetch_html,SCHEDULE_BASE
    from urllib.parse import urljoin
    try:
        html=await fetch_html(urljoin(SCHEDULE_BASE,profile["filename"]))
        from schedule_browser import render_schedule_pages
        pages=await render_schedule_pages(profile["group_name"],html)
        for index,png in enumerate(pages,1):
            await message.answer_photo(BufferedInputFile(png,filename=f"rktm-{index}.png"),caption="🗓 "+profile["group_name"]+f" · {index}/{len(pages)}" if index==1 else None)
    except Exception:
        await message.answer("Не удалось сформировать картинку. Попробуй позже.")

@router.message(Command("schedule"))
async def schedule_command(message:Message):
    await send_schedule(message,str(message.from_user.id))

@router.message(Command("id"))
async def my_id(message:Message):
    await message.answer("Твой Telegram ID: "+str(message.from_user.id))

@router.message(Command("profile"))
async def profile(message:Message):
    uid=str(message.from_user.id)
    p=user("telegram",uid)
    await message.answer("Роль: "+str(role(uid) or "не выбрана")+"\\nРасписание: "+(p["group_name"] if p and p["filename"] else "не выбрано"),reply_markup=bottom_menu(uid))

@router.message(Command("admin"))
async def admin(message:Message):
    uid=str(message.from_user.id)
    if uid not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return
    await message.answer("🔐 Админ-панель\n/addteacher TELEGRAM_ID — добавить преподавателя\n/removeteacher TELEGRAM_ID — убрать доступ\n/teachers — список преподавателей")

@router.message(Command("addteacher","removeteacher","teachers"))
async def admin_teachers(message:Message):
    if str(message.from_user.id) not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return
    command=(message.text or "").split()
    if command[0].split("@")[0]=="/teachers":
        with db() as c:
            rows=c.execute("SELECT uid FROM approved_teachers ORDER BY uid LIMIT 100").fetchall()
        await message.answer("Преподаватели:\n"+("\n".join(r["uid"] for r in rows) or "Пока нет"))
        return
    if len(command)!=2 or not command[1].isdigit():
        await message.answer("Укажи числовой Telegram ID.")
        return
    uid=command[1]
    with db() as c:
        if command[0].startswith("/addteacher"):
            c.execute("INSERT OR IGNORE INTO approved_teachers(uid) VALUES (?)",(uid,))
        else:
            c.execute("DELETE FROM approved_teachers WHERE uid=?",(uid,))
            c.execute("DELETE FROM bot_roles WHERE uid=? AND role='teacher'",(uid,))
    if command[0].startswith("/addteacher"):set_role(uid,"teacher")
    await message.answer("Готово.")

@router.message(F.text)
async def text_input(message:Message):
    uid=str(message.from_user.id)
    if state(uid)=="teacher_code":
        set_state(uid,None)
        if hmac.compare_digest((message.text or "").strip().encode(),TEACHER_CODE.encode()):
            set_role(uid,"teacher")
            await message.answer("👨‍🏫 Регистрация преподавателя завершена.",reply_markup=bottom_menu(uid))
        else:
            await message.answer("Неверное кодовое слово. Повтори через /startrole.")
        return
    if message.text in ("📅 Расписание","🖼 Моё расписание","📅 Моё расписание"):
        await send_schedule(message,uid)
    elif message.text in ("👥 Моя группа","📚 Выбрать расписание"):
        await choose(message,uid)
    elif message.text=="👤 Профиль":
        await profile(message)
    elif message.text=="🔐 Админ-панель":
        await admin(message)
    elif message.text=="🔔 Уведомления":
        await message.answer("🔔 Настройки уведомлений: /notify — включить, /mute — выключить")
    elif message.text=="📰 Новости":
        from features import news_list
        items=news_list()[:3]
        await message.answer("\\n\\n".join(x["title"]+"\\n"+x["body"][:350] for x in items) if items else "Новостей пока нет.")
    else:
        await message.answer("Выбери действие:",reply_markup=bottom_menu(uid))
