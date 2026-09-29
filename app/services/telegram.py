"""Secure Telegram command processing and durable OLX Reply enqueueing."""

from dataclasses import dataclass
from typing import Literal

from app.clients.telegram import TelegramClient, TelegramDeliveryError
from app.config import Settings
from app.db.models import ConnectionStatus, TelegramUpdateStatus
from app.db.repositories import (
    DeliveryJobRepository,
    OlxCredentialRepository,
    OlxMessageRepository,
    OutboundMessageRepository,
    TelegramUpdateRepository,
)
from app.db.session import Database
from app.schemas.telegram import TelegramUpdatePayload

HELP_TEXT = (
    "Comandos disponíveis:\n"
    "/help — mostrar esta ajuda\n"
    "/status — verificar app, banco, Telegram e OLX\n"
    "/olx_status — mostrar o estado da integração OLX"
)
REPLY_GUIDANCE = "Para responder à OLX, use Reply em uma notificação de mensagem do comprador."
INVALID_TEXT_GUIDANCE = "A resposta para a OLX precisa conter entre 1 e 4096 caracteres."


@dataclass(frozen=True, slots=True)
class TelegramWebhookResult:
    """Safe webhook processing outcome returned to the HTTP boundary."""

    status: Literal["duplicate", "ignored", "processed", "queued", "rejected"]


def parse_command(text: str | None) -> str | None:
    """Return a normalized Telegram command without a possible bot suffix."""

    if text is None:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    first_token = stripped.split(maxsplit=1)[0]
    command = first_token[1:].partition("@")[0].lower()
    return command or None


def format_olx_status(connection_status: ConnectionStatus | None) -> str:
    """Render a non-secret OLX connection state for the authorized user."""

    if connection_status is None:
        return "desconectado"
    labels = {
        ConnectionStatus.DISCONNECTED: "desconectado",
        ConnectionStatus.AUTHORIZED: "autorizado; webhook pendente",
        ConnectionStatus.CONNECTED: "conectado",
        ConnectionStatus.REAUTHORIZATION_REQUIRED: "reautorização necessária",
    }
    return labels[connection_status]


class TelegramWebhookService:
    """Authorize, deduplicate, and durably enqueue mapped Telegram replies."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        telegram: TelegramClient,
        updates: TelegramUpdateRepository | None = None,
        credentials: OlxCredentialRepository | None = None,
        messages: OlxMessageRepository | None = None,
        outbound: OutboundMessageRepository | None = None,
        jobs: DeliveryJobRepository | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.telegram = telegram
        self.updates = updates or TelegramUpdateRepository()
        self.credentials = credentials or OlxCredentialRepository()
        self.messages = messages or OlxMessageRepository()
        self.outbound = outbound or OutboundMessageRepository()
        self.jobs = jobs or DeliveryJobRepository()

    async def _olx_connection_status(self) -> ConnectionStatus | None:
        async with self.database.session_factory() as session:
            credential = await self.credentials.get_current(session)
            return credential.connection_status if credential is not None else None

    async def _set_update_status(
        self,
        update_id: int,
        update_status: TelegramUpdateStatus,
    ) -> None:
        async with self.database.session_factory.begin() as session:
            await self.updates.set_status(
                session,
                update_id=update_id,
                status=update_status,
            )

    async def _command_response(self, command: str) -> str:
        if command == "help":
            return HELP_TEXT

        olx_status = format_olx_status(await self._olx_connection_status())
        if command == "olx_status":
            return f"OLX: {olx_status}."

        if command == "status":
            database_status = "ok" if await self.database.is_ready() else "indisponível"
            telegram_status = "ok" if await self.telegram.is_available() else "indisponível"
            return (
                "Status do OLX Telegram Bridge:\n"
                "App: ok\n"
                f"Banco: {database_status}\n"
                f"Telegram: {telegram_status}\n"
                f"OLX: {olx_status}"
            )

        return "Comando não reconhecido. Use /help."

    async def _finish_with_feedback(
        self,
        *,
        update_id: int,
        chat_id: str,
        text: str,
    ) -> TelegramWebhookResult:
        await self.telegram.send_message(text, chat_id=chat_id)
        await self._set_update_status(update_id, TelegramUpdateStatus.PROCESSED)
        return TelegramWebhookResult(status="processed")

    async def _process_reply(
        self,
        *,
        payload: TelegramUpdatePayload,
        chat_id: str,
    ) -> TelegramWebhookResult:
        message = payload.message
        if message is None or message.reply_to_message is None:
            return await self._finish_with_feedback(
                update_id=payload.update_id,
                chat_id=chat_id,
                text=REPLY_GUIDANCE,
            )

        text = message.text.strip() if message.text is not None else ""
        if not text or len(text) > 4_096:
            return await self._finish_with_feedback(
                update_id=payload.update_id,
                chat_id=chat_id,
                text=INVALID_TEXT_GUIDANCE,
            )

        async with self.database.session_factory() as session:
            reference = await self.messages.get_by_telegram_message_id(
                session,
                telegram_message_id=message.reply_to_message.message_id,
                telegram_chat_id=chat_id,
            )
        if reference is None or reference.origin != "buyer":
            return await self._finish_with_feedback(
                update_id=payload.update_id,
                chat_id=chat_id,
                text=REPLY_GUIDANCE,
            )

        async with self.database.session_factory.begin() as session:
            outbound, _created = await self.outbound.add_if_new(
                session,
                telegram_update_id=payload.update_id,
                olx_chat_id=reference.chat_id,
                olx_reference_message_id=reference.message_id,
                text=text,
            )
            await self.jobs.add_telegram_to_olx_if_new(
                session,
                outbound_message_id=outbound.id,
                max_attempts=self.settings.delivery_max_attempts,
            )

        await self._set_update_status(payload.update_id, TelegramUpdateStatus.PROCESSED)
        return TelegramWebhookResult(status="queued")

    async def process(self, payload: TelegramUpdatePayload) -> TelegramWebhookResult:
        """Process one authorized command or persist one mapped OLX Reply idempotently."""

        async with self.database.session_factory.begin() as session:
            record, created = await self.updates.add_if_new(
                session,
                update_id=payload.update_id,
            )
        if not created and record.status != TelegramUpdateStatus.FAILED:
            return TelegramWebhookResult(status="duplicate")

        message = payload.message
        chat_id = str(message.chat.id) if message is not None else ""
        sender_id = (
            message.sender.id if message is not None and message.sender is not None else None
        )
        authorized = (
            message is not None
            and chat_id == self.settings.telegram_target_chat_id
            and sender_id in self.settings.allowed_telegram_user_ids
        )
        if not authorized:
            await self._set_update_status(payload.update_id, TelegramUpdateStatus.REJECTED)
            return TelegramWebhookResult(status="rejected")

        command = parse_command(message.text)
        if command is None:
            return await self._process_reply(payload=payload, chat_id=chat_id)

        response_text = await self._command_response(command)
        try:
            await self.telegram.send_message(response_text, chat_id=chat_id)
        except TelegramDeliveryError:
            await self._set_update_status(payload.update_id, TelegramUpdateStatus.FAILED)
            raise

        await self._set_update_status(payload.update_id, TelegramUpdateStatus.PROCESSED)
        return TelegramWebhookResult(status="processed")
