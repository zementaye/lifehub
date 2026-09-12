"""JSON API for nutrition — the seventh slice of the Vercel frontend
migration. Mirrors app.py's nutrition()/nutrition_meal()/nutrition_search()/
log_food()/delete_food_log()/add_custom_food()/delete_custom_food() routes.

Same duplication note as the other api_*.py files: `_clamped` and
`get_or_create_profile` are copied from app.py rather than imported
(circular — app.py imports this file before its own helpers are defined
further down). `nutrition_calc` and `nutrition_api` are separate,
standalone modules (not part of app.py itself), so those import
normally with no circularity concern.
"""

from datetime import date, timedelta

from flask import Blueprint, jsonify, request

import db
import nutrition_api
import nutrition_calc
from api_auth import get_authenticated_user

bp = Blueprint("api_nutrition", __name__, url_prefix="/api/nutrition")

MEAL_LABELS = {"breakfast": "🍳 Breakfast", "lunch": "🥗 Lunch", "dinner": "🍽️ Dinner", "snack": "🍎 Snack"}


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _clamped(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(value, hi))


def _get_or_create_profile(conn, user_id):
    profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    if profile is None:
        conn.execute("INSERT INTO profile (user_id, height_cm) VALUES (?, NULL)", (user_id,))
        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    return profile


@bp.get("")
def nutrition_summary():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        log = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ? ORDER BY id", (user_id, d)
        ).fetchall()
        profile = _get_or_create_profile(conn, user_id)
        latest_weight = conn.execute(
            "SELECT weight_kg FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
            (user_id,),
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

    recommendation = nutrition_calc.compute_recommendation(
        height_cm=profile["height_cm"],
        weight_kg=latest_weight["weight_kg"] if latest_weight else None,
        birth_date_str=profile["birth_date"],
        sex=profile["sex"],
        today=date.today(),
        session_count=session_count,
    )

    goals = {
        "calories": float(goal_cal) if goal_cal else (recommendation["calories"] if recommendation else None),
        "protein_g": float(goal_protein) if goal_protein else (recommendation["protein_g"] if recommendation else None),
    }

    return jsonify({
        "date": d,
        "totals": totals,
        "meal_summary": meal_summary,
        "goals": goals,
        "recommendation": recommendation,
    }), 200


@bp.get("/meal/<meal>")
def nutrition_meal(meal):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    if meal not in MEAL_LABELS:
        return jsonify({"error": "Unknown meal."}), 404

    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        entries = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ? AND meal = ? ORDER BY id",
            (user_id, d, meal),
        ).fetchall()
        custom_foods = conn.execute(
            "SELECT * FROM custom_foods WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in entries:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    return jsonify({
        "meal": meal,
        "meal_label": MEAL_LABELS[meal],
        "date": d,
        "entries": [dict(e) for e in entries],
        "custom_foods": [dict(c) for c in custom_foods],
        "totals": totals,
    }), 200


@bp.get("/search")
def nutrition_search():
    user, err = _require_user()
    if err:
        return err
    q = request.args.get("q", "")
    results = nutrition_api.search_foods(q)
    return jsonify({"results": results}), 200


@bp.post("/log")
def log_food():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    d = data.get("date") or date.today().isoformat()
    grams = _clamped(data.get("grams"), 1, 10000)
    servings = (grams / 100.0) if grams is not None else (data.get("servings") or 1.0)
    name = (data.get("name") or "").strip()
    source = data.get("source", "usda")
    meal = data.get("meal", "snack")
    if meal not in MEAL_LABELS:
        meal = "snack"

    if not name:
        return jsonify({"error": "No food name was given."}), 400

    bounds = {"calories": 2000, "protein_g": 100, "carbs_g": 100, "fat_g": 100, "fiber_g": 100}
    fields = {k: (_clamped(data.get(k), 0, hi) or 0.0) for k, hi in bounds.items()}

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO food_log (user_id, date, source, custom_food_id, name, meal, servings, "
            "calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, d, source, data.get("custom_food_id"), name, meal, servings,
             fields["calories"], fields["protein_g"], fields["carbs_g"], fields["fat_g"],
             fields["fiber_g"], db.now()),
        )
        new_row = conn.execute("SELECT * FROM food_log WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"entry": dict(new_row)}), 201


@bp.post("/log/<int:log_id>/delete")
def delete_food_log(log_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute("DELETE FROM food_log WHERE id = ? AND user_id = ?", (log_id, user["id"]))
    return jsonify({"ok": True}), 200


@bp.post("/custom")
def add_custom_food():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a name for the custom food."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO custom_foods (user_id, name, calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, name,
             _clamped(data.get("calories"), 0, 2000) or 0,
             _clamped(data.get("protein_g"), 0, 100) or 0,
             _clamped(data.get("carbs_g"), 0, 100) or 0,
             _clamped(data.get("fat_g"), 0, 100) or 0,
             _clamped(data.get("fiber_g"), 0, 100) or 0,
             db.now()),
        )
        new_row = conn.execute("SELECT * FROM custom_foods WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"food": dict(new_row)}), 201


@bp.post("/custom/<int:food_id>/delete")
def delete_custom_food(food_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute("DELETE FROM custom_foods WHERE id = ? AND user_id = ?", (food_id, user["id"]))
    return jsonify({"ok": True}), 200
