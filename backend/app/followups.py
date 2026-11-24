"""Follow-up agent: finds leads who went quiet and drafts a personal nudge (day 1, day 3, day 7).

Human in the loop (Lab 03): drafts wait in the admin dashboard; a human approves before anything is sent.
Sending = appending the message to the customer's chat thread, where they see it next time they open it.
"""
from datetime import timedelta

from langchain.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import insert, select, update

from app import config
from app.db import chat_log, followups, get_lead, leads, now, one, rows, run, upsert_lead
from app.tools import pkr

STEP_AFTER_HOURS = {1: 24, 2: 72, 3: 168}          # CUSTOM: follow-up cadence (day 1, 3, 7)
FOLLOWUP_PROMPT = """You are LeadPilot's follow-up agent for Skyline Realty (property) and AutoHub Motors (cars).
Write ONE short WhatsApp-style follow-up (max 45 words) to a customer who stopped replying.
Be friendly, reference what they wanted, give one reason to act now, and end with a simple question
that invites them to book a site visit or test drive. No made-up prices, discounts or listings. No emojis spam."""


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


def due_leads(ignore_wait: bool = False, business: str = "*") -> list[dict]:
    """Leads that are not booked/lost, went quiet long enough, and have no draft waiting."""
    pending = {r["user_id"] for r in rows(select(followups.c.user_id).where(followups.c.status == "draft"))}
    # Only leads who had at least one real (not guard-blocked) exchange deserve a follow-up.
    engaged = {r["user_id"] for r in rows(select(chat_log.c.user_id).where(chat_log.c.blocked == 0).distinct())}
    due = []
    q = select(leads).where(leads.c.status.in_(["new", "qualified"]))
    if business != "*":
        q = q.where(leads.c.business_id == business)
    for lead in rows(q):
        step = (lead["followup_count"] or 0) + 1
        if (step > 3 or lead["user_id"] in pending or lead["user_id"] not in engaged
                or not lead["last_thread_id"] or not lead["last_customer_at"]):
            continue
        quiet = now() - _aware(lead["last_customer_at"])
        if ignore_wait or quiet >= timedelta(hours=STEP_AFTER_HOURS[step]):
            due.append({**lead, "step": step})
    return due


def draft_text(lead: dict, history: list, model) -> str:
    profile = (f"Name: {lead.get('name') or 'unknown'}; wants: {lead.get('category') or 'not said'}; "
               f"budget: {pkr(lead['budget_pkr']) if lead.get('budget_pkr') else 'not said'}; "
               f"timeline: {lead.get('timeline') or 'not said'}; preferences: {lead.get('preferences') or '-'}; "
               f"follow-up number: {lead['step']} of 3.")
    recent = "\n".join(f"{'Customer' if isinstance(m, HumanMessage) else 'Agent'}: {m.text[:200]}"
                       for m in history[-6:] if isinstance(m, (HumanMessage, AIMessage)) and m.text)
    reply = model.invoke([SystemMessage(FOLLOWUP_PROMPT),
                          HumanMessage(f"Customer profile: {profile}\nLast messages:\n{recent or '(none)'}")])
    return reply.text.strip()


def run_followups(graph, model, thread_key, ignore_wait: bool = False, auto_send: bool = False,
                  business: str = "*") -> dict:
    """Draft follow-ups for due leads, most promising first, at most MAX_FOLLOWUPS_PER_RUN per run
    (audit 11.2: a bounded number of AI calls per run, never one per lead in the whole database)."""
    drafted = 0
    due = due_leads(ignore_wait, business)
    due.sort(key=lambda l: (l["score"] != "hot", l["score"] != "warm", not l["budget_pkr"], not l["name"]))
    for lead in due[:config.MAX_FOLLOWUPS_PER_RUN]:
        cfg = {"configurable": {"thread_id": thread_key(lead["user_id"], lead["last_thread_id"])}}
        history = graph.get_state(cfg).values.get("messages", [])
        text = draft_text(lead, history, model)
        res = run(insert(followups).values(user_id=lead["user_id"], thread_id=lead["last_thread_id"],
                                           business_id=lead["business_id"], step=lead["step"], message=text,
                                           status="draft", created_at=now()))
        drafted += 1
        if auto_send:
            send_followup(res.inserted_primary_key[0], graph, thread_key)
    return {"drafted": drafted, "due": len(due), "cap": config.MAX_FOLLOWUPS_PER_RUN}


def send_followup(followup_id: int, graph, thread_key, edited_text: str | None = None) -> dict | None:
    """Approve: put the message into the customer's thread and count it."""
    f = one(select(followups).where(followups.c.id == followup_id))
    if not f or f["status"] != "draft":
        return None
    text = (edited_text or f["message"]).strip()
    cfg = {"configurable": {"thread_id": thread_key(f["user_id"], f["thread_id"])}}
    graph.update_state(cfg, {"messages": [AIMessage(text, name="followup")]}, as_node="memory")
    run(update(followups).where(followups.c.id == followup_id).values(status="sent", message=text, sent_at=now()))
    lead = get_lead(f["user_id"]) or {}
    upsert_lead(f["user_id"], followup_count=(lead.get("followup_count") or 0) + 1, last_agent_at=now())
    return {"id": followup_id, "status": "sent"}


def skip_followup(followup_id: int) -> bool:
    res = run(update(followups).where(followups.c.id == followup_id, followups.c.status == "draft").values(status="skipped"))
    return res.rowcount > 0
