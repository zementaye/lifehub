"""Symmetric encryption for stored passwords, using the Fernet recipe
(AES-128-CBC + HMAC) from the `cryptography` package.

The Fernet key is derived from FLASK_SECRET_KEY so there's nothing extra to
configure — consistent with the rest of this app's "zero friction by
default" philosophy. IMPORTANT: if FLASK_SECRET_KEY ever changes, every
stored password becomes undecryptable, since the key derived from it
changes too. Treat FLASK_SECRET_KEY as sensitive and stable once you start
storing real passwords — back it up somewhere safe.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

import config


def _get_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Most likely cause: FLASK_SECRET_KEY changed since this was saved.
        return "⚠️ Could not decrypt — was FLASK_SECRET_KEY changed?"
