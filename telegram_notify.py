import logging

import requests

import config
import db

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _credentials(user_id: int):
    token = db.get_setting(user_id, "tg_bot_token", config.TG_BOT_TOKEN)
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
    message didn't go through."""
    token, chat_id = _credentials(user_id)
    if not token or not chat_id:
        return False, "Telegram bot token / chat ID not configured."

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

    try:
        return False, resp.json().get("description", resp.text)
    except ValueError:
        return False, resp.text
