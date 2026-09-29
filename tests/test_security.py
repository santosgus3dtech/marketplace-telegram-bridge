"""Credential encryption and one-time OAuth state tests."""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.db.models import OAuthState, OlxCredential, utc_now
from app.db.repositories import OAuthStateRepository
from app.db.session import Database
from app.services.credentials import CredentialService
from app.services.oauth import OAuthStateService
from app.services.security import TokenCipher, TokenDecryptionError, hash_oauth_state


def test_token_cipher_round_trip_and_authentication() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    sample_value = "olx-access-value-for-test"

    encrypted = cipher.encrypt(sample_value)

    assert encrypted != sample_value
    assert cipher.decrypt(encrypted) == sample_value
    with pytest.raises(TokenDecryptionError):
        cipher.decrypt("not-a-valid-ciphertext")


async def test_credential_service_stores_only_ciphertext(schema_database: Database) -> None:
    cipher = TokenCipher(Fernet.generate_key().decode("ascii"))
    service = CredentialService(cipher)
    sample_value = "private-access-value"

    async with schema_database.session_factory() as session:
        await service.store_access_token(session, access_token=sample_value)
        await session.commit()
        stored = await session.scalar(select(OlxCredential))
        loaded = await service.load_access_token(session)

    assert stored is not None
    assert stored.access_token_encrypted != sample_value
    assert sample_value not in stored.access_token_encrypted
    assert loaded == sample_value


async def test_oauth_state_is_hashed_and_single_use(schema_database: Database) -> None:
    service = OAuthStateService()

    async with schema_database.session_factory() as session:
        raw_state = await service.issue(session)
        await session.commit()
        stored = await session.scalar(select(OAuthState))

        first_use = await service.consume(session, raw_state)
        second_use = await service.consume(session, raw_state)
        await session.commit()

    assert stored is not None
    assert stored.state_hash == hash_oauth_state(raw_state)
    assert raw_state != stored.state_hash
    assert first_use is True
    assert second_use is False
    assert stored.used_at is not None


async def test_expired_oauth_state_cannot_be_consumed(schema_database: Database) -> None:
    repository = OAuthStateRepository()
    service = OAuthStateService(repository=repository)
    expired_state = "expired-state-value"

    async with schema_database.session_factory() as session:
        await repository.create(
            session,
            state_hash=hash_oauth_state(expired_state),
            expires_at=utc_now() - timedelta(seconds=1),
        )
        await session.commit()

        consumed = await service.consume(session, expired_state)

    assert consumed is False
