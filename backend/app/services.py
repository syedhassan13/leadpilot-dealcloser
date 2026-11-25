"""Application services shared by the routers: run a conversation turn, read and erase customer data."""
import logging
import re
import time

from langchain.messages import AIMessage, HumanMessage
from sqlalchemy import insert, select

from app.db import bookings, chat_log, delete_customer, get_lead, listings, now, rows, run, upsert_lead
from app.graph import thread_key
from app.runtime import rt
from app.tools import LeadContext

log = logging.getLogger("leadpilot")

FALLBACK_REPLY = "Sorry, could you say that again? Are you looking for a property or a car?"
ERROR_REPLY = "Sorry, I hit a temporary problem. Please send your message again."


def config_for(user_id: str, thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_key(user_id, thread_id)}}


def run_turn(user_id: str, thread_id: str, message: str, dealer: str | None = None) -> dict:
    """One conversation turn through the graph. Never raises: always returns the graded response shape."""
    cfg = config_for(user_id, thread_id)
    start = time.perf_counter()
    try:
        before = len(rt.graph.get_state(cfg).values.get("messages", []))
        out = rt.graph.invoke({"messages": [HumanMessage(message)]}, cfg,
                              context=LeadContext(user_id=user_id, thread_id=thread_id, dealer=dealer))
        ai = [m for m in out["messages"][before:] if isinstance(m, AIMessage)]
        reply = next((m.text for m in reversed(ai) if m.text.strip()), "") or FALLBACK_REPLY
        tool_calls = [{"name": c["name"], "args": c["args"]} for m in ai for c in (m.tool_calls or [])]
        tokens = {"in": sum((m.usage_metadata or {}).get("input_tokens", 0) for m in ai),
                  "out": sum((m.usage_metadata or {}).get("output_tokens", 0) for m in ai)}
        blocked = bool(out.get("blocked"))
    except Exception:
        log.exception("chat turn failed user=%s", user_id[:12])
        reply, tool_calls, tokens, blocked = ERROR_REPLY, [], {"in": 0, "out": 0}, False
    latency = int((time.perf_counter() - start) * 1000)
    try:
        run(insert(chat_log).values(user_id=user_id, thread_id=thread_id, message=message, reply=reply,
                                    tool_calls=tool_calls, blocked=int(blocked), tokens_in=tokens["in"],
                                    tokens_out=tokens["out"], latency_ms=latency, created_at=now()))
    except Exception:
        log.exception("chat log write failed")
    return {"reply": reply, "tool_calls": tool_calls, "tokens": tokens, "latency_ms": latency}


PHONE_RE = re.compile(r"(?:\+?92|0)3\d{2}[\s-]?\d{7}|\b0\d{2,3}[\s-]?\d{6,8}\b")


def capture_turn(dealer: dict, user_id: str, thread_id: str, message: str, reason: str) -> str:
    """Capture-only reply (dealer at its plan limit or suspended): no AI call, no AI cost, but the buyer is
    answered politely and the lead (with any phone number they typed) is saved for the dealer's team."""
    phone = PHONE_RE.search(message)
    lead = get_lead(user_id) or {}
    upsert_lead(user_id, business_id=None if lead.get("business_id") else dealer["id"],
                category=None if lead.get("category") else (dealer["category"] if dealer["category"] != "all" else None),
                phone=None if lead.get("phone") or not phone else phone.group(0), last_thread_id=thread_id,
                last_customer_at=now(), source=lead.get("source") or "chat (capture-only)")
    asked = bool(lead.get("phone") or phone)
    reply = (f"Thanks for your message! A sales officer from {dealer['name']} will get back to you shortly. "
             + ("" if asked else "Please share your name and phone number so they can reach you."))
    cfg = config_for(user_id, thread_id)
    try:   # keep the transcript complete for the dealer's console
        rt.graph.update_state(cfg, {"messages": [HumanMessage(message), AIMessage(reply, name="capture")]}, as_node="memory")
        ns = ("leads", user_id)
        act = rt.store.get(ns, "activity")
        threads = act.value.get("threads", []) if act else []
        if thread_id not in threads:
            rt.store.put(ns, "activity", {"threads": (threads + [thread_id])[-20:], "last_seen": now().isoformat()})
    except Exception:
        log.exception("could not store capture-only turn")
    try:
        run(insert(chat_log).values(user_id=user_id, thread_id=thread_id, message=message, reply=reply, tool_calls=[],
                                    blocked=0, tokens_in=0, tokens_out=0, latency_ms=0, created_at=now()))
    except Exception:
        log.exception("chat log write failed")
    return reply


def visible_messages(messages) -> list[dict]:
    """What a person should see of a thread: customer and agent text, no tool plumbing."""
    out = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "text": m.text})
        elif isinstance(m, AIMessage) and m.text.strip():
            out.append({"role": "assistant", "text": m.text, "kind": m.name or "closer"})
    return out


def thread_messages(user_id: str, thread_id: str) -> list[dict]:
    return visible_messages(rt.graph.get_state(config_for(user_id, thread_id)).values.get("messages", []))


def thread_ids(user_id: str) -> list[str]:
    item = rt.store.get(("leads", user_id), "activity")
    return item.value.get("threads", []) if item else []


def profile_of(user_id: str) -> dict:
    item = rt.store.get(("leads", user_id), "profile")
    return dict(item.value) if item else {}


def bookings_of(user_id: str) -> list[dict]:
    q = (select(bookings.c.id, bookings.c.listing_id, bookings.c.kind, bookings.c.date, bookings.c.time,
                bookings.c.status, listings.c.title, listings.c.location)
         .join(listings, listings.c.id == bookings.c.listing_id)
         .where(bookings.c.user_id == user_id).order_by(bookings.c.date, bookings.c.time))
    return rows(q)


def erase_customer(user_id: str):
    """Delete everything held about one customer: business rows, long-term memory and every chat thread."""
    for t in set(thread_ids(user_id) + ["t1"]):
        try:
            rt.saver.delete_thread(thread_key(user_id, t))
        except Exception:
            log.exception("could not delete a thread")
    for key in ("profile", "activity"):
        rt.store.delete(("leads", user_id), key)
    delete_customer(user_id)
