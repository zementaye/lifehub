"""JSON auth API for the separate frontend (React on Vercel) to log in
against. This runs alongside the existing server-rendered routes in
app.py — it's deliberately its own file/blueprint rather than an edit to
app.py's existing login()/register()/login_2fa() routes, so this diff
stays reviewable and the current HTML-based login keeps working
unmodified while the new frontend is built out.

Auth model is unchanged: the same signed Flask session cookie either side
sets/reads. What differs is the transport (JSON in, JSON out, no
redirects/flashes) and that CORS + SameSite=None (see config.py) let the
cookie flow to/from a different origin.

Deliberately NOT yet replicated here (left for a follow-up once the
frontend actually needs them): the "/admin" email-suffix login shortcut,
new-device email notifications, and email verification kickoff on
register. None of those are required for a basic working login/register
flow, and skipping them keeps this first slice small and reviewable.
"""

import time

from flask import Blueprint, jsonify, request, session

import crypto
import db
import totp

bp = Blueprint("api_auth", __name__, url_prefix="/api/auth")

# Same reasoning as app.py's login_2fa(): a pending 2FA login that's sat
# unfinished this long is more likely abandoned than in progress.
_PENDING_TOTP_TIMEOUT = 10 * 60


def _user_public(user):
    return {
        "id": user["id"],
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
    }


def _establish_session(user_id: int) -> None:
    session["user_id"] = user_id
    session.permanent = True
    session["admin_verified_at"] = time.time()
    session["session_issued_at"] = time.time()


@bp.get("/me")
def me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"authenticated": False}), 200
    user = db.get_user_by_id(user_id)
    if not user:
        session.clear()
        return jsonify({"authenticated": False}), 200
    return jsonify({"authenticated": True, "user": _user_public(user)}), 200


@bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    confirm = data.get("confirm", password)

    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if password != confirm:
        return jsonify({"error": "Passwords don't match."}), 400

    user_id = db.create_user(email, password)
    if user_id is None:
        return jsonify({"error": "An account with that email already exists."}), 409

    _establish_session(user_id)
    user = db.get_user_by_id(user_id)
    return jsonify({"authenticated": True, "user": _user_public(user)}), 201


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, password):
        db.admin_log(
            None, "(anonymous)", "login_failed",
            target_id=user["id"] if user else None, target_email=email,
            details=f"ip={request.remote_addr} via=api",
        )
        return jsonify({"error": "Incorrect email or password."}), 401

    if user["disabled_at"]:
        db.admin_log(
            None, "(anonymous)", "login_blocked_suspended",
            target_id=user["id"], target_email=user["email"],
            details=f"ip={request.remote_addr} via=api",
        )
        return jsonify({"error": "This account has been suspended."}), 403

    if user["totp_enabled_at"]:
        session.clear()
        session["pending_totp_user_id"] = user["id"]
        session["pending_totp_started_at"] = time.time()
        return jsonify({"authenticated": False, "totp_required": True}), 200

    _establish_session(user["id"])
    return jsonify({"authenticated": True, "user": _user_public(user)}), 200


@bp.post("/login/2fa")
def login_2fa():
    pending_user_id = session.get("pending_totp_user_id")
    started_at = session.get("pending_totp_started_at") or 0
    if not pending_user_id or time.time() - started_at > _PENDING_TOTP_TIMEOUT:
        session.pop("pending_totp_user_id", None)
        session.pop("pending_totp_started_at", None)
        return jsonify({"error": "That login attempt expired — log in again."}), 401

    user = db.get_user_by_id(pending_user_id)
    if not user or not user["totp_enabled_at"]:
        session.clear()
        return jsonify({"error": "Log in again."}), 401

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    secret = crypto.decrypt(user["totp_secret_enc"])

    used_backup_code = False
    if totp.verify_totp(secret, code):
        pass
    elif db.consume_backup_code(pending_user_id, code):
        used_backup_code = True
    else:
        db.admin_log(
            None, "(anonymous)", "login_2fa_failed",
            target_id=user["id"], target_email=user["email"],
            details=f"ip={request.remote_addr} via=api",
        )
        return jsonify({"error": "That code wasn't right."}), 401

    session.pop("pending_totp_user_id", None)
    session.pop("pending_totp_started_at", None)
    _establish_session(user["id"])

    result = {"authenticated": True, "user": _user_public(user)}
    if used_backup_code:
        result["backup_code_used"] = True
        result["backup_codes_remaining"] = db.count_unused_backup_codes(user["id"])
    return jsonify(result), 200


@bp.post("/logout")
def logout():
    session.clear()
    return jsonify({"authenticated": False}), 200
