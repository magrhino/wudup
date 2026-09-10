"""Best-effort WUD API discovery and metadata enrichment."""

# Compatibility imports deliberately re-export these diagnostic model names.

from __future__ import annotations

import base64
import json
import logging
import math
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import cast

from . import web_wud_config, web_wud_observation_store
from .db import DatabaseError
from .digest_verifier import DOCKER_HUB_REGISTRIES
from .images import (
    image_has_tag,
    image_matches_resolved_target,
    image_tag,
    normalize_digest,
    strip_digest,
    tag_value_valid,
)
from .platforms import ImagePlatform, parse_platform, platform_from_parts
from .release_notes import OCI_SOURCE_LABEL, github_repo_from_source
from .web_auth import (
    WebConfigError,
    _redact_sensitive_text,
    _redact_unknown_absolute_paths,
)
from .web_models import (
    PendingMetadataStatus,
    ReleaseNotificationTrigger,
    WebSettings,
    WudApiClientConfig,
    WudApiConfigurationDiagnostics,
    WudApiObservationCounts,
    WudApiObservationDiagnostic,
    WudApiObservationDiagnostics,
    WudApiObservationOutcome,
    WudApiObservationReason,
    WudApiState,
    WudApiStatus,
    WudContainerMetadata,
)
from .web_models import (
    WudApiAppDiagnostics as WudApiAppDiagnostics,
)
from .web_models import (
    WudApiDiagnosticEndpointStatus as WudApiDiagnosticEndpointStatus,
)
from .web_models import (
    WudApiLogDiagnostics as WudApiLogDiagnostics,
)
from .web_models import (
    WudApiRegistryDiagnostics as WudApiRegistryDiagnostics,
)
from .web_models import (
    WudApiStoreDiagnostics as WudApiStoreDiagnostics,
)
from .web_models import (
    WudApiWatcherDiagnostics as WudApiWatcherDiagnostics,
)
from .web_wud_config import _auth_required_detail
from .wud_file import WudTarget, parse_wud_text

DEFAULT_WUD_API_BASE_URL = "http://wud:3000"
WUD_API_BASE_URL_ENV = "WUD_API_BASE_URL"
WUD_API_STARTUP_WAIT_SECONDS_ENV = "WUD_API_STARTUP_WAIT_SECONDS"
WUD_API_AUTH_BEARER_TOKEN_ENV = "WUD_API_AUTH_BEARER_TOKEN"
WUD_API_AUTH_BEARER_TOKEN_FILE_ENV = "WUD_API_AUTH_BEARER_TOKEN_FILE"
WUD_API_AUTH_BASIC_USER_ENV = "WUD_API_AUTH_BASIC_USER"
WUD_API_AUTH_BASIC_PASSWORD_ENV = "WUD_API_AUTH_BASIC_PASSWORD"
WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV = "WUD_API_AUTH_BASIC_PASSWORD_FILE"
WUD_API_HEADERS_FILE_ENV = "WUD_API_HEADERS_FILE"
DEFAULT_WUD_API_STARTUP_WAIT_SECONDS = 0.0
WUD_API_TIMEOUT_SECONDS = 1.0
WUD_API_WATCH_TIMEOUT_SECONDS = 120.0
WUD_API_WATCH_BATCH_TIMEOUT_SECONDS = 120.0
WUD_API_STARTUP_RETRY_INTERVAL_SECONDS = 0.5
WUD_API_CACHE_TTL_SECONDS = 30.0
WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS = 5.0
WUD_API_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
WUD_API_USER_AGENT = "wudup-webui-wud-api/1.0"
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_429_RE = re.compile(r"(?:^|\D)429(?:\D|$)")
_HTTP_STATUS_DETAIL_RE = re.compile(
    r"\b(?:HTTP(?:\s+status)?|status(?:\s+code)?)\s*(?:[:=]\s*)?([1-5]\d{2})\b",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_UNSUPPORTED_REGISTRY_ERROR_PREFIX = "unsupported registry "
LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class WudApiContainer:
    id: str
    name: str
    display_name: str
    status: str
    watcher: str
    image: str
    local_tag: str
    local_digest: str
    remote_tag: str
    remote_digest: str
    update_kind: str
    semver_diff: str
    link: str
    error: str
    labels: Mapping[str, str] = field(default_factory=dict)
    platform: ImagePlatform | None = None
    local_image_id: str = ""
    metadata_status: PendingMetadataStatus = "fresh"

    def response(self) -> WudContainerMetadata:
        platform = self.platform
        return WudContainerMetadata(
            id=self.id,
            name=self.name,
            display_name=self.display_name,
            status=self.status,
            watcher=self.watcher,
            local_tag=self.local_tag,
            local_digest=self.local_digest,
            remote_tag=self.remote_tag,
            remote_digest=self.remote_digest,
            update_kind=self.update_kind,
            semver_diff=self.semver_diff,
            link=self.link,
            error=self.error,
            platform=platform.value if platform is not None else "",
            platform_os=platform.os if platform is not None else "",
            platform_architecture=platform.architecture if platform is not None else "",
            platform_variant=platform.variant if platform is not None else "",
        )


_PERSISTED_WUD_API_CONTAINER_FIELDS = frozenset(
    {
        "id",
        "name",
        "display_name",
        "status",
        "watcher",
        "image",
        "local_tag",
        "local_digest",
        "remote_tag",
        "remote_digest",
        "update_kind",
        "semver_diff",
        "link",
        "platform",
        "local_image_id",
    }
)
_EXCLUDED_WUD_API_CONTAINER_FIELDS = frozenset(
    {"error", "labels", "metadata_status"}
)
assert (
    _PERSISTED_WUD_API_CONTAINER_FIELDS | _EXCLUDED_WUD_API_CONTAINER_FIELDS
    == {item.name for item in fields(WudApiContainer)}
)
assert not (
    _PERSISTED_WUD_API_CONTAINER_FIELDS & _EXCLUDED_WUD_API_CONTAINER_FIELDS
)


@dataclass(frozen=True)
class WudApiSnapshot:
    status: WudApiStatus
    containers: tuple[WudApiContainer, ...] = ()
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
class _WudContainerObservation:
    container: WudApiContainer
    update_available: bool | None
    usable_scan_result: bool
    degraded: bool
    unsupported: bool


@dataclass(frozen=True)
class _PendingObservation:
    container: WudApiContainer
    observed_at: str


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


def configured_base_url(environ: Mapping[str, str]) -> str:
    return (
        environ.get(WUD_API_BASE_URL_ENV, "").strip() or DEFAULT_WUD_API_BASE_URL
    )


def configured_startup_wait_seconds(environ: Mapping[str, str]) -> float:
    raw_value = environ.get(WUD_API_STARTUP_WAIT_SECONDS_ENV, "").strip()
    if not raw_value:
        return DEFAULT_WUD_API_STARTUP_WAIT_SECONDS
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise WebConfigError(
            f"{WUD_API_STARTUP_WAIT_SECONDS_ENV} must be a number of seconds"
        ) from exc
    if not math.isfinite(value):
        raise WebConfigError(
            f"{WUD_API_STARTUP_WAIT_SECONDS_ENV} must be a finite number of seconds"
        )
    if value < 0:
        raise WebConfigError(
            f"{WUD_API_STARTUP_WAIT_SECONDS_ENV} must be zero or greater"
        )
    return value


def configured_client_config(environ: Mapping[str, str]) -> WudApiClientConfig:
    static_headers = _configured_static_headers(environ)
    auth_header, auth_secrets = _configured_authorization_header(environ)
    if auth_header and _has_header(static_headers, "Authorization"):
        raise WebConfigError(
            f"{WUD_API_HEADERS_FILE_ENV} must not define Authorization when WUD API "
            "bearer or basic auth is configured"
        )

    header_items = static_headers
    if auth_header:
        header_items = (*header_items, ("Authorization", auth_header))
    secret_values = tuple(
        value
        for value in (
            *auth_secrets,
            *(value for _name, value in static_headers),
            auth_header,
        )
        if value
    )
    return WudApiClientConfig(
        header_items=header_items,
        secret_values=secret_values,
        fingerprint=_client_config_fingerprint(header_items),
    )


def format_startup_wait_seconds(value: float) -> str:
    numeric_value = float(value)
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return str(numeric_value)


def _configured_authorization_header(
    environ: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    bearer_token = _configured_secret_value(
        environ,
        direct_name=WUD_API_AUTH_BEARER_TOKEN_ENV,
        file_name=WUD_API_AUTH_BEARER_TOKEN_FILE_ENV,
    )
    basic_user = environ.get(WUD_API_AUTH_BASIC_USER_ENV, "").strip()
    basic_password = _configured_secret_value(
        environ,
        direct_name=WUD_API_AUTH_BASIC_PASSWORD_ENV,
        file_name=WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV,
    )
    if bearer_token and (basic_user or basic_password):
        raise WebConfigError("WUD API bearer and basic auth cannot both be configured")
    if bool(basic_user) != bool(basic_password):
        raise WebConfigError(
            f"{WUD_API_AUTH_BASIC_USER_ENV} and "
            f"{WUD_API_AUTH_BASIC_PASSWORD_ENV}/"
            f"{WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV} must be set together"
        )
    if bearer_token:
        authorization = f"Bearer {bearer_token}"
        _validate_header_value("Authorization", authorization)
        return authorization, (bearer_token, authorization)
    if basic_user:
        user_password = f"{basic_user}:{basic_password}"
        token = base64.b64encode(user_password.encode("utf-8")).decode("ascii")
        authorization = f"Basic {token}"
        _validate_header_value("Authorization", authorization)
        return authorization, (basic_password, authorization)
    return "", ()


def _configured_secret_value(
    environ: Mapping[str, str],
    *,
    direct_name: str,
    file_name: str,
) -> str:
    direct_value = environ.get(direct_name, "").strip()
    file_value = environ.get(file_name, "").strip()
    if direct_value and file_value:
        raise WebConfigError(f"{direct_name} and {file_name} cannot both be set")
    if direct_value:
        return direct_value
    if not file_value:
        return ""
    return _read_secret_file(file_name, file_value)


def _read_secret_file(name: str, value: str) -> str:
    try:
        secret = Path(value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WebConfigError(f"{name} could not be read") from exc
    if not secret:
        raise WebConfigError(f"{name} must not be empty")
    return secret


def _configured_static_headers(
    environ: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    path = environ.get(WUD_API_HEADERS_FILE_ENV, "").strip()
    if not path:
        return ()
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise WebConfigError(f"{WUD_API_HEADERS_FILE_ENV} could not be read") from exc
    if not raw.strip():
        raise WebConfigError(f"{WUD_API_HEADERS_FILE_ENV} must contain a JSON object")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebConfigError(
            f"{WUD_API_HEADERS_FILE_ENV} must contain a JSON object"
        ) from exc
    if not isinstance(payload, dict):
        raise WebConfigError(f"{WUD_API_HEADERS_FILE_ENV} must contain a JSON object")

    headers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_value in payload.items():
        name = str(raw_name).strip()
        _validate_header_name(name)
        normalized = name.lower()
        if normalized in seen:
            raise WebConfigError(
                f"{WUD_API_HEADERS_FILE_ENV} must not define duplicate headers"
            )
        seen.add(normalized)
        if not isinstance(raw_value, str):
            raise WebConfigError(
                f"{WUD_API_HEADERS_FILE_ENV} values must be strings"
            )
        _validate_header_value(name, raw_value)
        headers.append((name, raw_value))
    return tuple(headers)


def _validate_header_name(name: str) -> None:
    if not name or not _HEADER_NAME_RE.fullmatch(name):
        raise WebConfigError(f"{WUD_API_HEADERS_FILE_ENV} contains an invalid header")


def _validate_header_value(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise WebConfigError(f"WUD API header {name} must not contain newlines")


def _has_header(headers: Sequence[tuple[str, str]], name: str) -> bool:
    normalized = name.lower()
    return any(header_name.lower() == normalized for header_name, _value in headers)


def _client_config_fingerprint(header_items: Sequence[tuple[str, str]]) -> str:
    if not header_items:
        return ""
    # Partition per configured client without deriving a reusable digest from secrets.
    return secrets.token_hex(16)


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


def get_observation_diagnostics(
    settings: WebSettings,
    *,
    snapshot: WudApiSnapshot | None = None,
) -> WudApiObservationDiagnostics:
    active_snapshot = snapshot or get_snapshot(settings, include_containers=True)
    return WudApiObservationDiagnostics(
        counts=WudApiObservationCounts(
            available=len(active_snapshot.containers),
            degraded=active_snapshot.degraded_container_count,
            retained=active_snapshot.retained_update_count,
            recovered=active_snapshot.recovered_update_count,
            unresolved=max(
                0,
                active_snapshot.degraded_container_count
                - active_snapshot.retained_update_count
                - active_snapshot.recovered_update_count,
            ),
            unsupported_ignored=active_snapshot.unsupported_container_count,
        ),
        items=list(active_snapshot.observation_diagnostics),
    )


def _count_phrase(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _degraded_observation_detail(
    degraded_container_count: int,
    retained_update_count: int,
    recovered_update_count: int,
) -> str:
    detail = (
        " The last WUD update check failed for "
        f"{_count_phrase(degraded_container_count, 'container')}."
    )
    if retained_update_count:
        retained = _count_phrase(retained_update_count, "update")
        detail += (
            f" {retained} {'uses the result' if retained_update_count == 1 else 'use results'} "
            "from the last successful WUD check."
        )
    if recovered_update_count:
        recovered = _count_phrase(recovered_update_count, "update")
        detail += (
            f" {recovered} {'was' if recovered_update_count == 1 else 'were'} "
            "recovered from the pending file."
        )
    unresolved_count = max(
        0,
        degraded_container_count
        - retained_update_count
        - recovered_update_count,
    )
    if unresolved_count:
        detail += (
            " Update status is unknown for "
            f"{_count_phrase(unresolved_count, 'container')}."
        )
    return detail


def _observation_status_detail(
    available_update_count: int,
    degraded_container_count: int,
    retained_update_count: int,
    recovered_update_count: int,
    unsupported_container_count: int,
) -> str:
    updates = _count_phrase(available_update_count, "update")
    detail = f"{updates} {'is' if available_update_count == 1 else 'are'} available."
    if degraded_container_count:
        detail += _degraded_observation_detail(
            degraded_container_count,
            retained_update_count,
            recovered_update_count,
        )
    if unsupported_container_count:
        containers = _count_phrase(unsupported_container_count, "container")
        registry = "its registry is" if unsupported_container_count == 1 else "their registries are"
        detail += f" WUD skipped {containers} because {registry} unsupported."
    return detail


def _snapshot_cache_ttl(snapshot: WudApiSnapshot) -> float:
    if snapshot.status.state in {"unavailable", "error"} or snapshot.degraded_container_count:
        return WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS
    return WUD_API_CACHE_TTL_SECONDS


def _cache_key(settings: WebSettings, base_url: str) -> WudApiCacheKey:
    return (base_url, settings.wud_api_client.fingerprint)


def metadata_by_target(
    settings: WebSettings,
    targets: Sequence[WudTarget],
    *,
    snapshot: WudApiSnapshot | None = None,
) -> dict[int, WudApiContainer]:
    active_snapshot = snapshot or get_snapshot(settings, include_containers=True)
    if not active_snapshot.status.metadata_available:
        return {}
    result: dict[int, WudApiContainer] = {}
    for target in targets:
        match = _match_container(target, active_snapshot.containers)
        if match is not None:
            result[target.line_no] = match
    return result


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


def metadata_response_by_line(
    metadata: Mapping[int, WudApiContainer],
) -> dict[int, WudContainerMetadata]:
    return {line_no: item.response() for line_no, item in metadata.items()}


def source_resolver_from_metadata(
    metadata: Mapping[int, WudApiContainer],
) -> Callable[[WudTarget], str]:
    def resolve(target: WudTarget) -> str:
        item = metadata.get(target.line_no)
        if item is None:
            return ""
        source_label = item.labels.get(OCI_SOURCE_LABEL, "")
        if github_repo_from_source(source_label):
            return source_label
        if github_repo_from_source(item.link):
            return item.link
        return source_label

    return resolve


def target_tag_resolver_from_metadata(
    metadata: Mapping[int, WudApiContainer],
) -> Callable[[WudTarget], str]:
    def resolve(target: WudTarget) -> str:
        item = metadata.get(target.line_no)
        if item is None or not item.remote_tag or not tag_value_valid(item.remote_tag):
            return ""
        return item.remote_tag

    return resolve


def container_triggers(
    settings: WebSettings,
    container_id: str,
) -> tuple[list[ReleaseNotificationTrigger], str]:
    if not container_id:
        return [], ""
    base_url = settings.wud_api_base_url or DEFAULT_WUD_API_BASE_URL
    try:
        normalized_base_url = _normalize_base_url(base_url)
    except ValueError as exc:
        return [], _sanitize_detail(settings, f"invalid WUD API base URL: {exc}")
    path = f"/api/containers/{urllib.parse.quote(container_id, safe='')}/triggers"
    try:
        payload = _request_json(
            _join_url(normalized_base_url, path),
            settings.wud_api_client,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return [], _auth_required_detail(
                settings,
                "WUD API trigger metadata requires authentication",
            )
        return [], _sanitize_detail(
            settings,
            f"WUD API trigger metadata returned HTTP {exc.code}",
        )
    except (OSError, ValueError) as exc:
        return [], _sanitize_detail(
            settings,
            f"WUD API trigger metadata is unavailable: {exc}",
        )
    if not isinstance(payload, list):
        return [], "WUD API trigger metadata payload was not a list"
    trigger_payloads = cast(list[object], payload)
    return [
        _parse_trigger(raw)
        for raw in trigger_payloads
        if isinstance(raw, Mapping)
    ], ""


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
        _request_json(_join_url(normalized_base_url, "/health"), settings.wud_api_client)
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
        payload = _request_json(
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
        unsupported_container_count=unsupported_container_count,
        observation_diagnostics=tuple(observation_diagnostics),
    )
    _store_snapshot(
        cache_key,
        snapshot,
        pending_observations=pending_observations,
    )
    return snapshot


def _parse_trigger(raw: Mapping[str, object]) -> ReleaseNotificationTrigger:
    trigger_type = _string(raw.get("type") or raw.get("kind"))
    name = _string(raw.get("name"))
    trigger_id = _string(raw.get("id"))
    if not trigger_id:
        if trigger_type and name:
            trigger_id = f"{trigger_type}.{name}"
        else:
            trigger_id = name or trigger_type
    return ReleaseNotificationTrigger(
        id=trigger_id,
        type=trigger_type,
        name=name,
    )


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
        request_json=lambda url: _request_json(url, settings.wud_api_client),
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
            payload = _post_json(
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


def _request_json(
    url: str,
    client_config: WudApiClientConfig | None = None,
) -> object:
    return _request_json_with_method(url, method="GET", client_config=client_config)


def _post_json(
    url: str,
    client_config: WudApiClientConfig | None = None,
    *,
    timeout: float = WUD_API_TIMEOUT_SECONDS,
) -> object:
    return _request_json_with_method(
        url,
        method="POST",
        client_config=client_config,
        timeout=timeout,
    )


def _request_json_with_method(
    url: str,
    *,
    method: str,
    client_config: WudApiClientConfig | None = None,
    timeout: float = WUD_API_TIMEOUT_SECONDS,
) -> object:
    request = urllib.request.Request(
        url,
        method=method,
        headers=_request_headers(client_config),
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _request_headers(
    client_config: WudApiClientConfig | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": WUD_API_USER_AGENT,
    }
    if client_config is not None:
        headers.update(dict(client_config.header_items))
    return headers


def _normalize_base_url(value: str) -> str:
    stripped = value.strip()
    parsed = urllib.parse.urlsplit(stripped)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("host is required")
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url}{path}"


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


def _stored_observation(container: WudApiContainer) -> Mapping[str, object]:
    platform = container.platform
    stored = {
        "id": container.id,
        "name": container.name,
        "display_name": container.display_name,
        "status": container.status,
        "watcher": container.watcher,
        "image": container.image,
        "local_tag": container.local_tag,
        "local_digest": container.local_digest,
        "remote_tag": container.remote_tag,
        "remote_digest": container.remote_digest,
        "update_kind": container.update_kind,
        "semver_diff": container.semver_diff,
        "link": container.link,
        "platform": platform.value if platform is not None else "",
        "local_image_id": container.local_image_id,
    }
    assert stored.keys() == _PERSISTED_WUD_API_CONTAINER_FIELDS
    return stored


def _container_from_stored_observation(
    raw: Mapping[str, object],
) -> WudApiContainer | None:
    platform_value = _string(raw.get("platform"))
    platform = parse_platform(platform_value) if platform_value else None
    container = WudApiContainer(
        id=_string(raw.get("id")),
        name=_string(raw.get("name")),
        display_name=_string(raw.get("display_name")),
        status=_string(raw.get("status")),
        watcher=_string(raw.get("watcher")),
        image=_string(raw.get("image")),
        local_tag=_string(raw.get("local_tag")),
        local_digest=_string(raw.get("local_digest")),
        remote_tag=_string(raw.get("remote_tag")),
        remote_digest=_string(raw.get("remote_digest")),
        update_kind=_string(raw.get("update_kind")),
        semver_diff=_string(raw.get("semver_diff")),
        link=_string(raw.get("link")),
        error="",
        platform=platform,
        local_image_id=_string(raw.get("local_image_id")),
    )
    if (
        _container_identity(container) is None
        or not (container.remote_tag or container.remote_digest)
    ):
        return None
    return container


def _safe_cache_error(settings: WebSettings, exc: BaseException) -> str:
    return _sanitize_detail(settings, str(exc))


def _append_pending_observation(
    container: WudApiContainer,
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
    *,
    observed_at: str,
) -> None:
    containers.append(container)
    identity = _container_identity(container)
    if identity is not None:
        pending_observations[identity] = _PendingObservation(
            container=container,
            observed_at=observed_at,
        )


def _retain_previous_observation(
    container: WudApiContainer,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
) -> bool:
    match = _previous_observation_for_container(container, previous)
    if match is None:
        return False
    identity, previous_observation = match

    retained = previous_observation.container
    retained = replace(
        retained,
        display_name=container.display_name,
        status=container.status,
        error=container.error or "WUD update result is unavailable",
        labels=container.labels,
        metadata_status="retained",
    )
    containers.append(retained)
    pending_observations[identity] = _PendingObservation(
        container=retained,
        observed_at=previous_observation.observed_at,
    )
    return True


def _previous_observation_for_container(
    container: WudApiContainer,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
) -> tuple[WudContainerIdentity, _PendingObservation] | None:
    identity = _container_identity(container)
    if identity is None:
        return None
    exact = previous.get(identity)
    if exact is not None:
        return identity, exact
    if container.local_digest or not container.local_image_id:
        return None
    matches = [
        (previous_identity, observation)
        for previous_identity, observation in previous.items()
        if previous_identity[:5] == identity[:5]
        and previous_identity[6] == identity[6]
    ]
    return matches[0] if len(matches) == 1 else None


def _pending_file_recovery_targets(settings: WebSettings) -> tuple[WudTarget, ...]:
    if not settings.legacy_scripts_enabled:
        return ()
    try:
        text = settings.config.wud_out_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    return parse_wud_text(text).targets


def _recover_pending_file_observation(
    container: WudApiContainer,
    targets: Sequence[WudTarget],
) -> WudApiContainer | None:
    for target in targets:
        if not _pending_file_target_is_recoverable(container, target):
            continue

        return cast(
            WudApiContainer,
            replace(
                container,
                remote_tag=target.desired_tag,
                remote_digest=target.digest,
                update_kind="tag" if target.desired_tag else "digest",
                error=(
                    container.error
                    or "WUD update result is unavailable; pending update recovered "
                    "from WUD_OUT_FILE"
                ),
                metadata_status="recovered",
            ),
        )
    return None


def _pending_file_target_is_recoverable(
    container: WudApiContainer,
    target: WudTarget,
) -> bool:
    if not (target.desired_tag or target.digest):
        return False
    if not _recovery_container_matches_target(container, target):
        return False
    if target.platform is not None and target.platform != container.platform:
        return False
    if target.desired_tag:
        return target.desired_tag != container.local_tag
    return target.digest != normalize_digest(container.local_digest)


def _reconcile_degraded_observation(
    observation: _WudContainerObservation,
    settings: WebSettings,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
    recovery_targets: tuple[WudTarget, ...] | None,
) -> tuple[
    tuple[WudTarget, ...] | None,
    int,
    int,
    int,
    int,
    WudApiObservationOutcome,
]:
    container = observation.container
    if _retain_previous_observation(
        container,
        previous,
        containers,
        pending_observations,
    ):
        return recovery_targets, 1, 1, 0, 0, "retained"

    if observation.unsupported:
        return recovery_targets, 0, 0, 0, 1, "unsupported_ignored"
    if recovery_targets is None:
        recovery_targets = _pending_file_recovery_targets(settings)
    recovered = _recover_pending_file_observation(container, recovery_targets)
    if recovered is not None:
        containers.append(recovered)
        return recovery_targets, 1, 0, 1, 0, "recovered"
    return recovery_targets, 1, 0, 0, 0, "unresolved"


def _record_retryable_degraded_container(
    observation: _WudContainerObservation,
    seen_container_ids: set[str],
    container_ids: list[str],
) -> None:
    container_id = observation.container.id
    if (
        not observation.degraded
        or not container_id
        or container_id in seen_container_ids
    ):
        return
    seen_container_ids.add(container_id)
    container_ids.append(container_id)


def _reconcile_container_observations(
    payload: Sequence[object],
    settings: WebSettings,
    *,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    observed_at: str,
) -> tuple[
    tuple[WudApiContainer, ...],
    tuple[WudApiContainer, ...],
    tuple[WudApiContainer, ...],
    tuple[str, ...],
    int,
    int,
    int,
    int,
    tuple[WudApiObservationDiagnostic, ...],
    Mapping[WudContainerIdentity, _PendingObservation],
]:
    containers: list[WudApiContainer] = []
    unresolved_containers: list[WudApiContainer] = []
    hidden_update_candidates: list[WudApiContainer] = []
    retryable_degraded_container_ids: list[str] = []
    seen_retryable_container_ids: set[str] = set()
    pending_observations: dict[WudContainerIdentity, _PendingObservation] = {}
    degraded_container_count = 0
    retained_update_count = 0
    recovered_update_count = 0
    unsupported_container_count = 0
    observation_diagnostics: list[WudApiObservationDiagnostic] = []
    recovery_targets: tuple[WudTarget, ...] | None = None

    for raw in payload:
        observation = _parse_container_observation(raw, settings)
        if observation is None:
            degraded_container_count += 1
            observation_diagnostics.append(
                _malformed_observation_diagnostic(raw, settings)
            )
            continue

        container = observation.container
        _record_retryable_degraded_container(
            observation,
            seen_retryable_container_ids,
            retryable_degraded_container_ids,
        )
        if observation.unsupported or observation.degraded:
            (
                recovery_targets,
                degraded_delta,
                retained_delta,
                recovered_delta,
                unsupported_delta,
                outcome,
            ) = _reconcile_degraded_observation(
                observation,
                settings,
                previous,
                containers,
                pending_observations,
                recovery_targets,
            )
            degraded_container_count += degraded_delta
            retained_update_count += retained_delta
            recovered_update_count += recovered_delta
            unsupported_container_count += unsupported_delta
            observation_diagnostics.append(
                _observation_diagnostic(observation, outcome, settings)
            )
            if outcome == "unresolved":
                unresolved_containers.append(container)
            continue

        if observation.update_available:
            _append_pending_observation(
                container,
                containers,
                pending_observations,
                observed_at=observed_at,
            )
            continue

        update_kind = _object(cast(Mapping[str, object], raw).get("updateKind"))
        if _hidden_update_kind_has_delta(update_kind):
            hidden_update_candidates.append(container)

    return (
        tuple(containers),
        tuple(unresolved_containers),
        tuple(hidden_update_candidates),
        tuple(retryable_degraded_container_ids),
        degraded_container_count,
        retained_update_count,
        recovered_update_count,
        unsupported_container_count,
        tuple(observation_diagnostics),
        pending_observations,
    )


def _parse_container_observation(
    raw: object,
    settings: WebSettings,
) -> _WudContainerObservation | None:
    if not isinstance(raw, dict):
        return None
    container = _parse_container_payload(raw, settings)
    if container is None:
        return None
    update_available = raw.get("updateAvailable")
    usable_scan_result = _has_usable_scan_result(raw, container)
    unsupported = (
        update_available is False
        and not usable_scan_result
        and container.error.casefold().startswith(_UNSUPPORTED_REGISTRY_ERROR_PREFIX)
    )
    return _WudContainerObservation(
        container=container,
        update_available=(
            update_available if isinstance(update_available, bool) else None
        ),
        usable_scan_result=usable_scan_result,
        unsupported=unsupported,
        degraded=(
            not unsupported
            and (
                not isinstance(update_available, bool)
                or bool(container.error)
                or not usable_scan_result
            )
        ),
    )


def _malformed_observation_diagnostic(
    raw: object,
    settings: WebSettings,
) -> WudApiObservationDiagnostic:
    if not isinstance(raw, dict):
        return WudApiObservationDiagnostic(
            outcome="unresolved",
            reason_code="malformed_observation",
        )

    image = _object(raw.get("image"))
    update_available = raw.get("updateAvailable")
    registry = _registry_host(_path_string(image, "registry", "url"))
    return WudApiObservationDiagnostic(
        outcome="unresolved",
        reason_code="missing_image",
        container_id=_diagnostic_text(settings, _string(raw.get("id"))),
        name=_diagnostic_text(settings, _string(raw.get("name"))),
        registry=_diagnostic_text(settings, registry),
        watcher=_diagnostic_text(settings, _string(raw.get("watcher"))),
        update_available=(
            update_available if isinstance(update_available, bool) else None
        ),
        error=_diagnostic_error_text(settings, _error_message(raw.get("error"))),
    )


def _observation_diagnostic(
    observation: _WudContainerObservation,
    outcome: WudApiObservationOutcome,
    settings: WebSettings,
) -> WudApiObservationDiagnostic:
    container = observation.container
    return WudApiObservationDiagnostic(
        outcome=outcome,
        reason_code=_observation_reason_code(observation),
        container_id=_diagnostic_text(settings, container.id),
        name=_diagnostic_text(settings, container.name),
        image=_diagnostic_text(settings, container.image),
        registry=_diagnostic_text(
            settings,
            _image_registry_key(container.image) or "docker.io",
        ),
        watcher=_diagnostic_text(settings, container.watcher),
        update_available=observation.update_available,
        usable_result=observation.usable_scan_result,
        retryable=observation.degraded and bool(container.id),
        error=_diagnostic_error_text(settings, container.error),
    )


def _observation_reason_code(
    observation: _WudContainerObservation,
) -> WudApiObservationReason:
    if observation.unsupported:
        return "unsupported_registry"
    if observation.update_available is None:
        return "invalid_update_flag"
    if observation.container.error:
        return "reported_error"
    return "missing_scan_result"


def _diagnostic_text(settings: WebSettings, value: str) -> str:
    return _sanitize_detail(settings, value)


def _diagnostic_error_text(settings: WebSettings, value: str) -> str:
    sanitized = _sanitize_detail(settings, value)
    if not sanitized:
        return ""
    if sanitized.casefold().startswith(_UNSUPPORTED_REGISTRY_ERROR_PREFIX):
        return "Unsupported registry"
    status = _HTTP_STATUS_DETAIL_RE.search(sanitized)
    if status is not None:
        if status.group(1) == "429":
            return (
                "The registry rate-limited the last WUD update check "
                "(HTTP 429: too many requests). Wait before rescanning; "
                "a successful check clears this error."
            )
        return (
            "The last WUD update check failed: "
            f"registry request returned HTTP status {status.group(1)}. "
            "Check WUD logs for details."
        )
    return "The last WUD update check failed. Check WUD logs for details."


def _has_usable_scan_result(
    raw: Mapping[str, object],
    container: WudApiContainer,
) -> bool:
    result = raw.get("result")
    if not isinstance(result, dict):
        return False
    if raw.get("updateAvailable") is True:
        return bool(container.remote_tag or container.remote_digest)
    return bool(
        _string(result.get("tag"))
        or _string(result.get("digest"))
        or _string(result.get("created"))
    )


def _hidden_update_kind_has_delta(update_kind: Mapping[str, object]) -> bool:
    if _string(update_kind.get("kind")) not in {"tag", "digest"}:
        return False
    local_value = _string(update_kind.get("localValue"))
    remote_value = _string(update_kind.get("remoteValue"))
    return bool(local_value and remote_value and local_value != remote_value)


def _container_identity(
    container: WudApiContainer,
) -> WudContainerIdentity | None:
    if not container.id or not container.image:
        return None
    platform = container.platform.value if container.platform is not None else ""
    return (
        container.watcher,
        container.id,
        container.name,
        container.image,
        container.local_image_id,
        container.local_digest,
        platform,
    )


def _parse_container_payload(
    raw: Mapping[str, object],
    settings: WebSettings,
) -> WudApiContainer | None:
    image = _object(raw.get("image"))
    result = _object(raw.get("result"))
    update_kind = _object(raw.get("updateKind"))
    image_ref = _image_ref(image)
    if not image_ref:
        return None
    labels = {
        **_string_mapping(image.get("labels")),
        **_string_mapping(raw.get("labels")),
    }
    return WudApiContainer(
        id=_string(raw.get("id")),
        name=_string(raw.get("name")),
        display_name=_string(raw.get("displayName")),
        status=_string(raw.get("status")),
        watcher=_string(raw.get("watcher")),
        image=image_ref,
        local_tag=_path_string(image, "tag", "value"),
        local_digest=_digest_from_value(_path_string(image, "digest", "value")),
        remote_tag=_remote_tag(result, update_kind),
        remote_digest=_remote_digest(result, update_kind),
        update_kind=_string(update_kind.get("kind")),
        semver_diff=_string(update_kind.get("semverDiff")),
        link=_string(result.get("link") or raw.get("link")),
        error=_sanitize_detail(settings, _error_message(raw.get("error"))),
        labels=labels,
        platform=_container_platform(raw, image),
        local_image_id=_string(image.get("id")),
    )


def _container_platform(
    raw: Mapping[str, object],
    image: Mapping[str, object],
) -> ImagePlatform | None:
    for value in (
        _string(image.get("platform")),
        _string(raw.get("platform")),
        _string(raw.get("image_platform")),
        _string(raw.get("imagePlatform")),
    ):
        platform = parse_platform(value) if value else None
        if platform is not None:
            return platform

    for source in (
        _object(image.get("platform")),
        _object(raw.get("platform")),
        image,
        raw,
        _object(raw.get("container_json")),
        _object(raw.get("containerJson")),
    ):
        platform = platform_from_parts(
            _string(source.get("os") or source.get("image_os") or source.get("imageOs")),
            _string(
                source.get("architecture")
                or source.get("arch")
                or source.get("image_architecture")
                or source.get("imageArchitecture")
            ),
            _string(source.get("variant") or source.get("image_variant") or source.get("imageVariant")),
        )
        if platform is not None:
            return platform
    return None


def _match_container(
    target: WudTarget,
    containers: Sequence[WudApiContainer],
) -> WudApiContainer | None:
    for container in containers:
        if _container_matches_target(container, target):
            return container
    return None


def _container_matches_target(container: WudApiContainer, target: WudTarget) -> bool:
    if target.first in {container.name, container.display_name, container.id}:
        return True
    if not container.image:
        return False
    allow_repo = target.allow_repo or not image_has_tag(target.first)
    return image_matches_resolved_target(container.image, target.first, allow_repo)


def _recovery_container_matches_target(
    container: WudApiContainer,
    target: WudTarget,
) -> bool:
    if target.first in {container.name, container.display_name, container.id}:
        return True
    if not container.image:
        return False
    if _image_registry_key(container.image) != _image_registry_key(target.first):
        return False
    allow_repo = target.allow_repo or not image_has_tag(target.first)
    return image_matches_resolved_target(container.image, target.first, allow_repo)


def _image_registry_key(image: str) -> str:
    if not _image_has_registry(image):
        return ""
    registry = strip_digest(image).partition("/")[0].lower()
    return "" if registry in DOCKER_HUB_REGISTRIES else registry


def _image_ref(image: Mapping[str, object]) -> str:
    name = _string(image.get("name"))
    tag = _path_string(image, "tag", "value")
    if not name:
        return ""
    registry = _registry_host(_path_string(image, "registry", "url"))
    if registry and not _image_has_registry(name):
        name = f"{registry}/{name}"
    if tag and not image_has_tag(name):
        return f"{name}:{tag}"
    return name


def _registry_host(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value if "://" in value else f"//{value}")
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).split("@")[-1].lower()
    return "" if host in DOCKER_HUB_REGISTRIES else host


def _image_has_registry(image: str) -> bool:
    left, sep, _rest = strip_digest(image).partition("/")
    return bool(sep and ("." in left or ":" in left or left == "localhost"))


def _remote_tag(
    result: Mapping[str, object],
    update_kind: Mapping[str, object],
) -> str:
    result_tag = _string(result.get("tag"))
    if tag_value_valid(result_tag):
        return result_tag
    if _string(update_kind.get("kind")) != "tag":
        return ""
    return _tag_from_remote_value(_string(update_kind.get("remoteValue")))


def _tag_from_remote_value(value: str) -> str:
    if not value:
        return ""
    candidate = value.split("@sha256:", 1)[0]
    if image_has_tag(candidate):
        candidate = image_tag(candidate)
    if tag_value_valid(candidate):
        return candidate
    return ""


def _remote_digest(
    result: Mapping[str, object],
    update_kind: Mapping[str, object],
) -> str:
    result_digest = _digest_from_value(_string(result.get("digest")))
    if result_digest:
        return result_digest
    if _string(update_kind.get("kind")) not in {"digest", "tag"}:
        return ""
    return _digest_from_value(_string(update_kind.get("remoteValue")))


def _digest_from_value(value: str) -> str:
    if not value:
        return ""
    if "@sha256:" in value or value.startswith("sha256:"):
        return normalize_digest(value)
    return ""


def _object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _path_string(value: Mapping[str, object], *parts: str) -> str:
    current: object = value
    for part in parts:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return _string(current)


def _string_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _error_message(value: object) -> str:
    if isinstance(value, dict):
        return _string(value.get("message") or value.get("error"))
    return _string(value)


def _sanitize_detail(settings: WebSettings, value: str) -> str:
    if not value:
        return ""
    sanitized = _scrub_http_url_secrets(value)
    sanitized = _redact_sensitive_text(settings, sanitized)
    return _redact_unknown_absolute_paths(sanitized)


def _scrub_http_url_secrets(value: str) -> str:
    return _HTTP_URL_RE.sub(_scrub_http_url_match, value)


def _scrub_http_url_match(match: re.Match[str]) -> str:
    candidate = match.group(0)
    trailing = ""
    while candidate and candidate[-1] in ".,;!?)]}":
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    try:
        parsed = urllib.parse.urlsplit(candidate)
        if (
            parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        ):
            return f"{candidate}{trailing}"
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("URL host is unavailable")
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
    except ValueError:
        scheme = candidate.partition("://")[0]
        return f"{scheme}://<redacted>{trailing}"
    netloc = f"{host}:{port}" if port is not None else host
    return (
        urllib.parse.urlunsplit(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                "<redacted>" if parsed.query else "",
                "<redacted>" if parsed.fragment else "",
            )
        )
        + trailing
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
