"""
Western Chat — Western Parts Order Processing Interface
coresteensma.com/westernchat

Core Loop: Identify → Commit  (contact/shipping collected via form, not chat)
"""
from flask import Flask, request, jsonify, render_template, session, send_from_directory, abort
from flask_session import Session
from flask_cors import CORS
import os
import logging
import hashlib
import json
import re
import sqlite3
import time
import threading
from collections import defaultdict
import boto3
from botocore.exceptions import ClientError
from pathlib import Path
from datetime import datetime
from urllib.parse import quote as urlquote
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app = Flask(__name__, static_url_path="/westernchat/static", static_folder="static")
CORS(app, resources={r"/westernchat/*": {"origins": "https://coresteensma.com"}})

# ── Secret key ────────────────────────────────────────────────────────────────
_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret or len(_secret) < 32:
    raise RuntimeError(
        "FLASK_SECRET_KEY is required (≥ 32 chars). "
        "Generate: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )
app.config["SECRET_KEY"] = _secret

# ── Server-side session ───────────────────────────────────────────────────────
_session_dir = Path(__file__).parent / "instance" / "flask_session"
_session_dir.mkdir(parents=True, exist_ok=True)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = str(_session_dir)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)

# ── App logger ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("westernchat")

# ── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL         = os.getenv("WESTERNCHAT_MODEL", "gpt-4o")
ESCALATION_TO        = os.getenv("ESCALATION_EMAIL", "jeffd@steensmalawn.com")
SES_FROM             = os.getenv("SES_FROM", "jeffd@steensmalawn.com")  # must be SES-verified
SES_REGION           = os.getenv("SES_REGION", "us-east-1")
PAYMENT_PORTAL_BASE  = os.getenv("PAYMENT_PORTAL_URL", "https://steensmalawn.com/checkout")
CONFIDENCE_THRESHOLD = 0.85
AUTO_ESCALATION_ENABLED = os.getenv("AUTO_ESCALATION_ENABLED", "false").lower() == "true"
MAX_HISTORY_TURNS    = 20

# ── Orders SQLite database ────────────────────────────────────────────────────
ORDERS_DB = Path("/var/www/westernchat/instance/orders.db")
ORDERS_DB.parent.mkdir(parents=True, exist_ok=True)

def _init_orders_db() -> None:
    with sqlite3.connect(ORDERS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                email       TEXT    NOT NULL,
                phone       TEXT    NOT NULL DEFAULT '',
                address     TEXT    NOT NULL DEFAULT '',
                city        TEXT    NOT NULL DEFAULT '',
                state       TEXT    NOT NULL DEFAULT '',
                zip         TEXT    NOT NULL DEFAULT '',
                cart_json   TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'new',
                created_at  TEXT    NOT NULL
            )
        """)
        conn.commit()

_init_orders_db()

# ── Interaction log (append-only JSONL) ───────────────────────────────────────
LOG_DIR  = Path("/var/log/westernchat")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "interactions.jsonl"

def _log_interaction(payload: dict) -> None:
    """Append one JSON record per line to the interaction log."""
    try:
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as e:
        logger.warning("Could not write interaction log: %s", e)

# ── OpenAI ────────────────────────────────────────────────────────────────────
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is required.")
openai_client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("OpenAI initialized — model: %s", OPENAI_MODEL)

# ── Knowledge dir (read-only share from westernai) ────────────────────────────
KNOWLEDGE_DIR = Path("/var/www/westernai/knowledge")

# ── Document index — PDFs served at /westernchat/docs/<filename> ─────────────────
ALLOWED_DOC_EXTS = {".pdf"}
_DOC_LABEL_MAP = {
    "01 - 3 Port UT Mount Offering PS 2011.pdf":            "3-Port UT Mount Offering",
    "02 - 3 Port Suburbanite Mount Offering PS 2011.pdf":   "3-Port Suburbanite Mount",
    "03a - Conv Kits PS 2011.pdf":                          "Conversion Kits Reference",
    "03b - Relay_4-port reference PS 2011.pdf":             "Relay / 4-Port Reference",
    "03c - 4-port_3-port reference PS 2011.pdf":            "4-Port / 3-Port Wiring Reference",
    "04 - Cutting Edge  Back Drag Guidelines - w Bolt Bag PS 2011.pdf": "Cutting Edge & Back Drag Guidelines",
    "05 - A-Frame Quadrant Reference Sheet PS 2011.pdf":    "A-Frame Quadrant Reference",
    "06 - Hydraulic Ram Cross Reference Sheet.pdf":         "Hydraulic RAM Cross Reference",
    "07 - History of Whole Good Parts With Dash Numbers PS 2011.pdf": "Whole Good Parts / Dash Numbers History",
    "Electrical Schematics Guide Isolation Module and Relay Systems.pdf": "Electrical Schematics — Isolation Module & Relay",
    "FORD ELECTRICAL PROBLEM ANSWER 20200130074552931 (1).pdf": "Ford Electrical Problem Guide",
    "Hydraulic Hose Comparison.pdf":                        "Hydraulic Hose Comparison",
    "ISARMATIC.pdf":                                        "ISARMATIC Reference",
    "LED Cheat Sheet -112119.pdf":                          "LED Cheat Sheet",
    "MVP PLUS Mechanics Guide.pdf":                         "MVP PLUS Mechanics Guide",
    "MVP w Isolation Module System  Mechanics Guide.pdf":   "MVP w/ Isolation Module Mechanics Guide",
    "Straight Blade with Relay System. Mechanic's Guide.pdf": "Straight Blade Relay System Mechanics Guide",
    "WESTERN O-RING CHART.pdf":                             "Western O-Ring Chart",
    "WESTERN TOOLS.pdf":                                    "Western Tools Reference",
    "WIDE OUT Mechanics Guide.pdf":                         "Wide-Out Mechanics Guide",
    "Western Plow Transfer Guide_23.pdf":                   "Western Plow Transfer Guide (2023)",
    "Western Reference Guide.pdf":                         "Western Reference Guide",
    "Western Reference Manual.pdf":                        "Western Reference Manual",
    "Western Shipping Error Shortage Claim Form Master Copy 11-9-18.pdf": "Shipping Error / Shortage Claim Form",
    # Parts Posters — exploded-view drawings with full item→part# callouts
    "parts_posters/Wideout.pdf":                           "WIDE-OUT Parts Poster",
    "parts_posters/MVPPlus.pdf":                           "MVP PLUS Parts Poster",
    "parts_posters/MVP3MVPPlusMVP.pdf":                    "MVP 3 / MVP PLUS / MVP Parts Poster",
    "parts_posters/ProPlowSeries2.pdf":                    "PRO-PLOW Series 2 Parts Poster",
    "parts_posters/ProPlus.pdf":                           "PRO-PLUS Parts Poster",
    "parts_posters/Suburbanite.pdf":                       "Suburbanite Parts Poster",
    "parts_posters/HTS.pdf":                               "HTS Parts Poster",
    "parts_posters/HTS_Ultramount.pdf":                    "HTS UltraMount Parts Poster",
    "parts_posters/Midweight.pdf":                         "MIDWEIGHT Parts Poster",
    "parts_posters/Defender.pdf":                          "DEFENDER Parts Poster",
    "parts_posters/Enforcer.pdf":                          "ENFORCER Parts Poster",
    "parts_posters/Prodigy.pdf":                           "PRODIGY Parts Poster",
    "parts_posters/ProdigyWideoutUltramountUltramount2.pdf": "PRODIGY / WIDE-OUT / UltraMount 2 Parts Poster",
    "parts_posters/ImpactUTV.pdf":                         "IMPACT UTV Parts Poster",
    "parts_posters/UltraMountHydraulics.pdf":              "UltraMount Hydraulics Parts Poster",
    "parts_posters/UlramountHydraulics_Straight.pdf":      "UltraMount Hydraulics - Straight Blade Parts Poster",
    "parts_posters/UltramountWideoutMVP.pdf":              "UltraMount / WIDE-OUT / MVP Parts Poster",
    "parts_posters/UltramountWideoutMVPMVP3.pdf":          "UltraMount / WIDE-OUT / MVP / MVP 3 Parts Poster",
    "parts_posters/Unimount.pdf":                          "UniMount Parts Poster",
    "parts_posters/UnimountHydraulics.pdf":                "UniMount Hydraulics Parts Poster",
    "parts_posters/PileDriverandSkid.pdf":                 "Pile Driver & Skid Steer Parts Poster",
    "parts_posters/SnowPlowElectrical.pdf":                "Snowplow Electrical Parts Poster",
    "parts_posters/StrikerHopperSpreader.pdf":             "Striker Hopper Spreader Parts Poster",
    "parts_posters/TornadoPolyHopperSpreader.pdf":         "Tornado Poly Hopper Spreader Parts Poster",
}

def _build_doc_index() -> list[dict]:
    """Return list of {label, filename, url} for every available PDF."""
    docs = []
    for fname, label in _DOC_LABEL_MAP.items():
        fpath = KNOWLEDGE_DIR / fname
        if fpath.exists():
            docs.append({
                "label":    label,
                "filename": fname,
                "url":      "/westernchat/docs/" + urlquote(fname),
            })
    return docs

DOC_INDEX = _build_doc_index()
logger.info("Document index: %d PDFs available", len(DOC_INDEX))

# ── Knowledge context — per-request keyword filter ───────────────────────────
# Small files: loaded fully at startup (these fit in context without truncation)
_SMALL_FILES = [
    "cutting_edge_catalog.csv",
    "transfer_kits.csv",
    "western_terminology_glossary.csv",
    "WESTERN_PARTS_TERMINOLOGY.md",
    "western_guide.txt",
]
# Large CSVs: searched per-request — only matching rows injected into prompt
_LARGE_CSV_FILES = [
    "western_parts_posters.csv",
    "western_parts_extracted.csv",
    "western_parts_master_catalog.csv",
]

def _load_small_files() -> str:
    snippets = []
    for fname in _SMALL_FILES:
        fpath = KNOWLEDGE_DIR / fname
        if fpath.exists():
            try:
                snippets.append(f"=== {fname} ===\n{fpath.read_text(errors='replace')}")
            except Exception as e:
                logger.warning("Could not read %s: %s", fname, e)
    return "\n\n".join(snippets)

_static_knowledge = _load_small_files()
logger.info("Static knowledge loaded — %d chars", len(_static_knowledge))


_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "are", "was",
    "have", "has", "not", "but", "what", "all", "were", "can", "your",
    "need", "want", "get", "its", "into", "our", "out", "use", "new",
    # brand/domain words that appear in almost every row — useless as filters
    "western", "steensma", "plow", "snow", "parts", "part",
}


def _build_knowledge_context(query: str) -> str:
    """Return knowledge relevant to this query.
    Small files: full text (pre-loaded at startup).
    Large CSVs: rows that match ≥ 2 meaningful keywords from the query."""
    raw_keywords = set(w.lower() for w in re.split(r'\W+', query) if len(w) >= 3)
    keywords = raw_keywords - _STOP_WORDS
    if not keywords:
        keywords = raw_keywords  # fall back if everything was a stop word

    snippets = [_static_knowledge]
    for fname in _LARGE_CSV_FILES:
        fpath = KNOWLEDGE_DIR / fname
        if not fpath.exists():
            continue
        try:
            lines = fpath.read_text(errors='replace').splitlines()
            if not lines:
                continue
            header = lines[0]
            matching = []
            for line in lines[1:]:
                line_lower = line.lower()
                hits = sum(1 for kw in keywords if kw in line_lower)
                if hits >= 2:
                    matching.append(line)
            if matching:
                snippets.append(
                    f"=== {fname} ({len(matching)} rows matched) ===\n"
                    + header + "\n"
                    + "\n".join(matching[:300])
                )
                logger.info("%s: %d rows matched for query", fname, len(matching))
        except Exception as e:
            logger.warning("Could not search %s: %s", fname, e)
    return "\n\n".join(snippets)

# ── Document list for system prompt ──────────────────────────────────────────
_doc_list_for_prompt = "\n".join(
    f'  - "{d["label"]}": {d["url"]}' for d in DOC_INDEX
)

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT_BASE = f"""You are the primary digital interface for Western Parts (Steensma Lawn & Power), \
acting as an expert technical consultant for parts identification and order processing. \
Your objective is to identify the correct part, secure the order commitment, and facilitate the transaction.

OPERATIONAL PARAMETERS

Core Loop:
1. IDENTIFY — Analyze the request. Ask questions to reach ≥ 85% confidence. Reference the knowledge base.
2. COMMIT   — Present the complete parts list to the customer for confirmation. Wait for YES.
             Once the customer confirms, you are DONE. The customer will submit their contact &
             shipping info separately via a form — you do NOT need to ask for name, email, or phone.
             After customer says YES, respond with a brief confirmation message and set phase=COMMIT.

Phase Transition Rules (STRICT — phases only move forward, NEVER backward):
- IDENTIFY → COMMIT: Only when confidence ≥ 85%. Present the full parts_list clearly.
- COMMIT (final): The moment the customer says YES or any clear confirmation, respond with something
  like "Perfect — your order is confirmed. Click Submit Order in the cart to complete your request."
  Do NOT ask for contact info. Do NOT re-identify parts. Stay in COMMIT phase.
- You CANNOT return to IDENTIFY once you have advanced to COMMIT.

Logic & Flow:
- Maintain strict If-Then logic for all interactions.
- One-offs (e.g., plow transfers, special orders) are handled as distinct subroutines.
- Payment: Never process payments in this chat. When ready to transact, populate payment_link.
- Multi-item orders: use parts_list for all line items. part_identified holds the primary/single item only.

Multi-Item Cart Rules:
- A customer may order multiple different parts in one session (e.g., cutting edges + solenoids + dust pan).
- CRITICAL — parts_list vs. options: parts_list must ONLY contain items the customer has explicitly
  selected or confirmed. When you are presenting multiple choices for the customer to pick from,
  parts_list must be EMPTY (or contain only items already confirmed in a prior turn). NEVER
  pre-populate parts_list with options the customer has not yet chosen. parts_list grows
  one confirmed item at a time.
- After the customer explicitly chooses an item (by part number, description, or position like
  "the first one"), confirm it with full detail, add ONLY that item to parts_list, and ask:
  "Got it — what else do you need, or is that everything?"
- When a customer references a part number from earlier in the conversation (e.g., "Two of 44285-2"),
  look up its description and pricing from the conversation history — never ask for clarification
  on a part you already identified and named in the same session.
- CRITICAL — Immediate confirmation rule: If you just presented a specific part (description +
  part number + price) in the immediately preceding message, and the customer replies with any
  affirmative ("yes", "that one", "I'll take it", "order it", "correct", "that's right", "perfect",
  "add it", or similar), treat that as confirmation of the part you just presented.
  DO NOT ask "which part?" or "what's the part number?" or "can you specify the model?" —
  you already have all the information. Immediately add that exact part to parts_list.
- CRITICAL — No re-identification after YES: Once a customer confirms "yes" or "the previous one"
  after you have named a specific part, never ask them to re-specify. You have the part in
  conversation history — use it.
- Stay in IDENTIFY phase while collecting additional items.
- Always return the COMPLETE accumulated parts_list on every response — every previously confirmed item
  PLUS any new item just confirmed. Check CURRENT CART in your context and always include those items.
- If a customer says "never mind" or "remove" for a specific line item, drop only that item from parts_list.
- Advance to COMMIT only when the customer explicitly says they are done adding items.
- At COMMIT: present the full parts_list for final confirmation. Do not solicit more items.
- After customer says YES at COMMIT: confirm briefly and remind them to click Submit Order. Do NOT ask for contact details.

Accuracy & Escalation:
- Target ≥ 85% confidence before moving to COMMIT. Never guess on part numbers.
- If confidence is below 85%, DO NOT escalate. Ask clarifying questions —
  request a photo description, model number, serial tag, or symptom details to raise confidence.
  A good question or a shared diagram can take confidence from 70% to 100%.
- Only set escalate=true when you have exhausted reasonable questions and still cannot reach 85%.
- BEFORE escalating: always ask for the customer's name and email first so the shop can follow up.
  Say something like: "I want to make sure a technician can reach you — what is your name and email?"
  Then set escalate=true in the NEXT response after capturing at least an email.
  Never send an escalation with email = empty string unless the customer explicitly refuses to provide it.

Document References:
- When a plow model is identified (e.g., MVP 3, WIDE-OUT, PRO-PLOW Series 2), immediately include
  the corresponding Parts Poster PDF in the docs[] array. This gives the customer the exploded diagram
  so they can visually confirm the exact part they need. A model+drawing together is far more
  accurate than guessing from description alone.
- When a user asks about wiring, harnesses, or electrical components, also include SnowPlowElectrical.pdf.
- When a user asks about a diagram, wiring schematic, parts guide, or any visual reference,
  populate the "docs" array with the relevant document(s) from the list below.
- Always offer documents proactively when relevant.

Parts Poster → Model mapping (use these exact URLs for docs[]):
  MVP 3 → "MVP 3 / MVP PLUS / MVP Parts Poster"
  MVP PLUS → "MVP PLUS Parts Poster"
  WIDE-OUT / Wide-Out / Wide-Out XL → "WIDE-OUT Parts Poster"
  PRO-PLOW Series 2 / Pro Plow → "PRO-PLOW Series 2 Parts Poster"
  PRO-PLUS → "PRO-PLUS Parts Poster"
  Suburbanite → "Suburbanite Parts Poster"
  HTS → "HTS Parts Poster"
  MIDWEIGHT → "MIDWEIGHT Parts Poster"
  DEFENDER → "DEFENDER Parts Poster"
  PRODIGY → "PRODIGY Parts Poster"
  ENFORCER → "ENFORCER Parts Poster"
  IMPACT UTV → "IMPACT UTV Parts Poster"
  (Match label text from AVAILABLE DOCUMENTS list below to get the correct URL)

AVAILABLE DOCUMENTS:
{_doc_list_for_prompt}

Response Format — ALWAYS return a valid JSON object (no markdown fences, no prose outside JSON):
{{
  "message": "<your reply — plain text only, no HTML>",
  "phase": "IDENTIFY | COMMIT | ESCALATE",
  "confidence": <float 0.0–1.0>,
  "docs": [
    {{"title": "<document label>", "url": "<document url from list above>"}}
  ],
  "parts_list": [
    {{
      "description": "<part description>",
      "part_number": "<part number or empty string>",
      "quantity": <int>,
      "unit_price": "<price string or empty string>"
    }}
  ],
  "part_identified": {{
    "description": "<primary part description>",
    "part_number": "<part number or empty string>",
    "quantity": <int or null>,
    "unit_price": "<price string or empty string>"
  }} | null,
  "send_order_summary": false,
  "payment_link": "<pre-populated checkout URL or empty string>",
  "escalate": <true | false>,
  "escalation_reason": "<reason string or empty string>"
}}

Style: Professional, direct, concise. Zero filler. Technical accuracy first.

STARTUP — On first interaction respond exactly:
{{"message":"Welcome to Western Parts — Steensma Lawn & Power. I can help you identify the right component and get your order moving.\\n\\nWhat are you working with today?\\n\\nPlow lines: Straight Blade · Pro Plow · MVP V-Plow · Wide-Out · Wide-Out XL\\nMounts: UltraMount · UltraMount 2 · vehicle-specific receiver mounts\\nComponents: hydraulics · cutting edges · wiring harnesses · controllers · lift frames","phase":"IDENTIFY","confidence":1.0,"docs":[],"parts_list":[],"part_identified":null,"send_order_summary":false,"payment_link":"","escalate":false,"escalation_reason":""}}

KNOWLEDGE BASE — see dynamic section appended per request.
"""


def _build_system_prompt(query: str, cart: list | None = None) -> str:
    """Return the full system prompt with cart state and knowledge context filtered for this query."""
    if cart:
        lines = [
            f"  {i+1}. {it.get('description', '?')}  "
            f"| Part#: {it.get('part_number', 'TBD')}  "
            f"| Qty: {it.get('quantity', 1)}  "
            f"| Price: {it.get('unit_price') or 'Contact for pricing'}"
            for i, it in enumerate(cart)
        ]
        cart_section = (
            "\n\nCURRENT CART — " + str(len(cart)) + " item(s) already confirmed:\n"
            + "\n".join(lines)
            + "\n(Always include ALL of these in parts_list on every response.)"
        )
    else:
        cart_section = "\n\nCURRENT CART: empty — no items confirmed yet."
    return SYSTEM_PROMPT_BASE + cart_section + "\nKNOWLEDGE BASE:\n" + _build_knowledge_context(query)

# ── Per-user state (conversation history + cart) ───────────────────────────────
MAX_HISTORY_USERS = 200
user_state: dict[str, dict] = {}
# Each entry: {"history": [...messages...], "cart": [...parts...]}


def _user_key() -> str:
    email = (request.headers.get("X-Auth-Email") or request.headers.get("X-Forwarded-Email"))
    user  = (request.headers.get("X-Auth-User")  or request.headers.get("X-Forwarded-User"))
    ident = email or user
    if ident:
        return "u:" + hashlib.sha256(ident.lower().strip().encode()).hexdigest()[:16]
    if "sid" not in session:
        session["sid"] = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
    return "s:" + session["sid"]


def _get_user_state(key: str) -> dict:
    if key not in user_state:
        user_state[key] = {"history": [], "cart": []}
    return user_state[key]


def _get_history(key: str) -> list:
    return _get_user_state(key)["history"]


def _get_cart(key: str) -> list:
    return _get_user_state(key)["cart"]


def _trim_history(history: list) -> None:
    while len(history) > MAX_HISTORY_TURNS:
        history.pop(0)


def _evict_if_needed() -> None:
    if len(user_state) > MAX_HISTORY_USERS:
        del user_state[next(iter(user_state))]


def _reset_user(key: str) -> None:
    """Wipe all state for this user and rotate their session SID for a clean slate."""
    user_state.pop(key, None)
    session["sid"] = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
    for k in list(session.keys()):
        if k.startswith("order_sent_"):
            session.pop(k, None)


# ── AWS SES escalation (uses EC2 IAM role — no credentials stored) ──────────────
def _send_escalation_email(user_key: str, history: list, ai_json: dict) -> None:
    """Send escalation alert via AWS SES using the instance IAM role.
    Zero credentials in config — the EC2 role grants SES:SendEmail automatically.
    SES_FROM must be a verified SES identity (verification email sent on deploy).
    """
    try:
        part    = ai_json.get("part_identified") or {}
        contact = ai_json.get("contact_data") or {}
        body_lines = [
            "Western Chat — escalation triggered",
            "",
            f"Session:    {user_key}",
            f"Time:       {datetime.utcnow().isoformat()}Z",
            f"Reason:     {ai_json.get('escalation_reason', 'Threshold breach')}",
            f"Confidence: {ai_json.get('confidence', 'N/A')}",
            "",
            "Customer contact:",
            f"  Name:   {contact.get('name', 'Not captured')}",
            f"  Email:  {contact.get('email', 'Not captured')}",
            f"  Phone:  {contact.get('phone', 'Not captured')}",
            "",
            "Part identified so far:",
            f"  Part:   {part.get('description', 'N/A')}  [{part.get('part_number', '')}]",
            "",
            "Last 8 turns:",
        ]
        for m in history[-8:]:
            body_lines.append(f"  [{m.get('role','?').upper()}] {str(m.get('content',''))[:400]}")

        ses = boto3.client("sesv2", region_name=SES_REGION)
        ses.send_email(
            FromEmailAddress=SES_FROM,
            Destination={"ToAddresses": [ESCALATION_TO]},
            Content={
                "Simple": {
                    "Subject": {"Data": "⚠ Western Chat Escalation", "Charset": "UTF-8"},
                    "Body":    {"Text": {"Data": "\n".join(body_lines), "Charset": "UTF-8"}},
                }
            },
        )
        logger.info("Escalation email sent via SES → %s", ESCALATION_TO)
    except ClientError as e:
        logger.error("SES send failed: %s", e.response["Error"]["Message"])
    except Exception as e:
        logger.error("Escalation email failed: %s", e)


# ── Order summary email (SES) ─────────────────────────────────────────────────
def _send_order_summary(user_key: str, parts_list: list, part_identified: dict | None,
                        contact: dict) -> None:
    """Email the compiled parts list + shipping info to Western team and customer."""
    try:
        name    = contact.get("name",    "Customer")
        email   = contact.get("email",   "")
        phone   = contact.get("phone",   "")
        address = contact.get("address", "")
        city    = contact.get("city",    "")
        state   = contact.get("state",   "")
        zip_    = contact.get("zip",     "")

        # Build shipping line
        addr_parts = [p for p in [address, city, state, zip_] if p]
        shipping_line = ", ".join(addr_parts) if addr_parts else "Not provided"

        items = parts_list or []
        if not items and part_identified:
            items = [part_identified]

        item_lines = []
        for i, item in enumerate(items, 1):
            desc  = item.get("description", "—")
            pn    = item.get("part_number", "") or "TBD"
            qty   = item.get("quantity", 1) or 1
            price = item.get("unit_price", "") or "Contact for pricing"
            item_lines.append(f"  {i}. {desc}")
            item_lines.append(f"     Part #: {pn}   Qty: {qty}   Price: {price}")

        jeff_body = "\n".join([
            "Western Chat — New Parts Order",
            "=" * 40,
            f"Customer:  {name}",
            f"Email:     {email}",
            f"Phone:     {phone}",
            f"Ship To:   {shipping_line}",
            f"Session:   {user_key}",
            f"Time:      {datetime.utcnow().isoformat()}Z",
            "",
            "Parts List:",
            *item_lines,
            "",
            "— Sent automatically by Western Chat",
        ])

        customer_body = "\n".join([
            f"Hi {name},",
            "",
            "Here is your Western Parts order summary from Steensma Lawn & Power.",
            "Our team will follow up to confirm availability and finalize pricing.",
            "",
            f"Ship To: {shipping_line}",
            "",
            "Order Summary:",
            *item_lines,
            "",
            "Questions? Call us or reply to this email.",
            "— Steensma Lawn & Power, Western Parts",
        ])

        ses = boto3.client("sesv2", region_name=SES_REGION)

        # Always notify the Western team
        ses.send_email(
            FromEmailAddress=SES_FROM,
            ReplyToAddresses=["jeffd@steensmalawn.com"],
            Destination={"ToAddresses": [ESCALATION_TO]},
            Content={"Simple": {
                "Subject": {"Data": f"Western Chat Order — {name}", "Charset": "UTF-8"},
                "Body":    {"Text": {"Data": jeff_body, "Charset": "UTF-8"}},
            }},
        )
        logger.info("Order summary sent to Western team (%s)", ESCALATION_TO)

        # Confirmation copy to customer
        if email and "@" in email:
            ses.send_email(
                FromEmailAddress=SES_FROM,
                ReplyToAddresses=["jeffd@steensmalawn.com"],
                Destination={"ToAddresses": [email]},
                Content={"Simple": {
                    "Subject": {"Data": "Your Western Parts Order — Steensma Lawn & Power",
                                "Charset": "UTF-8"},
                    "Body":    {"Text": {"Data": customer_body, "Charset": "UTF-8"}},
                }},
            )
            logger.info("Order confirmation sent to customer (%s)", email)

    except ClientError as e:
        logger.error("SES order summary failed: %s", e.response["Error"]["Message"])
    except Exception as e:
        logger.error("Order summary email failed: %s", e)


# ── Payment link ──────────────────────────────────────────────────────────────
def _build_payment_link(part: dict | None, contact: dict | None, parts_list: list | None = None) -> str:
    items = parts_list or []
    if not items and part:
        items = [part]
    if not items:
        return ""
    from urllib.parse import urlencode
    try:
        cart_payload = json.dumps([
            {
                "description": i.get("description", ""),
                "part_number": i.get("part_number", ""),
                "quantity": i.get("quantity", 1) or 1,
                "unit_price": i.get("unit_price", ""),
            }
            for i in items
        ], separators=(",", ":"))
    except Exception:
        cart_payload = "[]"

    primary = items[0]
    params = {
        "part":  primary.get("part_number", ""),
        "desc":  primary.get("description", ""),
        "qty":   str(primary.get("quantity") or 1),
        "cart":  cart_payload,
        "email": (contact or {}).get("email", ""),
        "phone": (contact or {}).get("phone", ""),
        "src":   "westernchat",
    }
    return PAYMENT_PORTAL_BASE + "?" + urlencode({k: v for k, v in params.items() if v})


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/westernchat")
@app.route("/westernchat/")
def index():
    return render_template("index.html", doc_index=DOC_INDEX)


@app.route("/westernchat/docs/<path:filename>")
def serve_doc(filename: str):
    """Serve a PDF from the westernai knowledge directory.
    Only allows files in the explicit doc index — no path traversal possible.
    """
    # Validate: filename must be in our curated index
    allowed = {d["filename"] for d in DOC_INDEX}
    if filename not in allowed:
        abort(404)
    # send_from_directory resolves symlinks and refuses to go above the root
    return send_from_directory(str(KNOWLEDGE_DIR), filename, as_attachment=False)


# ── Simple in-process rate limiter ──────────────────────────────────────────
_rate_lock    = threading.Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT   = 20   # max requests
_RATE_WINDOW  = 60   # per N seconds

def _check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate limit exceeded."""
    now = time.monotonic()
    with _rate_lock:
        timestamps = _rate_buckets[ip]
        # Drop timestamps outside the window
        _rate_buckets[ip] = [t for t in timestamps if now - t < _RATE_WINDOW]
        if len(_rate_buckets[ip]) >= _RATE_LIMIT:
            return False
        _rate_buckets[ip].append(now)
        return True


@app.route("/westernchat/chat", methods=["POST"])
def chat():
    client_ip = request.headers.get("X-Real-IP") or request.remote_addr or "unknown"
    if not _check_rate_limit(client_ip):
        return jsonify({"error": "Too many requests — please slow down."}), 429
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    user_message = data.get("message", "").strip()
    if not user_message or len(user_message) > 2000:
        return jsonify({"error": "Message missing or too long (max 2000 chars)"}), 400

    _evict_if_needed()
    key     = _user_key()
    history = _get_history(key)
    cart    = _get_cart(key)
    is_first = len(history) == 0

    if is_first:
        history.append({"role": "user", "content": "__INIT__"})

    history.append({"role": "user", "content": user_message})
    _trim_history(history)

    # Build message list
    messages = [{"role": "system", "content": _build_system_prompt(user_message, cart)}]
    if is_first:
        messages.append({"role": "assistant", "content": (
            '{"message":"Welcome to Western Parts \u2014 Steensma Lawn & Power. '
            'I can help you identify the right component and get your order moving.'
            '\\n\\nWhat are you working with today?\\n\\n'
            'Plow lines: Straight Blade \u00b7 Pro Plow \u00b7 MVP V-Plow \u00b7 Wide-Out \u00b7 Wide-Out XL\\n'
            'Mounts: UltraMount \u00b7 UltraMount 2 \u00b7 vehicle-specific receiver mounts\\n'
            'Components: hydraulics \u00b7 cutting edges \u00b7 wiring harnesses \u00b7 controllers \u00b7 lift frames",'
            '"phase":"IDENTIFY","confidence":1.0,'
            '"docs":[],"parts_list":[],"part_identified":null,"contact_data":null,'
            '"send_order_summary":false,"payment_link":"",'
            '"escalate":false,"escalation_reason":""}'
        )})
    messages += [m for m in history if m["content"] != "__INIT__"]

    # Call OpenAI
    try:
        completion = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        raw = completion.choices[0].message.content or "{}"
    except Exception as e:
        logger.error("OpenAI error: %s", e)
        return jsonify({"error": "AI service error. Please try again."}), 502

    # Parse
    try:
        ai = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("AI returned non-JSON: %s", raw[:300])
        return jsonify({"error": "Unexpected AI response format."}), 502

    history.append({"role": "assistant", "content": raw})
    _trim_history(history)

    # Merge AI parts_list into server-side cart (only when AI returns items — never wipe on empty)
    ai_parts = ai.get("parts_list") or []
    if ai_parts:
        _get_user_state(key)["cart"] = list(ai_parts)
        cart = _get_user_state(key)["cart"]

    # Confidence / escalation check
    # NOTE: low confidence alone does NOT trigger escalation — the AI is expected to
    # keep asking questions to raise it. Only escalate when the AI explicitly sets
    # escalate=true after exhausting clarifying options.
    confidence = float(ai.get("confidence", 1.0))

    if AUTO_ESCALATION_ENABLED and ai.get("escalate"):
        _send_escalation_email(key, history, ai)

    # Validate doc URLs — only allow URLs from our known index
    allowed_urls = {d["url"] for d in DOC_INDEX}
    safe_docs = [
        d for d in (ai.get("docs") or [])
        if isinstance(d, dict) and d.get("url") in allowed_urls
    ]

    # Log interaction
    _log_interaction({
        "ts":         datetime.utcnow().isoformat() + "Z",
        "session":    key,
        "user_msg":   user_message,
        "phase":      ai.get("phase"),
        "confidence": confidence,
        "escalate":   ai.get("escalate", False),
        "parts_list": cart,
        "part":       ai.get("part_identified"),
        "docs":       safe_docs,
    })

    return jsonify({
        "message":           ai.get("message", ""),
        "phase":             ai.get("phase", "IDENTIFY"),
        "confidence":        confidence,
        "docs":              safe_docs,
        "parts_list":        ai.get("parts_list") or [],
        "part":              ai.get("part_identified"),
        "escalate":          ai.get("escalate", False),
        "escalation_reason": ai.get("escalation_reason", ""),
        "cart":              cart,
    })


@app.route("/westernchat/reset", methods=["POST"])
def reset_chat():
    """Wipe server-side state for this user. Called by the New Order button."""
    key = _user_key()
    _reset_user(key)
    return jsonify({"ok": True})


@app.route("/westernchat/cart/remove", methods=["POST"])
def cart_remove():
    """Remove one line item from the server-side cart by index."""
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "JSON required"}), 400
    try:
        idx = int(data.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid index"}), 400

    key  = _user_key()
    cart = _get_cart(key)
    if idx < 0 or idx >= len(cart):
        return jsonify({"error": "Index out of range"}), 400

    cart.pop(idx)
    return jsonify({"ok": True, "cart": list(cart)})


@app.route("/westernchat/order", methods=["POST"])
def submit_order():
    """Accept contact + shipping form submission, save to DB, send email, reset user."""
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "JSON required"}), 400

    # Validate required fields
    name  = (data.get("name")  or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not email or "@" not in email:
        return jsonify({"error": "Name and valid email are required"}), 400

    phone   = (data.get("phone")   or "").strip()
    address = (data.get("address") or "").strip()
    city    = (data.get("city")    or "").strip()
    state   = (data.get("state")   or "").strip()
    zip_    = (data.get("zip")     or "").strip()

    key  = _user_key()
    cart = _get_cart(key)

    if not cart:
        return jsonify({"error": "Cart is empty"}), 400

    cart_snapshot = list(cart)

    # ── Save to SQLite ────────────────────────────────────────────────────────
    try:
        with sqlite3.connect(ORDERS_DB) as conn:
            conn.execute(
                """INSERT INTO orders
                   (session_key, name, email, phone, address, city, state, zip,
                    cart_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
                (key, name, email, phone, address, city, state, zip_,
                 json.dumps(cart_snapshot), datetime.utcnow().isoformat() + "Z"),
            )
            conn.commit()
    except Exception as e:
        logger.error("DB insert failed: %s", e)
        # Continue — still send the email even if DB write fails

    # ── Send email (non-blocking — failure must not fail the order) ────────────
    try:
        _send_order_summary(key, cart_snapshot, None, {
            "name":    name,
            "email":   email,
            "phone":   phone,
            "address": address,
            "city":    city,
            "state":   state,
            "zip":     zip_,
        })
    except Exception as e:
        logger.error("Email send failed (order still saved): %s", e)

    # ── Reset user state ──────────────────────────────────────────────────────
    _reset_user(key)

    return jsonify({"ok": True})


@app.route("/westernchat/health")
def health():
    return jsonify({"status": "ok", "service": "westernchat", "docs": len(DOC_INDEX),
                    "ts": datetime.utcnow().isoformat()}), 200


_ADMIN_ALLOWED_IPS = {
    "127.0.0.1", "::1",
    "172.58.122.24", "172.59.188.194", "172.58.126.126",
    "172.58.123.170", "172.58.123.19",
}
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def _admin_check():
    """Shared auth for all admin routes. Returns None if OK, else aborts."""
    remote = request.headers.get("X-Real-IP") or request.remote_addr or ""
    if remote not in _ADMIN_ALLOWED_IPS:
        abort(403)
    supplied = (request.args.get("token") or
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    if ADMIN_TOKEN and supplied != ADMIN_TOKEN:
        abort(403)


@app.route("/westernchat/admin/orders")
def admin_orders():
    """Order management dashboard — reads from SQLite orders table."""
    _admin_check()
    token = request.args.get("token", "")

    with sqlite3.connect(ORDERS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC"
        ).fetchall()

    # Summary counters
    counts = {"new": 0, "sent": 0, "completed": 0, "lost": 0}
    for r in rows:
        s = (r["status"] or "new").lower()
        if s in counts:
            counts[s] += 1

    STATUS_COLORS = {
        "new":       ("#1e40af", "#dbeafe"),
        "sent":      ("#854d0e", "#fef9c3"),
        "completed": ("#166534", "#dcfce7"),
        "lost":      ("#991b1b", "#fee2e2"),
    }
    STATUSES = ["new", "sent", "completed", "lost"]

    rows_html = []
    for r in rows:
        status  = (r["status"] or "new").lower()
        fg, bg  = STATUS_COLORS.get(status, ("#374151", "#f3f4f6"))
        ts      = (r["created_at"] or "")[:16].replace("T", " ")

        # Parse cart JSON safely
        try:
            cart = json.loads(r["cart_json"] or "[]")
        except Exception:
            cart = []

        parts_html = "".join(
            "<li style='margin-bottom:3px'>"
            f"<b>{p.get('description','—')}</b>"
            f"<br><small>Part # {p.get('part_number','TBD')} &nbsp;·&nbsp; "
            f"Qty {p.get('quantity',1)} &nbsp;·&nbsp; "
            f"{p.get('unit_price','—')}</small></li>"
            for p in cart
        ) or "<li style='color:#999'>No parts data</li>"

        ship = ", ".join(filter(None, [r["address"], r["city"], r["state"], r["zip"]]))

        # Status dropdown — posts to PATCH endpoint
        opts = "".join(
            f'<option value="{s}" {"selected" if s == status else ""}>{s.capitalize()}</option>'
            for s in STATUSES
        )
        status_sel = (
            f'<select data-id="{r["id"]}" data-token="{token}" '
            f'onchange="updateStatus(this)" '
            f'style="background:{bg};color:{fg};border:1px solid {fg};'
            f'border-radius:4px;padding:3px 6px;font-size:12px;font-weight:600;cursor:pointer">'
            f'{opts}</select>'
        )

        rows_html.append(f"""
        <tr>
          <td style="white-space:nowrap;color:#6b7280;font-size:11px">{ts}</td>
          <td>
            <div style="font-weight:600">{r['name']}</div>
            <div style="color:#2563eb;font-size:12px">{r['email']}</div>
            <div style="color:#6b7280;font-size:12px">{r['phone']}</div>
          </td>
          <td style="font-size:12px;color:#374151">{ship or '<span style="color:#9ca3af">—</span>'}</td>
          <td><ul style="margin:0;padding-left:16px;font-size:12px">{parts_html}</ul></td>
          <td style="text-align:center">{status_sel}</td>
        </tr>""")

    summary_html = "".join(
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;'
        f'padding:12px 20px;text-align:center;min-width:90px">'
        f'<div style="font-size:26px;font-weight:700;color:{STATUS_COLORS[s][0]}">{counts[s]}</div>'
        f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px">{s}</div>'
        f'</div>'
        for s in STATUSES
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WesternChat — Order Management</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; font-size: 13px;
         background: #f1f5f9; margin: 0; padding: 0; color: #1e293b; }}
  header {{ background: #0f172a; color: #fff; padding: 14px 24px;
            display: flex; align-items: center; gap: 12px; }}
  header h1 {{ margin: 0; font-size: 16px; font-weight: 700; }}
  header span {{ font-size: 12px; color: #94a3b8; }}
  .container {{ padding: 20px 24px; }}
  .summary {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff;
           border-radius: 8px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.07); }}
  th {{ background: #1e293b; color: #fff; padding: 10px 12px;
        text-align: left; font-size: 11px; text-transform: uppercase;
        letter-spacing: .5px; white-space: nowrap; }}
  td {{ border-bottom: 1px solid #f1f5f9; padding: 10px 12px; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f8fafc; }}
  .empty {{ text-align: center; padding: 48px; color: #9ca3af; font-size: 14px; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>WesternChat — Order Management</h1>
    <span>{len(rows)} order{"s" if len(rows) != 1 else ""} total</span>
  </div>
</header>
<div class="container">
  <div class="summary">{summary_html}</div>
  {"<table><thead><tr><th>Submitted</th><th>Customer</th><th>Ship To</th><th>Parts</th><th>Status</th></tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>" if rows else '<div class="empty">No orders yet.</div>'}
</div>
<script>
async function updateStatus(sel) {{
  const id    = sel.dataset.id;
  const token = sel.dataset.token;
  const status = sel.value;
  try {{
    const res = await fetch('/westernchat/admin/orders/' + id + '/status?token=' + encodeURIComponent(token), {{
      method: 'PATCH',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{status}})
    }});
    if (!res.ok) {{ alert('Update failed'); sel.form && sel.form.reset(); }}
  }} catch(e) {{ alert('Network error'); }}
}}
</script>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/westernchat/admin/orders/<int:order_id>/status", methods=["PATCH"])
def admin_update_status(order_id):
    """Update order status — same auth as admin_orders."""
    _admin_check()
    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in {"new", "sent", "completed", "lost"}:
        return jsonify({"error": "Invalid status"}), 400
    with sqlite3.connect(ORDERS_DB) as conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        conn.commit()
    return jsonify({"ok": True, "id": order_id, "status": status})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8088, debug=False)
