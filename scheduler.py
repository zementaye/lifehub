import calendar
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

import config
import db
import telegram_notify

logger = logging.getLogger(__name__)


def get_tz() -> ZoneInfo:
    tz_name = db.get_setting("timezone", config.TIMEZONE)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.error("Invalid timezone %r in settings — falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def get_reminder_hour() -> int:
    return int(db.get_setting("reminder_hour", config.REMINDER_HOUR))


def get_nudge_hour() -> int:
    return int(db.get_setting("nudge_hour", config.NUDGE_HOUR))


def get_week_end_day() -> int:
    return int(db.get_setting("week_end_day", config.WEEK_END_DAY))


def today_local() -> date:
    return datetime.now(get_tz()).date()


def iso_week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def iso_month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def is_last_day_of_month(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def next_due_after(d: date, recurrence: str) -> date:
    if recurrence == "monthly":
        return add_months(d, 1)
    if recurrence == "yearly":
        return add_months(d, 12)
    return d  # 'once' — caller deactivates instead


def period_key_for(freq: str, d: date) -> str:
    if freq == "daily":
        return d.isoformat()
    if freq == "weekly":
        return iso_week_key(d)
    if freq == "monthly":
        return iso_month_key(d)
    return d.isoformat()


# ── Jobs ─────────────────────────────────────────────────────────────────

def check_reminders() -> None:
    today = today_local()
    with db.get_conn() as conn:
        due = conn.execute(
            "SELECT * FROM reminders WHERE active = 1 AND date(next_due) <= date(?)",
            (today.isoformat(),),
        ).fetchall()

        for r in due:
            ok = telegram_notify.send(f"🔔 Reminder: <b>{r['title']}</b>")
            if not ok:
                continue
            if r["recurrence"] == "once":
                conn.execute(
                    "UPDATE reminders SET active = 0, last_sent = ? WHERE id = ?",
                    (today.isoformat(), r["id"]),
                )
            else:
                new_due = next_due_after(date.fromisoformat(r["next_due"]), r["recurrence"])
                # Guard against a stale next_due sending the same reminder daily
                while new_due <= today:
                    new_due = next_due_after(new_due, r["recurrence"])
                conn.execute(
                    "UPDATE reminders SET next_due = ?, last_sent = ? WHERE id = ?",
                    (new_due.isoformat(), today.isoformat(), r["id"]),
                )


def step_back(d: date, freq: str) -> date:
    if freq == "daily":
        return d.fromordinal(d.toordinal() - 1)
    if freq == "weekly":
        return d.fromordinal(d.toordinal() - 7)
    if freq == "monthly":
        return add_months(d, -1)
    return d.fromordinal(d.toordinal() - 1)


def compute_streak(habit_id: int, frequency: str) -> int:
    """Consecutive completed periods leading up to now. Doesn't require today's
    period to already be checked off — an in-progress day/week/month doesn't
    break the streak, matching how most habit trackers count it."""
    today = today_local()
    with db.get_conn() as conn:
        checkins = {
            row["period_key"]
            for row in conn.execute(
                "SELECT period_key FROM habit_checkins WHERE habit_id = ?", (habit_id,)
            )
        }

    if not checkins:
        return 0

    cursor = today
    if period_key_for(frequency, today) not in checkins:
        cursor = step_back(cursor, frequency)

    streak = 0
    while period_key_for(frequency, cursor) in checkins:
        streak += 1
        cursor = step_back(cursor, frequency)
    return streak


def get_habit_status_batch(habits: list) -> dict:
    """Same result as calling compute_streak() + a 'done today' check for
    each habit individually, but with ONE database query total instead of
    two per habit. That N+1 pattern (9 round-trips for just 4 habits) is
    fine on local sqlite, but each round-trip is a real network request
    once the DB is remote (Turso), which is what was making the Habits
    page and dashboard noticeably slow to load/update.

    Returns {habit_id: {"done": bool, "streak": int}}.
    """
    if not habits:
        return {}

    today = today_local()
    habit_ids = [h["id"] for h in habits]
    placeholders = ",".join("?" for _ in habit_ids)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT habit_id, period_key FROM habit_checkins WHERE habit_id IN ({placeholders})",
            tuple(habit_ids),
        ).fetchall()

    checkins_by_habit = {}
    for row in rows:
        checkins_by_habit.setdefault(row["habit_id"], set()).add(row["period_key"])

    result = {}
    for h in habits:
        freq = h["frequency"]
        checkins = checkins_by_habit.get(h["id"], set())
        current_pkey = period_key_for(freq, today)
        done = current_pkey in checkins

        if not checkins:
            streak = 0
        else:
            cursor = today
            if current_pkey not in checkins:
                cursor = step_back(cursor, freq)
            streak = 0
            while period_key_for(freq, cursor) in checkins:
                streak += 1
                cursor = step_back(cursor, freq)

        result[h["id"]] = {"done": done, "streak": streak}
    return result


def get_incomplete_habits() -> list[str]:
    today = today_local()
    weekly_day = today.weekday() == get_week_end_day()
    monthly_day = is_last_day_of_month(today)

    incomplete = []
    with db.get_conn() as conn:
        habits = conn.execute(
            "SELECT * FROM habits WHERE active = 1 AND reminder_hour IS NULL"
        ).fetchall()
        for h in habits:
            freq = h["frequency"]
            if freq == "weekly" and not weekly_day:
                continue
            if freq == "monthly" and not monthly_day:
                continue

            pkey = period_key_for(freq, today)
            done = conn.execute(
                "SELECT 1 FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (h["id"], pkey),
            ).fetchone()
            if not done:
                incomplete.append(h["title"])
    return incomplete


def check_individual_habit_reminders() -> None:
    """Habits with their own reminder_hour set (e.g. 'Morning Prayer' at 7,
    'Bible Study' at 20) get pinged individually at that exact hour instead
    of being lumped into the one shared evening digest. Self-contained
    dedupe (like check_recurring_transactions) so it's safe to call every
    15-minute tick rather than needing its own top-level hour gate."""
    today = today_local()
    now_hour = datetime.now(get_tz()).hour
    weekly_day = today.weekday() == get_week_end_day()
    monthly_day = is_last_day_of_month(today)

    with db.get_conn() as conn:
        habits = conn.execute(
            "SELECT * FROM habits WHERE active = 1 AND reminder_hour = ?", (now_hour,)
        ).fetchall()

        for h in habits:
            freq = h["frequency"]
            if freq == "weekly" and not weekly_day:
                continue
            if freq == "monthly" and not monthly_day:
                continue

            dedupe_key = f"habit_reminder_sent:{h['id']}:{today.isoformat()}"
            if db.get_setting(dedupe_key):
                continue

            pkey = period_key_for(freq, today)
            done = conn.execute(
                "SELECT 1 FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (h["id"], pkey),
            ).fetchone()
            if done:
                continue

            if telegram_notify.send(f"⏰ Reminder: <b>{h['title']}</b>"):
                db.set_setting(dedupe_key, "1")


def check_document_expiries() -> None:
    """Vault documents with an expiry date get a reminder at 1 month out,
    2 weeks out, and then daily for the final 7 days — matching how you'd
    actually want to be nagged about a license renewal. Stops early if
    you've acknowledged that expiry date (see /vault/<id>/acknowledge), and
    naturally resets whenever you renew (update the expiry date to a new
    one) since that no longer matches the acknowledged date."""
    today = today_local()
    with db.get_conn() as conn:
        docs = conn.execute(
            "SELECT * FROM documents WHERE expiry_date IS NOT NULL"
        ).fetchall()

    for d in docs:
        try:
            expiry = date.fromisoformat(d["expiry_date"])
        except ValueError:
            continue

        if d["expiry_ack_date"] == d["expiry_date"]:
            continue  # already acknowledged this expiry cycle

        days_left = (expiry - today).days
        if days_left < 0 or days_left > 30:
            continue
        if not (days_left in (30, 14) or days_left <= 7):
            continue

        dedupe_key = f"doc_expiry_sent:{d['id']}:{today.isoformat()}"
        if db.get_setting(dedupe_key):
            continue

        if days_left == 0:
            msg = f"⚠️ <b>{d['label']}</b> expires <b>today</b>."
        elif days_left <= 7:
            msg = f"⚠️ <b>{d['label']}</b> expires in {days_left} day{'s' if days_left != 1 else ''}."
        elif days_left == 14:
            msg = f"📅 <b>{d['label']}</b> expires in 2 weeks."
        else:  # 30
            msg = f"📅 <b>{d['label']}</b> expires in 1 month."

        if telegram_notify.send(msg):
            db.set_setting(dedupe_key, "1")


def get_overbudget_categories() -> list[str]:
    today = today_local()
    month_key = iso_month_key(today)
    currency = db.get_setting("currency", "ETB")

    over = []
    with db.get_conn() as conn:
        cats = conn.execute(
            "SELECT * FROM budget_categories WHERE monthly_limit IS NOT NULL"
        ).fetchall()
        for c in cats:
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                "WHERE type = 'expense' AND category_id = ? AND strftime('%Y-%m', date) = ?",
                (c["id"], month_key),
            ).fetchone()["total"]
            if spent > c["monthly_limit"]:
                over.append(f"{c['name']}: {spent:.0f} / {c['monthly_limit']:.0f} {currency}")
    return over


def check_recurring_transactions() -> None:
    """Auto-logs due recurring income (jobs/salary) and expenses (rent, bills,
    etc.) as real transactions, then advances each to its next month."""
    today = today_local()
    currency = db.get_setting("currency", "ETB")

    with db.get_conn() as conn:
        due = conn.execute(
            "SELECT * FROM recurring_transactions WHERE active = 1 AND date(next_run) <= date(?)",
            (today.isoformat(),),
        ).fetchall()

        for r in due:
            conn.execute(
                "INSERT INTO transactions (date, type, category_id, description, amount, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (r["next_run"], r["type"], r["category_id"], f"{r['title']} (recurring)",
                 r["amount"], db.now()),
            )
            icon = "💰" if r["type"] == "income" else "💸"
            telegram_notify.send(f"{icon} Recurring {r['type']} logged: <b>{r['title']}</b> — {r['amount']:.0f} {currency}")

            new_next = date.fromisoformat(r["next_run"])
            if r["frequency"] == "weekly":
                new_next += timedelta(days=7)
                while new_next <= today:
                    new_next += timedelta(days=7)
            else:
                new_next = add_months(new_next, 1)
                while new_next <= today:
                    new_next = add_months(new_next, 1)
            conn.execute(
                "UPDATE recurring_transactions SET next_run = ? WHERE id = ?",
                (new_next.isoformat(), r["id"]),
            )


def send_evening_digest() -> None:
    """One combined Telegram message covering unfinished habits and any
    over-budget categories this month, instead of separate pings."""
    incomplete = get_incomplete_habits()
    overbudget = get_overbudget_categories()

    if not incomplete and not overbudget:
        return

    parts = []
    if incomplete:
        parts.append("⏰ Still open today:\n" + "\n".join(f"• {t}" for t in incomplete))
    if overbudget:
        parts.append("💸 Over budget this month:\n" + "\n".join(f"• {t}" for t in overbudget))

    telegram_notify.send("\n\n".join(parts))


def tick() -> None:
    """Runs every 15 minutes. Fires reminder-check / evening-digest jobs exactly
    once on the day they're due, at whatever hour is currently configured —
    so changing the hour in Settings takes effect without a restart."""
    today = today_local()
    now_hour = datetime.now(get_tz()).hour

    if now_hour == get_reminder_hour() and db.get_setting("last_reminder_run") != today.isoformat():
        try:
            check_reminders()
            check_recurring_transactions()
            check_document_expiries()
        finally:
            db.set_setting("last_reminder_run", today.isoformat())

    if now_hour == get_nudge_hour() and db.get_setting("last_habit_run") != today.isoformat():
        try:
            send_evening_digest()
        finally:
            db.set_setting("last_habit_run", today.isoformat())

    # Self-contained dedupe, safe to call every tick regardless of hour —
    # it only actually sends when now_hour matches a habit's own reminder_hour.
    check_individual_habit_reminders()


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=config.TIMEZONE)
    sched.add_job(tick, "interval", minutes=15, id="tick", next_run_time=datetime.now())
    sched.start()
    logger.info("Scheduler started — polling every 15 minutes.")
    return sched
