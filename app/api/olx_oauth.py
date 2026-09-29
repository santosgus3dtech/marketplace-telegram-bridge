"""OLX OAuth authorization-code routes."""

import logging
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients.olx import (
    OlxChatClient,
    OlxOAuthClient,
    OlxTokenExchangeRejected,
    OlxTokenExchangeUnavailable,
    OlxWebhookRegistrationUnavailable,
    OlxWebhookUnauthorized,
)
from app.config import Settings
from app.services.credentials import CredentialService
from app.services.oauth import InvalidOAuthState, OlxOAuthFlowService
from app.services.security import TokenCipher

router = APIRouter(prefix="/oauth/olx", tags=["olx-oauth"])
logger = logging.getLogger(__name__)


def html_response(message: str, status_code: int) -> HTMLResponse:
    """Return a minimal static page that cannot echo OAuth secrets."""

    body = (
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        "<title>OLX Telegram Bridge</title></head>"
        f"<body><p>{message}</p></body></html>"
    )
    return HTMLResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


def build_flow(request: Request) -> tuple[Settings, OlxOAuthFlowService]:
    """Build the OAuth service from application-owned dependencies."""

    settings: Settings = request.app.state.settings
    required_values = (
        settings.olx_client_id,
        settings.olx_client_secret.get_secret_value(),
        settings.olx_redirect_uri,
        settings.token_encryption_key.get_secret_value(),
        settings.olx_webhook_path_secret.get_secret_value(),
    )
    if not all(required_values):
        raise ValueError("OLX OAuth configuration is incomplete")

    cipher = TokenCipher(settings.token_encryption_key)
    client = OlxOAuthClient(settings, request.app.state.outbound_http_client)
    chat_client = OlxChatClient(settings, request.app.state.outbound_http_client)
    webhook_secret = quote(settings.olx_webhook_path_secret.get_secret_value(), safe="")
    webhook_url = f"{settings.public_base_url.rstrip('/')}/webhooks/olx/{webhook_secret}"
    flow = OlxOAuthFlowService(
        client=client,
        chat_client=chat_client,
        webhook_url=webhook_url,
        credentials=CredentialService(cipher),
    )
    return settings, flow


@router.get("/start", response_model=None)
async def start(request: Request) -> RedirectResponse | HTMLResponse:
    """Persist a state digest and redirect the browser to OLX authorization."""

    try:
        settings, flow = build_flow(request)
    except ValueError:
        logger.error(
            "olx_oauth_configuration_invalid",
            extra={"event": "olx_oauth_start", "status": "configuration_error"},
        )
        return html_response(
            "A integração OLX ainda não está configurada.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    state_value = await flow.begin(request.app.state.database.session_factory)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.olx_client_id,
            "redirect_uri": settings.olx_redirect_uri,
            "scope": settings.olx_scope,
            "state": state_value,
        }
    )
    response = RedirectResponse(
        f"{settings.olx_auth_url}?{query}", status_code=status.HTTP_302_FOUND
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback", response_model=None)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Validate callback state and store the exchanged token encrypted at rest."""

    if error is not None:
        logger.warning(
            "olx_oauth_authorization_denied",
            extra={"event": "olx_oauth_callback", "status": "denied"},
        )
        return html_response(
            "A autorização da OLX foi cancelada ou negada.", status.HTTP_400_BAD_REQUEST
        )
    if not code or not state:
        return html_response("Callback OAuth incompleto.", status.HTTP_400_BAD_REQUEST)

    try:
        _settings, flow = build_flow(request)
    except ValueError:
        logger.error(
            "olx_oauth_configuration_invalid",
            extra={"event": "olx_oauth_callback", "status": "configuration_error"},
        )
        return html_response(
            "A integração OLX ainda não está configurada.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        await flow.complete(
            request.app.state.database.session_factory,
            state=state,
            code=code,
        )
    except InvalidOAuthState:
        logger.warning(
            "olx_oauth_state_rejected",
            extra={"event": "olx_oauth_callback", "status": "invalid_state"},
        )
        return html_response("Estado OAuth inválido ou expirado.", status.HTTP_400_BAD_REQUEST)
    except OlxTokenExchangeRejected:
        logger.warning(
            "olx_oauth_token_rejected",
            extra={"event": "olx_oauth_callback", "status": "token_rejected"},
        )
        return html_response("A OLX rejeitou o código de autorização.", status.HTTP_400_BAD_REQUEST)
    except OlxTokenExchangeUnavailable:
        logger.error(
            "olx_oauth_token_unavailable",
            extra={"event": "olx_oauth_callback", "status": "upstream_error"},
        )
        return html_response(
            "Não foi possível concluir a autorização com a OLX.",
            status.HTTP_502_BAD_GATEWAY,
        )
    except OlxWebhookUnauthorized:
        logger.warning(
            "olx_webhook_registration_unauthorized",
            extra={"event": "olx_oauth_callback", "status": "reauthorization_required"},
        )
        return html_response(
            "A OLX recusou a autorização. Inicie uma nova conexão.",
            status.HTTP_401_UNAUTHORIZED,
        )
    except OlxWebhookRegistrationUnavailable:
        logger.error(
            "olx_webhook_registration_unavailable",
            extra={"event": "olx_oauth_callback", "status": "upstream_error"},
        )
        return html_response(
            "A autorização foi salva, mas o webhook OLX não pôde ser registrado.",
            status.HTTP_502_BAD_GATEWAY,
        )

    logger.info(
        "olx_oauth_connected",
        extra={"event": "olx_oauth_callback", "status": "connected"},
    )
    return html_response("Integração OLX conectada com sucesso.", status.HTTP_200_OK)
