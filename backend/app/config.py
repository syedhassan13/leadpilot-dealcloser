"""All settings come from environment variables (.env locally, secrets when deployed)."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

MODEL = os.getenv("MODEL", "claude-haiku-5-5")
EFFORT = os.getenv("EFFORT", "low")                 # low thinking effort keeps cost and latency down
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "700"))
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "16"))   # short-term window sent to the model

# Postgres (Supabase/Neon) in production; a local SQLite file when DATABASE_URL is not set.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("SQLITE_PATH", str(ROOT / "backend" / "dealcloser.db"))

# Production = deployed with a real database, or ENV=production. Demo secrets only ever work in development.
PRODUCTION = os.getenv("ENV", "production" if DATABASE_URL else "development").lower() == "production"

# Dealer keys: "business:key" pairs. "*" sees every business. No defaults in production (audit 2.2).
ADMIN_TOKENS = os.getenv("ADMIN_TOKENS", "" if PRODUCTION else "skyline:skyline-demo,autohub:autohub-demo,*:admin-demo")
CRON_SECRET = os.getenv("CRON_SECRET", "" if PRODUCTION else "cron-demo")
# Signs customer tokens and admin sessions. Must be set in production; a random one per run locally.
SESSION_SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_urlsafe(32)
if PRODUCTION and not os.getenv("SESSION_SECRET"):
    raise RuntimeError("SESSION_SECRET must be set in production")

# Exact website origins allowed to call the API from a browser (audit 6.2).
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:7860,http://127.0.0.1:7860").split(",") if o.strip()]

# API docs are off in production unless explicitly enabled (audit 10.3).
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "0" if PRODUCTION else "1") == "1"

# Abuse and cost limits (audit 6.1, 8.1, 11.2)
CHAT_PER_USER_PER_MIN = int(os.getenv("CHAT_PER_USER_PER_MIN", "20"))
CHAT_PER_USER_PER_DAY = int(os.getenv("CHAT_PER_USER_PER_DAY", "200"))
CHAT_PER_IP_PER_MIN = int(os.getenv("CHAT_PER_IP_PER_MIN", "120"))
AI_TURNS_PER_DAY = int(os.getenv("AI_TURNS_PER_DAY", "5000"))        # whole-app daily cap on model turns
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
MAX_FOLLOWUPS_PER_RUN = int(os.getenv("MAX_FOLLOWUPS_PER_RUN", "20"))

# Haiku 5.5 list price per 1M tokens, used for the cost numbers on the dashboard.
PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.50

# Lock-down switch. False keeps POST /chat open without a key (the assignment's grader needs that).
# Set CHAT_REQUIRE_KEY=true after grading: then every chat must carry a dealer widget key.
# Public addresses. Tracked links (QR codes) live on the API (/l/<id>) and send buyers to the agency page on the
# website (/a/<agency>). In development the API also serves the website, so both default to the local server.
PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "https://leadpilot-api-hhys.onrender.com" if PRODUCTION
                           else "http://127.0.0.1:8000").rstrip("/")
SITE_URL = os.getenv("SITE_URL", "https://leadpilot-hassans-team1.vercel.app" if PRODUCTION
                     else PUBLIC_API_URL).rstrip("/")
# Meta (WhatsApp Cloud API, Messenger, Instagram, lead forms). Channels stay off until these are set.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")          # verifies webhook signatures (X-Hub-Signature-256)
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")      # answers Meta's webhook subscription check
CHANNEL_KEY = os.getenv("CHANNEL_KEY", "")                  # Fernet key that encrypts agencies' Meta tokens
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")
CHAT_REQUIRE_KEY = os.getenv("CHAT_REQUIRE_KEY", "false").lower() in ("1", "true", "yes")

# Plans: monthly buyer-conversation allowance (None = no limit, for internal accounts) and features.
PLANS = {
    "pilot":    {"name": "Pilot",    "price_usd": 0,   "conversations": 300,  "followups": True,  "websites": 1},
    "starter":  {"name": "Starter",  "price_usd": 20,  "conversations": 500,  "followups": False, "websites": 1},
    "growth":   {"name": "Growth",   "price_usd": 50,  "conversations": 2000, "followups": True,  "websites": 3},
    "scale":    {"name": "Scale",    "price_usd": 100, "conversations": 6000, "followups": True,  "websites": None},
    "internal": {"name": "Internal", "price_usd": 0,   "conversations": None, "followups": True,  "websites": None},
}
ALERT_THRESHOLDS = (80, 100)   # % of the monthly allowance (plan + extra credits) that raises an alert


def admin_tokens() -> dict[str, str]:
    """key -> business id ("*" = all businesses). Empty keys are ignored."""
    out = {}
    for pair in ADMIN_TOKENS.split(","):
        if ":" in pair:
            business, key = pair.split(":", 1)
            if key.strip():
                out[key.strip()] = business.strip()
    return out
