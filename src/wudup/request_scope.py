"""Per-request phase timing and read-only command reuse for WebUI requests.

The WebUI opens a scope for each HTTP request. Docker commands and WUD API calls
made while it is active report their time, which the response exposes as a
``Server-Timing`` header. Read-only requests also reuse the result of an
identical read-only Docker command inside that one request, so planning and its
Doctor preflight do not render the same Compose stack twice. Local Docker state
is never reused across requests. The one exception is a successful check that
a registry tag exists, which plan review and plan apply requests may reuse for
a few minutes so the apply-time recheck of a reviewed plan does not repeat every registry call
and cannot flip on a transient registry failure. Registry output such as
digests is never reused. Code running outside a request (background jobs, the
CLI) sees no scope and never reuses anything.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
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


# Successful registry tag-existence checks only; failures are always retried.
REGISTRY_CHECK_TTL_SECONDS = 300.0
REGISTRY_CHECK_LIMIT = 256


@dataclass
class RequestScope:
    reuse_reads: bool
    reuse_registry_checks: bool = False
    active: bool = True
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
_registry_checks: OrderedDict[Hashable, float] = OrderedDict()
_registry_lock = threading.Lock()


def begin(
    *,
    reuse_reads: bool,
    reuse_registry_checks: bool = False,
) -> Token[RequestScope | None]:
    return _current.set(
        RequestScope(
            reuse_reads=reuse_reads,
            reuse_registry_checks=reuse_registry_checks,
        )
    )


def end(token: Token[RequestScope | None]) -> RequestScope | None:
    scope = _current.get()
    _current.reset(token)
    if scope is not None:
        # Work that outlives the response, such as a streamed body, may still
        # hold this scope through a copied context; it must read fresh state.
        with scope._lock:
            scope.active = False
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


def reuse_registry_check(key: Hashable, check: Callable[[], None]) -> None:
    """Run ``check()``, skipping it after a recent identical success.

    ``check`` raises when the registry lookup fails. Only requests whose scope
    opts in (plan review and its apply-time recheck) reuse these checks, and
    only successes are stored, so a transient registry failure is retried
    instead of remembered. No registry output is stored.
    """

    scope = _current.get()
    if scope is None or not scope.active or not scope.reuse_registry_checks:
        check()
        return
    now = time.monotonic()
    with _registry_lock:
        checked_at = _registry_checks.get(key)
        if checked_at is not None and now - checked_at < REGISTRY_CHECK_TTL_SECONDS:
            with scope._lock:
                scope.reused += 1
            return
        _registry_checks.pop(key, None)
    check()
    with _registry_lock:
        _registry_checks[key] = time.monotonic()
        _registry_checks.move_to_end(key)
        while len(_registry_checks) > REGISTRY_CHECK_LIMIT:
            _registry_checks.popitem(last=False)


def clear_registry_checks() -> None:
    with _registry_lock:
        _registry_checks.clear()


def server_timing_header(scope: RequestScope | None) -> str:
    """Format recorded phases, or return "" when the request did no tracked work."""

    if scope is None:
        return ""
    # A streamed body may still record into this scope while the header is built.
    with scope._lock:
        phases = sorted(
            (name, phase.seconds, phase.count) for name, phase in scope.phases.items()
        )
        reused = scope.reused
    if not phases:
        return ""
    entries = [
        f'{name};dur={seconds * 1000:.1f};desc="{count} calls"'
        for name, seconds, count in phases
    ]
    if reused:
        entries.append(f'reused;desc="{reused} reads"')
    return ", ".join(entries)
