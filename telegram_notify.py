import logging

import requests

import config
import db

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _credentials(user_id: int):
    # The bot itself is always the operator's shared bot (config.TG_BOT_TOKEN)
    # — not something an individual user can point at their own bot. Only
    # the chat ID (who the bot DMs) is per-user.
    token = config.TG_BOT_TOKEN
    chat_id = db.get_setting(user_id, "tg_chat_id", config.TG_CHAT_ID)
    return token, chat_id


def send(user_id: int, message: str) -> bool:
    """Fire-and-forget notification for a given user. Returns True/False and
    swallows errors (logging them) so a Telegram outage never breaks a
    reminder/habit-checkin flow that's calling this."""
    ok, err = send_detailed(user_id, message)
    if not ok:
        logger.warning("Telegram send failed for user_id=%s: %s", user_id, err)
    return ok


def send_detailed(user_id: int, message: str):
    """Same as send(), but returns (ok, error_message) so callers like the
    Settings 'Send test notification' button can show the real reason a
    message didn't go through.

    The bot token is a fixed server-side value now (see _credentials) — a
    user reading this error can't do anything about *that* half, so a
    missing/bad token is reported as a server problem ("ask an admin"),
    never phrased as something for them to fix. Only chat-ID problems,
    which are actually theirs to correct, get surfaced as such.
    """
    token, chat_id = _credentials(user_id)
    if not token:
        return False, "Notifications aren't set up on this server yet — ask an admin to configure the bot."
    if not chat_id:
        return False, "Add your chat ID in Settings first."

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException as e:
        return False, str(e)

    if resp.status_code == 200 and resp.json().get("ok"):
        return True, None

    # 401/404 here mean the *token* is wrong/revoked — a server-side
    # misconfiguration, not anything the user's chat ID could cause. Report
    # it as such rather than showing Telegram's raw "Unauthorized", which
    # reads like it's about something the user entered.
    if resp.status_code in (401, 404):
        logger.error(
            "Telegram API rejected the configured bot token (HTTP %s) for user_id=%s",
            resp.status_code, user_id,
        )
        return False, "Notifications aren't working right now — ask an admin to check the bot setup."

    try:
        return False, resp.json().get("description", resp.text)
    except ValueError:
        return False, resp.text
