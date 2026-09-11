"""JSON API for the calendar — the fifth slice of the Vercel frontend
migration. Mirrors app.py's calendar_view() route: a month grid with
events pulled from reminders (due dates), todos (added dates), vault
documents (added + expiry dates), and notes/shared-notes with an
optional recurrence (birthdays etc., projected on the fly — nothing
per-occurrence is stored).

Scope note: this is a READ-ONLY calendar for now. Creating/editing notes
(with their image and voice-memo attachments) is deliberately left for
the separate "Notes" slice later in the roadmap — bundling note creation
in here would mean re-doing the file-upload work this slice doesn't need
yet. `add_note()` etc. in app.py are untouched; the old HTML calendar
page still has full note-creation.

Same duplication note as the other api_*.py files: `_note_occurrences_in_range`
is duplicated verbatim from app.py rather than imported (circular —
app.py imports this file before its own helpers are defined further
down). If app.py's version changes, update this copy too.
"""

import calendar as calendar_mod
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_calendar", __name__, url_prefix="/api")

_NOTE_RECURRENCE_LOOP_CAP = 1000  # safety net, not a realistic ceiling


def _note_occurrences_in_range(anchor, recurrence, start, end):
    """Verbatim copy of app.py's helper of the same name — see there for
    the full explanation of each recurrence branch."""
    if anchor > end:
        return []

    if recurrence == "weekly":
        occs = []
        cur = anchor
        while cur < start:
            cur += timedelta(days=7)
        i = 0
        while cur <= end and i < _NOTE_RECURRENCE_LOOP_CAP:
            occs.append(cur)
            cur += timedelta(days=7)
            i += 1
        return occs

    if recurrence == "monthly":
        occs = []
        y, m = anchor.year, anchor.month
        i = 0
        while i < _NOTE_RECURRENCE_LOOP_CAP:
            days_in = calendar_mod.monthrange(y, m)[1]
            candidate = date(y, m, min(anchor.day, days_in))
            if candidate > end:
                break
            if candidate >= start:
                occs.append(candidate)
            m += 1
            if m > 12:
                m = 1
                y += 1
            i += 1
        return occs

    if recurrence == "yearly":
        occs = []
        y = anchor.year
        i = 0
        while i < _NOTE_RECURRENCE_LOOP_CAP:
            try:
                candidate = date(y, anchor.month, anchor.day)
            except ValueError:
                candidate = date(y, anchor.month, 28)
            if candidate > end:
                break
            if candidate >= start:
                occs.append(candidate)
            y += 1
            i += 1
        return occs

    return [anchor] if start <= anchor <= end else []


@bp.get("/calendar")
def calendar_view():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Not authenticated."}), 401
    user_id = user["id"]

    tz = scheduler.get_tz(user_id)
    today = scheduler.today_local(user_id)

    month_param = request.args.get("month")
    first_of_month = None
    if month_param:
        try:
            y, mo = month_param.split("-")
            first_of_month = date(int(y), int(mo), 1)
        except (ValueError, TypeError):
            first_of_month = None
    if first_of_month is None:
        first_of_month = date(today.year, today.month, 1)

    year, month = first_of_month.year, first_of_month.month
    days_in_month = calendar_mod.monthrange(year, month)[1]
    last_of_month = date(year, month, days_in_month)
    prev_month = (first_of_month - timedelta(days=1)).replace(day=1)
    next_month = last_of_month + timedelta(days=1)

    events_by_day = {d: [] for d in range(1, days_in_month + 1)}

    with db.get_conn() as conn:
        due_reminders = conn.execute(
            "SELECT title, next_due, recurrence, active FROM reminders "
            "WHERE user_id = ? AND next_due BETWEEN ? AND ?",
            (user_id, first_of_month.isoformat(), last_of_month.isoformat()),
        ).fetchall()
        todo_rows = conn.execute(
            "SELECT title, created_at, done FROM todos WHERE user_id = ?", (user_id,)
        ).fetchall()
        doc_rows = conn.execute(
            "SELECT label, expiry_date, created_at FROM documents WHERE user_id = ?", (user_id,)
        ).fetchall()
        note_rows = conn.execute(
            "SELECT title, linked_date, recurrence FROM notes WHERE user_id = ? AND linked_date IS NOT NULL",
            (user_id,),
        ).fetchall()

    shared_rows = db.shared_dates_for_calendar(user_id)

    for r in due_reminders:
        d = date.fromisoformat(r["next_due"])
        events_by_day[d.day].append({
            "type": "reminder",
            "state": "active" if r["active"] else "paused",
            "title": r["title"],
            "sublabel": "due" if r["recurrence"] == "once" else f"due · repeats {r['recurrence']}",
        })

    for t in todo_rows:
        d = datetime.fromtimestamp(t["created_at"], tz=tz).date()
        if d.year == year and d.month == month:
            events_by_day[d.day].append({
                "type": "todo",
                "state": "done" if t["done"] else "open",
                "title": t["title"],
                "sublabel": "added",
            })

    for doc in doc_rows:
        added = datetime.fromtimestamp(doc["created_at"], tz=tz).date()
        if added.year == year and added.month == month:
            events_by_day[added.day].append({
                "type": "doc",
                "state": "added",
                "title": doc["label"],
                "sublabel": "added to vault",
            })
        if doc["expiry_date"]:
            try:
                exp = date.fromisoformat(doc["expiry_date"])
            except ValueError:
                exp = None
            if exp and exp.year == year and exp.month == month:
                days_left = (exp - today).days
                state = "overdue" if days_left < 0 else ("soon" if days_left <= 30 else "ok")
                events_by_day[exp.day].append({
                    "type": "doc",
                    "state": state,
                    "title": doc["label"],
                    "sublabel": "expires",
                })

    for n in note_rows:
        anchor = date.fromisoformat(n["linked_date"])
        recurrence = n["recurrence"] or "once"
        for occ in _note_occurrences_in_range(anchor, recurrence, first_of_month, last_of_month):
            events_by_day[occ.day].append({
                "type": "note",
                "state": "added",
                "title": n["title"],
                "sublabel": "note" if recurrence == "once" else f"note · repeats {recurrence}",
            })

    for s in shared_rows:
        anchor = date.fromisoformat(s["linked_date"])
        recurrence = s["recurrence"] or "once"
        for occ in _note_occurrences_in_range(anchor, recurrence, first_of_month, last_of_month):
            events_by_day[occ.day].append({
                "type": "shared",
                "state": "added",
                "title": s["title"],
                "sublabel": (
                    f"shared by {s['owner_email']}" if recurrence == "once"
                    else f"shared by {s['owner_email']} · repeats {recurrence}"
                ),
            })

    leading_blanks = first_of_month.weekday()  # Monday = 0, matches the rest of the app
    prev_days_in_month = calendar_mod.monthrange(prev_month.year, prev_month.month)[1]
    leading_cells = [
        {"day": d, "in_month": False, "date": prev_month.replace(day=d).isoformat()}
        for d in range(prev_days_in_month - leading_blanks + 1, prev_days_in_month + 1)
    ]
    current_cells = [
        {"day": d, "in_month": True, "date": date(year, month, d).isoformat()}
        for d in range(1, days_in_month + 1)
    ]
    cells = leading_cells + current_cells
    trailing_day = 1
    while len(cells) % 7 != 0:
        cells.append({
            "day": trailing_day, "in_month": False,
            "date": next_month.replace(day=trailing_day).isoformat(),
        })
        trailing_day += 1
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]

    return jsonify({
        "weeks": weeks,
        "events_by_day": {str(k): v for k, v in events_by_day.items()},
        "month_label": first_of_month.strftime("%B %Y"),
        "month_value": first_of_month.strftime("%Y-%m"),
        "today": today.isoformat(),
        "is_current_month": (year == today.year and month == today.month),
        "prev_month": prev_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "this_month": today.strftime("%Y-%m"),
    }), 200
