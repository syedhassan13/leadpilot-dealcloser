"""Messaging channels: WhatsApp (Cloud API), Facebook Messenger, Instagram DMs and Facebook/Instagram lead forms.

Security (pre-launch audit):
- 1.3/1.4 Each agency's Meta access token is encrypted at rest (Fernet, key from CHANNEL_KEY) and never logged,
  returned or put in a URL (sent as an Authorization header).
- 5.4 Outbound calls only ever go to the fixed Graph API host; no user-provided URL is fetched.
- 11.2 Sending goes through an outbox with bounded retries and exponential backoff.
- H16 Business-initiated WhatsApp messages (follow-ups, the first message to a form lead) use approved templates and
  never go to a buyer who opted out ("STOP").
"""
import json
import logging
import re
import threading
from datetime import timedelta

import requests
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, insert, or_, select, update

from app import config
from app.db import channels, engine, leads, now, one, outbox, rows, run

log = logging.getLogger("leadpilot")

KINDS = ("whatsapp", "messenger", "instagram", "leadgen")
WINDOW = timedelta(hours=24)                 # WhatsApp/Messenger free-form reply window
MAX_ATTEMPTS = 4
BACKOFF = (60, 300, 1800)                    # seconds before attempts 2, 3 and 4
TEXT_LIMIT = 4000                            # WhatsApp text body limit is 4096
OPT_OUT = re.compile(r"^\s*(stop|unsubscribe|band karo|mat bhejo)\s*[.!]*\s*$", re.I)
OPT_IN = re.compile(r"^\s*(start|subscribe)\s*[.!]*\s*$", re.I)


def enabled() -> bool:
    """Channels work only when the Meta app secret (webhook signatures) and the encryption key are configured."""
    return bool(config.META_APP_SECRET and config.CHANNEL_KEY)


# ───────────────────────── tokens at rest ─────────────────────────
def _fernet() -> Fernet:
    if not config.CHANNEL_KEY:
        raise RuntimeError("CHANNEL_KEY is not set")
    return Fernet(config.CHANNEL_KEY.encode())


def seal(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def unseal(blob: str | None) -> str | None:
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, RuntimeError):
        log.error("a channel token could not be decrypted (was CHANNEL_KEY changed?)")
        return None


# ───────────────────────── Graph API (fixed host) ─────────────────────────
class GraphError(Exception):
    def __init__(self, status: int, message: str, retry: bool):
        super().__init__(message)
        self.status, self.retry = status, retry


def graph(method: str, path: str, token: str, body: dict | None = None, params: dict | None = None) -> dict:
    """Call graph.facebook.com/<version>/<path>. path is built by us from ids we stored, never from user URLs."""
    if not re.fullmatch(r"[A-Za-z0-9_]+(/[A-Za-z0-9_]+)?", path):
        raise GraphError(400, "bad graph path", False)
    url = f"https://graph.facebook.com/{config.GRAPH_API_VERSION}/{path}"
    try:
        r = requests.request(method, url, json=body, params=params, timeout=15,
                             headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as e:
        raise GraphError(0, type(e).__name__, True)
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            msg = f"{err.get('code', r.status_code)}: {str(err.get('message', ''))[:200]}"
        except ValueError:
            msg = str(r.status_code)
        raise GraphError(r.status_code, msg, r.status_code in (429,) or r.status_code >= 500)
    return r.json() if r.content else {}


# ───────────────────────── channel records ─────────────────────────
def public(ch: dict) -> dict:
    """What the console sees: never the token."""
    out = {k: ch.get(k) for k in ("id", "kind", "external_id", "display", "status", "settings", "created_at",
                                  "last_event_at", "last_error")}
    out["has_token"] = bool(ch.get("token_enc"))
    return out


def find(kind: str, external_id: str) -> dict | None:
    return one(select(channels).where(and_(channels.c.kind == kind, channels.c.external_id == str(external_id),
                                           channels.c.status == "active")))


def for_business(business_id: str, kind: str | None = None) -> list[dict]:
    q = select(channels).where(channels.c.business_id == business_id)
    if kind:
        q = q.where(channels.c.kind == kind)
    return rows(q.order_by(channels.c.kind, channels.c.created_at))


def whatsapp_of(business_id: str) -> dict | None:
    return next((c for c in for_business(business_id, "whatsapp") if c["status"] == "active"), None)


def settings_of(ch: dict | None) -> dict:
    s = (ch or {}).get("settings") or {}
    return s if isinstance(s, dict) else json.loads(s)


def seen(ch: dict, error: str | None = None):
    run(update(channels).where(channels.c.id == ch["id"]).values(last_event_at=now(), last_error=error))


# ───────────────────────── message formatting ─────────────────────────
def to_whatsapp(text: str) -> str:
    """Our markdown -> WhatsApp formatting (*bold*), within the length limit."""
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text or "")
    t = re.sub(r"^\s*[-•]\s+", "• ", t, flags=re.M)
    return t[:TEXT_LIMIT]


def to_plain(text: str) -> str:
    """Messenger and Instagram show plain text."""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    return re.sub(r"^\s*[-•]\s+", "• ", t, flags=re.M)[:1900]


def wa_number(phone_norm: str | None) -> str | None:
    """+923001234567 -> 923001234567 (the Cloud API's 'to' format)."""
    return phone_norm[1:] if phone_norm and phone_norm.startswith("+") and phone_norm[1:].isdigit() else None


def in_window(lead: dict) -> bool:
    last = lead.get("last_inbound_at")
    if not last:
        return False
    if last.tzinfo is None:
        from datetime import timezone
        last = last.replace(tzinfo=timezone.utc)
    return now() - last < WINDOW


# ───────────────────────── outbox ─────────────────────────
def queue(ch: dict, to: str, kind: str, payload: dict, lead_id: str | None = None, purpose: str = "reply",
          send_now: bool = True) -> int:
    """Queue one outbound message (and try it straight away unless send_now=False)."""
    res = run(insert(outbox).values(business_id=ch["business_id"], channel_id=ch["id"], to=to, kind=kind,
                                    payload=payload, lead_id=lead_id, purpose=purpose, status="queued", attempts=0,
                                    next_at=now(), created_at=now()))
    oid = res.inserted_primary_key[0]
    if send_now:
        deliver(oid)
    return oid


def _body(ch: dict, item: dict) -> tuple[str, dict]:
    p = item["payload"] if isinstance(item["payload"], dict) else json.loads(item["payload"])
    if ch["kind"] == "whatsapp":
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": item["to"]}
        if item["kind"] == "template":
            comps = [{"type": "body", "parameters": [{"type": "text", "text": str(v)[:900].replace("\n", " ")}
                                                     for v in p.get("params", [])]}] if p.get("params") else []
            msg = {**base, "type": "template", "template": {"name": p["name"], "language": {"code": p.get("lang", "en")},
                                                            **({"components": comps} if comps else {})}}
        else:
            msg = {**base, "type": "text", "text": {"preview_url": False, "body": to_whatsapp(p["text"])}}
        return f"{ch['external_id']}/messages", msg
    # Messenger and Instagram (Facebook Login, page token)
    return "me/messages", {"recipient": {"id": item["to"]}, "messaging_type": "RESPONSE",
                           "message": {"text": to_plain(p["text"])}}


def deliver(outbox_id: int) -> bool:
    """Send one queued message. Retryable failures are rescheduled with backoff; others fail for good."""
    item = one(select(outbox).where(outbox.c.id == outbox_id))
    if not item or item["status"] != "queued":
        return False
    ch = one(select(channels).where(channels.c.id == item["channel_id"]))
    token = unseal(ch["token_enc"]) if ch and ch["status"] == "active" else None
    attempts = (item["attempts"] or 0) + 1
    if not token:
        run(update(outbox).where(outbox.c.id == outbox_id).values(status="failed", attempts=attempts,
                                                                  error="channel disconnected"))
        return False
    try:
        path, body = _body(ch, item)
        res = graph("POST", path, token, body)
        mid = (res.get("messages") or [{}])[0].get("id") or res.get("message_id")
        run(update(outbox).where(outbox.c.id == outbox_id).values(status="sent", attempts=attempts, sent_at=now(),
                                                                  external_id=str(mid or "")[:120], error=None))
        return True
    except GraphError as e:
        retry = e.retry and attempts < MAX_ATTEMPTS
        run(update(outbox).where(outbox.c.id == outbox_id).values(
            status="queued" if retry else "failed", attempts=attempts, error=str(e)[:300],
            next_at=now() + timedelta(seconds=BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]) if retry else None))
        seen(ch, str(e)[:300])
        log.warning("send failed (channel %s, attempt %d): %s", ch["kind"], attempts, e)
        return False


def flush(limit: int = 50) -> dict:
    """Send everything that is due (cron and after webhooks)."""
    due = rows(select(outbox.c.id).where(and_(outbox.c.status == "queued", outbox.c.next_at <= now()))
               .order_by(outbox.c.id).limit(limit))
    sent = sum(1 for d in due if deliver(d["id"]))
    return {"due": len(due), "sent": sent}


def kick():
    """Flush in the background without holding up a request (e.g. a staff alert raised during a web chat)."""
    threading.Thread(target=lambda: _safe(flush), daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception:
        log.exception("outbox flush failed")


# ───────────────────────── business-initiated messages ─────────────────────────
def contact_lead(user_id: str) -> dict | None:
    """Of a lead and every lead merged into it, the one we can message (most recent inbound first)."""
    from app.identity import aliases_of
    ids = [user_id] + aliases_of(user_id)
    found = rows(select(leads).where(and_(leads.c.user_id.in_(ids), leads.c.contact_kind.is_not(None))))
    if not found:
        return None
    from datetime import timezone

    def last(l):
        t = l.get("last_inbound_at")
        return 0.0 if not t else (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    return max(found, key=last)


def can_contact(lead: dict) -> bool:
    return not lead.get("opted_out")


def send_to_lead(lead: dict, text: str, purpose: str, template_param: str | None = None) -> str:
    """Send text to a lead on their own channel. Inside the 24h window: free text. Outside (WhatsApp): the agency's
    approved template, if configured. Returns 'sent' | 'queued' | 'template' | 'no_channel' | 'outside_window' | 'opted_out'."""
    if not can_contact(lead):
        return "opted_out"
    kind = lead.get("contact_kind")
    ch = one(select(channels).where(and_(channels.c.id == lead.get("contact_channel_id"), channels.c.status == "active"))) \
        if lead.get("contact_channel_id") else None
    if not ch or not lead.get("contact_id"):
        return "no_channel"
    if in_window(lead):
        return "sent" if deliver(queue(ch, lead["contact_id"], "text", {"text": text}, lead["user_id"], purpose, False)) \
            else "queued"
    if kind == "whatsapp":
        tpl = settings_of(ch).get("followup_template")
        if tpl:
            queue(ch, lead["contact_id"], "template", {"name": tpl, "lang": settings_of(ch).get("template_lang", "en"),
                                                      "params": [template_param or text]}, lead["user_id"], purpose)
            return "template"
    return "outside_window"


def alert_staff(business_id: str, member_phone: str | None, message: str):
    """A hot-lead / booked-visit alert to a salesperson's WhatsApp, via the agency's approved alert template."""
    from app.identity import normalize_phone
    ch = whatsapp_of(business_id)
    to = wa_number(normalize_phone(member_phone))
    tpl = settings_of(ch).get("alert_template") if ch else None
    if not (ch and to and tpl):
        return
    queue(ch, to, "template", {"name": tpl, "lang": settings_of(ch).get("template_lang", "en"), "params": [message]},
          None, "staff_alert", send_now=False)
    kick()


def wa_link(business_id: str, text: str) -> str | None:
    """https://wa.me/<agency number>?text=... for tracked links that open the agency's WhatsApp."""
    ch = whatsapp_of(business_id)
    num = re.sub(r"\D", "", (ch or {}).get("display") or "")
    if not num:
        return None
    from urllib.parse import quote
    return f"https://wa.me/{num}?text={quote(text)}"
