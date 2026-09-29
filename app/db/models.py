"""SQLAlchemy models for the bridge's durable state."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db.base import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist aware UTC datetimes and restore timezone data lost by SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ConnectionStatus(StrEnum):
    """OLX authorization and webhook connection states."""

    DISCONNECTED = "disconnected"
    AUTHORIZED = "authorized"
    CONNECTED = "connected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class TelegramUpdateStatus(StrEnum):
    """Processing states for Telegram updates."""

    RECEIVED = "received"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


class OutboundMessageStatus(StrEnum):
    """Delivery states used by the future persistent outbox."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DEAD_LETTER = "dead_letter"


class DeliveryJobKind(StrEnum):
    """External delivery directions handled by the persistent worker."""

    OLX_TO_TELEGRAM = "olx_to_telegram"
    TELEGRAM_TO_OLX = "telegram_to_olx"


class DeliveryJobStatus(StrEnum):
    """Durable worker states for one external delivery."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


def enum_type(enum_class: type[StrEnum], name: str) -> SqlEnum:
    """Build a portable string enum that stores values rather than member names."""

    return SqlEnum(
        enum_class,
        values_callable=lambda members: [member.value for member in members],
        name=name,
        native_enum=False,
        create_constraint=False,
        validate_strings=True,
    )


class OAuthState(Base):
    """One-time OAuth state represented only by its SHA-256 digest."""

    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OlxCredential(Base):
    """Encrypted OLX token and its operational connection state."""

    __tablename__ = "olx_credentials"
    __table_args__ = (
        CheckConstraint(
            "connection_status IN ('disconnected', 'authorized', 'connected', "
            "'reauthorization_required')",
            name="connection_status_values",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer")
    connection_status: Mapped[ConnectionStatus] = mapped_column(
        enum_type(ConnectionStatus, "connection_status"),
        nullable=False,
        default=ConnectionStatus.AUTHORIZED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    last_401_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OlxListing(Base):
    """Local friendly metadata for an OLX listing ID."""

    __tablename__ = "olx_listings"
    __table_args__ = (CheckConstraint("price IS NULL OR price >= 0", name="price_non_negative"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class OlxChat(Base):
    """Conversation metadata received from OLX."""

    __tablename__ = "olx_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    list_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    buyer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    buyer_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class OlxMessage(Base):
    """An inbound OLX message and its Telegram mapping."""

    __tablename__ = "olx_messages"
    __table_args__ = (
        UniqueConstraint("message_id"),
        UniqueConstraint("telegram_message_id"),
        Index("ix_olx_messages_chat_received", "chat_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("olx_chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    list_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    sender_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    olx_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TelegramUpdate(Base):
    """A Telegram webhook update used for inbound idempotency."""

    __tablename__ = "telegram_updates"
    __table_args__ = (
        UniqueConstraint("update_id"),
        CheckConstraint(
            "status IN ('received', 'processed', 'rejected', 'failed')",
            name="status_values",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[TelegramUpdateStatus] = mapped_column(
        enum_type(TelegramUpdateStatus, "telegram_update_status"),
        nullable=False,
        default=TelegramUpdateStatus.RECEIVED,
        index=True,
    )


class OutboundMessage(Base):
    """A durable Telegram-to-OLX delivery attempt."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("telegram_update_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'retry', 'failed', 'unknown', 'dead_letter')",
            name="status_values",
        ),
        Index("ix_outbound_messages_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_update_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_updates.update_id", ondelete="RESTRICT"),
        nullable=False,
    )
    olx_chat_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("olx_chats.chat_id", ondelete="RESTRICT"), nullable=False
    )
    olx_reference_message_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("olx_messages.message_id", ondelete="RESTRICT"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboundMessageStatus] = mapped_column(
        enum_type(OutboundMessageStatus, "outbound_message_status"),
        nullable=False,
        default=OutboundMessageStatus.PENDING,
    )
    olx_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class DeliveryJob(Base):
    """Persistent outbox item claimed atomically by one asyncio worker."""

    __tablename__ = "delivery_jobs"
    __table_args__ = (
        UniqueConstraint("kind", "olx_message_id", name="uq_delivery_jobs_kind_olx_message"),
        UniqueConstraint(
            "kind", "outbound_message_id", name="uq_delivery_jobs_kind_outbound_message"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'succeeded', 'failed', 'dead_letter')",
            name="status_values",
        ),
        CheckConstraint(
            "(kind = 'olx_to_telegram' AND olx_message_id IS NOT NULL "
            "AND outbound_message_id IS NULL) OR "
            "(kind = 'telegram_to_olx' AND outbound_message_id IS NOT NULL "
            "AND olx_message_id IS NULL)",
            name="reference_matches_kind",
        ),
        Index("ix_delivery_jobs_due", "status", "next_attempt_at"),
        Index("ix_delivery_jobs_lock", "status", "locked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[DeliveryJobKind] = mapped_column(
        enum_type(DeliveryJobKind, "delivery_job_kind"),
        nullable=False,
    )
    status: Mapped[DeliveryJobStatus] = mapped_column(
        enum_type(DeliveryJobStatus, "delivery_job_status"),
        nullable=False,
        default=DeliveryJobStatus.PENDING,
    )
    olx_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("olx_messages.id", ondelete="CASCADE"),
        nullable=True,
    )
    outbound_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("outbound_messages.id", ondelete="CASCADE"),
        nullable=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now
    )
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class GmailReplyNotification(Base):
    """Minimal durable state used to suppress repeated Gmail reply notifications."""

    __tablename__ = "gmail_reply_notifications"
    __table_args__ = (
        UniqueConstraint(
            "uid_validity",
            "message_uid",
            name="uq_gmail_reply_notifications_mailbox_message",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid_validity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )


class AuditEvent(Base):
    """Secret-free append-only operational audit event."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_type_created", "event_type", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, server_default=func.current_timestamp()
    )
