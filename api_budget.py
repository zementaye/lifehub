"""JSON API for budget — the sixth slice of the Vercel frontend
migration. Mirrors app.py's budget()/add_transaction()/delete_transaction()/
add_budget_category()/delete_budget_category()/add_recurring_transaction()/
toggle_recurring_transaction()/delete_recurring_transaction()/
add_savings_goal()/set_savings_target()/contribute_savings_goal()/
withdraw_savings_goal()/delete_savings_goal() routes.

Scope note — two things deliberately NOT carried over from app.py's
budget() route:
1. The yearly summary/chart section (month-by-month income/expense
   breakdown for a whole year). It's a separate block of queries on top
   of the already-large monthly view; left for a follow-up once the
   frontend budget page actually needs a yearly chart.
2. Background AI auto-categorization of an expense's category when left
   blank (app.py's _auto_categorize_transaction, fired via a background
   thread from add_transaction()). Left for the "AI features" slice
   later in the roadmap, so this didn't need to duplicate the ai.py
   integration for one field. Transactions saved here just stay
   uncategorized if no category_id is given, same as if the AI call had
   failed/been unavailable on the HTML side.

Same duplication note as the other api_*.py files: `_augment_savings_goal`
and `_clamped` are copied from app.py rather than imported (circular —
app.py imports this file before its own helpers are defined further
down).
"""

import calendar as calendar_mod
from datetime import date, timedelta

from flask import Blueprint, jsonify, request

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_budget", __name__, url_prefix="/api/budget")


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _clamped(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(value, hi))


def _augment_savings_goal(g_row, user_id):
    entry = dict(g_row)
    target = g_row["target_amount"]
    current = g_row["current_amount"]
    target_date = g_row["target_date"]

    entry["remaining"] = max(0, target - current) if target else None
    entry["pct"] = (current / target * 100) if target else None
    entry["days_left"] = None
    entry["weekly_needed"] = None
    entry["status"] = None

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
                weeks_left = max(days_left / 7, 1 / 7)
                entry["weekly_needed"] = entry["remaining"] / weeks_left
                entry["status"] = "on_track"

    return entry


@bp.get("")
def budget_summary():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    month = request.args.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    currency = db.get_setting(user_id, "currency", "ETB") or "ETB"

    with db.get_conn() as conn:
        categories = conn.execute(
            "SELECT * FROM budget_categories WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()
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
        savings_goals = [_augment_savings_goal(sg, user_id) for sg in savings_goals_raw]

        income_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
            "WHERE user_id = ? AND type='income' AND strftime('%Y-%m', date)=?",
            (user_id, month),
        ).fetchone()["t"]
        expense_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
            "WHERE user_id = ? AND type='expense' AND strftime('%Y-%m', date)=?",
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
                "category": dict(c), "spent": spent, "limit": limit, "pct": pct,
                "over": bool(limit and spent > limit),
            })

    return jsonify({
        "month": month,
        "currency": currency,
        "categories": [dict(c) for c in categories],
        "txns": [dict(t) for t in txns],
        "recurring": [dict(r) for r in recurring],
        "savings_goals": savings_goals,
        "income_total": income_total,
        "expense_total": expense_total,
        "net": income_total - expense_total,
        "category_spend": category_spend,
    }), 200


@bp.post("/transaction")
def add_transaction():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    d = data.get("date") or scheduler.today_local(user_id).isoformat()
    ttype = data.get("type", "expense")
    if ttype not in ("income", "expense"):
        ttype = "expense"
    amount = data.get("amount")
    try:
        amount = min(abs(float(amount)), 1_000_000_000) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    description = (data.get("description") or "").strip()
    category_id = data.get("category_id")

    if not amount:
        return jsonify({"error": "Enter a valid, non-zero amount."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, d, ttype, category_id if ttype == "expense" else None, description, amount, db.now()),
        )
        new_row = conn.execute(
            "SELECT t.*, c.name AS category_name FROM transactions t "
            "LEFT JOIN budget_categories c ON c.id = t.category_id WHERE t.id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return jsonify({"transaction": dict(new_row)}), 201


@bp.post("/transaction/<int:txn_id>/delete")
def delete_transaction(txn_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (txn_id, user["id"]))
    return jsonify({"ok": True}), 200


@bp.post("/category")
def add_category():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a category name."}), 400
    try:
        limit = _clamped(float(data["monthly_limit"]), 0, 1_000_000_000) if data.get("monthly_limit") else None
    except (TypeError, ValueError):
        limit = None

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM budget_categories WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if existing:
            return jsonify({"error": f"'{name}' already exists."}), 409
        cur = conn.execute(
            "INSERT INTO budget_categories (user_id, name, monthly_limit, created_at) VALUES (?,?,?,?)",
            (user_id, name, limit, db.now()),
        )
        new_row = conn.execute(
            "SELECT * FROM budget_categories WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify({"category": dict(new_row)}), 201


@bp.post("/category/<int:cat_id>/delete")
def delete_category(cat_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM budget_categories WHERE id = ? AND user_id = ?", (cat_id, user["id"])
        )
    return jsonify({"ok": True}), 200


@bp.post("/recurring")
def add_recurring():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    ttype = data.get("type", "expense")
    if ttype not in ("income", "expense"):
        ttype = "expense"
    try:
        amount = min(abs(float(data.get("amount"))), 1_000_000_000)
    except (TypeError, ValueError):
        amount = None
    category_id = data.get("category_id")
    frequency = data.get("frequency", "monthly")
    if frequency not in ("monthly", "weekly"):
        frequency = "monthly"
    day_of_month = max(1, min(int(data.get("day_of_month") or 1), 28))
    day_of_week = data.get("day_of_week")
    day_of_week = 0 if day_of_week is None else max(0, min(int(day_of_week), 6))
    start_raw = data.get("start_date") or scheduler.today_local(user_id).isoformat()

    if not title or not amount:
        return jsonify({"error": "Enter a title and a valid amount."}), 400

    if len(start_raw) == 7:
        start_raw += "-01"
    start = date.fromisoformat(start_raw)

    if frequency == "weekly":
        next_run = start
        while next_run.weekday() != day_of_week:
            next_run += timedelta(days=1)
        while next_run < scheduler.today_local(user_id):
            next_run += timedelta(days=7)
    else:
        clamped_day = min(day_of_month, calendar_mod.monthrange(start.year, start.month)[1])
        next_run = start.replace(day=clamped_day)
        if next_run < scheduler.today_local(user_id):
            next_run = scheduler.add_months(next_run, 1)

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recurring_transactions "
            "(user_id, title, type, amount, category_id, frequency, day_of_month, day_of_week, next_run, active, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (user_id, title, ttype, amount, category_id if ttype == "expense" else None,
             frequency, day_of_month, day_of_week if frequency == "weekly" else None,
             next_run.isoformat(), db.now()),
        )
        new_row = conn.execute(
            "SELECT r.*, c.name AS category_name FROM recurring_transactions r "
            "LEFT JOIN budget_categories c ON c.id = r.category_id WHERE r.id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return jsonify({"recurring": dict(new_row)}), 201


@bp.post("/recurring/<int:rid>/toggle")
def toggle_recurring(rid):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT active FROM recurring_transactions WHERE id = ? AND user_id = ?",
            (rid, user["id"]),
        ).fetchone()
        if not r:
            return jsonify({"error": "Not found."}), 404
        new_active = 0 if r["active"] else 1
        conn.execute("UPDATE recurring_transactions SET active = ? WHERE id = ?", (new_active, rid))
    return jsonify({"ok": True, "active": bool(new_active)}), 200


@bp.post("/recurring/<int:rid>/delete")
def delete_recurring(rid):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM recurring_transactions WHERE id = ? AND user_id = ?", (rid, user["id"])
        )
    return jsonify({"ok": True}), 200


@bp.post("/savings")
def add_savings_goal():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a name for the savings goal."}), 400
    try:
        target_amount = _clamped(float(data["target_amount"]), 0, 1_000_000_000) if data.get("target_amount") else None
    except (TypeError, ValueError):
        target_amount = None
    target_date = (data.get("target_date") or "").strip() or None

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO savings_goals (user_id, name, target_amount, target_date, current_amount, created_at) "
            "VALUES (?,?,?,?,0,?)",
            (user_id, name, target_amount, target_date, db.now()),
        )
        new_row = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return jsonify({"goal": _augment_savings_goal(new_row, user_id)}), 201


@bp.post("/savings/<int:goal_id>/target")
def set_savings_target(goal_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    target_date = (data.get("target_date") or "").strip() or None
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE savings_goals SET target_date = ? WHERE id = ? AND user_id = ?",
            (target_date, goal_id, user["id"]),
        )
    return jsonify({"ok": True}), 200


@bp.post("/savings/<int:goal_id>/contribute")
def contribute_savings_goal(goal_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    try:
        amount = _clamped(float(data.get("amount")), 0, 1_000_000_000)
    except (TypeError, ValueError):
        amount = None
    if not amount or amount <= 0:
        return jsonify({"error": "Enter a valid, positive amount."}), 400

    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        ).fetchone()
        if not goal:
            return jsonify({"error": "Goal not found."}), 404
        # Money moving into savings reduces what's available to spend, so
        # it's logged as a normal expense transaction — the goal's
        # progress bar and the budget totals both stay accurate.
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
            "UPDATE savings_goals SET current_amount = current_amount + ? WHERE id = ?",
            (amount, goal_id),
        )
        updated = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
    return jsonify({"goal": _augment_savings_goal(updated, user_id)}), 200


@bp.post("/savings/<int:goal_id>/withdraw")
def withdraw_savings_goal(goal_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    try:
        amount = _clamped(float(data.get("amount")), 0, 1_000_000_000)
    except (TypeError, ValueError):
        amount = None
    if not amount or amount <= 0:
        return jsonify({"error": "Enter a valid, positive amount."}), 400

    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        ).fetchone()
        if not goal:
            return jsonify({"error": "Goal not found."}), 404
        amount = min(amount, goal["current_amount"])  # can't withdraw more than saved
        # Money coming back out of savings adds back to what's available
        # to spend, so it's logged as income.
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
            "UPDATE savings_goals SET current_amount = current_amount - ? WHERE id = ?",
            (amount, goal_id),
        )
        updated = conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
    return jsonify({"goal": _augment_savings_goal(updated, user_id)}), 200


@bp.post("/savings/<int:goal_id>/delete")
def delete_savings_goal(goal_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user["id"])
        )
    return jsonify({"ok": True}), 200
