"""Liveness and readiness endpoints."""

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["operations"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict[str, str]:
    """Report process liveness without touching external dependencies."""

    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def ready(request: Request) -> dict[str, object] | JSONResponse:
    """Report whether the database dependency is available."""

    database = request.app.state.database
    if await database.is_ready():
        return {"status": "ready", "checks": {"database": "ok"}}

    logger.warning(
        "readiness_check_failed",
        extra={"event": "readiness_check", "status": "not_ready"},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "checks": {"database": "unavailable"}},
    )
