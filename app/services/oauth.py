"""OAuth state lifecycle without external HTTP calls."""

import secrets
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.olx import OlxChatClient, OlxOAuthClient, OlxWebhookUnauthorized
from app.db.models import ConnectionStatus, utc_now
from app.db.repositories import OAuthStateRepository
from app.services.credentials import CredentialService
from app.services.security import hash_oauth_state


class InvalidOAuthState(ValueError):
    """Raised when an OAuth state is missing, expired, unknown, or already used."""


class OAuthStateService:
    """Issue and atomically consume short-lived one-time OAuth states."""

    def __init__(
        self,
        repository: OAuthStateRepository | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("OAuth state TTL must be positive")
        self.repository = repository or OAuthStateRepository()
        self.ttl = ttl

    async def issue(self, session: AsyncSession) -> str:
        """Return the raw state while storing only its digest."""

        state = secrets.token_urlsafe(32)
        now = utc_now()
        await self.repository.create(
            session,
            state_hash=hash_oauth_state(state),
            created_at=now,
            expires_at=now + self.ttl,
        )
        return state

    async def consume(self, session: AsyncSession, state: str) -> bool:
        """Accept an unexpired state exactly once."""

        return await self.repository.consume(
            session,
            state_hash=hash_oauth_state(state),
            consumed_at=utc_now(),
        )


class OlxOAuthFlowService:
    """Coordinate state persistence, code exchange, and encrypted token storage."""

    def __init__(
        self,
        *,
        client: OlxOAuthClient,
        chat_client: OlxChatClient,
        webhook_url: str,
        credentials: CredentialService,
        states: OAuthStateService | None = None,
    ) -> None:
        self.client = client
        self.chat_client = chat_client
        self.webhook_url = webhook_url
        self.credentials = credentials
        self.states = states or OAuthStateService()

    async def begin(self, session_factory: async_sessionmaker[AsyncSession]) -> str:
        """Create and commit a new one-time authorization state."""

        async with session_factory.begin() as session:
            return await self.states.issue(session)

    async def complete(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        state: str,
        code: str,
    ) -> None:
        """Consume state, exchange code, and persist only an encrypted token."""

        async with session_factory.begin() as session:
            if not await self.states.consume(session, state):
                raise InvalidOAuthState("OAuth state is invalid")

        grant = await self.client.exchange_code(code)

        async with session_factory.begin() as session:
            await self.credentials.store_access_token(
                session,
                access_token=grant.access_token,
                token_type=grant.token_type,
                connection_status=ConnectionStatus.AUTHORIZED,
            )

        try:
            await self.chat_client.register_webhook(
                access_token=grant.access_token,
                webhook_url=self.webhook_url,
            )
        except OlxWebhookUnauthorized:
            async with session_factory.begin() as session:
                await self.credentials.require_reauthorization(session)
            raise

        async with session_factory.begin() as session:
            await self.credentials.set_connection_status(session, ConnectionStatus.CONNECTED)
