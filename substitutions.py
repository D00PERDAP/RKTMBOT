"""Automatic college PDF substitution discovery and safe per-group extraction."""
import asyncio
import os
from zoneinfo import ZoneInfo
import hashlib
import json
import io
import re
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import httpx
import pdfplumber
from bs4 import BeautifulSoup

PAGE_URL = "https://rktm.info/расписание/расписание-студенов/"
GROUP_RE = re.compile(r"([А-ЯЁ]{1,4})[-\s]?(\d{2})", re.I)
DATE_RE = re.compile(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{2,4})")
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
          "июня": 6, "июля": 7, "августа": 8, "сентября": 9,
          "октября": 10, "ноября": 11, "декабря": 12}


def group_key(name):
    """Match official short group codes against full catalog labels."""
    value = re.sub(r"\s+", "", name or "").upper().replace("Ё", "Е").replace("–", "-").replace("—", "-")
    match = GROUP_RE.search(value)
    if not match:
        return None
    # Catalog names include training year: АТ09.29-15 -> АТ-15.
    suffix = re.search(r"[-–](\d{2})К?$", value)
    return (match.group(1).replace("Ё", "Е"), suffix.group(1) if suffix else match.group(2), "к" if value.endswith("К") else "")


def document_date(value):
    value = unquote(value).lower()
    match = DATE_RE.search(value)
    if match:
        d, m, y = map(int, match.groups())
        try:
            return datetime(2000 + y if y < 100 else y, m, d).date().isoformat()
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", value)
    if match:
        try:
            return datetime(int(match.group(3) or datetime.now().year),
                            MONTHS[match.group(2)], int(match.group(1))).date().isoformat()
        except ValueError:
            return None
    return None


def discover_pdfs(html, base_url=PAGE_URL):
    """Use links actually published on the official page, never guess filenames."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        href = urljoin(base_url, link["href"])
        label = link.get_text(" ", strip=True)
        name = unquote(urlparse(href).path.rsplit("/", 1)[-1])
        if urlparse(href).hostname not in {"rktm.info", "www.rktm.info"}:
            continue
        if not name.lower().endswith(".pdf"):
            continue
        if "замен" not in (name + " " + label).lower():
            continue
        date = document_date(name) or document_date(label)
        if date:
            found[href] = {"url": href, "date": date, "label": label or name}
    return list(found.values())


def parse_pdf(data, date):
    """Extract bordered tables; reject incomplete rows rather than invent lessons."""
    result = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                current_lesson = None
                for row in table:
                    if len(row) < 5:
                        continue
                    cells = [" ".join((cell or "").split()) for cell in row[:5]]
                    lesson, group, original, replacement, room = cells
                    if lesson.isdigit():
                        current_lesson = int(lesson)
                    if not current_lesson or not (1 <= current_lesson <= 12):
                        continue
                    if not group_key(group) or not original or not replacement:
                        continue
                    # Notes such as "АТ-15 — с 3-го урока" are not substitutions.
                    if re.search(r"с\s+\d+[- ]?го\s+урока", group, re.I):
                        continue
                    result.append({"date": date, "group": group, "lesson": current_lesson,
                                   "original": original, "replacement": replacement, "room": room})
    # The same row can be extracted twice on documents with overlapping borders.
    return list({tuple(r.values()): r for r in result}.values())


async def fetch_documents():
    async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get(PAGE_URL)
        response.raise_for_status()
        return discover_pdfs(response.text, str(response.url))


async def download_pdf(url):
    if urlparse(url).hostname not in {"rktm.info", "www.rktm.info"}:
        raise ValueError("Untrusted PDF host")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > 12_000_000 or not response.content.startswith(b"%PDF"):
            raise ValueError("Invalid or oversized PDF")
        return response.content


def digest(data):
    return hashlib.sha256(data).hexdigest()


def format_group_changes(rows, group_name, date):
    key = group_key(group_name)
    matching = sorted((r for r in rows if group_key(r["group"]) == key),
                      key=lambda r: r["lesson"]) if key else []
    if not matching:
        return ""
    return format_day_changes(matching, date)


def setup_store():
    from features import db
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS substitution_parts (
            platform TEXT NOT NULL, uid TEXT NOT NULL, day TEXT NOT NULL,
            content_hash TEXT NOT NULL, part INTEGER NOT NULL,
            PRIMARY KEY(platform,uid,day,content_hash,part)
        );
        CREATE TABLE IF NOT EXISTS substitution_documents (
            url TEXT PRIMARY KEY, day TEXT NOT NULL, digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS substitution_rows_v2 (
            url TEXT NOT NULL, day TEXT NOT NULL, group_code TEXT NOT NULL,
            lesson INTEGER NOT NULL, original TEXT NOT NULL,
            replacement TEXT NOT NULL, room TEXT NOT NULL,
            PRIMARY KEY(url,group_code,lesson,original,replacement,room)
        );
        CREATE TABLE IF NOT EXISTS substitution_migrations (name TEXT PRIMARY KEY);

        CREATE TABLE IF NOT EXISTS substitution_deliveries (
            platform TEXT NOT NULL, uid TEXT NOT NULL, day TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY(platform,uid,day)
        );
        """)
        # Additive migration preserves the legacy table for rollback. Copy once only.
        if not conn.execute("SELECT 1 FROM substitution_migrations WHERE name='rows_v2'").fetchone():
            legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='substitution_rows'").fetchone()
            if legacy:
                conn.execute("INSERT OR IGNORE INTO substitution_rows_v2 SELECT * FROM substitution_rows")
            conn.execute("INSERT INTO substitution_migrations VALUES('rows_v2')")


def substitutions_on(day):
    """All substitutions published for a date, across groups."""
    from features import db
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT day,group_code AS 'group',lesson,original,replacement,room "
            "FROM substitution_rows_v2 WHERE day=? ORDER BY group_code,lesson,original,replacement,room", (day,)
        ).fetchall()
    return [dict(r) for r in rows]


def teacher_label(text):
    names = re.findall(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.", text)
    return ", ".join(names) or "не указан в PDF"


def display_date(day):
    try:
        return datetime.fromisoformat(day).strftime("%d.%m.%Y")
    except ValueError:
        return day


def format_day_changes(rows, day):
    if not rows:
        return f"🔄 Замены на {display_date(day)}\n\nОпубликованных изменений пока нет."
    parts = [f"🔄 Замены на {display_date(day)}"]
    for r in rows:
        parts.append(f"🎓 {r['group']} · Урок {r['lesson']}\n"
                     f"Было: {r['original']}\nСтало: {r['replacement']}"
                     f"\n👨‍🏫 Преподаватель: {teacher_label(r['replacement'])}"
                     + f"\n🏫 Кабинет: {r['room'] or 'не указан в PDF'}")
    return "\n\n".join(parts)


def substitutions_for(group_name, day):
    from features import db
    key = group_key(group_name)
    if not key:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT day,group_code AS 'group',lesson,original,replacement,room "
            "FROM substitution_rows_v2 WHERE day=? ORDER BY lesson,original,replacement,room", (day,)
        ).fetchall()
    return [dict(r) for r in rows if group_key(r["group"]) == key]


def retire_superseded(day, documents):
    from features import db
    active = {d["url"] for d in documents if d["date"] == day}
    with db() as conn:
        for previous in conn.execute("SELECT url FROM substitution_documents WHERE day=?", (day,)).fetchall():
            if previous["url"] not in active:
                conn.execute("DELETE FROM substitution_rows_v2 WHERE url=?", (previous["url"],))


async def refresh():
    """Return changed dates; store only successfully parsed documents."""
    from features import db
    setup_store()
    changed = set()
    documents = await fetch_documents()
    for doc in documents:
        try:
            data = await download_pdf(doc["url"])
            checksum = digest(data)
            with db() as conn:
                old = conn.execute("SELECT digest,day FROM substitution_documents WHERE url=?",
                                   (doc["url"],)).fetchone()
                has_rows = conn.execute("SELECT 1 FROM substitution_rows_v2 WHERE url=? LIMIT 1", (doc["url"],)).fetchone()
            if old and has_rows and old["digest"] == checksum and old["day"] == doc["date"]:
                retire_superseded(doc["date"], documents)
                continue
            rows = await asyncio.to_thread(parse_pdf, data, doc["date"])
            if not rows:
                continue  # Never erase known data on an unrecognized PDF.
            with db() as conn:
                conn.execute("DELETE FROM substitution_rows_v2 WHERE url=?", (doc["url"],))
                conn.executemany(
                    "INSERT INTO substitution_rows_v2 VALUES(?,?,?,?,?,?,?)",
                    [(doc["url"], r["date"], r["group"], r["lesson"],
                      r["original"], r["replacement"], r["room"]) for r in rows]
                )
                conn.execute(
                    "INSERT INTO substitution_documents VALUES(?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET day=excluded.day,digest=excluded.digest",
                    (doc["url"], doc["date"], checksum))
            retire_superseded(doc["date"], documents)
            changed.add(doc["date"])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Could not process substitution PDF: %s", doc["url"])
    return changed


def published_dates():
    from features import db
    setup_store()
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM substitution_documents ORDER BY day DESC")]


def teacher_key(value):
    return re.sub(r"[^а-яa-z]", "", (value or "").lower().replace("ё", "е"))


def teacher_matches(name, text):
    # Catalog uses surname + initials; require a complete surname, not a substring.
    name = (name or "").replace("ё", "е").replace("Ё", "Е")
    text = text.replace("ё", "е").replace("Ё", "Е")
    tokens = re.findall(r"[А-ЯЁа-яё]+", name)
    if not tokens:
        return False
    surname = tokens[0]
    for match in re.finditer(r"(?<![А-ЯЁа-яё])" + re.escape(surname) +
                             r"(?![А-ЯЁа-яё])(?:\s+([А-ЯЁ])\.?\s*([А-ЯЁ])\.?)?",
                             text, re.I):
        initials = "".join(t[0] for t in tokens[1:3])
        found = "".join(x or "" for x in match.groups())
        if not initials or not found or teacher_key(initials) == teacher_key(found):
            return True
    return False


def personal_rows(person, day):
    title = person.get("group_name", person.get("title", ""))
    if person["source"] == "Praspisan":
        return [r for r in substitutions_on(day)
                if teacher_matches(title, r["original"]) or teacher_matches(title, r["replacement"])]
    if person["source"] == "raspisan":
        return substitutions_for(title, day)
    return []


def personal_message(person, day):
    rows = personal_rows(person, day)
    return format_day_changes(rows, day) if rows else ""


def personal_overview(person):
    today = datetime.now(ZoneInfo(os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow"))).date().isoformat()
    return "\n\n".join(filter(None, (personal_message(person, day)
        for day in sorted(published_dates()) if day >= today))) or "Для тебя пока нет опубликованных замен."


def message_parts(message, limit=3500):
    while message:
        end = min(len(message), limit)
        if len(message) > limit:
            boundary = message.rfind("\n", 0, limit)
            end = boundary if boundary > 0 else limit
        yield message[:end]
        message = message[end:].lstrip("\n")


def notification_fingerprint(person, day, rows):
    title = person["group_name"]
    identity = teacher_key(title) if person["source"] == "Praspisan" else group_key(title)
    payload = {"day":day, "source":person["source"], "identity":identity,
               "rows":sorted([dict(row) for row in rows], key=lambda row: json.dumps(row,sort_keys=True,ensure_ascii=False))}
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())


def legacy_notification(rows, day):
    """Recognize successful deliveries from versions before semantic fingerprints."""
    if not rows:
        return f"🔄 Замены на {day}: ранее опубликованные замены для тебя отменены."
    parts = [f"🔄 Замены на {day}"]
    for row in rows:
        parts.append(f"🎓 {row['group']} · {row['lesson']}-й урок\nБыло: {row['original']}\n"
            f"Стало: {row['replacement']}\n👨‍🏫 Преподаватель: {teacher_label(row['replacement'])}"
            + (f"\n🏫 Кабинет: {row['room']}" if row['room'] else ""))
    return "\n\n".join(parts)


def legacy_notification_hashes(person, rows, day):
    hashes = {digest(legacy_notification(rows, day).encode())}
    if rows and person["source"] == "raspisan":
        parts = [f"🔔 Замены на {day}\n🎓 {person['group_name']}"]
        for row in sorted(rows, key=lambda row: row["lesson"]):
            parts.append(f"🔄 {row['lesson']}-й урок\nБыло: {row['original']}\nСтало: {row['replacement']}"
                + (f"\n🏫 {row['room']}" if row['room'] else ""))
        hashes.add(digest("\n\n".join(parts).encode()))
    return hashes


async def notify_changed(days, telegram_bot=None):
    """Retry undelivered current versions each cycle; mark only successful sends."""
    from features import db
    setup_store()
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    with db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT * FROM users WHERE notifications=1 AND group_name != ''")]
    for day in sorted(days):
        for person in users:
            with db() as conn:
                active = conn.execute("SELECT notifications FROM users WHERE platform=? AND user_id=?",
                    (person["platform"], str(person["user_id"]))).fetchone()
            if not active or not active["notifications"]:
                continue
            message = personal_message(person, day)
            with db() as conn:
                old = conn.execute(
                    "SELECT content_hash FROM substitution_deliveries WHERE platform=? AND uid=? AND day=?",
                    (person["platform"], str(person["user_id"]), day)).fetchone()
            if not message:
                if not old:
                    continue
                message = f"🔄 Замены на {day}: ранее опубликованные замены для тебя отменены."
            rows = personal_rows(person, day)
            checksum = notification_fingerprint(person, day, rows)
            part_hash = digest(message.encode())
            if old and old["content_hash"] in ({checksum} | legacy_notification_hashes(person,rows,day)):
                if old["content_hash"] != checksum:
                    with db() as conn:
                        conn.execute("UPDATE substitution_deliveries SET content_hash=? WHERE platform=? AND uid=? AND day=?",
                                     (checksum,person["platform"],str(person["user_id"]),day))
                continue
            try:
                for index, part in enumerate(message_parts(message)):
                    with db() as conn:
                        sent = conn.execute("SELECT 1 FROM substitution_parts WHERE platform=? AND uid=? "
                            "AND day=? AND content_hash=? AND part=?",
                            (person["platform"], str(person["user_id"]), day, part_hash, index)).fetchone()
                    if sent:
                        continue
                    if person["platform"] == "telegram" and telegram_bot:
                        await telegram_bot.send_message(int(person["user_id"]), part)
                    elif person["platform"] == "max" and token:
                        async with httpx.AsyncClient(base_url="https://platform-api2.max.ru",
                            headers={"Authorization": token}, timeout=20) as client:
                            response = await client.post("/messages", params={"user_id": int(person["user_id"])},
                                                         json={"text": part})
                            response.raise_for_status()
                            if response.json().get("success") is False:
                                raise ValueError("MAX rejected notification")
                    else:
                        break
                    with db() as conn:
                        conn.execute("INSERT OR IGNORE INTO substitution_parts VALUES(?,?,?,?,?)",
                            (person["platform"], str(person["user_id"]), day, part_hash, index))
                    await asyncio.sleep(0.05)
                else:
                    with db() as conn:
                        conn.execute("INSERT INTO substitution_deliveries VALUES(?,?,?,?) "
                            "ON CONFLICT(platform,uid,day) DO UPDATE SET content_hash=excluded.content_hash",
                            (person["platform"], str(person["user_id"]), day, checksum))
                        conn.execute("DELETE FROM substitution_parts WHERE platform=? AND uid=? AND day=?",
                            (person["platform"], str(person["user_id"]), day))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Substitution notification failed")


async def monitor(telegram_bot=None):
    """One monitor per database/process host; MAX and web may both start safely."""
    from features import DB_PATH
    import logging
    setup_store()
    lock = open(str(DB_PATH) + ".substitutions.lock", "a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logging.getLogger(__name__).info("Another substitution monitor owns this database")
            return
        while True:
            try:
                await refresh()
            except Exception:
                logging.getLogger(__name__).exception("Substitution discovery failed")
            try:
                today = datetime.now(ZoneInfo(os.getenv("SCHEDULE_TIMEZONE", "Europe/Moscow"))).date().isoformat()
                await notify_changed([d for d in published_dates() if d >= today], telegram_bot)
            except Exception:
                logging.getLogger(__name__).exception("Substitution delivery failed")
            await asyncio.sleep(300)
    finally:
        lock.close()
