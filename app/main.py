import hashlib
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import BaseModel, Field, SecretStr

from app.config import Settings
from app.domains import DomainStore
from app.service import MonitoringService
from app.storage import Database

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("phinix")


class Login(BaseModel):
    token: SecretStr = Field(max_length=512)


def create_app(settings=None, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        lock = None
        client = None
        service = None
        remote = None
        try:
            try:
                cfg = settings or Settings()
                cfg.database_path.parent.mkdir(parents=True, exist_ok=True)
                lock = FileLock(str(cfg.database_path) + ".instance.lock", timeout=0)
                lock.acquire()
                db = Database(cfg.database_path)
                db.initialize()
                db.import_legacy(cfg.legacy_state_path, cfg)
                client = httpx.AsyncClient(
                    timeout=cfg.http_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    transport=transport,
                    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                )
                if cfg.supabase_snapshot:
                    from app.supabase_viewer import SupabaseViewer

                    remote = SupabaseViewer(client)
                    db = await remote.refresh()
                service = MonitoringService(db, cfg, client)
                app.state.settings, app.state.service, app.state.db = cfg, service, db
                app.state.remote = remote
                app.state.login_attempts = {}
                await service.start()
            except Exception:
                raise RuntimeError(
                    "Phinix startup failed: check configuration, database access, and single-instance deployment"
                ) from None
            yield
        finally:
            if service:
                await service.stop()
            if client:
                await client.aclose()
            if remote:
                remote.close()
            if lock and lock.is_locked:
                lock.release()

    app = FastAPI(title="Phinix", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Invalid request fields"}, status_code=422)

    @app.middleware("http")
    async def security_headers(request, call_next):
        try:
            allowed = True
            if request.app.state.settings.local_only_access:
                try:
                    allowed = bool(request.client and ip_address(request.client.host).is_loopback)
                except ValueError:
                    allowed = False
                allowed = allowed and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
            response = (
                None
                if allowed
                else JSONResponse({"detail": "Access restricted to this device"}, status_code=403)
            )
            if allowed:
                request.app.state.service.refresh_snapshot()
                request.app.state.db = request.app.state.service.db
                response = await call_next(request)
        except Exception:
            log.error("api_request_failed")
            response = JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    def cookie_signature(value, token):
        return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()

    async def refresh_remote(request):
        remote = request.app.state.remote
        if remote:
            db = await remote.refresh()
            request.app.state.db = request.app.state.service.db = db
            request.app.state.service.snapshot_refresh_error = remote.failed

    async def authenticate(request: Request):
        token = request.app.state.settings.admin_token.get_secret_value()
        if not token:
            raise HTTPException(503, "Admin access is not configured")
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer ") and secrets.compare_digest(
            authorization[7:].encode(), token.encode()
        ):
            await refresh_remote(request)
            return
        cookie = request.cookies.get("phinix_session", "")
        try:
            nonce, expires, signature = cookie.split(".")
            value = f"{nonce}.{expires}"
            if float(expires) > time.time() and secrets.compare_digest(
                signature, cookie_signature(value, token)
            ):
                if request.method not in {"GET", "HEAD"}:
                    # Strict same-site cookie plus exact origin verification for browser mutations.
                    if request.headers.get("origin") != str(request.base_url).rstrip("/"):
                        raise HTTPException(403, "Origin check failed")
                await refresh_remote(request)
                return
        except (ValueError, TypeError):
            pass
        raise HTTPException(401, "Sign in required")

    @app.get("/health")
    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        ok = request.app.state.service.ready()
        return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503)

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origin check failed")
        now = time.time()
        ip = request.client.host if request.client else "unknown"
        attempts = request.app.state.login_attempts
        for key in list(attempts):
            if attempts[key][0] < now - 60:
                del attempts[key]
        if len(attempts) >= 1000 and ip not in attempts:
            raise HTTPException(429, "Try again in a minute")
        start, count = attempts.get(ip, (now, 0))
        if count >= 10:
            raise HTTPException(429, "Try again in a minute")
        attempts[ip] = (start, count + 1)
        token = request.app.state.settings.admin_token.get_secret_value()
        if not token:
            raise HTTPException(503, "Set ADMIN_TOKEN on the server to enable access")
        if not secrets.compare_digest(body.token.get_secret_value().encode(), token.encode()):
            raise HTTPException(401, "Invalid admin token")
        value = f"{secrets.token_hex(20)}.{int(now + 28800)}"
        response.set_cookie(
            "phinix_session",
            f"{value}.{cookie_signature(value, token)}",
            httponly=True,
            secure=request.app.state.settings.app_env == "production",
            samesite="strict",
            max_age=28800,
        )
        attempts.pop(ip, None)
        return {"status": "authenticated"}

    @app.post("/api/logout", dependencies=[Depends(authenticate)])
    async def logout(response: Response):
        response.delete_cookie("phinix_session")
        return {"status": "signed_out"}

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    @app.get("/api/monitoring", dependencies=[Depends(authenticate)])
    async def status(request: Request):
        return request.app.state.service.status()

    @app.get("/api/updates", dependencies=[Depends(authenticate)])
    async def updates(request: Request):
        remote = request.app.state.remote
        status = request.app.state.service.status()
        result = {
            "revision": remote.state.revision if remote else None,
            "refresh_failed": bool(remote and remote.failed),
            "stale": status.get("snapshot_stale", False),
        }
        if status.get("mode") == "supabase_edge":
            # Health can change without a new domain/feed revision.
            result["monitor_status"] = status["integrations"][0]["status"]
        return result

    @app.get("/api/integrations", dependencies=[Depends(authenticate)])
    async def integrations(request: Request):
        status = request.app.state.service.status()
        return {"providers": status["integrations"], "discord": status["discord"]}

    @app.get("/api/notifications/status", dependencies=[Depends(authenticate)])
    async def notifications(request: Request):
        return request.app.state.db.notification_status()

    @app.post("/api/notifications/test", dependencies=[Depends(authenticate)])
    async def test_notification(request: Request):
        if not request.app.state.settings.delivery_enabled:
            raise HTTPException(409, "Discord webhook is not configured")
        if not request.app.state.db.queue_test():
            raise HTTPException(429, "A connection test can be queued once per minute")
        return {"status": "queued"}

    @app.get("/api/events", dependencies=[Depends(authenticate)])
    async def events(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000000),
        provider: str = Query("", max_length=20),
        severity: str = Query("", max_length=50),
        q: str = Query("", max_length=200),
    ):
        return request.app.state.db.events(limit, offset, provider, severity, q)

    @app.get("/api/domains", dependencies=[Depends(authenticate)])
    async def domains(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000000),
        q: str = Query("", max_length=200),
    ):
        return DomainStore(request.app.state.db).listings(limit, offset, q)

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
