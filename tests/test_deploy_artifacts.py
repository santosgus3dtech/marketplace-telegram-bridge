"""Production deployment artifacts and safe OLX registration tests."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import Settings
from app.db.models import ConnectionStatus, OlxCredential
from app.db.session import Database
from app.services.credentials import CredentialService
from app.services.security import TokenCipher

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLX_TOKEN = "olx-token-for-deploy-test"  # noqa: S105
WEBHOOK_SECRET = "olx-webhook-path-for-deploy-test"  # noqa: S105


def load_olx_registration_script() -> ModuleType:
    """Load the hyphenated operational script as an importable test module."""

    script_path = PROJECT_ROOT / "scripts" / "olx-register-webhook.py"
    spec = importlib.util.spec_from_file_location("olx_register_webhook_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load OLX registration script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def seed_credential(database: Database, encryption_key: str) -> None:
    """Persist one encrypted access token without placing plaintext in fixtures."""

    service = CredentialService(TokenCipher(encryption_key))
    async with database.session_factory.begin() as session:
        await service.store_access_token(
            session,
            access_token=OLX_TOKEN,
            connection_status=ConnectionStatus.AUTHORIZED,
        )


def registration_settings(database: Database, encryption_key: str) -> Settings:
    """Return complete, isolated settings for the OLX registration script."""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url=str(database.engine.url),
        public_base_url="https://bridge.example.test",
        olx_webhook_path_secret=WEBHOOK_SECRET,
        token_encryption_key=encryption_key,
    )


async def test_olx_registration_uses_encrypted_token_without_printing_it(
    schema_database: Database,
    capsys,
) -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    await seed_credential(schema_database, encryption_key)
    settings = registration_settings(schema_database, encryption_key)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"ok": True})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    module = load_olx_registration_script()
    try:
        await module.configure_webhook(
            settings=settings,
            database=schema_database,
            http_client=upstream,
        )
    finally:
        await upstream.aclose()

    async with schema_database.session_factory() as session:
        credential = await session.scalar(select(OlxCredential))

    output = capsys.readouterr().out
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {OLX_TOKEN}"
    assert json.loads(requests[0].content) == {
        "webhook": f"https://bridge.example.test/webhooks/olx/{WEBHOOK_SECRET}"
    }
    assert OLX_TOKEN not in output
    assert WEBHOOK_SECRET not in output
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.CONNECTED


async def test_olx_registration_401_marks_reauthorization_without_secret_output(
    schema_database: Database,
    capsys,
) -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    await seed_credential(schema_database, encryption_key)
    settings = registration_settings(schema_database, encryption_key)
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401))
    )
    module = load_olx_registration_script()
    try:
        with pytest.raises(module.OlxWebhookUnauthorized):
            await module.configure_webhook(
                settings=settings,
                database=schema_database,
                http_client=upstream,
            )
    finally:
        await upstream.aclose()

    async with schema_database.session_factory() as session:
        credential = await session.scalar(select(OlxCredential))

    output = capsys.readouterr().out
    assert OLX_TOKEN not in output
    assert WEBHOOK_SECRET not in output
    assert credential is not None
    assert credential.connection_status == ConnectionStatus.REAUTHORIZATION_REQUIRED


def test_deployment_artifacts_keep_the_app_private_and_recoverable() -> None:
    compose = (PROJECT_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    compose_example = (PROJECT_ROOT / "deploy" / "docker-compose.example.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    restore = (PROJECT_ROOT / "scripts" / "restore-db.sh").read_text(encoding="utf-8")
    check = (PROJECT_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")

    assert '"127.0.0.1:${APP_BIND_PORT:-8010}:8000"' in compose
    assert 'user: "${APP_UID:-1000}:${APP_GID:-1000}"' in compose
    assert compose_example == compose
    assert "../data:/app/data" in compose
    assert "restart: unless-stopped" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "stop_grace_period: 30s" in compose
    assert "USER app" in dockerfile
    assert 'ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]' in dockerfile
    assert "0o700" in check
    assert "0o600" in check
    assert "Digite RESTAURAR" in restore
    assert "pre-restore-" in restore
    assert "alembic upgrade head" in restore
