"""Dealers (tenants): lookup, keys, plans, monthly usage, credits and alerts.

A dealer is a row in dc_businesses. Nothing about a specific dealer is hard-coded anywhere else.

Billing unit = one buyer conversation (a thread) per calendar month (UTC). Each new conversation is counted
once, against the plan allowance first and then against extra credits. When both are used up the dealer is in
capture-only mode for NEW conversations (no AI call, lead still saved); conversations already started may finish.
"""
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import and_, insert, select, update

from app import config
from app.db import alerts, businesses, conversations_seen, credit_ledger, engine, is_postgres, now, one, rows, run, usage

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
PREFIX_RE = re.compile(r"^[A-Z]{1,3}$")
LISTING_ID_PATTERN = r"^[A-Za-z]{1,3}[0-9]{3,4}$"
CATEGORY_WORDS = {"property": ("property", "properties"), "car": ("car", "cars"), "all": ("property or car", "properties and cars")}


# ───────────────────────── lookup (cached briefly; dealers change rarely) ─────────────────────────
_cache: dict = {"at": 0.0, "by_id": {}, "by_widget": {}}


def _refresh(force=False):
    if not force and time.time() - _cache["at"] < 30:
        return
    all_rows = rows(select(businesses))
    _cache.update(at=time.time(), by_id={r["id"]: r for r in all_rows},
                  by_widget={r["widget_key"]: r for r in all_rows if r.get("widget_key")})


def invalidate():
    _cache["at"] = 0.0


def get(dealer_id: str | None) -> dict | None:
    if not dealer_id:
        return None
    _refresh()
    return _cache["by_id"].get(dealer_id)


def all_dealers() -> list[dict]:
    _refresh()
    return sorted(_cache["by_id"].values(), key=lambda r: r["id"])


def by_widget_key(key: str | None) -> dict | None:
    if not key:
        return None
    _refresh()
    d = _cache["by_widget"].get(key)
    if d is None:                       # a key created seconds ago on another worker
        _refresh(force=True)
        d = _cache["by_widget"].get(key)
    return d


def category_of(dealer_id: str | None) -> str | None:
    d = get(dealer_id)
    return d["category"] if d else None


def words(category: str | None) -> tuple[str, str]:
    return CATEGORY_WORDS.get(category or "all", CATEGORY_WORDS["all"])


def default_dealer_for(category: str | None) -> str | None:
    """When a chat isn't tied to a dealer (platform demo / open grading mode), leads go to the first
    showcase dealer that sells this category."""
    for d in all_dealers():
        if d.get("showcase") and d["category"] == category:
            return d["id"]
    return None


def scope_of(dealer: dict | None) -> str | None:
    """The listing scope for a dealer: its id, or None for the platform demo (category 'all')."""
    if not dealer or dealer["category"] == "all":
        return None
    return dealer["id"]


# ───────────────────────── keys ─────────────────────────
def new_widget_key() -> str:
    return "wk_" + secrets.token_urlsafe(24)


def new_console_key(dealer_id: str) -> str:
    return f"{dealer_id}_" + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def dealer_for_console_key(key: str) -> str | None:
    """Database-issued dealer keys (stored as sha256). Keys in ADMIN_TOKENS are checked separately."""
    if not key:
        return None
    h = hash_key(key)
    for d in all_dealers():
        if d.get("console_key_hash") and hmac.compare_digest(d["console_key_hash"], h):
            return d["id"]
    return None


def check_origin(dealer: dict, request: Request):
    """If the dealer listed its websites, browsers on other sites can't use its widget key."""
    allowed = [o.strip().rstrip("/") for o in (dealer.get("allowed_origins") or "").split(",") if o.strip()]
    origin = (request.headers.get("origin") or "").rstrip("/")
    if allowed and origin and origin not in allowed + [o.rstrip("/") for o in config.ALLOWED_ORIGINS]:
        raise HTTPException(403, "This website isn't allowed to use this chat. Ask the dealer to add it in LeadPilot.")


def resolve_widget(request: Request, required: bool | None = None) -> dict | None:
    """The dealer behind this request's X-Widget-Key, or None. Raises when a key is required but missing/invalid."""
    key = request.headers.get("x-widget-key")
    must = config.CHAT_REQUIRE_KEY if required is None else required
    if not key:
        if must:
            raise HTTPException(401, "A dealer widget key is required.")
        return None
    dealer = by_widget_key(key)
    if dealer is None:
        raise HTTPException(401, "Unknown widget key.")
    check_origin(dealer, request)
    return dealer


# ───────────────────────── plans and usage ─────────────────────────
def month_key(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m")


def plan_of(dealer: dict) -> dict:
    return config.PLANS.get(dealer.get("plan") or "pilot", config.PLANS["pilot"])


def usage_row(dealer_id: str, month: str | None = None) -> dict:
    r = one(select(usage).where(and_(usage.c.business_id == dealer_id, usage.c.month == (month or month_key()))))
    return r or {"business_id": dealer_id, "month": month or month_key(), "conversations": 0, "credits_used": 0,
                 "blocked": 0, "ai_turns": 0, "tokens_in": 0, "tokens_out": 0}


def _dialect_insert():
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as ins
    else:
        from sqlalchemy.dialects.sqlite import insert as ins
    return ins


def _bump(c, dealer_id: str, month: str, **inc):
    ins = _dialect_insert()
    stmt = ins(usage).values(business_id=dealer_id, month=month, **{k: v for k, v in inc.items()})
    stmt = stmt.on_conflict_do_update(index_elements=["business_id", "month"],
                                      set_={k: getattr(usage.c, k) + v for k, v in inc.items()})
    c.execute(stmt)


def summary(dealer: dict) -> dict:
    """Everything the console needs to show a usage meter."""
    p, u = plan_of(dealer), usage_row(dealer["id"])
    included = p["conversations"]
    credits = int(dealer.get("extra_credits") or 0)
    limit = None if included is None else included + credits + u["credits_used"]
    return {"plan": dealer.get("plan") or "pilot", "plan_name": p["name"], "price_usd": p["price_usd"],
            "included": included, "extra_credits": credits, "credits_used_this_month": u["credits_used"],
            "conversations": u["conversations"], "limit": limit, "blocked": u["blocked"],
            "percent": None if not limit else round(100 * u["conversations"] / limit, 1),
            "ai_turns": u["ai_turns"], "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"],
            "cost_usd": round(u["tokens_in"] / 1e6 * config.PRICE_IN_PER_M + u["tokens_out"] / 1e6 * config.PRICE_OUT_PER_M, 4),
            "status": dealer.get("status") or "active", "followups": p["followups"], "websites": p["websites"],
            "month": u["month"]}


NEW_CONVERSATIONS_PER_IP_PER_HOUR = 10   # stops someone burning a dealer's allowance with its public widget key


def admit_conversation(dealer: dict, thread: str, ip: str | None = None) -> tuple[bool, str]:
    """Count a conversation the first time it's seen this month and decide if it may use the AI.
    Returns (allowed, reason). Atomic: concurrent first messages of one thread are counted once."""
    if (dealer.get("status") or "active") != "active":
        return False, "suspended"
    month = month_key()
    ins = _dialect_insert()
    seen = one(select(conversations_seen.c.allowed).where(and_(
        conversations_seen.c.business_id == dealer["id"], conversations_seen.c.month == month,
        conversations_seen.c.thread == thread)))
    if seen is not None:                           # already counted: an ongoing conversation may finish
        return bool(seen["allowed"]), "ongoing" if seen["allowed"] else "limit"
    if ip is not None:
        from app.security import hit
        if not hit("new_conv_ip", f"{dealer['id']}|{ip}", NEW_CONVERSATIONS_PER_IP_PER_HOUR, 3600):
            return False, "too_many_new"
    with engine.begin() as c:
        included = plan_of(dealer)["conversations"]
        current = c.execute(select(usage.c.conversations).where(and_(usage.c.business_id == dealer["id"],
                                                                      usage.c.month == month))).scalar() or 0
        allowed, use_credit = True, False
        if included is not None and current >= included:
            # beyond the plan: spend one extra credit, atomically (never below zero)
            took = c.execute(update(businesses).where(and_(businesses.c.id == dealer["id"], businesses.c.extra_credits > 0))
                             .values(extra_credits=businesses.c.extra_credits - 1)).rowcount
            allowed, use_credit = bool(took), bool(took)
        res = c.execute(ins(conversations_seen).values(business_id=dealer["id"], month=month, thread=thread,
                                                       allowed=int(allowed)).on_conflict_do_nothing())
        if not res.rowcount:                       # another request counted it first
            if use_credit:                         # give the credit back
                c.execute(update(businesses).where(businesses.c.id == dealer["id"])
                          .values(extra_credits=businesses.c.extra_credits + 1))
            return True, "ongoing"
        if allowed:
            _bump(c, dealer["id"], month, conversations=1, credits_used=1 if use_credit else 0)
        else:
            _bump(c, dealer["id"], month, blocked=1)
    if use_credit:
        invalidate()
    _maybe_alert(dealer["id"])
    return allowed, "credit" if use_credit else ("ok" if allowed else "limit")


def record_ai(dealer_id: str | None, tokens_in: int, tokens_out: int, turns: int = 1):
    if not dealer_id or (not tokens_in and not tokens_out):
        return
    with engine.begin() as c:
        _bump(c, dealer_id, month_key(), ai_turns=turns, tokens_in=int(tokens_in), tokens_out=int(tokens_out))


# ───────────────────────── alerts ─────────────────────────
def alert(dealer_id: str, dedupe: str, level: str, message: str, member_id: str | None = None,
          lead_id: str | None = None):
    """One alert per dedupe key. member_id = a personal alert for one team member (else the whole agency)."""
    ins = _dialect_insert()
    with engine.begin() as c:
        c.execute(ins(alerts).values(business_id=dealer_id, dedupe=dedupe[:80], level=level, message=message[:500],
                                     member_id=member_id, lead_id=lead_id, read=0, created_at=now())
                  .on_conflict_do_nothing())


def _maybe_alert(dealer_id: str):
    invalidate()
    dealer = get(dealer_id)
    s = summary(dealer)
    if not s["limit"]:
        return
    for pct in config.ALERT_THRESHOLDS:
        if s["conversations"] >= s["limit"] * pct / 100:
            if pct >= 100:
                msg = (f"You've used all {s['limit']:,} conversations for {s['month']}. New buyers now get a "
                       "'we'll call you back' reply and their details are saved. Upgrade or add credits to switch the AI back on.")
                level = "critical"
            else:
                msg = f"You've used {s['conversations']:,} of {s['limit']:,} conversations ({pct}%) for {s['month']}."
                level = "warning"
            alert(dealer_id, f"{dealer_id}:{s['month']}:{pct}", level, msg)


def add_credits(dealer_id: str, delta: int, reason: str):
    with engine.begin() as c:
        c.execute(update(businesses).where(businesses.c.id == dealer_id)
                  .values(extra_credits=businesses.c.extra_credits + delta))
        c.execute(insert(credit_ledger).values(business_id=dealer_id, delta=delta, reason=reason[:200], created_at=now()))
    invalidate()
    if delta > 0:
        alert(dealer_id, f"{dealer_id}:credit:{int(time.time())}", "info", f"{delta:,} conversation credits were added: {reason}.")


def page_on(dealer: dict) -> bool:
    """The hosted agency page is on unless a manager turned it off (NULL on agencies created before the setting)."""
    return dealer.get("page_enabled") != 0
