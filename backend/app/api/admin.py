"""Agency console endpoints. Login exchanges a console key for a signed session; every other route needs it.

Who can see what (enforced here, on the server, for every route; audit 3.1, 3.3, 3.5):
- owner   (LeadPilot team, business "*"): every agency
- manager (agency key, or a personal manager key): everything in their own agency; manages team, links, settings
- agent   (a salesperson's personal key): only leads assigned to them, and their bookings and follow-ups
Leads merged into another lead (same buyer, same phone) are only shown through their primary lead.
"""
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, insert, or_, select, update

from app import config, identity, team, tenants
from app.db import (alerts, audit_log, bookings, businesses, chat_log, followups, get_lead, leads, links, listings,
                    members, now, one, pilot_requests, rows, run, upsert_lead, usage)
from app.followups import run_followups, send_followup, skip_followup
from app.graph import thread_key
from app.runtime import rt
from app.security import admin_login, client_ip, enforce, hit, log_event, peek, revoke_admin, verify_principal
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
    # lockout after 10 FAILED attempts per IP per 15 minutes (successful logins don't count). Keys are 192-bit
    # random values, so per-account guessing is not a practical attack (audit 2.4).
    if peek("admin_login_fail", ip, 900) >= 10:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    session = admin_login(body.key)
    if not session:
        hit("admin_login_fail", ip, 10, 900)
        log_event("admin_login_failed", "", {"ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:12]})
        raise HTTPException(401, "Invalid key")
    log_event("admin_login", "", {"business": session["business"], "role": session["role"]})
    return session


@dataclass(frozen=True)
class Principal:
    business: str          # "*" for the LeadPilot owner
    role: str              # owner | manager | agent
    member_id: str | None  # a personal team key, else None
    jti: str

    @property
    def is_owner(self):
        return self.business == "*"

    @property
    def is_agent(self):
        return self.role == "agent"


def principal(request: Request, authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Please log in")
    p = verify_principal(authorization[7:].strip())
    request.state.admin_jti = p["jti"]
    return Principal(p["business"], p["role"], p["member_id"], p["jti"])


def admin_scope(p: Principal = Depends(principal)) -> str:
    """The business of the caller (used by the owner router)."""
    return p.business


def super_admin(p: Principal = Depends(principal)) -> str:
    if not p.is_owner:
        raise HTTPException(403, "Only the LeadPilot team can see this")
    return p.business


def manager(p: Principal = Depends(principal)) -> Principal:
    """Agency managers only (team, links, settings, billing). The owner manages agencies from the owner API."""
    if p.is_owner or p.role != "manager":
        raise HTTPException(403, "Only the agency's manager can do this")
    return p


def not_agent(p: Principal = Depends(principal)) -> Principal:
    if p.is_agent:
        raise HTTPException(403, "Ask your manager to do this")
    return p


@router.post("/admin/logout")
def logout(request: Request, p: Principal = Depends(principal)):
    revoke_admin(p.jti)
    log_event("admin_logout", p.member_id or "", {"business": p.business})
    return {"ok": True}


# ───────────────────────── visibility helpers ─────────────────────────

def lead_conds(p: Principal, aliases: bool = False) -> list:
    """Conditions on dc_leads that limit rows to what this caller may see."""
    c = [] if aliases else [leads.c.merged_into.is_(None)]
    if not p.is_owner:
        c.append(leads.c.business_id == p.business)
    if p.is_agent:
        c.append(leads.c.assigned_to == p.member_id)
    return c


def scoped_rows(q, table, p: Principal):
    """Scope bookings / follow-ups: own agency, and for salespeople only their own leads (incl. merged aliases)."""
    if not p.is_owner:
        q = q.where(table.c.business_id == p.business)
    if p.is_agent:
        q = q.where(table.c.user_id.in_(select(leads.c.user_id).where(leads.c.assigned_to == p.member_id)))
    return q


def page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE)), max(0, offset)


def can_see(lead: dict | None, p: Principal) -> bool:
    return bool(lead) and (p.is_owner or lead["business_id"] == p.business) and \
        (not p.is_agent or lead.get("assigned_to") == p.member_id)


def own_lead(user_id: str, p: Principal) -> dict:
    """The lead (its primary, if it was merged) when the caller may see it, else 404 (no existence leak)."""
    lead = get_lead(user_id)
    if lead and lead.get("merged_into"):
        lead = get_lead(lead["merged_into"])
    if not can_see(lead, p):
        raise HTTPException(404, "Lead not found")
    return lead


def own_followup(followup_id: int, p: Principal) -> dict:
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or not can_see(get_lead(f["user_id"]), p):
        raise HTTPException(404, "Follow-up not found")
    return f


def day_key(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


# ───────────────────────── who am I ─────────────────────────

@router.get("/admin/me")
def me(p: Principal = Depends(principal)):
    if p.is_owner:
        return {"business": "*", "name": "LeadPilot owner", "role": "owner", "member": None}
    d = tenants.get(p.business) or {}
    m = team.get(p.business, p.member_id) if p.member_id else None
    out = {"business": p.business, "name": d.get("name", p.business), "role": p.role, "category": d.get("category"),
           "member": team.public(m) if m else None, "assign_mode": d.get("assign_mode") or "manual",
           "page_enabled": tenants.page_on(d)}
    if p.role == "manager":
        out.update(widget_key=d.get("widget_key"), plan=tenants.summary(d) if d else None)
    return out


# ───────────────────────── plan, usage and alerts (managers) ─────────────────────────

@router.get("/admin/usage")
def my_usage(dealer: str | None = None, p: Principal = Depends(not_agent)):
    """This month's usage meter, recent months and agency alerts. The owner can look at any agency with ?dealer=."""
    target = dealer if p.is_owner and dealer else p.business
    d = tenants.get(target)
    if d is None:
        raise HTTPException(404, "Dealer not found")
    history = rows(select(usage).where(usage.c.business_id == target).order_by(usage.c.month.desc()).limit(12))
    for h in history:
        h["cost_usd"] = round((h["tokens_in"] or 0) / 1e6 * config.PRICE_IN_PER_M + (h["tokens_out"] or 0) / 1e6 * config.PRICE_OUT_PER_M, 4)
    return {"summary": tenants.summary(d), "history": history, "plans": config.PLANS,
            "alerts": rows(select(alerts).where(and_(alerts.c.business_id == target, alerts.c.lead_id.is_(None)))
                           .order_by(alerts.c.id.desc()).limit(20))}


@router.post("/admin/alerts/read")
def read_alerts(p: Principal = Depends(not_agent)):
    if not p.is_owner:
        run(update(alerts).where(and_(alerts.c.business_id == p.business, alerts.c.lead_id.is_(None), alerts.c.read == 0))
            .values(read=1))
    return {"ok": True}


def notification_conds(p: Principal) -> list:
    c = [alerts.c.business_id == p.business, alerts.c.lead_id.is_not(None)]
    if p.is_agent:
        c.append(alerts.c.member_id == p.member_id)
    return c


@router.get("/admin/notifications")
def notifications(p: Principal = Depends(principal)):
    """Hot leads and booked visits: salespeople see their own, managers see the whole agency."""
    if p.is_owner:
        return {"items": [], "unread": 0}
    items = rows(select(alerts.c.id, alerts.c.level, alerts.c.message, alerts.c.lead_id, alerts.c.member_id,
                        alerts.c.read, alerts.c.created_at).where(*notification_conds(p))
                 .order_by(alerts.c.id.desc()).limit(30))
    unread = one(select(func.count().label("n")).where(*notification_conds(p), alerts.c.read == 0))["n"]
    return {"items": items, "unread": unread}


@router.post("/admin/notifications/read")
def read_notifications(p: Principal = Depends(principal)):
    if not p.is_owner:
        run(update(alerts).where(*notification_conds(p), alerts.c.read == 0).values(read=1))
    return {"ok": True}


# ───────────────────────── overview ─────────────────────────

@router.get("/admin/stats")
def stats(p: Principal = Depends(principal)):
    """KPIs, pipeline funnel, a 14-day series and where leads come from, aggregated in the database."""
    conds = lead_conds(p)
    counts = rows(select(leads.c.score, leads.c.status, func.count().label("n")).where(*conds)
                  .group_by(leads.c.score, leads.c.status))
    total = sum(c["n"] for c in counts)
    by = lambda field, value: sum(c["n"] for c in counts if c[field] == value)

    seen = select(leads.c.user_id).where(*lead_conds(p, aliases=True))
    log_q = select(func.count().label("messages"), func.coalesce(func.sum(chat_log.c.blocked), 0).label("blocked"),
                   func.coalesce(func.sum(chat_log.c.tokens_in), 0).label("tin"),
                   func.coalesce(func.sum(chat_log.c.tokens_out), 0).label("tout"),
                   func.avg(chat_log.c.latency_ms).label("lat"))
    if not p.is_owner:
        log_q = log_q.where(chat_log.c.user_id.in_(seen))
    lg = one(log_q)
    first = one(select(func.avg(leads.c.first_response_ms).label("v")).where(*conds))["v"]
    nbook = one(scoped_rows(select(func.count().label("n")).where(bookings.c.status == "confirmed"), bookings, p))["n"]
    upcoming = one(scoped_rows(select(func.count().label("n")).where(and_(
        bookings.c.status == "confirmed", bookings.c.date >= today_pkt().isoformat())), bookings, p))["n"]
    pending = one(scoped_rows(select(func.count().label("n")).where(followups.c.status == "draft"), followups, p))["n"]

    since = now() - timedelta(days=13)
    day = func.date(leads.c.created_at)
    per_day = {day_key(r["d"]): r["n"] for r in rows(
        select(day.label("d"), func.count().label("n")).where(leads.c.created_at >= since, *conds).group_by(day))}
    mday = func.date(chat_log.c.created_at)
    mq = select(mday.label("d"), func.count().label("n")).where(chat_log.c.created_at >= since)
    if not p.is_owner:
        mq = mq.where(chat_log.c.user_id.in_(seen))
    msg_day = {day_key(r["d"]): r["n"] for r in rows(mq.group_by(mday))}
    start = since.date()
    series = [{"date": (start + timedelta(days=i)).isoformat(),
               "leads": per_day.get((start + timedelta(days=i)).isoformat(), 0),
               "messages": msg_day.get((start + timedelta(days=i)).isoformat(), 0)} for i in range(14)]

    # where leads come from: channel and tracked link, with how many booked a visit
    booked = func.sum(case((leads.c.status == "booked", 1), else_=0)).label("booked")
    ch = rows(select(func.coalesce(leads.c.channel, "unknown").label("channel"), func.count().label("leads"), booked)
              .where(*conds).group_by(func.coalesce(leads.c.channel, "unknown")))
    lk = rows(select(leads.c.link_id, func.count().label("leads"), booked)
              .where(*conds, leads.c.link_id.is_not(None)).group_by(leads.c.link_id))
    names = {r["id"]: r for r in rows(select(links.c.id, links.c.name, links.c.channel)
                                      .where(links.c.id.in_([x["link_id"] for x in lk] or [""])))}
    by_link = [{"id": x["link_id"], "name": names.get(x["link_id"], {}).get("name", x["link_id"]),
                "channel": names.get(x["link_id"], {}).get("channel"), "leads": x["leads"], "booked": int(x["booked"] or 0)}
               for x in lk]

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
        "by_channel": sorted([{**c, "booked": int(c["booked"] or 0)} for c in ch], key=lambda c: -c["leads"]),
        "by_link": sorted(by_link, key=lambda x: -x["leads"])[:20],
    }


# ───────────────────────── leads ─────────────────────────

@router.get("/admin/leads")
def list_leads(limit: int = 100, offset: int = 0, q: str | None = None,
               status: Literal["new", "qualified", "booked", "lost"] | None = None,
               score: Literal["hot", "warm", "cold"] | None = None,
               channel: str | None = Query(default=None, pattern=r"^[a-z]{2,20}$"),
               assigned: str | None = Query(default=None, pattern=r"^(none|me|m_[0-9a-f]{16})$"),
               p: Principal = Depends(principal)):
    limit, offset = page(limit, offset)
    query = select(leads).where(*lead_conds(p))
    if status:
        query = query.where(leads.c.status == status)
    if score:
        query = query.where(leads.c.score == score)
    if channel:
        query = query.where(leads.c.channel == channel)
    if assigned == "none":
        query = query.where(leads.c.assigned_to.is_(None))
    elif assigned == "me":
        query = query.where(leads.c.assigned_to == p.member_id)
    elif assigned:
        query = query.where(leads.c.assigned_to == assigned)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        digits = re.sub(r"\D", "", q)
        conds = [leads.c.name.ilike(like), leads.c.phone.ilike(like), leads.c.user_id.ilike(like), leads.c.preferences.ilike(like)]
        if len(digits) >= 4:
            conds.append(leads.c.phone_norm.ilike(f"%{digits[-9:]}%"))
        query = query.where(or_(*conds))
    return rows(query.order_by(leads.c.last_customer_at.desc().nullslast(), leads.c.created_at.desc())
                .limit(limit).offset(offset))


@router.get("/admin/leads/{user_id}")
def lead_detail(user_id: str, p: Principal = Depends(principal)):
    lead = own_lead(user_id, p)
    ids = [lead["user_id"]] + identity.aliases_of(lead["user_id"])
    alias_rows = {r["user_id"]: r for r in rows(select(leads.c.user_id, leads.c.channel, leads.c.created_at)
                                                .where(leads.c.user_id.in_(ids[1:] or [""])))}
    conversations = {}
    for uid in ids:
        for t in thread_ids(uid):
            label = t if uid == lead["user_id"] else f"{(alias_rows.get(uid) or {}).get('channel') or 'earlier'} · {t}"
            conversations[label] = thread_messages(uid, t)
    tools = rows(select(audit_log.c.tool, audit_log.c.args, audit_log.c.created_at)
                 .where(and_(audit_log.c.user_id.in_(ids), ~audit_log.c.tool.startswith("event:")))
                 .order_by(audit_log.c.id.desc()).limit(40))
    lead_followups = rows(select(followups.c.id, followups.c.step, followups.c.message, followups.c.status,
                                 followups.c.created_at, followups.c.sent_at)
                          .where(followups.c.user_id.in_(ids)).order_by(followups.c.id.desc()))
    memory = {}
    for uid in reversed(ids):
        memory.update(profile_of(uid))
    link = one(select(links.c.id, links.c.name, links.c.channel).where(links.c.id == lead["link_id"])) if lead.get("link_id") else None
    assignee = team.get(lead["business_id"], lead["assigned_to"]) if lead.get("assigned_to") else None
    return {"lead": lead, "memory": memory, "conversations": conversations,
            "bookings": [b for uid in ids for b in bookings_of(uid)], "followups": lead_followups, "tool_audit": tools,
            "source": {"channel": lead.get("channel"), "link": link, "utm": lead.get("utm")},
            "merged": [{"user_id": u, "channel": r.get("channel"), "created_at": r.get("created_at")} for u, r in alias_rows.items()],
            "assignee": team.public(assignee) if assignee else None,
            "team": [team.public(m) for m in team.list_for(lead["business_id"]) if m["active"]] if p.role == "manager" else []}


class LeadStatusIn(BaseModel):
    status: Literal["new", "qualified", "booked", "lost"]


@router.post("/admin/leads/{user_id}/status")
def set_lead_status(user_id: str, body: LeadStatusIn, p: Principal = Depends(principal)):
    lead = own_lead(user_id, p)
    upsert_lead(lead["user_id"], status=body.status)
    log_event("admin_lead_status", p.member_id or "", {"business": lead["business_id"], "status": body.status})
    return {"ok": True}


class AssignIn(BaseModel):
    member_id: str | None = Field(default=None, pattern=r"^m_[0-9a-f]{16}$")


@router.post("/admin/leads/{user_id}/assign")
def assign_lead(user_id: str, body: AssignIn, p: Principal = Depends(manager)):
    lead = own_lead(user_id, p)
    if body.member_id:
        m = team.get(p.business, body.member_id)          # must be an active member of THIS agency
        if not m or not m["active"]:
            raise HTTPException(404, "Team member not found")
    identity.assign(lead["user_id"], body.member_id)
    if body.member_id:
        tenants.alert(p.business, f"assign:{lead['user_id']}:{body.member_id}:{int(now().timestamp())}"[:80], "info",
                      f"New lead for you: {identity._who(lead)}.", member_id=body.member_id, lead_id=lead["user_id"])
    log_event("admin_lead_assigned", p.member_id or "", {"business": p.business, "to": body.member_id})
    return {"ok": True}


@router.delete("/admin/leads/{user_id}")
def delete_lead(user_id: str, p: Principal = Depends(not_agent)):
    """Handle a customer's data deletion request: the lead and every lead merged into it (audit 12.3)."""
    lead = own_lead(user_id, p)
    for uid in [lead["user_id"]] + identity.aliases_of(lead["user_id"]):
        erase_customer(uid)
    log_event("admin_deleted_lead", p.member_id or "", {"business": lead["business_id"]})
    return {"deleted": True}


# ───────────────────────── bookings ─────────────────────────

@router.get("/admin/bookings")
def list_bookings(limit: int = 100, offset: int = 0, p: Principal = Depends(principal)):
    limit, offset = page(limit, offset)
    q = (select(bookings, listings.c.title, listings.c.location, leads.c.name, leads.c.phone,
                func.coalesce(leads.c.merged_into, bookings.c.user_id).label("lead_id"))
         .join(listings, listings.c.id == bookings.c.listing_id)
         .join(leads, leads.c.user_id == bookings.c.user_id, isouter=True))
    return rows(scoped_rows(q, bookings, p).order_by(bookings.c.date, bookings.c.time).limit(limit).offset(offset))


class BookingStatusIn(BaseModel):
    status: Literal["confirmed", "cancelled", "completed"]


@router.post("/admin/bookings/{booking_id}/status")
def set_booking_status(booking_id: int, body: BookingStatusIn, p: Principal = Depends(principal)):
    b = one(select(bookings).where(bookings.c.id == booking_id))
    if not b or not can_see(get_lead(b["user_id"]), p) or (not p.is_owner and b["business_id"] != p.business):
        raise HTTPException(404, "Booking not found")
    run(update(bookings).where(bookings.c.id == booking_id).values(status=body.status))
    log_event("admin_booking_status", p.member_id or "", {"business": b["business_id"], "booking": booking_id, "status": body.status})
    return {"ok": True}


# ───────────────────────── follow-ups ─────────────────────────

@router.get("/admin/followups")
def list_followups(limit: int = 100, offset: int = 0, p: Principal = Depends(principal)):
    limit, offset = page(limit, offset)
    q = (select(followups, leads.c.name, leads.c.phone, leads.c.score)
         .join(leads, leads.c.user_id == followups.c.user_id, isouter=True))
    return rows(scoped_rows(q, followups, p).order_by(followups.c.id.desc()).limit(limit).offset(offset))


@router.post("/admin/followups/run")
def run_followup_agent(ignore_wait: bool = False, p: Principal = Depends(not_agent)):
    """Draft follow-ups now (max 20 per run). ignore_wait=true skips the 1/3/7-day wait (for demos)."""
    enforce("followup_run", p.business, 10, 3600)
    log_event("admin_run_followups", p.member_id or "", {"business": p.business})
    return run_followups(rt.graph, rt.model, thread_key, ignore_wait=ignore_wait, business=p.business)


class ApproveIn(BaseModel):
    message: str | None = Field(default=None, max_length=1000)


@router.post("/admin/followups/{followup_id}/approve")
def approve_followup(followup_id: int, body: ApproveIn | None = None, p: Principal = Depends(principal)):
    own_followup(followup_id, p)
    res = send_followup(followup_id, rt.graph, thread_key, body.message if body else None)
    if not res:
        raise HTTPException(409, "Already handled")
    log_event("admin_followup_sent", p.member_id or "", {"business": p.business, "followup": followup_id})
    return res


@router.post("/admin/followups/{followup_id}/skip")
def skip(followup_id: int, p: Principal = Depends(principal)):
    own_followup(followup_id, p)
    return {"ok": skip_followup(followup_id)}


# ───────────────────────── team (managers) ─────────────────────────

class MemberIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=30, pattern=r"^[0-9+()\- ]{5,30}$")
    role: Literal["manager", "agent"] = "agent"


class MemberPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=30, pattern=r"^([0-9+()\- ]{5,30})?$")
    role: Literal["manager", "agent"] | None = None
    active: bool | None = None


@router.get("/admin/team")
def team_list(p: Principal = Depends(manager)):
    counts = {r["assigned_to"]: r for r in rows(
        select(leads.c.assigned_to, func.count().label("leads"),
               func.sum(case((leads.c.status == "booked", 1), else_=0)).label("booked"))
        .where(leads.c.business_id == p.business, leads.c.merged_into.is_(None), leads.c.assigned_to.is_not(None))
        .group_by(leads.c.assigned_to))}
    d = tenants.get(p.business) or {}
    return {"members": [{**team.public(m), "leads": (counts.get(m["id"]) or {}).get("leads", 0),
                         "booked": int((counts.get(m["id"]) or {}).get("booked") or 0)} for m in team.list_for(p.business)],
            "assign_mode": d.get("assign_mode") or "manual", "me": p.member_id}


@router.post("/admin/team", status_code=201)
def team_add(body: MemberIn, p: Principal = Depends(manager)):
    if one(select(func.count().label("n")).where(members.c.business_id == p.business))["n"] >= 50:
        raise HTTPException(422, "An agency can have at most 50 team members.")
    key = team.new_key()
    mid = team.new_id()
    run(insert(members).values(id=mid, business_id=p.business, name=body.name.strip(), phone=(body.phone or "").strip() or None,
                               role=body.role, key_hash=team.hash_key(key), active=1, created_at=now()))
    log_event("team_member_added", p.member_id or "", {"business": p.business, "role": body.role})
    return {"member": team.public(team.get(p.business, mid)), "key": key}   # the key is shown ONCE


@router.post("/admin/team/{member_id}")
def team_update(member_id: str, body: MemberPatch, p: Principal = Depends(manager)):
    m = team.get(p.business, member_id)
    if not m:
        raise HTTPException(404, "Team member not found")
    changes = body.model_dump(exclude_unset=True)
    if member_id == p.member_id and (changes.get("active") is False or changes.get("role") == "agent"):
        raise HTTPException(422, "You can't remove or demote yourself. Ask another manager.")
    if "phone" in changes:
        changes["phone"] = (changes["phone"] or "").strip() or None
    if "active" in changes:
        changes["active"] = int(changes["active"])
    if not changes:
        return {"member": team.public(m)}
    run(update(members).where(and_(members.c.id == member_id, members.c.business_id == p.business)).values(**changes))
    if ("role" in changes and changes["role"] != m["role"]) or changes.get("active") == 0:
        team.sign_out(member_id)                       # new permissions apply from the next sign-in
    if changes.get("active") == 0:                     # their open leads go back to the manager's queue
        run(update(leads).where(and_(leads.c.business_id == p.business, leads.c.assigned_to == member_id,
                                     leads.c.status.in_(["new", "qualified"]))).values(assigned_to=None))
    log_event("team_member_updated", p.member_id or "", {"business": p.business, "fields": sorted(changes)})
    return {"member": team.public(team.get(p.business, member_id))}


@router.post("/admin/team/{member_id}/key")
def team_new_key(member_id: str, p: Principal = Depends(manager)):
    if not team.get(p.business, member_id):
        raise HTTPException(404, "Team member not found")
    key = team.new_key()
    run(update(members).where(and_(members.c.id == member_id, members.c.business_id == p.business))
        .values(key_hash=team.hash_key(key)))
    team.sign_out(member_id)
    log_event("team_member_new_key", p.member_id or "", {"business": p.business})
    return {"key": key}


class SettingsIn(BaseModel):
    assign_mode: Literal["manual", "round_robin"] | None = None
    page_enabled: bool | None = None


@router.post("/admin/settings")
def agency_settings(body: SettingsIn, p: Principal = Depends(manager)):
    changes = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if changes:
        run(update(businesses).where(businesses.c.id == p.business).values(**changes))
        tenants.invalidate()
        log_event("agency_settings", p.member_id or "", {"business": p.business, "fields": sorted(changes)})
    d = tenants.get(p.business) or {}
    return {"assign_mode": d.get("assign_mode") or "manual", "page_enabled": tenants.page_on(d)}


# ───────────────────────── tracked links and QR codes (managers) ─────────────────────────

LINK_CHANNELS = ("qr", "facebook", "instagram", "whatsapp", "google", "tiktok", "youtube", "email", "sms", "print", "portal",
                 "website", "other")


class LinkIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    channel: Literal[LINK_CHANNELS] = "qr"
    target_url: str | None = Field(default=None, max_length=300)


class LinkPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    active: bool | None = None


def link_url(link_id: str) -> str:
    return f"{config.PUBLIC_API_URL}/l/{link_id}"


def safe_target(url: str | None, dealer: dict) -> str | None:
    """Audit 5.5 (no open redirects): a link may only send buyers to the agency page or to one of the agency's
    own websites that the LeadPilot team approved (allowed_origins)."""
    if not url:
        return None
    from urllib.parse import urlsplit
    u = urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise HTTPException(422, "Use a full https:// address of your own website.")
    origin = f"https://{u.hostname}" + (f":{u.port}" if u.port else "")
    allowed = [o.strip().rstrip("/") for o in (dealer.get("allowed_origins") or "").split(",") if o.strip()]
    if origin not in allowed:
        raise HTTPException(422, "That website isn't on your agency's approved list. Ask LeadPilot to add it, "
                                 "or leave the address empty to use your LeadPilot page.")
    return url.strip()


def link_out(l: dict, stats: dict | None = None) -> dict:
    s = stats or {}
    return {**{k: l.get(k) for k in ("id", "name", "channel", "target_url", "clicks", "active", "created_at")},
            "url": link_url(l["id"]), "leads": s.get("leads", 0), "booked": int(s.get("booked") or 0)}


@router.get("/admin/links")
def list_links(p: Principal = Depends(manager)):
    stats_by = {r["link_id"]: r for r in rows(
        select(leads.c.link_id, func.count().label("leads"), func.sum(case((leads.c.status == "booked", 1), else_=0)).label("booked"))
        .where(leads.c.business_id == p.business, leads.c.merged_into.is_(None), leads.c.link_id.is_not(None))
        .group_by(leads.c.link_id))}
    ls = rows(select(links).where(links.c.business_id == p.business).order_by(links.c.active.desc(), links.c.created_at.desc()))
    d = tenants.get(p.business) or {}
    return {"links": [link_out(l, stats_by.get(l["id"])) for l in ls], "page_url": f"{config.SITE_URL}/a/{p.business}",
            "page_enabled": tenants.page_on(d),
            "websites": [o.strip() for o in (d.get("allowed_origins") or "").split(",") if o.strip()]}


@router.post("/admin/links", status_code=201)
def create_link(body: LinkIn, p: Principal = Depends(manager)):
    d = tenants.get(p.business) or {}
    if one(select(func.count().label("n")).where(links.c.business_id == p.business))["n"] >= 500:
        raise HTTPException(422, "Link limit reached (500). Archive old links first.")
    import secrets
    lid = secrets.token_hex(5)                          # 10 hex chars, random (audit 4.2)
    run(insert(links).values(id=lid, business_id=p.business, name=body.name.strip(), channel=body.channel,
                             target_url=safe_target(body.target_url, d), clicks=0, active=1,
                             created_by=p.member_id or "manager", created_at=now()))
    log_event("link_created", p.member_id or "", {"business": p.business, "channel": body.channel})
    return link_out(one(select(links).where(links.c.id == lid)))


@router.post("/admin/links/{link_id}")
def update_link(link_id: str, body: LinkPatch, p: Principal = Depends(manager)):
    changes = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    res = run(update(links).where(and_(links.c.id == link_id, links.c.business_id == p.business)).values(**changes)) if changes else None
    l = one(select(links).where(and_(links.c.id == link_id, links.c.business_id == p.business)))
    if not l or (res is not None and not res.rowcount):
        raise HTTPException(404, "Link not found")
    return link_out(l)


@router.get("/admin/links/{link_id}/qr.svg")
def link_qr(link_id: str, p: Principal = Depends(manager)):
    l = one(select(links.c.id).where(and_(links.c.id == link_id, links.c.business_id == p.business)))
    if not l:
        raise HTTPException(404, "Link not found")
    import io
    import segno
    buf = io.BytesIO()
    segno.make(link_url(l["id"]), error="m").save(buf, kind="svg", scale=8, border=2, dark="#15140f", light="#ffffff",
                                                   xmldecl=False, svgns=True)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Content-Disposition": f'attachment; filename="leadpilot-{l["id"]}.svg"'})


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
