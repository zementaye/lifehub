import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

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

# ── Email (password reset) ──────────────────────────────────────────────
# Standard SMTP — works with a Gmail "App Password" (myaccount.google.com/
# apppasswords) or any other SMTP provider. If unset, password reset emails
# just can't be sent (registration/login still work fine without this).
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")  # e.g. https://lifehub-g8z9.onrender.com — used to build reset links

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

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
