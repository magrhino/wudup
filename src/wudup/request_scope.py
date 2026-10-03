"""Per-request phase timing and read-only command reuse for WebUI requests.

The WebUI opens a scope for each HTTP request. Docker commands and WUD API calls
made while it is active report their time, which the response exposes as a
``Server-Timing`` header. Read-only requests also reuse the result of an
identical read-only Docker command inside that one request, so planning and its
Doctor preflight do not render the same Compose stack twice. Nothing is reused
across requests, and code running outside a request (background jobs, the CLI)
sees no scope at all.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TypeVar

_Result = TypeVar("_Result")


@dataclass
class _Phase:
    seconds: float = 0.0
    count: int = 0


@dataclass
class RequestScope:
    reuse_reads: bool
    phases: dict[str, _Phase] = field(default_factory=dict)
    reused: int = 0
    _reads: dict[Hashable, object] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, phase: str, seconds: float) -> None:
        with self._lock:
            entry = self.phases.setdefault(phase, _Phase())
            entry.seconds += seconds
            entry.count += 1


_current: ContextVar[RequestScope | None] = ContextVar(
    "wudup_request_scope",
    default=None,
)


def begin(*, reuse_reads: bool) -> Token[RequestScope | None]:
    return _current.set(RequestScope(reuse_reads=reuse_reads))


def end(token: Token[RequestScope | None]) -> RequestScope | None:
    scope = _current.get()
    _current.reset(token)
    if scope is not None:
        # Work that outlives the response, such as a streamed body, may still
        # hold this scope through a copied context; it must read fresh state.
        with scope._lock:
            scope.reuse_reads = False
            scope._reads.clear()
    return scope


def current() -> RequestScope | None:
    return _current.get()


@contextmanager
def timed(phase: str) -> Iterator[None]:
    scope = _current.get()
    if scope is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        scope.record(phase, time.perf_counter() - started)


def reuse_read(key: Hashable, compute: Callable[[], _Result]) -> _Result:
    """Return ``compute()``, reusing an identical earlier read in this request."""

    scope = _current.get()
    if scope is None or not scope.reuse_reads:
        return compute()
    with scope._lock:
        if key in scope._reads:
            scope.reused += 1
            return scope._reads[key]  # type: ignore[return-value]
    result = compute()
    with scope._lock:
        if scope.reuse_reads:
            scope._reads.setdefault(key, result)
    return result


def server_timing_header(scope: RequestScope | None) -> str:
    """Format recorded phases, or return "" when the request did no tracked work."""

    if scope is None or not scope.phases:
        return ""
    entries = [
        f'{name};dur={phase.seconds * 1000:.1f};desc="{phase.count} calls"'
        for name, phase in sorted(scope.phases.items())
    ]
    if scope.reused:
        entries.append(f'reused;desc="{scope.reused} reads"')
    return ", ".join(entries)
