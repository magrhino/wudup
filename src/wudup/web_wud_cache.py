"""Single owner of WUD snapshots, refresh serialization, and watch cooldowns.

The existing cache lock protects every dictionary transition. The refresh lock
still serializes network refresh and observation checkpoint publication; neither
lock nor cache is duplicated by the public web_wud_api facade.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
import urllib.error
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock

from . import web_wud_config, web_wud_observation_store, web_wud_transport
from .db import DatabaseError
from .web_models import (
    WebSettings,
    WudApiConfigurationDiagnostics,
    WudApiObservationDiagnostic,
    WudApiState,
    WudApiStatus,
)
from .web_wud_config import _auth_required_detail
from .web_wud_observations import (
    WudApiContainer,
    _container_from_stored_observation,
    _container_identity,
    _count_phrase,
    _observation_status_detail,
    _parse_container_observation,
    _PendingObservation,
    _reconcile_container_observations,
    _stored_observation,
)
from .web_wud_transport import (
    DEFAULT_WUD_API_BASE_URL,
    _join_url,
    _normalize_base_url,
    _sanitize_detail,
)

WUD_API_WATCH_TIMEOUT_SECONDS = 120.0
WUD_API_WATCH_BATCH_TIMEOUT_SECONDS = 120.0
WUD_API_STARTUP_RETRY_INTERVAL_SECONDS = 0.5
WUD_API_CACHE_TTL_SECONDS = 30.0
WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS = 5.0
WUD_API_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_HTTP_429_RE = re.compile(r"(?:^|\D)429(?:\D|$)")
LOGGER = logging.getLogger("wudup.web_wud_api")

@dataclass(frozen=True)
class WudApiSnapshot:
    status: WudApiStatus
    containers: tuple[WudApiContainer, ...] = ()
    inventory_containers: tuple[WudApiContainer, ...] = ()
    unresolved_containers: tuple[WudApiContainer, ...] = ()
    hidden_update_candidates: tuple[WudApiContainer, ...] = ()
    retryable_degraded_container_ids: tuple[str, ...] = ()
    degraded_container_count: int = 0
    retained_update_count: int = 0
    recovered_update_count: int = 0
    unsupported_container_count: int = 0
    observation_diagnostics: tuple[WudApiObservationDiagnostic, ...] = ()
    metadata_checked: bool = False
    checked_monotonic: float = 0.0


@dataclass(frozen=True)
class WudApiWatchResult:
    snapshot: WudApiSnapshot
    watched: bool
    requested_count: int = 0
    watched_count: int = 0
    remaining_degraded_container_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _WudApiWatchBatchResult:
    watched_all: bool
    watched_count: int
    missing_count: int
    cooldown_remaining: float
    error: WudApiWatchResult | None = None


WudApiConfigurationSnapshot = web_wud_config.WudApiConfigurationSnapshot
WudApiCacheKey = tuple[str, str]
WudApiWatchCooldownKey = tuple[WudApiCacheKey, str]
WudContainerIdentity = web_wud_observation_store.WudContainerIdentity


_cache_lock = Lock()
# ponytail: global lock; use per-cache locks only if WUD refresh contention is measured.
_refresh_lock = Lock()
_snapshot_cache: dict[WudApiCacheKey, WudApiSnapshot] = {}
_pending_observation_cache: dict[
    WudApiCacheKey,
    Mapping[WudContainerIdentity, _PendingObservation],
] = {}
_configuration_diagnostics_cache: dict[WudApiCacheKey, WudApiConfigurationSnapshot] = {}
_watch_rate_limit_until: dict[WudApiWatchCooldownKey, float] = {}
_WATCH_ALL_COOLDOWN_CONTAINER_ID = "*"


def startup_probe(settings: WebSettings) -> WudApiSnapshot:
    snapshot = _refresh_snapshot(settings, include_containers=False)
    wait_seconds = max(settings.wud_api_startup_wait_seconds, 0.0)
    if snapshot.status.state != "unavailable" or wait_seconds <= 0:
        return snapshot

    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return snapshot
        time.sleep(min(WUD_API_STARTUP_RETRY_INTERVAL_SECONDS, remaining))
        snapshot = _refresh_snapshot(settings, include_containers=False)
        if snapshot.status.state != "unavailable":
            return snapshot


def initialize_pending_observation_cache(settings: WebSettings) -> None:
    """Load restart-safe pending observations before serving requests."""

    if _observation_database_exists(settings) and settings.wud_api_client.configured:
        try:
            web_wud_observation_store.replace_pending_observations(
                settings.config.db_path,
                owner_uid=settings.config.out_uid,
                source=_observation_store_source(settings),
                observations=(),
            )
        except (OSError, ValueError, sqlite3.Error, DatabaseError) as exc:
            LOGGER.warning(
                "failed to clear persisted WUD pending observations: %s",
                _safe_cache_error(settings, exc),
            )
        return
    if not _observation_persistence_enabled(settings):
        return
    try:
        source = _observation_store_source(settings)
        stored = web_wud_observation_store.load_pending_observations(
            settings.config.db_path,
            owner_uid=settings.config.out_uid,
            source=source,
        )
    except (OSError, ValueError, sqlite3.Error, DatabaseError) as exc:
        LOGGER.warning(
            "failed to load persisted WUD pending observations: %s",
            _safe_cache_error(settings, exc),
        )
        return

    pending: dict[WudContainerIdentity, _PendingObservation] = {}
    for item in stored:
        container = _container_from_stored_observation(item.observation)
        if container is None or _container_identity(container) != item.identity:
            LOGGER.warning("ignored malformed persisted WUD pending observation")
            continue
        pending[item.identity] = _PendingObservation(
            container=container,
            observed_at=item.observed_at,
        )

    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    with _cache_lock:
        _pending_observation_cache[_cache_key(settings, base_url)] = pending


def checkpoint_pending_observation_cache(settings: WebSettings) -> None:
    """Atomically persist the latest in-memory pending observations."""

    if not _observation_persistence_enabled(settings):
        return
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    cache_key = _cache_key(settings, base_url)
    with _refresh_lock:
        with _cache_lock:
            cached = _pending_observation_cache.get(cache_key)
            if cached is None:
                return

        stored = tuple(
            web_wud_observation_store.StoredPendingObservation(
                identity=identity,
                observation=_stored_observation(state.container),
                observed_at=state.observed_at,
            )
            for identity, state in cached.items()
        )
        try:
            web_wud_observation_store.replace_pending_observations(
                settings.config.db_path,
                owner_uid=settings.config.out_uid,
                source=_observation_store_source(settings),
                observations=stored,
            )
        except (OSError, ValueError, sqlite3.Error, DatabaseError) as exc:
            LOGGER.warning(
                "failed to persist WUD pending observations: %s",
                _safe_cache_error(settings, exc),
            )


def get_snapshot(
    settings: WebSettings,
    *,
    include_containers: bool = False,
    force: bool = False,
) -> WudApiSnapshot:
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    cache_key = _cache_key(settings, base_url)
    now = time.monotonic()
    with _cache_lock:
        cached = _snapshot_cache.get(cache_key)
        if (
            not force
            and cached is not None
            and now - cached.checked_monotonic < _snapshot_cache_ttl(cached)
            and (not include_containers or cached.metadata_checked)
        ):
            return cached
    return _refresh_snapshot(settings, include_containers=include_containers)


def get_configuration_diagnostics(
    settings: WebSettings,
    *,
    force: bool = False,
) -> WudApiConfigurationDiagnostics:
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    cache_key = _cache_key(settings, base_url)
    now = time.monotonic()
    with _cache_lock:
        cached = _configuration_diagnostics_cache.get(cache_key)
        if (
            not force
            and cached is not None
            and now - cached.checked_monotonic
            < web_wud_config.configuration_diagnostics_cache_ttl(
                cached,
                cache_ttl=WUD_API_CACHE_TTL_SECONDS,
                degraded_retry_interval=WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS,
            )
        ):
            return cached.diagnostics
    return _refresh_configuration_diagnostics(settings).diagnostics


def _snapshot_cache_ttl(snapshot: WudApiSnapshot) -> float:
    if snapshot.status.state in {"unavailable", "error"} or snapshot.degraded_container_count:
        return WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS
    return WUD_API_CACHE_TTL_SECONDS


def _cache_key(settings: WebSettings, base_url: str) -> WudApiCacheKey:
    return (base_url, settings.wud_api_client.fingerprint)


def watch_all(settings: WebSettings) -> WudApiWatchResult:
    result = _watch_paths(settings, ("/api/containers/watch",))
    return replace(
        result,
        remaining_degraded_container_ids=result.snapshot.retryable_degraded_container_ids,
    )


def watch_containers(
    settings: WebSettings,
    container_ids: Sequence[str],
) -> WudApiWatchResult:
    container_ids = tuple(
        dict.fromkeys(
            container_id for container_id in container_ids if container_id
        )
    )
    paths = tuple(
        f"/api/containers/{urllib.parse.quote(container_id, safe='')}/watch"
        for container_id in container_ids
    )
    if not paths:
        return WudApiWatchResult(
            snapshot=get_snapshot(settings, include_containers=True, force=True),
            watched=False,
            requested_count=0,
            watched_count=0,
        )
    return _watch_paths(settings, paths, container_ids=container_ids)


def _refresh_snapshot(
    settings: WebSettings,
    *,
    include_containers: bool,
) -> WudApiSnapshot:
    with _refresh_lock:
        return _refresh_snapshot_serialized(
            settings,
            include_containers=include_containers,
        )


def _refresh_snapshot_serialized(
    settings: WebSettings,
    *,
    include_containers: bool,
) -> WudApiSnapshot:
    checked_at = _utc_timestamp()
    checked_monotonic = time.monotonic()
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    try:
        normalized_base_url = _normalize_base_url(base_url)
    except ValueError as exc:
        snapshot = _snapshot(
            "error",
            available=False,
            metadata_available=False,
            checked_at=checked_at,
            detail=_sanitize_detail(settings, f"invalid WUD API base URL: {exc}"),
            checked_monotonic=checked_monotonic,
            metadata_checked=include_containers,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot

    try:
        web_wud_transport._request_json(_join_url(normalized_base_url, "/health"), settings.wud_api_client)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            snapshot = _snapshot(
                "auth_required",
                available=True,
                metadata_available=False,
                checked_at=checked_at,
                detail=_auth_required_detail(
                    settings,
                    "WUD API requires authentication",
                ),
                checked_monotonic=checked_monotonic,
                metadata_checked=include_containers,
            )
        else:
            snapshot = _snapshot(
                "unavailable",
                available=False,
                metadata_available=False,
                checked_at=checked_at,
                detail=_sanitize_detail(
                    settings,
                    f"WUD API health check returned HTTP {exc.code}",
                ),
                checked_monotonic=checked_monotonic,
                metadata_checked=include_containers,
            )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot
    except (OSError, ValueError) as exc:
        snapshot = _snapshot(
            "unavailable",
            available=False,
            metadata_available=False,
            checked_at=checked_at,
            detail=_sanitize_detail(settings, f"WUD API is unavailable: {exc}"),
            checked_monotonic=checked_monotonic,
            metadata_checked=include_containers,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot

    if not include_containers:
        snapshot = _snapshot(
            "ready",
            available=True,
            metadata_available=False,
            checked_at=checked_at,
            detail="WUD API is reachable",
            checked_monotonic=checked_monotonic,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot

    try:
        payload = web_wud_transport._request_json(
            _join_url(normalized_base_url, "/api/containers"),
            settings.wud_api_client,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            snapshot = _snapshot(
                "auth_required",
                available=True,
                metadata_available=False,
                checked_at=checked_at,
                detail=_auth_required_detail(
                    settings,
                    "WUD API container metadata requires authentication",
                ),
                checked_monotonic=checked_monotonic,
                metadata_checked=True,
            )
        else:
            snapshot = _snapshot(
                "error",
                available=True,
                metadata_available=False,
                checked_at=checked_at,
                detail=_sanitize_detail(
                    settings,
                    f"WUD API container metadata returned HTTP {exc.code}",
                ),
                checked_monotonic=checked_monotonic,
                metadata_checked=True,
            )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot
    except (OSError, ValueError) as exc:
        snapshot = _snapshot(
            "error",
            available=True,
            metadata_available=False,
            checked_at=checked_at,
            detail=_sanitize_detail(
                settings,
                f"WUD API container metadata is unavailable: {exc}",
            ),
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot

    if not isinstance(payload, list):
        snapshot = _snapshot(
            "error",
            available=True,
            metadata_available=False,
            checked_at=checked_at,
            detail="WUD API container metadata payload was not a list",
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return snapshot

    cache_key = _cache_key(settings, base_url)
    (
        containers,
        inventory_containers,
        unresolved_containers,
        hidden_update_candidates,
        retryable_degraded_container_ids,
        degraded_container_count,
        retained_update_count,
        recovered_update_count,
        unsupported_container_count,
        observation_diagnostics,
        pending_observations,
    ) = _reconcile_container_observations(
        payload,
        settings,
        previous=_pending_observations(cache_key),
        observed_at=checked_at,
    )
    detail = _observation_status_detail(
        len(containers),
        degraded_container_count,
        retained_update_count,
        recovered_update_count,
        unsupported_container_count,
    )
    snapshot = replace(
        _snapshot(
            "ready",
            available=True,
            metadata_available=True,
            checked_at=checked_at,
            detail=detail,
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
            containers=containers,
            hidden_update_candidates=hidden_update_candidates,
            retryable_degraded_container_ids=retryable_degraded_container_ids,
            degraded_container_count=degraded_container_count,
            retained_update_count=retained_update_count,
            recovered_update_count=recovered_update_count,
        ),
        unresolved_containers=unresolved_containers,
        inventory_containers=inventory_containers,
        unsupported_container_count=unsupported_container_count,
        observation_diagnostics=tuple(observation_diagnostics),
    )
    _store_snapshot(
        cache_key,
        snapshot,
        pending_observations=pending_observations,
    )
    return snapshot


def _refresh_configuration_diagnostics(
    settings: WebSettings,
) -> WudApiConfigurationSnapshot:
    checked_at = _utc_timestamp()
    checked_monotonic = time.monotonic()
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    try:
        normalized_base_url = _normalize_base_url(base_url)
    except ValueError as exc:
        snapshot = web_wud_config.configuration_diagnostics_for_base_url_error(
            settings,
            error=exc,
            checked_at=checked_at,
            checked_monotonic=checked_monotonic,
            sanitize_detail=_sanitize_detail,
        )
        _store_configuration_diagnostics(_cache_key(settings, base_url), snapshot)
        return snapshot

    snapshot = web_wud_config.refresh_configuration_diagnostics(
        settings,
        normalized_base_url=normalized_base_url,
        checked_at=checked_at,
        checked_monotonic=checked_monotonic,
        request_json=lambda url: web_wud_transport._request_json(url, settings.wud_api_client),
        join_url=_join_url,
        sanitize_detail=_sanitize_detail,
    )
    _store_configuration_diagnostics(_cache_key(settings, base_url), snapshot)
    return snapshot


def _watch_paths(
    settings: WebSettings,
    paths: Sequence[str],
    *,
    container_ids: Sequence[str] = (),
) -> WudApiWatchResult:
    checked_at = _utc_timestamp()
    checked_monotonic = time.monotonic()
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    try:
        normalized_base_url = _normalize_base_url(base_url)
    except ValueError as exc:
        snapshot = _snapshot(
            "error",
            available=False,
            metadata_available=False,
            checked_at=checked_at,
            detail=_sanitize_detail(settings, f"invalid WUD API base URL: {exc}"),
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
        )
        _store_snapshot(_cache_key(settings, base_url), snapshot)
        return WudApiWatchResult(
            snapshot=snapshot,
            watched=False,
            requested_count=len(paths),
            watched_count=0,
            remaining_degraded_container_ids=(
                _remaining_degraded_container_ids(snapshot, container_ids)
            ),
        )

    cache_key = _cache_key(settings, base_url)
    watch_items, cooldown_remaining = _watch_items_after_cooldown(
        cache_key,
        paths,
        container_ids,
    )

    if not watch_items:
        snapshot = get_snapshot(settings, include_containers=True, force=True)
        snapshot = _with_watch_rate_limit_detail(
            snapshot,
            cooldown_remaining,
        )
        return WudApiWatchResult(
            snapshot=snapshot,
            watched=False,
            requested_count=len(paths),
            watched_count=0,
            remaining_degraded_container_ids=(
                _remaining_degraded_container_ids(snapshot, container_ids)
            ),
        )

    preflight = get_snapshot(settings, include_containers=False, force=True)
    if preflight.status.state != "ready":
        return WudApiWatchResult(
            snapshot=preflight,
            watched=False,
            requested_count=len(paths),
            watched_count=0,
        )

    batch = _watch_batch(
        settings,
        base_url=base_url,
        normalized_base_url=normalized_base_url,
        cache_key=cache_key,
        watch_items=watch_items,
        requested_count=len(paths),
        cooldown_remaining=cooldown_remaining,
    )
    if batch.error is not None:
        return batch.error

    snapshot = get_snapshot(settings, include_containers=True, force=True)
    if batch.missing_count:
        missing_detail = (
            f"WUD skipped {_count_phrase(batch.missing_count, 'container')} that no "
            f"longer {'exists' if batch.missing_count == 1 else 'exist'}."
        )
        detail = snapshot.status.detail.rstrip(";. ") if snapshot.status.detail else ""
        snapshot = replace(
            snapshot,
            status=snapshot.status.model_copy(
                update={
                    "detail": (
                        f"{detail}. {missing_detail}" if detail else missing_detail
                    )
                }
            ),
        )
    if batch.cooldown_remaining > 0:
        snapshot = _with_watch_rate_limit_detail(snapshot, batch.cooldown_remaining)
    return WudApiWatchResult(
        snapshot=snapshot,
        watched=batch.watched_all,
        requested_count=len(paths),
        watched_count=batch.watched_count,
        remaining_degraded_container_ids=_remaining_degraded_container_ids(
            snapshot,
            container_ids,
        ),
    )


def _watch_batch(
    settings: WebSettings,
    *,
    base_url: str,
    normalized_base_url: str,
    cache_key: WudApiCacheKey,
    watch_items: Sequence[tuple[str, str]],
    requested_count: int,
    cooldown_remaining: float,
) -> _WudApiWatchBatchResult:
    watched_count = 0
    watched_all = len(watch_items) == requested_count
    missing_count = 0
    remaining_watch_seconds = WUD_API_WATCH_BATCH_TIMEOUT_SECONDS
    for path, requested_container_id in watch_items:
        if remaining_watch_seconds <= 0:
            watched_all = False
            break
        request_started = time.monotonic()
        try:
            payload = web_wud_transport._post_json(
                _join_url(normalized_base_url, path),
                settings.wud_api_client,
                timeout=min(
                    WUD_API_WATCH_TIMEOUT_SECONDS,
                    remaining_watch_seconds,
                ),
            )
            remaining_watch_seconds -= max(
                0.0,
                time.monotonic() - request_started,
            )
            watched_count += 1
            # A global watch returns a list; selected watches return one container.
            for container_payload in payload if isinstance(payload, list) else [payload]:
                rate_limited_container_id = _watch_rate_limited_container_id(
                    container_payload,
                    settings,
                )
                if rate_limited_container_id is not None:
                    _start_watch_rate_limit_cooldown(
                        cache_key,
                        rate_limited_container_id or requested_container_id,
                    )
                    cooldown_remaining = max(
                        cooldown_remaining,
                        WUD_API_RATE_LIMIT_COOLDOWN_SECONDS,
                    )
                    watched_all = False
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and requested_container_id:
                remaining_watch_seconds -= max(
                    0.0,
                    time.monotonic() - request_started,
                )
                watched_all = False
                missing_count += 1
                continue
            snapshot = _watch_http_error_snapshot(
                settings,
                base_url=base_url,
                code=exc.code,
                checked_at=_utc_timestamp(),
                checked_monotonic=time.monotonic(),
            )
            return _WudApiWatchBatchResult(
                watched_all=False,
                watched_count=watched_count,
                missing_count=missing_count,
                cooldown_remaining=cooldown_remaining,
                error=WudApiWatchResult(
                    snapshot=snapshot,
                    watched=False,
                    requested_count=requested_count,
                    watched_count=watched_count,
                ),
            )
        except (OSError, ValueError) as exc:
            snapshot = _snapshot(
                "error",
                available=True,
                metadata_available=False,
                checked_at=_utc_timestamp(),
                detail=_sanitize_detail(
                    settings,
                    f"WUD API watch request failed: {exc}",
                ),
                checked_monotonic=time.monotonic(),
                metadata_checked=True,
            )
            _store_snapshot(cache_key, snapshot)
            return _WudApiWatchBatchResult(
                watched_all=False,
                watched_count=watched_count,
                missing_count=missing_count,
                cooldown_remaining=cooldown_remaining,
                error=WudApiWatchResult(
                    snapshot=snapshot,
                    watched=False,
                    requested_count=requested_count,
                    watched_count=watched_count,
                ),
            )
    return _WudApiWatchBatchResult(
        watched_all=watched_all,
        watched_count=watched_count,
        missing_count=missing_count,
        cooldown_remaining=cooldown_remaining,
    )


def _watch_items_after_cooldown(
    cache_key: WudApiCacheKey,
    paths: Sequence[str],
    container_ids: Sequence[str],
) -> tuple[list[tuple[str, str]], float]:
    watch_items: list[tuple[str, str]] = []
    cooldown_remaining = 0.0
    for index, path in enumerate(paths):
        container_id = container_ids[index] if index < len(container_ids) else ""
        item_cooldown_remaining = _watch_rate_limit_cooldown_remaining(
            cache_key,
            container_id,
        )
        if item_cooldown_remaining > 0:
            cooldown_remaining = max(
                cooldown_remaining,
                item_cooldown_remaining,
            )
            continue
        watch_items.append((path, container_id))
    return watch_items, cooldown_remaining


def _remaining_degraded_container_ids(
    snapshot: WudApiSnapshot,
    requested_container_ids: Sequence[str],
) -> tuple[str, ...]:
    degraded = set(snapshot.retryable_degraded_container_ids)
    return tuple(
        container_id
        for container_id in requested_container_ids
        if container_id in degraded
    )


def _watch_rate_limited_container_id(
    payload: object,
    settings: WebSettings,
) -> str | None:
    observation = _parse_container_observation(payload, settings)
    if (
        observation is not None
        and observation.degraded
        and _HTTP_429_RE.search(observation.container.error)
    ):
        return observation.container.id
    return None


def _watch_rate_limit_cooldown_remaining(
    cache_key: WudApiCacheKey,
    container_id: str,
) -> float:
    now = time.monotonic()
    with _cache_lock:
        _prune_expired_watch_rate_limits(now)
        if container_id:
            cooldown_keys = (
                (cache_key, _WATCH_ALL_COOLDOWN_CONTAINER_ID),
                (cache_key, container_id),
            )
        else:
            cooldown_keys = tuple(
                key for key in _watch_rate_limit_until if key[0] == cache_key
            )
        retry_at = 0.0
        for cooldown_key in cooldown_keys:
            item_retry_at = _watch_rate_limit_until.get(cooldown_key, 0.0)
            retry_at = max(retry_at, item_retry_at)
    return max(0.0, retry_at - now)


def _start_watch_rate_limit_cooldown(
    cache_key: WudApiCacheKey,
    container_id: str,
) -> None:
    cooldown_container_id = container_id or _WATCH_ALL_COOLDOWN_CONTAINER_ID
    now = time.monotonic()
    with _cache_lock:
        _prune_expired_watch_rate_limits(now)
        _watch_rate_limit_until[(cache_key, cooldown_container_id)] = (
            now + WUD_API_RATE_LIMIT_COOLDOWN_SECONDS
        )


def _prune_expired_watch_rate_limits(now: float) -> None:
    for cooldown_key, retry_at in tuple(_watch_rate_limit_until.items()):
        if retry_at <= now:
            _watch_rate_limit_until.pop(cooldown_key, None)


def _with_watch_rate_limit_detail(
    snapshot: WudApiSnapshot,
    cooldown_remaining: float,
) -> WudApiSnapshot:
    cooldown_seconds = math.ceil(cooldown_remaining)
    detail = (
        "WUD temporarily paused registry checks after receiving HTTP 429. "
        f"Try again in {_count_phrase(cooldown_seconds, 'second')}."
    )
    if snapshot.status.detail:
        detail = f"{snapshot.status.detail.rstrip(';. ')}. {detail}"
    return replace(
        snapshot,
        status=snapshot.status.model_copy(update={"detail": detail}),
    )


def _watch_http_error_snapshot(
    settings: WebSettings,
    *,
    base_url: str,
    code: int,
    checked_at: str,
    checked_monotonic: float,
) -> WudApiSnapshot:
    if code in {401, 403}:
        snapshot = _snapshot(
            "auth_required",
            available=True,
            metadata_available=False,
            checked_at=checked_at,
            detail=_auth_required_detail(
                settings,
                "WUD API watch request requires authentication",
            ),
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
        )
    else:
        snapshot = _snapshot(
            "error",
            available=True,
            metadata_available=False,
            checked_at=checked_at,
            detail=_sanitize_detail(
                settings,
                f"WUD API watch request returned HTTP {code}",
            ),
            checked_monotonic=checked_monotonic,
            metadata_checked=True,
        )
    _store_snapshot(_cache_key(settings, base_url), snapshot)
    return snapshot


def _store_configuration_diagnostics(
    cache_key: WudApiCacheKey,
    snapshot: WudApiConfigurationSnapshot,
) -> None:
    with _cache_lock:
        _configuration_diagnostics_cache[cache_key] = snapshot


def _pending_observations(
    cache_key: WudApiCacheKey,
) -> Mapping[WudContainerIdentity, _PendingObservation]:
    with _cache_lock:
        cached = _pending_observation_cache.get(cache_key)
        return {} if cached is None else cached


def _store_snapshot(
    cache_key: WudApiCacheKey,
    snapshot: WudApiSnapshot,
    *,
    pending_observations: Mapping[WudContainerIdentity, _PendingObservation]
    | None = None,
) -> None:
    with _cache_lock:
        current = _snapshot_cache.get(cache_key)
        if (
            current is not None
            and current.checked_monotonic > snapshot.checked_monotonic
        ):
            return
        _snapshot_cache[cache_key] = snapshot
        if pending_observations is not None:
            _pending_observation_cache[cache_key] = dict(pending_observations)


def _snapshot(
    state: WudApiState,
    *,
    available: bool,
    metadata_available: bool,
    checked_at: str,
    detail: str,
    checked_monotonic: float,
    metadata_checked: bool = False,
    containers: Sequence[WudApiContainer] = (),
    hidden_update_candidates: Sequence[WudApiContainer] = (),
    retryable_degraded_container_ids: Sequence[str] = (),
    degraded_container_count: int = 0,
    retained_update_count: int = 0,
    recovered_update_count: int = 0,
) -> WudApiSnapshot:
    return WudApiSnapshot(
        status=WudApiStatus(
            state=state,
            available=available,
            metadata_available=metadata_available,
            last_checked_at=checked_at,
            detail=detail,
        ),
        containers=tuple(containers),
        hidden_update_candidates=tuple(hidden_update_candidates),
        retryable_degraded_container_ids=tuple(retryable_degraded_container_ids),
        degraded_container_count=degraded_container_count,
        retained_update_count=retained_update_count,
        recovered_update_count=recovered_update_count,
        metadata_checked=metadata_checked,
        checked_monotonic=checked_monotonic,
    )


def _observation_store_source(settings: WebSettings) -> str:
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    normalized_base_url = _normalize_base_url(base_url)
    return web_wud_observation_store.source_key(normalized_base_url)


def _observation_database_exists(settings: WebSettings) -> bool:
    db_path = settings.config.db_path
    return str(db_path) != ":memory:" and db_path.is_file()


def _observation_persistence_enabled(settings: WebSettings) -> bool:
    # The configured client fingerprint is intentionally process-local so secret
    # values cannot become reusable hashes in SQLite. Without a stable, non-secret
    # principal identifier, authenticated observations must not cross restarts.
    return (
        _observation_database_exists(settings)
        and not settings.wud_api_client.configured
    )


def _safe_cache_error(settings: WebSettings, exc: BaseException) -> str:
    return _sanitize_detail(settings, str(exc))


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
