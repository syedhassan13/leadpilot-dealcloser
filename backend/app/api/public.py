"""Public endpoints: the graded agent API, the listings catalogue, new customers, leads and pilot requests."""
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Path, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from app import config
from app.db import businesses, listings, now, one, pilot_requests, rows, run, upsert_lead
from app.runtime import rt
from app.security import (ID_PATTERN, SITE_USER_PREFIX, check_customer, client_ip, enforce, hit, log_event,
                          new_customer)
from app.services import run_turn
from app.tools import BUSINESS_FOR, SLOTS, free_slots, today_pkt

router = APIRouter()
Dealer = Literal["skyline", "autohub"]


# ───────────────────────── graded agent API (open by assignment) ─────────────────────────

class ChatIn(BaseModel):
    user_id: str = Field(pattern=ID_PATTERN)
    thread_id: str = Field(pattern=ID_PATTERN)
    message: str = Field(max_length=config.MAX_MESSAGE_CHARS)
    dealer: Dealer | None = None       # optional: the widget on one dealer's site only shows that dealer's stock


class ChatOut(BaseModel):
    reply: str
    tool_calls: list[dict]
    tokens: dict[str, int]


def graded(reply: str) -> dict:
    return {"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}}


@router.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@router.post("/chat", response_model=ChatOut)
def chat(req: ChatIn, request: Request, x_user_token: str | None = Header(default=None)):
    # Website-issued ids are bound to a signed token; other ids stay open because the grader calls /chat
    # without login (documented, accepted risk).
    if req.user_id.startswith(SITE_USER_PREFIX):
        check_customer(req.user_id, x_user_token)
    ip = client_ip(request)
    if not (hit("chat_user_min", req.user_id, config.CHAT_PER_USER_PER_MIN, 60)
            and hit("chat_ip_min", ip, config.CHAT_PER_IP_PER_MIN, 60)
            and hit("chat_user_day", req.user_id, config.CHAT_PER_USER_PER_DAY, 86400)):
        return graded("You're sending messages very quickly. Please wait a minute and try again.")
    if not hit("ai_global_day", "all", config.AI_TURNS_PER_DAY, 86400):
        log_event("ai_daily_cap_reached", req.user_id)
        return graded("Our assistant is busy right now. A sales officer will contact you shortly.")
    r = run_turn(req.user_id, req.thread_id, req.message, req.dealer)
    return {"reply": r["reply"], "tool_calls": r["tool_calls"], "tokens": r["tokens"]}


# ───────────────────────── catalogue ─────────────────────────

LISTING_FIELDS = (listings.c.id, listings.c.business_id, listings.c.category, listings.c.title, listings.c.location,
                  listings.c.price_pkr, listings.c.bedrooms, listings.c.year, listings.c.details,
                  businesses.c.name.label("business"))


@router.get("/listings")
def get_listings(category: Literal["property", "car"] | None = None, dealer: Dealer | None = None):
    q = (select(*LISTING_FIELDS).join(businesses, businesses.c.id == listings.c.business_id)
         .where(listings.c.status == "available"))
    if category:
        q = q.where(listings.c.category == category)
    if dealer:
        q = q.where(listings.c.business_id == dealer)
    return rows(q.order_by(listings.c.category.desc(), listings.c.price_pkr))


@router.get("/listings/{listing_id}/slots")
def listing_slots(listing_id: str = Path(pattern=r"^[A-Za-z][0-9]{3}$"), days: int = 6):
    """Next open visit / test-drive slots, so the showroom can show real availability before chatting."""
    listing = one(select(listings.c.id).where(listings.c.id == listing_id.upper()))
    if not listing:
        raise HTTPException(404, "Listing not found")
    out, day = [], today_pkt()
    while len(out) < max(1, min(days, 14)):
        day += timedelta(days=1)
        d, free, error = free_slots(listing["id"], day.isoformat())
        if not error:
            out.append({"date": d.isoformat(), "weekday": f"{d:%a}", "free": free, "all": SLOTS})
    return out


@router.get("/dealers")
def dealers():
    return rows(select(businesses.c.id, businesses.c.name, businesses.c.category, businesses.c.city))


# ───────────────────────── customers and leads ─────────────────────────

@router.post("/customer/session")
def customer_session(request: Request):
    """A new anonymous customer: random id + signed token. The widget stores both."""
    enforce("session_ip", client_ip(request), 30, 600)
    user_id, token = new_customer()
    return {"user_id": user_id, "token": token}


class LeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=30, pattern=r"^[0-9+()\- ]+$")
    interest: Literal["property", "car"]
    message: str = Field(default="", max_length=1000)
    listing_id: str | None = Field(default=None, pattern=r"^[A-Z][0-9]{3}$")
    website: str = Field(default="", max_length=200)   # honeypot: real people never fill this hidden field
    elapsed_ms: int = Field(default=0, ge=0)            # time spent on the form; bots submit instantly


def reject_bots(form, kind: str):
    if form.website or form.elapsed_ms < 1500:
        log_event(f"{kind}_rejected_bot", "", {"reason": "honeypot" if form.website else "too_fast"})
        raise HTTPException(400, "Please try again.")


@router.post("/leads")
def new_lead(lead: LeadIn, request: Request):
    """Website call-back form: creates the customer and the agent replies instantly."""
    enforce("lead_ip", client_ip(request), 5, 600)
    reject_bots(lead, "lead")
    user_id, token = new_customer()
    dealer = BUSINESS_FOR[lead.interest]
    upsert_lead(user_id, name=lead.name.strip(), phone=lead.phone.strip(), category=lead.interest,
                source="web form", business_id=dealer)
    rt.store.put(("leads", user_id), "profile", {"name": lead.name.strip(), "phone": lead.phone.strip(),
                                                 "interested_in": lead.interest})
    about = f" about listing {lead.listing_id}" if lead.listing_id else ""
    first = f"Hi, I'm {lead.name.strip()}. I'm interested in a {lead.interest}{about}. {lead.message.strip()}".strip()
    r = run_turn(user_id, "t1", first, dealer)
    upsert_lead(user_id, first_response_ms=r["latency_ms"])
    return {"user_id": user_id, "token": token, "thread_id": "t1", "first_message": first,
            "reply": r["reply"], "response_ms": r["latency_ms"], "dealer": dealer}


class PilotIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    company: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=30, pattern=r"^[0-9+()\- ]+$")
    email: str | None = Field(default=None, max_length=120, pattern=r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
    dealer_type: Literal["property", "car", "both"]
    monthly_leads: Literal["<50", "50-200", "200-1000", "1000+"] | None = None
    message: str = Field(default="", max_length=1000)
    website: str = Field(default="", max_length=200)
    elapsed_ms: int = Field(default=0, ge=0)


@router.post("/pilot-requests", status_code=201)
def request_pilot(body: PilotIn, request: Request):
    """A dealer asks for a pilot from the landing page. Stored for the team; no AI call."""
    enforce("pilot_ip", client_ip(request), 3, 3600)
    reject_bots(body, "pilot")
    run(insert(pilot_requests).values(name=body.name.strip(), company=body.company.strip(), phone=body.phone.strip(),
                                      email=(body.email or "").strip() or None, dealer_type=body.dealer_type,
                                      monthly_leads=body.monthly_leads, message=body.message.strip(),
                                      status="new", created_at=now()))
    log_event("pilot_requested", "", {"dealer_type": body.dealer_type})
    return {"ok": True}
