"""Sends notifications via any Telegram bot you own — set it up on the Settings
page (or via TG_BOT_TOKEN/TG_CHAT_ID env vars as the initial default). No
dependency on python-telegram-bot needed, this just hits the HTTP Bot API."""

import logging

import requests

import config
import db

logger = logging.getLogger(__name__)


def get_credentials() -> tuple[str, str]:
    token = db.get_setting("tg_bot_token", config.TG_BOT_TOKEN)
    chat_id = db.get_setting("tg_chat_id", config.TG_CHAT_ID)
    return token, chat_id


def send(text: str) -> bool:
    token, chat_id = get_credentials()
    if not token or not chat_id:
        logger.warning("Telegram bot not configured — skipping notification: %s", text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            logger.error("Telegram notify failed: %s", resp.text)
            return False
        return True
    except requests.RequestException:
        logger.exception("Telegram notify request failed")
        return False
