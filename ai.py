"""AI features: a chat assistant over the user's own data, natural-language
quick-add (turn a typed sentence into a transaction/food-log/reminder/todo),
and auto-categorization of transactions.

Built on Google's Gemini API — picked over Anthropic/OpenAI for having an
actually-free tier (no credit card, no expiring trial credit) that's
generous enough for a single small personal app. See config.py for
GEMINI_API_KEY / GEMINI_MODEL.

Every public function here is best-effort and never raises: a missing key,
a network hiccup, a rate limit, or a malformed model response all come back
as (None, "some human-readable reason") rather than a stack trace. Nothing
in the app depends on AI actually working — adding a transaction, logging
food, setting a reminder etc. all still work by hand exactly as before
regardless of whether any of this is configured or currently reachable.
"""

import json
import logging

import requests

import config

logger = logging.getLogger(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def _call(system_prompt: str, user_prompt: str, want_json: bool = False, temperature: float = 0.2):
    """Low-level call to the Gemini API. Returns (result, error_message) —
    result is a parsed dict/list when want_json is True, else plain text.
    Exactly one of the two is ever non-None."""
    if not config.GEMINI_API_KEY:
        return None, "AI isn't configured on this server."

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if want_json:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    url = GEMINI_API.format(model=config.GEMINI_MODEL)
    try:
        resp = requests.post(url, params={"key": config.GEMINI_API_KEY}, json=payload, timeout=20)
    except requests.RequestException as e:
        logger.warning("Gemini request failed: %s", e)
        return None, "Couldn't reach the AI service — try again in a moment."

    if resp.status_code == 429:
        return None, "AI is rate-limited right now (free tier) — try again shortly."
    if resp.status_code == 401 or resp.status_code == 403:
        logger.error("Gemini API rejected the configured key (HTTP %s)", resp.status_code)
        return None, "AI isn't working right now — ask an admin to check the setup."
    if resp.status_code >= 400:
        logger.warning("Gemini API error %s: %s", resp.status_code, resp.text[:300])
        return None, "The AI service returned an error."

    try:
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # Most commonly a safety-filter block rather than an outage —
            # surface something the person can act on instead of a blank
            # failure that looks like the feature is just broken.
            reason = data.get("promptFeedback", {}).get("blockReason")
            return None, f"The AI declined to respond ({reason})." if reason else "The AI didn't return a response."
        text = candidates[0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError):
        logger.warning("Unexpected Gemini response shape: %s", resp.text[:300])
        return None, "Got an unexpected response from the AI service."

    if not want_json:
        return text, None

    try:
        return json.loads(text), None
    except ValueError:
        logger.warning("Gemini didn't return valid JSON: %s", text[:300])
        return None, "The AI's response wasn't in the expected format."


# ── Chat assistant ──────────────────────────────────────────────────────
# Answers questions using a plain-text digest the caller builds from the
# user's own data (see _build_chat_context in app.py) — never given direct
# DB/tool access, so it can't read or change anything beyond what's in that
# digest. Vault/passwords/notes are deliberately left out of that digest by
# the caller; this module has no opinion on that, it just answers from
# whatever text it's handed.

def ask(question: str, context: str):
    system = (
        "You are the assistant built into Life Hub, a personal budget, "
        "health, and habit-tracking app. Answer the user's question using "
        "ONLY the data summary below — never invent numbers, dates, or "
        "items that aren't in it. If the summary doesn't contain what's "
        "needed to answer, say so plainly rather than guessing. Keep "
        "answers short and conversational, like a quick reply, not a "
        "report with headers.\n\n"
        f"=== Their data ===\n{context}"
    )
    return _call(system, question, want_json=False, temperature=0.3)


# ── Natural-language quick add ──────────────────────────────────────────
# Turns one typed sentence into exactly one structured action. Deliberately
# narrow: it only ever proposes inserting a row shaped like what the normal
# manual forms already produce (see add_transaction/log_food/add_reminder/
# add_todo in app.py) — the caller re-validates every field before writing
# anything, so a bad or hallucinated response can't put garbage in the DB,
# only fail to add anything.

_QUICK_ADD_SYSTEM = """\
Turn the user's short note into ONE structured action for a personal budget/
health/habit app. Reply with ONLY a single JSON object, no prose, no markdown
fences, matching exactly one of these shapes:

Expense or income:
{{"type": "transaction", "txn_type": "expense" or "income", "amount": <number>, "description": <short string>, "category": <one of the existing categories below, or null>, "date": "YYYY-MM-DD" or null}}

Something eaten:
{{"type": "food", "name": <short string>, "meal": "breakfast"|"lunch"|"dinner"|"snack", "grams": <number, your best estimate of a typical portion if not stated>, "calories": <number>, "protein_g": <number>, "carbs_g": <number>, "fat_g": <number>, "fiber_g": <number>, "date": "YYYY-MM-DD" or null}}
(Estimate reasonable nutrition values for the food and portion described.)

A reminder for a future date:
{{"type": "reminder", "title": <short string>, "next_due": "YYYY-MM-DD", "recurrence": "once"|"daily"|"weekly"|"monthly"}}

A simple task with no date:
{{"type": "todo", "title": <short string>}}

If the note is too vague to confidently produce one of the above, reply:
{{"type": "unclear", "reason": <short string explaining what's missing>}}

Today's date is {today}. Existing expense categories for this user: {categories}.
Only use a category from that exact list (case-sensitive match), or null if
none clearly fits — never invent a new category name.
"""


def parse_quick_add(text: str, categories: list[str], today: str):
    system = _QUICK_ADD_SYSTEM.format(
        today=today,
        categories=", ".join(categories) if categories else "(none yet)",
    )
    return _call(system, text, want_json=True, temperature=0.0)


# ── Auto-categorization ──────────────────────────────────────────────────
# Used when a transaction is added without an explicit category (see
# add_transaction in app.py) — best-effort only, and only ever picks from
# the user's own existing category names so the result always maps to a
# real category_id or is discarded.

def suggest_category(description: str, categories: list[str]):
    if not description or not categories:
        return None, "Nothing to categorize."
    system = (
        "Given a single transaction description, reply with ONLY the single "
        "best-matching category name from this exact list (case-sensitive, "
        "no other text): " + ", ".join(categories) + ". "
        "If none fit reasonably, reply with exactly: none"
    )
    text, err = _call(system, description, want_json=False, temperature=0.0)
    if err:
        return None, err
    guess = (text or "").strip()
    if guess.lower() == "none" or guess not in categories:
        return None, None
    return guess, None
