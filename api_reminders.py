"""JSON API for reminders and todos — the fourth slice of the Vercel
frontend migration. Mirrors app.py's reminders()/add_reminder()/
snooze_reminder()/toggle_reminder()/delete_reminder() and its
add_todo()/check_todo()/uncheck_todo()/delete_todo() routes. Both live
here together (not split into api_reminders.py + api_todos.py) because
they're one page in the HTML app (the "To Do" page shows both), and
add_reminder() in app.py already splits into a todos-table insert when no
due date is given — the two are one feature, not two.

Same duplication note as the other api_*.py files: nothing imported from
app.py (circular — app.py imports this file before its own routes are
defined further down).
"""

from datetime import date, timedelta

from flask import Blueprint, jsonify, request

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_reminders", __name__, url_prefix="/api")


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


@bp.get("/reminders")
def list_reminders():
    """Returns both reminders and todos together — same pairing as the
    HTML "To Do" page, since the frontend page will show them together
    too."""
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        reminders = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY active DESC, date(next_due)",
            (user["id"],),
        ).fetchall()
        todos = conn.execute(
            "SELECT * FROM todos WHERE user_id = ? ORDER BY done, created_at DESC",
            (user["id"],),
        ).fetchall()
    return jsonify({
        "reminders": [dict(r) for r in reminders],
        "todos": [dict(t) for t in todos],
    }), 200


@bp.post("/reminders")
def add_reminder():
    """Same branch as app.py's add_reminder(): a title with a due date
    becomes a reminder; a title with no due date becomes a plain todo."""
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required."}), 400

    next_due = data.get("next_due") or None
    recurrence = data.get("recurrence", "once")

    with db.get_conn() as conn:
        if next_due:
            cur = conn.execute(
                "INSERT INTO reminders (user_id, title, next_due, recurrence, active, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (user["id"], title, next_due, recurrence, db.now()),
            )
            new_row = conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return jsonify({"kind": "reminder", "reminder": dict(new_row)}), 201
        else:
            cur = conn.execute(
                "INSERT INTO todos (user_id, title, done, created_at) VALUES (?,?,0,?)",
                (user["id"], title, db.now()),
            )
            new_row = conn.execute(
                "SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return jsonify({"kind": "todo", "todo": dict(new_row)}), 201


@bp.post("/reminders/<int:reminder_id>/snooze")
def snooze_reminder(reminder_id):
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days", 1))
    except (TypeError, ValueError):
        days = 1

    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT next_due FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user["id"]),
        ).fetchone()
        if not r:
            return jsonify({"error": "Reminder not found."}), 404
        base = max(date.fromisoformat(r["next_due"]), scheduler.today_local(user["id"]))
        new_due = base + timedelta(days=days)
        conn.execute(
            "UPDATE reminders SET next_due = ?, active = 1 WHERE id = ?",
            (new_due.isoformat(), reminder_id),
        )
    return jsonify({"ok": True, "next_due": new_due.isoformat()}), 200


@bp.post("/reminders/<int:reminder_id>/toggle")
def toggle_reminder(reminder_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT active FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user["id"]),
        ).fetchone()
        if not r:
            return jsonify({"error": "Reminder not found."}), 404
        new_active = 0 if r["active"] else 1
        conn.execute("UPDATE reminders SET active = ? WHERE id = ?", (new_active, reminder_id))
    return jsonify({"ok": True, "active": bool(new_active)}), 200


@bp.post("/reminders/<int:reminder_id>/delete")
def delete_reminder(reminder_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user["id"])
        )
    return jsonify({"ok": True}), 200


@bp.post("/todos/<int:todo_id>/check")
def check_todo(todo_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE todos SET done = 1, completed_at = ? WHERE id = ? AND user_id = ?",
            (db.now(), todo_id, user["id"]),
        )
    return jsonify({"ok": True}), 200


@bp.post("/todos/<int:todo_id>/uncheck")
def uncheck_todo(todo_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE todos SET done = 0, completed_at = NULL WHERE id = ? AND user_id = ?",
            (todo_id, user["id"]),
        )
    return jsonify({"ok": True}), 200


@bp.post("/todos/<int:todo_id>/delete")
def delete_todo(todo_id):
    user, err = _require_user()
    if err:
        return err

    with db.get_conn() as conn:
        conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user["id"]))
    return jsonify({"ok": True}), 200
