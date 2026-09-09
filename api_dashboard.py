"""JSON API for the dashboard page — the second slice of the Vercel
frontend migration (after auth). Mirrors app.py's dashboard() route
closely; see that route for the HTML page this is meant to replace once
the frontend has its own dashboard.

Deliberately duplicates a few small pure-logic helpers from app.py
(bmi calculation, category label, get-or-create-profile) rather than
importing them from app.py: app.py imports THIS file to register the
blueprint, before those functions are defined further down in app.py, so
importing them back would be circular. They're small and stable enough
that duplication is the lower-risk option — if they ever change, both
copies need updating (search "def bmi_of" / "_bmi_of" to find both).

Deliberately NOT included yet: the "Next Up" card (_next_up_summary in
app.py) — it pulls from four different tables plus a recurrence-expansion
helper, more to duplicate than seemed worth it for this slice. Left for a
follow-up once the frontend dashboard actually needs it.
"""

from flask import Blueprint, jsonify

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_dashboard", __name__, url_prefix="/api")


def _bmi_of(weight_kg, height_cm):
    if not weight_kg or not height_cm:
        return None
    h_m = height_cm / 100
    return round(weight_kg / (h_m * h_m), 1)


def _bmi_category(bmi):
    if bmi < 18.5:
        return "Underweight"
    if bmi < 25:
        return "Normal"
    if bmi < 30:
        return "Overweight"
    return "Obese"


def _get_or_create_profile(conn, user_id):
    profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    if profile is None:
        conn.execute("INSERT INTO profile (user_id, height_cm) VALUES (?, NULL)", (user_id,))
        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    return profile


@bp.get("/dashboard")
def dashboard():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Not authenticated."}), 401
    user_id = user["id"]

    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        profile = _get_or_create_profile(conn, user_id)
        latest_weight = conn.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        upcoming_reminders = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND active = 1 ORDER BY date(next_due) LIMIT 5",
            (user_id,),
        ).fetchall()
        habits = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1", (user_id,)
        ).fetchall()
        today_food = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ?", (user_id, today)
        ).fetchall()

    bmi = _bmi_of(latest_weight["weight_kg"], profile["height_cm"]) if latest_weight and profile["height_cm"] else None

    habit_status = []
    status_by_id = scheduler.get_habit_status_batch(habits)
    for h in habits:
        s = status_by_id[h["id"]]
        habit_status.append({"habit": dict(h), "done": s["done"], "streak": s["streak"]})

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in today_food:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    return jsonify({
        "profile": dict(profile),
        "latest_weight": dict(latest_weight) if latest_weight else None,
        "bmi": bmi,
        "bmi_category": _bmi_category(bmi) if bmi else None,
        "upcoming_reminders": [dict(r) for r in upcoming_reminders],
        "habit_status": habit_status,
        "totals": totals,
    }), 200
