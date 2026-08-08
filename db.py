import base64
import secrets
import sqlite3
import time
from contextlib import contextmanager

import requests
from werkzeug.security import generate_password_hash, check_password_hash

import config

USE_TURSO = bool(config.TURSO_DATABASE_URL and config.TURSO_AUTH_TOKEN)

# Reused across every Turso HTTP call for the life of the process, so the
# underlying TCP/TLS connection stays alive (keep-alive) instead of doing a
# fresh handshake on every single query — each query is a real network round
# trip once the DB is remote, so this matters a lot for perceived speed.
_http_session = requests.Session()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    used_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    used_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    height_cm REAL,
    birth_date TEXT,      -- used to compute age for recommended intake
    sex TEXT              -- 'male' or 'female', used for the BMR formula
);

CREATE TABLE IF NOT EXISTS weight_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_foods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
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
    user_id INTEGER,
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    custom_food_id INTEGER,
    name TEXT NOT NULL,
    meal TEXT NOT NULL DEFAULT 'snack',  -- 'breakfast' | 'lunch' | 'dinner' | 'snack'
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
    user_id INTEGER,
    label TEXT NOT NULL,
    filename TEXT NOT NULL,
    notes TEXT,
    expiry_date TEXT,
    expiry_ack_date TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT NOT NULL,
    next_due TEXT NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'once',
    active INTEGER NOT NULL DEFAULT 1,
    last_sent TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'daily',
    reminder_hour INTEGER,        -- optional per-habit reminder time (0-23); NULL = covered by the shared evening digest instead
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

CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER,
    key TEXT NOT NULL,
    value TEXT
);

CREATE TABLE IF NOT EXISTS budget_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    monthly_limit REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
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
    user_id INTEGER,
    title TEXT NOT NULL,          -- e.g. a job name ("Design job salary") or a bill ("Rent")
    type TEXT NOT NULL,           -- 'income' or 'expense'
    amount REAL NOT NULL,
    category_id INTEGER,          -- expenses only
    frequency TEXT NOT NULL DEFAULT 'monthly',  -- 'monthly' or 'weekly'
    day_of_month INTEGER NOT NULL DEFAULT 1,  -- 1-28, used when frequency = 'monthly'
    day_of_week INTEGER,          -- 0=Mon .. 6=Sun, used when frequency = 'weekly'
    next_run TEXT NOT NULL,       -- ISO date of the next auto-log
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (category_id) REFERENCES budget_categories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,              -- e.g. "Emergency fund", "New laptop"
    target_amount REAL,              -- optional, NULL = open-ended jar with no target
    target_date TEXT,                -- optional date to hit the target by
    current_amount REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    amount REAL NOT NULL,            -- positive = deposit, negative = withdrawal
    transaction_id INTEGER,          -- the matching row this created in `transactions`
    created_at REAL NOT NULL,
    FOREIGN KEY (goal_id) REFERENCES savings_goals(id) ON DELETE CASCADE,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS passwords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    label TEXT NOT NULL,          -- e.g. "Gmail", "Netflix"
    username TEXT,
    password_enc TEXT NOT NULL,   -- encrypted via crypto.py, never stored plain
    url TEXT,
    notes TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT NOT NULL,
    body TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# (table, column, sqlite type) — added after initial release, applied via ALTER TABLE
# to existing databases that predate the column. This is also how every
# table above picked up its `user_id` column on a database that already
# existed from before multi-user support: user_id is deliberately nullable
# here (a plain ALTER TABLE ADD COLUMN can't add a NOT NULL column to a
# table that already has rows), and any resulting NULL rows are adopted by
# the first person who registers — see claim_orphan_data() below.
_MIGRATIONS = [
    ("documents", "expiry_date", "TEXT"),
    ("documents", "expiry_ack_date", "TEXT"),
    ("recurring_transactions", "frequency", "TEXT NOT NULL DEFAULT 'monthly'"),
    ("recurring_transactions", "day_of_week", "INTEGER"),
    ("habits", "reminder_hour", "INTEGER"),
    ("savings_goals", "target_date", "TEXT"),
    ("profile", "birth_date", "TEXT"),
    ("profile", "sex", "TEXT"),
    ("food_log", "meal", "TEXT NOT NULL DEFAULT 'snack'"),
    ("profile", "user_id", "INTEGER"),
    ("weight_entries", "user_id", "INTEGER"),
    ("sessions", "user_id", "INTEGER"),
    ("custom_foods", "user_id", "INTEGER"),
    ("food_log", "user_id", "INTEGER"),
    ("documents", "user_id", "INTEGER"),
    ("reminders", "user_id", "INTEGER"),
    ("habits", "user_id", "INTEGER"),
    ("todos", "user_id", "INTEGER"),
    ("settings", "user_id", "INTEGER"),
    ("budget_categories", "user_id", "INTEGER"),
    ("transactions", "user_id", "INTEGER"),
    ("recurring_transactions", "user_id", "INTEGER"),
    ("savings_goals", "user_id", "INTEGER"),
    ("passwords", "user_id", "INTEGER"),
    ("notes", "user_id", "INTEGER"),
    ("users", "email_verified_at", "REAL"),
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
]

# Tables that carry a user_id column and get scanned for orphaned
# (user_id IS NULL) rows when the very first account is created.
_USER_SCOPED_TABLES = [
    "profile", "weight_entries", "sessions", "custom_foods", "food_log",
    "documents", "reminders", "habits", "todos", "settings",
    "budget_categories", "transactions", "recurring_transactions",
    "savings_goals", "passwords", "notes",
]

# These four tables carried a table-level constraint in the old single-user
# schema that only makes sense with exactly one user in the whole database:
#   - profile: PRIMARY KEY id CHECK (id = 1)      → only one row, ever
#   - settings: PRIMARY KEY (key)                  → one value per key, globally
#   - budget_categories: name TEXT ... UNIQUE       → one "Food" category, globally
#   - savings_goals: name TEXT ... UNIQUE           → one "Emergency Fund" goal, globally
# A plain `ALTER TABLE ADD COLUMN user_id` (used for every other table below)
# can't remove a constraint like that — SQLite has no ALTER TABLE DROP
# CONSTRAINT. So on a database that already has one of these tables in its
# old shape, it's renamed aside, rebuilt from the corresponding CREATE TABLE
# in SCHEMA (which already has no such constraint), and its rows are copied
# back in with user_id left NULL — to be claimed by the first person who
# registers, same as every other pre-existing row.
_LEGACY_REBUILD = ("profile", "settings", "budget_categories", "savings_goals")


def _extract_create_table(schema_sql: str, table: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = schema_sql.index(marker)
    end = schema_sql.index(");", start) + 2
    return schema_sql[start:end]


def _rebuild_legacy_table(conn, table: str) -> None:
    """Renames the old-shape table aside, creates the current-shape table in
    its place, and copies data across column-by-column — only for columns
    that exist in BOTH the legacy table (which may or may not have every
    optional column, depending on which migrations it already picked up
    over time) and the new one. Any new-only column (like user_id) is left
    to its default (NULL), same as a fresh ADD COLUMN would do."""
    legacy_name = f"{table}_legacy"
    old_cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]

    conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_name}")
    conn.execute(_extract_create_table(SCHEMA, table))

    new_cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    shared = [c for c in old_cols if c in new_cols]
    col_list = ", ".join(shared)
    conn.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {legacy_name}")
    conn.execute(f"DROP TABLE {legacy_name}")


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
        resp = _http_session.post(self._url, json=payload, headers=self._headers, timeout=15)
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
        # Run statements one at a time over HTTP. Strip '-- comment' text
        # from each line first — a naive split on ';' would otherwise break
        # a statement in half if any comment happens to contain a
        # semicolon (this bit us once already).
        cleaned_lines = []
        for line in script.split("\n"):
            if "--" in line:
                line = line[: line.index("--")]
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)
        for stmt in filter(None, (s.strip() for s in cleaned.split(";"))):
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
        # Must run BEFORE the SCHEMA script below: CREATE TABLE IF NOT EXISTS
        # is a no-op on a table that already exists in its old single-user
        # shape, so a constraint like settings' PRIMARY KEY (key) would
        # otherwise survive untouched.
        for table in _LEGACY_REBUILD:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if existing and "user_id" not in existing:
                _rebuild_legacy_table(conn, table)

        # Snapshot BEFORE the migration loop below adds the column, so we
        # can tell whether this is a fresh install (users table doesn't
        # exist yet — nothing to backfill) vs. an existing database that
        # predates email verification.
        users_cols_before = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}

        conn.executescript(SCHEMA)
        for table, column, coltype in _MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

        if users_cols_before and "email_verified_at" not in users_cols_before:
            # Email verification is a new feature — grandfather in every
            # account that already existed before it shipped, rather than
            # retroactively locking already-active users out. Only accounts
            # created from this point forward go through real verification.
            conn.execute("UPDATE users SET email_verified_at = created_at WHERE email_verified_at IS NULL")


def now() -> float:
    return time.time()


# ── Users / auth ─────────────────────────────────────────────────────────

def create_user(email: str, password: str):
    """Creates the account, hashing the password. If this is the very first
    account ever created, any pre-existing single-user data (rows with
    user_id IS NULL, left over from before multi-user support) is
    automatically claimed by this new account. Returns the new user_id, or
    None if the email is already taken by a *verified* account.

    If the email belongs to an existing but never-verified account (e.g. an
    earlier signup that was abandoned or crashed before verification), that
    account is reclaimed: the password is reset to the newly submitted one
    and its user_id is returned as if it were newly created. This avoids
    permanently locking an email out of signup just because a first attempt
    never got verified.
    """
    email = email.strip().lower()
    password_hash = generate_password_hash(password)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, email_verified_at FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            if existing["email_verified_at"] is not None:
                return None
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, existing["id"]),
            )
            return existing["id"]
        is_first_user = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, password_hash, now()),
        )
        user_id = cur.lastrowid
        if is_first_user:
            for table in _USER_SCOPED_TABLES:
                conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (user_id,))
            # The very first account on a deployment is automatically the
            # admin — there's no one else yet to grant that from a UI, so
            # someone has to start with it or the admin panel is unreachable.
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
    return user_id


def get_user_by_email(email: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def verify_password(user_row, password: str) -> bool:
    return check_password_hash(user_row["password_hash"], password)


def set_password(user_id: int, password: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )


def create_password_reset(user_id: int, ttl_seconds: int = 3600) -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO password_resets (user_id, token, expires_at, created_at) VALUES (?,?,?,?)",
            (user_id, token, now() + ttl_seconds, now()),
        )
    return token


def get_password_reset(token: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM password_resets WHERE token = ?", (token,)
        ).fetchone()


def use_password_reset(token: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE password_resets SET used_at = ? WHERE token = ?", (now(), token))


def is_email_verified(user_row) -> bool:
    return user_row["email_verified_at"] is not None


def mark_email_verified(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET email_verified_at = ? WHERE id = ? AND email_verified_at IS NULL",
            (now(), user_id),
        )


def create_email_verification(user_id: int, ttl_seconds: int = 60 * 60 * 48) -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO email_verifications (user_id, token, expires_at, created_at) VALUES (?,?,?,?)",
            (user_id, token, now() + ttl_seconds, now()),
        )
    return token


def get_email_verification(token: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM email_verifications WHERE token = ?", (token,)
        ).fetchone()


def use_email_verification(token: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE email_verifications SET used_at = ? WHERE token = ?", (now(), token))


# ── Settings (per-user) ──────────────────────────────────────────────────

def get_setting(user_id: int, key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key)
        ).fetchone()
        if row is None or row["value"] is None:
            return default
        return row["value"]


def set_setting(user_id: int, key: str, value) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM settings WHERE user_id = ? AND key = ?", (user_id, key)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE settings SET value = ? WHERE user_id = ? AND key = ?",
                (str(value), user_id, key),
            )
        else:
            conn.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                (user_id, key, str(value)),
            )


def delete_setting(user_id: int, key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE user_id = ? AND key = ?", (user_id, key))


# ── Admin ────────────────────────────────────────────────────────────────
# Everything below powers /admin (app.py). All of it assumes the caller has
# already checked the requesting user is an admin — these functions don't
# gate on that themselves, they're the data layer only.

# (display label, table) — used both for the per-user "records" count on
# the user list and for the content-totals chart on the stats overview.
_CONTENT_TABLES = [
    ("Habits", "habits"),
    ("To-dos", "todos"),
    ("Notes", "notes"),
    ("Vault documents", "documents"),
    ("Passwords", "passwords"),
    ("Transactions", "transactions"),
    ("Food logs", "food_log"),
    ("Reminders", "reminders"),
    ("Weight entries", "weight_entries"),
]


def admin_list_users(query: str = None):
    """All users, newest first, each annotated with a total record count
    across the main content tables (used for a quick "how much do they
    have stored" signal on the admin user list). Optionally filtered to
    emails containing `query`."""
    count_expr = " + ".join(
        f"(SELECT COUNT(*) FROM {table} WHERE {table}.user_id = users.id)"
        for _, table in _CONTENT_TABLES
    )
    sql = f"SELECT users.*, ({count_expr}) AS record_count FROM users"
    params = ()
    if query:
        sql += " WHERE users.email LIKE ?"
        params = (f"%{query.strip().lower()}%",)
    sql += " ORDER BY users.created_at DESC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def admin_overview_stats():
    """Headline numbers for the admin stats page."""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        admins = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
        verified = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE email_verified_at IS NOT NULL"
        ).fetchone()["c"]
        t = now()
        new_today = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (t - 86400,)
        ).fetchone()["c"]
        new_week = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (t - 7 * 86400,)
        ).fetchone()["c"]
        new_month = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ?", (t - 30 * 86400,)
        ).fetchone()["c"]
    return {
        "total_users": total,
        "admins": admins,
        "verified": verified,
        "unverified": total - verified,
        "new_today": new_today,
        "new_week": new_week,
        "new_month": new_month,
    }


def admin_signup_counts(days: int = 30):
    """Daily signup counts for the last `days` days, including zero-count
    days, so the chart doesn't silently skip gaps."""
    from datetime import datetime, timedelta, timezone

    cutoff = now() - days * 86400
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day, COUNT(*) AS c
            FROM users
            WHERE created_at >= ?
            GROUP BY day
            """,
            (cutoff,),
        ).fetchall()
    by_day = {r["day"]: r["c"] for r in rows}
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"day": d, "count": by_day.get(d, 0)})
    return out


def admin_content_totals():
    """Row counts across the main content tables, for a "what's actually
    being used" bar chart."""
    with get_conn() as conn:
        return [
            {"label": label, "count": conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]}
            for label, table in _CONTENT_TABLES
        ]


def admin_count_admins() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]


def admin_set_admin(user_id: int, is_admin: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id))


def admin_update_user(user_id: int, email: str = None, new_password: str = None) -> str | None:
    """Admin-side edit of a user's email and/or password. Returns an error
    message string if the email is taken by a *different* account, else
    None on success. Blank/omitted fields are left unchanged."""
    with get_conn() as conn:
        if email:
            email = email.strip().lower()
            clash = conn.execute(
                "SELECT id FROM users WHERE email = ? AND id != ?", (email, user_id)
            ).fetchone()
            if clash:
                return "That email is already used by another account."
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
        if new_password:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user_id),
            )
    return None


def admin_delete_user(user_id: int):
    """Deletes a user and every row of their data across every user-scoped
    table. Returns the list of vault document filenames the caller should
    also remove from file storage (local disk or B2) — that part isn't
    tracked here since db.py doesn't know about storage.py."""
    with get_conn() as conn:
        doc_rows = conn.execute(
            "SELECT filename FROM documents WHERE user_id = ?", (user_id,)
        ).fetchall()
        filenames = [r["filename"] for r in doc_rows]

        # savings_contributions hangs off savings_goals (goal_id), not a
        # direct user_id column, so it needs its own scoped delete before
        # the goals themselves go — same shape as export_data()'s handling.
        goal_rows = conn.execute(
            "SELECT id FROM savings_goals WHERE user_id = ?", (user_id,)
        ).fetchall()
        for row in goal_rows:
            conn.execute("DELETE FROM savings_contributions WHERE goal_id = ?", (row["id"],))

        for table in _USER_SCOPED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return filenames
