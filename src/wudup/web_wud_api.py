"""Stable WUD discovery API and metadata adapters.

Client requests, cache/watch state, and observation interpretation have separate
owners. Public imports stay here for WebUI consumers; mutable state does not.
"""

# Compatibility imports deliberately re-export these diagnostic model names.

from __future__ import annotations

import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from . import web_wud_transport
from .db import DatabaseError as DatabaseError
from .images import (
    tag_value_valid,
)
from .platforms import ImagePlatform as ImagePlatform
from .release_notes import OCI_SOURCE_LABEL, github_repo_from_source
from .web_auth import (
    WebConfigError as WebConfigError,
)
from .web_models import (
    PendingMetadataStatus as PendingMetadataStatus,
)
from .web_models import (
    ReleaseNotificationTrigger,
    WebSettings,
    WudApiObservationCounts,
    WudApiObservationDiagnostics,
    WudContainerMetadata,
)
from .web_models import (
    WudApiAppDiagnostics as WudApiAppDiagnostics,
)
from .web_models import (
    WudApiClientConfig as WudApiClientConfig,
)
from .web_models import WudApiConfigurationDiagnostics as WudApiConfigurationDiagnostics
from .web_models import (
    WudApiDiagnosticEndpointStatus as WudApiDiagnosticEndpointStatus,
)
from .web_models import (
    WudApiLogDiagnostics as WudApiLogDiagnostics,
)
from .web_models import WudApiObservationDiagnostic as WudApiObservationDiagnostic
from .web_models import (
    WudApiObservationOutcome as WudApiObservationOutcome,
)
from .web_models import (
    WudApiObservationReason as WudApiObservationReason,
)
from .web_models import (
    WudApiRegistryDiagnostics as WudApiRegistryDiagnostics,
)
from .web_models import WudApiState as WudApiState
from .web_models import WudApiStatus as WudApiStatus
from .web_models import (
    WudApiStoreDiagnostics as WudApiStoreDiagnostics,
)
from .web_models import (
    WudApiWatcherDiagnostics as WudApiWatcherDiagnostics,
)
from .web_wud_cache import (
    WUD_API_CACHE_TTL_SECONDS as WUD_API_CACHE_TTL_SECONDS,
)
from .web_wud_cache import (
    WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS as WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS,
)
from .web_wud_cache import (
    WUD_API_RATE_LIMIT_COOLDOWN_SECONDS as WUD_API_RATE_LIMIT_COOLDOWN_SECONDS,
)
from .web_wud_cache import (
    WUD_API_STARTUP_RETRY_INTERVAL_SECONDS as WUD_API_STARTUP_RETRY_INTERVAL_SECONDS,
)
from .web_wud_cache import (
    WUD_API_WATCH_BATCH_TIMEOUT_SECONDS as WUD_API_WATCH_BATCH_TIMEOUT_SECONDS,
)
from .web_wud_cache import (
    WUD_API_WATCH_TIMEOUT_SECONDS as WUD_API_WATCH_TIMEOUT_SECONDS,
)
from .web_wud_cache import (
    WudApiCacheKey as WudApiCacheKey,
)
from .web_wud_cache import (
    WudApiConfigurationSnapshot as WudApiConfigurationSnapshot,
)
from .web_wud_cache import (
    WudApiSnapshot as WudApiSnapshot,
)
from .web_wud_cache import (
    WudApiWatchCooldownKey as WudApiWatchCooldownKey,
)
from .web_wud_cache import (
    WudApiWatchResult as WudApiWatchResult,
)
from .web_wud_cache import (
    WudContainerIdentity as WudContainerIdentity,
)
from .web_wud_cache import (
    checkpoint_pending_observation_cache as checkpoint_pending_observation_cache,
)
from .web_wud_cache import (
    get_configuration_diagnostics as get_configuration_diagnostics,
)
from .web_wud_cache import (
    get_snapshot as get_snapshot,
)
from .web_wud_cache import (
    initialize_pending_observation_cache as initialize_pending_observation_cache,
)
from .web_wud_cache import (
    startup_probe as startup_probe,
)
from .web_wud_cache import (
    watch_all as watch_all,
)
from .web_wud_cache import (
    watch_containers as watch_containers,
)
from .web_wud_config import _auth_required_detail
from .web_wud_observations import WudApiContainer as WudApiContainer
from .web_wud_observations import (
    _match_container,
    _string,
)
from .web_wud_transport import (
    DEFAULT_WUD_API_BASE_URL as DEFAULT_WUD_API_BASE_URL,
)
from .web_wud_transport import (
    DEFAULT_WUD_API_STARTUP_WAIT_SECONDS as DEFAULT_WUD_API_STARTUP_WAIT_SECONDS,
)
from .web_wud_transport import (
    WUD_API_AUTH_BASIC_PASSWORD_ENV as WUD_API_AUTH_BASIC_PASSWORD_ENV,
)
from .web_wud_transport import (
    WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV as WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV,
)
from .web_wud_transport import (
    WUD_API_AUTH_BASIC_USER_ENV as WUD_API_AUTH_BASIC_USER_ENV,
)
from .web_wud_transport import (
    WUD_API_AUTH_BEARER_TOKEN_ENV as WUD_API_AUTH_BEARER_TOKEN_ENV,
)
from .web_wud_transport import (
    WUD_API_AUTH_BEARER_TOKEN_FILE_ENV as WUD_API_AUTH_BEARER_TOKEN_FILE_ENV,
)
from .web_wud_transport import (
    WUD_API_BASE_URL_ENV as WUD_API_BASE_URL_ENV,
)
from .web_wud_transport import (
    WUD_API_HEADERS_FILE_ENV as WUD_API_HEADERS_FILE_ENV,
)
from .web_wud_transport import (
    WUD_API_STARTUP_WAIT_SECONDS_ENV as WUD_API_STARTUP_WAIT_SECONDS_ENV,
)
from .web_wud_transport import (
    WUD_API_TIMEOUT_SECONDS as WUD_API_TIMEOUT_SECONDS,
)
from .web_wud_transport import (
    WUD_API_USER_AGENT as WUD_API_USER_AGENT,
)
from .web_wud_transport import (
    _join_url,
    _normalize_base_url,
    _sanitize_detail,
)
from .web_wud_transport import (
    configured_base_url as configured_base_url,
)
from .web_wud_transport import (
    configured_client_config as configured_client_config,
)
from .web_wud_transport import (
    configured_startup_wait_seconds as configured_startup_wait_seconds,
)
from .web_wud_transport import (
    format_startup_wait_seconds as format_startup_wait_seconds,
)
from .wud_file import WudTarget


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
        payload = web_wud_transport._request_json(
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
