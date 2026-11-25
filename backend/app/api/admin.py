"""Dealer console endpoints. Login exchanges a dealer key for a signed session; every other route needs it.
A dealer only ever sees its own business's rows ("*" = super admin sees all)."""
import hashlib
import hmac
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update

from app import config, tenants
from app.db import (admin_sessions, alerts, audit_log, bookings, businesses, chat_log, credit_ledger, engine, followups,
                    get_lead, leads, listings, now, one, pilot_requests, rows, run, upsert_lead, usage)
from app.followups import run_followups, send_followup, skip_followup
from app.graph import thread_key
from app.runtime import rt
from app.security import (admin_login, client_ip, enforce, hit, log_event, peek, revoke_admin, verify_admin)
from app.services import bookings_of, erase_customer, profile_of, thread_ids, thread_messages
from app.tools import today_pkt

router = APIRouter()
MAX_PAGE = 200


# ───────────────────────── session ─────────────────────────

class LoginIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.post("/admin/login")
def login(body: LoginIn, request: Request):
    ip = client_ip(request)
    # lockout after 10 FAILED attempts per IP per 15 minutes (successful logins don't count)
    if peek("admin_login_fail", ip, 900) >= 10:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    session = admin_login(body.key)
    if not session:
        hit("admin_login_fail", ip, 10, 900)
        log_event("admin_login_failed", "", {"ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:12]})
        raise HTTPException(401, "Invalid dealer key")
    log_event("admin_login", "", {"business": session["business"]})
    return session


def admin_scope(request: Request, authorization: str = Header(default="")) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in")
    business, jti = verify_admin(authorization[7:].strip())
    request.state.admin_jti = jti
    return business


def super_admin(business: str = Depends(admin_scope)) -> str:
    if business != "*":
        raise HTTPException(403, "Only the LeadPilot team can see this")
    return business


@router.post("/admin/logout")
def logout(request: Request, business: str = Depends(admin_scope)):
    revoke_admin(request.state.admin_jti)
    log_event("admin_logout", "", {"business": business})
    return {"ok": True}


# ───────────────────────── helpers ─────────────────────────

def scoped(q, column, business: str):
    return q if business == "*" else q.where(column == business)


def page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE)), max(0, offset)


def own_lead(user_id: str, business: str) -> dict:
    lead = get_lead(user_id)
    if not lead or (business != "*" and lead["business_id"] != business):
        raise HTTPException(404, "Lead not found")
    return lead


def own_followup(followup_id: int, business: str) -> dict:
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or (business != "*" and f["business_id"] != business):
        raise HTTPException(404, "Follow-up not found")
    return f


def day_key(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


# ───────────────────────── overview ─────────────────────────

@router.get("/admin/me")
def me(request: Request, business: str = Depends(admin_scope)):
    if business == "*":
        return {"business": "*", "name": "LeadPilot owner", "role": "Owner"}
    d = tenants.get(business) or {}
    return {"business": business, "name": d.get("name", business), "role": "Dealer", "category": d.get("category"),
            "widget_key": d.get("widget_key"), "plan": tenants.summary(d) if d else None}


# ───────────────────────── plan, usage and alerts (dealer view) ─────────────────────────

@router.get("/admin/usage")
def my_usage(dealer: str | None = None, business: str = Depends(admin_scope)):
    """This month's usage meter, recent months and alerts. The owner can look at any dealer with ?dealer=."""
    target = dealer if business == "*" and dealer else business
    d = tenants.get(target)
    if d is None:
        raise HTTPException(404, "Dealer not found")
    history = rows(select(usage).where(usage.c.business_id == target).order_by(usage.c.month.desc()).limit(12))
    for h in history:
        h["cost_usd"] = round((h["tokens_in"] or 0) / 1e6 * config.PRICE_IN_PER_M + (h["tokens_out"] or 0) / 1e6 * config.PRICE_OUT_PER_M, 4)
    return {"summary": tenants.summary(d), "history": history, "plans": config.PLANS,
            "alerts": rows(select(alerts).where(alerts.c.business_id == target).order_by(alerts.c.id.desc()).limit(20))}


@router.post("/admin/alerts/read")
def read_alerts(business: str = Depends(admin_scope)):
    if business != "*":
        run(update(alerts).where(and_(alerts.c.business_id == business, alerts.c.read == 0)).values(read=1))
    return {"ok": True}


@router.get("/admin/stats")
def stats(business: str = Depends(admin_scope)):
    """KPIs, pipeline funnel and a 14-day activity series, all aggregated in the database."""
    counts = rows(scoped(select(leads.c.score, leads.c.status, func.count().label("n")), leads.c.business_id, business)
                  .group_by(leads.c.score, leads.c.status))
    total = sum(c["n"] for c in counts)
    by = lambda field, value: sum(c["n"] for c in counts if c[field] == value)

    log_q = select(func.count().label("messages"), func.coalesce(func.sum(chat_log.c.blocked), 0).label("blocked"),
                   func.coalesce(func.sum(chat_log.c.tokens_in), 0).label("tin"),
                   func.coalesce(func.sum(chat_log.c.tokens_out), 0).label("tout"),
                   func.avg(chat_log.c.latency_ms).label("lat"))
    if business != "*":
        log_q = log_q.select_from(chat_log.join(leads, leads.c.user_id == chat_log.c.user_id)).where(leads.c.business_id == business)
    lg = one(log_q)
    first = one(scoped(select(func.avg(leads.c.first_response_ms).label("v")), leads.c.business_id, business))["v"]
    nbook = one(scoped(select(func.count().label("n")).where(bookings.c.status == "confirmed"),
                       bookings.c.business_id, business))["n"]
    upcoming = one(scoped(select(func.count().label("n")).where(and_(bookings.c.status == "confirmed",
                                                                       bookings.c.date >= today_pkt().isoformat())),
                          bookings.c.business_id, business))["n"]
    pending = one(scoped(select(func.count().label("n")).where(followups.c.status == "draft"),
                         followups.c.business_id, business))["n"]

    since = now() - timedelta(days=13)
    day = func.date(leads.c.created_at)
    per_day = {day_key(r["d"]): r["n"] for r in rows(
        scoped(select(day.label("d"), func.count().label("n")).where(leads.c.created_at >= since), leads.c.business_id, business)
        .group_by(day))}
    mday = func.date(chat_log.c.created_at)
    mq = select(mday.label("d"), func.count().label("n")).where(chat_log.c.created_at >= since)
    if business != "*":
        mq = mq.select_from(chat_log.join(leads, leads.c.user_id == chat_log.c.user_id)).where(leads.c.business_id == business)
    msg_day = {day_key(r["d"]): r["n"] for r in rows(mq.group_by(mday))}
    start = since.date()
    series = [{"date": (start + timedelta(days=i)).isoformat(),
               "leads": per_day.get((start + timedelta(days=i)).isoformat(), 0),
               "messages": msg_day.get((start + timedelta(days=i)).isoformat(), 0)} for i in range(14)]

    tin, tout = int(lg["tin"] or 0), int(lg["tout"] or 0)
    return {
        "leads": total, "hot": by("score", "hot"), "warm": by("score", "warm"), "cold": by("score", "cold"),
        "funnel": {s: by("status", s) for s in ("new", "qualified", "booked", "lost")},
        "bookings": nbook, "upcoming_bookings": upcoming,
        "conversion_pct": round(100 * by("status", "booked") / total, 1) if total else 0,
        "avg_first_response_s": round(float(first) / 1000, 1) if first else None,
        "avg_reply_s": round(float(lg["lat"]) / 1000, 1) if lg["lat"] else None,
        "messages": int(lg["messages"] or 0), "blocked": int(lg["blocked"] or 0),
        "tokens_in": tin, "tokens_out": tout,
        "cost_usd": round(tin / 1e6 * config.PRICE_IN_PER_M + tout / 1e6 * config.PRICE_OUT_PER_M, 4),
        "pending_followups": pending, "series": series,
    }


# ───────────────────────── leads ─────────────────────────

@router.get("/admin/leads")
def list_leads(limit: int = 100, offset: int = 0, q: str | None = None,
               status: Literal["new", "qualified", "booked", "lost"] | None = None,
               score: Literal["hot", "warm", "cold"] | None = None, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    query = scoped(select(leads), leads.c.business_id, business)
    if status:
        query = query.where(leads.c.status == status)
    if score:
        query = query.where(leads.c.score == score)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        query = query.where(or_(leads.c.name.ilike(like), leads.c.phone.ilike(like), leads.c.user_id.ilike(like),
                                leads.c.preferences.ilike(like)))
    return rows(query.order_by(leads.c.last_customer_at.desc().nullslast(), leads.c.created_at.desc())
                .limit(limit).offset(offset))


@router.get("/admin/leads/{user_id}")
def lead_detail(user_id: str, business: str = Depends(admin_scope)):
    lead = own_lead(user_id, business)
    tools = rows(select(audit_log.c.tool, audit_log.c.args, audit_log.c.created_at)
                 .where(and_(audit_log.c.user_id == user_id, ~audit_log.c.tool.startswith("event:")))
                 .order_by(audit_log.c.id.desc()).limit(40))
    lead_followups = rows(select(followups.c.id, followups.c.step, followups.c.message, followups.c.status,
                                 followups.c.created_at, followups.c.sent_at)
                          .where(followups.c.user_id == user_id).order_by(followups.c.id.desc()))
    return {"lead": lead, "memory": profile_of(user_id),
            "conversations": {t: thread_messages(user_id, t) for t in thread_ids(user_id)},
            "bookings": bookings_of(user_id), "followups": lead_followups, "tool_audit": tools}


class LeadStatusIn(BaseModel):
    status: Literal["new", "qualified", "booked", "lost"]


@router.post("/admin/leads/{user_id}/status")
def set_lead_status(user_id: str, body: LeadStatusIn, business: str = Depends(admin_scope)):
    own_lead(user_id, business)
    upsert_lead(user_id, status=body.status)
    log_event("admin_lead_status", "", {"business": business, "status": body.status})
    return {"ok": True}


@router.delete("/admin/leads/{user_id}")
def delete_lead(user_id: str, business: str = Depends(admin_scope)):
    """Handle a customer's data deletion request."""
    own_lead(user_id, business)
    erase_customer(user_id)
    log_event("admin_deleted_lead", "", {"business": business})
    return {"deleted": True}


# ───────────────────────── bookings ─────────────────────────

@router.get("/admin/bookings")
def list_bookings(limit: int = 100, offset: int = 0, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    q = (select(bookings, listings.c.title, listings.c.location, leads.c.name, leads.c.phone)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .join(leads, leads.c.user_id == bookings.c.user_id, isouter=True))
    return rows(scoped(q, bookings.c.business_id, business).order_by(bookings.c.date, bookings.c.time)
                .limit(limit).offset(offset))


class BookingStatusIn(BaseModel):
    status: Literal["confirmed", "cancelled", "completed"]


@router.post("/admin/bookings/{booking_id}/status")
def set_booking_status(booking_id: int, body: BookingStatusIn, business: str = Depends(admin_scope)):
    b = one(select(bookings).where(bookings.c.id == booking_id))
    if not b or (business != "*" and b["business_id"] != business):
        raise HTTPException(404, "Booking not found")
    run(update(bookings).where(bookings.c.id == booking_id).values(status=body.status))
    log_event("admin_booking_status", "", {"business": business, "booking": booking_id, "status": body.status})
    return {"ok": True}


# ───────────────────────── follow-ups ─────────────────────────

@router.get("/admin/followups")
def list_followups(limit: int = 100, offset: int = 0, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    q = (select(followups, leads.c.name, leads.c.phone, leads.c.score)
         .join(leads, leads.c.user_id == followups.c.user_id, isouter=True))
    return rows(scoped(q, followups.c.business_id, business).order_by(followups.c.id.desc()).limit(limit).offset(offset))


@router.post("/admin/followups/run")
def run_followup_agent(ignore_wait: bool = False, business: str = Depends(admin_scope)):
    """Draft follow-ups now (max 20 per run). ignore_wait=true skips the 1/3/7-day wait (for demos)."""
    enforce("followup_run", business, 10, 3600)
    log_event("admin_run_followups", "", {"business": business})
    return run_followups(rt.graph, rt.model, thread_key, ignore_wait=ignore_wait, business=business)


class ApproveIn(BaseModel):
    message: str | None = Field(default=None, max_length=1000)


@router.post("/admin/followups/{followup_id}/approve")
def approve_followup(followup_id: int, body: ApproveIn | None = None, business: str = Depends(admin_scope)):
    own_followup(followup_id, business)
    res = send_followup(followup_id, rt.graph, thread_key, body.message if body else None)
    if not res:
        raise HTTPException(409, "Already handled")
    log_event("admin_followup_sent", "", {"business": business, "followup": followup_id})
    return res


@router.post("/admin/followups/{followup_id}/skip")
def skip(followup_id: int, business: str = Depends(admin_scope)):
    own_followup(followup_id, business)
    return {"ok": skip_followup(followup_id)}


# ───────────────────────── LeadPilot team only ─────────────────────────

@router.get("/admin/pilot-requests")
def list_pilot_requests(_: str = Depends(super_admin)):
    return rows(select(pilot_requests).order_by(pilot_requests.c.id.desc()).limit(MAX_PAGE))


class PilotStatusIn(BaseModel):
    status: Literal["new", "contacted", "closed"]


@router.post("/admin/pilot-requests/{request_id}/status")
def set_pilot_status(request_id: int, body: PilotStatusIn, _: str = Depends(super_admin)):
    run(update(pilot_requests).where(pilot_requests.c.id == request_id).values(status=body.status))
    return {"ok": True}


@router.post("/cron/followups")
def cron_followups(request: Request, x_cron_secret: str = Header(default="")):
    """Called daily by GitHub Actions. Only drafts; a human approves in the console."""
    enforce("cron_ip", client_ip(request), 10, 3600)
    if not config.CRON_SECRET or not hmac.compare_digest(x_cron_secret, config.CRON_SECRET):
        log_event("cron_auth_failed")
        raise HTTPException(401, "Bad cron secret")
    return run_followups(rt.graph, rt.model, thread_key)
