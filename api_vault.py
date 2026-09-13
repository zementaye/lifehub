"""JSON API for the vault (documents) — the ninth slice of the Vercel
frontend migration, and the first one needing real file upload/download
handling rather than plain JSON in/out. Mirrors app.py's vault()/
vault_upload()/vault_renew()/vault_acknowledge()/vault_download()/
vault_delete() routes.

Upload: multipart/form-data (same as the HTML form always used) rather
than JSON — a file can't go in a JSON body. The frontend sends a
FormData POST with the Authorization header still attached (fetch()
supports auth headers alongside FormData bodies fine; just don't set
Content-Type manually, so the browser can set the multipart boundary).

Download: handled differently depending on config.USE_B2, same branch
app.py's vault_download() already has:
- USE_B2 → return the presigned URL as JSON ({"url": ...}); the URL
  itself is time-limited and self-authenticating, so the frontend can
  just navigate to it directly with no Authorization header needed.
- local disk (dev/no-B2 fallback) → stream the file bytes back through
  this API instead of redirecting, since a bare redirect would land the
  browser on a URL that (unlike session-cookie auth) has no way to prove
  who's asking. The frontend fetches this with its Authorization header
  attached, then downloads the resulting blob client-side.

Same duplication note as the other api_*.py files: `ALLOWED_EXT` is
copied from app.py rather than imported (circular — app.py imports this
file before its own module-level constants are defined further down).
"""

import logging
import uuid
from datetime import date

from flask import Blueprint, jsonify, request, send_from_directory

import config
import db
import scheduler
import storage
from api_auth import get_authenticated_user

bp = Blueprint("api_vault", __name__, url_prefix="/api/vault")

logger = logging.getLogger(__name__)

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "heic", "pdf"}


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _augment(doc_row, today):
    entry = dict(doc_row)
    if doc_row["expiry_date"]:
        try:
            entry["days_left"] = (date.fromisoformat(doc_row["expiry_date"]) - today).days
        except ValueError:
            entry["days_left"] = None
    else:
        entry["days_left"] = None
    entry["acknowledged"] = (
        doc_row["expiry_ack_date"] == doc_row["expiry_date"] and doc_row["expiry_date"] is not None
    )
    return entry


@bp.get("")
def list_documents():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY label", (user_id,)
        ).fetchall()

    today = scheduler.today_local(user_id)
    return jsonify({"documents": [_augment(d, today) for d in rows]}), 200


@bp.post("/upload")
def upload_document():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    label = (request.form.get("label") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    expiry_date = (request.form.get("expiry_date") or "").strip() or None
    file = request.files.get("file")

    if not label or not file or file.filename == "":
        return jsonify({"error": "Label and file are required."}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXT:
        return jsonify({"error": "Unsupported file type."}), 400

    filename = f"{uuid.uuid4().hex}.{ext}"

    if config.USE_B2:
        try:
            storage.upload_fileobj(file, filename, content_type=file.mimetype)
        except Exception:
            logger.exception("B2 upload failed for %s", filename)
            return jsonify({"error": "Upload failed — couldn't reach file storage. Please try again."}), 502
    else:
        logger.warning(
            "B2 not configured — saving %s to local disk, which Render wipes on "
            "every restart/redeploy.", filename,
        )
        file.save(config.UPLOAD_DIR / filename)

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO documents (user_id, label, filename, notes, expiry_date, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, label, filename, notes, expiry_date, db.now()),
        )
        new_row = conn.execute("SELECT * FROM documents WHERE id = ?", (cur.lastrowid,)).fetchone()

    return jsonify({"document": _augment(new_row, scheduler.today_local(user_id))}), 201


@bp.post("/<int:doc_id>/renew")
def renew_document(doc_id):
    user, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    new_expiry = (data.get("expiry_date") or "").strip() or None
    with db.get_conn() as conn:
        # Renewing to a new date naturally resets the reminder cycle, since
        # expiry_ack_date is cleared and no longer matches the old expiry.
        conn.execute(
            "UPDATE documents SET expiry_date = ?, expiry_ack_date = NULL WHERE id = ? AND user_id = ?",
            (new_expiry, doc_id, user["id"]),
        )
    return jsonify({"ok": True}), 200


@bp.post("/<int:doc_id>/acknowledge")
def acknowledge_document(doc_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
        ).fetchone()
        if not doc:
            return jsonify({"error": "Not found."}), 404
        conn.execute(
            "UPDATE documents SET expiry_ack_date = ? WHERE id = ?", (doc["expiry_date"], doc_id)
        )
    return jsonify({"ok": True}), 200


@bp.get("/<int:doc_id>/download")
def download_document(doc_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
        ).fetchone()
    if not doc:
        return jsonify({"error": "Not found."}), 404

    ext = doc["filename"].rsplit(".", 1)[-1] if "." in doc["filename"] else ""
    safe_label = "".join(c for c in doc["label"] if c.isalnum() or c in " -_").strip() or "document"
    download_name = f"{safe_label}.{ext}" if ext else safe_label

    if config.USE_B2:
        return jsonify({"url": storage.presigned_url(doc["filename"], download_name=download_name)}), 200

    # Local-disk fallback: no presigned URL to hand back, so stream the
    # bytes through this (already-authenticated) request instead of
    # redirecting to a URL with no auth of its own.
    return send_from_directory(
        config.UPLOAD_DIR, doc["filename"], as_attachment=True, download_name=download_name
    )


@bp.post("/<int:doc_id>/delete")
def delete_document(doc_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
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
    return jsonify({"ok": True}), 200
