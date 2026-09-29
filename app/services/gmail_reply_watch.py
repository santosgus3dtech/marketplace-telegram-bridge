"""Temporary Gmail IMAP watcher that alerts Telegram about one OLX reply."""

import asyncio
import hashlib
import imaplib
import logging
import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Protocol

from app.clients.telegram import TelegramClient, TelegramDeliveryError
from app.config import Settings
from app.db.repositories import GmailReplyNotificationRepository
from app.db.session import Database

logger = logging.getLogger(__name__)

_REPLY_PREFIX = re.compile(r"^(?:(?:re|res|enc|fw|fwd)\s*:\s*)+", re.IGNORECASE)
_INTERNAL_DATE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class GmailReplyWatchError(RuntimeError):
    """The Gmail mailbox could not be checked safely."""


class ImapClientFactory(Protocol):
    """Factory shape used by the scanner and its isolated tests."""

    def __call__(self, host: str, port: int, *, timeout: float) -> Any: ...


@dataclass(frozen=True, slots=True)
class GmailReplyCandidate:
    """Privacy-minimal headers for a matching reply."""

    uid_validity: int
    message_uid: int
    message_id_hash: str
    sender: str
    subject: str


def decode_email_header(value: str | None) -> str:
    """Decode RFC 2047 text and remove control characters."""

    if not value:
        return ""
    with suppress(LookupError, UnicodeError):
        value = str(make_header(decode_header(value)))
    return " ".join(value.replace("\x00", " ").split())


def normalize_subject(value: str) -> str:
    """Normalize common reply prefixes without broad substring matching."""

    normalized = unicodedata.normalize("NFKC", decode_email_header(value))
    normalized = _REPLY_PREFIX.sub("", normalized).strip()
    return " ".join(normalized.split()).casefold()


def sender_matches_domain(sender: str, allowed_domain: str) -> bool:
    """Accept only a configured domain or one of its subdomains."""

    address = parseaddr(sender)[1].strip().casefold()
    domains = {
        item.strip().lstrip("@").casefold()
        for item in allowed_domain.split(",")
        if item.strip().lstrip("@")
    }
    if not address or "@" not in address or not domains:
        return False
    sender_domain = address.rsplit("@", 1)[1]
    return any(
        sender_domain == domain or sender_domain.endswith(f".{domain}") for domain in domains
    )


def imap_date(value: datetime) -> str:
    """Format a locale-independent date for the IMAP SINCE criterion."""

    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year:04d}"


class GmailReplyScanner:
    """Synchronously scan Gmail headers; callers run this outside the event loop."""

    def __init__(
        self,
        settings: Settings,
        *,
        imap_factory: ImapClientFactory = imaplib.IMAP4_SSL,
    ) -> None:
        self.settings = settings
        self.imap_factory = imap_factory

    def scan(self) -> list[GmailReplyCandidate]:
        """Return matching headers without fetching message bodies or changing read state."""

        client: Any | None = None
        try:
            client = self.imap_factory(
                self.settings.gmail_imap_host,
                self.settings.gmail_imap_port,
                timeout=self.settings.http_timeout_seconds,
            )
            client.login(
                self.settings.gmail_imap_username,
                self.settings.gmail_imap_password,
            )
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise GmailReplyWatchError("Gmail INBOX is unavailable")

            uid_validity = self._uid_validity(client)
            since = imap_date(
                datetime.now(UTC) - timedelta(days=self.settings.gmail_reply_watch_lookback_days)
            )
            status, data = client.uid("search", None, "SINCE", since)
            if status != "OK":
                raise GmailReplyWatchError("Gmail search failed")
            message_uids = self._message_uids(data)
            message_uids = message_uids[-self.settings.gmail_reply_watch_max_messages :]

            matches: list[GmailReplyCandidate] = []
            for message_uid in message_uids:
                candidate = self._fetch_candidate(client, uid_validity, message_uid)
                if candidate is not None:
                    matches.append(candidate)
            return matches
        except GmailReplyWatchError:
            raise
        except (imaplib.IMAP4.error, OSError, TimeoutError) as error:
            raise GmailReplyWatchError("Gmail IMAP check failed") from error
        finally:
            if client is not None:
                with suppress(Exception):
                    client.logout()

    @staticmethod
    def _uid_validity(client: Any) -> int:
        _, values = client.response("UIDVALIDITY")
        if not values:
            raise GmailReplyWatchError("Gmail did not provide UIDVALIDITY")
        raw = values[-1]
        try:
            return int(raw.decode("ascii") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, UnicodeError) as error:
            raise GmailReplyWatchError("Gmail returned invalid UIDVALIDITY") from error

    @staticmethod
    def _message_uids(data: list[Any]) -> list[int]:
        if not data or not isinstance(data[0], bytes):
            return []
        result: list[int] = []
        for raw_uid in data[0].split():
            with suppress(ValueError):
                result.append(int(raw_uid))
        return result

    def _fetch_candidate(
        self,
        client: Any,
        uid_validity: int,
        message_uid: int,
    ) -> GmailReplyCandidate | None:
        status, data = client.uid(
            "fetch",
            str(message_uid),
            "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])",
        )
        if status != "OK":
            raise GmailReplyWatchError("Gmail header fetch failed")
        header_bytes = b"".join(
            item[1]
            for item in data
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes)
        )
        if not header_bytes:
            return None
        message = BytesParser(policy=policy.default).parsebytes(header_bytes, headersonly=True)
        sender = decode_email_header(message.get("From"))
        subject = decode_email_header(message.get("Subject"))
        if not sender_matches_domain(sender, self.settings.gmail_reply_watch_sender_domain):
            return None
        if normalize_subject(subject) != normalize_subject(self.settings.gmail_reply_watch_subject):
            return None
        cutoff = self.settings.gmail_reply_watch_not_before
        if cutoff is not None:
            received_at = self._internal_date(data)
            if received_at is None:
                raise GmailReplyWatchError("Gmail did not provide INTERNALDATE")
            if received_at <= cutoff.astimezone(UTC):
                return None

        message_id = decode_email_header(message.get("Message-ID"))
        fingerprint_source = message_id or f"{uid_validity}:{message_uid}"
        return GmailReplyCandidate(
            uid_validity=uid_validity,
            message_uid=message_uid,
            message_id_hash=hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
            sender=parseaddr(sender)[1][:320],
            subject=subject[:300],
        )

    @staticmethod
    def _internal_date(data: list[Any]) -> datetime | None:
        for item in data:
            if not isinstance(item, tuple) or not item or not isinstance(item[0], bytes):
                continue
            match = _INTERNAL_DATE.search(item[0])
            if match is None:
                continue
            with suppress(TypeError, ValueError, UnicodeError, OverflowError):
                parsed = parsedate_to_datetime(match.group(1).decode("ascii"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return parsed.astimezone(UTC)
        return None


def build_reply_notification(candidate: GmailReplyCandidate) -> str:
    """Create a short Telegram alert without including the email body."""

    return (
        "📧 Resposta da OLX recebida\n"
        f"Remetente: {candidate.sender}\n"
        f"Assunto: {candidate.subject}\n\n"
        "Abra o Gmail para ler e responder. O monitor temporário foi concluído."
    )


class GmailReplyWatchWorker:
    """Poll Gmail and retry Telegram delivery until one notification succeeds."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        telegram: TelegramClient,
        scanner: GmailReplyScanner | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.telegram = telegram
        self.scanner = scanner or GmailReplyScanner(settings)
        self.notifications = GmailReplyNotificationRepository()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="gmail-reply-watch-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.settings.gmail_reply_watch_stop_after_match:
                    async with self.database.session_factory() as session:
                        if await self.notifications.has_notified(session):
                            logger.info(
                                "gmail_reply_watch_completed",
                                extra={"event": "gmail_reply_watch", "status": "completed"},
                            )
                            return
                if await self.poll_once() and self.settings.gmail_reply_watch_stop_after_match:
                    return
            except GmailReplyWatchError:
                logger.warning(
                    "gmail_reply_watch_check_failed",
                    extra={"event": "gmail_reply_watch", "status": "check_failed"},
                )
            except Exception:
                logger.exception(
                    "gmail_reply_watch_unexpected_error",
                    extra={"event": "gmail_reply_watch", "status": "unexpected_error"},
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.gmail_reply_watch_poll_seconds,
                )
            except TimeoutError:
                pass

    async def poll_once(self) -> bool:
        """Scan once and return true after at least one confirmed Telegram alert."""

        candidates = await asyncio.to_thread(self.scanner.scan)
        for candidate in candidates:
            async with self.database.session_factory.begin() as session:
                record = await self.notifications.get_or_create(
                    session,
                    uid_validity=candidate.uid_validity,
                    message_uid=candidate.message_uid,
                    message_id_hash=candidate.message_id_hash,
                )
            if record.notified_at is not None:
                return True
            try:
                await self.telegram.send_message(build_reply_notification(candidate))
            except TelegramDeliveryError:
                async with self.database.session_factory.begin() as session:
                    await self.notifications.mark_attempt(
                        session,
                        record_id=record.id,
                        notified=False,
                    )
                logger.warning(
                    "gmail_reply_watch_telegram_failed",
                    extra={"event": "gmail_reply_watch", "status": "telegram_failed"},
                )
                continue

            async with self.database.session_factory.begin() as session:
                await self.notifications.mark_attempt(
                    session,
                    record_id=record.id,
                    notified=True,
                )
            logger.info(
                "gmail_reply_watch_notified",
                extra={"event": "gmail_reply_watch", "status": "notified"},
            )
            return True
        return False
