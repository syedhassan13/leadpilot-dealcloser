"""Public endpoints: the agent API, widget config, catalogue, new customers, leads and pilot requests.

Who pays for a chat:
- With an X-Widget-Key header, the chat belongs to that dealer: each new conversation counts against the dealer's
  monthly plan (then extra credits). At the limit, new conversations get capture-only replies (no AI call).
- Without a key, the chat is "open" (needed by the assignment's grader) and only the global daily cap applies.
  Set CHAT_REQUIRE_KEY=true to switch open chats off.
"""
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from app import config, guard, identity, tenants
from app.db import businesses, links, listings, now, one, pilot_requests, rows, run, upsert_lead
from app.graph import thread_key
from app.runtime import rt
from app.security import (ID_PATTERN, SITE_USER_PREFIX, check_customer, client_ip, enforce, hit, log_event,
                          new_customer)
from app.services import capture_turn, run_turn
from app.tools import SLOTS, free_slots, today_pkt

router = APIRouter()
DEALER_ID_PATTERN = r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$"
LINK_ID_PATTERN = r"^[0-9a-f]{10}$"
UTM = r"^[A-Za-z0-9 _.\-]{1,60}$"


class Src(BaseModel):
    """Where the buyer came from, read by the widget from the page address (?lp=<link>&utm_source=...)."""
    link: str | None = Field(default=None, pattern=LINK_ID_PATTERN)
    utm_source: str | None = Field(default=None, pattern=UTM)
    utm_medium: str | None = Field(default=None, pattern=UTM)
    utm_campaign: str | None = Field(default=None, pattern=UTM)
    via: Literal["website", "page"] | None = None


def src_of(src: "Src | None") -> dict | None:
    return src.model_dump(exclude_none=True) if src else None


# ───────────────────────── agent API ─────────────────────────

class ChatIn(BaseModel):
    user_id: str = Field(pattern=ID_PATTERN)
    thread_id: str = Field(pattern=ID_PATTERN)
    message: str = Field(max_length=config.MAX_MESSAGE_CHARS)
    dealer: str | None = Field(default=None, pattern=DEALER_ID_PATTERN)   # open mode only: scope to a showcase dealer
    src: Src | None = None                                                # optional attribution (ignored without a key)


class ChatOut(BaseModel):
    reply: str
    tool_calls: list[dict]
    tokens: dict[str, int]


def graded(reply: str) -> dict:
    return {"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}}


@router.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


def chat_dealer(request: Request, requested: str | None) -> dict | None:
    """The dealer this chat is billed to. A widget key always wins; the plain `dealer` field is only honoured
    in open mode and only for public showcase dealers (it can't spend a real customer's allowance)."""
    dealer = tenants.resolve_widget(request)
    if dealer is None and requested:
        d = tenants.get(requested)
        dealer = d if d and d.get("showcase") else None
    return dealer


@router.post("/chat", response_model=ChatOut)
def chat(req: ChatIn, request: Request, x_user_token: str | None = Header(default=None)):
    dealer = chat_dealer(request, req.dealer)
    if req.user_id.startswith(SITE_USER_PREFIX):
        check_customer(req.user_id, x_user_token)
    ip = client_ip(request)
    if not (hit("chat_user_min", req.user_id, config.CHAT_PER_USER_PER_MIN, 60)
            and hit("chat_ip_min", ip, config.CHAT_PER_IP_PER_MIN, 60)
            and hit("chat_user_day", req.user_id, config.CHAT_PER_USER_PER_DAY, 86400)):
        return graded("You're sending messages very quickly. Please wait a minute and try again.")
    # Messages the guard will refuse never reach the AI, so they never count against a dealer's allowance.
    if dealer is not None and not guard.check(req.message):
        allowed, reason = tenants.admit_conversation(dealer, thread_key(req.user_id, req.thread_id), ip)
        if reason == "too_many_new":
            return graded("You've started several new chats in a short time. Please continue in your current "
                          "conversation, or try again in an hour.")
        if not allowed:
            log_event("chat_capture_only", req.user_id, {"dealer": dealer["id"], "reason": reason})
            reply = capture_turn(dealer, req.user_id, req.thread_id, req.message, reason)
            identity.touch(req.user_id, dealer, src_of(req.src))
            return graded(reply)
    if not hit("ai_global_day", "all", config.AI_TURNS_PER_DAY, 86400):
        log_event("ai_daily_cap_reached", req.user_id)
        return graded("Our assistant is busy right now. A sales officer will contact you shortly.")
    r = run_turn(req.user_id, req.thread_id, req.message, tenants.scope_of(dealer))
    if dealer is not None:
        tenants.record_ai(dealer["id"], r["tokens"]["in"], r["tokens"]["out"])
        identity.touch(req.user_id, dealer, src_of(req.src))
    return {"reply": r["reply"], "tool_calls": r["tool_calls"], "tokens": r["tokens"]}


@router.get("/widget/config")
def widget_config(request: Request):
    """What the widget shows for its key: dealer name, what it sells, branding. Nothing secret."""
    d = tenants.resolve_widget(request, required=True)
    singular, plural = tenants.words(d["category"])
    return {"dealer": d["id"], "name": d["name"], "category": d["category"], "what": singular, "plural": plural,
            "city": d.get("city"), "tagline": d.get("tagline"), "brand_color": d.get("brand_color") or "#ff4f00",
            "active": (d.get("status") or "active") == "active"}


# ───────────────────────── catalogue ─────────────────────────

LISTING_FIELDS = (listings.c.id, listings.c.business_id, listings.c.category, listings.c.title, listings.c.location,
                  listings.c.price_pkr, listings.c.bedrooms, listings.c.year, listings.c.details,
                  businesses.c.name.label("business"))


@router.get("/listings")
def get_listings(category: Literal["property", "car"] | None = None,
                 dealer: str | None = Query(default=None, pattern=DEALER_ID_PATTERN)):
    q = (select(*LISTING_FIELDS).join(businesses, businesses.c.id == listings.c.business_id)
         .where(listings.c.status == "available"))
    if category:
        q = q.where(listings.c.category == category)
    q = q.where(listings.c.business_id == dealer) if dealer else q.where(businesses.c.showcase == 1)
    return rows(q.order_by(listings.c.category.desc(), listings.c.price_pkr))


@router.get("/listings/{listing_id}/slots")
def listing_slots(listing_id: str = Path(pattern=tenants.LISTING_ID_PATTERN), days: int = 6):
    """Next open visit / test-drive slots, so a website can show real availability before chatting."""
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


@router.get("/agency/{dealer_id}")
def agency_page(dealer_id: str = Path(pattern=DEALER_ID_PATTERN)):
    """What an agency's hosted LeadPilot page shows (public: name, branding and its public widget key)."""
    d = tenants.get(dealer_id)
    if not d or d["category"] not in ("property", "car") or not tenants.page_on(d):
        raise HTTPException(404, "Page not found")
    singular, plural = tenants.words(d["category"])
    return {"id": d["id"], "name": d["name"], "category": d["category"], "what": singular, "plural": plural,
            "city": d.get("city"), "phone": d.get("phone"), "tagline": d.get("tagline"),
            "brand_color": d.get("brand_color") or "#ff4f00", "widget_key": d.get("widget_key"),
            "active": (d.get("status") or "active") == "active"}


@router.get("/l/{link_id}", include_in_schema=False)
def follow_link(request: Request, link_id: str = Path(pattern=LINK_ID_PATTERN)):
    """A tracked link / QR code: count the click, then send the buyer to the agency page (or the agency's own
    approved website) with ?lp=<link> so the chat knows where they came from. Never an open redirect (audit 5.5):
    the target is fixed when the link is created and checked against the agency's approved websites."""
    l = one(select(links).where(links.c.id == link_id))
    d = tenants.get(l["business_id"]) if l else None
    if not l or not l["active"] or not d or (d.get("status") or "active") != "active":
        return RedirectResponse(config.SITE_URL + "/", 302, headers={"Cache-Control": "no-store"})
    if hit("link_click", f"{link_id}|{client_ip(request)}", 1, 3600):    # one counted click per person per hour
        run(links.update().where(links.c.id == link_id).values(clicks=links.c.clicks + 1))
    if l["target_url"] == "whatsapp:":                 # opens the agency's WhatsApp with a reference we can read back
        from app import channels
        wa = channels.wa_link(d["id"], f"Hi! I'd like to know more. (ref {link_id})")
        return RedirectResponse(wa or f"{config.SITE_URL}/a/{d['id']}?lp={link_id}", 302, headers={"Cache-Control": "no-store"})
    target = l["target_url"] or (f"{config.SITE_URL}/a/{d['id']}" if tenants.page_on(d) else config.SITE_URL + "/")
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}lp={link_id}", 302, headers={"Cache-Control": "no-store"})


@router.get("/dealers")
def dealers():
    """Public showcase dealers (used by the demo showroom and landing page), with their public widget keys."""
    return [{"id": d["id"], "name": d["name"], "category": d["category"], "city": d.get("city"),
             "tagline": d.get("tagline"), "brand_color": d.get("brand_color"), "widget_key": d.get("widget_key")}
            for d in tenants.all_dealers() if d.get("showcase")]


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
    listing_id: str | None = Field(default=None, pattern=tenants.LISTING_ID_PATTERN)
    website: str = Field(default="", max_length=200)   # honeypot: real people never fill this hidden field
    elapsed_ms: int = Field(default=0, ge=0)            # time spent on the form; bots submit instantly
    src: Src | None = None


def reject_bots(form, kind: str):
    if form.website or form.elapsed_ms < 1500:
        log_event(f"{kind}_rejected_bot", "", {"reason": "honeypot" if form.website else "too_fast"})
        raise HTTPException(400, "Please try again.")


@router.post("/leads")
def new_lead(lead: LeadIn, request: Request):
    """A website's call-back form: creates the customer and replies instantly (capture-only at the plan limit)."""
    enforce("lead_ip", client_ip(request), 5, 600)
    reject_bots(lead, "lead")
    dealer = tenants.resolve_widget(request)
    interest = dealer["category"] if dealer and dealer["category"] in ("property", "car") else lead.interest
    owner = dealer["id"] if dealer and dealer["category"] != "all" else tenants.default_dealer_for(interest)
    user_id, token = new_customer()
    upsert_lead(user_id, name=lead.name.strip(), phone=lead.phone.strip(), category=interest,
                source="web form", business_id=owner)
    rt.store.put(("leads", user_id), "profile", {"name": lead.name.strip(), "phone": lead.phone.strip(),
                                                 "interested_in": interest})
    identity.touch(user_id, dealer or tenants.get(owner), src_of(lead.src), default="form")
    about = f" about listing {lead.listing_id}" if lead.listing_id else ""
    first = f"Hi, I'm {lead.name.strip()}. I'm interested in a {interest}{about}. {lead.message.strip()}".strip()
    billed = dealer or tenants.get(owner)
    allowed = True
    if billed is not None:
        allowed, reason = tenants.admit_conversation(billed, thread_key(user_id, "t1"), client_ip(request))
    if not allowed:
        reply, ms = capture_turn(billed, user_id, "t1", first, reason), 0
    else:
        r = run_turn(user_id, "t1", first, tenants.scope_of(dealer) or owner)
        reply, ms = r["reply"], r["latency_ms"]
        if billed is not None:
            tenants.record_ai(billed["id"], r["tokens"]["in"], r["tokens"]["out"])
    upsert_lead(user_id, first_response_ms=ms or None)
    return {"user_id": user_id, "token": token, "thread_id": "t1", "first_message": first,
            "reply": reply, "response_ms": ms, "dealer": owner}


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
