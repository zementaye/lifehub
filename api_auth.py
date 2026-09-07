"""JSON auth API for the separate frontend (React on Vercel) to log in
against. This runs alongside the existing server-rendered routes in
app.py — it's deliberately its own file/blueprint rather than an edit to
app.py's existing login()/register()/login_2fa() routes, so this diff
stays reviewable and the current HTML-based login keeps working
unmodified while the new frontend is built out.

Auth model: NOT the shared session cookie. That was the first version of
this file, and it didn't work reliably: cross-site cookies get silently
dropped by third-party-cookie blocking in Safari, Firefox, and
privacy-conscious Chrome profiles, which made login fail unpredictably
depending on the visitor's browser. Instead, login/register hand back a
signed, timestamped token (itsdangerous — already a Flask dependency, no
new package needed) that the frontend stores itself and sends back as an
`Authorization: Bearer <token>` header on every request. That header is
attached explicitly by our own frontend code rather than auto-sent by the
browser, so it isn't subject to third-party cookie policy at all, and
isn't a CSRF vector either (CSRF relies on the browser attaching ambient
credentials automatically — a header we attach in JS isn't that).
Because of that, this blueprint is exempted from CSRFProtect in app.py.

The token just wraps {user_id, issued_at}; issued_at is compared against
users.sessions_invalidated_at on every request (mirroring the
before_request check the cookie-based side already does), so "log out
everywhere" / a password change still invalidates outstanding API tokens
without needing a server-side revocation list.

Deliberately NOT yet replicated here (left for a follow-up once the
frontend actually needs them): the "/admin" email-suffix login shortcut,
new-device email notifications, and email verification kickoff on
register.
"""

import secrets
import time

from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config
import crypto
import db
import totp

bp = Blueprint("api_auth", __name__, url_prefix="/api/auth")

# Same reasoning as app.py's login_2fa(): a pending 2FA login that's sat
# unfinished this long is more likely abandoned than in progress.
_PENDING_TOTP_TIMEOUT = 10 * 60

# Tokens carry their own expiry (checked by itsdangerous on decode), kept
# in step with the cookie-based side's own session lifetime so neither
# auth path outlives the other by policy.
_TOKEN_MAX_AGE = config.PERMANENT_SESSION_LIFETIME

# In-memory pending-2FA store, keyed by a short-lived handle given to the
# client after step 1 of login. NOT the session cookie (see module
# docstring — cookies don't survive cross-site here), and deliberately
# not a signed itsdangerous token either: unlike the post-login token
# above, this one has nothing worth a client being able to inspect or
# forge offline, so a plain unguessable random ID is enough to hold a
# slot for ten minutes. Lost on process restart, which just means an
# in-flight 2FA login has to restart too — an acceptable trade for not
# adding infrastructure (Redis etc.) for a ten-minute window. Fine on
# Render's single-instance free tier; would need a shared store behind
# more than one worker process.
_pending_totp = {}


def _serializer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="api-auth-token")


def _issue_token(user_id: int) -> str:
    return _serializer().dumps({"user_id": user_id, "issued_at": time.time()})


def _user_from_token(token: str):
    """Returns the user row for a valid, unexpired, not-since-invalidated
    token, or None. Doesn't raise — every caller just wants a yes/no."""
    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.get_user_by_id(payload.get("user_id"))
    if not user or user["disabled_at"]:
        return None
    invalidated_at = user["sessions_invalidated_at"]
    if invalidated_at and payload.get("issued_at", 0) < invalidated_at:
        return None
    return user


def get_authenticated_user():
    """Reads the Authorization: Bearer <token> header and returns the
    corresponding user row, or None. The one place every other route in
    this file (and any future protected /api/* route) should call rather
    than touching the token format directly."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return _user_from_token(auth_header[len("Bearer "):].strip())


def _user_public(user):
    return {
        "id": user["id"],
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
    }


@bp.get("/me")
def me():
    user = get_authenticated_user()
    if not user:
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

    user = db.get_user_by_id(user_id)
    return jsonify({
        "authenticated": True,
        "user": _user_public(user),
        "token": _issue_token(user_id),
    }), 201


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
        pending_token = secrets.token_urlsafe(24)
        _pending_totp[pending_token] = {"user_id": user["id"], "started_at": time.time()}
        return jsonify({"authenticated": False, "totp_required": True, "pending_token": pending_token}), 200

    return jsonify({
        "authenticated": True,
        "user": _user_public(user),
        "token": _issue_token(user["id"]),
    }), 200


@bp.post("/login/2fa")
def login_2fa():
    data = request.get_json(silent=True) or {}
    pending_token = data.get("pending_token") or ""
    pending = _pending_totp.get(pending_token)

    if not pending or time.time() - pending["started_at"] > _PENDING_TOTP_TIMEOUT:
        _pending_totp.pop(pending_token, None)
        return jsonify({"error": "That login attempt expired — log in again."}), 401

    user = db.get_user_by_id(pending["user_id"])
    if not user or not user["totp_enabled_at"]:
        _pending_totp.pop(pending_token, None)
        return jsonify({"error": "Log in again."}), 401

    code = (data.get("code") or "").strip()
    secret = crypto.decrypt(user["totp_secret_enc"])

    used_backup_code = False
    if totp.verify_totp(secret, code):
        pass
    elif db.consume_backup_code(pending["user_id"], code):
        used_backup_code = True
    else:
        db.admin_log(
            None, "(anonymous)", "login_2fa_failed",
            target_id=user["id"], target_email=user["email"],
            details=f"ip={request.remote_addr} via=api",
        )
        return jsonify({"error": "That code wasn't right."}), 401

    _pending_totp.pop(pending_token, None)

    result = {
        "authenticated": True,
        "user": _user_public(user),
        "token": _issue_token(user["id"]),
    }
    if used_backup_code:
        result["backup_code_used"] = True
        result["backup_codes_remaining"] = db.count_unused_backup_codes(user["id"])
    return jsonify(result), 200


@bp.post("/logout")
def logout():
    # Tokens are stateless (no server-side store), so there's nothing to
    # revoke here — logging out is just the frontend discarding the token
    # it's holding. This endpoint exists mainly for symmetry/future use
    # (e.g. if a server-side revocation list gets added later) and so the
    # frontend has a single place to call regardless of how logout ends
    # up working.
    return jsonify({"authenticated": False}), 200
