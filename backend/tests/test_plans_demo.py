"""Plan features enforced on the server (audit 3.4), demo channels (real pipeline, nothing sent to Meta),
and the owner's go-live checklist (never returns secrets)."""
import hashlib
import hmac
import json
import os
import random
import string
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import channels, config
from app.db import leads, now, outbox, rows, upsert_lead

turns = []


def fake_turn(user_id, thread_id, message, dealer=None):
    turns.append(message)
    upsert_lead(user_id, business_id=dealer, last_customer_at=now())
    return {"reply": "Hi! What's your budget?", "tool_calls": [], "tokens": {"in": 10, "out": 5}, "latency_ms": 5}


def no_network(*a, **k):
    raise AssertionError("demo channels must never call Meta")


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    import app.api.webhooks as wh
    monkeypatch.setattr(wh, "run_turn", fake_turn)
    monkeypatch.setattr(channels, "graph", no_network)
    monkeypatch.setattr(config, "META_APP_SECRET", "")           # Meta NOT configured: demo must still work
    monkeypatch.setattr(config, "CHANNEL_KEY", "")
    turns.clear()


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(c, key):
    r = c.post("/admin/login", json={"key": key})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session']}"}


def agency(c, plan="pilot"):
    owner = login(c, "root-test-key")
    did = "d-" + uuid.uuid4().hex[:8]
    for _ in range(20):
        r = c.post("/admin/owner/dealers", headers=owner, json={"id": did, "name": f"Agency {did}", "category": "property", "plan": plan,
                                                                "listing_prefix": "".join(random.choices(string.ascii_uppercase, k=3))})
        if r.status_code != 409:
            break
    assert r.status_code == 201, r.text
    return {"id": did, "mgr": login(c, r.json()["console_key"]), "name": r.json()["dealer"]["name"]}


# ── plans (audit 3.4) ──
def test_starter_has_no_meta_channels_and_two_seats(client):
    a = agency(client, "starter")
    for kind in ("whatsapp", "messenger", "instagram", "leadgen"):
        r = client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": kind})
        assert r.status_code == 403 and "Starter" in r.json()["detail"], kind
    r = client.post("/admin/channels", headers=a["mgr"], json={"kind": "whatsapp", "external_id": "12345678", "token": "EAAG" + "x" * 40})
    assert r.status_code == 403                                    # plan checked before anything else
    for i in range(2):
        assert client.post("/admin/team", headers=a["mgr"], json={"name": f"Rep {i}", "role": "agent"}).status_code == 201
    r = client.post("/admin/team", headers=a["mgr"], json={"name": "Rep 3", "role": "agent"})
    assert r.status_code == 403 and "2 team logins" in r.json()["detail"]
    ch = client.get("/admin/channels", headers=a["mgr"]).json()
    assert ch["plan"] == "Starter" and ch["included"] == []


def test_growth_gets_whatsapp_and_lead_forms_only(client):
    a = agency(client, "growth")
    assert client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "whatsapp"}).status_code == 201
    assert client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "leadgen"}).status_code == 201
    assert client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "instagram"}).status_code == 403


# ── demo channels: the real pipeline without Meta ──
def test_demo_whatsapp_runs_the_real_pipeline(client):
    a = agency(client)
    ch = client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "whatsapp"}).json()
    assert ch["status"] == "demo" and ch["display"] == "Demo WhatsApp"
    assert client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "whatsapp"}).json()["id"] == ch["id"]  # one per kind
    r = client.post(f"/admin/channels/{ch['id']}/simulate", headers=a["mgr"],
                    json={"name": "Ali Raza", "phone": "0300 1234567", "text": "Do you have 10 marla houses in DHA?"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["messages"][-1] == {"kind": "text", "purpose": "reply", "status": "simulated", "text": "Hi! What's your budget?"}
    lead = rows(select(leads).where(leads.c.user_id == j["lead_id"]))[0]
    assert lead["channel"] == "whatsapp" and lead["phone_norm"] == "+923001234567" and lead["contact_kind"] == "whatsapp"
    # takeover and reply from the console work on the demo channel too
    assert client.post(f"/admin/leads/{lead['user_id']}/reply", headers=a["mgr"], json={"text": "I'll call you at 5."}).json()["sent"]
    assert rows(select(outbox).where(outbox.c.lead_id == lead["user_id"]))[-1]["status"] == "simulated"
    client.post(f"/admin/channels/{ch['id']}/simulate", headers=a["mgr"], json={"name": "Ali Raza", "phone": "0300 1234567", "text": "ok"})
    assert turns == ["Do you have 10 marla houses in DHA?"]          # taken over: the AI stayed quiet
    # a demo channel can be turned off and on, but never becomes "live" without a Meta token
    assert client.post(f"/admin/channels/{ch['id']}", headers=a["mgr"], json={"status": "disabled"}).json()["status"] == "disabled"
    assert client.post(f"/admin/channels/{ch['id']}", headers=a["mgr"], json={"status": "active"}).json()["status"] == "demo"
    # only demo channels can be simulated, and only by that agency's manager
    other = agency(client)
    assert client.post(f"/admin/channels/{ch['id']}/simulate", headers=other["mgr"], json={"name": "X Y", "text": "hi"}).status_code == 404


def test_demo_lead_form_gets_first_whatsapp_template(client):
    a = agency(client)
    client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "whatsapp"})
    form = client.post("/admin/channels/demo", headers=a["mgr"], json={"kind": "leadgen"}).json()
    j = client.post(f"/admin/channels/{form['id']}/simulate", headers=a["mgr"],
                    json={"name": "Hamza Ali", "phone": "+92 333 9998887", "budget": "2 crore"}).json()
    lead = rows(select(leads).where(leads.c.user_id == j["lead_id"]))[0]
    assert lead["source"] == "lead form" and lead["channel"] == "facebook" and "budget: 2 crore" in lead["preferences"]
    assert j["messages"][0]["purpose"] == "first_contact" and "Hamza" in j["messages"][0]["text"] and a["name"] in j["messages"][0]["text"]


def test_downgraded_agency_stops_answering_on_meta(client, monkeypatch):
    monkeypatch.setattr(config, "META_APP_SECRET", "s3cret")
    monkeypatch.setattr(config, "CHANNEL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(channels, "graph", lambda *a, **k: {"display_phone_number": "+92 321 1111111", "messages": [{"id": "w"}]})
    a = agency(client)
    ch = client.post("/admin/channels", headers=a["mgr"], json={"kind": "whatsapp", "external_id": str(random.randrange(10**12)),
                                                                 "token": "EAAG" + "x" * 40}).json()
    owner = login(client, "root-test-key")
    client.post(f"/admin/owner/dealers/{a['id']}", headers=owner, json={"plan": "starter"})
    body = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": ch["external_id"]}, "contacts": [{"wa_id": "923005556667", "profile": {"name": "Z"}}],
        "messages": [{"from": "923005556667", "id": "wamid." + uuid.uuid4().hex, "type": "text", "text": {"body": "hi"}}]}}]}]}
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    client.post("/webhooks/meta", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert turns == []
    stored = rows(select(channels.channels).where(channels.channels.c.id == ch["id"]))[0]
    assert stored["last_error"] == "Not included in the agency's plan"


# ── owner's go-live checklist ──
def test_system_checklist_never_shows_secrets(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret-value")
    owner = login(client, "root-test-key")
    r = client.get("/admin/owner/system", headers=owner)
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()["items"]}
    assert {"ai_key", "meta", "lockdown", "hosting", "database"} <= set(items)
    assert items["ai_key"]["detail"].startswith("set") and items["meta"]["ok"] is False
    assert "very-secret" not in r.text and "root-test-key" not in r.text
    a = agency(client)
    assert client.get("/admin/owner/system", headers=a["mgr"]).status_code == 403
