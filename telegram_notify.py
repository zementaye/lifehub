"""Sends notifications via any Telegram bot you own — set it up on the Settings
page (or via TG_BOT_TOKEN/TG_CHAT_ID env vars as the initial default). No
dependency on python-telegram-bot needed, this just hits the HTTP Bot API."""

import logging

import requests

import config
import db

logger = logging.getLogger(__name__)


def get_credentials() -> tuple[str, str]:
    token = db.get_setting("tg_bot_token", config.TG_BOT_TOKEN).strip()
    chat_id = db.get_setting("tg_chat_id", config.TG_CHAT_ID).strip()
    return token, chat_id


def send(text: str) -> bool:
    ok, _err = send_detailed(text)
    return ok


def send_detailed(text: str) -> tuple[bool, str]:
    """Same as send(), but also returns Telegram's actual error message
    (e.g. 'Unauthorized' for a bad token, 'chat not found' for a bad chat
    ID or a bot you haven't started a conversation with) instead of just
    True/False, so failures are actually diagnosable."""
    token, chat_id = get_credentials()
    if not token or not chat_id:
        return False, "Bot token or chat ID isn't set."
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            try:
                err = resp.json().get("description", resp.text)
            except ValueError:
                err = resp.text
            logger.error("Telegram notify failed: %s", err)
            return False, err
        return True, ""
    except requests.RequestException as e:
        logger.exception("Telegram notify request failed")
        return False, str(e)
