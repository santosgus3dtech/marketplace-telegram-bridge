"""Security boundary for inbound official OLX Chat webhooks."""

import logging
import secrets

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.schemas.olx import OlxWebhookPayload
from app.services.bridge import OlxWebhookService

router = APIRouter(prefix="/webhooks/olx", tags=["olx-webhook"])
logger = logging.getLogger(__name__)


class BodyTooLarge(ValueError):
    """The request body exceeded the configured byte limit."""


async def read_limited_body(request: Request, maximum_bytes: int) -> bytes:
    """Read a streamed request body while enforcing a hard allocation limit."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise BodyTooLarge("invalid content length") from error
        if declared_length < 0 or declared_length > maximum_bytes:
            raise BodyTooLarge("declared body is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise BodyTooLarge("streamed body is too large")
    return bytes(body)


def secure_equals(first: str, second: str) -> bool:
    """Compare path/header secrets without content-dependent timing."""

    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


@router.post("/{path_secret}")
async def receive_olx_webhook(path_secret: str, request: Request) -> JSONResponse:
    """Validate and atomically persist one OLX message plus its delivery job."""

    settings: Settings = request.app.state.settings
    configured_secret = settings.olx_webhook_path_secret.get_secret_value()
    if not configured_secret:
        return JSONResponse(
            {"detail": "OLX webhook is not configured"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not secure_equals(path_secret, configured_secret):
        return JSONResponse({"detail": "Not found"}, status_code=status.HTTP_404_NOT_FOUND)

    if settings.trust_cloudflare:
        source_ip = request.headers.get("cf-connecting-ip", "")
        if not secure_equals(source_ip, settings.olx_allowed_source_ip):
            logger.warning(
                "olx_webhook_source_rejected",
                extra={"event": "olx_webhook", "status": "invalid_source"},
            )
            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse(
            {"detail": "Content-Type must be application/json"},
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    try:
        body = await read_limited_body(request, settings.olx_webhook_body_max_bytes)
    except BodyTooLarge:
        return JSONResponse(
            {"detail": "Request body too large"},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )

    try:
        payload = OlxWebhookPayload.model_validate_json(body, strict=True)
    except (ValidationError, ValueError):
        return JSONResponse(
            {"detail": "Invalid OLX webhook payload"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    service = OlxWebhookService(max_attempts=settings.delivery_max_attempts)
    result = await service.process(request.app.state.database.session_factory, payload)

    logger.info(
        "olx_webhook_processed",
        extra={"event": "olx_webhook", "status": result.status},
    )
    return JSONResponse({"status": result.status}, status_code=status.HTTP_200_OK)
