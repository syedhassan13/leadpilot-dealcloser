"""Meta webhooks: WhatsApp messages, Messenger and Instagram DMs, Facebook/Instagram lead forms.

Audit 6.6: every POST must carry Meta's X-Hub-Signature-256 (HMAC-SHA256 of the raw body with the app secret),
each event is processed once (dc_webhook_events), and the response is sent before the slow work (the AI turn),
which runs in the background. Audit 8.2: buyer messages are untrusted and go through the same guard, plan limits
and capture-only rules as website chat.
"""
import hashlib
import hmac
import json
import logging
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from langchain.messages import AIMessage, HumanMessage
from sqlalchemy import update

from app import channels, config, guard, identity, tenants
from app.db import get_lead, now, outbox, run, upsert_lead, webhook_events
from app.graph import thread_key
from app.runtime import rt
from app.security import client_ip, hit, log_event
from app.services import capture_turn, run_turn

log = logging.getLogger("leadpilot")
router = APIRouter()
MAX_BODY = 512 * 1024
THREAD = {"whatsapp": "wa", "messenger": "fb", "instagram": "ig"}
PREFIX = {"whatsapp": "wa", "messenger": "fb", "instagram": "ig", "leadgen": "fl"}
REF = re.compile(r"\(ref ([0-9a-f]{10})\)")
SAFE = re.compile(r"[^A-Za-z0-9 _.\-]")
TEXT_ONLY = ("Thanks! I can only read text messages here. Please type your question, or share your name and number "
             "and a sales officer will call you.")


# ───────────────────────── verification ─────────────────────────
@router.get("/webhooks/meta", response_class=PlainTextResponse, include_in_schema=False)
def verify_subscription(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
                        challenge: str = Query("", alias="hub.challenge", max_length=64)):
    """Meta's one-time subscription check."""
    if not config.META_VERIFY_TOKEN or mode != "subscribe" or not hmac.compare_digest(token, config.META_VERIFY_TOKEN):
        raise HTTPException(403, "Verification failed")
    if not challenge.isdigit():                     # only echo Meta's numeric challenge, never arbitrary text
        raise HTTPException(400, "Bad challenge")
    return challenge


def signature_ok(raw: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256=") or not config.META_APP_SECRET:
        return False
    expected = hmac.new(config.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def first_time(kind: str, event_id: str) -> bool:
    """True the first time we see this event (idempotent across retries and restarts)."""
    key = hashlib.sha256(f"{kind}|{event_id}".encode()).hexdigest()
    ins = tenants._dialect_insert()
    return bool(run(ins(webhook_events).values(id=key, received_at=now()).on_conflict_do_nothing()).rowcount)


@router.post("/webhooks/meta", include_in_schema=False)
async def receive(request: Request, background: BackgroundTasks):
    if not channels.enabled():
        raise HTTPException(503, "Channels are not configured")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Too large")
    if not signature_ok(raw, request.headers.get("x-hub-signature-256")):
        ip = client_ip(request)
        hit("webhook_bad_sig", ip, 1000, 3600)
        log_event("webhook_bad_signature", "", {"ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:12]})
        raise HTTPException(401, "Bad signature")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Bad JSON")
    jobs = collect(body)
    for job in jobs:
        background.add_task(run_job, job)
    background.add_task(lambda: channels._safe(channels.flush))
    return {"ok": True, "events": len(jobs)}


# ───────────────────────── parse ─────────────────────────
def collect(body: dict) -> list[tuple]:
    """Turn a webhook payload into jobs, skipping events we've already processed and channels we don't know."""
    jobs = []
    obj = body.get("object")
    for entry in body.get("entry") or []:
        if obj == "whatsapp_business_account":
            for change in entry.get("changes") or []:
                v = change.get("value") or {}
                if change.get("field") != "messages":
                    continue
                ch = channels.find("whatsapp", (v.get("metadata") or {}).get("phone_number_id", ""))
                if not ch:
                    continue
                names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "") for c in v.get("contacts") or []}
                for m in v.get("messages") or []:
                    if m.get("id") and m.get("from") and first_time("wa", m["id"]):
                        jobs.append(("whatsapp", ch, m, names.get(m["from"], "")))
                for s in v.get("statuses") or []:
                    jobs.append(("status", ch, s, None))
        elif obj in ("page", "instagram"):
            for change in entry.get("changes") or []:
                v = change.get("value") or {}
                if change.get("field") == "leadgen" and v.get("leadgen_id"):
                    ch = channels.find("leadgen", str(v.get("page_id") or entry.get("id")))
                    if ch and first_time("leadgen", str(v["leadgen_id"])):
                        jobs.append(("leadgen", ch, v, None))
            kind = "messenger" if obj == "page" else "instagram"
            ch = channels.find(kind, str(entry.get("id", "")))
            for ev in entry.get("messaging") or []:
                msg = ev.get("message") or {}
                sender = str((ev.get("sender") or {}).get("id", ""))
                if not ch or not msg or msg.get("is_echo") or not sender or sender == ch["external_id"]:
                    continue                                    # our own replies come back as echoes: never answer them
                if msg.get("mid") and first_time(kind, msg["mid"]):
                    jobs.append((kind, ch, {"from": sender, "text": msg.get("text"), "attachments": msg.get("attachments")}, None))
    return jobs


def run_job(job: tuple):
    kind, ch, payload, extra = job
    try:
        if kind == "status":
            return record_status(payload)
        if not tenants.allows_channel(tenants.get(ch["business_id"]), "leadgen" if kind == "leadgen" else kind):
            return channels.seen(ch, "Not included in the agency's plan")      # downgraded: stop answering here
        channels.seen(ch)
        if kind == "leadgen":
            return process_leadgen(ch, payload)
        if kind == "whatsapp":
            return process_whatsapp(ch, payload, extra)
        return process_dm(ch, kind, payload)
    except Exception:
        log.exception("webhook job failed (%s)", kind)
        channels.seen(ch, "processing error")


def record_status(s: dict):
    if s.get("status") == "failed" and s.get("id"):
        err = ((s.get("errors") or [{}])[0].get("title") or "delivery failed")[:300]
        run(update(outbox).where(outbox.c.external_id == s["id"]).values(status="failed", error=err))


# ───────────────────────── conversations ─────────────────────────
def uid_for(kind: str, business_id: str, contact: str) -> str:
    """Stable lead id per agency and contact. The phone number itself never goes into ids or logs."""
    return f"{PREFIX[kind]}_" + hashlib.sha256(f"{business_id}|{contact}".encode()).hexdigest()[:24]


def wa_text(m: dict) -> str | None:
    t = m.get("type")
    if t == "text":
        return (m.get("text") or {}).get("body")
    if t == "button":
        return (m.get("button") or {}).get("text")
    if t == "interactive":
        i = m.get("interactive") or {}
        return (i.get("button_reply") or i.get("list_reply") or {}).get("title")
    return None


def referral_src(ref: dict | None) -> dict | None:
    """Click-to-WhatsApp ads tell us which ad the buyer tapped."""
    if not ref:
        return None
    url = (ref.get("source_url") or "").lower()
    camp = SAFE.sub("", (ref.get("headline") or ref.get("source_id") or "")[:60]).strip()
    return {"utm_source": "instagram" if "instagram" in url else "facebook", "utm_medium": "ctwa_ad",
            **({"utm_campaign": camp} if camp else {})}


def process_whatsapp(ch: dict, m: dict, profile_name: str):
    from app.identity import normalize_phone
    phone = normalize_phone(m["from"])
    text = wa_text(m)
    src = referral_src(m.get("referral"))
    if text and REF.search(text):                              # tracked link that opened WhatsApp ("Hi! (ref ...)")
        src = {"link": REF.search(text).group(1)}
        text = REF.sub("", text).strip() or "Hi"
    converse(ch, "whatsapp", m["from"], text, name=profile_name[:80] or None, phone=phone, src=src)


def process_dm(ch: dict, kind: str, p: dict):
    converse(ch, kind, p["from"], p.get("text"))


def converse(ch: dict, kind: str, contact: str, text: str | None, name: str | None = None, phone: str | None = None,
             src: dict | None = None):
    dealer = tenants.get(ch["business_id"])
    if not dealer:
        return
    uid, thread = uid_for(kind, dealer["id"], contact), THREAD[kind]
    to = channels.wa_number(phone) if kind == "whatsapp" else contact
    if not hit(f"{kind}_sender", uid, 30, 3600):                 # one sender can't flood the AI (audit 6.1 / 8.1)
        return
    lead = get_lead(uid) or {}
    upsert_lead(uid, business_id=None if lead.get("business_id") else dealer["id"],
                category=None if lead.get("category") or dealer["category"] == "all" else dealer["category"],
                name=None if lead.get("name") else name, phone=None if lead.get("phone") else phone,
                source=None if lead.get("source") else kind, contact_kind=kind, contact_channel_id=ch["id"],
                contact_id=to, last_inbound_at=now(), last_customer_at=now())
    identity.touch(uid, dealer, src, default=kind)
    lead = get_lead(uid)

    def reply(msg: str):
        channels.queue(ch, to, "text", {"text": msg}, uid, "reply", send_now=False)

    if not text or not text.strip():
        return reply(TEXT_ONLY)
    text = text.strip()[: config.MAX_MESSAGE_CHARS]
    if channels.OPT_OUT.match(text):
        upsert_lead(uid, opted_out=1)
        log_event("buyer_opted_out", "", {"business": dealer["id"], "channel": kind})
        return reply("You won't receive any more messages from us. Reply START any time to chat again.")
    if channels.OPT_IN.match(text) and lead.get("opted_out"):
        upsert_lead(uid, opted_out=0)
    if paused(lead):                                           # a salesperson took over: keep the message, no AI
        remember(uid, thread, text, None)
        notify_takeover(dealer["id"], lead, text)
        return
    if not guard.check(text):
        allowed, reason = tenants.admit_conversation(dealer, thread_key(uid, thread))
        if not allowed:
            return reply(capture_turn(dealer, uid, thread, text, reason))
    if not hit("ai_global_day", "all", config.AI_TURNS_PER_DAY, 86400):
        return reply("Thanks for your message! A sales officer will contact you shortly.")
    r = run_turn(uid, thread, text, tenants.scope_of(dealer))
    tenants.record_ai(dealer["id"], r["tokens"]["in"], r["tokens"]["out"])
    reply(r["reply"])


def paused(lead: dict) -> bool:
    if lead.get("ai_paused"):
        return True
    if lead.get("merged_into"):
        p = get_lead(lead["merged_into"])
        return bool(p and p.get("ai_paused"))
    return False


def remember(uid: str, thread: str, buyer: str | None, staff: str | None):
    """Keep the transcript complete when the AI isn't the one answering."""
    msgs = ([HumanMessage(buyer)] if buyer else []) + ([AIMessage(staff, name="staff")] if staff else [])
    try:
        rt.graph.update_state({"configurable": {"thread_id": thread_key(uid, thread)}}, {"messages": msgs}, as_node="memory")
        ns = ("leads", uid)
        act = rt.store.get(ns, "activity")
        threads = act.value.get("threads", []) if act else []
        if thread not in threads:
            rt.store.put(ns, "activity", {"threads": (threads + [thread])[-20:], "last_seen": now().isoformat()})
    except Exception:
        log.exception("could not store a takeover message")


def notify_takeover(business_id: str, lead: dict, text: str):
    target = get_lead(lead["merged_into"]) if lead.get("merged_into") else lead
    tenants.alert(business_id, f"msg:{lead['user_id']}:{int(now().timestamp())}"[:80], "info",
                  f"New message from {target.get('name') or 'a buyer'}: \"{text[:120]}\"",
                  member_id=target.get("assigned_to"), lead_id=target["user_id"])


# ───────────────────────── lead forms ─────────────────────────
NAME_FIELDS = ("full_name", "first_name", "last_name", "phone_number", "email")


def process_leadgen(ch: dict, v: dict):
    dealer = tenants.get(ch["business_id"])
    token = channels.unseal(ch["token_enc"])
    if not dealer or not token or not re.fullmatch(r"\d{1,30}", str(v["leadgen_id"])):
        return
    data = channels.graph("GET", str(v["leadgen_id"]), token,
                          params={"fields": "field_data,created_time,ad_id,ad_name,campaign_name,form_id,platform"})
    create_form_lead(ch, dealer, str(v["leadgen_id"]), data, v.get("form_id"))


def create_form_lead(ch: dict, dealer: dict, leadgen_id: str, data: dict, form_id=None, demo: bool = False) -> str:
    """A lead-form submission (from Meta, or from the demo simulator) becomes a lead + first WhatsApp contact."""
    v = {"leadgen_id": leadgen_id, "form_id": form_id}
    f = {x.get("name"): str((x.get("values") or [""])[0])[:300] for x in data.get("field_data") or []}
    name = (f.get("full_name") or " ".join(p for p in (f.get("first_name"), f.get("last_name")) if p)).strip()[:80] or None
    from app.identity import normalize_phone
    phone = normalize_phone(f.get("phone_number"))
    extra = "; ".join(f"{k.replace('_', ' ')}: {val}" for k, val in f.items() if k not in NAME_FIELDS and val)[:1000]
    uid = uid_for("leadgen", dealer["id"], str(v["leadgen_id"]))
    upsert_lead(uid, business_id=dealer["id"], name=name, phone=f.get("phone_number") if phone else None,
                category=dealer["category"] if dealer["category"] != "all" else None, source="lead form",
                preferences=extra or None, last_customer_at=now())
    camp = SAFE.sub("", str(data.get("campaign_name") or data.get("ad_name") or v.get("form_id") or ""))[:60].strip()
    identity.touch(uid, dealer, {"utm_source": "instagram" if data.get("platform") == "ig" else "facebook",
                                 "utm_medium": "lead_form", **({"utm_campaign": camp} if camp else {})}, default="facebook")
    log_event("lead_form_received", "", {"business": dealer["id"]})
    # First contact on WhatsApp within a minute, with the agency's approved template (business-initiated).
    wa = channels.whatsapp_of(dealer["id"], demo=demo)
    tpl = channels.settings_of(wa).get("first_template") if wa else None
    to = channels.wa_number(phone)
    if wa and tpl and to and tenants.allows_channel(dealer, "whatsapp"):
        upsert_lead(uid, contact_kind="whatsapp", contact_channel_id=wa["id"], contact_id=to)
        channels.queue(wa, to, "template", {"name": tpl, "lang": channels.settings_of(wa).get("template_lang", "en"),
                                            "params": [(name or "there").split(" ")[0], dealer["name"]]},
                       uid, "first_contact", send_now=demo)
    return uid
