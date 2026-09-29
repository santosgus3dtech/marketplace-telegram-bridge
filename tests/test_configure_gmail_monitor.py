"""Safe Gmail monitor environment configuration tests."""

import os
import stat

from app.operations.gmail_monitor_config import disable, prepare, read_env, update_env


def test_prepare_preserves_existing_values_and_file_mode(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=keep-secret\nLOG_LEVEL=INFO\n", encoding="utf-8")
    os.chmod(env_file, 0o600)

    prepare(
        env_file,
        username="owner@example.com",
        not_before="2026-09-29T12:18:35-03:00",
    )

    values = read_env(env_file)
    assert values["TELEGRAM_BOT_TOKEN"] == "keep-secret"  # noqa: S105
    assert values["GMAIL_REPLY_WATCH_ENABLED"] == "false"
    assert values["GMAIL_IMAP_APP_PASSWORD"] == ""
    assert values["GMAIL_REPLY_WATCH_SUBJECT"] == "Atendimento OLX"
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_update_replaces_existing_key_once_and_disable_clears_password(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GMAIL_REPLY_WATCH_ENABLED=false\nGMAIL_IMAP_APP_PASSWORD=temporary\n",
        encoding="utf-8",
    )

    update_env(env_file, {"GMAIL_REPLY_WATCH_ENABLED": "true"})
    disable(env_file)

    contents = env_file.read_text(encoding="utf-8")
    values = read_env(env_file)
    assert contents.count("GMAIL_REPLY_WATCH_ENABLED=") == 1
    assert values["GMAIL_REPLY_WATCH_ENABLED"] == "false"
    assert values["GMAIL_IMAP_APP_PASSWORD"] == ""
