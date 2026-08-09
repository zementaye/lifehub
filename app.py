import logging
import os
import secrets
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import itsdangerous
import requests
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_from_directory, make_response, session, g, jsonify,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import generate_password_hash

import ai
import config
import crypto
import db
import nutrition_api
import nutrition_calc
import scheduler
import storage
import telegram_notify
import totp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# Cookie hardening + upload size cap — see config.py for rationale on each.
app.config.update(
    SESSION_COOKIE_HTTPONLY=config.SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=config.PERMANENT_SESSION_LIFETIME,
    MAX_CONTENT_LENGTH=config.MAX_CONTENT_LENGTH,
)

# CSRF protection on every POST/PUT/PATCH/DELETE. Forms get their token via
# the {{ csrf_token() }} auto-injection in static/app.js (reads the meta tag
# base.html renders); the two raw fetch()/sendBeacon() calls in app.js send
# it explicitly. See CSRFError handler below for the user-facing failure page.
csrf = CSRFProtect(app)

# Brute-force protection. In-memory storage is fine for this app's single
# small deployment (1 gunicorn worker per scheduler.py's own lock — see
# that file); swap storage_uri for a shared backend (e.g. redis://) first
# if this is ever run with multiple workers/instances.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://", default_limits=[])

# Changes every time the app process starts (i.e. every deploy/restart).
# Appended as a ?v= query string on static assets in base.html so browsers
# fetch a fresh copy after each deploy instead of serving a stale cached
# style.css/app.js for up to 12 hours (Flask's default static cache time).
app.jinja_env.globals["asset_version"] = str(int(time.time()))


def _hour12(hour):
    """Format an integer hour (0-23) as a friendly 12-hour clock label, e.g. 15 -> '3 PM'."""
    if hour is None:
        return None
    hour = int(hour) % 24
    period = "AM" if hour < 12 else "PM"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour} {period}"


app.jinja_env.filters["hour12"] = _hour12


def _fmt_date(epoch):
    """Format a stored epoch-seconds timestamp (created_at, etc.) as a
    plain YYYY-MM-DD for display — used on the admin user list."""
    if epoch is None:
        return "—"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


app.jinja_env.filters["fmtdate"] = _fmt_date


def _fmt_datetime(epoch):
    """Like fmtdate but with a time component — used on the audit log,
    where knowing *when* an action happened down to the minute matters."""
    if epoch is None:
        return "—"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


app.jinja_env.filters["fmtdatetime"] = _fmt_datetime

db.init_db()


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    # Most commonly hit when a form was left open across a session timeout
    # (token no longer valid) rather than an actual attack — send them back
    # to log in rather than showing a raw 400.
    flash("Your session expired — please log in again and retry.")
    return redirect(url_for("login"))


@app.errorhandler(RequestEntityTooLarge)
def handle_upload_too_large(e):
    flash(f"That file is too large — the limit is {config.MAX_UPLOAD_MB} MB.")
    return redirect(request.referrer or url_for("dashboard"))


# ── Auth ─────────────────────────────────────────────────────────────────
# Public routes: no session required. Everything else demands a logged-in
# user, since the app is now open for anyone to sign up and use — there's
# no longer a shared APP_ACCESS_TOKEN gate in front of it.
_PUBLIC_ENDPOINTS = {"home", "login", "login_2fa", "register", "forgot_password", "reset_password", "verify_email", "static"}


@app.before_request
def load_logged_in_user():
    # A fresh nonce per request, used by the CSP's script-src (see
    # security_headers below) and echoed into every inline <script> tag
    # via {{ csp_nonce() }}. Generated unconditionally, before the
    # public-endpoint early-return, since login/register/etc. also have
    # inline scripts that need it.
    g.csp_nonce = secrets.token_urlsafe(16)
    g.user_id = session.get("user_id")
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if not g.user_id:
        return redirect(url_for("login", next=request.path))
    # A suspension applied mid-session shouldn't wait for the cookie to
    # expire — check on every request and boot them out immediately.
    user = db.get_user_by_id(g.user_id)
    if not user or user["disabled_at"]:
        session.clear()
        flash("This account has been suspended. Contact an admin if you think that's wrong.")
        return redirect(url_for("login"))
    # Sessions are plain signed cookies with no server-side store, so
    # "Log out everywhere" (and a password change, which does the same
    # thing) can't delete anyone else's session directly. Instead they
    # bump users.sessions_invalidated_at, and every request compares that
    # against when *this* cookie was issued — a cookie older than the
    # bump is stale and gets logged out here, on its very next request.
    invalidated_at = user["sessions_invalidated_at"]
    issued_at = session.get("session_issued_at")
    if invalidated_at and (not issued_at or issued_at < invalidated_at):
        session.clear()
        flash("You were logged out because of a password change or a 'log out everywhere' request.")
        return redirect(url_for("login"))


def _safe_next(path, allowed_prefix="/"):
    """Validate a ?next=... redirect target before it's ever passed to
    redirect(). Without this, redirect(request.args.get("next")) is an
    open redirect: a link like /login?next=https://evil-lookalike.com
    logs the person in for real and then sends their already-authenticated
    browser straight to an attacker's site. Only a same-site relative path
    is allowed — never an absolute URL, a scheme, or a protocol-relative
    "//host" path (browsers treat a leading "//" as "go to this host").
    `allowed_prefix` further restricts where within the app it may point
    (e.g. "/admin" for the admin step-up flow)."""
    if not path:
        return None
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return None
    if "://" in path:
        return None
    if not path.startswith(allowed_prefix):
        return None
    return path


def login_required(view):
    """Defensive belt-and-suspenders on top of the before_request gate above
    — makes the requirement obvious on any route that touches user data."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Gate for every /admin/* route. Requires login (same as
    login_required) AND that the account has is_admin set — anyone else
    gets bounced to the dashboard with a flash rather than a raw 403, same
    tone as the rest of this app's error handling.

    On top of that, admin access needs a *recent* password check
    (session["admin_verified_at"]), separate from the regular week-long
    login session. Without this, anyone who can get to an already-unlocked
    browser — hours or days into the same login session, e.g. via browser
    history — could walk straight into the admin console with no prompt at
    all. The elevation window is sliding: it refreshes on every admin
    request, so an admin actively working stays in, but idle time beyond
    config.ADMIN_ELEVATION_LIFETIME requires re-entering the password."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user_id"):
            return redirect(url_for("login"))
        user = db.get_user_by_id(g.user_id)
        if not user or not user["is_admin"]:
            flash("That page is admin-only.")
            return redirect(url_for("dashboard"))
        verified_at = session.get("admin_verified_at")
        if not verified_at or time.time() - verified_at > config.ADMIN_ELEVATION_LIFETIME:
            return redirect(url_for("admin_verify", next=request.path))
        session["admin_verified_at"] = time.time()  # sliding window
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/verify", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"], key_func=get_remote_address)
def admin_verify():
    """Step-up re-authentication in front of the admin console. Reached
    whenever admin_required finds no recent elevation — see there for why
    this exists. Only confirms the password of the account already logged
    in; it doesn't grant admin access to anyone who doesn't have it."""
    if not g.get("user_id"):
        return redirect(url_for("login", next=request.full_path))
    user = db.get_user_by_id(g.user_id)
    if not user or not user["is_admin"]:
        flash("That page is admin-only.")
        return redirect(url_for("dashboard"))

    # Only allow redirecting back into /admin/... — never off-site and
    # never somewhere outside the admin console.
    next_path = _safe_next(request.values.get("next", ""), allowed_prefix="/admin") \
        or url_for("admin_dashboard")

    if request.method == "GET":
        return render_template("admin_verify.html", next=next_path, email=user["email"])

    password = request.form.get("password", "")
    if not db.verify_password(user, password):
        flash("Incorrect password.")
        return render_template("admin_verify.html", next=next_path, email=user["email"])

    session["admin_verified_at"] = time.time()
    return redirect(next_path)


@app.context_processor
def inject_user():
    """Makes {{ user }} available in every template (e.g. settings.html's
    'Logged in as ...') without every render_template() call needing to
    remember to pass it explicitly."""
    user_id = g.get("user_id")
    if not user_id:
        return {}
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"user": row}


@app.context_processor
def inject_csp_nonce():
    """Makes {{ csp_nonce() }} available in every template. Called as a
    function (not a bare variable) so Jinja re-evaluates it fresh if a
    template somehow renders more than once per request, rather than
    caching a stale value."""
    return {"csp_nonce": lambda: g.get("csp_nonce", "")}


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"], key_func=get_remote_address)
def register():
    if request.method == "GET":
        return render_template("register.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    if not email or "@" not in email:
        flash("Enter a valid email address.")
        return render_template("register.html")
    if len(password) < 8:
        flash("Password must be at least 8 characters.")
        return render_template("register.html")
    if password != confirm:
        flash("Passwords don't match.")
        return render_template("register.html")

    user_id = db.create_user(email, password)
    if user_id is None:
        flash("An account with that email already exists.")
        return render_template("register.html")

    session["user_id"] = user_id
    session.permanent = True
    session["session_issued_at"] = time.time()
    _start_email_verification(user_id, email)
    # Mark this browser as recognized without sending a "new sign-in"
    # email — they're already getting the verification email below, and
    # a second "new device" email for the browser they just registered
    # from would just be redundant noise.
    resp = make_response(redirect(url_for("dashboard")))
    return _remember_device(resp, user_id)


def _login_rate_key():
    # Keyed on IP + attempted email, so one bad actor guessing many
    # passwords against one account is throttled without also locking out
    # everyone else sharing that IP (e.g. an office/NAT). Strips a
    # trailing "/admin" (see login() below) so "user@x.com" and
    # "user@x.com/admin" share the same rate-limit bucket instead of each
    # getting their own 5-per-minute allowance.
    raw = request.form.get("email", "").strip().lower()
    if raw.endswith("/admin"):
        raw = raw[: -len("/admin")]
    return f"{get_remote_address()}:{raw}"


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"], key_func=_login_rate_key)
@limiter.limit("20 per minute", methods=["POST"], key_func=get_remote_address)
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    # A trailing "/admin" on the email field (same password, no separate
    # credential) is just a routing shortcut straight to the admin panel
    # on successful login — it grants nothing by itself. Anyone without
    # is_admin set who tries it just lands on the normal dashboard with a
    # note, same as if they'd typed their email plain.
    want_admin = email.lower().endswith("/admin")
    if want_admin:
        email = email[: -len("/admin")]

    user = db.get_user_by_email(email)
    if not user or not db.verify_password(user, password):
        db.admin_log(
            None, "(anonymous)", "login_failed",
            target_id=user["id"] if user else None, target_email=email,
            details=f"ip={get_remote_address()}",
        )
        flash("Incorrect email or password.")
        return render_template("login.html")

    if user["disabled_at"]:
        db.admin_log(
            None, "(anonymous)", "login_blocked_suspended",
            target_id=user["id"], target_email=user["email"],
            details=f"ip={get_remote_address()}",
        )
        flash("This account has been suspended. Contact an admin if you think that's wrong.")
        return render_template("login.html")

    if user["totp_enabled_at"]:
        # Password was correct, but 2FA is on — don't establish a real
        # session yet. Everything needed to finish logging in afterwards
        # (which account, where to send them, the /admin shortcut) rides
        # along in a "pending" session key instead, checked and cleared by
        # login_2fa() below.
        session.clear()
        session["pending_totp_user_id"] = user["id"]
        session["pending_totp_started_at"] = time.time()
        session["pending_totp_want_admin"] = want_admin
        session["pending_totp_next"] = _safe_next(request.args.get("next")) or ""
        return redirect(url_for("login_2fa"))

    _finish_login(user)
    next_path = _safe_next(request.args.get("next"))
    return _post_login_response(user, next_path, want_admin)


# 2FA codes are only valid for a short window around "now" (see
# totp.verify_totp), so a pending login that's been sitting unfinished for
# a while is more likely abandoned than genuinely still in progress —
# require starting over past this point rather than trusting a stale
# password check indefinitely.
_PENDING_TOTP_TIMEOUT = 10 * 60


@app.route("/login/2fa", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"], key_func=get_remote_address)
def login_2fa():
    pending_user_id = session.get("pending_totp_user_id")
    started_at = session.get("pending_totp_started_at") or 0
    if not pending_user_id or time.time() - started_at > _PENDING_TOTP_TIMEOUT:
        session.pop("pending_totp_user_id", None)
        session.pop("pending_totp_started_at", None)
        session.pop("pending_totp_want_admin", None)
        session.pop("pending_totp_next", None)
        flash("That login attempt expired — log in again.")
        return redirect(url_for("login"))

    user = db.get_user_by_id(pending_user_id)
    if not user or not user["totp_enabled_at"]:
        # Account state changed underneath the pending login (e.g. 2FA
        # got disabled from another session) — safest is to restart.
        session.clear()
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("login_2fa.html")

    code = request.form.get("code", "").strip()
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
            details=f"ip={get_remote_address()}",
        )
        flash("That code wasn't right. Check your authenticator app and try again.")
        return render_template("login_2fa.html")

    want_admin = session.get("pending_totp_want_admin", False)
    next_path = session.get("pending_totp_next") or None
    session.pop("pending_totp_user_id", None)
    session.pop("pending_totp_started_at", None)
    session.pop("pending_totp_want_admin", None)
    session.pop("pending_totp_next", None)

    _finish_login(user)
    if used_backup_code:
        remaining = db.count_unused_backup_codes(user["id"])
        flash(f"Logged in with a backup code — {remaining} left. Generate new ones from Settings when you can.")
    return _post_login_response(user, next_path, want_admin)


def _finish_login(user) -> None:
    """Establishes the real, logged-in session. Shared by the no-2FA path
    in login() and the post-code path in login_2fa() so both end up with
    exactly the same session state."""
    session["user_id"] = user["id"]
    session.permanent = True
    # A fresh login is itself a password check, so it also counts as
    # admin elevation (see admin_required) — no need to immediately
    # re-prompt someone who just typed their password 2 seconds ago.
    session["admin_verified_at"] = time.time()
    # Stamp when this session cookie was issued, checked against
    # users.sessions_invalidated_at on every request (see
    # load_logged_in_user) — this is what lets "Log out everywhere" and a
    # password change actually invalidate *other* logged-in sessions,
    # since sessions here are plain signed cookies with no server-side
    # store to delete from.
    session["session_issued_at"] = time.time()


def _post_login_response(user, next_path, want_admin):
    """Builds the redirect that follows a successful login (shared by the
    no-2FA path in login() and the post-code path in login_2fa()) and
    attaches the new-device notification cookie/email to it — that needs
    an actual response object to set a cookie on, which a bare
    redirect(...) return doesn't give a hook for."""
    if next_path:
        resp = make_response(redirect(next_path))
    elif want_admin and user["is_admin"]:
        resp = make_response(redirect(url_for("admin_dashboard")))
    else:
        if want_admin:
            flash("That account isn't an admin — logged in normally instead.")
        resp = make_response(redirect(url_for("dashboard")))
    return _notify_if_new_device(user, resp)



@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"], key_func=get_remote_address)
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = request.form.get("email", "").strip()
    user = db.get_user_by_email(email)
    if user:
        token = db.create_password_reset(user["id"])
        reset_link = f"{config.APP_BASE_URL.rstrip('/')}{url_for('reset_password', token=token)}"
        ok, err = _send_reset_email(user["email"], reset_link)
        if not ok:
            logger.error("Failed to send password reset email to %s: %s", user["email"], err)
    # Same message whether or not the account exists, so this can't be used
    # to probe which emails are registered.
    flash("If that email has an account, a reset link is on its way.")
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = db.get_password_reset(token)
    if not reset or reset["used_at"] or reset["expires_at"] < db.now():
        flash("That reset link is invalid or has expired.")
        return redirect(url_for("forgot_password"))

    if request.method == "GET":
        return render_template("reset_password.html")

    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.")
        return render_template("reset_password.html")
    if password != confirm:
        flash("Passwords don't match.")
        return render_template("reset_password.html")

    db.set_password(reset["user_id"], password)
    db.use_password_reset(token)
    target = db.get_user_by_id(reset["user_id"])
    if target:
        db.admin_log(
            reset["user_id"], target["email"], "self_password_reset",
            target_id=reset["user_id"], target_email=target["email"],
            details=f"ip={get_remote_address()}",
        )
    flash("Password updated — log in with your new password.")
    return redirect(url_for("login"))


def _start_email_verification(user_id: int, email: str) -> None:
    """Called right after an account is created. Sends a verification email
    if Resend is configured; otherwise there's no way for the user to receive
    a link at all, so — consistent with how this app treats every other
    optional integration (Telegram, B2, Turso) — the requirement quietly
    doesn't apply rather than leaving the account permanently unverifiable.
    """
    if not config.RESEND_API_KEY:
        db.mark_email_verified(user_id)
        flash("Welcome to Life Hub!")
        return

    token = db.create_email_verification(user_id)
    verify_link = f"{config.APP_BASE_URL.rstrip('/')}{url_for('verify_email', token=token)}"
    ok, err = _send_verification_email(email, verify_link)
    if ok:
        flash("Welcome to Life Hub! Check your email to verify your account.")
    else:
        logger.error("Failed to send verification email to %s: %s", email, err)
        flash("Welcome to Life Hub! We couldn't send a verification email right now — you can resend it from Settings.")


@app.route("/verify-email/<token>")
def verify_email(token):
    v = db.get_email_verification(token)
    if not v or v["used_at"] or v["expires_at"] < db.now():
        flash("That verification link is invalid or has expired — you can request a new one from Settings.")
        return redirect(url_for("dashboard") if g.user_id else url_for("login"))

    db.mark_email_verified(v["user_id"])
    db.use_email_verification(token)
    flash("Email verified — thanks!")
    return redirect(url_for("dashboard") if g.user_id else url_for("login"))


@app.route("/resend-verification", methods=["POST"])
@login_required
@limiter.limit("3 per hour", key_func=lambda: str(g.user_id))
def resend_verification():
    user = db.get_user_by_id(g.user_id)
    if db.is_email_verified(user):
        flash("Your email is already verified.")
        return redirect(request.referrer or url_for("settings"))

    if not config.RESEND_API_KEY:
        # Shouldn't normally be reachable (accounts are auto-verified at
        # signup when Resend isn't configured), but covers the case where it
        # was removed from the environment after this account registered.
        db.mark_email_verified(g.user_id)
        flash("Email verification isn't configured on this server, so your account has been marked verified.")
        return redirect(request.referrer or url_for("settings"))

    token = db.create_email_verification(g.user_id)
    verify_link = f"{config.APP_BASE_URL.rstrip('/')}{url_for('verify_email', token=token)}"
    ok, err = _send_verification_email(user["email"], verify_link)
    if ok:
        flash("Verification email sent — check your inbox.")
    else:
        logger.error("Failed to resend verification email to %s: %s", user["email"], err)
        flash("Couldn't send the verification email right now. Please try again shortly.")
    return redirect(request.referrer or url_for("settings"))


def _send_via_resend(to_email: str, subject: str, body: str):
    """Sends a plain-text email through Resend's HTTPS API. Used instead of
    smtplib because Render's free tier blocks outbound SMTP ports (25/465/
    587) entirely — HTTPS to api.resend.com isn't affected."""
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": config.RESEND_FROM,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            return False, f"Resend API error {resp.status_code}: {resp.text[:200]}"
        return True, None
    except Exception as e:
        return False, str(e)


def _send_reset_email(to_email: str, reset_link: str):
    if not config.RESEND_API_KEY:
        return False, "RESEND_API_KEY not configured"

    return _send_via_resend(
        to_email=to_email,
        subject="Reset your Life Hub password",
        body=(
            f"Someone requested a password reset for your Life Hub account.\n\n"
            f"Reset your password: {reset_link}\n\n"
            f"If you didn't request this, you can ignore this email."
        ),
    )


def _send_verification_email(to_email: str, verify_link: str):
    if not config.RESEND_API_KEY:
        return False, "RESEND_API_KEY not configured"

    return _send_via_resend(
        to_email=to_email,
        subject="Verify your Life Hub email",
        body=(
            f"Welcome to Life Hub! Please verify your email address to finish setting up your account.\n\n"
            f"Verify your email: {verify_link}\n\n"
            f"This link expires in 48 hours. If you didn't create this account, you can ignore this email."
        ),
    )


def _send_new_device_login_email(to_email: str, ip: str, when_text: str):
    if not config.RESEND_API_KEY:
        return False, "RESEND_API_KEY not configured"

    return _send_via_resend(
        to_email=to_email,
        subject="New sign-in to your Life Hub account",
        body=(
            f"Your Life Hub account was just signed into from a browser we haven't seen before.\n\n"
            f"When: {when_text}\n"
            f"IP address: {ip}\n\n"
            f"If this was you, there's nothing else to do — you won't be asked again from this "
            f"browser. If it wasn't you, change your password right away from Settings, or use "
            f"'Log out everywhere' if you're still signed in somewhere.\n"
        ),
    )


# Separate from the login session cookie (see load_logged_in_user):
# long-lived, signed, and holds only "this browser has completed a full
# login as user N before" — used purely to decide whether a login is from
# a *recognized* browser, so a new-device notification email only fires
# the first time, not on every single login. Losing/clearing this cookie
# just means one extra notification email next time, not a security
# issue — it grants no access by itself.
_device_signer = itsdangerous.URLSafeSerializer(config.SECRET_KEY, salt="device-recognition")
_DEVICE_COOKIE = "lh_device"
_DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60


def _is_recognized_device(user_id: int) -> bool:
    token = request.cookies.get(_DEVICE_COOKIE)
    if not token:
        return False
    try:
        data = _device_signer.loads(token)
    except itsdangerous.BadSignature:
        return False
    return data.get("uid") == user_id


def _remember_device(resp, user_id: int):
    token = _device_signer.dumps({"uid": user_id})
    resp.set_cookie(
        _DEVICE_COOKIE, token,
        max_age=_DEVICE_COOKIE_MAX_AGE,
        httponly=True, samesite="Lax", secure=config.IS_PRODUCTION,
    )
    return resp


def _notify_if_new_device(user, resp):
    """Called right after a successful login. Sends a heads-up email the
    first time a given browser logs in as this account, then marks the
    browser recognized so it doesn't fire again. Best-effort — a failed
    or unconfigured email send never blocks the login itself."""
    if _is_recognized_device(user["id"]):
        return resp
    when_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        _send_new_device_login_email(user["email"], get_remote_address(), when_text)
    except Exception:
        logger.exception("Failed to send new-device login email to user %s", user["id"])
    return _remember_device(resp, user["id"])


@app.after_request
def no_cache_static(resp):
    # Belt-and-suspenders on top of the ?v= cache-busting: force browsers to
    # always revalidate style.css/app.js with the server instead of trusting
    # a locally cached copy, no matter how that copy got cached in the past.
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# Vault images/downloads are served via a redirect to a presigned URL on
# whatever B2-compatible endpoint is configured (see vault_file()), so the
# CSP's img-src needs to allow that specific host — computed once here from
# config rather than a wildcard, so an image tag pointing anywhere else is
# still blocked.
_B2_HOST = urllib.parse.urlparse(config.R2_ENDPOINT_URL).netloc if config.R2_ENDPOINT_URL else ""

_IMG_SRC = "img-src 'self' data:" + (f" https://{_B2_HOST}" if _B2_HOST else "")


def _build_csp(nonce: str) -> str:
    # script-src uses a per-request nonce instead of 'unsafe-inline' — the
    # nonce is generated fresh in load_logged_in_user() and only appears
    # in the response actually sent for this request, so a script an
    # attacker manages to inject (e.g. via a stored-XSS field) has no way
    # to know it and won't execute. Every legitimate inline <script> tag
    # in the templates carries {{ csp_nonce() }} so they still run.
    #
    # style-src still allows 'unsafe-inline': a lot of templates use
    # style="..." attributes, and CSP nonces don't cover those (only
    # <style> blocks) — tightening that means replacing every inline
    # style attribute with a class, which is separate follow-up work.
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' https://cdnjs.cloudflare.com 'nonce-{nonce}'",
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'",
        "font-src 'self' https://fonts.gstatic.com",
        _IMG_SRC,
        "connect-src 'self'",   # all fetch()/sendBeacon calls in app.js are same-origin
        "frame-ancestors 'none'",  # modern equivalent of X-Frame-Options: DENY
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ])


@app.after_request
def security_headers(resp):
    # Baseline hardening headers on every response. None of these are
    # session- or route-specific, so one blanket after_request covers the
    # whole app rather than repeating this per-view.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"  # legacy fallback for browsers that ignore frame-ancestors
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = _build_csp(g.get("csp_nonce", ""))
    if config.IS_PRODUCTION:
        # Only sent over HTTPS deployments (matches SESSION_COOKIE_SECURE
        # below) — meaningless, and potentially locks out local http://
        # dev, if sent when the app isn't actually served over TLS.
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


# ── Helpers ──────────────────────────────────────────────────────────────

def bmi_of(weight_kg: float, height_cm: float) -> float | None:
    if not weight_kg or not height_cm:
        return None
    h_m = height_cm / 100
    return round(weight_kg / (h_m * h_m), 1)


def bmi_category(bmi: float) -> str:
    if bmi < 18.5:
        return "Underweight"
    if bmi < 25:
        return "Normal"
    if bmi < 30:
        return "Overweight"
    return "Obese"


def bmi_category_slug(bmi: float) -> str:
    """CSS-friendly hook (e.g. 'underweight'/'normal'/'overweight'/'obese')
    so templates can color-code the BMI category label."""
    return bmi_category(bmi).lower()


def weight_sparkline_svg(weights_desc, width=560, height=90, pad=10) -> str | None:
    """weights_desc: rows ordered most-recent-first (as queried). Renders oldest
    to newest, left to right."""
    weights_asc = list(reversed(weights_desc))
    if len(weights_asc) < 2:
        return None
    vals = [w["weight_kg"] for w in weights_asc]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)

    def px(i):
        return pad + i * (width - 2 * pad) / (n - 1)

    def py(v):
        return height - pad - (v - lo) * (height - 2 * pad) / rng

    points = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="#2f6f5e" />'
        for i, v in enumerate(vals)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="sparkline" preserveAspectRatio="none">'
        f'<polyline points="{points}" fill="none" stroke="#2f6f5e" stroke-width="2.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}</svg>'
    )


def get_or_create_profile(conn, user_id):
    profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    if profile is None:
        conn.execute("INSERT INTO profile (user_id, height_cm) VALUES (?, NULL)", (user_id,))
        profile = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    return profile


# ── Public landing page ─────────────────────────────────────────────────

@app.route("/")
def home():
    """Public marketing/landing page — what visitors see before signing up.
    Logged-in users get a 'Go to Dashboard' link instead of Log In/Sign Up
    (see home.html), but they're not force-redirected off this page, so the
    URL still works as a normal home page for anyone, logged in or not."""
    return render_template("home.html")


# ── Dashboard ────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = g.user_id
    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        profile = get_or_create_profile(conn, user_id)
        latest_weight = conn.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        upcoming_reminders = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND active = 1 ORDER BY date(next_due) LIMIT 5",
            (user_id,),
        ).fetchall()
        habits = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1", (user_id,)
        ).fetchall()
        today_food = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ?", (user_id, today)
        ).fetchall()

    bmi = bmi_of(latest_weight["weight_kg"], profile["height_cm"]) if latest_weight and profile["height_cm"] else None

    habit_status = []
    status_by_id = scheduler.get_habit_status_batch(habits)
    for h in habits:
        s = status_by_id[h["id"]]
        habit_status.append({"habit": h, "done": s["done"], "streak": s["streak"]})

    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for f in today_food:
        for k in totals:
            totals[k] += f[k] * f["servings"]

    return render_template(
        "dashboard.html",
        profile=profile, latest_weight=latest_weight, bmi=bmi,
        bmi_category=bmi_category(bmi) if bmi else None,
        bmi_category_slug=bmi_category_slug(bmi) if bmi else None,
        upcoming_reminders=upcoming_reminders, habit_status=habit_status,
        totals=totals,
    )


# ── Health ───────────────────────────────────────────────────────────────

@app.route("/health")
@login_required
def health():
    user_id = g.user_id
    with db.get_conn() as conn:
        profile = get_or_create_profile(conn, user_id)
        weights = conn.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30",
            (user_id,),
        ).fetchall()
        sessions = conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 30",
            (user_id,),
        ).fetchall()

    latest = weights[0] if weights else None
    bmi = bmi_of(latest["weight_kg"], profile["height_cm"]) if latest and profile["height_cm"] else None
    return render_template(
        "health.html", profile=profile, weights=weights, sessions=sessions,
        bmi=bmi, bmi_category=bmi_category(bmi) if bmi else None,
        bmi_category_slug=bmi_category_slug(bmi) if bmi else None,
        today=date.today().isoformat(),
        sparkline_svg=weight_sparkline_svg(weights),
    )


@app.route("/health/height", methods=["POST"])
@login_required
def set_height():
    height = request.form.get("height_cm", type=float)
    birth_date = request.form.get("birth_date", "").strip() or None
    sex = request.form.get("sex", "").strip() or None
    with db.get_conn() as conn:
        get_or_create_profile(conn, g.user_id)
        conn.execute(
            "UPDATE profile SET height_cm = ?, birth_date = ?, sex = ? WHERE user_id = ?",
            (height, birth_date, sex, g.user_id),
        )
    flash("Profile updated.")
    return redirect(url_for("health"))


@app.route("/health/weight", methods=["POST"])
@login_required
def add_weight():
    d = request.form.get("date") or date.today().isoformat()
    w = request.form.get("weight_kg", type=float)
    if w:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO weight_entries (user_id, date, weight_kg, created_at) VALUES (?, ?, ?, ?)",
                (g.user_id, d, w, db.now()),
            )
        flash("Weight logged.")
    return redirect(url_for("health"))


@app.route("/health/weight/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_weight(entry_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM weight_entries WHERE id = ? AND user_id = ?", (entry_id, g.user_id))
    return redirect(url_for("health"))


@app.route("/health/session", methods=["POST"])
@login_required
def add_session():
    d = request.form.get("date") or date.today().isoformat()
    stype = request.form.get("type", "").strip()
    duration = request.form.get("duration_minutes", type=int)
    notes = request.form.get("notes", "").strip()
    if stype:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, date, type, duration_minutes, notes, created_at) VALUES (?,?,?,?,?,?)",
                (g.user_id, d, stype, duration, notes, db.now()),
            )
        flash("Session logged.")
    return redirect(url_for("health"))


@app.route("/health/session/<int:session_id>/delete", methods=["POST"])
@login_required
def delete_session(session_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, g.user_id))
    return redirect(url_for("health"))


# ── Nutrition ────────────────────────────────────────────────────────────

@app.route("/nutrition")
@login_required
def nutrition():
    user_id = g.user_id
    d = request.args.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        log = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ? ORDER BY id", (user_id, d)
        ).fetchall()

        profile = get_or_create_profile(conn, user_id)
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

    # A manual target set on the Settings page always wins; otherwise fall
    # back to the computed recommendation automatically (no more separate
    # "apply as goal" button/step — the recommendation *is* the goal unless
    # overridden).
    goals = {
        "calories": float(goal_cal) if goal_cal else (recommendation["calories"] if recommendation else None),
        "protein_g": float(goal_protein) if goal_protein else (recommendation["protein_g"] if recommendation else None),
    }

    return render_template("nutrition.html", totals=totals, meal_summary=meal_summary,
                            view_date=d, goals=goals, recommendation=recommendation)


MEAL_LABELS = {"breakfast": "🍳 Breakfast", "lunch": "🥗 Lunch", "dinner": "🍽️ Dinner", "snack": "🍎 Snack"}


@app.route("/nutrition/meal/<meal>")
@login_required
def nutrition_meal(meal):
    user_id = g.user_id
    if meal not in MEAL_LABELS:
        return redirect(url_for("nutrition"))
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

    return render_template("nutrition_meal.html", meal=meal, meal_label=MEAL_LABELS[meal],
                            entries=entries, custom_foods=custom_foods, totals=totals, view_date=d)


@app.route("/nutrition/search")
@login_required
def nutrition_search():
    q = request.args.get("q", "")
    results = nutrition_api.search_foods(q)
    return {"results": results}


@app.route("/nutrition/log", methods=["POST"])
@login_required
def log_food():
    user_id = g.user_id
    d = request.form.get("date") or date.today().isoformat()
    grams = request.form.get("grams", type=float)
    if grams is not None:
        servings = grams / 100.0
    else:
        # backward compatible fallback if an old cached page still submits "servings"
        servings = request.form.get("servings", type=float) or 1.0
    name = request.form.get("name", "").strip()
    source = request.form.get("source", "usda")
    meal = request.form.get("meal", "snack")
    if meal not in ("breakfast", "lunch", "dinner", "snack"):
        meal = "snack"

    fields = {}
    for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
        fields[k] = request.form.get(k, type=float) or 0.0

    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO food_log (user_id, date, source, custom_food_id, name, meal, servings, "
                "calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, d, source, request.form.get("custom_food_id", type=int), name, meal, servings,
                 fields["calories"], fields["protein_g"], fields["carbs_g"], fields["fat_g"],
                 fields["fiber_g"], db.now()),
            )
        flash(f"Logged {name}.")
    return redirect(url_for("nutrition_meal", meal=meal, date=d))


@app.route("/nutrition/log/<int:log_id>/delete", methods=["POST"])
@login_required
def delete_food_log(log_id):
    user_id = g.user_id
    d = request.form.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        entry = conn.execute(
            "SELECT meal FROM food_log WHERE id = ? AND user_id = ?", (log_id, user_id)
        ).fetchone()
        conn.execute("DELETE FROM food_log WHERE id = ? AND user_id = ?", (log_id, user_id))
    meal = entry["meal"] if entry else "snack"
    return redirect(url_for("nutrition_meal", meal=meal, date=d))


@app.route("/nutrition/custom", methods=["POST"])
@login_required
def add_custom_food():
    user_id = g.user_id
    name = request.form.get("name", "").strip()
    meal = request.form.get("meal", "")
    d = request.form.get("date") or date.today().isoformat()
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO custom_foods (user_id, name, calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, name,
                 request.form.get("calories", type=float) or 0,
                 request.form.get("protein_g", type=float) or 0,
                 request.form.get("carbs_g", type=float) or 0,
                 request.form.get("fat_g", type=float) or 0,
                 request.form.get("fiber_g", type=float) or 0,
                 db.now()),
            )
        flash(f"Added custom food: {name}")
    if meal in MEAL_LABELS:
        return redirect(url_for("nutrition_meal", meal=meal, date=d))
    return redirect(url_for("nutrition"))


@app.route("/nutrition/custom/<int:food_id>/delete", methods=["POST"])
@login_required
def delete_custom_food(food_id):
    user_id = g.user_id
    meal = request.form.get("meal", "")
    d = request.form.get("date") or date.today().isoformat()
    with db.get_conn() as conn:
        food = conn.execute(
            "SELECT name FROM custom_foods WHERE id = ? AND user_id = ?", (food_id, user_id)
        ).fetchone()
        conn.execute("DELETE FROM custom_foods WHERE id = ? AND user_id = ?", (food_id, user_id))
    if food:
        flash(f"Deleted custom food: {food['name']}")
    if meal in MEAL_LABELS:
        return redirect(url_for("nutrition_meal", meal=meal, date=d))
    return redirect(url_for("nutrition"))


# ── ID Vault ─────────────────────────────────────────────────────────────

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "heic", "pdf"}


@app.route("/vault")
@login_required
def vault():
    user_id = g.user_id
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY label", (user_id,)
        ).fetchall()

    today = scheduler.today_local(user_id)
    docs = []
    for d in rows:
        entry = dict(d)
        if d["expiry_date"]:
            try:
                entry["days_left"] = (date.fromisoformat(d["expiry_date"]) - today).days
            except ValueError:
                entry["days_left"] = None
        else:
            entry["days_left"] = None
        entry["acknowledged"] = d["expiry_ack_date"] == d["expiry_date"] and d["expiry_date"] is not None
        docs.append(entry)

    return render_template("vault.html", docs=docs)


@app.route("/vault/upload", methods=["POST"])
@login_required
def vault_upload():
    user_id = g.user_id
    label = request.form.get("label", "").strip()
    notes = request.form.get("notes", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip() or None
    file = request.files.get("file")

    if not label or not file or file.filename == "":
        flash("Label and file are required.")
        return redirect(url_for("vault"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        flash("Unsupported file type.")
        return redirect(url_for("vault"))

    filename = f"{uuid.uuid4().hex}.{ext}"

    if config.USE_B2:
        try:
            storage.upload_fileobj(file, filename, content_type=file.mimetype)
        except Exception:
            logger.exception("B2 upload failed for %s", filename)
            flash("Upload failed — couldn't reach file storage. Please try again.")
            return redirect(url_for("vault"))
    else:
        logger.warning(
            "B2 not configured — saving %s to local disk, which Render wipes on "
            "every restart/redeploy. Set B2_KEY_ID/B2_APPLICATION_KEY/B2_ENDPOINT_URL "
            "to persist vault files.", filename,
        )
        file.save(config.UPLOAD_DIR / filename)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (user_id, label, filename, notes, expiry_date, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, label, filename, notes, expiry_date, db.now()),
        )

    flash(f"Saved {label}." + (" You'll get expiry reminders automatically as the date approaches." if expiry_date else ""))
    return redirect(url_for("vault"))


@app.route("/vault/<int:doc_id>/renew", methods=["POST"])
@login_required
def vault_renew(doc_id):
    new_expiry = request.form.get("expiry_date", "").strip() or None
    with db.get_conn() as conn:
        # Renewing to a new date naturally resets the reminder cycle, since
        # expiry_ack_date is cleared and no longer matches the old expiry.
        conn.execute(
            "UPDATE documents SET expiry_date = ?, expiry_ack_date = NULL WHERE id = ? AND user_id = ?",
            (new_expiry, doc_id, g.user_id),
        )
    flash("Expiry date updated.")
    return redirect(url_for("vault"))


@app.route("/vault/<int:doc_id>/acknowledge", methods=["POST"])
@login_required
def vault_acknowledge(doc_id):
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, g.user_id)
        ).fetchone()
        if doc:
            conn.execute(
                "UPDATE documents SET expiry_ack_date = ? WHERE id = ?",
                (doc["expiry_date"], doc_id),
            )
    flash("Got it — reminders paused for this expiry until you renew it.")
    return redirect(url_for("vault"))


@app.route("/vault/file/<filename>")
@login_required
def vault_file(filename):
    with db.get_conn() as conn:
        owned = conn.execute(
            "SELECT 1 FROM documents WHERE filename = ? AND user_id = ?", (filename, g.user_id)
        ).fetchone()
    if not owned:
        return "Not found.", 404
    if config.USE_B2:
        return redirect(storage.presigned_url(filename))
    return send_from_directory(config.UPLOAD_DIR, filename)


@app.route("/vault/<int:doc_id>/download")
@login_required
def vault_download(doc_id):
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, g.user_id)
        ).fetchone()
    if not doc:
        return redirect(url_for("vault"))
    ext = doc["filename"].rsplit(".", 1)[-1] if "." in doc["filename"] else ""
    safe_label = "".join(c for c in doc["label"] if c.isalnum() or c in " -_").strip() or "document"
    download_name = f"{safe_label}.{ext}" if ext else safe_label
    if config.USE_B2:
        return redirect(storage.presigned_url(doc["filename"], download_name=download_name))
    return send_from_directory(config.UPLOAD_DIR, doc["filename"], as_attachment=True, download_name=download_name)


@app.route("/vault/<int:doc_id>/delete", methods=["POST"])
@login_required
def vault_delete(doc_id):
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, g.user_id)
        ).fetchone()
        if doc:
            if config.USE_B2:
                try:
                    storage.delete_file(doc["filename"])
                except Exception:
                    logger.exception("B2 delete failed for %s", doc["filename"])
            else:
                path = config.UPLOAD_DIR / doc["filename"]
                if path.exists():
                    path.unlink()
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return redirect(url_for("vault"))


# ── Reminders ────────────────────────────────────────────────────────────

@app.route("/reminders")
@login_required
def reminders():
    with db.get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY active DESC, date(next_due)",
            (g.user_id,),
        ).fetchall()
    return render_template("reminders.html", reminders=items)


@app.route("/reminders", methods=["POST"])
@login_required
def add_reminder():
    title = request.form.get("title", "").strip()
    next_due = request.form.get("next_due")
    recurrence = request.form.get("recurrence", "once")
    if title and next_due:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, next_due, recurrence, active, created_at) VALUES (?,?,?,?,1,?)",
                (g.user_id, title, next_due, recurrence, db.now()),
            )
        flash(f"Reminder set: {title}")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/snooze", methods=["POST"])
@login_required
def snooze_reminder(reminder_id):
    days = request.form.get("days", type=int) or 1
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT next_due FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, g.user_id)
        ).fetchone()
        if r:
            base = max(date.fromisoformat(r["next_due"]), scheduler.today_local(g.user_id))
            new_due = base + timedelta(days=days)
            conn.execute("UPDATE reminders SET next_due = ?, active = 1 WHERE id = ?",
                         (new_due.isoformat(), reminder_id))
    flash(f"Snoozed {days} day(s).")
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/toggle", methods=["POST"])
@login_required
def toggle_reminder(reminder_id):
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT active FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, g.user_id)
        ).fetchone()
        if r:
            conn.execute("UPDATE reminders SET active = ? WHERE id = ?", (0 if r["active"] else 1, reminder_id))
    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/delete", methods=["POST"])
@login_required
def delete_reminder(reminder_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, g.user_id))
    return redirect(url_for("reminders"))


# ── Habits ───────────────────────────────────────────────────────────────

@app.route("/habits")
@login_required
def habits():
    user_id = g.user_id
    with db.get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY frequency, title",
            (user_id,),
        ).fetchall()
        todos = conn.execute(
            "SELECT * FROM todos WHERE user_id = ? ORDER BY done, created_at DESC", (user_id,)
        ).fetchall()

    today = scheduler.today_local(user_id)
    status = []
    status_by_id = scheduler.get_habit_status_batch(items)
    for h in items:
        pkey = scheduler.period_key_for(h["frequency"], today)
        s = status_by_id[h["id"]]
        status.append({"habit": h, "done": s["done"], "period_key": pkey, "streak": s["streak"]})

    return render_template("habits.html", status=status, todos=todos)


@app.route("/habits", methods=["POST"])
@login_required
def add_habit():
    title = request.form.get("title", "").strip()
    frequency = request.form.get("frequency", "daily")
    reminder_hour = request.form.get("reminder_hour", type=int)
    if reminder_hour is not None:
        reminder_hour = max(0, min(reminder_hour, 23))
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO habits (user_id, title, frequency, reminder_hour, active, created_at) VALUES (?,?,?,?,1,?)",
                (g.user_id, title, frequency, reminder_hour, db.now()),
            )
        flash(f"Habit added: {title}")
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/reminder", methods=["POST"])
@login_required
def set_habit_reminder(habit_id):
    reminder_hour = request.form.get("reminder_hour", type=int)
    if reminder_hour is not None:
        reminder_hour = max(0, min(reminder_hour, 23))
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE habits SET reminder_hour = ? WHERE id = ? AND user_id = ?",
            (reminder_hour, habit_id, g.user_id),
        )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/checkin", methods=["POST"])
@login_required
def checkin_habit(habit_id):
    with db.get_conn() as conn:
        h = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, g.user_id)
        ).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(g.user_id))
            conn.execute(
                "INSERT OR IGNORE INTO habit_checkins (habit_id, period_key, done_at) VALUES (?,?,?)",
                (habit_id, pkey, db.now()),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/uncheck", methods=["POST"])
@login_required
def uncheck_habit(habit_id):
    with db.get_conn() as conn:
        h = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, g.user_id)
        ).fetchone()
        if h:
            pkey = scheduler.period_key_for(h["frequency"], scheduler.today_local(g.user_id))
            conn.execute(
                "DELETE FROM habit_checkins WHERE habit_id = ? AND period_key = ?",
                (habit_id, pkey),
            )
    return redirect(url_for("habits"))


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
@login_required
def delete_habit(habit_id):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE habits SET active = 0 WHERE id = ? AND user_id = ?", (habit_id, g.user_id)
        )
    return redirect(url_for("habits"))


# ── To-Dos (one-time, non-recurring) ────────────────────────────────────

@app.route("/todos", methods=["POST"])
@login_required
def add_todo():
    title = request.form.get("title", "").strip()
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO todos (user_id, title, done, created_at) VALUES (?,?,0,?)",
                (g.user_id, title, db.now()),
            )
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/check", methods=["POST"])
@login_required
def check_todo(todo_id):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE todos SET done = 1, completed_at = ? WHERE id = ? AND user_id = ?",
            (db.now(), todo_id, g.user_id),
        )
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/uncheck", methods=["POST"])
@login_required
def uncheck_todo(todo_id):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE todos SET done = 0, completed_at = NULL WHERE id = ? AND user_id = ?",
            (todo_id, g.user_id),
        )
    return redirect(url_for("habits"))


@app.route("/todos/<int:todo_id>/delete", methods=["POST"])
@login_required
def delete_todo(todo_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, g.user_id))
    return redirect(url_for("habits"))


# ── Budget ───────────────────────────────────────────────────────────────

def _augment_savings_goal(g_row):
    """Adds computed fields to a savings_goals row: how much is left to
    save, and — if a target date is set — how much per week that works out
    to given what's already saved and how much time is left."""
    entry = dict(g_row)
    target = g_row["target_amount"]
    current = g_row["current_amount"]
    target_date = g_row["target_date"]

    entry["remaining"] = max(0, target - current) if target else None
    entry["pct"] = (current / target * 100) if target else None
    entry["days_left"] = None
    entry["weekly_needed"] = None
    entry["status"] = None  # 'reached' | 'overdue' | 'on_track' | None

    if target and current >= target:
        entry["status"] = "reached"
    elif target_date:
        try:
            td = date.fromisoformat(target_date)
        except ValueError:
            td = None
        if td:
            days_left = (td - scheduler.today_local(g.user_id)).days
            entry["days_left"] = days_left
            if days_left <= 0:
                entry["status"] = "overdue"
            elif target:
                weeks_left = max(days_left / 7, 1 / 7)  # never divide by zero
                entry["weekly_needed"] = entry["remaining"] / weeks_left
                entry["status"] = "on_track"

    return entry


@app.route("/budget")
@login_required
def budget():
    user_id = g.user_id
    month = request.args.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    year = request.args.get("year") or month[:4]
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
        savings_goals = [_augment_savings_goal(sg) for sg in savings_goals_raw]

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
                "category": c, "spent": spent, "limit": limit, "pct": pct,
                "over": bool(limit and spent > limit),
            })

        # ── Yearly summary section ──
        yearly_rows = conn.execute(
            "SELECT strftime('%m', date) AS m, type, COALESCE(SUM(amount),0) AS total "
            "FROM transactions WHERE user_id = ? AND strftime('%Y', date) = ? GROUP BY m, type",
            (user_id, year),
        ).fetchall()
        year_rows = conn.execute(
            "SELECT DISTINCT strftime('%Y', date) AS y FROM transactions WHERE user_id = ? ORDER BY y DESC",
            (user_id,),
        ).fetchall()

        yearly_category_totals = []
        for c in categories:
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
                "WHERE user_id = ? AND type='expense' AND category_id=? AND strftime('%Y', date)=?",
                (user_id, c["id"], year),
            ).fetchone()["t"]
            if spent:
                annual_limit = c["monthly_limit"] * 12 if c["monthly_limit"] else None
                pct = min(round(spent / annual_limit * 100), 100) if annual_limit else None
                yearly_category_totals.append({
                    "category": c, "spent": spent, "annual_limit": annual_limit, "pct": pct,
                    "over": bool(annual_limit and spent > annual_limit),
                })
        yearly_category_totals.sort(key=lambda x: x["spent"], reverse=True)

    by_month = {f"{i:02d}": {"income": 0.0, "expense": 0.0} for i in range(1, 13)}
    for r in yearly_rows:
        if r["m"] in by_month and r["type"] in ("income", "expense"):
            by_month[r["m"]][r["type"]] = r["total"]

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    max_val = 0.01  # avoid div-by-zero for empty years
    for v in by_month.values():
        max_val = max(max_val, v["income"], v["expense"])

    yearly_months = []
    for i in range(1, 13):
        key = f"{i:02d}"
        inc, exp = by_month[key]["income"], by_month[key]["expense"]
        yearly_months.append({
            "num": key, "label": month_names[i - 1], "income": inc, "expense": exp,
            "net": inc - exp,
            "income_pct": round(inc / max_val * 100),
            "expense_pct": round(exp / max_val * 100),
        })

    yearly_income_total = sum(m["income"] for m in yearly_months)
    yearly_expense_total = sum(m["expense"] for m in yearly_months)
    available_years = sorted({r["y"] for r in year_rows} | {year}, reverse=True)

    return render_template(
        "budget.html", month=month, categories=categories, txns=txns,
        income_total=income_total, expense_total=expense_total,
        net=income_total - expense_total, category_spend=category_spend,
        currency=currency, recurring=recurring, savings_goals=savings_goals,
        year=year, available_years=available_years, yearly_months=yearly_months,
        yearly_income_total=yearly_income_total, yearly_expense_total=yearly_expense_total,
        yearly_net=yearly_income_total - yearly_expense_total,
        yearly_category_totals=yearly_category_totals,
    )


@app.route("/budget/transaction", methods=["POST"])
@login_required
def add_transaction():
    user_id = g.user_id
    month = request.form.get("month") or scheduler.today_local(user_id).strftime("%Y-%m")
    d = request.form.get("date") or date.today().isoformat()
    ttype = request.form.get("type", "expense")
    amount = request.form.get("amount", type=float)
    description = request.form.get("description", "").strip()
    category_id = request.form.get("category_id", type=int) or None

    # If the person left the category blank on an expense, take one best-
    # effort shot at guessing it from the description before saving —
    # never blocks the save, and only ever picks from their own existing
    # categories (see ai.suggest_category), so a bad/unavailable AI call
    # just leaves the transaction uncategorized exactly as before.
    auto_categorized = False
    if ttype == "expense" and category_id is None and description and ai.available():
        with db.get_conn() as conn:
            cats = conn.execute(
                "SELECT id, name FROM budget_categories WHERE user_id = ?", (user_id,)
            ).fetchall()
        if cats:
            guess, _err = ai.suggest_category(description, [c["name"] for c in cats])
            if guess:
                category_id = next((c["id"] for c in cats if c["name"] == guess), None)
                auto_categorized = category_id is not None

    if amount:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, d, ttype, category_id if ttype == "expense" else None, description, abs(amount), db.now()),
            )
        suffix = " (category auto-filled by AI)" if auto_categorized else ""
        flash(f"Logged {'income' if ttype == 'income' else 'expense'}: {amount}{suffix}")
    return redirect(url_for("budget", month=month))


@app.route("/budget/transaction/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    month = request.form.get("month") or scheduler.today_local(g.user_id).strftime("%Y-%m")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (txn_id, g.user_id))
    return redirect(url_for("budget", month=month))


@app.route("/budget/category", methods=["POST"])
@login_required
def add_budget_category():
    user_id = g.user_id
    name = request.form.get("name", "").strip()
    limit = request.form.get("monthly_limit", type=float)
    if name:
        with db.get_conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM budget_categories WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
            if existing:
                flash(f"'{name}' already exists.")
            else:
                conn.execute(
                    "INSERT INTO budget_categories (user_id, name, monthly_limit, created_at) VALUES (?,?,?,?)",
                    (user_id, name, limit, db.now()),
                )
                flash(f"Category added: {name}")
    return redirect(url_for("budget"))


@app.route("/budget/category/<int:cat_id>/delete", methods=["POST"])
@login_required
def delete_budget_category(cat_id):
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM budget_categories WHERE id = ? AND user_id = ?", (cat_id, g.user_id)
        )
    return redirect(url_for("budget"))


@app.route("/budget/recurring", methods=["POST"])
@login_required
def add_recurring_transaction():
    import calendar

    user_id = g.user_id
    title = request.form.get("title", "").strip()
    ttype = request.form.get("type", "expense")
    amount = request.form.get("amount", type=float)
    category_id = request.form.get("category_id", type=int) or None
    frequency = request.form.get("frequency", "monthly")
    if frequency not in ("monthly", "weekly"):
        frequency = "monthly"
    day_of_month = request.form.get("day_of_month", type=int) or 1
    day_of_month = max(1, min(day_of_month, 28))
    day_of_week = request.form.get("day_of_week", type=int)
    day_of_week = 0 if day_of_week is None else max(0, min(day_of_week, 6))
    start_raw = request.form.get("start_date") or scheduler.today_local(user_id).isoformat()

    if title and amount:
        if len(start_raw) == 7:  # "YYYY-MM" from the month picker (monthly only)
            start_raw += "-01"
        start = date.fromisoformat(start_raw)

        if frequency == "weekly":
            next_run = start
            while next_run.weekday() != day_of_week:
                next_run += timedelta(days=1)
            while next_run < scheduler.today_local(user_id):
                next_run += timedelta(days=7)
        else:
            clamped_day = min(day_of_month, calendar.monthrange(start.year, start.month)[1])
            next_run = start.replace(day=clamped_day)
            if next_run < scheduler.today_local(user_id):
                next_run = scheduler.add_months(next_run, 1)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO recurring_transactions "
                "(user_id, title, type, amount, category_id, frequency, day_of_month, day_of_week, next_run, active, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (user_id, title, ttype, amount, category_id if ttype == "expense" else None,
                 frequency, day_of_month, day_of_week if frequency == "weekly" else None,
                 next_run.isoformat(), db.now()),
            )
        flash(f"Recurring {ttype} set up: {title} — first run {next_run.isoformat()}")
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/toggle", methods=["POST"])
@login_required
def toggle_recurring_transaction(rid):
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT active FROM recurring_transactions WHERE id = ? AND user_id = ?", (rid, g.user_id)
        ).fetchone()
        if r:
            conn.execute("UPDATE recurring_transactions SET active = ? WHERE id = ?",
                         (0 if r["active"] else 1, rid))
    return redirect(url_for("budget"))


@app.route("/budget/recurring/<int:rid>/delete", methods=["POST"])
@login_required
def delete_recurring_transaction(rid):
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM recurring_transactions WHERE id = ? AND user_id = ?", (rid, g.user_id)
        )
    return redirect(url_for("budget"))


# ── Savings goals ────────────────────────────────────────────────────────

@app.route("/budget/savings", methods=["POST"])
@login_required
def add_savings_goal():
    user_id = g.user_id
    name = request.form.get("name", "").strip()
    target_amount = request.form.get("target_amount", type=float)
    target_date = request.form.get("target_date", "").strip() or None
    if name:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO savings_goals (user_id, name, target_amount, target_date, current_amount, created_at) "
                "VALUES (?,?,?,?,0,?)",
                (user_id, name, target_amount, target_date, db.now()),
            )
        flash(f"Savings goal created: {name}")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/target", methods=["POST"])
@login_required
def set_savings_target(goal_id):
    target_date = request.form.get("target_date", "").strip() or None
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE savings_goals SET target_date = ? WHERE id = ? AND user_id = ?",
            (target_date, goal_id, g.user_id),
        )
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/contribute", methods=["POST"])
@login_required
def contribute_savings_goal(goal_id):
    user_id = g.user_id
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local(user_id).isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute(
                "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
            ).fetchone()
            if goal:
                # Money moving into savings reduces what's available to spend,
                # so it's logged as a normal expense transaction — the goal's
                # progress bar and your budget totals both stay accurate.
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
        flash(f"Added {amount:.0f} to savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/withdraw", methods=["POST"])
@login_required
def withdraw_savings_goal(goal_id):
    user_id = g.user_id
    amount = request.form.get("amount", type=float)
    today = scheduler.today_local(user_id).isoformat()
    if amount and amount > 0:
        with db.get_conn() as conn:
            goal = conn.execute(
                "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
            ).fetchone()
            if goal:
                amount = min(amount, goal["current_amount"])  # can't withdraw more than saved
                # Money coming back out of savings adds back to what's
                # available to spend, so it's logged as income.
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
        flash(f"Withdrew {amount:.0f} from savings")
    return redirect(url_for("budget"))


@app.route("/budget/savings/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_savings_goal(goal_id):
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, g.user_id)
        )
    return redirect(url_for("budget"))


# ── Passwords ────────────────────────────────────────────────────────────

@app.route("/passwords")
@login_required
def passwords():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM passwords WHERE user_id = ? ORDER BY label", (g.user_id,)
        ).fetchall()
    items = []
    for r in rows:
        entry = dict(r)
        entry["password"] = crypto.decrypt(r["password_enc"])
        items.append(entry)
    return render_template("passwords.html", items=items)


@app.route("/passwords", methods=["POST"])
@login_required
def add_password():
    user_id = g.user_id
    label = request.form.get("label", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    url = request.form.get("url", "").strip()
    notes = request.form.get("notes", "").strip()
    if label and password:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO passwords (user_id, label, username, password_enc, url, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, label, username, crypto.encrypt(password), url, notes, db.now()),
            )
        flash(f"Saved password: {label}")
    return redirect(url_for("passwords"))


@app.route("/passwords/<int:pw_id>/edit", methods=["POST"])
@login_required
def edit_password(pw_id):
    user_id = g.user_id
    label = request.form.get("label", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")  # blank = keep existing password
    url = request.form.get("url", "").strip()
    notes = request.form.get("notes", "").strip()
    with db.get_conn() as conn:
        if password:
            conn.execute(
                "UPDATE passwords SET label=?, username=?, password_enc=?, url=?, notes=? WHERE id=? AND user_id=?",
                (label, username, crypto.encrypt(password), url, notes, pw_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE passwords SET label=?, username=?, url=?, notes=? WHERE id=? AND user_id=?",
                (label, username, url, notes, pw_id, user_id),
            )
    flash(f"Updated: {label}")
    return redirect(url_for("passwords"))


@app.route("/passwords/<int:pw_id>/delete", methods=["POST"])
@login_required
def delete_password(pw_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM passwords WHERE id = ? AND user_id = ?", (pw_id, g.user_id))
    return redirect(url_for("passwords"))


# ── Notes ────────────────────────────────────────────────────────────────

@app.route("/notes")
@login_required
def notes():
    with db.get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY updated_at DESC", (g.user_id,)
        ).fetchall()
    return render_template("notes.html", items=items)


@app.route("/notes", methods=["POST"])
@login_required
def add_note():
    user_id = g.user_id
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    if title:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO notes (user_id, title, body, created_at, updated_at) VALUES (?,?,?,?,?)",
                (user_id, title, body, db.now(), db.now()),
            )
        flash(f"Note added: {title}")
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/edit", methods=["POST"])
@login_required
def edit_note(note_id):
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE notes SET title=?, body=?, updated_at=? WHERE id=? AND user_id=?",
            (title, body, db.now(), note_id, g.user_id),
        )
    flash("Note updated.")
    return redirect(url_for("notes"))


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, g.user_id))
    return redirect(url_for("notes"))


@app.route("/notes/bulk-delete", methods=["POST"])
@login_required
def bulk_delete_notes():
    ids = request.form.getlist("note_ids", type=int)
    if ids:
        with db.get_conn() as conn:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM notes WHERE user_id = ? AND id IN ({placeholders})",
                (g.user_id, *ids),
            )
        flash(f"Deleted {len(ids)} note{'s' if len(ids) != 1 else ''}.")
    return redirect(url_for("notes"))


# ── AI ────────────────────────────────────────────────────────────────────
# Two user-facing features (chat assistant, natural-language quick-add) on
# top of the ai.py module. Both are best-effort and read config.py's
# GEMINI_API_KEY — with nothing set, the pages just show a "not configured"
# state rather than erroring (same pattern as Telegram/Resend/B2 elsewhere
# in this app). See ai.py for the actual Gemini calls and why Gemini.

def _build_chat_context(user_id: int) -> str:
    """Plain-text digest of the user's own data for the chat assistant to
    answer from. Deliberately excludes vault documents, saved passwords,
    and notes — those are the most sensitive things in this app, and the
    assistant has no need to see them to answer budget/health/habit
    questions. This is the *only* thing the model sees about the account;
    it never gets direct DB or tool access (see ai.ask)."""
    today = scheduler.today_local(user_id)
    month = today.strftime("%Y-%m")
    lines = [f"Today's date: {today.isoformat()}"]

    with db.get_conn() as conn:
        income = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
            "WHERE user_id = ? AND type='income' AND strftime('%Y-%m', date)=?",
            (user_id, month),
        ).fetchone()["t"]
        expense = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM transactions "
            "WHERE user_id = ? AND type='expense' AND strftime('%Y-%m', date)=?",
            (user_id, month),
        ).fetchone()["t"]
        by_cat = conn.execute(
            "SELECT c.name, COALESCE(SUM(t.amount),0) AS spent, c.monthly_limit FROM budget_categories c "
            "LEFT JOIN transactions t ON t.category_id = c.id AND t.type='expense' AND strftime('%Y-%m', t.date)=? "
            "WHERE c.user_id = ? GROUP BY c.id ORDER BY spent DESC",
            (month, user_id),
        ).fetchall()
        currency = db.get_setting(user_id, "currency", "ETB") or "ETB"
        lines.append(f"This month ({month}) so far: income {income:.0f} {currency}, expenses {expense:.0f} {currency}.")
        if by_cat:
            lines.append("Spending by category this month:")
            for c in by_cat:
                limit_txt = f" (limit {c['monthly_limit']:.0f})" if c["monthly_limit"] else ""
                lines.append(f"  - {c['name']}: {c['spent']:.0f} {currency}{limit_txt}")

        reminders = conn.execute(
            "SELECT title, next_due FROM reminders WHERE user_id = ? AND active = 1 ORDER BY date(next_due) LIMIT 8",
            (user_id,),
        ).fetchall()
        if reminders:
            lines.append("Upcoming reminders:")
            for r in reminders:
                lines.append(f"  - {r['title']} due {r['next_due']}")

        habits_rows = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1", (user_id,)
        ).fetchall()
        todos_open = conn.execute(
            "SELECT COUNT(*) AS n FROM todos WHERE user_id = ? AND done = 0", (user_id,)
        ).fetchone()["n"]

        today_food = conn.execute(
            "SELECT * FROM food_log WHERE user_id = ? AND date = ?", (user_id, today.isoformat())
        ).fetchall()
        latest_weight = conn.execute(
            "SELECT weight_kg, date FROM weight_entries WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    if habits_rows:
        status_by_id = scheduler.get_habit_status_batch(habits_rows)
        lines.append("Habits:")
        for h in habits_rows:
            s = status_by_id[h["id"]]
            lines.append(f"  - {h['title']} ({h['frequency']}): {'done' if s['done'] else 'not done'} this period, streak {s['streak']}")
    lines.append(f"Open to-dos: {todos_open}")

    if today_food:
        cals = sum(f["calories"] * f["servings"] for f in today_food)
        protein = sum(f["protein_g"] * f["servings"] for f in today_food)
        lines.append(f"Logged today so far: {cals:.0f} kcal, {protein:.0f}g protein, across {len(today_food)} food entries.")
    else:
        lines.append("Nothing logged in Nutrition today yet.")

    if latest_weight:
        lines.append(f"Most recent weight entry: {latest_weight['weight_kg']} kg on {latest_weight['date']}.")

    return "\n".join(lines)


@app.route("/ai/chat", methods=["GET", "POST"])
@login_required
def ai_chat():
    if request.method == "GET":
        return render_template("ai_chat.html", ai_available=ai.available(), answer=None, question=None)

    question = request.form.get("question", "").strip()
    if not question:
        return render_template("ai_chat.html", ai_available=ai.available(), answer=None, question=None)
    if not ai.available():
        flash("AI isn't configured on this server.")
        return render_template("ai_chat.html", ai_available=False, answer=None, question=question)

    context = _build_chat_context(g.user_id)
    answer, err = ai.ask(question, context)
    if err:
        flash(err)
    return render_template("ai_chat.html", ai_available=True, answer=answer, question=question)


@app.route("/ai/quick-add", methods=["GET", "POST"])
@login_required
def ai_quick_add():
    if request.method == "GET":
        return render_template("ai_quick_add.html", ai_available=ai.available())

    text = request.form.get("text", "").strip()
    if not text:
        flash("Type something to add first.")
        return redirect(url_for("ai_quick_add"))
    if not ai.available():
        flash("AI isn't configured on this server.")
        return redirect(url_for("ai_quick_add"))

    user_id = g.user_id
    today = scheduler.today_local(user_id).isoformat()
    with db.get_conn() as conn:
        categories = conn.execute(
            "SELECT id, name FROM budget_categories WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()

    result, err = ai.parse_quick_add(text, [c["name"] for c in categories], today)
    if err or not isinstance(result, dict):
        flash(f"Couldn't parse that: {err or 'unexpected response'}")
        return redirect(url_for("ai_quick_add"))

    kind = result.get("type")

    if kind == "transaction":
        ttype = result.get("txn_type") if result.get("txn_type") in ("income", "expense") else "expense"
        amount = result.get("amount")
        try:
            amount = abs(float(amount))
        except (TypeError, ValueError):
            amount = None
        if not amount:
            flash("Couldn't tell how much that was for — try including an amount.")
            return redirect(url_for("ai_quick_add"))
        description = str(result.get("description") or text)[:200]
        cat_name = result.get("category")
        category_id = next((c["id"] for c in categories if c["name"] == cat_name), None)
        d = result.get("date") or today
        try:
            date.fromisoformat(d)
        except ValueError:
            d = today
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, date, type, category_id, description, amount, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, d, ttype, category_id if ttype == "expense" else None, description, amount, db.now()),
            )
        flash(f"Added {ttype}: {amount:.0f} — {description}")
        return redirect(url_for("budget"))

    if kind == "food":
        name = str(result.get("name") or "").strip()[:200]
        if not name:
            flash("Couldn't tell what food that was — try naming it.")
            return redirect(url_for("ai_quick_add"))
        meal = result.get("meal") if result.get("meal") in ("breakfast", "lunch", "dinner", "snack") else "snack"
        grams = result.get("grams")
        try:
            servings = max(float(grams), 1) / 100.0
        except (TypeError, ValueError):
            servings = 1.0
        macros = {}
        for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
            try:
                macros[k] = max(float(result.get(k) or 0), 0)
            except (TypeError, ValueError):
                macros[k] = 0.0
        d = result.get("date") or today
        try:
            date.fromisoformat(d)
        except ValueError:
            d = today
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO food_log (user_id, date, source, custom_food_id, name, meal, servings, "
                "calories, protein_g, carbs_g, fat_g, fiber_g, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, d, "ai", None, name, meal, servings,
                 macros["calories"], macros["protein_g"], macros["carbs_g"], macros["fat_g"],
                 macros["fiber_g"], db.now()),
            )
        flash(f"Logged {name} ({meal}) — nutrition estimated by AI, edit it on the Nutrition page if it's off.")
        return redirect(url_for("nutrition_meal", meal=meal, date=d))

    if kind == "reminder":
        title = str(result.get("title") or "").strip()[:200]
        next_due = result.get("next_due")
        try:
            date.fromisoformat(next_due)
        except (TypeError, ValueError):
            flash("Couldn't tell what date that reminder was for — try including one.")
            return redirect(url_for("ai_quick_add"))
        recurrence = result.get("recurrence") if result.get("recurrence") in ("once", "daily", "weekly", "monthly") else "once"
        if not title:
            flash("Couldn't tell what to remind you about.")
            return redirect(url_for("ai_quick_add"))
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, next_due, recurrence, active, created_at) VALUES (?,?,?,?,1,?)",
                (user_id, title, next_due, recurrence, db.now()),
            )
        flash(f"Reminder set: {title} ({next_due})")
        return redirect(url_for("reminders"))

    if kind == "todo":
        title = str(result.get("title") or "").strip()[:200]
        if not title:
            flash("Couldn't tell what the task was.")
            return redirect(url_for("ai_quick_add"))
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO todos (user_id, title, done, created_at) VALUES (?,?,0,?)",
                (user_id, title, db.now()),
            )
        flash(f"Added to-do: {title}")
        return redirect(url_for("habits"))

    # "unclear" or any other/unexpected value — nothing gets written.
    reason = result.get("reason") if isinstance(result.get("reason"), str) else None
    flash(f"Wasn't sure what you meant{': ' + reason if reason else ''} — try rephrasing with more detail.")
    return redirect(url_for("ai_quick_add"))


# ── Settings ─────────────────────────────────────────────────────────────

@app.route("/settings")
@login_required
def settings():
    user_id = g.user_id
    values = {
        "tg_chat_id": db.get_setting(user_id, "tg_chat_id", config.TG_CHAT_ID),
        "timezone": db.get_setting(user_id, "timezone", config.TIMEZONE),
        "reminder_hour": db.get_setting(user_id, "reminder_hour", config.REMINDER_HOUR),
        "nudge_hour": db.get_setting(user_id, "nudge_hour", config.NUDGE_HOUR),
        "week_end_day": db.get_setting(user_id, "week_end_day", config.WEEK_END_DAY),
        "currency": db.get_setting(user_id, "currency", "ETB"),
        "nutrition_goal_calories": db.get_setting(user_id, "nutrition_goal_calories", ""),
        "nutrition_goal_protein": db.get_setting(user_id, "nutrition_goal_protein", ""),
    }
    user = db.get_user_by_id(user_id)
    totp_enabled = bool(user["totp_enabled_at"])
    return render_template(
        "settings.html",
        values=values,
        totp_enabled=totp_enabled,
        backup_codes_remaining=db.count_unused_backup_codes(user_id) if totp_enabled else 0,
        tg_bot_configured=bool(config.TG_BOT_TOKEN),
    )


@app.route("/settings", methods=["POST"])
@login_required
def save_settings():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    user_id = g.user_id
    for key in ("tg_chat_id", "timezone", "reminder_hour", "nudge_hour",
                "week_end_day", "currency", "nutrition_goal_calories", "nutrition_goal_protein"):
        val = request.form.get(key)
        if val is None:
            continue  # field wasn't part of the form that was submitted — leave untouched
        val = val.strip()  # copy-pasted tokens/IDs often carry a stray space or newline
        if val == "":
            db.delete_setting(user_id, key)  # explicit blank = revert to default
            continue
        if key == "timezone":
            try:
                ZoneInfo(val)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                flash(f"'{val}' isn't a valid timezone (e.g. Africa/Addis_Ababa) — not saved.")
                continue
        db.set_setting(user_id, key, val)
    flash("Settings saved.")
    return redirect(url_for("settings"))


@app.route("/settings/change-password", methods=["POST"])
@login_required
@limiter.limit("5 per minute", key_func=lambda: f"changepw:{g.user_id}")
def change_password():
    user = db.get_user_by_id(g.user_id)
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not db.verify_password(user, current_password):
        flash("Current password is incorrect.")
        return redirect(url_for("settings"))
    if len(new_password) < 8:
        flash("New password must be at least 8 characters.")
        return redirect(url_for("settings"))
    if new_password != confirm_password:
        flash("New passwords don't match.")
        return redirect(url_for("settings"))

    db.set_password(g.user_id, new_password)
    # A password change is exactly the situation "Log out everywhere"
    # exists for — if someone else had your old password on another
    # device, this is what actually kicks them out, rather than leaving
    # their still-valid session sitting there.
    db.invalidate_other_sessions(g.user_id)
    session["session_issued_at"] = time.time()  # keep *this* session logged in
    flash("Password changed, and you've been logged out everywhere else.")
    return redirect(url_for("settings"))


@app.route("/settings/logout-everywhere", methods=["POST"])
@login_required
def logout_everywhere():
    db.invalidate_other_sessions(g.user_id)
    session["session_issued_at"] = time.time()  # keep *this* session logged in
    flash("You've been logged out on all other devices/browsers.")
    return redirect(url_for("settings"))


@app.route("/settings/2fa/setup")
@login_required
def totp_setup():
    """Starts (or restarts) enrollment: generates a fresh secret, stores it
    as *pending* (not yet trusted for login — see confirm_totp), and shows
    it for manual entry into an authenticator app. Revisiting this page
    generates a new secret each time, which is fine — nothing is "spent"
    until totp_confirm succeeds."""
    user = db.get_user_by_id(g.user_id)
    if user["totp_enabled_at"]:
        flash("2FA is already enabled. Disable it first if you want to re-enroll.")
        return redirect(url_for("settings"))
    secret = totp.generate_secret()
    db.set_totp_pending_secret(g.user_id, crypto.encrypt(secret))
    uri = totp.provisioning_uri(secret, user["email"])
    return render_template("totp_setup.html", secret=secret, uri=uri)


@app.route("/settings/2fa/confirm", methods=["POST"])
@login_required
@limiter.limit("10 per minute", key_func=lambda: f"totpconfirm:{g.user_id}")
def totp_confirm():
    user = db.get_user_by_id(g.user_id)
    pending_enc = user["totp_pending_secret_enc"]
    if not pending_enc:
        flash("2FA setup expired or wasn't started — start again.")
        return redirect(url_for("totp_setup"))
    secret = crypto.decrypt(pending_enc)
    code = request.form.get("code", "")
    if not totp.verify_totp(secret, code):
        flash("That code didn't match. Check the time on your phone and try again.")
        return render_template("totp_setup.html", secret=secret, uri=totp.provisioning_uri(secret, user["email"]))

    db.confirm_totp(g.user_id, pending_enc)
    backup_codes = totp.generate_backup_codes()
    db.set_totp_backup_codes(g.user_id, [generate_password_hash(c) for c in backup_codes])
    # Enabling 2FA is itself a security-relevant event worth invalidating
    # other sessions for, same reasoning as a password change.
    db.invalidate_other_sessions(g.user_id)
    session["session_issued_at"] = time.time()
    return render_template("totp_backup_codes.html", codes=backup_codes, heading="2FA is on")


@app.route("/settings/2fa/disable", methods=["POST"])
@login_required
@limiter.limit("5 per minute", key_func=lambda: f"totpdisable:{g.user_id}")
def totp_disable():
    user = db.get_user_by_id(g.user_id)
    # Require the password, not just an active session, to turn 2FA off —
    # otherwise anyone who gets to an unlocked/hijacked session (exactly
    # what 2FA is meant to add a layer against) could disable it in one
    # click with nothing else required.
    if not db.verify_password(user, request.form.get("password", "")):
        flash("Incorrect password — 2FA was not disabled.")
        return redirect(url_for("settings"))
    db.disable_totp(g.user_id)
    # Same reasoning as turning 2FA on (see totp_confirm): this changes the
    # account's security posture, so any other lingering session should have
    # to re-authenticate under the new state rather than ride out its
    # existing cookie for up to a week.
    db.invalidate_other_sessions(g.user_id)
    session["session_issued_at"] = time.time()
    flash("2FA has been disabled on your account.")
    return redirect(url_for("settings"))


@app.route("/settings/2fa/regenerate-codes", methods=["POST"])
@login_required
@limiter.limit("5 per minute", key_func=lambda: f"totpregen:{g.user_id}")
def totp_regenerate_codes():
    user = db.get_user_by_id(g.user_id)
    if not user["totp_enabled_at"]:
        flash("2FA isn't enabled.")
        return redirect(url_for("settings"))
    if not db.verify_password(user, request.form.get("password", "")):
        flash("Incorrect password — backup codes were not regenerated.")
        return redirect(url_for("settings"))
    backup_codes = totp.generate_backup_codes()
    db.set_totp_backup_codes(g.user_id, [generate_password_hash(c) for c in backup_codes])
    return render_template("totp_backup_codes.html", codes=backup_codes, heading="New backup codes")


@app.route("/settings/test-notify", methods=["POST"])
@login_required
def test_notify():
    ok, err = telegram_notify.send_detailed(g.user_id, "✅ Test notification from your Life Hub!")
    if ok:
        flash("Test message sent!")
    else:
        flash(f"Telegram said: {err}")
    return redirect(url_for("settings"))


@app.route("/settings/export")
@login_required
def export_data():
    import json

    user_id = g.user_id
    tables = [
        "profile", "weight_entries", "sessions", "custom_foods", "food_log",
        "documents", "reminders", "habits", "habit_checkins", "budget_categories",
        "transactions", "recurring_transactions", "savings_goals", "savings_contributions", "settings",
    ]
    # habit_checkins and savings_contributions don't carry user_id directly
    # (they hang off habits/savings_goals), so they're scoped via a subquery
    # against the parent table instead of a plain WHERE user_id = ?.
    scoped_via_parent = {
        "habit_checkins": ("habit_id", "habits"),
        "savings_contributions": ("goal_id", "savings_goals"),
    }

    data = {}
    with db.get_conn() as conn:
        for t in tables:
            if t in scoped_via_parent:
                fk_col, parent = scoped_via_parent[t]
                rows = conn.execute(
                    f"SELECT * FROM {t} WHERE {fk_col} IN (SELECT id FROM {parent} WHERE user_id = ?)",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM {t} WHERE user_id = ?", (user_id,)).fetchall()
            data[t] = [dict(r) for r in rows]

    payload = json.dumps(data, indent=2, default=str)
    filename = f"lifehub_export_{scheduler.today_local(user_id).isoformat()}.json"
    resp = make_response(payload)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


@app.route("/settings/delete-account", methods=["POST"])
@login_required
@limiter.limit("5 per hour", key_func=lambda: f"selfdelete:{g.user_id}")
def delete_own_account():
    """Self-service equivalent of admin_delete_user (which deliberately
    refuses to delete the currently-logged-in admin — see there). Same
    underlying db.admin_delete_user() call, so the two paths can't drift
    apart on what actually gets removed."""
    user = db.get_user_by_id(g.user_id)
    if not db.verify_password(user, request.form.get("password", "")):
        flash("Incorrect password — your account was not deleted.")
        return redirect(url_for("settings"))
    if user["is_admin"] and db.admin_count_admins() <= 1:
        flash("You're the only admin — promote someone else to admin first, or the admin console would be unreachable.")
        return redirect(url_for("settings"))

    user_id = g.user_id
    email = user["email"]
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
            logger.exception("Failed to delete vault file %s for self-deleted user %s", filename, user_id)

    db.admin_log(
        None, email, "self_delete_account",
        target_id=user_id, target_email=email,
        details=f"{len(filenames)} vault file(s) removed" if filenames else None,
    )
    session.clear()
    flash("Your account and all of its data have been deleted.")
    return redirect(url_for("login"))


# ── Admin ────────────────────────────────────────────────────────────────
# Every route below requires is_admin (see admin_required above). The very
# first account ever registered on a deployment gets is_admin=1
# automatically (see db.create_user) — that's the only way in the first
# time, since there's no one else yet to grant it from this UI.

@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template(
        "admin.html",
        stats=db.admin_overview_stats(),
        signups=db.admin_signup_counts(30),
        content_totals=db.admin_content_totals(),
        top_users=db.admin_top_users(10),
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    q = request.args.get("q", "").strip()
    users = db.admin_list_users(q or None)

    # Live-search requests (see admin_users.html) only need the table
    # fragment + updated count, not a full page render.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        count_text = f"{len(users)} account{'' if len(users) == 1 else 's'} shown"
        if q:
            count_text += f' for "{q}"'
        return jsonify(
            table_html=render_template("_admin_users_table.html", users=users),
            count_text=count_text,
        )

    return render_template(
        "admin_users.html",
        users=users,
        q=q,
        admin_count=db.admin_count_admins(),
    )


def _admin_actor():
    """(id, email) for the admin performing the current request — used to
    stamp every audit log entry."""
    return g.user_id, db.get_user_by_id(g.user_id)["email"]


@app.route("/admin/users/<int:user_id>/update", methods=["POST"])
@admin_required
def admin_update_user(user_id):
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not email:
        flash("Email can't be blank.")
        return redirect(url_for("admin_users"))

    target = db.get_user_by_id(user_id)
    if not target:
        flash("That user no longer exists.")
        return redirect(url_for("admin_users"))

    error = db.admin_update_user(user_id, email=email, new_password=password or None)
    if error:
        flash(error)
        return redirect(url_for("admin_users"))

    actor_id, actor_email = _admin_actor()
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
    flash("User updated.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
@admin_required
def admin_toggle_admin(user_id):
    target = db.get_user_by_id(user_id)
    if not target:
        flash("That user no longer exists.")
        return redirect(url_for("admin_users"))

    actor_id, actor_email = _admin_actor()
    if target["is_admin"]:
        # Refuse to demote the last remaining admin — otherwise the whole
        # admin panel becomes permanently unreachable (no one left who can
        # re-grant it), including to the person doing this action.
        if db.admin_count_admins() <= 1:
            flash("Can't remove admin from the last remaining admin.")
            return redirect(url_for("admin_users"))
        db.admin_set_admin(user_id, False)
        db.admin_log(actor_id, actor_email, "demote_admin", target_id=user_id, target_email=target["email"])
        flash(f"{target['email']} is no longer an admin.")
    else:
        db.admin_set_admin(user_id, True)
        db.admin_log(actor_id, actor_email, "promote_admin", target_id=user_id, target_email=target["email"])
        flash(f"{target['email']} is now an admin.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle-suspend", methods=["POST"])
@admin_required
def admin_toggle_suspend(user_id):
    if user_id == g.user_id:
        flash("You can't suspend your own account from here.")
        return redirect(url_for("admin_users"))

    target = db.get_user_by_id(user_id)
    if not target:
        flash("That user no longer exists.")
        return redirect(url_for("admin_users"))
    if target["is_admin"] and not target["disabled_at"] and db.admin_count_admins() <= 1:
        flash("Can't suspend the last remaining admin.")
        return redirect(url_for("admin_users"))

    actor_id, actor_email = _admin_actor()
    reason = request.form.get("reason", "").strip()
    if target["disabled_at"]:
        db.admin_set_disabled(user_id, False)
        db.admin_log(actor_id, actor_email, "unsuspend_user", target_id=user_id, target_email=target["email"])
        flash(f"{target['email']} can log in again.")
    else:
        db.admin_set_disabled(user_id, True)
        db.admin_log(
            actor_id, actor_email, "suspend_user",
            target_id=user_id, target_email=target["email"],
            details=reason or None,
        )
        flash(f"{target['email']} has been suspended.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == g.user_id:
        flash("You can't delete your own account from here — use Settings instead.")
        return redirect(url_for("admin_users"))

    target = db.get_user_by_id(user_id)
    if not target:
        flash("That user no longer exists.")
        return redirect(url_for("admin_users"))
    if target["is_admin"] and db.admin_count_admins() <= 1:
        flash("Can't delete the last remaining admin.")
        return redirect(url_for("admin_users"))

    # Snapshot before the row is gone — admin_log below needs the email,
    # and the actor is looked up here too since g.user_id won't resolve
    # to anything once we log out an admin who deletes themself elsewhere.
    actor_id, actor_email = _admin_actor()
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
            logger.exception("Failed to delete vault file %s for removed user %s", filename, user_id)

    db.admin_log(
        actor_id, actor_email, "delete_user",
        target_id=user_id, target_email=target_email,
        details=f"{len(filenames)} vault file(s) removed" if filenames else None,
    )
    flash(f"Deleted {target_email} and all of their data.")
    return redirect(url_for("admin_users"))


@app.route("/admin/audit-log")
@admin_required
def admin_audit_log():
    q = request.args.get("q", "").strip()
    entries = db.admin_list_audit_log(limit=300, query=q or None)

    # Live-search requests (see admin_audit_log.html) only need the table
    # fragment + updated count, not a full page render.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        count_text = f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} shown"
        if q:
            count_text += f' for "{q}"'
        count_text += " — most recent 300, newest first."
        return jsonify(
            table_html=render_template("_admin_audit_log_table.html", entries=entries, q=q),
            count_text=count_text,
        )

    return render_template(
        "admin_audit_log.html",
        entries=entries,
        q=q,
    )


@app.route("/admin/audit-log/export.csv")
@admin_required
def admin_audit_log_export():
    import csv
    import io

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


if __name__ == "__main__":
    scheduler.start_scheduler()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # Under gunicorn, __main__ isn't executed — start the scheduler here instead.
    scheduler.start_scheduler()
