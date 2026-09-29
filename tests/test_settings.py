"""Settings loading and validation tests."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_load_from_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=test\nAPP_PORT=8123\nLOG_LEVEL=debug\nTELEGRAM_ALLOWED_USER_IDS=1001, 1002\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.app_env == "test"
    assert settings.app_port == 8123
    assert settings.log_level == "DEBUG"
    assert settings.allowed_telegram_user_ids == frozenset({1001, 1002})


def test_settings_require_chat_scope() -> None:
    with pytest.raises(ValidationError, match="must include 'chat'"):
        Settings(_env_file=None, olx_scope="basic_user_info")


def test_settings_validate_telegram_webhook_secret_alphabet() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(
            _env_file=None,
            telegram_webhook_secret="invalid secret with spaces",  # noqa: S106
        )


def test_settings_require_gmail_and_telegram_credentials_when_watch_is_enabled() -> None:
    with pytest.raises(ValidationError, match="GMAIL_IMAP_USERNAME"):
        Settings(_env_file=None, gmail_reply_watch_enabled=True)


def test_settings_normalize_gmail_app_password_spacing() -> None:
    settings = Settings(
        _env_file=None,
        gmail_reply_watch_enabled=True,
        gmail_imap_username="owner@example.com",
        gmail_imap_app_password="abcd efgh ijkl mnop",  # noqa: S106
        telegram_bot_token="123:test",  # noqa: S106
        telegram_target_chat_id="123",
    )

    assert settings.gmail_imap_password == "".join(("abcd", "efgh", "ijkl", "mnop"))


def test_settings_require_timezone_for_gmail_cutoff() -> None:
    with pytest.raises(ValidationError, match="must include a UTC offset"):
        Settings(_env_file=None, gmail_reply_watch_not_before=datetime(2026, 9, 29, 12, 18))
