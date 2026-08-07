# Life Hub

A personal dashboard for health tracking, nutrition, digital ID storage, reminders,
and daily habits — all in one place, with Telegram notifications for the stuff
that needs to reach you (reminders, unfinished habits).

## What's in it

- **Health** — height, weight history with a trend sparkline, auto-calculated BMI, and a log of sports/gym sessions
- **Nutrition** — search real food data (USDA database) or log your own custom foods
  with calories/protein/carbs/fat/fiber; daily totals with optional calorie/protein goal bars
- **ID Vault** — photos of your license, national ID, etc., with an optional expiry date that
  auto-creates a reminder for you
- **Budget** — log income/expenses by category, set optional monthly limits, spend-vs-limit bars,
  plus **recurring income and expenses** (jobs/salary, rent, subscriptions) that auto-log
  themselves every month and notify you when they post
- **Reminders** — one-off, monthly, or yearly, sent via Telegram, with quick snooze (3d/7d)
- **Habits** — daily/weekly/monthly recurring checklist with streak tracking (🔥) and Telegram nudges
- **Passwords** — a small password vault, encrypted at rest (see "About the Passwords section" below)
- **Notes** — quick free-text notes, editable in place
- **Settings** — Telegram bot connection, timezone, currency, nutrition goals, and notification
  timing, all editable from the app itself — plus a one-click JSON backup of everything

> Real per-user login (email + password, with signup at `/register`) protects everything —
> only you can see your data. `APP_ACCESS_TOKEN` was an old stopgap from before login existed
> and is no longer needed; see "Locking it down" below if you still want an extra layer.

## 1. Local setup

```bash
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open http://localhost:5000 — that's it, no login needed locally.

### Get a free USDA nutrition API key (optional but recommended)
The app ships with `DEMO_KEY` which works but is rate-limited (~30 requests/hour).
For real use: https://api.data.gov/signup/ — instant, free, no approval wait.
Put it in `.env` as `USDA_API_KEY`.

### Connect Telegram notifications
You can do this from the **Settings** page in the app itself — no env vars or
redeploy required:
1. Create a bot (or reuse one you already have) via [@BotFather](https://t.me/BotFather) — it gives you a token.
2. Message [@userinfobot](https://t.me/userinfobot) to find your own numeric chat ID.
3. Paste both into Settings → Notifications, hit Save, then "Send test notification."

## 2. Deploying on Railway

Same general pattern as your other bots:

1. Push this folder to a new GitHub repo, create a new Railway project from it.
2. **Add a Volume**, mounted at `/data` (Project canvas → Cmd/Ctrl+K → "volume", or
   right-click the canvas → New → Volume, then attach it to this service).
3. Set `DATA_DIR=/data` in the Variables tab — this is what keeps your database and
   uploaded ID photos alive across redeploys. Without it, Railway wipes them every deploy.
4. Set `FLASK_SECRET_KEY` to something random (used for flash-message cookies).
5. Deploy. Everything else (Telegram bot, USDA key, timezone) can be set from the
   in-app Settings page after it's live — or via env vars if you'd rather set them there.

## Locking it down (optional extra layer on top of login)

Real per-user login (email + password) already protects your data — this section is
only for an *additional* gate in front of the whole app (e.g. hiding the login page
itself from search engines and randomly-guessed URLs). Set `APP_ACCESS_TOKEN` to some
long random string in Railway's Variables. Then the app requires visiting once with
`?key=<that value>` in the URL — after that it remembers you via a cookie, no extra
password screen, nothing to type again. Bookmark the link with the key included and
you'll never see it. Anyone without that exact link is blocked. It's not bank-grade
security, but it stops search engines, guessed URLs, and casual snooping — a meaningful
step up, on top of login, while staying

## Persisting data on Render's free tier (no paid disk) with Turso

If you're on Render's free tier, the filesystem resets whenever the instance spins
down from inactivity — not just on redeploys. Without a paid disk, your sqlite
database gets wiped repeatedly. `db.py` supports an alternative: point it at
[Turso](https://turso.tech), a free hosted SQLite-compatible database, instead of a
local file.

**This only covers the database** (weight logs, budget entries, habits, reminders,
settings, etc). Uploaded ID vault **photos are separate** — they use local disk by
default (which still resets on Render's free tier), unless you also configure
Backblaze B2 object storage (see `B2_KEY_ID`/`B2_APPLICATION_KEY`/`B2_BUCKET_NAME`/
`B2_ENDPOINT_URL` in `.env.example`), which makes vault uploads persist across
restarts and redeploys too.

### Setup
1. Create a free account at [turso.tech](https://turso.tech) and install their CLI, or
   use the dashboard.
2. Create a database, e.g. `turso db create lifehub`.
3. Get the connection URL: `turso db show lifehub --url` → looks like
   `libsql://lifehub-yourname.turso.io`.
4. Create an auth token: `turso db tokens create lifehub`.
5. In Render's Environment tab, set:
   - `TURSO_DATABASE_URL` = the URL from step 3
   - `TURSO_AUTH_TOKEN` = the token from step 4
6. Leave `DATA_DIR` unset. Redeploy (or restart the service).

That's it — no code changes needed on your end. `db.py` automatically detects both
env vars and switches from local sqlite to Turso; the app behaves identically either
way, and every existing page/query keeps working unchanged.

To go back to local sqlite (e.g. for local testing), just unset those two env vars.

just as simple to use day-to-day.

## Notes on the recurring logic

- **Reminders**: checked once daily at the "Reminder send hour" you set. When one fires,
  monthly/yearly ones automatically reschedule themselves forward; one-off ones deactivate.
- **Habit nudges & budget alerts**: checked once daily at the "Habit nudge hour," combined
  into a single Telegram message — unfinished habits and any categories over their monthly
  budget limit, so you get one evening digest instead of multiple pings. Daily habits are
  checked every day; weekly habits only on whichever day you set as "week ends on"; monthly
  habits only on the last calendar day of the month.
- Set your currency label (e.g. ETB, USD) on the Settings page — it's just a display label,
  no conversion happens.
- Both respect whatever timezone you set in Settings.

## About the Passwords section

Passwords are encrypted at rest (`crypto.py`, using `cryptography`'s Fernet) — the raw
SQLite/Turso rows never contain plaintext. The encryption key is derived from
`FLASK_SECRET_KEY`, so there's nothing extra to configure, but two things follow from that:

- **`FLASK_SECRET_KEY` must stay stable once you start storing real passwords.** If it
  changes (e.g. you regenerate it, or move to a new Railway/Render project without copying
  it over), every stored password becomes undecryptable — back that value up somewhere safe.
- This is reversible encryption (by design — you need to get the password back), not a
  one-way hash. It's meant to protect the data at rest in the database. Access control
  is handled by per-user login (only your account can see your entries) — `APP_ACCESS_TOKEN`
  is an optional extra gate on top of that (see "Locking it down" above), not a substitute.

## Customizing

- Add more session types: edit the `<select>` options in `templates/health.html`.
- Change allowed vault file types: edit `ALLOWED_EXT` in `app.py`.
- Everything is plain Flask + SQLite + Jinja templates — no build step, easy to extend.
