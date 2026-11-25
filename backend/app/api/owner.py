"""Owner (LeadPilot team) endpoints: create and manage dealers without touching the database or redeploying.

Every route needs the owner session (the "*" key in ADMIN_TOKENS). Dealers can never reach these.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, insert, select, update

from app import config, tenants
from app.api.admin import super_admin
from app.db import (admin_sessions, bookings, businesses, credit_ledger, engine, leads, listings, now, one, rows, run,
                    usage)
from app.security import log_event

router = APIRouter(prefix="/admin/owner")
Plan = Literal["pilot", "starter", "growth", "scale", "internal"]
HEX = r"^#[0-9a-fA-F]{6}$"
ORIGINS = r"^(https?://[a-zA-Z0-9.\-]+(:\d+)?)(\s*,\s*https?://[a-zA-Z0-9.\-]+(:\d+)?)*$"

PUBLIC_FIELDS = ("id", "name", "category", "city", "phone", "tagline", "brand_color", "policies", "listing_prefix",
                 "plan", "status", "extra_credits", "widget_key", "allowed_origins", "showcase", "created_at")


def public(d: dict) -> dict:
    out = {k: d.get(k) for k in PUBLIC_FIELDS}
    out["has_console_key"] = bool(d.get("console_key_hash"))
    return out


def must_get(dealer_id: str) -> dict:
    tenants.invalidate()
    d = tenants.get(dealer_id)
    if d is None:
        raise HTTPException(404, "Dealer not found")
    return d


def revoke_sessions(dealer_id: str):
    run(update(admin_sessions).where(admin_sessions.c.business == dealer_id).values(revoked=1))


# ───────────────────────── overview ─────────────────────────

@router.get("/overview")
def overview(_: str = Depends(super_admin)):
    """This month across all dealers: conversations, AI cost, subscription revenue and margin."""
    month = tenants.month_key()
    used = {r["business_id"]: r for r in rows(select(usage).where(usage.c.month == month))}
    lead_counts = {r["business_id"]: r["n"] for r in rows(select(leads.c.business_id, func.count().label("n")).group_by(leads.c.business_id))}
    listing_counts = {r["business_id"]: r["n"] for r in rows(select(listings.c.business_id, func.count().label("n"))
                                                             .where(listings.c.status == "available").group_by(listings.c.business_id))}
    out, revenue, cost = [], 0, 0.0
    for d in tenants.all_dealers():
        s = tenants.summary(d)
        if s["status"] == "active" and not d.get("showcase"):
            revenue += s["price_usd"]
        cost += s["cost_usd"]
        out.append({**public(d), "usage": s, "leads": lead_counts.get(d["id"], 0), "listings": listing_counts.get(d["id"], 0)})
    return {"month": month, "dealers": out, "revenue_usd": revenue, "ai_cost_usd": round(cost, 4),
            "margin_usd": round(revenue - cost, 2), "plans": config.PLANS, "chat_requires_key": config.CHAT_REQUIRE_KEY}


# ───────────────────────── create / update ─────────────────────────

class DealerIn(BaseModel):
    id: str = Field(pattern=tenants.ID_RE.pattern)
    name: str = Field(min_length=2, max_length=120)
    category: Literal["property", "car"]
    city: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=40)
    tagline: str | None = Field(default=None, max_length=240)
    brand_color: str | None = Field(default=None, pattern=HEX)
    policies: str | None = Field(default=None, max_length=20000)
    listing_prefix: str = Field(pattern=r"^[A-Za-z]{1,3}$")
    plan: Plan = "pilot"
    allowed_origins: str | None = Field(default=None, max_length=1000, pattern=ORIGINS)

    @field_validator("listing_prefix")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()


@router.post("/dealers", status_code=201)
def create_dealer(body: DealerIn, _: str = Depends(super_admin)):
    if tenants.get(body.id) or one(select(businesses.c.id).where(businesses.c.id == body.id)):
        raise HTTPException(409, "A dealer with that id already exists.")
    if one(select(businesses.c.id).where(businesses.c.listing_prefix == body.listing_prefix)):
        raise HTTPException(409, f"Listing prefix {body.listing_prefix} is already used by another dealer.")
    console_key = tenants.new_console_key(body.id)
    run(insert(businesses).values(**body.model_dump(), status="active", extra_credits=0, showcase=0,
                                  widget_key=tenants.new_widget_key(), console_key_hash=tenants.hash_key(console_key),
                                  created_at=now()))
    tenants.invalidate()
    log_event("owner_created_dealer", "", {"dealer": body.id, "plan": body.plan})
    # The console key is shown ONCE. Only its hash is stored.
    return {"dealer": public(must_get(body.id)), "console_key": console_key}


class DealerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    city: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=40)
    tagline: str | None = Field(default=None, max_length=240)
    brand_color: str | None = Field(default=None, pattern=HEX)
    policies: str | None = Field(default=None, max_length=20000)
    plan: Plan | None = None
    status: Literal["active", "suspended"] | None = None
    allowed_origins: str | None = Field(default=None, max_length=1000)


@router.get("/dealers/{dealer_id}")
def dealer_detail(dealer_id: str, _: str = Depends(super_admin)):
    d = must_get(dealer_id)
    return {"dealer": public(d), "usage": tenants.summary(d),
            "history": rows(select(usage).where(usage.c.business_id == dealer_id).order_by(usage.c.month.desc()).limit(12)),
            "ledger": rows(select(credit_ledger).where(credit_ledger.c.business_id == dealer_id).order_by(credit_ledger.c.id.desc()).limit(50)),
            "listings": rows(select(listings).where(listings.c.business_id == dealer_id).order_by(listings.c.status, listings.c.id))}


@router.post("/dealers/{dealer_id}")
def update_dealer(dealer_id: str, body: DealerPatch, _: str = Depends(super_admin)):
    d = must_get(dealer_id)
    changes = body.model_dump(exclude_unset=True)
    if "allowed_origins" in changes:
        o = (changes["allowed_origins"] or "").strip()
        import re
        if o and not re.match(ORIGINS, o):
            raise HTTPException(422, "Websites must be full origins like https://arani.pk, separated by commas.")
        changes["allowed_origins"] = o or None
    if not changes:
        return {"dealer": public(d)}
    run(update(businesses).where(businesses.c.id == dealer_id).values(**changes))
    if changes.get("status") == "suspended":
        revoke_sessions(dealer_id)                     # a suspended dealer is signed out everywhere
    if "plan" in changes and changes["plan"] != d.get("plan"):
        tenants.alert(dealer_id, f"{dealer_id}:plan:{tenants.month_key()}:{changes['plan']}", "info",
                      f"Your plan is now {config.PLANS[changes['plan']]['name']}.")
    log_event("owner_updated_dealer", "", {"dealer": dealer_id, "fields": sorted(changes)})
    return {"dealer": public(must_get(dealer_id))}


class CreditsIn(BaseModel):
    amount: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=3, max_length=200)


@router.post("/dealers/{dealer_id}/credits")
def credits(dealer_id: str, body: CreditsIn, _: str = Depends(super_admin)):
    d = must_get(dealer_id)
    if body.amount == 0:
        raise HTTPException(422, "Amount can't be zero.")
    if (d.get("extra_credits") or 0) + body.amount < 0:
        raise HTTPException(422, f"The dealer only has {d.get('extra_credits') or 0} credits.")
    tenants.add_credits(dealer_id, body.amount, body.reason)
    log_event("owner_credits", "", {"dealer": dealer_id, "amount": body.amount})
    return {"dealer": public(must_get(dealer_id))}


class RotateIn(BaseModel):
    kind: Literal["console", "widget"]


@router.post("/dealers/{dealer_id}/rotate")
def rotate(dealer_id: str, body: RotateIn, _: str = Depends(super_admin)):
    must_get(dealer_id)
    if body.kind == "console":
        key = tenants.new_console_key(dealer_id)
        run(update(businesses).where(businesses.c.id == dealer_id).values(console_key_hash=tenants.hash_key(key)))
        revoke_sessions(dealer_id)
    else:
        key = tenants.new_widget_key()
        run(update(businesses).where(businesses.c.id == dealer_id).values(widget_key=key))
    tenants.invalidate()
    log_event("owner_rotated_key", "", {"dealer": dealer_id, "kind": body.kind})
    return {"kind": body.kind, "key": key}


# ───────────────────────── listings import (from CSV in the console) ─────────────────────────

class ListingRow(BaseModel):
    id: str | None = Field(default=None, pattern=tenants.LISTING_ID_PATTERN)
    title: str = Field(min_length=2, max_length=200)
    location: str = Field(default="", max_length=120)
    price_pkr: int = Field(gt=0, lt=10_000_000_000)
    bedrooms: int | None = Field(default=None, ge=0, le=50)
    year: int | None = Field(default=None, ge=1950, le=2100)
    details: str = Field(default="", max_length=1000)


class ImportIn(BaseModel):
    mode: Literal["append", "replace"] = "append"
    rows: list[ListingRow] = Field(min_length=1, max_length=2000)


@router.post("/dealers/{dealer_id}/listings")
def import_listings(dealer_id: str, body: ImportIn, _: str = Depends(super_admin)):
    d = must_get(dealer_id)
    if d["category"] not in ("property", "car"):
        raise HTTPException(422, "This dealer can't have listings.")
    prefix = (d.get("listing_prefix") or "").upper()
    if not prefix:
        raise HTTPException(422, "Set a listing prefix for this dealer first.")
    with engine.begin() as c:
        mine = {r.id: r for r in c.execute(select(listings.c.id, listings.c.business_id).where(listings.c.id.like(f"{prefix}%")))}
        taken_elsewhere = {i for i, r in mine.items() if r.business_id != dealer_id}
        numbers = [int(i[len(prefix):]) for i, r in mine.items() if i[len(prefix):].isdigit()]
        next_no = max(numbers, default=0) + 1
        seen, created, updated = set(), 0, 0
        for row in body.rows:
            lid = (row.id or "").upper()
            if lid and not lid.startswith(prefix):
                raise HTTPException(422, f"Listing id {lid} must start with this dealer's prefix {prefix}.")
            if lid in taken_elsewhere:
                raise HTTPException(409, f"Listing id {lid} belongs to another dealer.")
            if not lid:
                lid = f"{prefix}{next_no:03d}"
                next_no += 1
            if lid in seen:
                raise HTTPException(422, f"Listing id {lid} appears twice in the file.")
            seen.add(lid)
            values = {"business_id": dealer_id, "category": d["category"], "title": row.title.strip(),
                      "location": row.location.strip(), "price_pkr": row.price_pkr, "bedrooms": row.bedrooms,
                      "year": row.year, "details": row.details.strip(), "status": "available", "image": ""}
            if lid in mine:
                c.execute(update(listings).where(listings.c.id == lid).values(**values)); updated += 1
            else:
                c.execute(insert(listings).values(id=lid, **values)); created += 1
        archived = 0
        if body.mode == "replace":   # listings not in the file are archived (kept for past bookings, never shown)
            archived = c.execute(update(listings).where(and_(listings.c.business_id == dealer_id, listings.c.id.not_in(seen),
                                                             listings.c.status == "available")).values(status="archived")).rowcount
    log_event("owner_imported_listings", "", {"dealer": dealer_id, "created": created, "updated": updated, "archived": archived})
    return {"created": created, "updated": updated, "archived": archived}
