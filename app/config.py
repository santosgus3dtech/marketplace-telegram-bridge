"""Application configuration loaded from environment variables."""

import re
from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Secret values use ``SecretStr`` so their representation cannot leak into logs.
    Integrations are configured here but are not used during the bootstrap stage.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///./data/bridge.db"
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    olx_client_id: str = ""
    olx_client_secret: SecretStr = SecretStr("")
    olx_redirect_uri: str = ""
    olx_scope: str = "chat"
    olx_auth_url: str = "https://auth.olx.com.br/oauth"
    olx_token_url: str = "https://auth.olx.com.br/oauth/token"  # noqa: S105
    olx_chat_config_url: str = "https://apps.olx.com.br/autoservice/v1/chat"
    olx_chat_send_url: str = "https://apps.olx.com.br/autoservice/v1/chat/send"
    olx_webhook_path_secret: SecretStr = SecretStr("")
    olx_allowed_source_ip: str = "54.162.151.93"
    olx_webhook_body_max_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_target_chat_id: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_webhook_body_max_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)

    gmail_reply_watch_enabled: bool = False
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = Field(default=993, ge=1, le=65_535)
    gmail_imap_username: str = ""
    gmail_imap_app_password: SecretStr = SecretStr("")
    gmail_reply_watch_sender_domain: str = "olx.com.br,olxbr.com"
    gmail_reply_watch_subject: str = "Atendimento OLX"
    gmail_reply_watch_not_before: datetime | None = None
    gmail_reply_watch_poll_seconds: float = Field(default=120, ge=30, le=3_600)
    gmail_reply_watch_lookback_days: int = Field(default=14, ge=1, le=90)
    gmail_reply_watch_max_messages: int = Field(default=250, ge=1, le=1_000)
    gmail_reply_watch_stop_after_match: bool = True

    delivery_worker_enabled: bool = True
    delivery_worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    delivery_worker_batch_size: int = Field(default=10, ge=1, le=100)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    delivery_backoff_seconds: float = Field(default=1.0, ge=0, le=300)
    delivery_lock_timeout_seconds: float = Field(default=60.0, gt=0, le=3_600)

    token_encryption_key: SecretStr = SecretStr("")
    trust_cloudflare: bool = True
    rate_limit_enabled: bool = True
    oauth_rate_limit_requests: int = Field(default=20, ge=1, le=10_000)
    webhook_rate_limit_requests: int = Field(default=120, ge=1, le=100_000)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0, le=3_600)
    rate_limit_max_clients: int = Field(default=2_048, ge=16, le=100_000)
    log_level: str = "INFO"
    message_retention_days: int = Field(default=90, ge=1, le=3_650)
    audit_retention_days: int = Field(default=30, ge=1, le=3_650)
    retention_cleanup_enabled: bool = False
    retention_cleanup_interval_seconds: float = Field(default=86_400, ge=60, le=604_800)

    @field_validator("olx_scope")
    @classmethod
    def require_chat_scope(cls, value: str) -> str:
        """Prevent accidentally starting without the required OLX chat scope."""

        scopes = value.split()
        if "chat" not in scopes:
            raise ValueError("OLX_SCOPE must include 'chat'")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Validate the configured standard-library logging level."""

        normalized = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_telegram_webhook_secret(cls, value: SecretStr) -> SecretStr:
        """Enforce Telegram's documented secret-token alphabet and length."""

        secret = value.get_secret_value()
        if secret and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret) is None:
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters from A-Z, a-z, 0-9, _ or -"
            )
        return value

    @model_validator(mode="after")
    def validate_gmail_reply_watch(self) -> "Settings":
        """Require every secret-bearing dependency before enabling Gmail polling."""

        if not self.gmail_reply_watch_enabled:
            return self
        required = {
            "GMAIL_IMAP_USERNAME": self.gmail_imap_username.strip(),
            "GMAIL_IMAP_APP_PASSWORD": self.gmail_imap_app_password.get_secret_value().strip(),
            "GMAIL_REPLY_WATCH_SENDER_DOMAIN": self.gmail_reply_watch_sender_domain.strip(),
            "GMAIL_REPLY_WATCH_SUBJECT": self.gmail_reply_watch_subject.strip(),
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token.get_secret_value().strip(),
            "TELEGRAM_TARGET_CHAT_ID": self.telegram_target_chat_id.strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "Gmail reply watch requires configured values for: " + ", ".join(missing)
            )
        return self

    @field_validator("gmail_reply_watch_not_before")
    @classmethod
    def require_timezone_for_gmail_cutoff(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous local timestamps used to exclude existing messages."""

        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("GMAIL_REPLY_WATCH_NOT_BEFORE must include a UTC offset")
        return value

    @property
    def gmail_imap_password(self) -> str:
        """Return the app password in the format expected by IMAP clients."""

        return self.gmail_imap_app_password.get_secret_value().replace(" ", "")

    @property
    def allowed_telegram_user_ids(self) -> frozenset[int]:
        """Return the configured Telegram user allowlist as integer IDs."""

        if not self.telegram_allowed_user_ids.strip():
            return frozenset()
        return frozenset(
            int(item.strip()) for item in self.telegram_allowed_user_ids.split(",") if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance for the process."""

    return Settings()
