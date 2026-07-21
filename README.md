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
- **Settings** — Telegram bot connection, timezone, currency, nutrition goals, and notification
  timing, all editable from the app itself — plus a one-click JSON backup of everything

> ⚠️ **No login by default**, as requested — anyone with the URL can see everything,
> including your ID photos. See "Locking it down" below before you put real documents in
> if you're deploying it somewhere public.

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

## Locking it down (recommended before storing real documents)

Since there's no login, set `APP_ACCESS_TOKEN` to some long random string in Railway's
Variables. Then the app requires visiting once with `?key=<that value>` in the URL —
after that it remembers you via a cookie, no password screen, nothing to type again.
Bookmark the link with the key included and you'll never see it. Anyone without that
exact link is blocked. It's not bank-grade security, but it stops search engines,
guessed URLs, and casual snooping — a meaningful step up from wide open, while staying
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

## Customizing

- Add more session types: edit the `<select>` options in `templates/health.html`.
- Change allowed vault file types: edit `ALLOWED_EXT` in `app.py`.
- Everything is plain Flask + SQLite + Jinja templates — no build step, easy to extend.
