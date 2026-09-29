"""Security primitives for OAuth state and encrypted credentials."""

import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class TokenDecryptionError(ValueError):
    """Raised when an encrypted token cannot be authenticated or decrypted."""


def hash_oauth_state(state: str) -> str:
    """Return a deterministic SHA-256 digest without persisting the raw state."""

    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class TokenCipher:
    """Authenticated encryption for OLX access tokens using Fernet."""

    def __init__(self, encryption_key: str | SecretStr) -> None:
        key = (
            encryption_key.get_secret_value()
            if isinstance(encryption_key, SecretStr)
            else encryption_key
        )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (TypeError, ValueError) as error:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from error

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a non-empty token for database storage."""

        if not plaintext:
            raise ValueError("access token cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt and authenticate a stored token without leaking its value."""

        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as error:
            raise TokenDecryptionError("stored access token could not be decrypted") from error
