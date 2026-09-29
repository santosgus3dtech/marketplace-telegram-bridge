"""Public-boundary hardening tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet

from app.config import Settings
from app.db.session import Database
from app.main import create_app
from app.services.rate_limit import SlidingWindowRateLimiter


@asynccontextmanager
async def hardened_client(
    database: Database,
    *,
    app_env: str = "test",
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,
        app_env=app_env,
        database_url=str(database.engine.url),
        log_level="WARNING",
        public_base_url="https://bridge.example.test",
        olx_client_id="hardening-client",
        olx_client_secret="hardening-secret",  # noqa: S106
        olx_redirect_uri="https://bridge.example.test/oauth/olx/callback",
        olx_webhook_path_secret="hardening-webhook-path",  # noqa: S106
        token_encryption_key=Fernet.generate_key().decode("ascii"),
        delivery_worker_enabled=False,
        retention_cleanup_enabled=False,
        trust_cloudflare=False,
        oauth_rate_limit_requests=1,
        webhook_rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                AssertionError(f"unexpected upstream request: {request.url.path}")
            )
        )
    )
    application = create_app(
        settings=settings,
        database=database,
        outbound_http_client=upstream,
    )
    try:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                yield client
    finally:
        await upstream.aclose()


async def test_public_rate_limit_does_not_limit_health(schema_database: Database) -> None:
    async with hardened_client(schema_database) as client:
        first_oauth = await client.get("/oauth/olx/start")
        limited_oauth = await client.get("/oauth/olx/start")
        first_health = await client.get("/health", headers={"Origin": "https://example.test"})
        second_health = await client.get("/health")

    assert first_oauth.status_code == 302
    assert limited_oauth.status_code == 429
    assert limited_oauth.headers["retry-after"] == "60"
    assert first_health.status_code == 200
    assert second_health.status_code == 200
    assert "access-control-allow-origin" not in first_health.headers
    assert limited_oauth.headers["x-content-type-options"] == "nosniff"
    assert limited_oauth.headers["referrer-policy"] == "no-referrer"


async def test_production_disables_api_docs_and_adds_csp(schema_database: Database) -> None:
    async with hardened_client(schema_database, app_env="production") as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")
        health = await client.get("/health")

    assert docs.status_code == 404
    assert schema.status_code == 404
    assert health.status_code == 200
    assert "default-src 'none'" in health.headers["content-security-policy"]


async def test_rate_limiter_expires_windows_and_bounds_client_keys() -> None:
    limiter = SlidingWindowRateLimiter(requests=1, window_seconds=10, max_keys=2)

    assert await limiter.allow("client-a", now=0) is True
    assert await limiter.allow("client-a", now=1) is False
    assert await limiter.allow("client-b", now=2) is True
    assert await limiter.allow("client-c", now=3) is True
    assert len(limiter._buckets) == 2
    assert await limiter.allow("client-a", now=11) is True
