"""Business data (listings, leads, bookings, follow-ups, logs) with SQLAlchemy Core.

The same code runs on Postgres (deployed, Supabase/Neon) and SQLite (local dev).
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, insert, select, update)

from app import config

metadata = MetaData()

businesses = Table(
    "dc_businesses", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("category", String(20), nullable=False),       # property | car
    Column("city", String(60)),
    Column("phone", String(40)),
)

listings = Table(
    "dc_listings", metadata,
    Column("id", String(20), primary_key=True),
    Column("business_id", String(40), ForeignKey("dc_businesses.id"), nullable=False),
    Column("category", String(20), nullable=False),
    Column("title", String(200), nullable=False),
    Column("location", String(120)),
    Column("price_pkr", Integer, nullable=False),
    Column("bedrooms", Integer),
    Column("year", Integer),
    Column("details", Text),
    Column("image", String(300)),
    Column("status", String(20), default="available"),
)

leads = Table(
    "dc_leads", metadata,
    Column("user_id", String(80), primary_key=True),
    Column("business_id", String(40)),
    Column("name", String(120)),
    Column("phone", String(40)),
    Column("category", String(20)),
    Column("budget_pkr", Integer),
    Column("timeline", String(120)),
    Column("timeline_months", Integer),
    Column("preferences", Text),
    Column("financing", String(120)),
    Column("score", String(10), default="cold"),          # hot | warm | cold
    Column("status", String(20), default="new"),          # new | qualified | booked | lost
    Column("source", String(20), default="chat"),
    Column("first_response_ms", Integer),
    Column("followup_count", Integer, default=0),
    Column("last_thread_id", String(120)),
    Column("last_customer_at", DateTime(timezone=True)),
    Column("last_agent_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
)

bookings = Table(
    "dc_bookings", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(80), nullable=False, index=True),
    Column("listing_id", String(20), ForeignKey("dc_listings.id"), nullable=False),
    Column("business_id", String(40), nullable=False),
    Column("kind", String(20)),                           # site visit | test drive
    Column("date", String(10), nullable=False),
    Column("time", String(5), nullable=False),
    Column("status", String(20), default="confirmed"),    # confirmed | cancelled | completed
    Column("created_at", DateTime(timezone=True)),
)

followups = Table(
    "dc_followups", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(80), nullable=False, index=True),
    Column("thread_id", String(120)),
    Column("business_id", String(40)),
    Column("step", Integer),                              # 1, 2, 3 -> day 1, 3, 7
    Column("message", Text),
    Column("status", String(20), default="draft"),        # draft | sent | skipped
    Column("created_at", DateTime(timezone=True)),
    Column("sent_at", DateTime(timezone=True)),
)

chat_log = Table(
    "dc_chat_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(80), nullable=False, index=True),
    Column("thread_id", String(120)),
    Column("message", Text),
    Column("reply", Text),
    Column("tool_calls", JSON),
    Column("blocked", Integer, default=0),
    Column("tokens_in", Integer, default=0),
    Column("tokens_out", Integer, default=0),
    Column("latency_ms", Integer),
    Column("created_at", DateTime(timezone=True)),
)

audit_log = Table(
    "dc_audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(80), index=True),
    Column("tool", String(60)),
    Column("args", JSON),
    Column("created_at", DateTime(timezone=True)),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def is_postgres() -> bool:
    return bool(config.DATABASE_URL)


def sqlalchemy_url() -> str:
    url = config.DATABASE_URL
    if not url:
        return f"sqlite:///{config.SQLITE_PATH}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _make_engine():
    url = sqlalchemy_url()
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # prepare_threshold=None keeps psycopg working behind Supabase's connection pooler
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                         connect_args={"prepare_threshold": None})


engine = _make_engine()


def init_db():
    metadata.create_all(engine)


def rows(stmt) -> list[dict]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def one(stmt) -> dict | None:
    r = rows(stmt)
    return r[0] if r else None


def run(stmt):
    with engine.begin() as c:
        return c.execute(stmt)


def get_lead(user_id: str) -> dict | None:
    return one(select(leads).where(leads.c.user_id == user_id))


def upsert_lead(user_id: str, **fields) -> dict:
    """Create the lead row if needed, then update only the fields given (None = leave as is)."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if get_lead(user_id) is None:
        base = {"score": "cold", "status": "new", "followup_count": 0, "created_at": now()}
        run(insert(leads).values(user_id=user_id, **{**base, **fields}))
    elif fields:
        run(update(leads).where(leads.c.user_id == user_id).values(**fields))
    return get_lead(user_id)
