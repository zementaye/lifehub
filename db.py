import base64
import sqlite3
import time
from contextlib import contextmanager

import requests

import config

USE_TURSO = bool(config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN)

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    height_cm REAL
);

CREATE TABLE IF NOT EXISTS weight_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_foods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    calories REAL NOT NULL DEFAULT 0,
    protein_g REAL NOT NULL DEFAULT 0,
    carbs_g REAL NOT NULL DEFAULT 0,
    fat_g REAL NOT NULL DEFAULT 0,
    fiber_g REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS food_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    custom_food_id INTEGER,
    name TEXT NOT NULL,
    servings REAL NOT NULL DEFAULT 1,
    calories REAL NOT NULL DEFAULT 0,
    protein_g REAL NOT NULL DEFAULT 0,
    carbs_g REAL NOT NULL DEFAULT 0,
    fat_g REAL NOT NULL DEFAULT 0,
    fiber_g REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    filename TEXT NOT NULL,
    notes TEXT,
    expiry_date TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    next_due TEXT NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'once',
    active INTEGER NOT NULL DEFAULT 1,
    last_sent TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'daily',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS habit_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    period_key TEXT NOT NULL,
    done_at REAL NOT NULL,
    UNIQUE(habit_id, period_key)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS budget_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    monthly_limit REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,           -- 'income' or 'expense'
    category_id INTEGER,          -- NULL allowed, especially for income
    description TEXT,
    amount REAL NOT NULL,         -- always stored positive
    created_at REAL NOT NULL,
    FOREIGN KEY (category_id) REFERENCES budget_categories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS recurring_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,          -- e.g. a job name ("Design job salary") or a bill ("Rent")
    type TEXT NOT NULL,           -- 'income' or 'expense'
    amount REAL NOT NULL,
    category_id INTEGER,          -- expenses only
    day_of_month INTEGER NOT NULL DEFAULT 1,  -- 1-28, which day it posts each month
    next_run TEXT NOT NULL,       -- ISO date of the next auto-log
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (category_id) REFERENCES budget_categories(id) ON DELETE SET NULL
);
"""

# (table, column, sqlite type) — added after initial release, applied via ALTER TABLE
# to existing databases that predate the column.
_MIGRATIONS = [
    ("documents", "expiry_date", "TEXT"),
]


def _to_turso_arg(value):
    """Convert a Python value into Turso's typed HTTP-API arg format."""
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        return {"type": "blob", "value": base64.b64encode(value).decode("ascii")}
    return {"type": "text", "value": str(value)}


def _from_turso_cell(cell):
    """Convert a Turso HTTP-API result cell back into a plain Python value."""
    if cell is None or cell.get("type") == "null":
        return None
    t = cell.get("type")
    v = cell.get("value")
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    if t == "blob":
        return base64.b64decode(v)
    return v  # text


class _Row(dict):
    """dict subclass so row['col'] works like sqlite3.Row. Jinja's attribute
    lookup (row.col) falls back to __getitem__ when getattr fails, so
    templates that use dot-notation on rows keep working unchanged too."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)


class _TursoCursor:
    """Mimics a sqlite3 cursor's read interface on top of a parsed Turso
    HTTP-API result, so callers can keep using fetchone()/fetchall()/
    iteration exactly as they would with sqlite3."""

    def __init__(self, cols, rows, lastrowid=None):
        self._cols = cols
        self._rows = rows
        self.lastrowid = lastrowid

    def _wrap(self, raw_row):
        return _Row(zip(self._cols, raw_row))

    def fetchone(self):
        return self._wrap(self._rows[0]) if self._rows else None

    def fetchall(self):
        return [self._wrap(r) for r in self._rows]

    def __iter__(self):
        return iter(self.fetchall())


class _TursoHttpConn:
    """Talks to Turso over its plain HTTP API (POST /v2/pipeline) instead of
    the `libsql` Python package. The `libsql` package wraps a Rust/Tokio
    async runtime, and creating/using those connections inside a gunicorn
    worker (which also runs the APScheduler background thread) caused
    repeated deadlocks ("failed to join thread: Resource deadlock avoided")
    that crashed the whole process. Plain HTTP requests have none of that —
    each call is just a stateless POST, so there's nothing to deadlock and
    no shared connection object to worry about across threads."""

    def __init__(self, database_url: str, auth_token: str):
        # database_url looks like libsql://name-org.turso.io — the HTTP API
        # is served over https on the same host.
        https_url = database_url.replace("libsql://", "https://", 1)
        self._url = https_url.rstrip("/") + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def execute(self, sql, params=()):
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": [_to_turso_arg(p) for p in params]}},
                {"type": "close"},
            ]
        }
        resp = requests.post(self._url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        exec_result = data["results"][0]
        if exec_result.get("type") == "error":
            err = exec_result.get("error", {})
            raise RuntimeError(f"Turso error: {err.get('message', err)}")

        result = exec_result["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = [
            [_from_turso_cell(cell) for cell in row]
            for row in result.get("rows", [])
        ]
        lastrowid = result.get("last_insert_rowid")
        return _TursoCursor(cols, rows, int(lastrowid) if lastrowid else None)

    def executescript(self, script):
        # Run statements one at a time over HTTP; fine here since SCHEMA has
        # no ';' inside string literals.
        for stmt in filter(None, (s.strip() for s in script.split(";"))):
            self.execute(stmt)

    def commit(self):
        pass  # each HTTP call already autocommits; nothing to do

    def close(self):
        pass  # stateless — nothing to keep open between calls


@contextmanager
def get_conn():
    if USE_TURSO:
        conn = _TursoHttpConn(config.TURSO_DATABASE_URL, config.TURSO_AUTH_TOKEN)
        yield conn
        conn.commit()
    else:
        raw_conn = sqlite3.connect(config.DB_PATH)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield raw_conn
            raw_conn.commit()
        finally:
            raw_conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO profile (id, height_cm) VALUES (1, NULL)")
        for table, column, coltype in _MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def now() -> float:
    return time.time()


def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def delete_setting(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
