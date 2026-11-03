"""Read-only integrations API routes (`/api/integrations/v1`).

This router accepts only the read-only API token and registers only GET
routes, so it cannot reach any mutation even when WebUI mutations are on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from . import web_integration_summary
from .web_integration_auth import require_readonly_token
from .web_integration_models import (
    IntegrationServiceResponse,
    IntegrationSummaryResponse,
)
from .web_request_context import request_settings

INTEGRATIONS_PREFIX = "/api/integrations/v1"


def register_integration_routes(app: FastAPI) -> None:
    router = APIRouter(
        prefix=INTEGRATIONS_PREFIX,
        dependencies=[Depends(require_readonly_token)],
    )
    _add_read_route(
        router,
        "/summary",
        api_integration_summary,
        response_model=IntegrationSummaryResponse,
    )
    _add_read_route(
        router,
        "/services/{stack}/{service}",
        api_integration_service,
        response_model=IntegrationServiceResponse,
    )
    app.include_router(router)


def _add_read_route(
    router: APIRouter,
    path: str,
    endpoint: Callable[..., Any],
    *,
    response_model: type[Any],
) -> None:
    # The only way to add an integrations route: methods are fixed to GET.
    router.add_api_route(path, endpoint, methods=["GET"], response_model=response_model)


def api_integration_summary(request: Request) -> IntegrationSummaryResponse:
    return web_integration_summary.integration_summary(request_settings(request))


def api_integration_service(
    stack: str,
    service: str,
    request: Request,
) -> IntegrationServiceResponse:
    try:
        return web_integration_summary.integration_service(
            request_settings(request),
            stack,
            service,
        )
    except web_integration_summary.IntegrationServiceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
