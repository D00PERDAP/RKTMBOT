
import os
from dotenv import load_dotenv
load_dotenv()
import json
import hmac
import hashlib
from urllib.parse import parse_qsl
from datetime import datetime, timezone
import re
import logging
import time
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from pydantic import BaseModel, Field
from features import initialize, save_user, user, news_list, add_news, add_feedback, compare_snapshot, subscribers, admin_authorized
from fastapi.responses import FileResponse, HTMLResponse
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "webapp" / "index.html"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "",
).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))
_html_cache: dict[str, tuple[float, str]] = {}
_cache_lock = asyncio.Lock()

SCHEDULE_BASE = "https://rktm.info/wp-content/uploads/HTML/"

SCHEDULE_URLS = {
    "raspisan": urljoin(SCHEDULE_BASE, "raspisan.html"),
    "Praspisan": urljoin(SCHEDULE_BASE, "Praspisan.html"),
    "Araspisan": urljoin(SCHEDULE_BASE, "Araspisan.html"),
}

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


class GroupParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.groups = []
        self.href = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "a" or self.href is None:
            return

        name = " ".join("".join(self.parts).split())
        filename = unquote(self.href.strip())

        if (
            name
            and re.fullmatch(r"[^/\\?#]+\.html?", filename, re.I)
            and name.lower() not in {"здесь", "скачать"}
        ):
            item = {"name": name, "file": filename}
            if item not in self.groups:
                self.groups.append(item)

        self.href = None
        self.parts = []


@dp.message(CommandStart())
async def start_command(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🔴 Открыть РКТМ NEXT",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]],
        resize_keyboard=True,
        is_persistent=True,
    )
    await message.answer(
        "🔴 РКТМ NEXT — твой цифровой кампус.\n\n"
        "📅 Расписание и изменения\n⚡ Новости\n👤 Твоя группа и уведомления\n💬 Поддержка\n\n"
        "Открой приложение — профиль синхронизируется с ботом.",
        reply_markup=keyboard,
    )
    await message.answer("Открыть мини-приложение:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Запустить РКТМ NEXT", web_app=WebAppInfo(url=WEBAPP_URL))]]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize()
    if bot and WEBHOOK_SECRET and WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_webhook(
                url=f"{WEBAPP_URL}/telegram/webhook",
                secret_token=WEBHOOK_SECRET or None,
                allowed_updates=dp.resolve_used_update_types(),
            )
            logger.info("Telegram webhook configured")
        except Exception:
            logger.exception("Не удалось настроить Telegram webhook")

    monitor = asyncio.create_task(schedule_monitor()) if bot and WEBHOOK_SECRET and WEBAPP_URL.startswith("https://") else None
    yield
    if monitor:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass

    if bot:
        await bot.session.close()


app = FastAPI(title="РКТМ — расписание", lifespan=lifespan)


async def fetch_html(url: str, force_refresh: bool = False) -> str:
    # Only fixed trusted college URLs reach this function.
    async with _cache_lock:
        cached = _html_cache.get(url)
        if cached and not force_refresh and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content

            content_type = response.headers.get("content-type", "")
            match = re.search(
                r"charset\s*=\s*[\"']?([^;\"'\s]+)",
                content_type,
                re.I,
            )
            encoding = match.group(1).strip() if match else ""

            if not encoding:
                meta = re.search(
                    rb"charset\s*=\s*[\"']?([\w-]+)",
                    content[:4096],
                    re.I,
                )
                if meta:
                    encoding = meta.group(1).decode(
                        "ascii", errors="ignore"
                    )

            candidates = []
            if encoding:
                candidates.append(encoding)
            candidates.extend(["utf-8", "cp1251", "windows-1251"])

            for candidate in dict.fromkeys(candidates):
                try:
                    decoded = content.decode(candidate)
                    if "\ufffd" not in decoded:
                        async with _cache_lock:
                            _html_cache[url] = (time.monotonic(), decoded)
                        return decoded
                except (LookupError, UnicodeDecodeError):
                    continue

            decoded = content.decode("cp1251", errors="replace")
            async with _cache_lock:
                _html_cache[url] = (time.monotonic(), decoded)
            return decoded

    except httpx.HTTPStatusError as exc:
        if url in _html_cache:
            logger.warning("Serving cached schedule after upstream HTTP failure")
            return _html_cache[url][1]
        logger.exception("Ошибка HTTP источника расписания")
        raise HTTPException(
            status_code=502,
            detail=f"Сайт РКТМ вернул HTTP {exc.response.status_code}",
        ) from exc
    except httpx.HTTPError as exc:
        if url in _html_cache:
            logger.warning("Serving cached schedule after network failure")
            return _html_cache[url][1]
        logger.exception("Не удалось получить расписание")
        raise HTTPException(
            status_code=502,
            detail="Не удалось получить данные с сайта РКТМ",
        ) from exc


def get_source(source: str) -> str:
    if source not in SCHEDULE_URLS:
        raise HTTPException(status_code=404, detail="Раздел не найден")
    return source


def extract_schedule_info(html: str) -> dict:
    """
    Извлекает общую информацию из старого HTML Microsoft Word.
    Полное отображение расписания выполняется в Mini App.
    """
    title_match = re.search(
        r"Расписание\s+занятий\s+учебной\s+группы\s*:",
        html,
        re.I,
    )

    group = ""
    week = ""

    if title_match:
        tail = re.sub(r"<[^>]+>", " ", html[title_match.end():])
        tail = re.sub(r"\s+", " ", tail)

        group_match = re.search(
            r"([А-ЯЁA-Z]{1,5}[- ]?\d{1,3})",
            tail,
            re.I,
        )
        week_match = re.search(r"Неделя\s*:\s*(\d+)", tail, re.I)

        if group_match:
            group = group_match.group(1).strip()
        if week_match:
            week = week_match.group(1)

    times = re.findall(
        r"\b(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\b",
        html,
    )

    return {
        "group": group,
        "week": week,
        "times": [
            {"start": start, "end": end}
            for start, end in times
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    if not INDEX_FILE.is_file():
        raise HTTPException(
            status_code=500,
            detail="Не найден файл webapp/index.html",
        )
    return FileResponse(INDEX_FILE, media_type="text/html")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "NEXT Red Edition",
        "bot_configured": bot is not None,
        "webapp_exists": INDEX_FILE.is_file(),
    }


@app.get("/api/groups/{source}")
async def get_groups(source: str):
    get_source(source)
    html = await fetch_html(SCHEDULE_URLS[source])

    parser = GroupParser()
    parser.feed(html)

    return {
        "source": source,
        "groups": parser.groups,
    }


@app.get("/api/group/{source}/{filename}")
async def get_group_schedule(source: str, filename: str):
    get_source(source)
    filename = unquote(filename)

    if (
        not re.fullmatch(r"[^/\\?#]+\.html?", filename, re.I)
        or ".." in filename
    ):
        raise HTTPException(
            status_code=400,
            detail="Некорректное имя файла",
        )

    # Allow only filenames published in the selected official index.
    index_html = await fetch_html(SCHEDULE_URLS[source])
    parser = GroupParser()
    parser.feed(index_html)
    if filename not in {item["file"] for item in parser.groups}:
        raise HTTPException(status_code=404, detail="Расписание отсутствует в официальном списке")
    url = urljoin(SCHEDULE_BASE, filename)
    html = await fetch_html(url)

    return {
        "name": filename,
        "html": html,
        "base_url": SCHEDULE_BASE,
        "info": extract_schedule_info(html),
    }


@app.get("/api/schedule/{source}")
async def get_schedule(source: str):
    get_source(source)
    return {"html": await fetch_html(SCHEDULE_URLS[source])}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        raise HTTPException(
            status_code=503,
            detail="BOT_TOKEN не настроен в Render",
        )

    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="WEBHOOK_SECRET is required")

    if WEBHOOK_SECRET:
        received = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if not hmac.compare_digest(received, WEBHOOK_SECRET):
            raise HTTPException(
                status_code=403,
                detail="Invalid webhook secret",
            )

    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as exc:
        logger.exception("Ошибка обработки обновления Telegram")
        raise HTTPException(
            status_code=500,
            detail="Ошибка обработки обновления Telegram",
        ) from exc


@app.get("/api")
async def api_info():
    return {
        "service": "RKTM Schedule",
        "sources": list(SCHEDULE_URLS.keys()),
    }


class NewsPost(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    body: str = Field(min_length=3, max_length=4000)

@app.get('/api/news')
async def get_news():
    return {'items': news_list()}

@app.post('/api/admin/news')
async def create_news(post: NewsPost, x_admin_token: str = Header(default='')):
    if not admin_authorized(x_admin_token):
        raise HTTPException(403, 'Admin access required')
    add_news(post.title, post.body)
    return {'ok': True}

def verified_telegram_user(init_data: str) -> str:
    """Telegram Mini App HMAC validation; never trust client-supplied user IDs."""
    if not bot or not BOT_TOKEN:
        raise HTTPException(503, 'Telegram bot is not configured')
    if not init_data or len(init_data) > 8192:
        raise HTTPException(401, 'Open the application from Telegram')
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        provided = fields.pop('hash')
        auth_date = int(fields['auth_date'])
        age = int(datetime.now(timezone.utc).timestamp()) - auth_date
        if age < -60 or age > 86400:
            raise ValueError('Expired initData')
        check = '\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, digest):
            raise ValueError('Invalid signature')
        user_data = json.loads(fields['user'])
        return str(int(user_data['id']))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, 'Invalid Telegram authentication')


class MiniProfile(BaseModel):
    source: str = Field(pattern=r'^(raspisan|Praspisan|Araspisan)$')
    filename: str = Field(min_length=1, max_length=180)
    group_name: str = Field(min_length=1, max_length=180)
    notifications: bool = False


@app.get('/api/me')
async def get_my_profile(x_telegram_init_data: str = Header(default='')):
    uid = verified_telegram_user(x_telegram_init_data)
    return {'profile': user('telegram', uid), 'user_id': uid}


@app.post('/api/me')
async def save_my_profile(profile: MiniProfile, x_telegram_init_data: str = Header(default='')):
    uid = verified_telegram_user(x_telegram_init_data)
    if not re.fullmatch(r'[^/\\?#]+\.html?', profile.filename, re.I) or '..' in profile.filename:
        raise HTTPException(400, 'Invalid filename')
    # A file must appear in the official source index, not just match a pattern.
    html = await fetch_html(SCHEDULE_URLS[profile.source])
    parser = GroupParser(); parser.feed(html)
    found = next((g for g in parser.groups if g['file'] == profile.filename), None)
    if not found:
        raise HTTPException(400, 'Schedule not found in official index')
    save_user('telegram', uid, profile.source, profile.filename, found['name'], profile.notifications)
    return {'ok': True, 'profile': user('telegram', uid)}


@app.post('/api/me/feedback')
async def send_my_feedback(feedback: NewsPost, x_telegram_init_data: str = Header(default='')):
    uid = verified_telegram_user(x_telegram_init_data)
    add_feedback('telegram', uid, feedback.title + '\n' + feedback.body)
    return {'ok': True}


@app.get('/api/admin/feedback')
async def list_feedback(x_admin_token: str = Header(default='')):
    if not admin_authorized(x_admin_token):
        raise HTTPException(403, 'Admin access required')
    from features import db
    with db() as c:
        return {'items': [dict(x) for x in c.execute('SELECT * FROM feedback ORDER BY id DESC LIMIT 100')]}

@dp.message(Command('group'))
async def group_command(message: Message):
    parts=(message.text or '').split(maxsplit=2)
    if len(parts)!=3 or not re.fullmatch(r'[^/\\?#]+\.html?',parts[2],re.I):
        await message.answer('Формат: /group raspisan filename.html\nИмя файла можно узнать из списка расписания.')
        return
    if parts[1] not in SCHEDULE_URLS:
        await message.answer('Неизвестный раздел расписания')
        return
    index_html = await fetch_html(SCHEDULE_URLS[parts[1]])
    parser = GroupParser(); parser.feed(index_html)
    selected = next((g for g in parser.groups if g['file'] == parts[2]), None)
    if not selected:
        await message.answer('Такого расписания нет в официальном списке. Выбери его через приложение.')
        return
    save_user('telegram',str(message.from_user.id),parts[1],parts[2],selected['name'],True)
    await message.answer('Группа сохранена. Уведомления об изменениях включены. /mute — отключить.')

@dp.message(Command('profile'))
async def profile_command(message: Message):
    current = user('telegram', str(message.from_user.id))
    if not current or not current['filename']:
        await message.answer('🔴 Группа пока не выбрана. Открой РКТМ NEXT и сохрани расписание.')
        return
    await message.answer('👤 Мой профиль\nГруппа: '+current['group_name']+'\nУведомления: '+('включены 🔔' if current['notifications'] else 'выключены 🔕')+'\n\nИзменить настройки можно в мини-приложении.')

@dp.message(Command('mute'))
async def mute_command(message: Message):
    current=user('telegram',str(message.from_user.id))
    if current:
        save_user('telegram',str(message.from_user.id),current['source'],current['filename'],current['group_name'],False)
    await message.answer('Уведомления выключены.')

@dp.message(Command('news'))
async def news_command(message: Message):
    items=news_list()[:5]
    await message.answer('\n\n'.join(x['title']+'\n'+x['body'][:600] for x in items) if items else 'Пока нет опубликованных новостей.')

@dp.message(Command('help'))
async def help_command(message: Message):
    await message.answer('🔴 РКТМ NEXT\n/start — открыть приложение\n/profile — моя группа\n/group raspisan filename.html — выбрать группу\n/mute — отключить уведомления\n/news — новости\n/help — помощь')

async def schedule_monitor():
    # Checks only subscribed files. First observation sets baseline without notification.
    await asyncio.sleep(10)
    while True:
        try:
            from features import db
            with db() as c:
                items=[dict(x) for x in c.execute('SELECT DISTINCT source,filename FROM users WHERE notifications=1 AND filename != ""')]
            for item in items:
                try:
                    html=await fetch_html(urljoin(SCHEDULE_BASE,item['filename']), force_refresh=True)
                    if compare_snapshot(item['source'],item['filename'],html):
                        for recipient in subscribers(item['source'],item['filename']):
                            if recipient['platform']=='telegram' and bot:
                                try:
                                    await bot.send_message(int(recipient['user_id']), '📅 Расписание изменилось! Проверь актуальные пары в мини-приложении.')
                                except Exception:
                                    logger.exception('Failed to notify subscriber')
                except Exception:
                    logger.exception('Schedule monitor failed for %s',item['filename'])
        except Exception:
            logger.exception('Schedule monitor cycle failed')
        await asyncio.sleep(max(300,int(os.getenv('MONITOR_INTERVAL_SECONDS','900'))))
