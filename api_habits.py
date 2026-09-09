"""JSON API for habits — the third slice of the Vercel frontend
migration (after auth, dashboard). Mirrors app.py's habits()/add_habit()/
checkin_habit()/uncheck_habit()/delete_habit()/set_habit_reminder()
routes; see those for the HTML/form-based equivalents this is meant to
replace once the frontend has its own habits page.

Same duplication note as api_dashboard.py: nothing here is imported from
app.py (it would be circular — app.py imports this file before defining
its own routes further down), so anything shared conceptually (like
"daily"/"weekly"/"monthly" being the only valid frequencies) is
duplicated, not imported. If that validation ever changes in app.py,
this file needs the same change made separately.
"""

from flask import Blueprint, jsonify, request

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_habits", __name__, url_prefix="/api/habits")

_VALID_FREQUENCIES = ("daily", "weekly", "monthly")


def _require_user():
    """Returns (user, None) or (None, error_response). Every route below
    starts with `user, err = _require_user(); if err: return err` rather
    than repeating the 401 shape five times."""
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _clamp_reminder_fields(data, frequency):
    """Same rules as app.py's add_habit()/set_habit_reminder(): hour is
    0-23, day only makes sense (and is only kept) for a weekly habit."""
    reminder_hour = data.get("reminder_hour")
    if reminder_hour is not None:
        try:
            reminder_hour = max(0, min(int(reminder_hour), 23))
        except (TypeError, ValueError):
            reminder_hour = None
    reminder_day = data.get("reminder_day")
    if frequency != "weekly" or reminder_day is None:
        reminder_day = None
    else:
        try:
            reminder_day = max(0, min(int(reminder_day), 6))
        except (TypeError, ValueError):
            reminder_day = None
    return reminder_hour, reminder_day


@bp.get("")
def list_habits():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY frequency, title",
            (user_id,),
        ).fetchall()

    today = scheduler.today_local(user_id)
    status_by_id = scheduler.get_habit_status_batch(items)
    status = []
    for h in items:
        s = status_by_id[h["id"]]
        status.append({
            "habit": dict(h),
            "done": s["done"],
            "streak": s["streak"],
            "period_key": scheduler.period_key_for(h["frequency"], today),
        })

    return jsonify({"status": status}), 200


@bp.post("")
def add_habit():
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required."}), 400

    frequency = data.get("frequency", "daily")
    if frequency not in _VALID_FREQUENCIES:
        frequency = "daily"
    reminder_hour, reminder_day = _clamp_reminder_fields(data, frequency)

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO habits (user_id, title, frequency, reminder_hour, reminder_day, active, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (user["id"], title, frequency, reminder_hour, reminder_day, db.now()),
        )
        new_habit = conn.execute(
            "SELECT * FROM habits WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    return jsonify({"habit": dict(new_habit)}), 201


@bp.post("/<int:habit_id>/reminder")
def set_habit_reminder(habit_id):
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    with db.get_conn() as conn:
        habit = conn.execute(
            "SELECT frequency FROM habits WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        ).fetchone()
        if not habit:
            return jsonify({"error": "Habit not found."}), 404
        reminder_hour, reminder_day = _clamp_reminder_fields(data, habit["frequency"])
        conn.execute(
            "UPDATE habits SET reminder_hour = ?, reminder_day = ? WHERE id = ? AND user_id = ?",
            (reminder_hour, reminder_day, habit_id, user["id"]),
        )
    return jsonify({"ok": True}), 200


@bp.post("/<int:habit_id>/checkin")
def checkin_habit(habit_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        h = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        ).fetchone()
        if not h:
            return jsonify({"error": "Habit not found."}), 404
        pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(user["id"]))
        conn.execute(
            "INSERT OR IGNORE INTO habit_checkins (habit_id, period_key, done_at) VALUES (?,?,?)",
            (habit_id, pkey, db.now()),
        )
    return jsonify({"ok": True}), 200


@bp.post("/<int:habit_id>/uncheck")
def uncheck_habit(habit_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        h = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        ).fetchone()
        if not h:
            return jsonify({"error": "Habit not found."}), 404
        pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(user["id"]))
        conn.execute(
            "DELETE FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
            (habit_id, pkey),
        )
    return jsonify({"ok": True}), 200


@bp.post("/<int:habit_id>/delete")
def delete_habit(habit_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE habits SET active = 0 WHERE id = ? AND user_id = ?", (habit_id, user["id"])
        )
    return jsonify({"ok": True}), 200
