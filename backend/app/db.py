"""Business data (listings, leads, bookings, follow-ups, logs) with SQLAlchemy Core.

The same code runs on Postgres (deployed, Supabase/Neon) and SQLite (local dev).
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, text, update)

from app import config

metadata = MetaData()

businesses = Table(                                       # one row per dealer (tenant)
    "dc_businesses", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("category", String(20), nullable=False),       # property | car | all (platform demo)
    Column("city", String(60)),
    Column("phone", String(40)),
    Column("tagline", Text),                              # one line shown in the showroom / widget
    Column("brand_color", String(9)),                     # widget accent, e.g. #0a7d5a
    Column("policies", Text),                             # this dealer's own FAQ / policies (RAG source)
    Column("listing_prefix", String(3)),                  # listing ids: <prefix><number>, e.g. AR001
    Column("plan", String(20), default="pilot"),
    Column("status", String(20), default="active"),       # active | suspended
    Column("extra_credits", Integer, default=0),          # bought/granted conversations beyond the plan
    Column("widget_key", String(64), unique=True),        # public key the chat widget sends
    Column("console_key_hash", String(64)),               # sha256 of the dealer console key (never stored plain)
    Column("allowed_origins", Text),                      # websites allowed to use the widget (comma list)
    Column("showcase", Integer, default=0),               # 1 = shown on the public showroom/landing
    Column("assign_mode", String(20), default="manual"),  # manual | round_robin (new leads -> next salesperson)
    Column("page_enabled", Integer, default=1),           # 1 = the hosted agency page /a/<id> is public
    Column("created_at", DateTime(timezone=True)),
)

members = Table(                                          # agency team: managers and salespeople (audit 3.5)
    "dc_members", metadata,
    Column("id", String(24), primary_key=True),           # random (audit 4.2)
    Column("business_id", String(40), nullable=False, index=True),
    Column("name", String(80), nullable=False),
    Column("phone", String(40)),                          # for hot-lead alerts (WhatsApp in phase 2)
    Column("role", String(20), nullable=False),           # manager | agent
    Column("key_hash", String(64), unique=True),          # sha256 of the personal console key (never stored plain)
    Column("active", Integer, default=1),
    Column("last_assigned_at", DateTime(timezone=True)),  # round-robin order
    Column("last_login_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
)

channels = Table(                                         # an agency's connected WhatsApp number / Facebook page / Instagram
    "dc_channels", metadata,
    Column("id", String(24), primary_key=True),           # random
    Column("business_id", String(40), nullable=False, index=True),
    Column("kind", String(20), nullable=False),           # whatsapp | messenger | instagram | leadgen
    Column("external_id", String(64), nullable=False),    # phone_number_id | page id | instagram account id
    Column("display", String(120)),                       # +92 300 1234567 | page name
    Column("token_enc", Text),                            # Meta access token, Fernet-encrypted (audit 1.3)
    Column("settings", JSON),                             # template names, language
    Column("status", String(20), default="active"),       # active | disabled
    Column("created_at", DateTime(timezone=True)),
    Column("last_event_at", DateTime(timezone=True)),
    Column("last_error", String(300)),
)

outbox = Table(                                           # every outbound message, with retries (audit 11.2)
    "dc_outbox", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_id", String(40), index=True),
    Column("channel_id", String(24)),
    Column("lead_id", String(80), index=True),
    Column("to", String(64)),
    Column("kind", String(20)),                           # text | template
    Column("payload", JSON),
    Column("purpose", String(20)),                        # reply | followup | first_contact | staff_alert | manual
    Column("status", String(20), default="queued"),       # queued | sent | failed
    Column("attempts", Integer, default=0),
    Column("next_at", DateTime(timezone=True), index=True),
    Column("external_id", String(120)),
    Column("error", String(300)),
    Column("created_at", DateTime(timezone=True)),
    Column("sent_at", DateTime(timezone=True)),
)

webhook_events = Table(                                   # each Meta event is processed exactly once (audit 6.6)
    "dc_webhook_events", metadata,
    Column("id", String(64), primary_key=True),           # sha256 of the provider's event / message id
    Column("received_at", DateTime(timezone=True)),
)

links = Table(                                            # tracked links and QR codes: where leads come from
    "dc_links", metadata,
    Column("id", String(16), primary_key=True),           # random short slug used in /l/<id> and ?lp=<id>
    Column("business_id", String(40), nullable=False, index=True),
    Column("name", String(80), nullable=False),           # e.g. "DHA Phase 9 board"
    Column("channel", String(20), nullable=False),        # qr | facebook | instagram | whatsapp | google | print | website | other
    Column("target_url", String(300)),                    # null = the hosted agency page
    Column("clicks", Integer, default=0),
    Column("active", Integer, default=1),
    Column("created_by", String(80)),
    Column("created_at", DateTime(timezone=True)),
)

usage = Table(                                            # per dealer per month (YYYY-MM)
    "dc_usage", metadata,
    Column("business_id", String(40), primary_key=True),
    Column("month", String(7), primary_key=True),
    Column("conversations", Integer, default=0),
    Column("credits_used", Integer, default=0),
    Column("blocked", Integer, default=0),                # new conversations refused at the limit (capture-only)
    Column("ai_turns", Integer, default=0),
    Column("tokens_in", Integer, default=0),
    Column("tokens_out", Integer, default=0),
)

conversations_seen = Table(                               # makes "count each conversation once" atomic
    "dc_conversations_seen", metadata,
    Column("business_id", String(40), primary_key=True),
    Column("month", String(7), primary_key=True),
    Column("thread", String(260), primary_key=True),
    Column("allowed", Integer, default=1),
)

alerts = Table(
    "dc_alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_id", String(40), index=True),
    Column("dedupe", String(80), unique=True),            # e.g. arani:2026-12:80 -> each alert fires once
    Column("level", String(10)),                          # info | warning | critical
    Column("member_id", String(24), index=True),          # set = a personal alert for one team member
    Column("lead_id", String(80)),                        # the lead the alert is about, if any
    Column("message", Text),
    Column("read", Integer, default=0),
    Column("created_at", DateTime(timezone=True)),
)

credit_ledger = Table(                                    # every credit change, for audit and support
    "dc_credit_ledger", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("business_id", String(40), index=True),
    Column("delta", Integer),
    Column("reason", String(200)),
    Column("created_at", DateTime(timezone=True)),
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
    Column("channel", String(20)),                        # first touch: website | page | form | qr | facebook | ...
    Column("link_id", String(16)),                        # the tracked link that brought them, if any
    Column("utm", JSON),                                  # {source, medium, campaign} from the landing URL
    Column("phone_norm", String(20), index=True),         # +923001234567: one buyer across channels
    Column("merged_into", String(80), index=True),        # set when this lead turned out to be a known buyer
    Column("assigned_to", String(24), index=True),        # dc_members.id
    Column("contact_kind", String(20)),                   # whatsapp | messenger | instagram: where we can message them
    Column("contact_channel_id", String(24)),             # dc_channels.id
    Column("contact_id", String(64)),                     # WhatsApp number (923001234567) or page-scoped id
    Column("last_inbound_at", DateTime(timezone=True)),   # last message from the buyer (24h window)
    Column("opted_out", Integer, default=0),              # buyer sent STOP: no business-initiated messages
    Column("ai_paused", Integer, default=0),              # a salesperson took over the conversation
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


pilot_requests = Table(                                    # dealers asking to try LeadPilot (landing page)
    "dc_pilot_requests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(80), nullable=False),
    Column("company", String(120), nullable=False),
    Column("phone", String(30), nullable=False),
    Column("email", String(120)),
    Column("dealer_type", String(20)),                    # property | car | both
    Column("monthly_leads", String(20)),
    Column("message", Text),
    Column("status", String(20), default="new"),          # new | contacted | closed
    Column("created_at", DateTime(timezone=True)),
)

rate_limits = Table(                                       # audit 6.1: shared, restart-proof counters
    "dc_rate_limits", metadata,
    Column("key", String(64), primary_key=True),
    Column("count", Integer, nullable=False, default=0),
    Column("expires_at", DateTime(timezone=True), index=True),
)

admin_sessions = Table(                                    # audit 2.5: expiring, revocable dealer sessions
    "dc_admin_sessions", metadata,
    Column("jti", String(64), primary_key=True),
    Column("business", String(40), nullable=False),
    Column("member_id", String(24), index=True),          # null = an agency or platform key, not a personal one
    Column("role", String(20)),                           # owner | manager | agent
    Column("created_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked", Integer, default=0),
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
    migrate()


# Columns added after the first deployment. create_all() never alters existing tables, so add them here.
LATER_COLUMNS = {
    "dc_businesses": {"tagline": "TEXT", "brand_color": "VARCHAR(9)", "policies": "TEXT", "listing_prefix": "VARCHAR(3)",
                      "plan": "VARCHAR(20)", "status": "VARCHAR(20)", "extra_credits": "INTEGER",
                      "widget_key": "VARCHAR(64)", "console_key_hash": "VARCHAR(64)", "allowed_origins": "TEXT",
                      "showcase": "INTEGER", "created_at": "TIMESTAMP WITH TIME ZONE",
                      "assign_mode": "VARCHAR(20)", "page_enabled": "INTEGER"},
    "dc_leads": {"channel": "VARCHAR(20)", "link_id": "VARCHAR(16)", "utm": "JSON", "phone_norm": "VARCHAR(20)",
                 "merged_into": "VARCHAR(80)", "assigned_to": "VARCHAR(24)", "contact_kind": "VARCHAR(20)",
                 "contact_channel_id": "VARCHAR(24)", "contact_id": "VARCHAR(64)",
                 "last_inbound_at": "TIMESTAMP WITH TIME ZONE", "opted_out": "INTEGER", "ai_paused": "INTEGER"},
    "dc_alerts": {"member_id": "VARCHAR(24)", "lead_id": "VARCHAR(80)"},
    "dc_admin_sessions": {"member_id": "VARCHAR(24)", "role": "VARCHAR(20)"},
}
LATER_INDEXES = {"dc_leads": ("phone_norm", "merged_into", "assigned_to"), "dc_alerts": ("member_id",),
                 "dc_admin_sessions": ("member_id",)}


def migrate():
    """Idempotent: adds missing columns on both Postgres and SQLite."""
    with engine.begin() as c:
        for table, cols in LATER_COLUMNS.items():
            if is_postgres():
                for name, typ in cols.items():
                    c.execute(text(f'alter table {table} add column if not exists "{name}" {typ}'))
                if table == "dc_businesses":
                    c.execute(text(f"create unique index if not exists ux_{table}_widget_key on {table} (widget_key)"))
                for col in LATER_INDEXES.get(table, ()):
                    c.execute(text(f"create index if not exists ix_{table}_{col} on {table} ({col})"))
            else:
                have = {r[1] for r in c.execute(text(f"pragma table_info({table})"))}
                for name, typ in cols.items():
                    if name not in have:
                        c.execute(text(f"alter table {table} add column {name} {typ.replace(' WITH TIME ZONE', '')}"))


def lock_down_supabase():
    """Audit 4.5: Supabase exposes the public schema through its Data API to the `anon` and `authenticated`
    roles. This app never uses that API (the server connects as the database owner), so turn RLS on for every
    table and revoke those roles' access. Runs at every start, so tables created later are covered too."""
    if not is_postgres():
        return
    with engine.begin() as c:
        roles = {r[0] for r in c.execute(text("select rolname from pg_roles where rolname in ('anon','authenticated')"))}
        tables = [r[0] for r in c.execute(text(
            "select tablename from pg_tables where schemaname = 'public'"))]
        for t in tables:
            c.execute(text(f'alter table public."{t}" enable row level security'))
            for role in roles:
                c.execute(text(f'revoke all on table public."{t}" from {role}'))
        for role in roles:
            c.execute(text(f"alter default privileges in schema public revoke all on tables from {role}"))
            c.execute(text(f"revoke all on all sequences in schema public from {role}"))


def delete_customer(user_id: str):
    """Audit 12.3: remove this customer's rows from every business table."""
    with engine.begin() as c:
        for table in (followups, bookings, chat_log, audit_log, leads):
            c.execute(delete(table).where(table.c.user_id == user_id))
        c.execute(delete(outbox).where(outbox.c.lead_id == user_id))
        c.execute(delete(alerts).where(alerts.c.lead_id == user_id))


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
    """Create the lead row if needed, then update only the fields given (None = leave as is).
    Then the lead hooks run (phone matching, auto-assignment, hot-lead alerts); they never raise."""
    fields = {k: v for k, v in fields.items() if v is not None}
    from app import identity
    if "phone" in fields:
        fields["phone_norm"] = identity.normalize_phone(fields["phone"])
    before = get_lead(user_id)
    if before is None:
        base = {"score": "cold", "status": "new", "followup_count": 0, "created_at": now()}
        run(insert(leads).values(user_id=user_id, **{**base, **fields}))
    elif fields:
        run(update(leads).where(leads.c.user_id == user_id).values(**fields))
    identity.after_change(before, get_lead(user_id))
    return get_lead(user_id)
