"""LeadPilot API: app factory, middleware and startup.

Routers: app.api.public (graded /chat and /health, catalogue, leads), app.api.customer (signed customer token),
app.api.admin (dealer console session).
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config
from app.agent import build_model
from app.api import admin, customer, owner, public, webhooks
from app.db import init_db, lock_down_supabase
from app.graph import build_graph, build_persistence
from app.runtime import rt
from app.seed import seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("leadpilot")
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    lock_down_supabase()
    seed()
    rt.saver, rt.store = build_persistence()
    rt.graph = build_graph(rt.saver, rt.store)
    rt.model = build_model()
    if not config.admin_tokens():
        log.warning("ADMIN_TOKENS is not set: the dealer console is disabled")
    log.info("LeadPilot API ready (production=%s)", config.PRODUCTION)
    yield


app = FastAPI(title="LeadPilot API", version="2.0", lifespan=lifespan,
              docs_url="/docs" if config.ENABLE_DOCS else None,
              redoc_url="/redoc" if config.ENABLE_DOCS else None,
              openapi_url="/openapi.json" if config.ENABLE_DOCS else None)

# Two-tier CORS. The chat widget is embedded on dealers' own websites, so its public routes accept any origin
# (no cookies are ever used; a dealer's own website list is enforced per widget key in tenants.check_origin).
# Everything else (the dealer/owner console API) only answers our own website(s) in ALLOWED_ORIGINS.
WIDGET_PATHS = ("/chat", "/widget/", "/customer/", "/leads", "/listings", "/dealers", "/agency/", "/health")
CORS_HEADERS = "Content-Type, Authorization, X-User-Token, X-Widget-Key"


def cors_allowed(path: str, origin: str) -> bool:
    return origin in config.ALLOWED_ORIGINS or path.startswith(WIDGET_PATHS)


@app.middleware("http")
async def cors(request: Request, call_next):
    origin = request.headers.get("origin")
    path = request.url.path
    if request.method == "OPTIONS" and origin and request.headers.get("access-control-request-method"):
        if not cors_allowed(path, origin):
            return Response(status_code=403)
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST, DELETE",
            "Access-Control-Allow-Headers": CORS_HEADERS, "Access-Control-Max-Age": "600", "Vary": "Origin"})
    response = await call_next(request)
    if origin and cors_allowed(path, origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


SITE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'; object-src 'none'")


@app.middleware("http")
async def headers_and_timing(request: Request, call_next):
    """Security headers on every response, no caching of private data, and one log line per request."""
    request_id = uuid.uuid4().hex[:10]
    start = time.perf_counter()
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if config.PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path.startswith(("/admin", "/customer", "/chat", "/leads")):
        response.headers["Cache-Control"] = "no-store"
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers.setdefault("Content-Security-Policy", SITE_CSP)   # dev website: same policy as Vercel
    elif "json" in content_type:
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    response.headers["X-Request-Id"] = request_id
    if request.url.path != "/health":
        log.info("%s %s %s %dms id=%s", request.method, request.url.path.split("/customer/")[0] or "/",
                 response.status_code, int((time.perf_counter() - start) * 1000), request_id)
    return response


app.include_router(public.router)
app.include_router(customer.router)
app.include_router(admin.router)
app.include_router(owner.router)
app.include_router(webhooks.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "Something went wrong. Please try again."})


if config.PRODUCTION:
    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "LeadPilot API", "status": "ok", "website": "https://leadpilot-hassans-team1.vercel.app"}
elif FRONTEND.exists():
    # Development only: serve the website from the API so everything runs on one port.
    # In production the website lives on Vercel (no second copy of the site; vercel.json rewrites /a/<agency>).
    from fastapi.responses import FileResponse

    @app.get("/a/{agency}", include_in_schema=False)
    def agency_page_dev(agency: str):
        return FileResponse(FRONTEND / "a" / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="site")
