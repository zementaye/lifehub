import logging
import os
import time
import uuid
from datetime import date, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, make_response, session

import auth
import config
import db
import nutrition_api
import nutrition_calc
import scheduler
import storage
import telegram_notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(days=365)

# Changes every time the app process starts (i.e. every deploy/restart).
# Appended as a ?v= query string on static assets in base.html so browsers
# fetch a fresh copy after each deploy instead of serving a stale cached
# style.css/app.js for up to 12 hours (Flask's default static cache time).
app.jinja_env.globals["asset_version"] = str(int(time.time()))

db.init_db()


# ── Auth gate ────────────────────────────────────────────────────────────
# Every page requires a logged-in user, except the auth pages themselves
# and static assets. Individual data routes still call auth.current_user_id()
# to scope their queries — this just blocks anonymous access globally.
_PUBLIC_ENDPOINTS = {"login", "register", "forgot_password", "reset_password", "static"}


@app.before_request
def require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if not auth.current_user_id():
        return redirect(url_for("login", next=request.path))


@app.after_request
def no_cache_static(resp):
    # Belt-and-suspenders on top of the ?v= cache-busting: force browsers to
    # always revalidate style.css/app.js with the server instead of trusting
    # a locally cached copy, no matter how that copy got cached in the past.
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ── Auth routes ──────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if auth.current_user_id():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not email or "@" not in email:
            flash("Enter a valid email address.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        elif password != confirm:
            flash("Passwords don't match.")
        else:
            try:
                user_id = auth.create_user(email, password)
            except ValueError as e:
                flash(str(e))
            else:
                auth.login_user(user_id)
                flash("Welcome to LifeHub!")
                return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user_id():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = auth.get_user_by_email(email)
        if user and auth.verify_password(password, user["password_hash"]):
            auth.login_user(user["id"])
            next_path = request.args.get("next") or url_for("dashboard")
            return redirect(next_path)
        flash("Incorrect email or password.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        user = auth.get_user_by_email(email)
        if user:
            token = auth.create_reset_token(user["id"])
            auth.send_reset_email(user["email"], token)
        # Same message either way — don't reveal whether an email is registered.
        flash("If that email has an account, a reset link is on its way.")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("reset_password.html", token=token)
        if password != confirm:
            flash("Passwords don't match.")
            return render_template("reset_password.html", token=token)
        user_id = auth.consume_reset_token(token)
        if not user_id:
            flash("That reset link is invalid or has expired. Request a new one.")
            return redirect(url_for("forgot_password"))
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (auth.hash_password(password), user_id),
            )
        flash("Password updated — you can log in now.")
        return redirect(url_for("login"))
    return render_template("reset_password.html", token=token)


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
    user_id = auth.current_user_id()
    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
        latest_weight = conn.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1", (user_id,)
        ).fetchone()
        upcoming_reminders = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND active = 1 ORDER BY date(next_due) LIMIT 5", (user_id,)
        ).fetchall()
        habits = conn.execute("SELECT * FROM habits WHERE user_id = ? AND active = 1", (user_id,)).fetchall()
        today_food = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ?", (user_id, today)
        ).fetchall()

    bmi = bmi_of(latest_weight["weight_kg"], profile["height_cm"]) if latest_weight and profile["height_cm"] else None

    habit_status = []
    status_by_id = scheduler.get_habit_status_batch(habits)
    for h in habits:
        s = status_by_id[h["id"]]
        habit_status.append({"habit": h, "done": s["done"], "streak": s["streak"]})

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
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
        weights = conn.execute("SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30", (user_id,)).fetchall()
        sessions = conn.execute("SELECT * FROM sessions WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30", (user_id,)).fetchall()

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
    user_id = auth.current_user_id()
    height = request.form.get("height_cm", type=float)
    birth_date = request.form.get("birth_date", "").strip() or None
    sex = request.form.get("sex", "").strip() or None
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE profile SET height_cm = ?, birth_date = ?, sex = ? WHERE user_id = ?",
            (height, birth_date, sex, user_id),
        )
    flash("Profile updated.")
    return redirect(url_for("health"))


@app.route("/health/weight", methods=["POST"])
def add_weight():
    user_id = auth.current_user_id()
    d = request.form.get("date") or date.today().isoformat()
    w = request.form.get("weight_kg", type=float)
    if w:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO weight_entries (user_id, date, weight_kg, created_at) VALUES (?,?,?,?)",
                (user_id, d, w, db.now()),
            )
        flash("Weight logged.")
    return redirect(url_for("health"))


@app.route("/health/weight/<int:entry_id>/delete", methods=["POST"])
def delete_weight(entry_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM weight_entries WHERE id = ? AND user_id = ?", (entry_id, user_id))
    return redirect(url_for("health"))


@app.route("/health/session", methods=["POST"])
def add_session():
    user_id = auth.current_user_id()
    d = request.form.get("date") or date.today().isoformat()
    stype = request.form.get("type", "").strip()
    duration = request.form.get("duration_minutes", type=int)
    notes = request.form.get("notes", "").strip()
    if stype:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, date, type, duration_minutes, notes, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, d, stype, duration, notes, db.now()),
            )
        flash("Session logged.")
    return redirect(url_for("health"))


@app.route("/health/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
    return redirect(url_for("health"))


# ── Nutrition ────────────────────────────────────────────────────────────

@app.route("/nutrition")
def nutrition():
    user_id = auth.current_user_id()
    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        log = conn.execute("SELECT * FROM food_log WHERE user_id = ? AND date = ? ORDER BY id", (user_id, d)).fetchall()

        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
        latest_weight = conn.execute(
            "SELECT weight_kg FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1", (user_id,)
        ).fetchone()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        session_count = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ? AND date >= ?", (user_id, week_ago)
        ).fetchone()["c"]

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in log:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    meal_order = ["breakfast", "lunch", "dinner", "snack"]
    meal_summary = {m: {"calories": 0.0, "count": 0} for m in meal_order}
    for f in log:
        m = f["meal"] if f["meal"] in meal_order else "snack"
        meal_summary[m]["calories"] += f["calories"] * f["servings"]
        meal_summary[m]["count"] += 1

    goal_cal = db.get_setting(user_id, "nutrition_goal_calories")
    goal_protein = db.get_setting(user_id, "nutrition_goal_protein")
    goals = {
        "calories": float(goal_cal) if goal_cal else None,
        "protein_g": float(goal_protein) if goal_protein else None,
    }

    recommendation = nutrition_calc.compute_recommendation(
        height_cm=profile["height_cm"],
        weight_kg=latest_weight["weight_kg"] if latest_weight else None,
        birth_date_str=profile["birth_date"],
        sex=profile["sex"],
        today=date.today(),
        session_count=session_count,
    )

    return render_template("nutrition.html", totals=totals, meal_summary=meal_summary,
                            view_date=d, goals=goals, recommendation=recommendation)


MEAL_LABELS = {"breakfast": "🍳 Breakfast", "lunch": "🥗 Lunch", "dinner": "🍽️ Dinner", "snack": "🍎 Snack"}


@app.route("/nutrition/meal/<meal>")
def nutrition_meal(meal):
    if meal not in MEAL_LABELS:
        return redirect(url_for("nutrition"))
    user_id = auth.current_user_id()
    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        entries = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ? AND meal = ? ORDER BY id", (user_id, d, meal)
        ).fetchall()
        custom_foods = conn.execute("SELECT * FROM custom_foods WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in entries:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    return render_template("nutrition_meal.html", meal=meal, meal_label=MEAL_LABELS[meal],
                            entries=entries, custom_foods=custom_foods, totals=totals, view_date=d)


@app.route("/nutrition/use-recommendation", methods=["POST"])
def use_recommended_nutrition():
    user_id = auth.current_user_id()
    calories = request.form.get("calories", type=int)
    protein_g = request.form.get("protein_g", type=int)
    if calories:
        db.set_setting(user_id, "nutrition_goal_calories", str(calories))
    if protein_g:
        db.set_setting(user_id, "nutrition_goal_protein", str(protein_g))
    flash("Recommended intake applied as your daily goal.")
    return redirect(url_for("nutrition"))


@app.route("/nutrition/search")
def nutrition_search():
    q = request.args.get("q", "")
    results = nutrition_api.search_foods(q)
    return {"results": results}


@app.route("/nutrition/log", methods=["POST"])
def log_food():
    user_id = auth.current_user_id()
    d = request.form.get("date") or date.today().isoformat()
    grams = request.form.get("grams", type=float)
    if grams is not None:
        servings = grams / 100.0
    else:
        # backward compatible fallback if an old cached page still submits "servings"
        servings = request.form.get("servings", type=float) or 1.0
    name = request.form.get("name", "").strip()
    source = request.form.get("source", "usda")
    meal = request.form.get("meal", "snack")
    if meal not in ("breakfast", "lunch", "dinner", "snack"):
        meal = "snack"

    fields = {}
    for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
        fields[k] = request.form.get(k, type=float) or 0.0

    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO food_log (user_id, date, source, custom_food_id, name, meal, servings, "
                "calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, d, source, request.form.get("custom_food_id", type=int), name, meal, servings,
                 fields["calories"], fields["protein_g"], fields["carbs_g"], fields["fat_g"],
                 fields["fiber_g"], db.now()),
            )
        flash(f"Logged {name}.")
    return redirect(url_for("nutrition_meal", meal=meal, date=d))


@app.route("/nutrition/log/<int:log_id>/delete", methods=["POST"])
def delete_food_log(log_id):
    user_id = auth.current_user_id()
    d = request.form.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        entry = conn.execute("SELECT meal FROM food_log WHERE id = ? AND user_id = ?", (log_id, user_id)).fetchone()
        conn.execute("DELETE FROM food_log WHERE id = ? AND user_id = ?", (log_id, user_id))
    meal = entry["meal"] if entry else "snack"
    return redirect(url_for("nutrition_meal", meal=meal, date=d))


@app.route("/nutrition/custom", methods=["POST"])
def add_custom_food():
    user_id = auth.current_user_id()
    name = request.form.get("name", "").strip()
    meal = request.form.get("meal", "")
    d = request.form.get("date") or date.today().isoformat()
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO custom_foods (user_id, name, calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, name,
                 request.form.get("calories", type=float) or 0,
                 request.form.get("protein_g", type=float) or 0,
                 request.form.get("carbs_g", type=float) or 0,
                 request.form.get("fat_g", type=float) or 0,
                 request.form.get("fiber_g", type=float) or 0,
                 db.now()),
            )
        flash(f"Added custom food: {name}")
    if meal in MEAL_LABELS:
        return redirect(url_for("nutrition_meal", meal=meal, date=d))
    return redirect(url_for("nutrition"))


@app.route("/nutrition/custom/<int:food_id>/delete", methods=["POST"])
def delete_custom_food(food_id):
    user_id = auth.current_user_id()
    meal = request.form.get("meal", "")
    d = request.form.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        food = conn.execute("SELECT name FROM custom_foods WHERE id = ? AND user_id = ?", (food_id, user_id)).fetchone()
        conn.execute("DELETE FROM custom_foods WHERE id = ? AND user_id = ?", (food_id, user_id))
    if food:
        flash(f"Deleted custom food: {food['name']}")
    if meal in MEAL_LABELS:
        return redirect(url_for("nutrition_meal", meal=meal, date=d))
    return redirect(url_for("nutrition"))


# ── ID Vault ─────────────────────────────────────────────────────────────

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "heic", "pdf"}


@app.route("/vault")
def vault():
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM documents WHERE user_id = ? ORDER BY label", (user_id,)).fetchall()

    today = scheduler.today_local(user_id)
    docs = []
    for d in rows:
        entry = dict(d)
        if d["expiry_date"]:
            try:
                entry["days_left"] = (date.fromisoformat(d["expiry_date"]) - today).days
            except ValueError:
                entry["days_left"] = None
        else:
            entry["days_left"] = None
        entry["acknowledged"] = d["expiry_ack_date"] == d["expiry_date"] and d["expiry_date"] is not None
        docs.append(entry)

    upload_token = uuid.uuid4().hex
    session["vault_upload_token"] = upload_token
    return render_template("vault.html", docs=docs, upload_token=upload_token)


@app.route("/vault/upload", methods=["POST"])
def vault_upload():
    user_id = auth.current_user_id()
    label = request.form.get("label", "").strip()
    notes = request.form.get("notes", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip() or None
    file = request.files.get("file")
    submitted_token = request.form.get("upload_token", "")

    # Idempotency guard: the token is minted fresh every time /vault renders
    # and is cleared the instant it's used, so a duplicate submission (a
    # double click that slips past the JS guard, a retried request, a
    # back-button resubmit) carries a stale token and gets silently dropped
    # instead of creating a second document.
    if not submitted_token or submitted_token != session.get("vault_upload_token"):
        flash("That upload was already saved (or the form expired) — refresh and try again if not.")
        return redirect(url_for("vault"))
    session.pop("vault_upload_token", None)

    if not label or not file or file.filename == "":
        flash("Label and file are required.")
        return redirect(url_for("vault"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        flash("Unsupported file type.")
        return redirect(url_for("vault"))

    filename = f"{uuid.uuid4().hex}.{ext}"
    storage.upload_fileobj(file, filename, content_type=file.mimetype)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (user_id, label, filename, notes, expiry_date, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, label, filename, notes, expiry_date, db.now()),
        )

    flash(f"Saved {label}." + (" You'll get expiry reminders automatically as the date approaches." if expiry_date else ""))
    return redirect(url_for("vault"))


@app.route("/vault/<int:doc_id>/renew", methods=["POST"])
def vault_renew(doc_id):
    user_id = auth.current_user_id()
    new_expiry = request.form.get("expiry_date", "").strip() or None
    with db.get_conn() as conn:
        # Renewing to a new date naturally resets the reminder cycle, since
        # expiry_ack_date is cleared and no longer matches the old expiry.
        conn.execute(
            "UPDATE documents SET expiry_date = ?, expiry_ack_date = NULL WHERE id = ? AND user_id = ?",
            (new_expiry, doc_id, user_id),
        )
    flash("Expiry date updated.")
    return redirect(url_for("vault"))


@app.route("/vault/<int:doc_id>/acknowledge", methods=["POST"])
def vault_acknowledge(doc_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id)).fetchone()
        if doc:
            conn.execute(
                "UPDATE documents SET expiry_ack_date = ? WHERE id = ? AND user_id = ?",
                (doc["expiry_date"], doc_id, user_id),
            )
    flash("Got it — reminders paused for this expiry until you renew it.")
    return redirect(url_for("vault"))


@app.route("/vault/file/<filename>")
def vault_file(filename):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT id FROM documents WHERE filename = ? AND user_id = ?", (filename, user_id)
        ).fetchone()
    if not doc:
        return "Not found.", 404
    return redirect(storage.presigned_url(filename))


@app.route("/vault/<int:doc_id>/download")
def vault_download(doc_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id)).fetchone()
    if not doc:
        return redirect(url_for("vault"))
    ext = doc["filename"].rsplit(".", 1)[-1] if "." in doc["filename"] else ""
    safe_label = "".join(c for c in doc["label"] if c.isalnum() or c in " -_").strip() or "document"
    download_name = f"{safe_label}.{ext}" if ext else safe_label
    return redirect(storage.presigned_url(doc["filename"], download_name=download_name))


@app.route("/vault/<int:doc_id>/delete", methods=["POST"])
def vault_delete(doc_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id)).fetchone()
        if doc:
            storage.delete_file(doc["filename"])
            conn.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, user_id))
    return redirect(url_for("vault"))


# ── Reminders ────────────────────────────────────────────────────────────

@app.route("/reminders")
def reminders():
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        items = conn.execute("SELECT * FROM reminders WHERE user_id = ? ORDER BY active DESC, date(next_due)", (user_id,)).fetchall()
    return render_template("reminders.html", reminders=items)


@app.route("/reminders", methods=["POST"])
def add_reminder():
    user_id = auth.current_user_id()
    title = request.form.get("title", "").strip()
    next_due = request.form.get("next_due")
    recurrence = request.form.get("recurrence", "once")
    if title and next_due:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, next_due, recurrence, active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, title, next_due, recurrence, db.now()),
            )
        flash(f"Reminder set: {title}")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/snooze", methods=["POST"])
def snooze_reminder(reminder_id):
    user_id = auth.current_user_id()
    days = request.form.get("days", type=int) or 1
    with db.get_conn() as conn:
        r = conn.execute("SELECT next_due FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)).fetchone()
        if r:
            base = max(date.fromisoformat(r["next_due"]), scheduler.today_local(user_id))
            new_due = base + timedelta(days=days)
            conn.execute("UPDATE reminders SET next_due = ?, active = 1 WHERE id = ? AND user_id = ?",
                         (new_due.isoformat(), reminder_id, user_id))
    flash(f"Snoozed {days} day(s).")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/toggle", methods=["POST"])
def toggle_reminder(reminder_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        r = conn.execute("SELECT active FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)).fetchone()
        if r:
            conn.execute("UPDATE reminders SET active = ? WHERE id = ? AND user_id = ?", (0 if r["active"] else 1, reminder_id, user_id))
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/delete", methods=["POST"])
def delete_reminder(reminder_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
    return redirect(url_for("reminders"))


# ── Habits ───────────────────────────────────────────────────────────────

@app.route("/habits")
def habits():
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        items = conn.execute("SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY frequency, title", (user_id,)).fetchall()
        todos = conn.execute("SELECT * FROM todos WHERE user_id = ? ORDER BY done, created_at DESC", (user_id,)).fetchall()

    today = scheduler.today_local(user_id)
    status = []
    status_by_id = scheduler.get_habit_status_batch(items)
    for h in items:
        pkey = scheduler.period_key_for(h["frequency"], today)
        s = status_by_id[h["id"]]
        status.append({"habit": h, "done": s["done"], "period_key": pkey, "streak": s["streak"]})

    return render_template("habits.html", status=status, todos=todos)


@app.route("/habits", methods=["POST"])
def add_habit():
    user_id = auth.current_user_id()
    title = request.form.get("title", "").strip()
    frequency = request.form.get("frequency", "daily")
    reminder_hour = request.form.get("reminder_hour", type=int)
    if reminder_hour is not None:
        reminder_hour = max(0, min(reminder_hour, 23))
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO habits (user_id, title, frequency, reminder_hour, active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, title, frequency, reminder_hour, db.now()),
            )
        flash(f"Habit added: {title}")
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/reminder", methods=["POST"])
def set_habit_reminder(habit_id):
    user_id = auth.current_user_id()
    reminder_hour = request.form.get("reminder_hour", type=int)
    if reminder_hour is not None:
        reminder_hour = max(0, min(reminder_hour, 23))
    with db.get_conn() as conn:
        conn.execute("UPDATE habits SET reminder_hour = ? WHERE id = ? AND user_id = ?", (reminder_hour, habit_id, user_id))
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/checkin", methods=["POST"])
def checkin_habit(habit_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        h = conn.execute("SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(user_id))
            conn.execute(
                "INSERT OR IGNORE INTO habit_checkins (habit_id, period_key, done_at) VALUES (?,?,?)",
                (habit_id, pkey, db.now()),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/uncheck", methods=["POST"])
def uncheck_habit(habit_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        h = conn.execute("SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(user_id))
            conn.execute(
                "DELETE FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (habit_id, pkey),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
def delete_habit(habit_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("UPDATE habits SET active = 0 WHERE id = ? AND user_id = ?", (habit_id, user_id))
    return redirect(url_for("habits"))


# ── To-Dos (one-time, non-recurring) ────────────────────────────────────

@app.route("/todos", methods=["POST"])
def add_todo():
    user_id = auth.current_user_id()
    title = request.form.get("title", "").strip()
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO todos (user_id, title, done, created_at) VALUES (?,?,0,?)",
                (user_id, title, db.now()),
            )
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/check", methods=["POST"])
def check_todo(todo_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE todos SET done = 1, completed_at = ? WHERE id = ? AND user_id = ?",
            (db.now(), todo_id, user_id),
        )
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/uncheck", methods=["POST"])
def uncheck_todo(todo_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("UPDATE todos SET done = 0, completed_at = NULL WHERE id = ? AND user_id = ?", (todo_id, user_id))
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/delete", methods=["POST"])
def delete_todo(todo_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id))
    return redirect(url_for("habits"))



# ── Budget ───────────────────────────────────────────────────────────────

def _augment_savings_goal(g, user_id):
    """Adds computed fields to a savings_goals row: how much is left to
    save, and — if a target date is set — how much per week that works out
    to given what's already saved and how much time is left."""
    entry = dict(g)
    target = g["target_amount"]
    current = g["current_amount"]
    target_date = g["target_date"]

    entry["remaining"] = max(0, target - current) if target else None
    entry["pct"] = (current / target * 100) if target else None
    entry["days_left"] = None
    entry["weekly_needed"] = None
    entry["status"] = None  # 'reached' | 'overdue' | 'on_track' | None

    if target and current >= target:
        entry["status"] = "reached"
    elif target_date:
        try:
            td = date.fromisoformat(target_date)
        except ValueError:
            td = None
        if td:
            days_left = (td - scheduler.today_local(user_id)).days
            entry["days_left"] = days_left
            if days_left <= 0:
                entry["status"] = "overdue"
            elif target:
                weeks_left = max(days_left / 7, 1 / 7)  # never divide by zero
                entry["weekly_needed"] = entry["remaining"] / weeks_left
                entry["status"] = "on_track"

    return entry

@app.route("/budget")
def budget():
    user_id = auth.current_user_id()
    month = request.args.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    currency = db.get_setting(user_id, "currency", "ETB")

    with db.get_conn() as conn:
        categories = conn.execute("SELECT * FROM budget_categories WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()
        txns = conn.execute(
            "SELECT t.*, c.name AS category_name FROM transactions t "
            "LEFT JOIN budget_categories c ON c.id = t.category_id "
            "WHERE t.user_id = ? AND strftime('%Y-%m', t.date) = ? ORDER BY t.date DESC, t.id DESC",
            (user_id, month),
        ).fetchall()
        recurring = conn.execute(
            "SELECT r.*, c.name AS category_name FROM recurring_transactions r "
            "LEFT JOIN budget_categories c ON c.id = r.category_id "
            "WHERE r.user_id = ? ORDER BY r.active DESC, r.next_run",
            (user_id,),
        ).fetchall()
        savings_goals_raw = conn.execute(
            "SELECT * FROM savings_goals WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()
        savings_goals = [_augment_savings_goal(g, user_id) for g in savings_goals_raw]

        income_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id = ? AND type='income' AND strftime('%Y-%m', date)=?",
            (user_id, month),
        ).fetchone()["t"]
        expense_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id = ? AND type='expense' AND strftime('%Y-%m', date)=?",
            (user_id, month),
        ).fetchone()["t"]

        category_spend = []
        for c in categories:
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
                "WHERE user_id = ? AND type='expense' AND category_id=? AND strftime('%Y-%m', date)=?",
                (user_id, c["id"], month),
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
    user_id = auth.current_user_id()
    month = request.form.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    d = request.form.get("date") or date.today().isoformat()
    ttype = request.form.get("type", "expense")
    amount = request.form.get("amount", type=float)
    description = request.form.get("description", "").strip()
    category_id = request.form.get("category_id", type=int) or None

    if amount:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, d, ttype, category_id if ttype == "expense" else None, description, abs(amount), db.now()),
            )
        flash(f"Logged {'income' if ttype == 'income' else 'expense'}: {amount}")
    return redirect(url_for("budget", month=month))


@app.route("/budget/transaction/<int:txn_id>/delete", methods=["POST"])
def delete_transaction(txn_id):
    user_id = auth.current_user_id()
    month = request.form.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (txn_id, user_id))
    return redirect(url_for("budget", month=month))


@app.route("/budget/category", methods=["POST"])
def add_budget_category():
    user_id = auth.current_user_id()
    name = request.form.get("name", "").strip()
    limit = request.form.get("monthly_limit", type=float)
    if name:
        with db.get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM budget_categories WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
            if existing:
                flash(f"'{name}' already exists.")
            else:
                conn.execute(
                    "INSERT INTO budget_categories (user_id, name, monthly_limit, created_at) VALUES (?,?,?,?)",
                    (user_id, name, limit, db.now()),
                )
                flash(f"Category added: {name}")
    return redirect(url_for("budget"))


@app.route("/budget/category/<int:cat_id>/delete", methods=["POST"])
def delete_budget_category(cat_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM budget_categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
    return redirect(url_for("budget"))


@app.route("/budget/recurring", methods=["POST"])
def add_recurring_transaction():
    import calendar
    from datetime import timedelta

    user_id = auth.current_user_id()
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
    start_raw = request.form.get("start_date") or scheduler.today_local(user_id).isoformat()

    if title and amount:
        if len(start_raw) == 7:  # "YYYY-MM" from the month picker (monthly only)
            start_raw += "-01"
        start = date.fromisoformat(start_raw)

        if frequency == "weekly":
            next_run = start
            while next_run.weekday() != day_of_week:
                next_run += timedelta(days=1)
            while next_run < scheduler.today_local(user_id):
                next_run += timedelta(days=7)
        else:
            clamped_day = min(day_of_month, calendar.monthrange(start.year, start.month)[1])
            next_run = start.replace(day=clamped_day)
            if next_run < scheduler.today_local(user_id):
                next_run = scheduler.add_months(next_run, 1)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO recurring_transactions "
                "(user_id, title, type, amount, category_id, frequency, day_of_month, day_of_week, next_run, active, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (user_id, title, ttype, amount, category_id if ttype == "expense" else None,
                 frequency, day_of_month, day_of_week if frequency == "weekly" else None,
                 next_run.isoformat(), db.now()),
            )
        flash(f"Recurring {ttype} set up: {title} — first run {next_run.isoformat()}")
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/toggle", methods=["POST"])
def toggle_recurring_transaction(rid):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        r = conn.execute("SELECT active FROM recurring_transactions WHERE id = ? AND user_id = ?", (rid, user_id)).fetchone()
        if r:
            conn.execute("UPDATE recurring_transactions SET active = ? WHERE id = ? AND user_id = ?",
                         (0 if r["active"] else 1, rid, user_id))
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/delete", methods=["POST"])
def delete_recurring_transaction(rid):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM recurring_transactions WHERE id = ? AND user_id = ?", (rid, user_id))
    return redirect(url_for("budget"))


# ── Savings goals ────────────────────────────────────────────────────────

@app.route("/budget/savings", methods=["POST"])
def add_savings_goal():
    user_id = auth.current_user_id()
    name = request.form.get("name", "").strip()
    target_amount = request.form.get("target_amount", type=float)
    target_date = request.form.get("target_date", "").strip() or None
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO savings_goals (user_id, name, target_amount, target_date, current_amount, created_at) "
                "VALUES (?,?,?,?,0,?)",
                (user_id, name, target_amount, target_date, db.now()),
            )
        flash(f"Savings goal created: {name}")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/target", methods=["POST"])
def set_savings_target(goal_id):
    user_id = auth.current_user_id()
    target_date = request.form.get("target_date", "").strip() or None
    with db.get_conn() as conn:
        conn.execute("UPDATE savings_goals SET target_date = ? WHERE id = ? AND user_id = ?", (target_date, goal_id, user_id))
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/contribute", methods=["POST"])
def contribute_savings_goal(goal_id):
    user_id = auth.current_user_id()
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local(user_id).isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute("SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)).fetchone()
            if goal:
                # Money moving into savings reduces what's available to spend,
                # so it's logged as a normal expense transaction — the goal's
                # progress bar and your budget totals both stay accurate.
                cur = conn.execute(
                    "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, today, "expense", None, f"Savings: {goal['name']}", amount, db.now()),
                )
                conn.execute(
                    "INSERT INTO savings_contributions (goal_id, date, amount, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (goal_id, today, amount, cur.lastrowid, db.now()),
                )
                conn.execute(
                    "UPDATE savings_goals SET current_amount = current_amount + ? WHERE id = ? AND user_id = ?",
                    (amount, goal_id, user_id),
                )
        flash(f"Added {amount:.0f} to savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/withdraw", methods=["POST"])
def withdraw_savings_goal(goal_id):
    user_id = auth.current_user_id()
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local(user_id).isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute("SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)).fetchone()
            if goal:
                amount = min(amount, goal["current_amount"])  # can't withdraw more than saved
                # Money coming back out of savings adds back to what's
                # available to spend, so it's logged as income.
                cur = conn.execute(
                    "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, today, "income", None, f"Savings withdrawal: {goal['name']}", amount, db.now()),
                )
                conn.execute(
                    "INSERT INTO savings_contributions (goal_id, date, amount, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (goal_id, today, -amount, cur.lastrowid, db.now()),
                )
                conn.execute(
                    "UPDATE savings_goals SET current_amount = current_amount - ? WHERE id = ? AND user_id = ?",
                    (amount, goal_id, user_id),
                )
        flash(f"Withdrew {amount:.0f} from savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/delete", methods=["POST"])
def delete_savings_goal(goal_id):
    user_id = auth.current_user_id()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
    return redirect(url_for("budget"))


# ── Settings ─────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    user_id = auth.current_user_id()
    user = auth.get_user_by_id(user_id)
    values = {
        "tg_bot_token": db.get_setting(user_id, "tg_bot_token", config.TG_BOT_TOKEN),
        "tg_chat_id": db.get_setting(user_id, "tg_chat_id", config.TG_CHAT_ID),
        "timezone": db.get_setting(user_id, "timezone", config.TIMEZONE),
        "reminder_hour": db.get_setting(user_id, "reminder_hour", config.REMINDER_HOUR),
        "nudge_hour": db.get_setting(user_id, "nudge_hour", config.NUDGE_HOUR),
        "week_end_day": db.get_setting(user_id, "week_end_day", config.WEEK_END_DAY),
        "currency": db.get_setting(user_id, "currency", "ETB"),
        "nutrition_goal_calories": db.get_setting(user_id, "nutrition_goal_calories", ""),
        "nutrition_goal_protein": db.get_setting(user_id, "nutrition_goal_protein", ""),
    }
    return render_template("settings.html", values=values, user=user)


@app.route("/settings", methods=["POST"])
def save_settings():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    user_id = auth.current_user_id()
    for key in ("tg_bot_token", "tg_chat_id", "timezone", "reminder_hour", "nudge_hour",
                "week_end_day", "currency", "nutrition_goal_calories", "nutrition_goal_protein"):
        val = request.form.get(key)
        if val is None:
            continue  # field wasn't part of the form that was submitted — leave untouched
        val = val.strip()  # copy-pasted tokens/IDs often carry a stray space or newline
        if val == "":
            db.delete_setting(user_id, key)  # explicit blank = revert to default
            continue
        if key == "timezone":
            try:
                ZoneInfo(val)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                flash(f"'{val}' isn't a valid timezone (e.g. Africa/Addis_Ababa) — not saved.")
                continue
        db.set_setting(user_id, key, val)
    flash("Settings saved.")
    return redirect(url_for("settings"))


@app.route("/settings/test-notify", methods=["POST"])
def test_notify():
    user_id = auth.current_user_id()
    ok, err = telegram_notify.send_detailed(user_id, "✅ Test notification from your Life Hub!")
    if ok:
        flash("Test message sent!")
    else:
        flash(f"Telegram said: {err}")
    return redirect(url_for("settings"))


@app.route("/settings/export")
def export_data():
    import json
    user_id = auth.current_user_id()
    direct_tables = [
        "profile", "weight_entries", "sessions", "custom_foods", "food_log",
        "documents", "reminders", "habits", "budget_categories",
        "transactions", "recurring_transactions", "savings_goals",
    ]
    data = {}
    with db.get_conn() as conn:
        for t in direct_tables:
            rows = conn.execute(f"SELECT * FROM {t} WHERE user_id = ?", (user_id,)).fetchall()
            data[t] = [dict(r) for r in rows]

        data["habit_checkins"] = [dict(r) for r in conn.execute(
            "SELECT hc.* FROM habit_checkins hc JOIN habits h ON h.id = hc.habit_id WHERE h.user_id = ?",
            (user_id,),
        ).fetchall()]
        data["savings_contributions"] = [dict(r) for r in conn.execute(
            "SELECT sc.* FROM savings_contributions sc JOIN savings_goals sg ON sg.id = sc.goal_id WHERE sg.user_id = ?",
            (user_id,),
        ).fetchall()]
        data["settings"] = [dict(r) for r in conn.execute(
            "SELECT * FROM settings WHERE user_id = ?", (user_id,)
        ).fetchall()]

    payload = json.dumps(data, indent=2, default=str)
    filename = f"lifehub_export_{scheduler.today_local(user_id).isoformat()}.json"
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
