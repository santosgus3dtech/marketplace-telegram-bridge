"""Security boundary for inbound Telegram Bot API webhooks."""

import logging
import secrets

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.olx_webhook import BodyTooLarge, read_limited_body
from app.clients.telegram import TelegramClient, TelegramDeliveryError
from app.config import Settings
from app.schemas.telegram import TelegramUpdatePayload
from app.services.telegram import TelegramWebhookService

router = APIRouter(prefix="/webhooks/telegram", tags=["telegram-webhook"])
logger = logging.getLogger(__name__)


def secure_header_equals(first: str, second: str) -> bool:
    """Compare webhook secrets without content-dependent timing."""

    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


@router.post("")
async def receive_telegram_webhook(request: Request) -> JSONResponse:
    """Validate, authorize, deduplicate, and process one Telegram update."""

    settings: Settings = request.app.state.settings
    configured_secret = settings.telegram_webhook_secret.get_secret_value()
    if (
        not configured_secret
        or not settings.telegram_bot_token.get_secret_value()
        or not settings.telegram_target_chat_id
        or not settings.allowed_telegram_user_ids
    ):
        return JSONResponse(
            {"detail": "Telegram webhook is not configured"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    provided_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secure_header_equals(provided_secret, configured_secret):
        logger.warning(
            "telegram_webhook_secret_rejected",
            extra={"event": "telegram_webhook", "status": "invalid_secret"},
        )
        return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse(
            {"detail": "Content-Type must be application/json"},
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    try:
        body = await read_limited_body(request, settings.telegram_webhook_body_max_bytes)
    except BodyTooLarge:
        return JSONResponse(
            {"detail": "Request body too large"},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )

    try:
        payload = TelegramUpdatePayload.model_validate_json(body, strict=True)
    except (ValidationError, ValueError):
        return JSONResponse(
            {"detail": "Invalid Telegram webhook payload"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    telegram = TelegramClient(settings, request.app.state.outbound_http_client)
    service = TelegramWebhookService(
        settings=settings,
        database=request.app.state.database,
        telegram=telegram,
    )
    try:
        result = await service.process(payload)
    except TelegramDeliveryError:
        logger.error(
            "telegram_webhook_response_failed",
            extra={"event": "telegram_webhook", "status": "telegram_error"},
        )
        return JSONResponse(
            {"detail": "Telegram response delivery failed"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    logger.info(
        "telegram_webhook_processed",
        extra={"event": "telegram_webhook", "status": result.status},
    )
    return JSONResponse({"status": result.status}, status_code=status.HTTP_200_OK)
