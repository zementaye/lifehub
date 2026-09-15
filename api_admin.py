"""JSON API for the admin panel — the thirteenth slice of the Vercel
frontend migration. Mirrors app.py's admin_dashboard()/admin_users()/
admin_update_user()/admin_toggle_admin()/admin_toggle_suspend()/
admin_delete_user()/admin_audit_log()/admin_audit_log_export() routes.

Step-up re-authentication: the HTML app's admin_required decorator gates
every /admin/* route behind BOTH being logged in AND a *recent* password
re-check (session["admin_verified_at"], a sliding window — see that
decorator's own docstring in app.py for the full reasoning). Bearer
tokens have no server-side session to stash that in, so this blueprint
uses a second, short-lived token instead: POST /verify re-checks the
account's password and, if it's an admin account, returns a signed
"elevation" token (same itsdangerous pattern as api_auth.py's login
token, different salt) valid for config.ADMIN_ELEVATION_LIFETIME. Every
other route here requires BOTH the normal Authorization: Bearer token
AND a valid X-Admin-Token header.

One deliberate simplification from the HTML version: this elevation
token's validity window is fixed from issuance (itsdangerous checks
max_age against a fixed timestamp), not a sliding one that renews on
each admin action. A genuinely idle-but-still-open admin session here
expires on a strict timer rather than staying alive through activity.
Reissuing a sliding-window token on every request would mean the
frontend has to notice and swap tokens mid-session, which felt like
more complexity than this slice needed — revisit if 15 minutes turns
out to be too short in practice.

Same duplication note as the other api_*.py files: `_fmt_datetime` is
copied from app.py rather than imported (circular — app.py imports this
file before its own helpers are defined further down). Everything else
(the db.admin_* functions) is imported directly from db.py, which
carries no circularity risk.
"""

import csv
import io
import time
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, make_response, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config
import db
import storage
from api_auth import get_authenticated_user

bp = Blueprint("api_admin", __name__, url_prefix="/api/admin")


def _fmt_datetime(epoch):
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _elevation_serializer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="admin-elevation-token")


def _require_admin():
    """Returns (user, None) once both checks pass, or (None, error_response).
    Every route below starts with this instead of repeating the two
    checks (regular auth + elevation) five times."""
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    if not user["is_admin"]:
        return None, (jsonify({"error": "Admin access required."}), 403)

    admin_token = request.headers.get("X-Admin-Token", "")
    try:
        payload = _elevation_serializer().loads(admin_token, max_age=config.ADMIN_ELEVATION_LIFETIME)
    except (BadSignature, SignatureExpired):
        return None, (jsonify({"error": "Admin session expired — re-verify your password.", "needs_reverify": True}), 401)
    if payload.get("user_id") != user["id"]:
        return None, (jsonify({"error": "Admin session expired — re-verify your password.", "needs_reverify": True}), 401)

    return user, None


def _admin_actor(user):
    return user["id"], user["email"]


@bp.post("/verify")
def verify():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Not authenticated."}), 401
    if not user["is_admin"]:
        return jsonify({"error": "Admin access required."}), 403

    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not db.verify_password(user, password):
        return jsonify({"error": "Incorrect password."}), 401

    token = _elevation_serializer().dumps({"user_id": user["id"], "elevated_at": time.time()})
    return jsonify({"admin_token": token, "expires_in": config.ADMIN_ELEVATION_LIFETIME}), 200


@bp.get("/overview")
def overview():
    user, err = _require_admin()
    if err:
        return err
    return jsonify({
        "stats": db.admin_overview_stats(),
        "signups": db.admin_signup_counts(30),
        "content_totals": db.admin_content_totals(),
        "top_users": db.admin_top_users(10),
    }), 200


@bp.get("/users")
def list_users():
    user, err = _require_admin()
    if err:
        return err
    q = request.args.get("q", "").strip()
    users = db.admin_list_users(q or None)
    return jsonify({
        "users": [dict(u) for u in users],
        "admin_count": db.admin_count_admins(),
    }), 200


@bp.post("/users/<int:user_id>/update")
def update_user(user_id):
    user, err = _require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    if not email:
        return jsonify({"error": "Email can't be blank."}), 400

    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"error": "That user no longer exists."}), 404

    error = db.admin_update_user(user_id, email=email, new_password=password or None)
    if error:
        return jsonify({"error": error}), 400

    actor_id, actor_email = _admin_actor(user)
    changes = []
    if email.lower() != target["email"].lower():
        changes.append(f"email changed to {email}")
    if password:
        changes.append("password reset")
    db.admin_log(
        actor_id, actor_email, "update_user",
        target_id=user_id, target_email=target["email"],
        details="; ".join(changes) or "no-op save",
    )
    return jsonify({"ok": True}), 200


@bp.post("/users/<int:user_id>/toggle-admin")
def toggle_admin(user_id):
    user, err = _require_admin()
    if err:
        return err

    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"error": "That user no longer exists."}), 404

    actor_id, actor_email = _admin_actor(user)
    if target["is_admin"]:
        if db.admin_count_admins() <= 1:
            return jsonify({"error": "Can't remove admin from the last remaining admin."}), 400
        db.admin_set_admin(user_id, False)
        db.admin_log(actor_id, actor_email, "demote_admin", target_id=user_id, target_email=target["email"])
    else:
        db.admin_set_admin(user_id, True)
        db.admin_log(actor_id, actor_email, "promote_admin", target_id=user_id, target_email=target["email"])
    return jsonify({"ok": True, "is_admin": not target["is_admin"]}), 200


@bp.post("/users/<int:user_id>/toggle-suspend")
def toggle_suspend(user_id):
    user, err = _require_admin()
    if err:
        return err

    if user_id == user["id"]:
        return jsonify({"error": "You can't suspend your own account from here."}), 400

    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"error": "That user no longer exists."}), 404
    if target["is_admin"] and not target["disabled_at"] and db.admin_count_admins() <= 1:
        return jsonify({"error": "Can't suspend the last remaining admin."}), 400

    actor_id, actor_email = _admin_actor(user)
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if target["disabled_at"]:
        db.admin_set_disabled(user_id, False)
        db.admin_log(actor_id, actor_email, "unsuspend_user", target_id=user_id, target_email=target["email"])
    else:
        db.admin_set_disabled(user_id, True)
        db.admin_log(
            actor_id, actor_email, "suspend_user",
            target_id=user_id, target_email=target["email"],
            details=reason or None,
        )
    return jsonify({"ok": True, "disabled": not target["disabled_at"]}), 200


@bp.post("/users/<int:user_id>/delete")
def delete_user(user_id):
    user, err = _require_admin()
    if err:
        return err

    if user_id == user["id"]:
        return jsonify({"error": "You can't delete your own account from here — use Settings instead."}), 400

    target = db.get_user_by_id(user_id)
    if not target:
        return jsonify({"error": "That user no longer exists."}), 404
    if target["is_admin"] and db.admin_count_admins() <= 1:
        return jsonify({"error": "Can't delete the last remaining admin."}), 400

    actor_id, actor_email = _admin_actor(user)
    target_email = target["email"]

    filenames = db.admin_delete_user(user_id)
    for filename in filenames:
        try:
            if config.USE_B2:
                storage.delete_file(filename)
            else:
                path = config.UPLOAD_DIR / filename
                if path.exists():
                    path.unlink()
        except Exception:
            pass  # best-effort, same as app.py's own admin_delete_user()

    db.admin_log(
        actor_id, actor_email, "delete_user",
        target_id=user_id, target_email=target_email,
        details=f"{len(filenames)} vault file(s) removed" if filenames else None,
    )
    return jsonify({"ok": True}), 200


@bp.get("/audit-log")
def audit_log():
    user, err = _require_admin()
    if err:
        return err
    q = request.args.get("q", "").strip()
    entries = db.admin_list_audit_log(limit=300, query=q or None)
    items = []
    for e in entries:
        entry = dict(e)
        entry["created_at_label"] = _fmt_datetime(e["created_at"])
        items.append(entry)
    return jsonify({"entries": items}), 200


@bp.get("/audit-log/export.csv")
def audit_log_export():
    user, err = _require_admin()
    if err:
        return err
    q = request.args.get("q", "").strip()
    entries = db.admin_list_audit_log(limit=10000, query=q or None)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["when_utc", "actor_email", "action", "target_email", "details"])
    for e in entries:
        writer.writerow([
            _fmt_datetime(e["created_at"]),
            e["actor_email"],
            e["action"],
            e["target_email"] or "",
            e["details"] or "",
        ])

    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = f"attachment; filename=lifehub_audit_log_{date.today().isoformat()}.csv"
    return resp
