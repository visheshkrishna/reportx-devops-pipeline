import asyncio
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from .db.session import build_database_manager
from .routers.audit import router as audit_router
from .routers.auth import router as auth_router
from .routers.clinician import router as clinician_router
from .routers.health import router as health_router
from .routers.interpret import router as interpret_router
from .routers.notifications import router as notifications_router
from .routers.parse import router as parse_router
from .routers.reports import router as reports_router
from .routers.threads import router as threads_router
from .routers.translate import router as translate_router
from .services.notifications import emit_share_expiry_warnings
from .services.reports import cleanup_expired_shares


async def _run_alembic_migrations(project_root: str, timeout: int) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
        cwd=project_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Alembic migration timed out after {timeout} seconds") from exc

    if process.returncode != 0:
        error_msg = stderr.decode().strip() or stdout.decode().strip()
        raise RuntimeError(f"Alembic migration failed: {error_msg}")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # Ensure migrations are applied on startup so required tables (e.g. roles) exist.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        logging.info("Running alembic migrations at startup...")
        timeout = int(os.getenv("ALEMBIC_STARTUP_TIMEOUT_SECONDS", "60"))
        await _run_alembic_migrations(project_root, timeout=timeout)
        logging.info("Alembic migrations applied successfully.")
    except Exception:
        logging.exception("Failed to apply migrations on startup")
        raise

    # Start background scheduler for cleanup jobs
    scheduler = AsyncIOScheduler()

    async def scheduled_cleanup():
        """Wrapper to execute cleanup with a database session."""
        async with app.state.database.session_factory() as session:
            count = await cleanup_expired_shares(session)
            if count > 0:
                logging.info(f"Cleaned up {count} expired shares")

    async def scheduled_share_warning_scan():
        """Emit notifications for shares nearing expiry."""
        async with app.state.database.session_factory() as session:
            count = await emit_share_expiry_warnings(session)
            if count > 0:
                logging.info(f"Emitted {count} share expiry warnings")

    # CLEANUP_INTERVAL_MINUTES controls how often expired shared reports are deleted.
    # Default: 5 minutes.
    # Valid values: positive whole-number minutes; lower values increase scheduler and
    # database activity, while higher values leave expired shares in place longer.
    # For production, choose an interval that balances prompt cleanup with operational load.
    cleanup_interval = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "5"))
    warning_interval = int(os.getenv("SHARE_WARNING_INTERVAL_MINUTES", str(cleanup_interval)))
    scheduler.add_job(
        scheduled_cleanup,
        "interval",
        minutes=cleanup_interval,
        id="cleanup-expired-shares",
        name="Cleanup expired shares",
    )
    scheduler.add_job(
        scheduled_share_warning_scan,
        "interval",
        minutes=warning_interval,
        id="share-expiry-warning-scan",
        name="Emit share expiry warnings",
    )
    scheduler.start()
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        scheduler.shutdown()
        await app.state.database.dispose()


def get_frontend_origin() -> str:
    return os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")


class PHIScrubbedLoggingMiddleware:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        # Configure basic structured-ish logging
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        self.logger = logging.getLogger("reportrx.backend")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method")
        path = scope.get("path")
        start = time.perf_counter()
        status_code_holder = {"status": None}
        request_id_holder = {"rid": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code_holder["status"] = message.get("status", 0)
                # Try to read request id from headers written by RequestIDMiddleware
                headers = message.get("headers") or []
                for k, v in headers:
                    if k.decode().lower() == "x-request-id":
                        request_id_holder["rid"] = v.decode()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            # If RequestIDMiddleware hasn't written yet, try to derive from scope
            if not request_id_holder["rid"]:
                for k, v in scope.get("headers", []):
                    if k.decode().lower() == "x-request-id":
                        request_id_holder["rid"] = v.decode()
            # Intentionally avoid logging headers, bodies, or files
            self.logger.info(
                {
                    "event": "http_request",
                    "method": method,
                    "path": path,
                    "status": status_code_holder["status"],
                    "duration_ms": duration_ms,
                    "request_id": request_id_holder["rid"],
                }
            )


def get_allowed_hosts() -> list[str]:
    raw = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").strip()
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    # Allow Starlette TestClient default host
    if "testserver" not in hosts:
        hosts.append("testserver")
    return hosts


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Conservative default security headers; keep PHI out of logs separately
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "interest-cohort=()")
        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to each response and propagate incoming X-Request-ID.

    - If the client supplies X-Request-ID, echo it back.
    - Otherwise, generate a UUID4 and set X-Request-ID.
    The ID is included in logs by PHIScrubbedLoggingMiddleware.
    """

    async def dispatch(self, request, call_next):
        incoming = request.headers.get("x-request-id")
        rid = incoming or uuid.uuid4().hex
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", rid)
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="ReportX API", version="0.1.0", lifespan=app_lifespan)
    app.state.database = build_database_manager()

    # CORS: only allow the configured frontend origin
    frontend_origin = get_frontend_origin()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )

    # PHI-scrubbed logging middleware
    # Request ID before logging so logs can capture the ID
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(PHIScrubbedLoggingMiddleware)

    # Security hardening
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_allowed_hosts())
    app.add_middleware(SecurityHeadersMiddleware)

    # Routers
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(parse_router, prefix="/api/v1")
    app.include_router(interpret_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(clinician_router, prefix="/api/v1")
    app.include_router(threads_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(translate_router, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root(_: Request) -> Response:
        return Response(status_code=204)

    return app


app = create_app()
