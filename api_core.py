"""Small shared /api endpoints that aren't auth-specific. Currently just
the CSRF token endpoint: the existing CSRFProtect(app) setup in app.py
protects every POST/PUT/PATCH/DELETE app-wide (including the new /api/*
routes), and normally gets its token from a hidden form field rendered
into HTML — which a decoupled frontend doesn't have. Flask-WTF also
accepts the token via an X-CSRFToken (or X-CSRF-Token) request header by
default, so the frontend fetches it once from here on load and attaches
it to every state-changing request's headers instead.
"""

from flask import Blueprint, jsonify
from flask_wtf.csrf import generate_csrf

bp = Blueprint("api_core", __name__, url_prefix="/api")


@bp.get("/csrf-token")
def csrf_token():
    return jsonify({"csrf_token": generate_csrf()}), 200
