"""Persistent news, settings, change detection and platform-neutral services."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv('DATABASE_PATH', str(Path(__file__).parent / 'data' / 'rktm.db')))

@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()

def initialize():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (platform TEXT NOT NULL, user_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'raspisan', filename TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '', notifications INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(platform,user_id));
        CREATE TABLE IF NOT EXISTS news (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (source TEXT NOT NULL, filename TEXT NOT NULL, digest TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(source,filename));
        CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, user_id TEXT NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, url TEXT NOT NULL);
        ''')

def save_user(platform, user_id, source, filename, group_name, notifications=False):
    with db() as c:
        c.execute('''INSERT INTO users VALUES (?,?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET source=excluded.source,filename=excluded.filename,group_name=excluded.group_name,notifications=excluded.notifications''', (platform,str(user_id),source,filename,group_name,int(notifications)))

def user(platform, user_id):
    with db() as c:
        row=c.execute('SELECT * FROM users WHERE platform=? AND user_id=?',(platform,str(user_id))).fetchone()
        return dict(row) if row else None

def news_list():
    with db() as c:
        return [dict(x) for x in c.execute('SELECT * FROM news ORDER BY id DESC LIMIT 30')]

def add_news(title,body):
    with db() as c:
        c.execute('INSERT INTO news(title,body,created_at) VALUES (?,?,?)',(title,body,int(time.time())))

def add_feedback(platform,user_id,message):
    with db() as c:
        c.execute('INSERT INTO feedback(platform,user_id,message,created_at) VALUES (?,?,?,?)',(platform,str(user_id),message,int(time.time())))

def digest_html(html):
    # Normalize whitespace to avoid false positives caused by formatting changes.
    import re
    return hashlib.sha256(re.sub(r'\s+',' ',html).encode()).hexdigest()

def compare_snapshot(source, filename, html):
    digest=digest_html(html)
    with db() as c:
        old=c.execute('SELECT digest FROM snapshots WHERE source=? AND filename=?',(source,filename)).fetchone()
        if old and old['digest']==digest: return False
        c.execute('INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(source,filename) DO UPDATE SET digest=excluded.digest,updated_at=excluded.updated_at',(source,filename,digest,int(time.time())))
        return old is not None

def subscribers(source, filename):
    with db() as c:
        return [dict(x) for x in c.execute('SELECT * FROM users WHERE source=? AND filename=? AND notifications=1',(source,filename))]

def admin_authorized(token):
    expected=os.getenv('ADMIN_TOKEN','')
    return bool(expected) and hmac.compare_digest(token,expected)
