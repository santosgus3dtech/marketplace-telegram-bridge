"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.health import router as health_router
from app.api.olx_oauth import router as olx_oauth_router
from app.api.olx_webhook import router as olx_webhook_router
from app.api.telegram_webhook import router as telegram_webhook_router
from app.clients.telegram import TelegramClient
from app.config import Settings, get_settings
from app.db.session import Database
from app.logging_config import configure_logging
from app.services.delivery import DeliveryWorker
from app.services.gmail_reply_watch import GmailReplyWatchWorker
from app.services.rate_limit import SlidingWindowRateLimiter
from app.services.retention import RetentionService, RetentionWorker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    outbound_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build an application with injectable settings and database for tests."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(
        resolved_settings.database_url,
        resolved_settings.sqlite_busy_timeout_ms,
    )
    oauth_rate_limiter = SlidingWindowRateLimiter(
        requests=resolved_settings.oauth_rate_limit_requests,
        window_seconds=resolved_settings.rate_limit_window_seconds,
        max_keys=resolved_settings.rate_limit_max_clients,
    )
    webhook_rate_limiter = SlidingWindowRateLimiter(
        requests=resolved_settings.webhook_rate_limit_requests,
        window_seconds=resolved_settings.rate_limit_window_seconds,
        max_keys=resolved_settings.rate_limit_max_clients,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if application.state.outbound_http_client is None:
            application.state.outbound_http_client = httpx.AsyncClient()
            application.state.owns_outbound_http_client = True
        database_ready = False
        try:
            await application.state.database.initialize()
            application.state.database.secure_sqlite_permissions(
                include_directory=resolved_settings.app_env == "production"
            )
            database_ready = True
            logger.info(
                "application_started",
                extra={"event": "application_start", "status": "ready"},
            )
        except Exception:
            logger.exception(
                "database_initialization_failed",
                extra={"event": "application_start", "status": "degraded"},
            )
        if database_ready and resolved_settings.delivery_worker_enabled:
            application.state.delivery_worker = DeliveryWorker(
                settings=resolved_settings,
                database=application.state.database,
                http_client=application.state.outbound_http_client,
            )
            await application.state.delivery_worker.start()
        if database_ready and resolved_settings.retention_cleanup_enabled:
            application.state.retention_worker = RetentionWorker(
                service=RetentionService(
                    database=application.state.database,
                    message_retention_days=resolved_settings.message_retention_days,
                    audit_retention_days=resolved_settings.audit_retention_days,
                ),
                interval_seconds=resolved_settings.retention_cleanup_interval_seconds,
            )
            await application.state.retention_worker.start()
        if database_ready and resolved_settings.gmail_reply_watch_enabled:
            application.state.gmail_reply_watch_worker = GmailReplyWatchWorker(
                settings=resolved_settings,
                database=application.state.database,
                telegram=TelegramClient(
                    resolved_settings,
                    application.state.outbound_http_client,
                ),
            )
            await application.state.gmail_reply_watch_worker.start()
        try:
            yield
        finally:
            if application.state.delivery_worker is not None:
                await application.state.delivery_worker.stop()
            if application.state.retention_worker is not None:
                await application.state.retention_worker.stop()
            if application.state.gmail_reply_watch_worker is not None:
                await application.state.gmail_reply_watch_worker.stop()
            if application.state.owns_outbound_http_client:
                await application.state.outbound_http_client.aclose()
            await application.state.database.dispose()
            logger.info(
                "application_stopped",
                extra={"event": "application_stop", "status": "stopped"},
            )

    application = FastAPI(
        title="OLX Telegram Bridge",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if resolved_settings.app_env == "production" else "/docs",
        redoc_url=None if resolved_settings.app_env == "production" else "/redoc",
        openapi_url=None if resolved_settings.app_env == "production" else "/openapi.json",
    )
    application.state.settings = resolved_settings
    application.state.database = resolved_database
    application.state.outbound_http_client = outbound_http_client
    application.state.owns_outbound_http_client = False
    application.state.delivery_worker = None
    application.state.retention_worker = None
    application.state.gmail_reply_watch_worker = None

    @application.middleware("http")
    async def public_security_boundary(request: Request, call_next):
        """Rate-limit public integrations and attach browser hardening headers."""

        path = request.url.path
        limiter: SlidingWindowRateLimiter | None = None
        group = ""
        if path.startswith("/oauth/olx/"):
            limiter = oauth_rate_limiter
            group = "oauth"
        elif path.startswith("/webhooks/"):
            limiter = webhook_rate_limiter
            group = "webhook"

        if limiter is not None and resolved_settings.rate_limit_enabled:
            source = ""
            if resolved_settings.trust_cloudflare:
                source = request.headers.get("cf-connecting-ip", "").strip()
            if not source and request.client is not None:
                source = request.client.host
            source = source or "unknown"
            if not await limiter.allow(f"{group}:{source}"):
                response = JSONResponse(
                    {"detail": "Too many requests"},
                    status_code=429,
                    headers={
                        "Retry-After": str(max(1, int(resolved_settings.rate_limit_window_seconds)))
                    },
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        if resolved_settings.app_env == "production":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
        return response

    application.include_router(health_router)
    application.include_router(olx_oauth_router)
    application.include_router(olx_webhook_router)
    application.include_router(telegram_webhook_router)
    return application


app = create_app()
