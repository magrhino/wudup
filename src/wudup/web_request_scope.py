"""Request scope middleware: Server-Timing and per-request read reuse."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from . import request_scope

# Plan preview only reads Docker and Compose state, so it may reuse reads too.
# Match exact paths so a future mutating route never inherits reuse by name.
_READ_ONLY_POST_PATHS = frozenset({"/api/v1/plans"})
# Plan review and its apply-time recheck may reuse a recent successful registry
# tag check, so applying a just-reviewed plan does not repeat every lookup.
# Other mutations, such as self-update prepare, always ask the registry.
_REGISTRY_CHECK_REUSE_PATHS = frozenset(
    {"/api/v1/plans", "/api/v1/plans/apply", "/api/v1/jobs"}
)


def reuses_reads(request: Request) -> bool:
    if request.method in {"GET", "HEAD"}:
        return True
    return request.method == "POST" and request.url.path in _READ_ONLY_POST_PATHS


def reuses_registry_checks(request: Request) -> bool:
    return (
        request.method == "POST"
        and request.url.path in _REGISTRY_CHECK_REUSE_PATHS
    )


async def request_scope_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    token = request_scope.begin(
        reuse_reads=reuses_reads(request),
        reuse_registry_checks=reuses_registry_checks(request),
    )
    try:
        response = await call_next(request)
    finally:
        scope = request_scope.end(token)
    header = request_scope.server_timing_header(scope)
    if header:
        response.headers["Server-Timing"] = header
    return response
