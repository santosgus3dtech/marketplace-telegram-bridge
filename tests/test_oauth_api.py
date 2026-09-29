"""OLX OAuth route tests with a fully mocked upstream server."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import ConnectionStatus, OAuthState, OlxCredential, utc_now
from app.db.repositories import OAuthStateRepository
from app.db.session import Database
from app.main import create_app
from app.services.security import TokenCipher, hash_oauth_state

ResponseHandler = Callable[[httpx.Request], httpx.Response]


def oauth_settings(database: Database) -> Settings:
    """Return complete fake configuration suitable for isolated tests."""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url=str(database.engine.url),
        log_level="WARNING",
        public_base_url="https://bridge.example",
        olx_client_id="client-id-for-test",
        olx_client_secret="client-secret-for-test",  # noqa: S106
        olx_redirect_uri="https://bridge.example/oauth/olx/callback",
        olx_webhook_path_secret="webhook-path-for-test",  # noqa: S106
        token_encryption_key=Fernet.generate_key().decode("ascii"),
    )


@asynccontextmanager
async def oauth_test_client(
    database: Database,
    handler: ResponseHandler,
) -> AsyncIterator[tuple[httpx.AsyncClient, Settings]]:
    """Run the app with an injected OLX MockTransport."""

    settings = oauth_settings(database)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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
            ) as test_client:
                yield test_client, settings
    finally:
        await upstream.aclose()


async def start_oauth(client: httpx.AsyncClient) -> tuple[httpx.Response, str]:
    """Start authorization and return the generated raw state."""

    response = await client.get("/oauth/olx/start")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return response, query["state"][0]


async def test_start_persists_only_state_hash_and_redirects(
    schema_database: Database,
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("start must not call the token endpoint")

    async with oauth_test_client(schema_database, unexpected_request) as (client, settings):
        response, raw_state = await start_oauth(client)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        async with schema_database.session_factory() as session:
            stored = await session.scalar(select(OAuthState))

    assert response.status_code == 302
    assert urlsplit(response.headers["location"]).scheme == "https"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [settings.olx_client_id]
    assert query["redirect_uri"] == [settings.olx_redirect_uri]
    assert query["scope"] == ["chat"]
    assert response.headers["cache-control"] == "no-store"
    assert stored is not None
    assert stored.state_hash == hash_oauth_state(raw_state)
    assert raw_state not in stored.state_hash


@pytest.mark.parametrize("registration_status", [200, 201])
async def test_callback_exchanges_form_registers_webhook_and_stores_encrypted_token(
    schema_database: Database,
    registration_status: int,
) -> None:
    observed_requests: list[httpx.Request] = []
    token_value = "returned-access-value"  # noqa: S105

    def token_response(request: httpx.Request) -> httpx.Response:
        observed_requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": token_value, "token_type": "Bearer"},
            )
        return httpx.Response(registration_status)

    async with oauth_test_client(schema_database, token_response) as (client, settings):
        _response, raw_state = await start_oauth(client)
        callback = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": raw_state},
        )
        async with schema_database.session_factory() as session:
            credential = await session.scalar(select(OlxCredential))

    assert callback.status_code == 200
    assert "conectada com sucesso" in callback.text
    assert token_value not in callback.text
    assert len(observed_requests) == 2
    token_request, registration_request = observed_requests
    assert token_request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    submitted = parse_qs(token_request.content.decode("utf-8"))
    assert submitted == {
        "code": ["authorization-code"],
        "client_id": [settings.olx_client_id],
        "client_secret": [settings.olx_client_secret.get_secret_value()],
        "redirect_uri": [settings.olx_redirect_uri],
        "grant_type": ["authorization_code"],
    }
    assert registration_request.headers["authorization"] == f"Bearer {token_value}"
    assert json.loads(registration_request.content) == {
        "webhook": "https://bridge.example/webhooks/olx/webhook-path-for-test"
    }
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.CONNECTED
    assert credential.access_token_encrypted != token_value
    assert (
        TokenCipher(settings.token_encryption_key).decrypt(credential.access_token_encrypted)
        == token_value
    )


async def test_invalid_state_is_rejected_before_token_exchange(
    schema_database: Database,
) -> None:
    calls = 0

    def token_response(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "unused", "token_type": "Bearer"})

    async with oauth_test_client(schema_database, token_response) as (client, _settings):
        response = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": "unknown-state"},
        )

    assert response.status_code == 400
    assert calls == 0


async def test_expired_state_is_rejected_before_token_exchange(
    schema_database: Database,
) -> None:
    calls = 0
    raw_state = "expired-state"

    def token_response(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "unused", "token_type": "Bearer"})

    async with oauth_test_client(schema_database, token_response) as (client, _settings):
        async with schema_database.session_factory.begin() as session:
            await OAuthStateRepository().create(
                session,
                state_hash=hash_oauth_state(raw_state),
                expires_at=utc_now() - timedelta(seconds=1),
            )
        response = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": raw_state},
        )

    assert response.status_code == 400
    assert calls == 0


async def test_oauth_state_replay_is_rejected(schema_database: Database) -> None:
    token_calls = 0
    registration_calls = 0

    def token_response(request: httpx.Request) -> httpx.Response:
        nonlocal registration_calls, token_calls
        if request.url.path.endswith("/oauth/token"):
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"value-{token_calls}", "token_type": "Bearer"},
            )
        registration_calls += 1
        return httpx.Response(201)

    async with oauth_test_client(schema_database, token_response) as (client, _settings):
        _response, raw_state = await start_oauth(client)
        first = await client.get(
            "/oauth/olx/callback",
            params={"code": "first-code", "state": raw_state},
        )
        replay = await client.get(
            "/oauth/olx/callback",
            params={"code": "second-code", "state": raw_state},
        )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert token_calls == 1
    assert registration_calls == 1


async def test_token_400_does_not_store_or_log_secrets(
    schema_database: Database,
    capsys,
) -> None:
    rejected_code = "rejected-authorization-code"

    def token_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with oauth_test_client(schema_database, token_response) as (client, settings):
        _response, raw_state = await start_oauth(client)
        callback = await client.get(
            "/oauth/olx/callback",
            params={"code": rejected_code, "state": raw_state},
        )
        async with schema_database.session_factory() as session:
            credential_count = await session.scalar(select(func.count()).select_from(OlxCredential))

    captured = capsys.readouterr()
    visible_output = captured.out + captured.err + callback.text
    assert callback.status_code == 400
    assert credential_count == 0
    assert rejected_code not in visible_output
    assert raw_state not in visible_output
    assert settings.olx_client_secret.get_secret_value() not in visible_output


async def test_token_timeout_returns_bad_gateway(schema_database: Database) -> None:
    def token_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async with oauth_test_client(schema_database, token_timeout) as (client, _settings):
        _response, raw_state = await start_oauth(client)
        callback = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": raw_state},
        )

    assert callback.status_code == 502


async def test_webhook_registration_401_requires_reauthorization(
    schema_database: Database,
) -> None:
    def olx_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "access-value", "token_type": "Bearer"},
            )
        return httpx.Response(401)

    async with oauth_test_client(schema_database, olx_response) as (client, _settings):
        _response, raw_state = await start_oauth(client)
        callback = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": raw_state},
        )
        async with schema_database.session_factory() as session:
            credential = await session.scalar(select(OlxCredential))

    assert callback.status_code == 401
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.REAUTHORIZATION_REQUIRED
    assert credential.last_401_at is not None


async def test_webhook_registration_500_keeps_authorized_token(
    schema_database: Database,
) -> None:
    def olx_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "access-value", "token_type": "Bearer"},
            )
        return httpx.Response(500)

    async with oauth_test_client(schema_database, olx_response) as (client, _settings):
        _response, raw_state = await start_oauth(client)
        callback = await client.get(
            "/oauth/olx/callback",
            params={"code": "authorization-code", "state": raw_state},
        )
        async with schema_database.session_factory() as session:
            credential = await session.scalar(select(OlxCredential))

    assert callback.status_code == 502
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.AUTHORIZED


async def test_callback_handles_denial_and_missing_parameters(
    schema_database: Database,
) -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid callbacks must not call the token endpoint")

    async with oauth_test_client(schema_database, unexpected_request) as (client, _settings):
        denied = await client.get("/oauth/olx/callback", params={"error": "access_denied"})
        incomplete = await client.get("/oauth/olx/callback")

    assert denied.status_code == 400
    assert incomplete.status_code == 400
