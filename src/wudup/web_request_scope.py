"""Request scope middleware: Server-Timing and per-request read reuse."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from . import request_scope

# Plan preview only reads Docker and Compose state, so it may reuse reads too.
_READ_ONLY_POST_SUFFIXES = ("/api/v1/plans",)


def reuses_reads(request: Request) -> bool:
    if request.method in {"GET", "HEAD"}:
        return True
    return request.method == "POST" and request.url.path.endswith(
        _READ_ONLY_POST_SUFFIXES
    )


async def request_scope_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    token = request_scope.begin(reuse_reads=reuses_reads(request))
    try:
        response = await call_next(request)
    finally:
        scope = request_scope.end(token)
    header = request_scope.server_timing_header(scope)
    if header:
        response.headers["Server-Timing"] = header
    return response
