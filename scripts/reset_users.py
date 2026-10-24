"""One-time irreversible purge of all stored user records.

Run only after stopping the service:
    .venv/bin/python scripts/reset_users.py --confirm-delete-all-users

Does not delete news or schedule snapshots.
"""
import argparse
import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DB = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "rktm.db")))

parser = argparse.ArgumentParser()
parser.add_argument("--confirm-delete-all-users", action="store_true")
args = parser.parse_args()
if not args.confirm_delete_all_users:
    parser.error("Refusing to delete without --confirm-delete-all-users")
if not DB.is_file():
    parser.error("Database not found: " + str(DB))
with sqlite3.connect(DB) as conn:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in ("users", "bot_roles", "bot_states", "approved_teachers", "mini_roles", "feedback", "bot_accounts", "bot_favorites", "bot_pending"):
        if name in tables:
            count=conn.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0]
            conn.execute('DELETE FROM "'+name+'"')
            print(name, count, "deleted")
print("User records cleared from", DB)
