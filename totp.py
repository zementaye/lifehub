"""TOTP two-factor authentication — RFC 6238 (built on RFC 4226 HOTP).

Deliberately hand-rolled instead of pulling in pyotp: the algorithm is
short, stable (hasn't changed since 2011), and entirely stdlib (hmac,
hashlib, base64, struct), which fits this project's existing "as few
dependencies as actually needed" style (see crypto.py, db.py's raw sqlite
rather than an ORM, etc). Verified below against the official RFC 4226
Appendix D test vectors.

Secrets are handled as base32 strings (the standard representation for
manual entry into an authenticator app, and what an otpauth:// URI
expects) everywhere outside this module. At rest, the caller is
responsible for encrypting it — see crypto.py and its use in app.py's
2FA routes; this module has no opinion on storage.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse


def generate_secret() -> str:
    """A fresh random base32 secret, suitable for a new enrollment."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    # Base32 requires padding to a multiple of 8 chars; secrets are
    # generated/stored without it (cleaner for manual entry), so pad back
    # out before decoding.
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


def now_totp(secret_b32: str, digits: int = 6, interval: int = 30, for_time: float | None = None) -> str:
    counter = int((for_time if for_time is not None else time.time()) // interval)
    return _hotp(secret_b32, counter, digits)


def verify_totp(
    secret_b32: str,
    code: str,
    digits: int = 6,
    interval: int = 30,
    valid_window: int = 1,
    for_time: float | None = None,
) -> bool:
    """True if `code` matches the current 30s window or is within
    `valid_window` steps either side (clock drift / the person being a
    little slow to type it in). Uses a constant-time comparison so timing
    can't be used to narrow down the correct code digit by digit."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits:
        return False
    now = for_time if for_time is not None else time.time()
    counter = int(now // interval)
    for offset in range(-valid_window, valid_window + 1):
        expected = _hotp(secret_b32, counter + offset, digits)
        if hmac.compare_digest(expected, code):
            return True
    return False


def provisioning_uri(secret_b32: str, account_email: str, issuer: str = "Life Hub") -> str:
    """otpauth:// URI — most authenticator apps can accept this pasted in
    directly as an alternative to scanning a QR code (this app doesn't
    render a QR image; see the 2FA setup route/template for why)."""
    label = urllib.parse.quote(f"{issuer}:{account_email}")
    params = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1", "digits": "6", "period": "30"})
    return f"otpauth://totp/{label}?{params}"


def generate_backup_codes(count: int = 10) -> list[str]:
    """One-time-use fallback codes for when the person doesn't have their
    authenticator app (lost phone, etc). Format: xxxx-xxxx, easy to read
    and type by hand. Caller is responsible for hashing before storage —
    see db.set_totp_backup_codes — and for only showing the plaintext
    once, at generation time."""
    codes = []
    for _ in range(count):
        raw = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
        codes.append(raw)
    return codes
