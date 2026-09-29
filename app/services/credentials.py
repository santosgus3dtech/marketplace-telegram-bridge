"""Encrypted OLX credential persistence."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectionStatus, OlxCredential
from app.db.repositories import OlxCredentialRepository
from app.services.security import TokenCipher


class CredentialService:
    """Ensure plaintext OLX tokens never cross the repository boundary."""

    def __init__(
        self,
        cipher: TokenCipher,
        repository: OlxCredentialRepository | None = None,
    ) -> None:
        self.cipher = cipher
        self.repository = repository or OlxCredentialRepository()

    async def store_access_token(
        self,
        session: AsyncSession,
        *,
        access_token: str,
        token_type: str = "Bearer",  # noqa: S107
        connection_status: ConnectionStatus = ConnectionStatus.AUTHORIZED,
    ) -> OlxCredential:
        """Encrypt an access token before sending it to persistence."""

        encrypted = self.cipher.encrypt(access_token)
        return await self.repository.save(
            session,
            access_token_encrypted=encrypted,
            token_type=token_type,
            connection_status=connection_status,
        )

    async def load_access_token(self, session: AsyncSession) -> str | None:
        """Decrypt the current token only for an authorized caller."""

        record = await self.repository.get_current(session)
        if record is None:
            return None
        return self.cipher.decrypt(record.access_token_encrypted)

    async def set_connection_status(
        self,
        session: AsyncSession,
        connection_status: ConnectionStatus,
    ) -> OlxCredential | None:
        """Change the current OLX integration status without rotating its token."""

        return await self.repository.set_connection_status(
            session,
            connection_status=connection_status,
        )

    async def require_reauthorization(self, session: AsyncSession) -> OlxCredential | None:
        """Mark the current credential unusable after an OLX 401 response."""

        return await self.repository.require_reauthorization(session)
