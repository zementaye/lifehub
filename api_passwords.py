"""JSON API for the password manager — the twelfth slice of the Vercel
frontend migration. Mirrors app.py's passwords()/add_password()/
edit_password()/delete_password() routes exactly — small, self-contained
feature, nothing deferred this slice.

Same trust boundary as the HTML version: the list endpoint returns
decrypted passwords in the response (crypto.decrypt(row["password_enc"])),
same as the old page rendered them straight into the HTML. Nothing new
here security-wise — it's the same authenticated user reading their own
stored data either way, just as JSON instead of a rendered table.
"""

from flask import Blueprint, jsonify, request

import crypto
import db
from api_auth import get_authenticated_user

bp = Blueprint("api_passwords", __name__, url_prefix="/api/passwords")


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


@bp.get("")
def list_passwords():
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM passwords WHERE user_id = ? ORDER BY label", (user["id"],)
        ).fetchall()
    items = []
    for r in rows:
        entry = dict(r)
        entry["password"] = crypto.decrypt(r["password_enc"])
        del entry["password_enc"]
        items.append(entry)
    return jsonify({"items": items}), 200


@bp.post("")
def add_password():
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    url = (data.get("url") or "").strip()
    notes = (data.get("notes") or "").strip()

    if not label or not password:
        return jsonify({"error": "Label and password are required."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO passwords (user_id, label, username, password_enc, url, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user["id"], label, username, crypto.encrypt(password), url, notes, db.now()),
        )
        new_row = conn.execute("SELECT * FROM passwords WHERE id = ?", (cur.lastrowid,)).fetchone()

    entry = dict(new_row)
    entry["password"] = password
    del entry["password_enc"]
    return jsonify({"item": entry}), 201


@bp.post("/<int:pw_id>/edit")
def edit_password(pw_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""  # blank = keep existing password
    url = (data.get("url") or "").strip()
    notes = (data.get("notes") or "").strip()

    if not label:
        return jsonify({"error": "Label is required."}), 400

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
        updated = conn.execute(
            "SELECT * FROM passwords WHERE id = ? AND user_id = ?", (pw_id, user_id)
        ).fetchone()

    if not updated:
        return jsonify({"error": "Not found."}), 404

    entry = dict(updated)
    entry["password"] = crypto.decrypt(updated["password_enc"])
    del entry["password_enc"]
    return jsonify({"item": entry}), 200


@bp.post("/<int:pw_id>/delete")
def delete_password(pw_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        conn.execute("DELETE FROM passwords WHERE id = ? AND user_id = ?", (pw_id, user["id"]))
    return jsonify({"ok": True}), 200
