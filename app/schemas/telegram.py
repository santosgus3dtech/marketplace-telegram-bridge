"""Validated subset of Telegram Bot API webhook updates."""

from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    """Telegram user identity required by the authorization boundary."""

    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramChat(BaseModel):
    """Telegram chat identity required by the authorization boundary."""

    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramReplyReference(BaseModel):
    """Minimal Telegram message reference used to route an explicit Reply."""

    model_config = ConfigDict(extra="ignore")

    message_id: int


class TelegramMessage(BaseModel):
    """Inbound message fields used by commands and future Reply routing."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    sender: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    reply_to_message: TelegramReplyReference | None = None


class TelegramUpdatePayload(BaseModel):
    """Top-level Telegram webhook update."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
