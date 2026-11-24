"""LeadPilot API (FastAPI).

Graded endpoints (open, no login, as the assignment requires):
  POST /chat    {"user_id","thread_id","message"} -> {"reply","tool_calls","tokens":{"in","out"}}
  GET  /health  -> ok
Customer endpoints: /listings, /customer/session, /leads, /customer/... (signed customer token)
Dealer endpoints:   /admin/login, then /admin/... with "Authorization: Bearer <session>"
"""
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, select, update

from app import config
from app.agent import build_model
from app.db import (audit_log, bookings, businesses, chat_log, delete_customer, followups, get_lead, init_db, leads,
                    listings, lock_down_supabase, now, one, rows, run, upsert_lead)
from app.followups import run_followups, send_followup, skip_followup
from app.graph import build_graph, build_persistence, thread_key
from app.security import (ID_PATTERN, SITE_USER_PREFIX, admin_login, check_customer, client_ip, customer_token,
                          enforce, hit, log_event, new_customer, peek, revoke_admin, verify_admin)
from app.seed import seed
from app.tools import BUSINESS_FOR, LeadContext

log = logging.getLogger("leadpilot")
STATE: dict = {}
MAX_PAGE = 200


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    lock_down_supabase()
    seed()
    saver, store = build_persistence()
    STATE.update(graph=build_graph(saver, store), saver=saver, store=store, model=build_model())
    if not config.admin_tokens():
        log.warning("ADMIN_TOKENS is not set: the dealer dashboard is disabled")
    yield


app = FastAPI(title="LeadPilot API (DealCloser)", version="1.1", lifespan=lifespan,
              docs_url="/docs" if config.ENABLE_DOCS else None,
              redoc_url="/redoc" if config.ENABLE_DOCS else None,
              openapi_url="/openapi.json" if config.ENABLE_DOCS else None)
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["Content-Type", "Authorization", "X-User-Token"])


# ───────────────────────── graded endpoints ─────────────────────────

class ChatIn(BaseModel):
    user_id: str = Field(pattern=ID_PATTERN)
    thread_id: str = Field(pattern=ID_PATTERN)
    message: str = Field(max_length=config.MAX_MESSAGE_CHARS)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/") if (FRONTEND / "index.html").exists() else PlainTextResponse("LeadPilot API")


def graded(reply: str) -> dict:
    return {"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}}


def run_turn(user_id: str, thread_id: str, message: str) -> dict:
    """One conversation turn through the graph. Always returns the graded response shape."""
    graph = STATE["graph"]
    cfg = {"configurable": {"thread_id": thread_key(user_id, thread_id)}}
    ctx = LeadContext(user_id=user_id, thread_id=thread_id)
    start = time.perf_counter()
    try:
        before = len(graph.get_state(cfg).values.get("messages", []))
        out = graph.invoke({"messages": [HumanMessage(message)]}, cfg, context=ctx)
        new = out["messages"][before:]
        ai = [m for m in new if isinstance(m, AIMessage)]
        reply = next((m.text for m in reversed(ai) if m.text.strip()), "")
        if not reply:
            reply = "Sorry, could you say that again? Are you looking for a property or a car?"
        tool_calls = [{"name": c["name"], "args": c["args"]} for m in ai for c in (m.tool_calls or [])]
        tokens = {"in": sum((m.usage_metadata or {}).get("input_tokens", 0) for m in ai),
                  "out": sum((m.usage_metadata or {}).get("output_tokens", 0) for m in ai)}
        blocked = bool(out.get("blocked"))
    except Exception:  # reliability + audit 5.6: generic message, details only in server logs
        log.exception("chat failed")
        reply = "Sorry, I hit a temporary problem. Please send your message again."
        tool_calls, tokens, blocked = [], {"in": 0, "out": 0}, False
    latency = int((time.perf_counter() - start) * 1000)
    try:
        run(insert(chat_log).values(user_id=user_id, thread_id=thread_id, message=message, reply=reply,
                                    tool_calls=tool_calls, blocked=int(blocked), tokens_in=tokens["in"],
                                    tokens_out=tokens["out"], latency_ms=latency, created_at=now()))
    except Exception:
        log.exception("chat log failed")
    return {"reply": reply, "tool_calls": tool_calls, "tokens": tokens, "latency_ms": latency}


@app.post("/chat")
def chat(req: ChatIn, request: Request, x_user_token: str | None = Header(default=None)):
    # Ids issued by our website are bound to a signed token, so nobody can talk "as" a real website customer.
    # Other ids stay open: the assignment's grader calls /chat without login (accepted, documented risk).
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
    r = run_turn(req.user_id, req.thread_id, req.message)
    return {"reply": r["reply"], "tool_calls": r["tool_calls"], "tokens": r["tokens"]}


# ───────────────────────── customer side ─────────────────────────

@app.get("/listings")
def get_listings(category: str | None = None):
    q = select(listings.c.id, listings.c.category, listings.c.title, listings.c.location, listings.c.price_pkr,
               listings.c.bedrooms, listings.c.year, listings.c.details, businesses.c.name.label("business")
               ).join(businesses, businesses.c.id == listings.c.business_id).where(listings.c.status == "available")
    if category in ("property", "car"):
        q = q.where(listings.c.category == category)
    return rows(q.order_by(listings.c.category.desc(), listings.c.price_pkr))


@app.post("/customer/session")
def customer_session(request: Request):
    """A new anonymous customer: random id + signed token. The website stores both."""
    enforce("session_ip", client_ip(request), 30, 600)
    user_id, token = new_customer()
    return {"user_id": user_id, "token": token}


class LeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=30, pattern=r"^[0-9+()\- ]+$")
    interest: str = Field(pattern="^(property|car)$")
    message: str = Field(default="", max_length=1000)
    listing_id: str | None = Field(default=None, pattern=r"^[A-Z][0-9]{3}$")
    website: str = Field(default="", max_length=200)   # honeypot: real people never fill this hidden field
    elapsed_ms: int = Field(default=0, ge=0)            # time spent on the form; bots submit instantly


@app.post("/leads")
def new_lead(lead: LeadIn, request: Request):
    """Website lead form: creates the customer and the agent replies instantly (the 'within a minute' promise)."""
    ip = client_ip(request)
    enforce("lead_ip", ip, 5, 600)                      # audit 6.1/6.3: max 5 leads per IP per 10 minutes
    if lead.website or lead.elapsed_ms < 1500:          # audit 6.3: honeypot + minimum fill time
        log_event("lead_rejected_bot", "", {"reason": "honeypot" if lead.website else "too_fast"})
        raise HTTPException(400, "Please try again.")
    user_id, token = new_customer()
    thread_id = "t1"
    upsert_lead(user_id, name=lead.name.strip(), phone=lead.phone.strip(), category=lead.interest, source="web form",
                business_id=BUSINESS_FOR[lead.interest])
    STATE["store"].put(("leads", user_id), "profile", {"name": lead.name.strip(), "phone": lead.phone.strip(),
                                                       "interested_in": lead.interest})
    about = f" about listing {lead.listing_id}" if lead.listing_id else ""
    first = (f"Hi, I'm {lead.name.strip()}. I'm interested in a {lead.interest}{about}. "
             f"{lead.message.strip()}").strip()
    r = run_turn(user_id, thread_id, first)
    upsert_lead(user_id, first_response_ms=r["latency_ms"])
    return {"user_id": user_id, "token": token, "thread_id": thread_id, "first_message": first,
            "reply": r["reply"], "response_ms": r["latency_ms"]}


def _visible(messages) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "text": m.text})
        elif isinstance(m, AIMessage) and m.text.strip():
            out.append({"role": "assistant", "text": m.text, "kind": m.name or "closer"})
    return out


def _thread_ids(user_id: str) -> list[str]:
    item = STATE["store"].get(("leads", user_id), "activity")
    return item.value.get("threads", []) if item else []


@app.get("/customer/{user_id}/threads/{thread_id}")
def customer_thread(user_id: str, thread_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    cfg = {"configurable": {"thread_id": thread_key(user_id, thread_id)}}
    return {"messages": _visible(STATE["graph"].get_state(cfg).values.get("messages", []))}


@app.get("/customer/{user_id}/threads")
def customer_threads(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return {"threads": _thread_ids(user_id)}


def _bookings_of(user_id: str) -> list[dict]:
    q = (select(bookings.c.id, bookings.c.listing_id, bookings.c.kind, bookings.c.date, bookings.c.time,
                bookings.c.status, listings.c.title, listings.c.location)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .where(bookings.c.user_id == user_id).order_by(bookings.c.date, bookings.c.time))
    return rows(q)


@app.get("/customer/{user_id}/bookings")
def customer_bookings(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    return _bookings_of(user_id)


@app.get("/customer/{user_id}/profile")
def customer_profile(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    item = STATE["store"].get(("leads", user_id), "profile")
    return {"profile": item.value if item else {}}


def erase_customer(user_id: str):
    """Audit 12.3: delete everything we hold about one customer: business rows, memory and chat history."""
    for t in _thread_ids(user_id) + ["t1"]:
        try:
            STATE["saver"].delete_thread(thread_key(user_id, t))
        except Exception:
            log.exception("could not delete a thread")
    for key in ("profile", "activity"):
        STATE["store"].delete(("leads", user_id), key)
    delete_customer(user_id)


@app.delete("/customer/{user_id}")
def customer_delete(user_id: str, x_user_token: str | None = Header(default=None)):
    check_customer(user_id, x_user_token)
    erase_customer(user_id)
    log_event("customer_deleted_own_data", "")
    return {"deleted": True}


# ───────────────────────── dealer dashboard ─────────────────────────

class LoginIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@app.post("/admin/login")
def admin_login_route(body: LoginIn, request: Request):
    ip = client_ip(request)
    # audit 2.4: lockout after 10 FAILED attempts per IP per 15 minutes (successful logins don't count)
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


@app.post("/admin/logout")
def admin_logout(request: Request, business: str = Depends(admin_scope)):
    revoke_admin(request.state.admin_jti)
    log_event("admin_logout", "", {"business": business})
    return {"ok": True}


def scoped(q, column, business: str):
    """Business isolation: a dealer admin only ever sees its own rows."""
    return q if business == "*" else q.where(column == business)


def own_lead(user_id: str, business: str) -> dict:
    lead = get_lead(user_id)
    if not lead or (business != "*" and lead["business_id"] != business):
        raise HTTPException(404, "Lead not found")
    return lead


@app.get("/admin/me")
def admin_me(business: str = Depends(admin_scope)):
    if business == "*":
        return {"business": "*", "name": "All businesses (super admin)"}
    b = one(select(businesses).where(businesses.c.id == business))
    return {"business": business, "name": b["name"] if b else business}


@app.get("/admin/stats")
def admin_stats(business: str = Depends(admin_scope)):
    """Aggregates computed in the database (no full-table loads)."""
    lead_q = scoped(select(leads.c.score, leads.c.status, func.count().label("n")), leads.c.business_id, business)
    counts = rows(lead_q.group_by(leads.c.score, leads.c.status))
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
    nbook = one(scoped(select(func.count().label("n")).where(bookings.c.status != "cancelled"), bookings.c.business_id, business))["n"]
    pending = one(scoped(select(func.count().label("n")).where(followups.c.status == "draft"), followups.c.business_id, business))["n"]
    tin, tout = int(lg["tin"] or 0), int(lg["tout"] or 0)
    return {
        "leads": total, "hot": by("score", "hot"), "warm": by("score", "warm"), "cold": by("score", "cold"),
        "booked_leads": by("status", "booked"), "bookings": nbook,
        "conversion_pct": round(100 * by("status", "booked") / total, 1) if total else 0,
        "avg_first_response_s": round(float(first) / 1000, 1) if first else None,
        "median_reply_s": round(float(lg["lat"]) / 1000, 1) if lg["lat"] else None,
        "messages": int(lg["messages"] or 0), "blocked": int(lg["blocked"] or 0),
        "tokens_in": tin, "tokens_out": tout,
        "cost_usd": round(tin / 1e6 * config.PRICE_IN_PER_M + tout / 1e6 * config.PRICE_OUT_PER_M, 4),
        "pending_followups": pending,
    }


def page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE)), max(0, offset)


@app.get("/admin/leads")
def admin_leads(limit: int = 100, offset: int = 0, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    q = scoped(select(leads), leads.c.business_id, business)
    return rows(q.order_by(leads.c.created_at.desc()).limit(limit).offset(offset))


@app.get("/admin/leads/{user_id}")
def admin_lead_detail(user_id: str, business: str = Depends(admin_scope)):
    lead = own_lead(user_id, business)
    profile = STATE["store"].get(("leads", user_id), "profile")
    convo = {t: _visible(STATE["graph"].get_state({"configurable": {"thread_id": thread_key(user_id, t)}})
                         .values.get("messages", [])) for t in _thread_ids(user_id)}
    tools = rows(select(audit_log.c.tool, audit_log.c.args, audit_log.c.created_at)
                 .where(and_(audit_log.c.user_id == user_id, ~audit_log.c.tool.startswith("event:")))
                 .order_by(audit_log.c.id.desc()).limit(30))
    return {"lead": lead, "memory": profile.value if profile else {}, "conversations": convo,
            "bookings": _bookings_of(user_id), "tool_audit": tools}


@app.delete("/admin/leads/{user_id}")
def admin_delete_lead(user_id: str, business: str = Depends(admin_scope)):
    """Handle a customer's data deletion request (audit 12.3)."""
    own_lead(user_id, business)
    erase_customer(user_id)
    log_event("admin_deleted_lead", "", {"business": business})
    return {"deleted": True}


@app.get("/admin/bookings")
def admin_bookings(limit: int = 100, offset: int = 0, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    q = (select(bookings, listings.c.title, leads.c.name, leads.c.phone)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .join(leads, leads.c.user_id == bookings.c.user_id, isouter=True))
    return rows(scoped(q, bookings.c.business_id, business).order_by(bookings.c.date, bookings.c.time)
                .limit(limit).offset(offset))


class StatusIn(BaseModel):
    status: str = Field(pattern="^(confirmed|cancelled|completed)$")


@app.post("/admin/bookings/{booking_id}/status")
def admin_booking_status(booking_id: int, body: StatusIn, business: str = Depends(admin_scope)):
    b = one(select(bookings).where(bookings.c.id == booking_id))
    if not b or (business != "*" and b["business_id"] != business):
        raise HTTPException(404, "Booking not found")
    run(update(bookings).where(bookings.c.id == booking_id).values(status=body.status))
    log_event("admin_booking_status", "", {"business": business, "booking": booking_id, "status": body.status})
    return {"ok": True}


class LeadStatusIn(BaseModel):
    status: str = Field(pattern="^(new|qualified|booked|lost)$")


@app.post("/admin/leads/{user_id}/status")
def admin_lead_status(user_id: str, body: LeadStatusIn, business: str = Depends(admin_scope)):
    own_lead(user_id, business)
    upsert_lead(user_id, status=body.status)
    log_event("admin_lead_status", "", {"business": business, "status": body.status})
    return {"ok": True}


@app.get("/admin/followups")
def admin_followups(limit: int = 100, offset: int = 0, business: str = Depends(admin_scope)):
    limit, offset = page(limit, offset)
    q = (select(followups, leads.c.name, leads.c.phone, leads.c.score)
         .join(leads, leads.c.user_id == followups.c.user_id, isouter=True))
    return rows(scoped(q, followups.c.business_id, business).order_by(followups.c.id.desc()).limit(limit).offset(offset))


@app.post("/admin/followups/run")
def admin_run_followups(ignore_wait: bool = False, business: str = Depends(admin_scope)):
    """Draft follow-ups now (capped per run). ignore_wait=true skips the 1/3/7-day wait (for demos)."""
    enforce("followup_run", business, 10, 3600)        # audit 11.2: bounded AI spend
    log_event("admin_run_followups", "", {"business": business})
    return run_followups(STATE["graph"], STATE["model"], thread_key, ignore_wait=ignore_wait, business=business)


class ApproveIn(BaseModel):
    message: str | None = Field(default=None, max_length=1000)


def own_followup(followup_id: int, business: str) -> dict:
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or (business != "*" and f["business_id"] != business):
        raise HTTPException(404, "Follow-up not found")
    return f


@app.post("/admin/followups/{followup_id}/approve")
def admin_approve(followup_id: int, body: ApproveIn | None = None, business: str = Depends(admin_scope)):
    own_followup(followup_id, business)
    res = send_followup(followup_id, STATE["graph"], thread_key, body.message if body else None)
    if not res:
        raise HTTPException(409, "Already handled")
    log_event("admin_followup_sent", "", {"business": business, "followup": followup_id})
    return res


@app.post("/admin/followups/{followup_id}/skip")
def admin_skip(followup_id: int, business: str = Depends(admin_scope)):
    own_followup(followup_id, business)
    return {"ok": skip_followup(followup_id)}


@app.post("/cron/followups")
def cron_followups(request: Request, x_cron_secret: str = Header(default="")):
    """Called daily by GitHub Actions. Only drafts; a human approves in the dashboard."""
    import hmac
    enforce("cron_ip", client_ip(request), 10, 3600)
    if not config.CRON_SECRET or not hmac.compare_digest(x_cron_secret, config.CRON_SECRET):
        log_event("cron_auth_failed")
        raise HTTPException(401, "Bad cron secret")
    return run_followups(STATE["graph"], STATE["model"], thread_key)


@app.exception_handler(Exception)
async def unhandled(request, exc):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"error": "Something went wrong. Please try again."})


# Serve the website from the API too (handy locally). Only the frontend folder is exposed (audit 10.4).
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND.exists():
    app.mount("/ui", StaticFiles(directory=FRONTEND, html=True), name="ui")
