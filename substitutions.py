"""Automatic college PDF substitution discovery and safe per-group extraction."""
import hashlib
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
    value = re.sub(r"\s+", "", name or "").upper().replace("Ё", "Е")
    match = GROUP_RE.search(value)
    return (match.group(1).replace("Ё", "Е"), match.group(2)) if match else None


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
    return list({(r["date"], r["group"], r["lesson"]): r for r in result}.values())


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
    parts = [f"🔔 Замены на {date}\n🎓 {group_name}"]
    for r in matching:
        parts.append(f"🔄 {r['lesson']}-й урок\nБыло: {r['original']}\n"
                     f"Стало: {r['replacement']}" + (f"\n🏫 {r['room']}" if r["room"] else ""))
    return "\n\n".join(parts)


def setup_store():
    from features import db
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS substitution_documents (
            url TEXT PRIMARY KEY, day TEXT NOT NULL, digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS substitution_rows (
            url TEXT NOT NULL, day TEXT NOT NULL, group_code TEXT NOT NULL,
            lesson INTEGER NOT NULL, original TEXT NOT NULL,
            replacement TEXT NOT NULL, room TEXT NOT NULL,
            PRIMARY KEY(url,group_code,lesson)
        );
        CREATE TABLE IF NOT EXISTS substitution_deliveries (
            platform TEXT NOT NULL, uid TEXT NOT NULL, day TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY(platform,uid,day)
        );
        """)


def substitutions_for(group_name, day):
    from features import db
    key = group_key(group_name)
    if not key:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT day,group_code AS 'group',lesson,original,replacement,room "
            "FROM substitution_rows WHERE day=? ORDER BY lesson", (day,)
        ).fetchall()
    return [dict(r) for r in rows if group_key(r["group"]) == key]


async def refresh():
    """Return changed dates; store only successfully parsed documents."""
    from features import db
    setup_store()
    changed = set()
    for doc in await fetch_documents():
        try:
            data = await download_pdf(doc["url"])
            checksum = digest(data)
            with db() as conn:
                old = conn.execute("SELECT digest FROM substitution_documents WHERE url=?",
                                   (doc["url"],)).fetchone()
            if old and old["digest"] == checksum:
                continue
            rows = parse_pdf(data, doc["date"])
            if not rows:
                continue  # Never erase known data on an unrecognized PDF.
            with db() as conn:
                conn.execute("DELETE FROM substitution_rows WHERE url=?", (doc["url"],))
                conn.executemany(
                    "INSERT INTO substitution_rows VALUES(?,?,?,?,?,?,?)",
                    [(doc["url"], r["date"], r["group"], r["lesson"],
                      r["original"], r["replacement"], r["room"]) for r in rows]
                )
                conn.execute(
                    "INSERT INTO substitution_documents VALUES(?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET day=excluded.day,digest=excluded.digest",
                    (doc["url"], doc["date"], checksum))
            changed.add(doc["date"])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Could not process substitution PDF: %s", doc["url"])
    return changed


async def notify_changed(days, telegram_bot=None):
    """Send a changed group-specific digest once per version per platform."""
    import os
    from features import db
    from html import escape
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    with db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT platform,user_id,group_name FROM users WHERE notifications=1 AND group_name != ''")]
        max_users = [dict(r) for r in conn.execute(
            "SELECT uid AS user_id,title AS group_name,'max' AS platform "
            "FROM max_profiles WHERE filename IS NOT NULL AND role='student'")]
    users += max_users
    for day in days:
        for person in users:
            rows = substitutions_for(person["group_name"], day)
            message = format_group_changes(rows, person["group_name"], day)
            if not message:
                continue
            checksum = digest(message.encode())
            with db() as conn:
                old = conn.execute(
                    "SELECT content_hash FROM substitution_deliveries "
                    "WHERE platform=? AND uid=? AND day=?",
                    (person["platform"], str(person["user_id"]), day)).fetchone()
            if old and old["content_hash"] == checksum:
                continue
            try:
                if person["platform"] == "telegram" and telegram_bot:
                    await telegram_bot.send_message(int(person["user_id"]), message)
                elif person["platform"] == "max" and token:
                    async with httpx.AsyncClient(
                        base_url="https://platform-api2.max.ru",
                        headers={"Authorization": token}, timeout=20
                    ) as client:
                        response = await client.post(
                            "/messages", params={"user_id": int(person["user_id"])},
                            json={"text": message})
                        response.raise_for_status()
                else:
                    continue
                with db() as conn:
                    conn.execute(
                        "INSERT INTO substitution_deliveries VALUES(?,?,?,?) "
                        "ON CONFLICT(platform,uid,day) DO UPDATE SET content_hash=excluded.content_hash",
                        (person["platform"], str(person["user_id"]), day, checksum))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Substitution notification failed")
