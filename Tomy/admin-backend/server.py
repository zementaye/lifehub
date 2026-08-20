"""
Tomi Fashion — Admin Backend
-----------------------------
A small Flask API that lets the admin page manage the product catalog.
Products are stored in MongoDB Atlas (free tier) so edits persist and
show up on the live site immediately — no redeploying the frontend.

Setup:
  1. Create a free cluster at https://www.mongodb.com/cloud/atlas/register
     (see README.md in this folder for click-by-click steps).
  2. Grab your connection string and set it as the MONGODB_URI env var.
  3. Pick an admin password and set it as the ADMIN_PASSWORD env var.
  4. Set a random SECRET_KEY env var (any long random string).
  5. pip install -r requirements.txt
  6. Run locally:  python server.py
     Or deploy for free on Render (see README.md).
  7. Point Tomy/products-api.js's API_BASE_URL at wherever this runs.

All product-reading endpoints are public (the storefront needs them).
All product-writing endpoints require the admin token returned by /api/login.
"""

import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import requests
from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, request, jsonify
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("MONGODB_DB_NAME", "tomy_fashion")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")  # tighten in production
TOKEN_MAX_AGE_SECONDS = 60 * 60 * 12  # 12 hour admin session

# Customer-support chat (Gemini). Use a NEW key here, separate from any other
# project's Gemini key — keeps quota/billing isolated per project.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

if not MONGODB_URI:
    print("WARNING: MONGODB_URI is not set. Set it before going live.")
if not ADMIN_PASSWORD:
    print("WARNING: ADMIN_PASSWORD is not set. /api/login will reject everyone.")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY is not set. /api/chat will return a friendly error until it is.")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": FRONTEND_ORIGIN}})


@app.after_request
def add_no_cache_headers(response):
    # This is a pure JSON API that should always reflect what's in Mongo
    # right now — e.g. an admin price edit should show up on the storefront
    # on the very next load. Some browsers/proxies will opportunistically
    # cache a GET with no explicit cache header, which is exactly the kind
    # of "I changed it but the site still shows the old value" bug this
    # heads off.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response

signer = URLSafeTimedSerializer(SECRET_KEY, salt="tomy-admin")

client = MongoClient(MONGODB_URI) if MONGODB_URI else None
db = client[DB_NAME] if client is not None else None
products_col = db["products"] if db is not None else None

VALID_CATEGORIES = {"hoodies", "shirts", "pants", "shoes", "tracksuits", "hats"}
VALID_TYPES = {"simple", "swatch", "slider"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def serialize(doc):
    """Turn a Mongo document into JSON-friendly plain dict."""
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def require_admin():
    """Returns None if the request is authenticated, otherwise an error response."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Missing admin token"}), 401
    token = auth[len("Bearer "):]
    try:
        signer.loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return jsonify({"error": "Session expired, please log in again"}), 401
    except BadSignature:
        return jsonify({"error": "Invalid admin token"}), 401
    return None


def validate_product_payload(data, partial=False):
    """Basic validation. Returns (cleaned_dict, error_message)."""
    cleaned = {}

    if "name" in data or not partial:
        name = (data.get("name") or "").strip()
        if not name:
            return None, "Product name is required"
        cleaned["name"] = name

    if "category" in data or not partial:
        category = (data.get("category") or "").strip().lower()
        if category not in VALID_CATEGORIES:
            return None, f"Category must be one of: {', '.join(sorted(VALID_CATEGORIES))}"
        cleaned["category"] = category

    if "price" in data or not partial:
        try:
            price = float(data.get("price"))
            if price < 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, "Price must be a positive number"
        cleaned["price"] = price

    if "oldPrice" in data:
        old_price = data.get("oldPrice")
        if old_price in ("", None):
            cleaned["oldPrice"] = None
        else:
            try:
                old_price = float(old_price)
                if old_price < 0:
                    raise ValueError
                cleaned["oldPrice"] = old_price
            except (TypeError, ValueError):
                return None, "Old price must be a positive number"

    if "discountPercent" in data:
        # Manual/"fake" discount badge for when the price didn't actually go
        # down (see build note on the /api/products response) — display-only,
        # never affects the real price the customer pays.
        discount_percent = data.get("discountPercent")
        if discount_percent in ("", None):
            cleaned["discountPercent"] = None
        else:
            try:
                discount_percent = float(discount_percent)
                if not (0 < discount_percent < 100):
                    raise ValueError
                cleaned["discountPercent"] = discount_percent
            except (TypeError, ValueError):
                return None, "Discount % must be a number between 1 and 99"

    if "featured" in data:
        cleaned["featured"] = bool(data.get("featured"))

    if "type" in data or not partial:
        p_type = (data.get("type") or "simple").strip().lower()
        if p_type not in VALID_TYPES:
            return None, f"Type must be one of: {', '.join(sorted(VALID_TYPES))}"
        cleaned["type"] = p_type

    if "images" in data or not partial:
        images = data.get("images") or []
        if not isinstance(images, list) or not images:
            return None, "At least one image is required"
        clean_images = []
        for img in images:
            url = (img.get("url") or "").strip() if isinstance(img, dict) else ""
            if not url:
                return None, "Every image needs a URL/path"
            clean_images.append({
                "url": url,
                "color": (img.get("color") or "").strip() or None,
            })
        cleaned["images"] = clean_images

    if "sizes" in data or not partial:
        sizes = data.get("sizes")
        if sizes in (None, ""):
            cleaned["sizes"] = []
        elif not isinstance(sizes, list):
            return None, "Sizes must be a list"
        else:
            clean_sizes = []
            seen = set()
            for row in sizes:
                label = (row.get("size") or "").strip() if isinstance(row, dict) else ""
                if not label:
                    continue
                if label.lower() in seen:
                    return None, f"Duplicate size: {label}"
                seen.add(label.lower())
                try:
                    stock = int(row.get("stock", 0))
                    if stock < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    return None, f"Stock for size '{label}' must be a non-negative whole number"
                clean_sizes.append({"size": label, "stock": stock})
            cleaned["sizes"] = clean_sizes

    return cleaned, None


def db_ready():
    return products_col is not None


# ---------------------------------------------------------------------------
# Customer-support chat (Gemini, grounded in the live product catalog)
# ---------------------------------------------------------------------------

STORE_INFO = """
Tomi Fashion — streetwear boutique, Addis Ababa.
Locations: 22 Town Square Mall 310, Addis Ababa · Bisrate Gebriel Adot ground 21, Addis Ababa
Hours: Open 9AM-9PM daily
WhatsApp / phone orders: +251 9 12 47 22 55
""".strip()

CHAT_SYSTEM_PROMPT_TEMPLATE = """You are the customer support assistant for Tomi Fashion, a streetwear boutique in Addis Ababa, chatting with a shopper on the website.

Store info:
{store_info}

Current catalog (name — category — price — sizes & stock; "one size" means it isn't size-tracked; a size showing "0 in stock" is sold out):
{catalog}

Rules:
- Only talk about products that are actually in the catalog above. Never invent products, prices, or stock numbers.
- If asked whether something is in stock, or in a specific size, answer exactly from the catalog. A size with 0 in stock is sold out — say so plainly. A size not listed for that item isn't offered.
- You cannot place an order yourself. To buy, tell the shopper to add the item to their bag on the site and check out through the WhatsApp order button.
- If you don't know something this data doesn't cover (exact delivery times, returns policy, etc.), say so honestly and point them to WhatsApp: +251 9 12 47 22 55.
- Keep replies short (1-4 sentences) and conversational, like a helpful shop assistant texting back — no headers, no bullet-point walls of text.
"""

_chat_rate_limit = defaultdict(deque)
CHAT_RATE_LIMIT_MAX = 20
CHAT_RATE_LIMIT_WINDOW_SECONDS = 600  # 20 messages per 10 minutes per IP


def check_rate_limit(key):
    now = time.time()
    bucket = _chat_rate_limit[key]
    while bucket and now - bucket[0] > CHAT_RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= CHAT_RATE_LIMIT_MAX:
        return False
    bucket.append(now)
    return True


def build_catalog_context(limit=150):
    if not db_ready():
        return "(catalog unavailable right now)"
    try:
        docs = list(
            products_col.find({}, {"name": 1, "category": 1, "price": 1, "sizes": 1}).limit(limit)
        )
    except PyMongoError:
        return "(catalog unavailable right now)"

    lines = []
    for d in docs:
        name = d.get("name", "Unknown")
        category = d.get("category", "")
        price = d.get("price")
        price_str = f"{int(price):,} Birr" if isinstance(price, (int, float)) else "price n/a"
        sizes = d.get("sizes") or []
        if sizes:
            size_str = ", ".join(f"{s.get('size')}: {s.get('stock', 0)} in stock" for s in sizes)
        else:
            size_str = "one size"
        lines.append(f"- {name} ({category}) — {price_str} — {size_str}")
    return "\n".join(lines) if lines else "(no products in the catalog yet)"


@app.route("/api/chat", methods=["POST"])
def chat():
    if not GEMINI_API_KEY:
        return jsonify({"error": "Chat isn't set up yet — ask the store to configure it."}), 503

    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not check_rate_limit(client_ip):
        return jsonify({"error": "You're sending messages a little fast — give it a moment and try again."}), 429

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []

    if not message:
        return jsonify({"error": "Message is required"}), 400
    if len(message) > 1000:
        return jsonify({"error": "That message is a bit long — try shortening it."}), 400
    if not isinstance(history, list) or len(history) > 30:
        return jsonify({"error": "Invalid conversation history"}), 400

    system_prompt = CHAT_SYSTEM_PROMPT_TEMPLATE.format(
        store_info=STORE_INFO, catalog=build_catalog_context()
    )

    contents = []
    for turn in history[-12:]:
        role = turn.get("role") if isinstance(turn, dict) else None
        text = (turn.get("text") or "").strip() if isinstance(turn, dict) else ""
        if role not in ("user", "model") or not text:
            continue
        contents.append({"role": role, "parts": [{"text": text[:1000]}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
    }

    try:
        resp = requests.post(GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.RequestException:
        return jsonify({"error": "The assistant is temporarily unavailable — please try again shortly."}), 502

    candidates = result.get("candidates") or []
    if not candidates:
        return jsonify({"error": "The assistant didn't return a response — please try again."}), 502
    parts = candidates[0].get("content", {}).get("parts", [])
    reply = "".join(p.get("text", "") for p in parts).strip()
    if not reply:
        return jsonify({"error": "The assistant didn't return a response — please try again."}), 502

    return jsonify({"reply": reply})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "db_connected": db_ready(),
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if not ADMIN_PASSWORD:
        return jsonify({"error": "Server has no ADMIN_PASSWORD configured"}), 500
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Incorrect password"}), 401
    token = signer.dumps({"role": "admin"})
    return jsonify({"token": token, "expiresInSeconds": TOKEN_MAX_AGE_SECONDS})


@app.route("/api/products", methods=["GET"])
def list_products():
    if not db_ready():
        return jsonify({"error": "Database not configured"}), 503

    query = {}
    category = request.args.get("category")
    if category:
        query["category"] = category.lower()
    if request.args.get("featured") == "true":
        query["featured"] = True

    try:
        docs = list(products_col.find(query).sort("_id", -1))
    except PyMongoError:
        return jsonify({"error": "Database error"}), 503

    return jsonify([serialize(d) for d in docs])


@app.route("/api/products/<product_id>", methods=["GET"])
def get_product(product_id):
    if not db_ready():
        return jsonify({"error": "Database not configured"}), 503
    try:
        doc = products_col.find_one({"_id": ObjectId(product_id)})
    except InvalidId:
        return jsonify({"error": "Invalid product id"}), 400
    if not doc:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(serialize(doc))


@app.route("/api/products", methods=["POST"])
def create_product():
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not db_ready():
        return jsonify({"error": "Database not configured"}), 503

    data = request.get_json(silent=True) or {}
    cleaned, err = validate_product_payload(data, partial=False)
    if err:
        return jsonify({"error": err}), 400

    cleaned.setdefault("oldPrice", None)
    cleaned.setdefault("discountPercent", None)
    cleaned.setdefault("featured", False)
    cleaned.setdefault("sizes", [])
    cleaned["createdAt"] = datetime.now(timezone.utc)

    result = products_col.insert_one(cleaned)
    doc = products_col.find_one({"_id": result.inserted_id})
    return jsonify(serialize(doc)), 201


@app.route("/api/products/<product_id>", methods=["PUT"])
def update_product(product_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not db_ready():
        return jsonify({"error": "Database not configured"}), 503

    try:
        oid = ObjectId(product_id)
    except InvalidId:
        return jsonify({"error": "Invalid product id"}), 400

    data = request.get_json(silent=True) or {}
    cleaned, err = validate_product_payload(data, partial=True)
    if err:
        return jsonify({"error": err}), 400
    if not cleaned:
        return jsonify({"error": "No valid fields to update"}), 400

    cleaned["updatedAt"] = datetime.now(timezone.utc)
    result = products_col.update_one({"_id": oid}, {"$set": cleaned})
    if result.matched_count == 0:
        return jsonify({"error": "Product not found"}), 404

    doc = products_col.find_one({"_id": oid})
    return jsonify(serialize(doc))


@app.route("/api/products/<product_id>", methods=["DELETE"])
def delete_product(product_id):
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not db_ready():
        return jsonify({"error": "Database not configured"}), 503

    try:
        oid = ObjectId(product_id)
    except InvalidId:
        return jsonify({"error": "Invalid product id"}), 400

    result = products_col.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "Product not found"}), 404
    return jsonify({"deleted": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
