"""Strict schemas for official OLX Chat webhook payloads."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=255)]
ListingIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
MessageText = Annotated[str, StringConstraints(min_length=1, max_length=16_000)]
Name = Annotated[str, StringConstraints(max_length=255)]
Email = Annotated[str, StringConstraints(max_length=320)]
Phone = Annotated[str, StringConstraints(max_length=64)]
SenderType = Annotated[str, StringConstraints(min_length=1, max_length=32)]


class OlxWebhookPayload(BaseModel):
    """Exactly the documented OLX-to-CRM chat message contract."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    chat_id: Identifier = Field(alias="chatId")
    message: MessageText
    sender_type: SenderType = Field(alias="senderType")
    email: Email
    name: Name
    phone: Phone
    message_timestamp: datetime = Field(alias="messageTimestamp")
    message_id: Identifier = Field(alias="messageId")
    origin: Literal["buyer", "seller"]
    list_id: ListingIdentifier = Field(alias="listId")

    @field_validator("message_timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        """Store the OLX timestamp as UTC, including the documented timezone-less form."""

        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
