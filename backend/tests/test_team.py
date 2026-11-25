"""Phase 1: team accounts and roles, lead assignment, phone matching, tracked links, attribution, agency page.
Access-control tests follow the pre-launch audit (3.1 ownership, 3.2 no self-promotion, 3.3 server-side roles,
3.5 teams stay separate, 5.5 no open redirects, 12.3 deletion). The AI is stubbed out."""
import random
import string
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, identity
from app.db import get_lead, leads, now, rows, upsert_lead


def fake_turn(user_id, thread_id, message, dealer=None):
    upsert_lead(user_id, business_id=dealer, last_customer_at=now())       # what the graph's memory node does
    return {"reply": "stub reply", "tool_calls": [], "tokens": {"in": 10, "out": 5}, "latency_ms": 5}


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    import app.api.public as public
    monkeypatch.setattr(public, "run_turn", fake_turn)
    monkeypatch.setattr(config, "CHAT_REQUIRE_KEY", False)


def login(c, key):
    r = c.post("/admin/login", json={"key": key})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session']}"}


@pytest.fixture(scope="module")
def owner(client):
    return login(client, "root-test-key")


def agency(c, owner, **extra):
    """A new agency, signed in as its manager (the agency key the LeadPilot owner issued)."""
    did = "a-" + uuid.uuid4().hex[:8]
    for _ in range(20):                       # listing prefixes are unique; retry on the rare random collision
        prefix = "".join(random.choice(string.ascii_uppercase) for _ in range(3))
        r = c.post("/admin/owner/dealers", headers=owner, json={"id": did, "name": f"Agency {did}", "category": "property",
                                                                "listing_prefix": prefix, **extra})
        if r.status_code != 409:
            break
    assert r.status_code == 201, r.text
    out = r.json()
    return {"id": did, "dealer": out["dealer"], "mgr": login(c, out["console_key"])}


def add_member(c, a, role="agent", name="Sara Khan"):
    r = c.post("/admin/team", headers=a["mgr"], json={"name": name, "role": role, "phone": "0300 1112223"})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["key"].startswith("lpm_") and "key_hash" not in j["member"]
    return j["member"], j["key"]


def lead(business, **fields):
    uid = "lp_" + uuid.uuid4().hex
    upsert_lead(uid, business_id=business, **fields)
    return uid


# ── phone numbers ──
@pytest.mark.parametrize("raw,norm", [
    ("0300 1234567", "+923001234567"), ("+92 300 1234567", "+923001234567"), ("0092-300-1234567", "+923001234567"),
    ("3001234567", "+923001234567"), ("923001234567", "+923001234567"), ("(051) 1234567", "+92511234567"),
    ("+44 20 7946 0958", "+442079460958"), ("12", None), ("", None),
])
def test_normalize_phone(raw, norm):
    assert identity.normalize_phone(raw) == norm


# ── roles (audit 3.2 / 3.3) ──
def test_agent_cannot_use_manager_features(client, owner):
    a = agency(client, owner)
    _, key = add_member(client, a)
    ag = login(client, key)
    me = client.get("/admin/me", headers=ag).json()
    assert me["role"] == "agent" and me["member"]["name"] == "Sara Khan" and "widget_key" not in me and "plan" not in me
    for method, path, body in [("get", "/admin/team", None), ("post", "/admin/team", {"name": "Evil", "role": "manager"}),
                               ("get", "/admin/links", None), ("post", "/admin/links", {"name": "x", "channel": "qr"}),
                               ("get", "/admin/usage", None), ("post", "/admin/settings", {"assign_mode": "round_robin"}),
                               ("post", "/admin/followups/run", None), ("get", "/admin/owner/overview", None)]:
        r = getattr(client, method)(path, headers=ag, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (path, r.status_code)
    # a manager key can't reach the LeadPilot owner API either
    assert client.get("/admin/owner/overview", headers=a["mgr"]).status_code == 403


def test_agent_sees_only_assigned_leads(client, owner):
    a = agency(client, owner)
    m, key = add_member(client, a)
    ag = login(client, key)
    mine, other = lead(a["id"], name="Mine"), lead(a["id"], name="Other")
    assert client.post(f"/admin/leads/{mine}/assign", headers=a["mgr"], json={"member_id": m["id"]}).status_code == 200
    assert [x["user_id"] for x in client.get("/admin/leads", headers=ag).json()] == [mine]
    assert {x["user_id"] for x in client.get("/admin/leads", headers=a["mgr"]).json()} == {mine, other}
    assert client.get(f"/admin/leads/{other}", headers=ag).status_code == 404
    assert client.post(f"/admin/leads/{other}/status", headers=ag, json={"status": "lost"}).status_code == 404
    assert client.get(f"/admin/leads/{mine}", headers=ag).status_code == 200
    assert client.post(f"/admin/leads/{mine}/status", headers=ag, json={"status": "qualified"}).status_code == 200
    assert client.delete(f"/admin/leads/{mine}", headers=ag).status_code == 403          # deleting data: managers only
    assert client.get("/admin/stats", headers=ag).json()["leads"] == 1


def test_agencies_stay_separate(client, owner):
    a, b = agency(client, owner), agency(client, owner)
    mb, _ = add_member(client, b)
    la = lead(a["id"])
    # A's manager can't see B's lead, B's member, or give A's lead to B's salesperson
    assert client.get(f"/admin/leads/{lead(b['id'])}", headers=a["mgr"]).status_code == 404
    assert client.post(f"/admin/team/{mb['id']}", headers=a["mgr"], json={"active": False}).status_code == 404
    assert client.post(f"/admin/team/{mb['id']}/key", headers=a["mgr"]).status_code == 404
    assert client.post(f"/admin/leads/{la}/assign", headers=a["mgr"], json={"member_id": mb["id"]}).status_code == 404
    assert all(x["id"] != mb["id"] for x in client.get("/admin/team", headers=a["mgr"]).json()["members"])


def test_remove_member_and_new_key_sign_them_out(client, owner):
    a = agency(client, owner)
    m, key = add_member(client, a)
    ag = login(client, key)
    new = client.post(f"/admin/team/{m['id']}/key", headers=a["mgr"]).json()["key"]
    assert client.get("/admin/me", headers=ag).status_code == 401                         # old session revoked
    assert client.post("/admin/login", json={"key": key}).status_code == 401              # old key dead
    ag = login(client, new)
    l1 = lead(a["id"], status="qualified")
    client.post(f"/admin/leads/{l1}/assign", headers=a["mgr"], json={"member_id": m["id"]})
    assert client.post(f"/admin/team/{m['id']}", headers=a["mgr"], json={"active": False}).status_code == 200
    assert client.get("/admin/me", headers=ag).status_code == 401
    assert client.post("/admin/login", json={"key": new}).status_code == 401
    assert get_lead(l1)["assigned_to"] is None                                             # back to the queue


def test_promotion_signs_out_and_takes_effect(client, owner):
    a = agency(client, owner)
    m, key = add_member(client, a)
    ag = login(client, key)
    assert client.post(f"/admin/team/{m['id']}", headers=a["mgr"], json={"role": "manager"}).status_code == 200
    assert client.get("/admin/me", headers=ag).status_code == 401
    assert client.get("/admin/team", headers=login(client, key)).status_code == 200


def test_manager_cannot_lock_themselves_out(client, owner):
    a = agency(client, owner)
    m, key = add_member(client, a, role="manager")
    me = login(client, key)
    assert client.post(f"/admin/team/{m['id']}", headers=me, json={"active": False}).status_code == 422
    assert client.post(f"/admin/team/{m['id']}", headers=me, json={"role": "agent"}).status_code == 422


# ── assignment and alerts ──
def test_round_robin_and_hot_lead_alerts(client, owner):
    a = agency(client, owner)
    assert client.post("/admin/settings", headers=a["mgr"], json={"assign_mode": "round_robin"}).json()["assign_mode"] == "round_robin"
    m1, k1 = add_member(client, a, name="Ali")
    m2, k2 = add_member(client, a, name="Bina")
    owners = [get_lead(lead(a["id"]))["assigned_to"] for _ in range(4)]
    assert sorted(owners) == sorted([m1["id"], m2["id"]] * 2) and owners[0] != owners[1]
    # a lead turning hot alerts only its salesperson (and the managers)
    hot = lead(a["id"], name="Hamza")
    upsert_lead(hot, budget_pkr=20_000_000, score="hot")
    who = get_lead(hot)["assigned_to"]
    mine = login(client, k1 if who == m1["id"] else k2)
    theirs = login(client, k2 if who == m1["id"] else k1)
    n = client.get("/admin/notifications", headers=mine).json()
    assert n["unread"] >= 1 and any("Hot lead: Hamza" in x["message"] for x in n["items"])
    assert not any(x["lead_id"] == hot for x in client.get("/admin/notifications", headers=theirs).json()["items"])
    assert any(x["lead_id"] == hot for x in client.get("/admin/notifications", headers=a["mgr"]).json()["items"])
    client.post("/admin/notifications/read", headers=mine)
    assert client.get("/admin/notifications", headers=mine).json()["unread"] == 0


# ── one buyer across channels ──
def test_same_phone_merges_within_an_agency_only(client, owner):
    a, b = agency(client, owner), agency(client, owner)
    first = lead(a["id"], name="Ali Raza", phone="0300 7654321")
    later = lead(a["id"])
    other_agency = lead(b["id"], phone="03007654321")
    upsert_lead(later, phone="+92 300 7654321", budget_pkr=15_000_000, status="booked")
    assert get_lead(later)["merged_into"] == first
    assert get_lead(other_agency)["merged_into"] is None                                   # never across agencies
    p = get_lead(first)
    assert p["status"] == "booked" and p["budget_pkr"] == 15_000_000                       # folded into the primary
    ids = [x["user_id"] for x in client.get("/admin/leads", headers=a["mgr"]).json()]
    assert first in ids and later not in ids
    d = client.get(f"/admin/leads/{later}", headers=a["mgr"]).json()                       # alias -> primary
    assert d["lead"]["user_id"] == first and d["merged"][0]["user_id"] == later
    # deleting the buyer's data deletes every merged lead too (audit 12.3)
    assert client.delete(f"/admin/leads/{first}", headers=a["mgr"]).status_code == 200
    assert get_lead(first) is None and get_lead(later) is None and get_lead(other_agency) is not None


# ── tracked links, QR codes and attribution ──
def test_links_redirect_safely_and_attribute_leads(client, owner):
    a = agency(client, owner, allowed_origins="https://arani-test.pk")
    wk = a["dealer"]["widget_key"]
    bad = [("http://arani-test.pk/x", 422), ("https://evil.example/", 422), ("javascript:alert(1)", 422),
           ("https://user:pw@arani-test.pk/", 422)]
    for url, code in bad:
        assert client.post("/admin/links", headers=a["mgr"], json={"name": "x", "channel": "qr", "target_url": url}).status_code == code, url
    board = client.post("/admin/links", headers=a["mgr"], json={"name": "DHA board", "channel": "qr"}).json()
    site = client.post("/admin/links", headers=a["mgr"], json={"name": "FB ad", "channel": "facebook",
                                                               "target_url": "https://arani-test.pk/offers?x=1"}).json()
    r = client.get(f"/l/{board['id']}")
    assert r.status_code == 302 and r.headers["location"] == f"{config.SITE_URL}/a/{a['id']}?lp={board['id']}"
    client.get(f"/l/{board['id']}")                                                       # same person again
    assert client.get(f"/l/{site['id']}").headers["location"] == f"https://arani-test.pk/offers?x=1&lp={site['id']}"
    assert client.get("/l/0123456789").headers["location"] == config.SITE_URL + "/"        # unknown -> home
    assert client.get("/l/not-a-link").status_code == 422
    qr = client.get(f"/admin/links/{board['id']}/qr.svg", headers=a["mgr"])
    assert qr.status_code == 200 and qr.text.lstrip().startswith("<svg")

    def chat(src, user):
        return client.post("/chat", headers={"X-Widget-Key": wk},
                           json={"user_id": user, "thread_id": "t1", "message": "Show me 3 bed houses", "src": src})
    u1, u2, u3 = ("u-" + uuid.uuid4().hex[:10] for _ in range(3))
    assert chat({"link": board["id"], "via": "page"}, u1).json()["reply"] == "stub reply"
    chat({"utm_source": "Facebook", "utm_campaign": "dha-6 launch"}, u2)
    other = agency(client, owner)
    other_link = client.post("/admin/links", headers=other["mgr"], json={"name": "theirs", "channel": "instagram"}).json()
    chat({"link": other_link["id"]}, u3)                                                   # another agency's link is ignored
    assert (get_lead(u1)["channel"], get_lead(u1)["link_id"]) == ("qr", board["id"])
    assert get_lead(u2)["channel"] == "facebook" and get_lead(u2)["utm"]["campaign"] == "dha-6 launch"
    assert (get_lead(u3)["channel"], get_lead(u3)["link_id"]) == ("website", None)
    chat({"utm_source": "google"}, u1)                                                     # first touch is kept
    assert get_lead(u1)["channel"] == "qr"
    links = {x["id"]: x for x in client.get("/admin/links", headers=a["mgr"]).json()["links"]}
    assert links[board["id"]]["clicks"] == 1 and links[board["id"]]["leads"] == 1
    st = client.get("/admin/stats", headers=a["mgr"]).json()
    assert {c["channel"] for c in st["by_channel"]} >= {"qr", "facebook", "website"}
    assert st["by_link"][0]["name"] == "DHA board"
    # archived links stop working and only the agency's manager can change them
    assert client.post(f"/admin/links/{board['id']}", headers=other["mgr"], json={"active": False}).status_code == 404
    client.post(f"/admin/links/{board['id']}", headers=a["mgr"], json={"active": False})
    assert client.get(f"/l/{board['id']}").headers["location"] == config.SITE_URL + "/"


def test_agency_page(client, owner):
    a = agency(client, owner, brand_color="#0a6cff", tagline="Homes in Lahore")
    j = client.get(f"/agency/{a['id']}").json()
    assert j["widget_key"] == a["dealer"]["widget_key"] and j["brand_color"] == "#0a6cff" and j["what"] == "property"
    assert "console_key_hash" not in j and "policies" not in j and "extra_credits" not in j
    assert client.get("/agency/leadpilot").status_code == 404                              # the platform demo has no page
    assert client.get("/agency/no-such-agency").status_code == 404
    client.post("/admin/settings", headers=a["mgr"], json={"page_enabled": False})
    assert client.get(f"/agency/{a['id']}").status_code == 404
