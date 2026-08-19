"""
Storage — one SQLite file at data/app.db.

The schema isn't written out by hand: it's built from modules.py every time
the app boots. Missing tables are created, missing columns are added. That
means editing modules.py is a safe, complete way to change this app.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import modules

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "app.db")

# logical field type -> SQLite type. Anything not listed is TEXT.
SQL_TYPE = {"money": "REAL", "number": "REAL", "check": "INTEGER"}


def _decl(field) -> str:
    return SQL_TYPE.get(field["type"], "TEXT")


def get_conn() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    # small key/value scratchpad the app keeps for itself -- e.g. which
    # finished wishlist items have already been announced, so the "we fixed
    # this" note only ever appears once
    conn.execute("CREATE TABLE IF NOT EXISTS app_state ("
                 "key TEXT PRIMARY KEY, value TEXT)")
    # One row per signed-in device. Only used when this copy is hosted (see
    # app.py) -- a copy on someone's own PC never has any rows here.
    #
    # The cookie holds a random token and NOTHING else, so it is worth nothing
    # away from this database: signing a device out is deleting its row, which
    # is what makes "I lost my phone" a one-click job rather than a password
    # change that knocks every other device out too.
    conn.execute("CREATE TABLE IF NOT EXISTS sessions ("
                 "token TEXT PRIMARY KEY, label TEXT, created_at TEXT, "
                 "last_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS login_attempts ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, at TEXT)")
    for m in modules.MODULES:
        cols = ", ".join("%s %s" % (f["name"], _decl(f)) for f in m["fields"])
        conn.execute(
            "CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "%s, created_at TEXT)" % (m["table"], cols))
        # a field added to modules.py after this app shipped -- add the column
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % m["table"])}
        for f in m["fields"]:
            if f["name"] not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                             % (m["table"], f["name"], _decl(f)))
    conn.commit()
    conn.close()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def insert(table: str, **fields) -> int:
    cols = ", ".join(fields.keys())
    marks = ", ".join("?" for _ in fields)
    conn = get_conn()
    cur = conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, cols, marks),
                       tuple(fields.values()))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def update(table: str, row_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join("%s = ?" % k for k in fields)
    conn = get_conn()
    conn.execute("UPDATE %s SET %s WHERE id = ?" % (table, sets),
                 tuple(fields.values()) + (row_id,))
    conn.commit()
    conn.close()


def delete(table: str, row_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))
    conn.commit()
    conn.close()


def write(sql: str, params: tuple = ()) -> int:
    """An UPDATE or DELETE that isn't by row id. `query` does NOT commit, so
    running one of these through it looks like it worked and changes nothing."""
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def query(sql: str, params: tuple = ()) -> list:
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def query_one(sql: str, params: tuple = ()):
    conn = get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


if __name__ == "__main__":
    init_db()
    print("Initialized", DB_PATH)
