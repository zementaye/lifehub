"""JSON API for notes — the eleventh slice of the Vercel frontend
migration. Mirrors app.py's notes()/add_note()/edit_note()/
add_note_images()/delete_note_image()/add_note_voice()/delete_note_voice()/
delete_note() routes.

Scope note — two things deliberately NOT carried over from app.py's
notes() route:
1. Sharing (share_requests: inviting another user by email to specific
   dates, accept/decline/revoke). That's a genuinely separate feature
   (multi-user collaboration, not just this account's own data) with its
   own routes (`/notes/share`, `/share/<id>/...`) — left for a follow-up
   once the frontend needs it. `list_outgoing_shares`/`list_incoming_shares`
   etc. in db.py are untouched.
2. Bulk-delete (`/notes/bulk-delete`) and AI voice transcription
   (mentioned in app.py's comments as already happening client-side via
   a separate `/api/notes/transcribe` endpoint elsewhere) — transcription
   is an AI-features-slice concern, not a notes-CRUD one.

File handling here follows the same pattern api_vault.py established:
uploads are multipart/form-data (a file can't go in a JSON body), and
file downloads/views branch on config.USE_B2 — a presigned URL as JSON
when B2/R2-backed storage is configured, otherwise the bytes are
streamed back through this (already-authenticated) API rather than
redirecting to a URL with no auth of its own.

Same duplication note as the other api_*.py files: `NOTE_IMAGE_EXT`,
`NOTE_VOICE_EXT`, `NOTE_RECURRENCES`, and the `_save_note_images`/
`_save_note_voice`/`_delete_note_image_files`/`_delete_note_voice_files`
helpers are copied from app.py rather than imported (circular — app.py
imports this file before its own module-level code is defined further
down).
"""

import logging
import threading
import uuid
from datetime import date

from flask import Blueprint, jsonify, request, send_from_directory

import config
import db
import storage
from api_auth import get_authenticated_user

bp = Blueprint("api_notes", __name__, url_prefix="/api/notes")

logger = logging.getLogger(__name__)

NOTE_IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "heic"}
NOTE_VOICE_EXT = {"webm", "mp4", "m4a", "ogg", "mp3", "wav"}
NOTE_RECURRENCES = {"once", "weekly", "monthly", "yearly"}


def _require_user():
    user = get_authenticated_user()
    if not user:
        return None, (jsonify({"error": "Not authenticated."}), 401)
    return user, None


def _delete_note_image_files(filenames):
    for filename in filenames:
        try:
            if config.USE_B2:
                storage.delete_file(filename)
            else:
                path = config.UPLOAD_DIR / filename
                if path.exists():
                    path.unlink()
        except Exception:
            logger.exception("Failed to delete note image %s", filename)


def _save_note_images(note_id, user_id, files):
    saved = 0
    skipped = 0
    for file in files:
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in NOTE_IMAGE_EXT:
            skipped += 1
            continue

        filename = f"{uuid.uuid4().hex}.{ext}"
        if config.USE_B2:
            try:
                storage.upload_fileobj(file, filename, content_type=file.mimetype)
            except Exception:
                logger.exception("B2 upload failed for note image %s", filename)
                skipped += 1
                continue
        else:
            file.save(config.UPLOAD_DIR / filename)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO note_images (note_id, user_id, filename, created_at) VALUES (?,?,?,?)",
                (note_id, user_id, filename, db.now()),
            )
        saved += 1
    return saved, skipped


def _delete_note_voice_files(filenames):
    for filename in filenames:
        try:
            if config.USE_B2:
                storage.delete_file(filename)
            else:
                path = config.UPLOAD_DIR / filename
                if path.exists():
                    path.unlink()
        except Exception:
            logger.exception("Failed to delete voice note %s", filename)


def _save_note_voice(note_id, user_id, files):
    saved = 0
    skipped = 0
    for file in files:
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in NOTE_VOICE_EXT:
            skipped += 1
            continue

        filename = f"{uuid.uuid4().hex}.{ext}"
        if config.USE_B2:
            try:
                storage.upload_fileobj(file, filename, content_type=file.mimetype)
            except Exception:
                logger.exception("B2 upload failed for voice note %s", filename)
                skipped += 1
                continue
        else:
            file.save(config.UPLOAD_DIR / filename)

        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO note_voice (note_id, user_id, filename, created_at) VALUES (?,?,?,?)",
                (note_id, user_id, filename, db.now()),
            )
        saved += 1
    return saved, skipped


@bp.get("")
def list_notes():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM notes WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
        image_rows = conn.execute(
            "SELECT * FROM note_images WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()
        voice_rows = conn.execute(
            "SELECT * FROM note_voice WHERE user_id = ? ORDER BY created_at", (user_id,)
        ).fetchall()

    images_by_note = {}
    for img in image_rows:
        images_by_note.setdefault(img["note_id"], []).append(dict(img))
    voice_by_note = {}
    for v in voice_rows:
        voice_by_note.setdefault(v["note_id"], []).append(dict(v))

    notes_out = []
    for n in items:
        entry = dict(n)
        entry["images"] = images_by_note.get(n["id"], [])
        entry["voice"] = voice_by_note.get(n["id"], [])
        notes_out.append(entry)

    return jsonify({"notes": notes_out}), 200


@bp.post("")
def add_note():
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    linked_date = (request.form.get("linked_date") or "").strip() or None
    if linked_date:
        try:
            date.fromisoformat(linked_date)
        except ValueError:
            linked_date = None
    recurrence = request.form.get("recurrence", "once")
    if recurrence not in NOTE_RECURRENCES or not linked_date:
        recurrence = "once"

    if not title:
        return jsonify({"error": "Title is required."}), 400

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes (user_id, title, body, linked_date, recurrence, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, title, body, linked_date, recurrence, db.now(), db.now()),
        )
        note_id = cur.lastrowid
        new_row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    images = [f for f in request.files.getlist("images") if f and f.filename]
    voice_clips = [f for f in request.files.getlist("voice") if f and f.filename]
    if images:
        _save_note_images(note_id, user_id, images)
    if voice_clips:
        _save_note_voice(note_id, user_id, voice_clips)

    entry = dict(new_row)
    with db.get_conn() as conn:
        entry["images"] = [dict(r) for r in conn.execute(
            "SELECT * FROM note_images WHERE note_id = ?", (note_id,)
        ).fetchall()]
        entry["voice"] = [dict(r) for r in conn.execute(
            "SELECT * FROM note_voice WHERE note_id = ?", (note_id,)
        ).fetchall()]
    return jsonify({"note": entry}), 201


@bp.post("/<int:note_id>/edit")
def edit_note(note_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT linked_date FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Note not found."}), 404

        if existing["linked_date"]:
            recurrence = request.form.get("recurrence", "once")
            if recurrence not in NOTE_RECURRENCES:
                recurrence = "once"
            conn.execute(
                "UPDATE notes SET title=?, body=?, recurrence=?, updated_at=? WHERE id=? AND user_id=?",
                (title, body, recurrence, db.now(), note_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE notes SET title=?, body=?, updated_at=? WHERE id=? AND user_id=?",
                (title, body, db.now(), note_id, user_id),
            )

    voice_clips = [f for f in request.files.getlist("voice") if f and f.filename]
    if voice_clips:
        _save_note_voice(note_id, user_id, voice_clips)

    with db.get_conn() as conn:
        updated = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        entry = dict(updated)
        entry["images"] = [dict(r) for r in conn.execute(
            "SELECT * FROM note_images WHERE note_id = ?", (note_id,)
        ).fetchall()]
        entry["voice"] = [dict(r) for r in conn.execute(
            "SELECT * FROM note_voice WHERE note_id = ?", (note_id,)
        ).fetchall()]
    return jsonify({"note": entry}), 200


@bp.post("/<int:note_id>/images")
def add_note_images(note_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        owned = conn.execute(
            "SELECT 1 FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        ).fetchone()
    if not owned:
        return jsonify({"error": "Note not found."}), 404

    images = [f for f in request.files.getlist("images") if f and f.filename]
    if not images:
        return jsonify({"error": "Choose at least one photo to add."}), 400

    saved, skipped = _save_note_images(note_id, user_id, images)
    with db.get_conn() as conn:
        new_images = [dict(r) for r in conn.execute(
            "SELECT * FROM note_images WHERE note_id = ?", (note_id,)
        ).fetchall()]
    return jsonify({"images": new_images, "saved": saved, "skipped": skipped}), 200


@bp.get("/image/<int:image_id>/download")
def download_note_image(image_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        img = conn.execute(
            "SELECT * FROM note_images WHERE id = ? AND user_id = ?", (image_id, user["id"])
        ).fetchone()
    if not img:
        return jsonify({"error": "Not found."}), 404
    if config.USE_B2:
        return jsonify({"url": storage.presigned_url(img["filename"])}), 200
    return send_from_directory(config.UPLOAD_DIR, img["filename"])


@bp.post("/image/<int:image_id>/delete")
def delete_note_image(image_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        img = conn.execute(
            "SELECT * FROM note_images WHERE id = ? AND user_id = ?", (image_id, user["id"])
        ).fetchone()
        if img:
            conn.execute("DELETE FROM note_images WHERE id = ?", (image_id,))
    if img:
        _delete_note_image_files([img["filename"]])
    return jsonify({"ok": True}), 200


@bp.post("/<int:note_id>/voice")
def add_note_voice(note_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        owned = conn.execute(
            "SELECT 1 FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)
        ).fetchone()
    if not owned:
        return jsonify({"error": "Note not found."}), 404

    clips = [f for f in request.files.getlist("voice") if f and f.filename]
    if not clips:
        return jsonify({"error": "Record or choose an audio clip to add."}), 400

    saved, skipped = _save_note_voice(note_id, user_id, clips)
    with db.get_conn() as conn:
        new_voice = [dict(r) for r in conn.execute(
            "SELECT * FROM note_voice WHERE note_id = ?", (note_id,)
        ).fetchall()]
    return jsonify({"voice": new_voice, "saved": saved, "skipped": skipped}), 200


@bp.get("/voice/<int:voice_id>/download")
def download_note_voice(voice_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        v = conn.execute(
            "SELECT * FROM note_voice WHERE id = ? AND user_id = ?", (voice_id, user["id"])
        ).fetchone()
    if not v:
        return jsonify({"error": "Not found."}), 404
    if config.USE_B2:
        return jsonify({"url": storage.presigned_url(v["filename"])}), 200
    return send_from_directory(config.UPLOAD_DIR, v["filename"])


@bp.post("/voice/<int:voice_id>/delete")
def delete_note_voice(voice_id):
    user, err = _require_user()
    if err:
        return err
    with db.get_conn() as conn:
        v = conn.execute(
            "SELECT * FROM note_voice WHERE id = ? AND user_id = ?", (voice_id, user["id"])
        ).fetchone()
        if v:
            conn.execute("DELETE FROM note_voice WHERE id = ?", (voice_id,))
    if v:
        _delete_note_voice_files([v["filename"]])
    return jsonify({"ok": True}), 200


@bp.post("/<int:note_id>/delete")
def delete_note(note_id):
    user, err = _require_user()
    if err:
        return err
    user_id = user["id"]

    with db.get_conn() as conn:
        image_rows = conn.execute(
            "SELECT filename FROM note_images WHERE note_id = ? AND user_id = ?", (note_id, user_id)
        ).fetchall()
        voice_rows = conn.execute(
            "SELECT filename FROM note_voice WHERE note_id = ? AND user_id = ?", (note_id, user_id)
        ).fetchall()
        conn.execute("DELETE FROM note_images WHERE note_id = ? AND user_id = ?", (note_id, user_id))
        conn.execute("DELETE FROM note_voice WHERE note_id = ? AND user_id = ?", (note_id, user_id))
        conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
    db.remove_note_from_shares(note_id)

    image_names = [r["filename"] for r in image_rows]
    voice_names = [r["filename"] for r in voice_rows]
    if image_names or voice_names:
        threading.Thread(
            target=lambda: (_delete_note_image_files(image_names), _delete_note_voice_files(voice_names)),
            daemon=True,
        ).start()
    return jsonify({"ok": True}), 200
