"""Agency team members: managers and salespeople, each with a personal console key.

Keys are 192-bit random, shown once, stored only as sha256 (audit 2.2: high-entropy keys, not passwords).
Removing a member, changing their role or issuing a new key signs them out everywhere (audit 2.5).
"""
import secrets

from sqlalchemy import and_, select, update

from app.db import admin_sessions, members, now, one, rows, run

ROLES = ("manager", "agent")


def new_id() -> str:
    return "m_" + secrets.token_hex(8)


def new_key() -> str:
    return "lpm_" + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    from app.tenants import hash_key as h
    return h(key)


def member_for_key(key: str) -> dict | None:
    """An active member whose key matches. Lookup by hash of a high-entropy key (no timing leak worth having)."""
    if not key or not key.startswith("lpm_"):
        return None
    m = one(select(members).where(members.c.key_hash == hash_key(key)))
    return m if m and m["active"] else None


def is_active(member_id: str, business_id: str) -> bool:
    m = one(select(members.c.active, members.c.business_id).where(members.c.id == member_id))
    return bool(m and m["active"] and m["business_id"] == business_id)


def touch_login(member_id: str):
    run(update(members).where(members.c.id == member_id).values(last_login_at=now()))


def sign_out(member_id: str):
    run(update(admin_sessions).where(admin_sessions.c.member_id == member_id).values(revoked=1))


def get(business_id: str, member_id: str) -> dict | None:
    """A member of THIS agency only (audit 3.1/3.5)."""
    return one(select(members).where(and_(members.c.id == member_id, members.c.business_id == business_id)))


def list_for(business_id: str) -> list[dict]:
    return rows(select(members).where(members.c.business_id == business_id).order_by(members.c.active.desc(), members.c.name))


def public(m: dict) -> dict:
    """What the console sees about a member: never the key hash (audit 6.4)."""
    return {k: m.get(k) for k in ("id", "name", "phone", "role", "active", "last_login_at", "created_at")}
