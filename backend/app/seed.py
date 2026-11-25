"""Demo data: two businesses (one real estate agency, one car dealer) and their inventory.

All data is made up for the demo. Prices are in PKR (1 crore = 10,000,000; 1 lakh = 100,000).
"""
from sqlalchemy import func, insert, select, update

from app.db import businesses, engine, listings, now

BUSINESSES = [
    {"id": "skyline", "name": "Skyline Realty", "category": "property", "city": "Islamabad", "phone": "051-111-759-463"},
    {"id": "autohub", "name": "AutoHub Motors", "category": "car", "city": "Islamabad", "phone": "051-111-288-648"},
]

LISTINGS = [
    # ── Skyline Realty: properties ──
    {"id": "P101", "business_id": "skyline", "category": "property", "title": "5 Marla house", "location": "Bahria Town Phase 8, Rawalpindi",
     "price_pkr": 16_500_000, "bedrooms": 3, "details": "Double storey, 3 bed, 3 bath, corner, near commercial area. Possession ready."},
    {"id": "P102", "business_id": "skyline", "category": "property", "title": "10 Marla house", "location": "DHA Phase 2, Islamabad",
     "price_pkr": 39_000_000, "bedrooms": 4, "details": "Brand new, 4 bed, 5 bath, servant quarter, park facing."},
    {"id": "P103", "business_id": "skyline", "category": "property", "title": "2 bed apartment", "location": "Gulberg Greens, Islamabad",
     "price_pkr": 9_500_000, "bedrooms": 2, "details": "4th floor, lift, 1,150 sq ft, covered parking, installment plan available."},
    {"id": "P104", "business_id": "skyline", "category": "property", "title": "1 Kanal house", "location": "F-11, Islamabad",
     "price_pkr": 95_000_000, "bedrooms": 5, "details": "Prime sector, 5 bed, basement, solar system, lawn."},
    {"id": "P105", "business_id": "skyline", "category": "property", "title": "7 Marla house", "location": "B-17 Multi Gardens, Islamabad",
     "price_pkr": 12_500_000, "bedrooms": 3, "details": "Single unit, 3 bed, gas available, near main boulevard."},
    {"id": "P106", "business_id": "skyline", "category": "property", "title": "3 bed apartment", "location": "E-11, Islamabad",
     "price_pkr": 24_000_000, "bedrooms": 3, "details": "1,850 sq ft, Margalla view, gym and backup generator."},
    {"id": "P107", "business_id": "skyline", "category": "property", "title": "8 Marla residential plot", "location": "DHA Phase 5, Islamabad",
     "price_pkr": 11_000_000, "bedrooms": None, "details": "Developed block, possession, utilities available. Good for building."},
    {"id": "P108", "business_id": "skyline", "category": "property", "title": "1 Kanal house", "location": "Bahria Enclave, Islamabad",
     "price_pkr": 62_000_000, "bedrooms": 5, "details": "Hill view, 5 bed, home theatre, double kitchen."},
    # ── AutoHub Motors: cars ──
    {"id": "C201", "business_id": "autohub", "category": "car", "title": "Toyota Corolla Altis 1.6", "location": "AutoHub G-9 showroom",
     "price_pkr": 6_200_000, "year": 2024, "details": "Automatic, 9,000 km, white, first owner, Islamabad registered."},
    {"id": "C202", "business_id": "autohub", "category": "car", "title": "Honda Civic RS Turbo", "location": "AutoHub G-9 showroom",
     "price_pkr": 9_500_000, "year": 2024, "details": "1.5 turbo, sunroof, 4,500 km, grey, company maintained."},
    {"id": "C203", "business_id": "autohub", "category": "car", "title": "Honda City 1.5 Aspire", "location": "AutoHub I-8 showroom",
     "price_pkr": 4_800_000, "year": 2023, "details": "Automatic, 21,000 km, silver, bumper to bumper genuine."},
    {"id": "C204", "business_id": "autohub", "category": "car", "title": "Suzuki Alto VXL AGS", "location": "AutoHub I-8 showroom",
     "price_pkr": 3_100_000, "year": 2024, "details": "Auto gear shift, 6,000 km, red, ideal city car."},
    {"id": "C205", "business_id": "autohub", "category": "car", "title": "KIA Sportage AWD", "location": "AutoHub G-9 showroom",
     "price_pkr": 10_500_000, "year": 2024, "details": "All wheel drive, panoramic roof, 12,000 km, black."},
    {"id": "C206", "business_id": "autohub", "category": "car", "title": "Hyundai Tucson AWD", "location": "AutoHub G-9 showroom",
     "price_pkr": 9_800_000, "year": 2023, "details": "Top of the line, 18,000 km, white, warranty remaining."},
    {"id": "C207", "business_id": "autohub", "category": "car", "title": "Toyota Yaris 1.5 ATIV X", "location": "AutoHub I-8 showroom",
     "price_pkr": 5_200_000, "year": 2023, "details": "CVT, 15,000 km, blue, excellent fuel average."},
    {"id": "C208", "business_id": "autohub", "category": "car", "title": "Suzuki Cultus VXL", "location": "AutoHub I-8 showroom",
     "price_pkr": 4_200_000, "year": 2024, "details": "AGS, 3,000 km, white, like new."},
]


# Showcase dealers (public demo) and the platform demo tenant used by the landing page's "Try it live".
SHOWCASE = {
    "skyline": {"tagline": "Houses, apartments and plots in DHA, Bahria Town, Gulberg Greens, B-17, E-11 and F-11.",
                "listing_prefix": "P", "brand_color": "#ff4f00"},
    "autohub": {"tagline": "Inspected new and used cars with a 200-point report and a 6-month engine warranty.",
                "listing_prefix": "C", "brand_color": "#ff4f00"},
    "leadpilot": {"name": "LeadPilot demo", "category": "all", "city": "Islamabad", "phone": "",
                  "tagline": "Property and cars from our showcase dealers.", "listing_prefix": "L", "brand_color": "#ff4f00"},
}


def seed():
    """Insert demo data only into empty tables, so restarts never duplicate or overwrite it."""
    with engine.begin() as c:
        if c.execute(select(func.count()).select_from(businesses)).scalar() == 0:
            c.execute(insert(businesses), BUSINESSES)
        if c.execute(select(func.count()).select_from(listings)).scalar() == 0:
            rows = [{"bedrooms": None, "year": None, "image": "", "status": "available", **row} for row in LISTINGS]
            c.execute(insert(listings), rows)
    ensure_dealers()


def ensure_dealers():
    """Backfill fields added later (plan, keys, branding) without touching anything a person already set."""
    from app.tenants import new_widget_key
    with engine.begin() as c:
        existing = {r.id for r in c.execute(select(businesses.c.id))}
        if "leadpilot" not in existing:
            c.execute(insert(businesses).values(id="leadpilot", **{k: v for k, v in SHOWCASE["leadpilot"].items()}))
        for r in c.execute(select(businesses)).mappings().all():
            fix = {}
            if not r["widget_key"]:
                fix["widget_key"] = new_widget_key()
            showcase = r["id"] in SHOWCASE
            # Showcase (public demo) dealers are always public and never run out (the column defaults
            # would otherwise make them hidden "pilot" dealers on a fresh database).
            if showcase and r["showcase"] != 1:
                fix["showcase"] = 1
            if showcase and r["plan"] in (None, "", "pilot"):
                fix["plan"] = "internal"
            elif not r["plan"]:
                fix["plan"] = "pilot"
            if not r["status"]:
                fix["status"] = "active"
            if r["extra_credits"] is None:
                fix["extra_credits"] = 0
            if r["showcase"] is None and not showcase:
                fix["showcase"] = 0
            if not r["created_at"]:
                fix["created_at"] = now()
            for k, v in SHOWCASE.get(r["id"], {}).items():
                if k in ("tagline", "listing_prefix", "brand_color") and not r[k]:
                    fix[k] = v
            if fix:
                c.execute(update(businesses).where(businesses.c.id == r["id"]).values(**fix))
