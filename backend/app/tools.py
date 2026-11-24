"""The closer agent's tools (Labs 00-01: @tool, ToolRuntime, context_schema; Lab 06: store).

Isolation rule: every tool reads the customer from runtime.context (set by the server from the
request), NEVER from a model argument. So no prompt can make a tool touch another user's data.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from langchain.messages import HumanMessage
from langchain.tools import ToolRuntime, tool
from sqlalchemy import and_, insert, select, update

from app.db import bookings, businesses, listings, now, one, rows, run, upsert_lead
from app.rag import search_knowledge

PKT = timezone(timedelta(hours=5))                     # Pakistan time
SLOTS = ["10:00", "12:00", "14:00", "16:00"]           # CUSTOM: visiting hours (see knowledge base)
BUSINESS_FOR = {"property": "skyline", "car": "autohub"}
CATEGORY_OF = {v: k for k, v in BUSINESS_FOR.items()}   # dealer id -> what it sells


@dataclass
class LeadContext:
    """Set by the API for every request. The model can't change it."""
    user_id: str
    thread_id: str = "default"
    dealer: str | None = None        # set when the chat runs on one dealer's website: only its stock is shown


def dealer_of(runtime) -> str | None:
    ctx = getattr(runtime, "context", None)
    return getattr(ctx, "dealer", None)


def visible_listing(listing_id: str, dealer: str | None) -> dict | None:
    """A listing by id, limited to the current dealer's stock when the chat is dealer-scoped."""
    q = select(listings).where(listings.c.id == listing_id.strip().upper())
    if dealer:
        q = q.where(listings.c.business_id == dealer)
    return one(q)


def free_slots(listing_id: str, day: str) -> tuple[date | None, list[str], str | None]:
    """(date, free slots, error). The single source of truth for availability."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None, [], "Date must be YYYY-MM-DD."
    if d < today_pkt():
        return d, [], "That date is in the past."
    if d > today_pkt() + timedelta(days=60):
        return d, [], "We take bookings up to 60 days ahead."
    if d.weekday() == 6:
        return d, [], f"{d:%A %d %b} is a Sunday and we are closed. Monday to Saturday only."
    taken = {r["time"] for r in rows(select(bookings.c.time).where(
        bookings.c.listing_id == listing_id, bookings.c.date == d.isoformat(), bookings.c.status == "confirmed"))}
    return d, [s for s in SLOTS if s not in taken], None


def today_pkt() -> date:
    return datetime.now(PKT).date()


def pkr(n: int | None) -> str:
    if n is None:
        return "-"
    if n >= 10_000_000:
        return f"PKR {n / 10_000_000:.2f}".rstrip("0").rstrip(".") + " crore"
    return f"PKR {n / 100_000:.1f}".removesuffix(".0") + " lakh"


def fmt_listing(r: dict) -> str:
    extra = f", {r['bedrooms']} bed" if r.get("bedrooms") else (f", {r['year']} model" if r.get("year") else "")
    return f"[{r['id']}] {r['title']}{extra} | {r['location']} | {pkr(r['price_pkr'])}"


# ── Customer confirmation gate (Lab 03: ask before risky actions) ──
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|confirm|confirmed|ok|okay|book it|go ahead|please do|do it|"
                 r"haan|han|ji|jee|theek|correct|perfect|sounds good|alright)\b", re.I)
NO = re.compile(r"\b(no|not|don'?t|nahi|nahin|wait|cancel)\b", re.I)


def customer_confirmed(runtime: ToolRuntime, allow_cancel_word: bool = False) -> bool:
    """True only if the customer's latest message clearly says yes."""
    humans = [m for m in runtime.state["messages"] if isinstance(m, HumanMessage)]
    if not humans:
        return False
    text = humans[-1].text
    negative = NO.search(text.replace("cancel", "")) if allow_cancel_word else NO.search(text)
    return bool(YES.search(text)) and not negative


# ── 1. Inventory search (database) ──
@tool
def search_inventory(category: Literal["property", "car"], runtime: ToolRuntime[LeadContext],
                     max_price_pkr: int | None = None, min_price_pkr: int | None = None, location: str | None = None,
                     keyword: str | None = None, min_bedrooms: int | None = None) -> str:
    """Search available properties or cars. Prices are in PKR (1 crore = 10,000,000; 1 lakh = 100,000).
    keyword matches the title/details, e.g. "apartment", "Civic", "plot". Returns up to 5 matches with IDs."""
    dealer = dealer_of(runtime)
    if dealer:
        category = CATEGORY_OF.get(dealer, category)
    q = select(listings).where(listings.c.category == category, listings.c.status == "available")
    if dealer:
        q = q.where(listings.c.business_id == dealer)
    if max_price_pkr:
        q = q.where(listings.c.price_pkr <= int(max_price_pkr * 1.1))     # show slightly-over options too
    if min_price_pkr:
        q = q.where(listings.c.price_pkr >= min_price_pkr)
    if min_bedrooms:
        q = q.where(listings.c.bedrooms >= min_bedrooms)
    found = rows(q.order_by(listings.c.price_pkr))
    for field, value in (("location", location), ("keyword", keyword)):
        if value:
            words = [w for w in re.findall(r"[a-z0-9]+", value.lower()) if len(w) > 1]
            hay = (lambda r: r["location"].lower()) if field == "location" else \
                  (lambda r: f"{r['title']} {r['details']}".lower())
            found = [r for r in found if any(w in hay(r) for w in words)] or found
    if not found:
        return "No matching listings. Suggest widening the budget or location."
    return "\n".join(fmt_listing(r) for r in found[:5])


# ── 2. Listing details ──
@tool
def get_listing(listing_id: str, runtime: ToolRuntime[LeadContext]) -> str:
    """Full details of one listing by its ID (e.g. P101 or C203)."""
    r = visible_listing(listing_id, dealer_of(runtime))
    if not r:
        return f"No listing with ID {listing_id}."
    return f"{fmt_listing(r)}\nDetails: {r['details']}\nStatus: {r['status']}"


# ── 3. Availability (database) ──
@tool
def check_availability(listing_id: str, date: str, runtime: ToolRuntime[LeadContext]) -> str:
    """Free visit / test-drive slots for a listing on a date (YYYY-MM-DD). Open Mon-Sat."""
    listing = visible_listing(listing_id, dealer_of(runtime))
    if not listing:
        return f"No listing with ID {listing_id.strip().upper()}."
    d, free, error = free_slots(listing["id"], date)
    if error:
        return error
    return f"{d:%A %d %b %Y} free slots for {listing['id']}: {', '.join(free) if free else 'none, try another day'}"


# ── 4. Book (risky: needs the customer's explicit yes) ──
@tool
def book_visit(listing_id: str, date: str, time: str, runtime: ToolRuntime[LeadContext]) -> str:
    """Book a site visit (property) or test drive (car). ONLY call after the customer said yes to the exact
    listing, date (YYYY-MM-DD) and time (HH:MM, one of 10:00/12:00/14:00/16:00)."""
    if not customer_confirmed(runtime):
        return ("NOT BOOKED: the customer has not confirmed yet. Ask them: 'Shall I confirm <listing> on <day> "
                "at <time>?' and wait for their yes.")
    user = runtime.context.user_id
    listing = visible_listing(listing_id, dealer_of(runtime))
    if not listing:
        return f"NOT BOOKED: no listing with ID {listing_id.strip().upper()}."
    lid = listing["id"]
    time = time.strip()[:5].zfill(5)
    if time not in SLOTS:
        return f"NOT BOOKED: {time} is not a slot. Slots are {', '.join(SLOTS)}."
    d, free, error = free_slots(lid, date)
    if error:
        return f"NOT BOOKED: {error}"
    if time not in free:
        return f"NOT BOOKED: {time} is taken on {d:%A %d %b}. Free: {', '.join(free) or 'none that day'}."
    date = d.isoformat()
    kind = "site visit" if listing["category"] == "property" else "test drive"
    res = run(insert(bookings).values(user_id=user, listing_id=lid, business_id=listing["business_id"], kind=kind,
                                      date=date, time=time, status="confirmed", created_at=now()))
    booking_id = res.inserted_primary_key[0]
    upsert_lead(user, status="booked", score="hot", business_id=listing["business_id"], category=listing["category"])
    _remember(runtime, {"last_booking": f"#{booking_id} {kind} for {listing['title']} on {date} {time}"})
    return f"BOOKED #{booking_id}: {kind} for {fmt_listing(listing)} on {date} at {time}."


# ── 5. The customer's own bookings ──
@tool
def my_bookings(runtime: ToolRuntime[LeadContext]) -> str:
    """List this customer's own bookings."""
    mine = rows(select(bookings, listings.c.title).join(listings, listings.c.id == bookings.c.listing_id)
                .where(bookings.c.user_id == runtime.context.user_id).order_by(bookings.c.date))
    if not mine:
        return "No bookings yet."
    return "\n".join(f"#{b['id']} {b['kind']} {b['title']} [{b['listing_id']}] on {b['date']} {b['time']} ({b['status']})"
                     for b in mine)


# ── 6. Cancel (risky: needs the customer's explicit yes) ──
@tool
def cancel_booking(booking_id: int, runtime: ToolRuntime[LeadContext]) -> str:
    """Cancel one of this customer's bookings. ONLY after the customer confirmed they want to cancel it."""
    if not customer_confirmed(runtime, allow_cancel_word=True):
        return "NOT CANCELLED: ask the customer to confirm the cancellation first."
    b = one(select(bookings).where(and_(bookings.c.id == booking_id, bookings.c.user_id == runtime.context.user_id)))
    if not b:
        return f"No booking #{booking_id} for this customer."
    run(update(bookings).where(bookings.c.id == booking_id).values(status="cancelled"))
    return f"CANCELLED booking #{booking_id}."


# ── 7. Qualify the lead + long-term memory (Lab 06: runtime.store) ──
def score_lead(budget: int | None, months: int | None, timeline: str | None) -> str:
    if budget and (months is not None and months <= 3):
        return "hot"
    if budget or timeline or months is not None:
        return "warm"
    return "cold"


def _remember(runtime: ToolRuntime, facts: dict):
    """Merge facts into this user's long-term profile, namespace ("leads", user_id)."""
    if runtime.store is None:
        return
    ns = ("leads", runtime.context.user_id)
    old = runtime.store.get(ns, "profile")
    runtime.store.put(ns, "profile", {**(old.value if old else {}), **facts})


@tool
def save_lead_details(runtime: ToolRuntime[LeadContext], name: str | None = None, phone: str | None = None,
                      category: Literal["property", "car"] | None = None, budget_pkr: int | None = None,
                      timeline: str | None = None, timeline_months: int | None = None,
                      preferences: str | None = None, financing: str | None = None) -> str:
    """Save what the customer told you about themselves: name, phone, what they want (property/car), budget in
    PKR, buying timeline (text + months from now), preferences (area, size, model, colour...) and financing
    need. Call it as soon as you learn any of these. Saved permanently for future conversations."""
    user = runtime.context.user_id
    lead = upsert_lead(user, name=name, phone=phone, category=category, budget_pkr=budget_pkr, timeline=timeline,
                       timeline_months=timeline_months, preferences=preferences, financing=financing,
                       business_id=BUSINESS_FOR.get(category or ""))
    score = "hot" if lead["status"] == "booked" else score_lead(lead["budget_pkr"], lead["timeline_months"], lead["timeline"])
    status = lead["status"]
    if status == "new" and lead["budget_pkr"] and (lead["timeline"] or lead["timeline_months"] is not None):
        status = "qualified"
    upsert_lead(user, score=score, status=status)
    facts = {"name": name, "phone": phone, "interested_in": category, "budget_pkr": budget_pkr,
             "budget": pkr(budget_pkr) if budget_pkr else None, "timeline": timeline,
             "timeline_months": timeline_months, "preferences": preferences, "financing": financing}
    _remember(runtime, {k: v for k, v in facts.items() if v is not None})
    return "Saved to the customer's profile (internal; don't mention scores to the customer)."


# ── 8. Company knowledge (Lab 04: RAG) ──
@tool
def search_faq(question: str) -> str:
    """Search company policies: financing/installments, visit hours and rules, test drives, documents needed,
    transfer process, cancellation, company info. Cite the [source]."""
    docs = search_knowledge(question, k=2)
    return "\n\n".join(f"[{d.metadata['source']}] {d.page_content}" for d in docs) or "Nothing found."


def business_names() -> str:
    return ", ".join(f"{b['name']} ({b['category']})" for b in rows(select(businesses)))


TOOLS = [search_inventory, get_listing, check_availability, book_visit, my_bookings, cancel_booking,
         save_lead_details, search_faq]
