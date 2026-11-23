"""LeadPilot API (FastAPI).

Graded endpoints (open, no login):
  POST /chat    {"user_id","thread_id","message"} -> {"reply","tool_calls","tokens":{"in","out"}}
  GET  /health  -> ok
Customer endpoints: /listings, /leads, /customer/...    Admin endpoints: /admin/... (X-Admin-Token header)
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update

from app import config
from app.agent import build_model
from app.db import (audit_log, bookings, businesses, chat_log, followups, get_lead, init_db, leads, listings, now,
                    one, rows, run, upsert_lead)
from app.followups import run_followups, send_followup, skip_followup
from app.graph import build_graph, build_persistence, thread_key
from app.seed import seed
from app.tools import BUSINESS_FOR, LeadContext

log = logging.getLogger("leadpilot")
STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed()
    saver, store = build_persistence()
    STATE["graph"] = build_graph(saver, store)
    STATE["store"] = store
    STATE["model"] = build_model()
    yield


app = FastAPI(title="LeadPilot API (DealCloser)", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ───────────────────────── graded endpoints ─────────────────────────

class ChatIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=120)
    thread_id: str = Field(min_length=1, max_length=120)
    message: str = Field(max_length=4000)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/") if (FRONTEND / "index.html").exists() else PlainTextResponse("LeadPilot API. See /docs")


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
    except Exception as e:  # reliability: never return a 500 to the caller
        log.exception("chat failed")
        reply = "Sorry, I hit a temporary problem. Please send your message again."
        tool_calls, tokens, blocked = [], {"in": 0, "out": 0}, False
        if "credit balance" in str(e).lower() or "spend" in str(e).lower():
            reply = "Our assistant is temporarily unavailable. A sales officer will contact you shortly."
    latency = int((time.perf_counter() - start) * 1000)
    try:
        run(insert(chat_log).values(user_id=user_id, thread_id=thread_id, message=message[:4000], reply=reply,
                                    tool_calls=tool_calls, blocked=int(blocked), tokens_in=tokens["in"],
                                    tokens_out=tokens["out"], latency_ms=latency, created_at=now()))
    except Exception:
        log.exception("chat log failed")
    return {"reply": reply, "tool_calls": tool_calls, "tokens": tokens, "latency_ms": latency}


@app.post("/chat")
def chat(req: ChatIn):
    r = run_turn(req.user_id.strip(), req.thread_id.strip(), req.message)
    return {"reply": r["reply"], "tool_calls": r["tool_calls"], "tokens": r["tokens"]}


# ───────────────────────── customer side ─────────────────────────

@app.get("/listings")
def get_listings(category: str | None = None):
    q = select(listings, businesses.c.name.label("business")).join(businesses, businesses.c.id == listings.c.business_id)
    if category in ("property", "car"):
        q = q.where(listings.c.category == category)
    return rows(q.order_by(listings.c.category.desc(), listings.c.price_pkr))


class LeadIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=5, max_length=30)
    interest: str = Field(pattern="^(property|car)$")
    message: str = Field(default="", max_length=1000)
    listing_id: str | None = None


@app.post("/leads")
def new_lead(lead: LeadIn):
    """Website lead form: creates the customer and the agent replies instantly (the 'within a minute' promise)."""
    user_id, thread_id = f"web-{uuid.uuid4().hex[:12]}", "t1"
    upsert_lead(user_id, name=lead.name.strip(), phone=lead.phone.strip(), category=lead.interest, source="web form",
                business_id=BUSINESS_FOR[lead.interest])
    store = STATE["store"]
    store.put(("leads", user_id), "profile", {"name": lead.name.strip(), "phone": lead.phone.strip(),
                                              "interested_in": lead.interest})
    about = f" about listing {lead.listing_id}" if lead.listing_id else ""
    first = (f"Hi, I'm {lead.name.strip()}. I'm interested in a {lead.interest}{about}. "
             f"{lead.message.strip()}").strip()
    r = run_turn(user_id, thread_id, first)
    upsert_lead(user_id, first_response_ms=r["latency_ms"])
    return {"user_id": user_id, "thread_id": thread_id, "first_message": first, "reply": r["reply"],
            "response_ms": r["latency_ms"]}


def _visible(messages) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "text": m.text})
        elif isinstance(m, AIMessage) and m.text.strip():
            out.append({"role": "assistant", "text": m.text, "kind": m.name or "closer"})
    return out


@app.get("/customer/{user_id}/threads/{thread_id}")
def customer_thread(user_id: str, thread_id: str):
    """The customer's own conversation (incl. approved follow-ups). Keyed by their own user_id only."""
    cfg = {"configurable": {"thread_id": thread_key(user_id, thread_id)}}
    return {"messages": _visible(STATE["graph"].get_state(cfg).values.get("messages", []))}


@app.get("/customer/{user_id}/threads")
def customer_threads(user_id: str):
    item = STATE["store"].get(("leads", user_id), "activity")
    return {"threads": item.value.get("threads", []) if item else []}


@app.get("/customer/{user_id}/bookings")
def customer_bookings(user_id: str):
    q = (select(bookings, listings.c.title, listings.c.location)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .where(bookings.c.user_id == user_id).order_by(bookings.c.date, bookings.c.time))
    return rows(q)


@app.get("/customer/{user_id}/profile")
def customer_profile(user_id: str):
    item = STATE["store"].get(("leads", user_id), "profile")
    return {"profile": item.value if item else {}}


# ───────────────────────── admin dashboard ─────────────────────────

def admin_scope(x_admin_token: str = Header(default="")) -> str:
    business = config.admin_tokens().get(x_admin_token)
    if not business:
        raise HTTPException(401, "Invalid admin token")
    return business


def scoped(q, column, business: str):
    """Business isolation: a dealer admin only ever sees its own rows."""
    return q if business == "*" else q.where(column == business)


@app.get("/admin/me")
def admin_me(business: str = Depends(admin_scope)):
    if business == "*":
        return {"business": "*", "name": "All businesses (super admin)"}
    b = one(select(businesses).where(businesses.c.id == business))
    return {"business": business, "name": b["name"] if b else business}


@app.get("/admin/stats")
def admin_stats(business: str = Depends(admin_scope)):
    lead_rows = rows(scoped(select(leads), leads.c.business_id, business))
    user_ids = [l["user_id"] for l in lead_rows]
    logs = rows(select(chat_log).where(chat_log.c.user_id.in_(user_ids))) if business != "*" else rows(select(chat_log))
    book = rows(scoped(select(bookings).where(bookings.c.status != "cancelled"), bookings.c.business_id, business))
    tin, tout = sum(l["tokens_in"] or 0 for l in logs), sum(l["tokens_out"] or 0 for l in logs)
    first = [l["first_response_ms"] for l in lead_rows if l["first_response_ms"]]
    lat = sorted(l["latency_ms"] for l in logs if l["latency_ms"])
    return {
        "leads": len(lead_rows),
        "hot": sum(l["score"] == "hot" for l in lead_rows),
        "warm": sum(l["score"] == "warm" for l in lead_rows),
        "cold": sum(l["score"] == "cold" for l in lead_rows),
        "booked_leads": sum(l["status"] == "booked" for l in lead_rows),
        "bookings": len(book),
        "conversion_pct": round(100 * sum(l["status"] == "booked" for l in lead_rows) / len(lead_rows), 1) if lead_rows else 0,
        "avg_first_response_s": round(sum(first) / len(first) / 1000, 1) if first else None,
        "median_reply_s": round(lat[len(lat) // 2] / 1000, 1) if lat else None,
        "messages": len(logs),
        "blocked": sum(l["blocked"] or 0 for l in logs),
        "tokens_in": tin, "tokens_out": tout,
        "cost_usd": round(tin / 1e6 * config.PRICE_IN_PER_M + tout / 1e6 * config.PRICE_OUT_PER_M, 4),
        "pending_followups": len(rows(scoped(select(followups.c.id).where(followups.c.status == "draft"),
                                             followups.c.business_id, business))),
    }


@app.get("/admin/leads")
def admin_leads(business: str = Depends(admin_scope)):
    q = scoped(select(leads), leads.c.business_id, business)
    return rows(q.order_by(leads.c.created_at.desc()))


@app.get("/admin/leads/{user_id}")
def admin_lead_detail(user_id: str, business: str = Depends(admin_scope)):
    lead = get_lead(user_id)
    if not lead or (business != "*" and lead["business_id"] != business):
        raise HTTPException(404, "Lead not found")
    profile = STATE["store"].get(("leads", user_id), "profile")
    activity = STATE["store"].get(("leads", user_id), "activity")
    threads = activity.value.get("threads", []) if activity else []
    convo = {t: _visible(STATE["graph"].get_state({"configurable": {"thread_id": thread_key(user_id, t)}})
                         .values.get("messages", [])) for t in threads}
    tools = rows(select(audit_log).where(audit_log.c.user_id == user_id).order_by(audit_log.c.id.desc()).limit(30))
    return {"lead": lead, "memory": profile.value if profile else {}, "conversations": convo,
            "bookings": customer_bookings(user_id), "tool_audit": tools}


@app.get("/admin/bookings")
def admin_bookings(business: str = Depends(admin_scope)):
    q = (select(bookings, listings.c.title, leads.c.name, leads.c.phone)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .join(leads, leads.c.user_id == bookings.c.user_id, isouter=True))
    return rows(scoped(q, bookings.c.business_id, business).order_by(bookings.c.date, bookings.c.time))


class StatusIn(BaseModel):
    status: str = Field(pattern="^(confirmed|cancelled|completed)$")


@app.post("/admin/bookings/{booking_id}/status")
def admin_booking_status(booking_id: int, body: StatusIn, business: str = Depends(admin_scope)):
    b = one(select(bookings).where(bookings.c.id == booking_id))
    if not b or (business != "*" and b["business_id"] != business):
        raise HTTPException(404, "Booking not found")
    run(update(bookings).where(bookings.c.id == booking_id).values(status=body.status))
    return {"ok": True}


class LeadStatusIn(BaseModel):
    status: str = Field(pattern="^(new|qualified|booked|lost)$")


@app.post("/admin/leads/{user_id}/status")
def admin_lead_status(user_id: str, body: LeadStatusIn, business: str = Depends(admin_scope)):
    lead = get_lead(user_id)
    if not lead or (business != "*" and lead["business_id"] != business):
        raise HTTPException(404, "Lead not found")
    upsert_lead(user_id, status=body.status)
    return {"ok": True}


@app.get("/admin/followups")
def admin_followups(business: str = Depends(admin_scope)):
    q = (select(followups, leads.c.name, leads.c.phone, leads.c.score)
         .join(leads, leads.c.user_id == followups.c.user_id, isouter=True))
    return rows(scoped(q, followups.c.business_id, business).order_by(followups.c.id.desc()).limit(100))


@app.post("/admin/followups/run")
def admin_run_followups(ignore_wait: bool = False, business: str = Depends(admin_scope)):
    """Draft follow-ups now. ignore_wait=true skips the 1/3/7-day wait (for demos)."""
    return run_followups(STATE["graph"], STATE["model"], thread_key, ignore_wait=ignore_wait, business=business)


class ApproveIn(BaseModel):
    message: str | None = Field(default=None, max_length=1000)


@app.post("/admin/followups/{followup_id}/approve")
def admin_approve(followup_id: int, body: ApproveIn | None = None, business: str = Depends(admin_scope)):
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or (business != "*" and f["business_id"] != business):
        raise HTTPException(404, "Follow-up not found")
    res = send_followup(followup_id, STATE["graph"], thread_key, body.message if body else None)
    if not res:
        raise HTTPException(409, "Already handled")
    return res


@app.post("/admin/followups/{followup_id}/skip")
def admin_skip(followup_id: int, business: str = Depends(admin_scope)):
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or (business != "*" and f["business_id"] != business):
        raise HTTPException(404, "Follow-up not found")
    return {"ok": skip_followup(followup_id)}


@app.post("/cron/followups")
def cron_followups(x_cron_secret: str = Header(default="")):
    """Called daily by GitHub Actions. Only drafts; a human approves in the dashboard."""
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(401, "Bad cron secret")
    return run_followups(STATE["graph"], STATE["model"], thread_key)


@app.exception_handler(Exception)
async def unhandled(request, exc):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"error": "internal error"})


# Serve the website from the API too (handy locally and as a backup to Vercel).
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND.exists():
    app.mount("/ui", StaticFiles(directory=FRONTEND, html=True), name="ui")
