"""Security helpers (pre-launch audit): rate limits, customer tokens, admin sessions, client IP, event log.

Everything here is enforced on the server. Nothing relies on the UI.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import delete, insert, select, update

from app import config
from app.db import admin_sessions, audit_log, engine, is_postgres, now, rate_limits, run, one

log = logging.getLogger("leadpilot.security")

# Ids we accept from callers: letters, digits and a few separators. Blocks HTML/JS in ids (audit 5.1, 5.2).
ID_PATTERN = r"^[A-Za-z0-9_.:@\-]{1,120}$"
ID_RE = re.compile(ID_PATTERN)
SITE_USER_PREFIX = "lp_"   # ids issued by our website; these need the signed customer token


# ───────────────────────── client IP (audit 6.1) ─────────────────────────
def client_ip(request: Request) -> str:
    """Render sits behind Cloudflare. CF-Connecting-IP is set by Cloudflare's edge (the client can't spoof it
    through Cloudflare). Fallback: the LAST X-Forwarded-For entry (added by the platform proxy), never the first."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff:
        return xff[-1][:64]
    return (request.client.host if request.client else "unknown")[:64]


# ───────────────────────── rate limits in the database (audit 6.1, 8.1) ─────────────────────────
def _rl_key(scope: str, ident: str, window_s: int, bucket: int) -> str:
    return hashlib.sha256(f"{scope}|{ident}|{window_s}|{bucket}".encode()).hexdigest()[:40]


def _sliding(c, scope: str, ident: str, window_s: int, current: int) -> float:
    """Sliding-window estimate: this window's count plus the previous window's count weighted by how much of
    it still overlaps the last `window_s` seconds. Stops a burst that straddles a window boundary from getting
    double the allowance (a plain fixed window resets to zero at the boundary)."""
    now_s = time.time()
    bucket = int(now_s // window_s)
    prev = c.execute(select(rate_limits.c.count).where(rate_limits.c.key == _rl_key(scope, ident, window_s, bucket - 1))).scalar() or 0
    return current + prev * (1 - (now_s % window_s) / window_s)


def hit(scope: str, ident: str, limit: int, window_s: int) -> bool:
    """Count one hit, shared by all server instances and surviving restarts. Returns True if still within the limit."""
    bucket = int(time.time() // window_s)
    key = _rl_key(scope, ident, window_s, bucket)
    expires = datetime.now(timezone.utc) + timedelta(seconds=2 * window_s)   # kept one extra window for _sliding
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(rate_limits).values(key=key, count=1, expires_at=expires)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"count": rate_limits.c.count + 1}).returning(rate_limits.c.count)
    with engine.begin() as c:
        count = c.execute(stmt).scalar()
        estimate = _sliding(c, scope, ident, window_s, count)
        if secrets.randbelow(200) == 0:   # occasional cleanup of old windows
            c.execute(delete(rate_limits).where(rate_limits.c.expires_at < datetime.now(timezone.utc)))
    return estimate <= limit


def peek(scope: str, ident: str, window_s: int) -> float:
    """Current (sliding) count, without adding a hit."""
    bucket = int(time.time() // window_s)
    with engine.connect() as c:
        current = c.execute(select(rate_limits.c.count).where(rate_limits.c.key == _rl_key(scope, ident, window_s, bucket))).scalar() or 0
        return _sliding(c, scope, ident, window_s, current)


def enforce(scope: str, ident: str, limit: int, window_s: int):
    if not hit(scope, ident, limit, window_s):
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.")


# ───────────────────────── customer tokens (audit 2.1, 3.1) ─────────────────────────
def _sign(data: str) -> str:
    mac = hmac.new(config.SESSION_SECRET.encode(), data.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def customer_token(user_id: str) -> str:
    return _sign(f"customer:{user_id}")


def new_customer() -> tuple[str, str]:
    user_id = f"{SITE_USER_PREFIX}{secrets.token_hex(16)}"   # 128-bit random, not guessable (audit 4.2)
    return user_id, customer_token(user_id)


def check_customer(user_id: str, token: str | None):
    """Customer endpoints: the caller must hold the token we issued for this user_id."""
    if not ID_RE.match(user_id or "") or not token or not hmac.compare_digest(customer_token(user_id), token):
        raise HTTPException(401, "Not authorised for this customer")


# ───────────────────────── dealer admin sessions (audit 2.1, 2.5, 3.3) ─────────────────────────
SESSION_HOURS = 8


ROLES = ("owner", "manager", "agent")      # owner = LeadPilot team (all agencies); manager/agent = one agency


def _resolve_key(key: str) -> tuple[str, str, str | None] | None:
    """A console key -> (business, role, member_id). Kinds of keys:
    - ADMIN_TOKENS in the environment ("*" = LeadPilot owner, otherwise an agency manager)
    - an agency key issued by the owner (manager)
    - a personal team key issued by the agency's manager (manager or agent)"""
    if not key:
        return None
    for token, biz in config.admin_tokens().items():
        if hmac.compare_digest(token, key):
            return biz, ("owner" if biz == "*" else "manager"), None
    from app import team, tenants
    business = tenants.dealer_for_console_key(key)
    if business:
        return business, "manager", None
    m = team.member_for_key(key)
    if m:
        return m["business_id"], m["role"], m["id"]
    return None


def admin_login(key: str) -> dict | None:
    """Exchange a console key for a short-lived, revocable, signed session."""
    found = _resolve_key(key)
    if found is None:
        return None
    business, role, member_id = found
    if business != "*":
        from app import tenants
        d = tenants.get(business)
        if d is None or (d.get("status") or "active") != "active":
            return None                                      # deleted or suspended agencies can't sign in
    if member_id:
        from app import team
        team.touch_login(member_id)
    jti = secrets.token_hex(16)
    exp = int(time.time()) + SESSION_HOURS * 3600
    run(insert(admin_sessions).values(jti=jti, business=business, member_id=member_id, role=role, created_at=now(),
                                      expires_at=datetime.fromtimestamp(exp, timezone.utc), revoked=0))
    payload = base64.urlsafe_b64encode(json.dumps({"j": jti, "b": business, "e": exp}).encode()).decode().rstrip("=")
    return {"session": f"{payload}.{_sign('admin:' + payload)}", "business": business, "role": role, "expires_at": exp}


def verify_principal(session: str) -> dict:
    """Who is calling: {business, role, member_id, jti}. 401 unless the signature is valid, the session hasn't
    expired or been revoked, and (for personal keys) the team member is still active. The role always comes
    from the server's session row, never from the token (audit 2.5, 3.2, 3.3)."""
    try:
        payload, sig = session.split(".", 1)
        if not hmac.compare_digest(_sign("admin:" + payload), sig):
            raise ValueError("bad signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["e"] < time.time():
            raise ValueError("expired")
        row = one(select(admin_sessions).where(admin_sessions.c.jti == data["j"]))
        if not row or row["revoked"] or row["business"] != data["b"]:
            raise ValueError("revoked")
        role = row.get("role") or ("owner" if row["business"] == "*" else "manager")
        if role not in ROLES:
            raise ValueError("bad role")
        if row.get("member_id"):
            from app import team
            if not team.is_active(row["member_id"], row["business"]):
                raise ValueError("member removed")
        return {"business": row["business"], "role": role, "member_id": row.get("member_id"), "jti": row["jti"]}
    except Exception:
        raise HTTPException(401, "Session expired or invalid. Please log in again.")


def verify_admin(session: str) -> tuple[str, str]:
    """Return (business, jti) for a valid session, else 401."""
    p = verify_principal(session)
    return p["business"], p["jti"]


def revoke_admin(jti: str):
    run(update(admin_sessions).where(admin_sessions.c.jti == jti).values(revoked=1))


# ───────────────────────── security event log (audit 10.5) ─────────────────────────
def log_event(event: str, actor: str = "", details: dict | None = None):
    """Failed logins, admin actions, deletions. Never secrets, tokens or full personal data."""
    try:
        run(insert(audit_log).values(user_id=actor[:80] or None, tool=f"event:{event}"[:60],
                                     args=details or {}, created_at=now()))
    except Exception:
        log.exception("could not write security event")
