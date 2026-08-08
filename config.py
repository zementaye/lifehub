import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# ── Environment ──────────────────────────────────────────────────────────
# Defaults to "production" on purpose: the safe fallback behaviors below
# (refusing to start without a secret key, secure cookies, etc.) should be
# the default posture unless someone deliberately opts into local dev mode.
FLASK_ENV = os.environ.get("FLASK_ENV", "production").strip().lower()
IS_PRODUCTION = FLASK_ENV != "development"

# Where the sqlite DB and uploaded files live. Point this at a Railway/Render
# volume (e.g. /data) in production so it survives redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "lifehub.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Turso (remote SQLite-compatible DB) ─────────────────────────────────
# If both of these are set, db.py talks to Turso over the network instead of
# a local sqlite file, so your data survives Render's free-tier restarts.
# Uploaded files (ID vault photos) still live on local disk and are NOT
# covered by this — they still reset unless you add a paid disk.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

# ── Telegram notifications ──────────────────────────────────────────────
# Point this at any bot you own (new or existing) — reminders/habit nudges
# get sent as DMs from that bot to TG_CHAT_ID.
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# ── Nutrition lookup (USDA FoodData Central) ────────────────────────────
# Free key: https://api.data.gov/signup/  (DEMO_KEY works but is rate-limited)
USDA_API_KEY = os.environ.get("USDA_API_KEY", "DEMO_KEY")

# ── Habit nudge / reminder scheduling ───────────────────────────────────
TIMEZONE = os.environ.get("TIMEZONE", "Africa/Addis_Ababa")
NUDGE_HOUR = int(os.environ.get("NUDGE_HOUR", "20"))   # 24h, local TZ — daily check time
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "9"))  # when due reminders get sent
WEEK_END_DAY = int(os.environ.get("WEEK_END_DAY", "6"))  # 0=Mon ... 6=Sun (ISO weekday-1)

# ── Optional lightweight access gate ────────────────────────────────────
# If set, visitors need ?key=<this value> once; after that a cookie remembers
# them. Leave blank for zero-friction access (fine for local/private use only).
APP_ACCESS_TOKEN = os.environ.get("APP_ACCESS_TOKEN", "")

# ── Email (password reset / verification) ────────────────────────────────
# Sent via Resend's HTTPS API (https://resend.com) rather than raw SMTP —
# Render's free tier blocks outbound traffic on SMTP ports 25/465/587, so
# smtplib can never connect there no matter how it's configured. Resend
# sends over normal HTTPS (443), which isn't blocked. Free tier: sign up at
# resend.com, grab an API key, done. If RESEND_FROM is left as the default
# "onboarding@resend.dev", Resend only allows delivery to the email address
# on your own Resend account — verify a domain in the Resend dashboard and
# set RESEND_FROM to an address on it to send to your actual users.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Life Hub <onboarding@resend.dev>")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")  # e.g. https://lifehub-g8z9.onrender.com — used to build reset links


# ── Secret key ───────────────────────────────────────────────────────────
# This isn't just session signing — crypto.py derives the vault password
# encryption key from it. If it's ever unset in production, session cookies
# become forgeable and every stored vault password becomes decryptable by
# anyone who has read the (public) source. So: no silent fallback in
# production. Only FLASK_ENV=development is allowed to use the placeholder,
# and even then it prints a loud warning.
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if IS_PRODUCTION:
        sys.exit(
            "FATAL: FLASK_SECRET_KEY is not set.\n"
            "This app refuses to start without it in production, because this key\n"
            "signs session cookies AND derives the vault's password-encryption key\n"
            "(see crypto.py). Set FLASK_SECRET_KEY to a long random value, e.g.:\n"
            "    python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "and keep it stable/backed-up once you start storing real data — if it\n"
            "changes, every stored vault password becomes undecryptable.\n"
            "(To run locally without setting this, export FLASK_ENV=development.)"
        )
    print(
        "WARNING: FLASK_SECRET_KEY is not set — using an insecure placeholder "
        "key because FLASK_ENV=development. Do NOT do this in production.",
        file=sys.stderr,
    )
    SECRET_KEY = "dev-secret-change-me"

# ── Upload size limit ────────────────────────────────────────────────────
# Applied as Flask's MAX_CONTENT_LENGTH so an oversized POST (e.g. to
# /vault/upload) is rejected before it's read into memory/disk.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024

# ── Session cookie hardening ─────────────────────────────────────────────
# HTTPONLY blocks JS access (mitigates XSS session theft), SAMESITE=Lax
# blocks the cookie being sent on most cross-site requests (CSRF defense in
# depth, on top of CSRFProtect below), SECURE requires HTTPS in production
# (Render terminates TLS in front of the app, so this is safe to require
# there; left off in dev so http://localhost still works).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = IS_PRODUCTION
# Sessions guard a password vault and ID documents — Flask's 31-day default
# is too long-lived for that. 7 days, refreshed on activity (default
# SESSION_REFRESH_EACH_REQUEST=True), configurable via env if needed.
PERMANENT_SESSION_LIFETIME = int(os.environ.get("SESSION_LIFETIME_DAYS", "7")) * 24 * 60 * 60

# The regular login session above is long-lived on purpose (it's fine for
# someone to stay logged in to their own dashboard for a week). Admin
# access is more sensitive, so it gets its own much shorter, sliding
# window on top: even a logged-in admin has to re-enter their password if
# they haven't touched the admin console in the last N minutes — e.g.
# after coming back to a browser-history link hours later on a shared or
# unlocked machine. See admin_required() in app.py.
ADMIN_ELEVATION_LIFETIME = int(os.environ.get("ADMIN_ELEVATION_MINUTES", "15")) * 60

# ── Vault file storage (Backblaze B2, S3-compatible) ────────────────────
# ID vault uploads (photos/PDFs) are stored here instead of local disk, so
# they survive Render free-tier restarts and redeploys.
# .strip() guards against a trailing space/newline sneaking in when the
# key/endpoint is copy-pasted into Render's env var box — that alone is
# enough to break SigV4 signing with a SignatureDoesNotMatch error.
def _clean_env(name, default=None):
    val = os.environ.get(name, default)
    return val.strip() if val else val

R2_ACCESS_KEY_ID = _clean_env("B2_KEY_ID")
R2_SECRET_ACCESS_KEY = _clean_env("B2_APPLICATION_KEY")
R2_BUCKET_NAME = _clean_env("B2_BUCKET_NAME", "lifehub-vault")
R2_ENDPOINT_URL = _clean_env("B2_ENDPOINT_URL")

# True once all three required B2 values are present — vault routes use this
# to decide whether to read/write B2 (persistent) or local disk (ephemeral
# on Render free tier; wiped on every restart/redeploy).
USE_B2 = bool(R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT_URL)
