"""Gmail reply matching, privacy, retry, and deduplication tests."""

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import select

from app.clients.telegram import (
    TelegramDeliveryTransientError,
    TelegramMessageReceipt,
)
from app.config import Settings
from app.db.models import GmailReplyNotification
from app.db.session import Database
from app.services.gmail_reply_watch import (
    GmailReplyCandidate,
    GmailReplyScanner,
    GmailReplyWatchWorker,
    normalize_subject,
    sender_matches_domain,
)

SUBJECT = "Atendimento OLX"


class FakeImap:
    """Small IMAP double that records whether bodies were ever requested."""

    def __init__(
        self,
        headers: dict[int, bytes],
        *,
        internal_dates: dict[int, str] | None = None,
    ) -> None:
        self.headers = headers
        self.internal_dates = internal_dates or {}
        self.fetch_queries: list[str] = []
        self.logged_out = False

    def login(self, _username: str, _password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
        assert mailbox == "INBOX"
        assert readonly is True
        return "OK", [str(len(self.headers)).encode("ascii")]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        assert name == "UIDVALIDITY"
        return name, [b"987654"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "search":
            return "OK", [b" ".join(str(uid).encode("ascii") for uid in self.headers)]
        assert command == "fetch"
        uid = int(args[0])
        query = str(args[1])
        self.fetch_queries.append(query)
        internal_date = self.internal_dates.get(uid, "30-Sep-2026 09:00:00 +0000")
        metadata = f'{uid} (INTERNALDATE "{internal_date}")'.encode("ascii")
        return "OK", [(metadata, self.headers[uid])]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]


class StaticScanner:
    def __init__(self, candidates: list[GmailReplyCandidate]) -> None:
        self.candidates = candidates
        self.calls = 0

    def scan(self) -> list[GmailReplyCandidate]:
        self.calls += 1
        return self.candidates


class RecordingTelegram:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.messages: list[str] = []
        self.calls = 0

    async def send_message(self, text: str) -> TelegramMessageReceipt:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise TelegramDeliveryTransientError("temporary")
        self.messages.append(text)
        return TelegramMessageReceipt(message_id=self.calls, chat_id="private")


def gmail_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "gmail_reply_watch_enabled": True,
        "gmail_imap_username": "owner@example.com",
        "gmail_imap_app_password": "test app password",
        "gmail_reply_watch_subject": SUBJECT,
        "telegram_bot_token": "123:test-token",
        "telegram_target_chat_id": "123",
        "delivery_worker_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def header_bytes(*, sender: str, subject: str, message_id: str) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content("This body must never be fetched by the scanner.")
    raw = message.as_bytes()
    return raw.split(b"\n\n", 1)[0] + b"\n\n"


def test_subject_and_sender_matching_is_strict() -> None:
    assert normalize_subject(f"Re: {SUBJECT}") == normalize_subject(SUBJECT)
    assert normalize_subject(f"ENC: Re: {SUBJECT}") == normalize_subject(SUBJECT)
    assert sender_matches_domain("OLX <agent@olxbr.com>", "olxbr.com")
    assert sender_matches_domain("agent@mail.olxbr.com", "olxbr.com")
    assert not sender_matches_domain("agent@olxbr.com.attacker.test", "olxbr.com")
    assert not sender_matches_domain("agent@example.com", "olxbr.com")
    assert sender_matches_domain("OLX <noreply@olx.com.br>", "olx.com.br,olxbr.com")


def test_scanner_fetches_only_headers_and_filters_candidates() -> None:
    fake = FakeImap(
        {
            10: header_bytes(
                sender="Integrações OLX <suporteintegrador@olxbr.com>",
                subject=f"Re: {SUBJECT}",
                message_id="<match@example>",
            ),
            11: header_bytes(
                sender="Attacker <suporte@olxbr.com.attacker.test>",
                subject=f"Re: {SUBJECT}",
                message_id="<reject@example>",
            ),
        }
    )
    scanner = GmailReplyScanner(
        gmail_settings(),
        imap_factory=lambda _host, _port, *, timeout: fake,
    )

    candidates = scanner.scan()

    assert len(candidates) == 1
    assert candidates[0].message_uid == 10
    assert candidates[0].sender == "suporteintegrador@olxbr.com"
    assert candidates[0].message_id_hash != "<match@example>"
    assert fake.logged_out is True
    assert fake.fetch_queries
    assert all("BODY.PEEK[HEADER.FIELDS" in query for query in fake.fetch_queries)
    assert all("BODY[]" not in query for query in fake.fetch_queries)


def test_scanner_ignores_matching_confirmation_at_or_before_cutoff() -> None:
    fake = FakeImap(
        {
            10: header_bytes(
                sender="OLX <noreply@olx.com.br>",
                subject=SUBJECT,
                message_id="<confirmation@example>",
            ),
            12: header_bytes(
                sender="Atendimento OLX <agent@olxbr.com>",
                subject=f"Re: {SUBJECT}",
                message_id="<reply@example>",
            ),
        },
        internal_dates={
            10: "29-Sep-2026 15:18:35 +0000",
            12: "29-Sep-2026 15:20:00 +0000",
        },
    )
    scanner = GmailReplyScanner(
        gmail_settings(
            gmail_reply_watch_sender_domain="olx.com.br,olxbr.com",
            gmail_reply_watch_not_before=datetime(2026, 9, 29, 15, 18, 35, tzinfo=UTC),
        ),
        imap_factory=lambda _host, _port, *, timeout: fake,
    )

    candidates = scanner.scan()

    assert [candidate.message_uid for candidate in candidates] == [12]


@pytest.mark.asyncio
async def test_worker_retries_telegram_then_deduplicates_after_confirmation(
    schema_database: Database,
) -> None:
    candidate = GmailReplyCandidate(
        uid_validity=987654,
        message_uid=10,
        message_id_hash="a" * 64,
        sender="suporteintegrador@olxbr.com",
        subject=f"Re: {SUBJECT}",
    )
    scanner = StaticScanner([candidate])
    telegram = RecordingTelegram(fail_first=True)
    worker = GmailReplyWatchWorker(
        settings=gmail_settings(),
        database=schema_database,
        telegram=telegram,  # type: ignore[arg-type]
        scanner=scanner,  # type: ignore[arg-type]
    )

    assert await worker.poll_once() is False
    assert await worker.poll_once() is True
    assert await worker.poll_once() is True

    async with schema_database.session_factory() as session:
        records = list((await session.scalars(select(GmailReplyNotification))).all())

    assert telegram.calls == 2
    assert len(telegram.messages) == 1
    assert "Resposta da OLX recebida" in telegram.messages[0]
    assert "body" not in telegram.messages[0].casefold()
    assert len(records) == 1
    assert records[0].attempts == 2
    assert records[0].notified_at is not None
