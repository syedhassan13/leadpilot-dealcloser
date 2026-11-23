"""All settings come from environment variables (.env locally, secrets when deployed)."""
import os
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

# "business:token" pairs. "*" sees every business (super admin).
ADMIN_TOKENS = os.getenv("ADMIN_TOKENS", "skyline:skyline-demo,autohub:autohub-demo,*:admin-demo")
CRON_SECRET = os.getenv("CRON_SECRET", "cron-demo")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Haiku 5.5 list price per 1M tokens, used for the cost numbers on the dashboard.
PRICE_IN_PER_M = 0.10
PRICE_OUT_PER_M = 0.50


def admin_tokens() -> dict[str, str]:
    """token -> business id ("*" = all businesses)."""
    pairs = (p.split(":", 1) for p in ADMIN_TOKENS.split(",") if ":" in p)
    return {token.strip(): business.strip() for business, token in pairs}
