"""JSON API for health — the eighth slice of the Vercel frontend
migration. Mirrors app.py's health()/set_height()/add_weight()/
delete_weight()/add_session()/delete_session() routes, plus the
exercise/set-level workout endpoints added alongside workout_session()
in app.py (session/<id>, session/<id>/exercises, exercise/<id>/sets,
exercise/<id>/delete, set/<id>/delete).

Deliberately NOT included: app.py's `weight_sparkline_svg` (a server-
rendered inline SVG chart) — the frontend gets the raw weight_entries
list instead and can draw its own chart with a JS charting lib if/when
that's wanted; duplicating SVG-string-building logic here for a chart
the new frontend won't render the same way didn't seem worth it.

Same duplication note as the other api_*.py files: `_clamped`, `bmi_of`,
`bmi_category`, and `get_or_create_profile` are copied from app.py
rather than imported (circular — app.py imports this file before its own
helpers are defined further down).
"""

from datetime import date

from flask import Blueprint, jsonify, request

import db
from api_auth import get_authenticated_user

bp = Blueprint("api_health", __name__, url_prefix="/api/health")


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _clamped(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(value, hi))


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


@bp.get("")
def health_summary():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        profile = _get_or_create_profile(conn, user_id)
        weights = conn.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30",
            (user_id,),
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30",
            (user_id,),
        ).fetchall()

    latest = weights[0] if weights else None
    bmi = _bmi_of(latest["weight_kg"], profile["height_cm"]) if latest and profile["height_cm"] else None

    return jsonify({
        "profile": dict(profile),
        "weights": [dict(w) for w in weights],
        "sessions": [dict(s) for s in sessions],
        "bmi": bmi,
        "bmi_category": _bmi_category(bmi) if bmi else None,
        "today": date.today().isoformat(),
    }), 200


@bp.post("/profile")
def set_profile():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    height = _clamped(data.get("height_cm"), 30, 300)
    birth_date = (data.get("birth_date") or "").strip() or None
    sex = (data.get("sex") or "").strip() or None

    with db.get_conn() as conn:
        _get_or_create_profile(conn, user_id)
        conn.execute(
            "UPDATE profile SET height_cm = ?, birth_date = ?, sex = ? WHERE user_id = ?",
            (height, birth_date, sex, user_id),
        )
        updated = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    return jsonify({"profile": dict(updated)}), 200


@bp.post("/weight")
def add_weight():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    d = data.get("date") or date.today().isoformat()
    w = _clamped(data.get("weight_kg"), 1, 500)
    if not w:
        return jsonify({"error": "Enter a weight between 1 and 500 kg."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO weight_entries (user_id, date, weight_kg, created_at) VALUES (?, ?, ?, ?)",
            (user_id, d, w, db.now()),
        )
        new_row = conn.execute(
            "SELECT * FROM weight_entries WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify({"entry": dict(new_row)}), 201


@bp.post("/weight/<int:entry_id>/delete")
def delete_weight(entry_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM weight_entries WHERE id = ? AND user_id = ?", (entry_id, user["id"])
        )
    return jsonify({"ok": True}), 200


@bp.post("/session")
def add_session():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    d = data.get("date") or date.today().isoformat()
    stype = (data.get("type") or "").strip()
    try:
        duration = int(data.get("duration_minutes")) if data.get("duration_minutes") is not None else None
    except (TypeError, ValueError):
        duration = None
    notes = (data.get("notes") or "").strip()

    if not stype:
        return jsonify({"error": "Enter a session type."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (user_id, date, type, duration_minutes, notes, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, d, stype, duration, notes, db.now()),
        )
        new_row = conn.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"session": dict(new_row)}), 201


@bp.post("/session/<int:session_id>/delete")
def delete_session(session_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        # No ON DELETE CASCADE relied on here (see workout_exercises'
        # schema comment in db.py) — same explicit-cascade shape as
        # app.py's delete_session().
        exercise_rows = conn.execute(
            "SELECT id FROM workout_exercises WHERE session_id = ? AND user_id = ?",
            (session_id, user["id"]),
        ).fetchall()
        for ex in exercise_rows:
            conn.execute(
                "DELETE FROM workout_sets WHERE exercise_id = ? AND user_id = ?",
                (ex["id"], user["id"]),
            )
        conn.execute(
            "DELETE FROM workout_exercises WHERE session_id = ? AND user_id = ?",
            (session_id, user["id"]),
        )
        conn.execute(
            "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])
        )
    return jsonify({"ok": True}), 200


@bp.get("/session/<int:session_id>")
def get_workout_session(session_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        ).fetchone()
        if not session_row:
            return jsonify({"error": "Session not found."}), 404
        exercises = db.get_workout_exercises(conn, session_id, user_id)

    return jsonify({
        "session": dict(session_row),
        "exercises": [
            {"exercise": dict(item["exercise"]), "sets": item["sets"]}
            for item in exercises
        ],
    }), 200


@bp.post("/session/<int:session_id>/exercises")
def add_workout_exercise(session_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter an exercise name."}), 400

    with db.get_conn() as conn:
        owns_session = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        ).fetchone()
        if not owns_session:
            return jsonify({"error": "Session not found."}), 404
        cur = conn.execute(
            "INSERT INTO workout_exercises (session_id, user_id, name, created_at) VALUES (?,?,?,?)",
            (session_id, user_id, name, db.now()),
        )
        new_row = conn.execute(
            "SELECT * FROM workout_exercises WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify({"exercise": dict(new_row)}), 201


@bp.post("/exercise/<int:exercise_id>/sets")
def add_workout_set(exercise_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    try:
        weight = float(data.get("weight_kg"))
        reps = int(data.get("reps"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a weight and rep count."}), 400
    is_drop_set = 1 if data.get("is_drop_set") else 0

    with db.get_conn() as conn:
        exercise = conn.execute(
            "SELECT session_id FROM workout_exercises WHERE id = ? AND user_id = ?",
            (exercise_id, user_id),
        ).fetchone()
        if not exercise:
            return jsonify({"error": "Exercise not found."}), 404
        cur = conn.execute(
            "INSERT INTO workout_sets (exercise_id, user_id, weight_kg, reps, is_drop_set, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (exercise_id, user_id, weight, reps, is_drop_set, db.now()),
        )
        new_row = conn.execute(
            "SELECT * FROM workout_sets WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify({"set": dict(new_row)}), 201


@bp.post("/exercise/<int:exercise_id>/delete")
def delete_workout_exercise(exercise_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        exercise = conn.execute(
            "SELECT session_id FROM workout_exercises WHERE id = ? AND user_id = ?",
            (exercise_id, user_id),
        ).fetchone()
        if exercise:
            conn.execute(
                "DELETE FROM workout_sets WHERE exercise_id = ? AND user_id = ?",
                (exercise_id, user_id),
            )
            conn.execute(
                "DELETE FROM workout_exercises WHERE id = ? AND user_id = ?",
                (exercise_id, user_id),
            )
    return jsonify({"ok": True}), 200


@bp.post("/set/<int:set_id>/delete")
def delete_workout_set(set_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM workout_sets WHERE id = ? AND user_id = ?", (set_id, user["id"])
        )
    return jsonify({"ok": True}), 200
