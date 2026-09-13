"""JSON API for notifications — the tenth slice of the Vercel frontend
migration. Mirrors app.py's notifications() route and its
inject_unread_notification_count() context processor.

Small feature, no duplication needed this time — db.py's
list_notifications()/unread_notification_count()/mark_all_notifications_read()
are standalone (not defined in app.py itself), so importing them
directly from db.py carries no circular-import risk, unlike the app.py
helpers other api_*.py files have had to copy.

Two endpoints instead of app.py's one route, because the HTML version
gets its unread count for free on every page via a Jinja context
processor — the frontend has no equivalent, so `AppNav` (or wherever a
bell/badge eventually goes) needs its own lightweight way to ask "how
many unread?" without also marking everything read, which the full list
endpoint does (matching the HTML page's own behavior: viewing the list
marks it read).
"""

from datetime import datetime

from flask import Blueprint, jsonify

import db
import scheduler
from api_auth import get_authenticated_user

bp = Blueprint("api_notifications", __name__, url_prefix="/api/notifications")


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


@bp.get("")
def list_notifications():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    rows = db.list_notifications(user_id)
    tz = scheduler.get_tz(user_id)
    items = []
    for r in rows:
        item = dict(r)
        item["created_at_label"] = datetime.fromtimestamp(r["created_at"], tz=tz).strftime("%Y-%m-%d %H:%M")
        items.append(item)
    # Matches the HTML page's own behavior: viewing the list marks it all
    # read, there's no separate "mark as read" action in this app.
    db.mark_all_notifications_read(user_id)
    return jsonify({"items": items}), 200


@bp.get("/unread-count")
def unread_count():
    user, err = _require_user()
    if err:
        return err
    return jsonify({"count": db.unread_notification_count(user["id"])}), 200
