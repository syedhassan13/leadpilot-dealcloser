"""Phase 2: WhatsApp, Messenger/Instagram DMs, lead forms, takeover, follow-ups on WhatsApp, outbox retries.
Signed webhook payloads in Meta's formats; the Graph API is replaced by a recorder (no network, no AI).
Audit: 6.6 signed + once-only webhooks, 1.3 tokens encrypted, 3.5 agencies separate, H16 opt-out, 11.2 backoff."""
import hashlib
import hmac
import json
import random
import string
import uuid
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app import channels, config
from app.db import alerts, followups, get_lead, insert, leads, now, outbox, rows, run, upsert_lead

SECRET = "test-app-secret"


class GraphRecorder:
    def __init__(self):
        self.calls, self.fail_next = [], []

    def __call__(self, method, path, token, body=None, params=None):
        self.calls.append({"method": method, "path": path, "token": token, "body": body, "params": params})
        if self.fail_next:
            raise self.fail_next.pop(0)
        if method == "GET" and params and "display_phone_number" in params.get("fields", ""):
            return {"display_phone_number": "+92 321 0000000", "verified_name": "Test"}
        if method == "GET" and params and params.get("fields") == "name":
            return {"name": "Test Page"}
        if method == "GET" and params and "field_data" in params.get("fields", ""):
            return {"field_data": [{"name": "full_name", "values": ["Hamza Ali"]},
                                   {"name": "phone_number", "values": ["+92 300 5550001"]},
                                   {"name": "budget", "values": ["2 crore"]}],
                    "campaign_name": "DHA 6 launch", "platform": "fb"}
        if path.endswith("/messages"):
            return {"messages": [{"id": "wamid." + uuid.uuid4().hex}], "message_id": "m_" + uuid.uuid4().hex}
        return {"success": True}

    def sends(self, to=None):
        out = [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("messages")]
        return [c for c in out if to is None or (c["body"].get("to") or c["body"].get("recipient", {}).get("id")) == to]


turns = []


def fake_turn(user_id, thread_id, message, dealer=None):
    turns.append((user_id, message))
    upsert_lead(user_id, business_id=dealer, last_customer_at=now())
    return {"reply": "stub reply", "tool_calls": [], "tokens": {"in": 10, "out": 5}, "latency_ms": 5}


@pytest.fixture(autouse=True)
def meta(monkeypatch):
    rec = GraphRecorder()
    monkeypatch.setattr(config, "META_APP_SECRET", SECRET)
    monkeypatch.setattr(config, "META_VERIFY_TOKEN", "verify-me")
    monkeypatch.setattr(config, "CHANNEL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CHAT_REQUIRE_KEY", False)
    monkeypatch.setattr(channels, "graph", rec)
    monkeypatch.setattr(channels, "kick", lambda: channels.flush())
    import app.api.webhooks as wh
    monkeypatch.setattr(wh, "run_turn", fake_turn)
    turns.clear()
    return rec


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        yield c


def login(c, key):
    r = c.post("/admin/login", json={"key": key})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session']}"}


def agency(c, **extra):
    owner = login(c, "root-test-key")
    did = "w-" + uuid.uuid4().hex[:8]
    for _ in range(20):
        r = c.post("/admin/owner/dealers", headers=owner, json={"id": did, "name": f"Agency {did}", "category": "property",
                                                                "listing_prefix": "".join(random.choices(string.ascii_uppercase, k=3)), **extra})
        if r.status_code != 409:
            break
    assert r.status_code == 201, r.text
    return {"id": did, "mgr": login(c, r.json()["console_key"]), "dealer": r.json()["dealer"]}


def connect(c, a, kind="whatsapp", ext=None, settings=None):
    ext = ext or str(random.randrange(10**14, 10**15))
    r = c.post("/admin/channels", headers=a["mgr"], json={"kind": kind, "external_id": ext, "token": "EAAG" + "x" * 40,
                                                          **({"waba_id": "1234567890"} if kind == "whatsapp" else {})})
    assert r.status_code == 201, r.text
    ch = r.json()
    if settings:
        c.post(f"/admin/channels/{ch['id']}", headers=a["mgr"], json={"settings": settings})
    return ch


def post(c, payload, secret=SECRET):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return c.post("/webhooks/meta", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def wa_msg(phone_number_id, sender, text, name="Ali Raza", mid=None, **extra):
    m = {"from": sender, "id": mid or "wamid." + uuid.uuid4().hex, "timestamp": "1", "type": "text", "text": {"body": text}, **extra}
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "923210000000", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": name}, "wa_id": sender}], "messages": [m]}}]}]}


def lead_of(business, phone_norm):
    r = rows(select(leads).where(leads.c.business_id == business, leads.c.phone_norm == phone_norm))
    return r[0] if r else None


# ── the webhook door (audit 6.6) ──
def test_subscription_check(client):
    ok = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}).status_code == 403
    assert client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                "hub.challenge": "<script>"}).status_code == 400


def test_unsigned_or_forged_webhooks_are_refused(client, monkeypatch):
    body = wa_msg("111", "923001112223", "hi")
    assert client.post("/webhooks/meta", json=body).status_code == 401
    assert post(client, body, secret="wrong").status_code == 401
    monkeypatch.setattr(config, "META_APP_SECRET", "")
    assert post(client, body).status_code == 503


# ── connecting (audit 1.3, 3.3, 3.5) ──
def test_connect_channel_encrypts_token_and_stays_with_one_agency(client, meta):
    a, b = agency(client), agency(client)
    ch = connect(client, a)
    assert ch["display"] == "+92 321 0000000" and ch["has_token"] and "token" not in ch and "token_enc" not in ch
    stored = rows(select(channels.channels).where(channels.channels.c.id == ch["id"]))[0]
    assert "EAAG" not in stored["token_enc"] and channels.unseal(stored["token_enc"]).startswith("EAAG")
    assert any(c["path"] == "1234567890/subscribed_apps" for c in meta.calls)            # webhooks subscribed
    r = client.post("/admin/channels", headers=b["mgr"], json={"kind": "whatsapp", "external_id": ch["external_id"],
                                                               "token": "EAAG" + "y" * 40})
    assert r.status_code == 409                                                          # can't hijack A's number
    assert client.post(f"/admin/channels/{ch['id']}", headers=b["mgr"], json={"status": "disabled"}).status_code == 404
    meta.fail_next.append(channels.GraphError(400, "190: Invalid OAuth access token", False))
    bad = client.post("/admin/channels", headers=a["mgr"], json={"kind": "messenger", "external_id": "99887766554",
                                                                 "token": "EAAG" + "z" * 40})
    assert bad.status_code == 422 and "Invalid OAuth" in bad.json()["detail"]
    owner = login(client, "root-test-key")
    assert client.get("/admin/channels", headers=owner).status_code == 403
    from app.api.admin import team  # noqa: F401  (agents are refused too, see test_team)


# ── WhatsApp conversations ──
def test_whatsapp_message_becomes_a_lead_and_gets_an_ai_reply_once(client, meta):
    a = agency(client)
    ch = connect(client, a)
    body = wa_msg(ch["external_id"], "923001112223", "Do you have 3 bed houses in DHA?", mid="wamid.once-1")
    assert post(client, body).json()["events"] == 1
    lead = lead_of(a["id"], "+923001112223")
    assert lead["name"] == "Ali Raza" and lead["channel"] == "whatsapp" and lead["contact_kind"] == "whatsapp"
    assert lead["contact_id"] == "923001112223" and lead["user_id"].startswith("wa_") and "3001112223" not in lead["user_id"]
    sent = meta.sends("923001112223")
    assert len(sent) == 1 and sent[0]["path"] == f"{ch['external_id']}/messages" and sent[0]["body"]["text"]["body"] == "stub reply"
    assert post(client, body).json()["events"] == 0                                       # Meta retry: processed once
    assert len(meta.sends("923001112223")) == 1 and len(turns) == 1
    post(client, wa_msg("000000", "923001112223", "hello"))                               # unknown number: ignored
    assert len(turns) == 1


def test_whatsapp_buyer_matches_their_website_lead(client, meta):
    a = agency(client)
    ch = connect(client, a)
    web = "lp_" + uuid.uuid4().hex
    upsert_lead(web, business_id=a["id"], name="Sana", phone="0300 7778889", channel="website")
    post(client, wa_msg(ch["external_id"], "923007778889", "Is the 10 marla still available?", name="Sana K"))
    wa_lead = rows(select(leads).where(leads.c.business_id == a["id"], leads.c.contact_kind == "whatsapp"))[0]
    assert wa_lead["merged_into"] == web
    d = client.get(f"/admin/leads/{web}", headers=a["mgr"]).json()
    assert d["contact"]["kind"] == "whatsapp" and d["contact"]["in_window"] is True


def test_stop_opts_out_and_blocks_business_messages(client, meta):
    a = agency(client)
    ch = connect(client, a, settings={"followup_template": "lp_followup"})
    post(client, wa_msg(ch["external_id"], "923004445556", "STOP"))
    lead = lead_of(a["id"], "+923004445556")
    assert lead["opted_out"] == 1 and "won't receive" in meta.sends("923004445556")[-1]["body"]["text"]["body"]
    assert channels.send_to_lead(lead, "Still interested?", "followup") == "opted_out"
    assert turns == []                                                                    # STOP never reaches the AI


def test_capture_only_at_the_plan_limit(client, meta, monkeypatch):
    monkeypatch.setitem(config.PLANS["pilot"], "conversations", 0)
    a = agency(client)
    ch = connect(client, a)
    post(client, wa_msg(ch["external_id"], "923006667778", "I want a house"))
    assert turns == [] and "sales officer" in meta.sends("923006667778")[-1]["body"]["text"]["body"]


def test_takeover_and_staff_reply(client, meta):
    a = agency(client)
    ch = connect(client, a)
    post(client, wa_msg(ch["external_id"], "923008889990", "hi"))
    lead = lead_of(a["id"], "+923008889990")
    assert client.post(f"/admin/leads/{lead['user_id']}/ai", headers=a["mgr"], json={"paused": True}).status_code == 200
    post(client, wa_msg(ch["external_id"], "923008889990", "Can you call me after 5?"))
    assert len(turns) == 1                                                                # AI stayed quiet
    assert any("call me after 5" in x["message"] for x in rows(select(alerts).where(alerts.c.lead_id == lead["user_id"])))
    r = client.post(f"/admin/leads/{lead['user_id']}/reply", headers=a["mgr"], json={"text": "Sure, I'll call at 5:30."})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert meta.sends("923008889990")[-1]["body"]["text"]["body"] == "Sure, I'll call at 5:30."
    run(update(leads).where(leads.c.user_id == lead["user_id"]).values(last_inbound_at=now() - timedelta(hours=25)))
    late = client.post(f"/admin/leads/{lead['user_id']}/reply", headers=a["mgr"], json={"text": "Hello?"})
    assert late.status_code == 409 and "24 hours" in late.json()["detail"]
    client.post(f"/admin/leads/{lead['user_id']}/ai", headers=a["mgr"], json={"paused": False})
    post(client, wa_msg(ch["external_id"], "923008889990", "ok thanks"))
    assert len(turns) == 2                                                                # AI back on
    other = agency(client)
    assert client.post(f"/admin/leads/{lead['user_id']}/reply", headers=other["mgr"], json={"text": "x"}).status_code == 404


def test_click_to_whatsapp_ad_and_whatsapp_tracked_link(client, meta):
    a = agency(client)
    ch = connect(client, a)
    post(client, wa_msg(ch["external_id"], "923001230001", "Is this available?",
                        referral={"source_url": "https://fb.me/abc", "source_type": "ad", "headline": "10 Marla <DHA> 6"}))
    l1 = lead_of(a["id"], "+923001230001")
    assert l1["channel"] == "facebook" and l1["utm"]["medium"] == "ctwa_ad" and l1["utm"]["campaign"] == "10 Marla DHA 6"
    link = client.post("/admin/links", headers=a["mgr"], json={"name": "Board WA", "channel": "qr", "target_url": "whatsapp"}).json()
    loc = client.get(f"/l/{link['id']}").headers["location"]
    assert loc.startswith("https://wa.me/923210000000?text=") and link["id"] in loc
    post(client, wa_msg(ch["external_id"], "923001230002", f"Hi! I'd like to know more. (ref {link['id']})"))
    l2 = lead_of(a["id"], "+923001230002")
    assert l2["link_id"] == link["id"] and l2["channel"] == "qr"
    assert "(ref" not in turns[-1][1]                                                     # the AI never sees the tag
    b = agency(client)
    assert client.post("/admin/links", headers=b["mgr"], json={"name": "x", "channel": "qr", "target_url": "whatsapp"}).status_code == 422


# ── lead forms and first contact ──
def test_lead_form_creates_lead_and_sends_first_whatsapp_template(client, meta):
    a = agency(client)
    wa = connect(client, a, settings={"first_template": "lp_first_contact"})
    page = connect(client, a, kind="leadgen", ext="55500011122")
    body = {"object": "page", "entry": [{"id": page["external_id"], "time": 1, "changes": [{"field": "leadgen", "value": {
        "leadgen_id": "777000111", "page_id": page["external_id"], "form_id": "42", "created_time": 1}}]}]}
    assert post(client, body).json()["events"] == 1
    lead = lead_of(a["id"], "+923005550001")
    assert lead["name"] == "Hamza Ali" and lead["source"] == "lead form" and lead["channel"] == "facebook"
    assert lead["utm"]["campaign"] == "DHA 6 launch" and "budget: 2 crore" in lead["preferences"]
    first = meta.sends("923005550001")[-1]["body"]
    assert first["type"] == "template" and first["template"]["name"] == "lp_first_contact"
    assert [x["text"] for x in first["template"]["components"][0]["parameters"]] == ["Hamza", a["dealer"]["name"]]
    assert post(client, body).json()["events"] == 0                                       # once only
    # when Hamza answers on WhatsApp it's the same buyer (matched by phone)
    post(client, wa_msg(wa["external_id"], "923005550001", "Yes, still looking"))
    wa_lead = rows(select(leads).where(leads.c.business_id == a["id"], leads.c.user_id.like("wa_%")))[0]
    assert wa_lead["merged_into"] == lead["user_id"]


# ── Messenger / Instagram ──
def test_messenger_reply_and_echoes_ignored(client, meta):
    a = agency(client)
    page = connect(client, a, kind="messenger", ext="66600011122")
    ev = lambda mid, text, echo=False, sender="2468": {"object": "page", "entry": [{"id": page["external_id"], "time": 1, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": page["external_id"]}, "timestamp": 1,
         "message": {"mid": mid, "text": text, **({"is_echo": True} if echo else {})}}]}]}
    post(client, ev("m.1", "price of the 10 marla?"))
    s = meta.sends("2468")
    assert len(s) == 1 and s[0]["path"] == "me/messages" and s[0]["body"]["message"]["text"] == "stub reply"
    post(client, ev("m.2", "stub reply", echo=True))                                     # our own message coming back
    post(client, ev("m.3", "hello", sender=page["external_id"]))
    assert len(turns) == 1


# ── follow-ups and staff alerts on WhatsApp ──
def test_followups_delivered_on_whatsapp_within_meta_rules(client, meta):
    a = agency(client)
    ch = connect(client, a)
    post(client, wa_msg(ch["external_id"], "923009990001", "hi"))
    lead = lead_of(a["id"], "+923009990001")
    from app.followups import send_followup
    from app.graph import thread_key
    from app.runtime import rt

    def draft(text):
        return run(insert(followups).values(user_id=lead["user_id"], thread_id="wa", business_id=a["id"], step=1,
                                            message=text, status="draft", created_at=now())).inserted_primary_key[0]
    assert send_followup(draft("Still keen on DHA?"), rt.graph, thread_key)["delivery"] == "sent"
    run(update(leads).where(leads.c.user_id == lead["user_id"]).values(last_inbound_at=now() - timedelta(days=2)))
    assert send_followup(draft("Any questions?"), rt.graph, thread_key)["delivery"] == "outside_window"
    client.post(f"/admin/channels/{ch['id']}", headers=a["mgr"], json={"settings": {"followup_template": "lp_followup"}})
    assert send_followup(draft("New listings in DHA"), rt.graph, thread_key)["delivery"] == "template"
    assert meta.sends("923009990001")[-1]["body"]["template"]["name"] == "lp_followup"


def test_hot_lead_alerts_the_salesperson_on_whatsapp(client, meta):
    a = agency(client)
    ch = connect(client, a, settings={"alert_template": "lp_staff_alert"})
    m = client.post("/admin/team", headers=a["mgr"], json={"name": "Bilal", "role": "agent", "phone": "0333 1112223"}).json()["member"]
    uid = "lp_" + uuid.uuid4().hex
    upsert_lead(uid, business_id=a["id"], name="Zara")
    client.post(f"/admin/leads/{uid}/assign", headers=a["mgr"], json={"member_id": m["id"]})
    upsert_lead(uid, budget_pkr=30_000_000, score="hot")
    alert = meta.sends("923331112223")[-1]["body"]
    assert alert["template"]["name"] == "lp_staff_alert" and "Zara" in alert["template"]["components"][0]["parameters"][0]["text"]


# ── retries (audit 11.2) ──
def test_temporary_failures_retry_with_backoff(client, meta):
    a = agency(client)
    ch = connect(client, a)
    meta.fail_next.append(channels.GraphError(503, "temporarily unavailable", True))
    post(client, wa_msg(ch["external_id"], "923007770001", "hi"))
    item = rows(select(outbox).where(outbox.c.to == "923007770001"))[-1]
    assert item["status"] == "queued" and item["attempts"] == 1 and item["error"]
    assert channels.flush()["sent"] == 0                                                  # not due yet (backoff)
    run(update(outbox).where(outbox.c.id == item["id"]).values(next_at=now() - timedelta(seconds=1)))
    assert channels.flush()["sent"] >= 1
    assert rows(select(outbox).where(outbox.c.id == item["id"]))[0]["status"] == "sent"
    meta.fail_next.append(channels.GraphError(400, "131026: not a WhatsApp user", False))
    post(client, wa_msg(ch["external_id"], "923007770001", "again"))
    assert rows(select(outbox).where(outbox.c.to == "923007770001"))[-1]["status"] == "failed"   # permanent: no retry
    r = client.post("/cron/outbox", headers={"X-Cron-Secret": "cron-test"})
    assert r.status_code == 200 and client.post("/cron/outbox", headers={"X-Cron-Secret": "nope"}).status_code == 401


def test_deleting_a_buyer_removes_their_messages(client, meta):
    a = agency(client)
    ch = connect(client, a)
    post(client, wa_msg(ch["external_id"], "923001010101", "hi"))
    lead = lead_of(a["id"], "+923001010101")
    assert rows(select(outbox).where(outbox.c.lead_id == lead["user_id"]))
    assert client.delete(f"/admin/leads/{lead['user_id']}", headers=a["mgr"]).status_code == 200
    assert not rows(select(outbox).where(outbox.c.lead_id == lead["user_id"])) and get_lead(lead["user_id"]) is None
