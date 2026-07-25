import logging
import os
import uuid
from datetime import date, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, make_response

import config
import db
import nutrition_api
import scheduler
import telegram_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

db.init_db()


# ── Optional lightweight access gate ────────────────────────────────────
@app.before_request
def check_access():
    if not config.APP_ACCESS_TOKEN:
        return  # gate disabled — zero friction
    if request.cookies.get("access_ok") == config.APP_ACCESS_TOKEN:
        return
    if request.args.get("key") == config.APP_ACCESS_TOKEN:
        return  # will set cookie in after_request
    return "Access denied.", 403


@app.after_request
def set_access_cookie(resp):
    if config.APP_ACCESS_TOKEN and request.args.get("key") == config.APP_ACCESS_TOKEN:
        resp.set_cookie(
            "access_ok", config.APP_ACCESS_TOKEN, max_age=60 * 60 * 24 * 365,
            httponly=True, samesite="Lax",
        )
    return resp


# ── Helpers ──────────────────────────────────────────────────────────────

def bmi_of(weight_kg: float, height_cm: float) -> float | None:
    if not weight_kg or not height_cm:
        return None
    h_m = height_cm / 100
    return round(weight_kg / (h_m * h_m), 1)


def bmi_category(bmi: float) -> str:
    if bmi < 18.5:
        return "Underweight"
    if bmi < 25:
        return "Normal"
    if bmi < 30:
        return "Overweight"
    return "Obese"


def weight_sparkline_svg(weights_desc, width=560, height=90, pad=10) -> str | None:
    """weights_desc: rows ordered most-recent-first (as queried). Renders oldest
    to newest, left to right."""
    weights_asc = list(reversed(weights_desc))
    if len(weights_asc) < 2:
        return None
    vals = [w["weight_kg"] for w in weights_asc]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)

    def px(i):
        return pad + i * (width - 2 * pad) / (n - 1)

    def py(v):
        return height - pad - (v - lo) * (height - 2 * pad) / rng

    points = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="#2f6f5e" />'
        for i, v in enumerate(vals)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="sparkline" preserveAspectRatio="none">'
        f'<polyline points="{points}" fill="none" stroke="#2f6f5e" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}</svg>'
    )


# ── Dashboard ────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    today = scheduler.today_local().isoformat()
    with db.get_conn() as conn:
        profile = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        latest_weight = conn.execute(
            "SELECT * FROM weight_entries ORDER BY date DESC, id DESC LIMIT 1"
        ).fetchone()
        upcoming_reminders = conn.execute(
            "SELECT * FROM reminders WHERE active = 1 ORDER BY date(next_due) LIMIT 5"
        ).fetchall()
        habits = conn.execute("SELECT * FROM habits WHERE active = 1").fetchall()
        today_food = conn.execute(
            "SELECT * FROM food_log WHERE date = ?", (today,)
        ).fetchall()

    bmi = bmi_of(latest_weight["weight_kg"], profile["height_cm"]) if latest_weight and profile["height_cm"] else None

    habit_status = []
    for h in habits:
        pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local())
        with db.get_conn() as conn:
            done = conn.execute(
                "SELECT 1 FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (h["id"], pkey),
            ).fetchone()
        streak = scheduler.compute_streak(h["id"], h["frequency"])
        habit_status.append({"habit": h, "done": bool(done), "streak": streak})

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in today_food:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    return render_template(
        "dashboard.html",
        profile=profile, latest_weight=latest_weight, bmi=bmi,
        bmi_category=bmi_category(bmi) if bmi else None,
        upcoming_reminders=upcoming_reminders, habit_status=habit_status,
        totals=totals,
    )


# ── Health ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    with db.get_conn() as conn:
        profile = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        weights = conn.execute("SELECT * FROM weight_entries ORDER BY date DESC, id DESC LIMIT 30").fetchall()
        sessions = conn.execute("SELECT * FROM sessions ORDER BY date DESC, id DESC LIMIT 30").fetchall()

    latest = weights[0] if weights else None
    bmi = bmi_of(latest["weight_kg"], profile["height_cm"]) if latest and profile["height_cm"] else None
    return render_template(
        "health.html", profile=profile, weights=weights, sessions=sessions,
        bmi=bmi, bmi_category=bmi_category(bmi) if bmi else None,
        today=date.today().isoformat(),
        sparkline_svg=weight_sparkline_svg(weights),
    )


@app.route("/health/height", methods=["POST"])
def set_height():
    height = request.form.get("height_cm", type=float)
    with db.get_conn() as conn:
        conn.execute("UPDATE profile SET height_cm = ? WHERE id = 1", (height,))
    flash("Height updated.")
    return redirect(url_for("health"))


@app.route("/health/weight", methods=["POST"])
def add_weight():
    d = request.form.get("date") or date.today().isoformat()
    w = request.form.get("weight_kg", type=float)
    if w:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO weight_entries (date, weight_kg, created_at) VALUES (?, ?, ?)",
                (d, w, db.now()),
            )
        flash("Weight logged.")
    return redirect(url_for("health"))


@app.route("/health/weight/<int:entry_id>/delete", methods=["POST"])
def delete_weight(entry_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM weight_entries WHERE id = ?", (entry_id,))
    return redirect(url_for("health"))


@app.route("/health/session", methods=["POST"])
def add_session():
    d = request.form.get("date") or date.today().isoformat()
    stype = request.form.get("type", "").strip()
    duration = request.form.get("duration_minutes", type=int)
    notes = request.form.get("notes", "").strip()
    if stype:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO sessions (date, type, duration_minutes, notes, created_at) VALUES (?,?,?,?,?)",
                (d, stype, duration, notes, db.now()),
            )
        flash("Session logged.")
    return redirect(url_for("health"))


@app.route("/health/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return redirect(url_for("health"))


# ── Nutrition ────────────────────────────────────────────────────────────

@app.route("/nutrition")
def nutrition():
    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        log = conn.execute("SELECT * FROM food_log WHERE date = ? ORDER BY id", (d,)).fetchall()
        custom_foods = conn.execute("SELECT * FROM custom_foods ORDER BY name").fetchall()

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in log:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    goal_cal = db.get_setting("nutrition_goal_calories")
    goal_protein = db.get_setting("nutrition_goal_protein")
    goals = {
        "calories": float(goal_cal) if goal_cal else None,
        "protein_g": float(goal_protein) if goal_protein else None,
    }

    return render_template("nutrition.html", log=log, custom_foods=custom_foods, totals=totals,
                            view_date=d, goals=goals)


@app.route("/nutrition/search")
def nutrition_search():
    q = request.args.get("q", "")
    results = nutrition_api.search_foods(q)
    return {"results": results}


@app.route("/nutrition/log", methods=["POST"])
def log_food():
    d = request.form.get("date") or date.today().isoformat()
    servings = request.form.get("servings", type=float) or 1.0
    name = request.form.get("name", "").strip()
    source = request.form.get("source", "usda")

    fields = {}
    for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
        fields[k] = request.form.get(k, type=float) or 0.0

    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO food_log (date, source, custom_food_id, name, servings, "
                "calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d, source, request.form.get("custom_food_id", type=int), name, servings,
                 fields["calories"], fields["protein_g"], fields["carbs_g"], fields["fat_g"],
                 fields["fiber_g"], db.now()),
            )
        flash(f"Logged {name}.")
    return redirect(url_for("nutrition", date=d))


@app.route("/nutrition/log/<int:log_id>/delete", methods=["POST"])
def delete_food_log(log_id):
    d = request.form.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM food_log WHERE id = ?", (log_id,))
    return redirect(url_for("nutrition", date=d))


@app.route("/nutrition/custom", methods=["POST"])
def add_custom_food():
    name = request.form.get("name", "").strip()
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO custom_foods (name, calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (name,
                 request.form.get("calories", type=float) or 0,
                 request.form.get("protein_g", type=float) or 0,
                 request.form.get("carbs_g", type=float) or 0,
                 request.form.get("fat_g", type=float) or 0,
                 request.form.get("fiber_g", type=float) or 0,
                 db.now()),
            )
        flash(f"Added custom food: {name}")
    return redirect(url_for("nutrition"))


# ── ID Vault ─────────────────────────────────────────────────────────────

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "heic", "pdf"}


@app.route("/vault")
def vault():
    with db.get_conn() as conn:
        docs = conn.execute("SELECT * FROM documents ORDER BY label").fetchall()
    return render_template("vault.html", docs=docs)


@app.route("/vault/upload", methods=["POST"])
def vault_upload():
    label = request.form.get("label", "").strip()
    notes = request.form.get("notes", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip() or None
    make_reminder = request.form.get("make_reminder") == "on"
    file = request.files.get("file")

    if not label or not file or file.filename == "":
        flash("Label and file are required.")
        return redirect(url_for("vault"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        flash("Unsupported file type.")
        return redirect(url_for("vault"))

    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(config.UPLOAD_DIR / filename)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (label, filename, notes, expiry_date, created_at) VALUES (?,?,?,?,?)",
            (label, filename, notes, expiry_date, db.now()),
        )
        if expiry_date and make_reminder:
            conn.execute(
                "INSERT INTO reminders (title, next_due, recurrence, active, created_at) VALUES (?,?,?,1,?)",
                (f"{label} expires", expiry_date, "once", db.now()),
            )

    flash(f"Saved {label}." + (" Reminder created too." if expiry_date and make_reminder else ""))
    return redirect(url_for("vault"))


@app.route("/vault/file/<filename>")
def vault_file(filename):
    return send_from_directory(config.UPLOAD_DIR, filename)


@app.route("/vault/<int:doc_id>/delete", methods=["POST"])
def vault_delete(doc_id):
    with db.get_conn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc:
            path = config.UPLOAD_DIR / doc["filename"]
            if path.exists():
                path.unlink()
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return redirect(url_for("vault"))


# ── Reminders ────────────────────────────────────────────────────────────

@app.route("/reminders")
def reminders():
    with db.get_conn() as conn:
        items = conn.execute("SELECT * FROM reminders ORDER BY active DESC, date(next_due)").fetchall()
    return render_template("reminders.html", reminders=items)


@app.route("/reminders", methods=["POST"])
def add_reminder():
    title = request.form.get("title", "").strip()
    next_due = request.form.get("next_due")
    recurrence = request.form.get("recurrence", "once")
    if title and next_due:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO reminders (title, next_due, recurrence, active, created_at) VALUES (?,?,?,1,?)",
                (title, next_due, recurrence, db.now()),
            )
        flash(f"Reminder set: {title}")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/snooze", methods=["POST"])
def snooze_reminder(reminder_id):
    days = request.form.get("days", type=int) or 1
    with db.get_conn() as conn:
        r = conn.execute("SELECT next_due FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if r:
            base = max(date.fromisoformat(r["next_due"]), scheduler.today_local())
            new_due = base + timedelta(days=days)
            conn.execute("UPDATE reminders SET next_due = ?, active = 1 WHERE id = ?",
                         (new_due.isoformat(), reminder_id))
    flash(f"Snoozed {days} day(s).")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/toggle", methods=["POST"])
def toggle_reminder(reminder_id):
    with db.get_conn() as conn:
        r = conn.execute("SELECT active FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if r:
            conn.execute("UPDATE reminders SET active = ? WHERE id = ?", (0 if r["active"] else 1, reminder_id))
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/delete", methods=["POST"])
def delete_reminder(reminder_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    return redirect(url_for("reminders"))


# ── Habits ───────────────────────────────────────────────────────────────

@app.route("/habits")
def habits():
    with db.get_conn() as conn:
        items = conn.execute("SELECT * FROM habits WHERE active = 1 ORDER BY frequency, title").fetchall()

    today = scheduler.today_local()
    status = []
    for h in items:
        pkey = scheduler.period_key_for(h["frequency"], today)
        with db.get_conn() as conn:
            done = conn.execute(
                "SELECT 1 FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (h["id"], pkey),
            ).fetchone()
        streak = scheduler.compute_streak(h["id"], h["frequency"])
        status.append({"habit": h, "done": bool(done), "period_key": pkey, "streak": streak})

    return render_template("habits.html", status=status)


@app.route("/habits", methods=["POST"])
def add_habit():
    title = request.form.get("title", "").strip()
    frequency = request.form.get("frequency", "daily")
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO habits (title, frequency, active, created_at) VALUES (?,?,1,?)",
                (title, frequency, db.now()),
            )
        flash(f"Habit added: {title}")
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/checkin", methods=["POST"])
def checkin_habit(habit_id):
    with db.get_conn() as conn:
        h = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local())
            conn.execute(
                "INSERT OR IGNORE INTO habit_checkins (habit_id, period_key, done_at) VALUES (?,?,?)",
                (habit_id, pkey, db.now()),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/uncheck", methods=["POST"])
def uncheck_habit(habit_id):
    with db.get_conn() as conn:
        h = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local())
            conn.execute(
                "DELETE FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (habit_id, pkey),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
def delete_habit(habit_id):
    with db.get_conn() as conn:
        conn.execute("UPDATE habits SET active = 0 WHERE id = ?", (habit_id,))
    return redirect(url_for("habits"))


# ── Budget ───────────────────────────────────────────────────────────────

@app.route("/budget")
def budget():
    month = request.args.get("month") or scheduler.today_local().strftime("%Y-%m")
    currency = db.get_setting("currency", "ETB")

    with db.get_conn() as conn:
        categories = conn.execute("SELECT * FROM budget_categories ORDER BY name").fetchall()
        txns = conn.execute(
            "SELECT t.*, c.name AS category_name FROM transactions t "
            "LEFT JOIN budget_categories c ON c.id = t.category_id "
            "WHERE strftime('%Y-%m', t.date) = ? ORDER BY t.date DESC, t.id DESC",
            (month,),
        ).fetchall()
        recurring = conn.execute(
            "SELECT r.*, c.name AS category_name FROM recurring_transactions r "
            "LEFT JOIN budget_categories c ON c.id = r.category_id "
            "ORDER BY r.active DESC, r.next_run"
        ).fetchall()
        savings_goals = conn.execute(
            "SELECT * FROM savings_goals ORDER BY created_at"
        ).fetchall()

        income_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE type='income' AND strftime('%Y-%m', date)=?",
            (month,),
        ).fetchone()["t"]
        expense_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE type='expense' AND strftime('%Y-%m', date)=?",
            (month,),
        ).fetchone()["t"]

        category_spend = []
        for c in categories:
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
                "WHERE type='expense' AND category_id=? AND strftime('%Y-%m', date)=?",
                (c["id"], month),
            ).fetchone()["t"]
            limit = c["monthly_limit"]
            pct = min(round(spent / limit * 100), 100) if limit else None
            category_spend.append({
                "category": c, "spent": spent, "limit": limit, "pct": pct,
                "over": bool(limit and spent > limit),
            })

    return render_template(
        "budget.html", month=month, categories=categories, txns=txns,
        income_total=income_total, expense_total=expense_total,
        net=income_total - expense_total, category_spend=category_spend,
        currency=currency, recurring=recurring, savings_goals=savings_goals,
    )


@app.route("/budget/transaction", methods=["POST"])
def add_transaction():
    month = request.form.get("month") or scheduler.today_local().strftime("%Y-%m")
    d = request.form.get("date") or date.today().isoformat()
    ttype = request.form.get("type", "expense")
    amount = request.form.get("amount", type=float)
    description = request.form.get("description", "").strip()
    category_id = request.form.get("category_id", type=int) or None

    if amount:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO transactions (date, type, category_id, description, amount, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (d, ttype, category_id if ttype == "expense" else None, description, abs(amount), db.now()),
            )
        flash(f"Logged {'income' if ttype == 'income' else 'expense'}: {amount}")
    return redirect(url_for("budget", month=month))


@app.route("/budget/transaction/<int:txn_id>/delete", methods=["POST"])
def delete_transaction(txn_id):
    month = request.form.get("month") or scheduler.today_local().strftime("%Y-%m")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    return redirect(url_for("budget", month=month))


@app.route("/budget/category", methods=["POST"])
def add_budget_category():
    name = request.form.get("name", "").strip()
    limit = request.form.get("monthly_limit", type=float)
    if name:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO budget_categories (name, monthly_limit, created_at) VALUES (?,?,?)",
                (name, limit, db.now()),
            )
            if cur.rowcount:
                flash(f"Category added: {name}")
            else:
                flash(f"'{name}' already exists.")
    return redirect(url_for("budget"))


@app.route("/budget/category/<int:cat_id>/delete", methods=["POST"])
def delete_budget_category(cat_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM budget_categories WHERE id = ?", (cat_id,))
    return redirect(url_for("budget"))


@app.route("/budget/recurring", methods=["POST"])
def add_recurring_transaction():
    import calendar
    from datetime import timedelta

    title = request.form.get("title", "").strip()
    ttype = request.form.get("type", "expense")
    amount = request.form.get("amount", type=float)
    category_id = request.form.get("category_id", type=int) or None
    frequency = request.form.get("frequency", "monthly")
    if frequency not in ("monthly", "weekly"):
        frequency = "monthly"
    day_of_month = request.form.get("day_of_month", type=int) or 1
    day_of_month = max(1, min(day_of_month, 28))
    day_of_week = request.form.get("day_of_week", type=int)
    day_of_week = 0 if day_of_week is None else max(0, min(day_of_week, 6))
    start_raw = request.form.get("start_date") or scheduler.today_local().isoformat()

    if title and amount:
        if len(start_raw) == 7:  # "YYYY-MM" from the month picker (monthly only)
            start_raw += "-01"
        start = date.fromisoformat(start_raw)

        if frequency == "weekly":
            next_run = start
            while next_run.weekday() != day_of_week:
                next_run += timedelta(days=1)
            while next_run < scheduler.today_local():
                next_run += timedelta(days=7)
        else:
            clamped_day = min(day_of_month, calendar.monthrange(start.year, start.month)[1])
            next_run = start.replace(day=clamped_day)
            if next_run < scheduler.today_local():
                next_run = scheduler.add_months(next_run, 1)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO recurring_transactions "
                "(title, type, amount, category_id, frequency, day_of_month, day_of_week, next_run, active, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?)",
                (title, ttype, amount, category_id if ttype == "expense" else None,
                 frequency, day_of_month, day_of_week if frequency == "weekly" else None,
                 next_run.isoformat(), db.now()),
            )
        flash(f"Recurring {ttype} set up: {title} — first run {next_run.isoformat()}")
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/toggle", methods=["POST"])
def toggle_recurring_transaction(rid):
    with db.get_conn() as conn:
        r = conn.execute("SELECT active FROM recurring_transactions WHERE id = ?", (rid,)).fetchone()
        if r:
            conn.execute("UPDATE recurring_transactions SET active = ? WHERE id = ?",
                         (0 if r["active"] else 1, rid))
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/delete", methods=["POST"])
def delete_recurring_transaction(rid):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM recurring_transactions WHERE id = ?", (rid,))
    return redirect(url_for("budget"))


# ── Savings goals ────────────────────────────────────────────────────────

@app.route("/budget/savings", methods=["POST"])
def add_savings_goal():
    name = request.form.get("name", "").strip()
    target_amount = request.form.get("target_amount", type=float)
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO savings_goals (name, target_amount, current_amount, created_at) "
                "VALUES (?,?,0,?)",
                (name, target_amount, db.now()),
            )
        flash(f"Savings goal created: {name}")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/contribute", methods=["POST"])
def contribute_savings_goal(goal_id):
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local().isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
            if goal:
                # Money moving into savings reduces what's available to spend,
                # so it's logged as a normal expense transaction — the goal's
                # progress bar and your budget totals both stay accurate.
                cur = conn.execute(
                    "INSERT INTO transactions (date, type, category_id, description, amount, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (today, "expense", None, f"Savings: {goal['name']}", amount, db.now()),
                )
                conn.execute(
                    "INSERT INTO savings_contributions (goal_id, date, amount, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (goal_id, today, amount, cur.lastrowid, db.now()),
                )
                conn.execute(
                    "UPDATE savings_goals SET current_amount = current_amount + ? WHERE id = ?",
                    (amount, goal_id),
                )
        flash(f"Added {amount:.0f} to savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/withdraw", methods=["POST"])
def withdraw_savings_goal(goal_id):
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local().isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
            if goal:
                amount = min(amount, goal["current_amount"])  # can't withdraw more than saved
                # Money coming back out of savings adds back to what's
                # available to spend, so it's logged as income.
                cur = conn.execute(
                    "INSERT INTO transactions (date, type, category_id, description, amount, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (today, "income", None, f"Savings withdrawal: {goal['name']}", amount, db.now()),
                )
                conn.execute(
                    "INSERT INTO savings_contributions (goal_id, date, amount, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (goal_id, today, -amount, cur.lastrowid, db.now()),
                )
                conn.execute(
                    "UPDATE savings_goals SET current_amount = current_amount - ? WHERE id = ?",
                    (amount, goal_id),
                )
        flash(f"Withdrew {amount:.0f} from savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/delete", methods=["POST"])
def delete_savings_goal(goal_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
    return redirect(url_for("budget"))


# ── Settings ─────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    values = {
        "tg_bot_token": db.get_setting("tg_bot_token", config.TG_BOT_TOKEN),
        "tg_chat_id": db.get_setting("tg_chat_id", config.TG_CHAT_ID),
        "timezone": db.get_setting("timezone", config.TIMEZONE),
        "reminder_hour": db.get_setting("reminder_hour", config.REMINDER_HOUR),
        "nudge_hour": db.get_setting("nudge_hour", config.NUDGE_HOUR),
        "week_end_day": db.get_setting("week_end_day", config.WEEK_END_DAY),
        "currency": db.get_setting("currency", "ETB"),
        "nutrition_goal_calories": db.get_setting("nutrition_goal_calories", ""),
        "nutrition_goal_protein": db.get_setting("nutrition_goal_protein", ""),
    }
    return render_template("settings.html", values=values)


@app.route("/settings", methods=["POST"])
def save_settings():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    for key in ("tg_bot_token", "tg_chat_id", "timezone", "reminder_hour", "nudge_hour",
                "week_end_day", "currency", "nutrition_goal_calories", "nutrition_goal_protein"):
        val = request.form.get(key)
        if val is None:
            continue  # field wasn't part of the form that was submitted — leave untouched
        if val == "":
            db.delete_setting(key)  # explicit blank = revert to default
            continue
        if key == "timezone":
            try:
                ZoneInfo(val)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                flash(f"'{val}' isn't a valid timezone (e.g. Africa/Addis_Ababa) — not saved.")
                continue
        db.set_setting(key, val)
    flash("Settings saved.")
    return redirect(url_for("settings"))


@app.route("/settings/test-notify", methods=["POST"])
def test_notify():
    ok = telegram_notify.send("✅ Test notification from your Life Hub!")
    flash("Test message sent!" if ok else "Failed to send — check your bot token/chat ID.")
    return redirect(url_for("settings"))


@app.route("/settings/export")
def export_data():
    import json
    tables = [
        "profile", "weight_entries", "sessions", "custom_foods", "food_log",
        "documents", "reminders", "habits", "habit_checkins", "budget_categories",
        "transactions", "recurring_transactions", "savings_goals", "savings_contributions", "settings",
    ]
    data = {}
    with db.get_conn() as conn:
        for t in tables:
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            data[t] = [dict(r) for r in rows]

    payload = json.dumps(data, indent=2, default=str)
    filename = f"lifehub_export_{scheduler.today_local().isoformat()}.json"
    resp = make_response(payload)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


if __name__ == "__main__":
    scheduler.start_scheduler()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Under gunicorn, __main__ isn't executed — start the scheduler here instead.
    scheduler.start_scheduler()
