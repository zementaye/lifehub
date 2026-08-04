"""Authentication: registration, login sessions, and password reset via
email. Deliberately simple — session-cookie based (Flask's built-in signed
session, no separate library), werkzeug for password hashing (already a
dependency via Flask), smtplib (stdlib) for reset emails.
"""

import logging
import secrets
import smtplib
import time
from email.mime.text import MIMEText
from functools import wraps

from flask import session, redirect, url_for, request

from werkzeug.security import generate_password_hash, check_password_hash

import config
import db

logger = logging.getLogger(__name__)

RESET_TOKEN_TTL = 60 * 60  # 1 hour


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def create_user(email: str, password: str) -> int:
    """Creates a new user. Raises ValueError if the email is already taken."""
    email = email.strip().lower()
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise ValueError("An account with that email already exists.")
        is_first_user = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
            (email, hash_password(password), db.now()),
        )
        user_id = cur.lastrowid

    if is_first_user:
        # The very first account claims all pre-existing single-user data.
        db.claim_orphaned_data(user_id)
    # Every user gets their own blank profile row to start.
    with db.get_conn() as conn:
        conn.execute("INSERT INTO profile (user_id, height_cm) VALUES (?, NULL)", (user_id,))
    return user_id


def get_user_by_email(email: str):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()


def get_user_by_id(user_id: int):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_user(user_id: int) -> None:
    session["user_id"] = user_id
    session.permanent = True


def logout_user() -> None:
    session.pop("user_id", None)


def current_user_id():
    return session.get("user_id")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ── Password reset ──────────────────────────────────────────────────────

def create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO password_resets (token, user_id, expires_at, used) VALUES (?,?,?,0)",
            (token, user_id, db.now() + RESET_TOKEN_TTL),
        )
    return token


def consume_reset_token(token: str):
    """Returns the user_id if the token is valid and unused, else None.
    Marks it used so it can't be replayed."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM password_resets WHERE token = ?", (token,)
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < db.now():
            return None
        conn.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (token,))
        return row["user_id"]


def send_reset_email(to_email: str, token: str) -> bool:
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD):
        logger.warning("SMTP not configured — can't send password reset email")
        return False

    base_url = config.APP_BASE_URL or f"{request.scheme}://{request.host}"
    reset_link = f"{base_url}/reset-password/{token}"
    body = (
        f"Someone (hopefully you) requested a password reset for your LifeHub account.\n\n"
        f"Reset your password here (link expires in 1 hour):\n{reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Reset your LifeHub password"
    msg["From"] = config.SMTP_FROM
    msg["To"] = to_email

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception:
        logger.exception("Failed to send password reset email")
        return False
