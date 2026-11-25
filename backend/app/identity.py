"""Who a lead is, where they came from, and who on the agency team owns them.

- Phone matching: one buyer = one lead. When a lead gives a phone number that another lead of the SAME agency
  already has, the newer lead is merged into the older one (never across agencies, audit 3.5 / 8.5).
- Attribution: the first channel/tracked link/UTM campaign that brought the buyer (first touch).
- Assignment: new leads go to the next salesperson when the agency uses round-robin.
- Alerts: the assigned salesperson (or the managers) hear about hot leads and booked visits.

Every hook is best-effort: a failure here is logged and never breaks a buyer's chat.
"""
import logging
import re

from sqlalchemy import and_, select, update

from app.db import businesses, engine, leads, links, members, now, one, rows, run

log = logging.getLogger("leadpilot")

CHANNELS = ("website", "page", "form", "qr", "facebook", "instagram", "whatsapp", "google", "tiktok", "youtube",
            "email", "sms", "print", "portal", "other")
UTM_CHANNEL = {"facebook": "facebook", "fb": "facebook", "meta": "facebook", "instagram": "instagram", "ig": "instagram",
               "google": "google", "adwords": "google", "whatsapp": "whatsapp", "wa": "whatsapp", "tiktok": "tiktok",
               "youtube": "youtube", "yt": "youtube", "email": "email", "newsletter": "email", "sms": "sms",
               "zameen": "portal", "graana": "portal", "olx": "portal", "qr": "qr"}
STATUS_RANK = {"new": 0, "qualified": 1, "lost": 1, "booked": 2}
SCORE_RANK = {"cold": 0, "warm": 1, "hot": 2}
COPY_IF_MISSING = ("name", "phone", "category", "budget_pkr", "timeline", "timeline_months", "preferences", "financing")


# ───────────────────────── phone numbers ─────────────────────────
def normalize_phone(raw: str | None) -> str | None:
    """Pakistani numbers in any common format -> +923001234567. Other countries: +<digits>. Junk -> None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("03"):          # 0300 1234567
        return "+92" + digits[1:]
    if len(digits) == 10 and digits.startswith("3"):           # 300 1234567
        return "+92" + digits
    if len(digits) == 12 and digits.startswith("923"):         # 92 300 1234567
        return "+" + digits
    if digits.startswith("92") and 11 <= len(digits) <= 12:    # landlines like 92 51 1234567
        return "+" + digits
    if len(digits) == 10 and digits.startswith("0"):           # 051 1234567 (landline)
        return "+92" + digits[1:]
    if 8 <= len(digits) <= 15 and not digits.startswith("0"):
        return "+" + digits
    return None


# ───────────────────────── the hook ─────────────────────────
def after_change(before: dict | None, after: dict | None):
    """Runs after every lead write (db.upsert_lead)."""
    if not after:
        return
    try:
        if after.get("merged_into"):
            _propagate(after)
            return
        if after.get("business_id") and after.get("phone_norm") and after["phone_norm"] != (before or {}).get("phone_norm"):
            if _merge_by_phone(after):
                return
        if after.get("business_id") and not after.get("assigned_to") and not (before or {}).get("business_id"):
            auto_assign(after)
        _alerts(before, after)
    except Exception:
        log.exception("lead hook failed")


def _merge_by_phone(lead: dict) -> bool:
    """If an older lead of the same agency has this phone, fold this one into it. Returns True if merged."""
    primary = one(select(leads).where(and_(
        leads.c.business_id == lead["business_id"], leads.c.phone_norm == lead["phone_norm"],
        leads.c.merged_into.is_(None), leads.c.user_id != lead["user_id"])).order_by(leads.c.created_at).limit(1))
    if not primary:
        return False
    with engine.begin() as c:
        # this lead and anything already merged into it now point at the primary
        c.execute(update(leads).where(leads.c.user_id == lead["user_id"])
                  .values(merged_into=primary["user_id"], assigned_to=primary.get("assigned_to")))
        c.execute(update(leads).where(leads.c.merged_into == lead["user_id"])
                  .values(merged_into=primary["user_id"], assigned_to=primary.get("assigned_to")))
    _propagate({**lead, "merged_into": primary["user_id"]})
    _share_memory(primary["user_id"], lead["user_id"])
    from app.security import log_event
    log_event("lead_merged_by_phone", "", {"business": lead["business_id"]})
    return True


def _propagate(alias: dict):
    """Fold what the buyer told us under the alias id into the primary lead (never downgrade anything)."""
    primary = one(select(leads).where(leads.c.user_id == alias["merged_into"]))
    if not primary:
        return
    changes = {k: alias[k] for k in COPY_IF_MISSING if alias.get(k) and not primary.get(k)}
    if STATUS_RANK.get(alias.get("status"), 0) > STATUS_RANK.get(primary.get("status"), 0):
        changes["status"] = alias["status"]
    if SCORE_RANK.get(alias.get("score"), 0) > SCORE_RANK.get(primary.get("score"), 0):
        changes["score"] = alias["score"]
    for k in ("last_customer_at", "last_agent_at"):
        if alias.get(k) and (not primary.get(k) or _ts(alias[k]) > _ts(primary[k])):
            changes[k] = alias[k]
    if changes:
        run(update(leads).where(leads.c.user_id == primary["user_id"]).values(**changes))
        _alerts(primary, {**primary, **changes})


def _ts(v):
    return v.timestamp() if hasattr(v, "timestamp") else 0


def _share_memory(primary_id: str, alias_id: str):
    """The assistant should recognise the returning buyer: copy the known profile into the new id's memory
    (same agency only, because merging never crosses agencies)."""
    try:
        from app.runtime import rt
        if rt.store is None:
            return
        p = rt.store.get(("leads", primary_id), "profile")
        a = rt.store.get(("leads", alias_id), "profile")
        if p and p.value:
            rt.store.put(("leads", alias_id), "profile", {**p.value, **(a.value if a else {}), "returning_customer": True})
    except Exception:
        log.exception("could not share memory between merged leads")


def aliases_of(user_id: str) -> list[str]:
    return [r["user_id"] for r in rows(select(leads.c.user_id).where(leads.c.merged_into == user_id))]


# ───────────────────────── assignment ─────────────────────────
def auto_assign(lead: dict):
    biz = one(select(businesses.c.assign_mode).where(businesses.c.id == lead["business_id"]))
    if not biz or (biz["assign_mode"] or "manual") != "round_robin":
        return
    team = rows(select(members.c.id, members.c.last_assigned_at).where(and_(
        members.c.business_id == lead["business_id"], members.c.active == 1, members.c.role == "agent")))
    if not team:
        return
    team.sort(key=lambda m: (m["last_assigned_at"] is not None, _ts(m["last_assigned_at"])))
    assign(lead["user_id"], team[0]["id"])


def assign(user_id: str, member_id: str | None):
    """Give a lead (and every alias merged into it) to one team member, or unassign with None."""
    with engine.begin() as c:
        c.execute(update(leads).where(leads.c.user_id == user_id).values(assigned_to=member_id))
        c.execute(update(leads).where(leads.c.merged_into == user_id).values(assigned_to=member_id))
        if member_id:
            c.execute(update(members).where(members.c.id == member_id).values(last_assigned_at=now()))


# ───────────────────────── alerts for the team ─────────────────────────
def _who(lead: dict) -> str:
    bits = [lead.get("name") or "A buyer"]
    if lead.get("budget_pkr"):
        from app.tools import pkr
        bits.append(pkr(lead["budget_pkr"]))
    if lead.get("timeline"):
        bits.append(str(lead["timeline"])[:40])
    return ", ".join(bits)


def _alerts(before: dict | None, after: dict):
    if not after.get("business_id"):
        return
    from app import tenants
    b = before or {}
    if after.get("score") == "hot" and b.get("score") != "hot" and after.get("status") != "booked":
        tenants.alert(after["business_id"], f"hot:{after['user_id']}"[:80], "warning",
                      f"Hot lead: {_who(after)}. Call them while they're interested.",
                      member_id=after.get("assigned_to"), lead_id=after["user_id"])
    if after.get("status") == "booked" and b.get("status") != "booked":
        tenants.alert(after["business_id"], f"booked:{after['user_id']}"[:80], "info",
                      f"Visit booked by {_who(after)}. Confirm it with a call.",
                      member_id=after.get("assigned_to"), lead_id=after["user_id"])


# ───────────────────────── attribution (first touch) ─────────────────────────
def channel_for(src: dict | None, dealer_id: str) -> tuple[str, str | None, dict | None]:
    """(channel, link_id, utm) for a visit. Links from other agencies are ignored (can't be spoofed across tenants)."""
    src = src or {}
    utm = {k: src.get(f"utm_{k}") for k in ("source", "medium", "campaign") if src.get(f"utm_{k}")} or None
    if src.get("link"):
        link = one(select(links).where(and_(links.c.id == src["link"], links.c.business_id == dealer_id,
                                            links.c.active == 1)))
        if link:
            return link["channel"], link["id"], utm
    if utm and utm.get("source"):
        return UTM_CHANNEL.get(utm["source"].strip().lower(), "other"), None, utm
    return ("page" if src.get("via") == "page" else src.get("default") or "website"), None, utm


def touch(user_id: str, dealer: dict | None, src: dict | None, default: str = "website"):
    """Record where this buyer came from, once (first touch). Never overwrites."""
    if not dealer or dealer.get("category") == "all":
        return
    try:
        lead = one(select(leads.c.channel, leads.c.business_id).where(leads.c.user_id == user_id))
        if not lead or lead["channel"] or (lead["business_id"] and lead["business_id"] != dealer["id"]):
            return
        channel, link_id, utm = channel_for({**(src or {}), "default": default}, dealer["id"])
        run(update(leads).where(and_(leads.c.user_id == user_id, leads.c.channel.is_(None)))
            .values(channel=channel, link_id=link_id, utm=utm))
    except Exception:
        log.exception("attribution failed")
