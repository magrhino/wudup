"""Read-only WUD API configuration diagnostics."""

from __future__ import annotations

import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .web_models import (
    WebSettings,
    WudApiAppDiagnostics,
    WudApiConfigurationDiagnostics,
    WudApiDiagnosticEndpointStatus,
    WudApiLogDiagnostics,
    WudApiRegistryDiagnostics,
    WudApiState,
    WudApiStoreDiagnostics,
    WudApiWatcherDiagnostics,
)
from .web_redaction import sensitive_mapping_key as _sensitive_mapping_key

APP_CONFIGURATION_LABEL = "app configuration"
LOG_CONFIGURATION_LABEL = "log configuration"
STORE_CONFIGURATION_LABEL = "store configuration"
WATCHER_CONFIGURATION_LABEL = "watcher configuration"
REGISTRY_CONFIGURATION_LABEL = "registry configuration"
WUD_API_CONFIG_ENDPOINTS = (
    ("app", "/api/app", APP_CONFIGURATION_LABEL),
    ("log", "/api/log", LOG_CONFIGURATION_LABEL),
    ("store", "/api/store", STORE_CONFIGURATION_LABEL),
    ("watchers", "/api/watchers", WATCHER_CONFIGURATION_LABEL),
    ("registries", "/api/registries", REGISTRY_CONFIGURATION_LABEL),
)

RequestJson = Callable[[str], object]
JoinUrl = Callable[[str, str], str]
SanitizeDetail = Callable[[WebSettings, str], str]


@dataclass(frozen=True)
class WudApiConfigurationSnapshot:
    diagnostics: WudApiConfigurationDiagnostics
    checked_monotonic: float = 0.0


@dataclass(frozen=True)
class WudApiConfigurationContext:
    settings: WebSettings
    normalized_base_url: str
    checked_at: str
    request_json: RequestJson
    join_url: JoinUrl
    sanitize_detail: SanitizeDetail


def configuration_diagnostics_cache_ttl(
    snapshot: WudApiConfigurationSnapshot,
    *,
    cache_ttl: float,
    degraded_retry_interval: float,
) -> float:
    if any(
        status.state in {"unavailable", "error"}
        for status in configuration_diagnostic_statuses(snapshot.diagnostics)
    ):
        return degraded_retry_interval
    return cache_ttl


def configuration_diagnostic_statuses(
    diagnostics: WudApiConfigurationDiagnostics,
) -> tuple[WudApiDiagnosticEndpointStatus, ...]:
    return (
        diagnostics.health,
        diagnostics.app.status,
        diagnostics.log.status,
        diagnostics.store.status,
        diagnostics.watchers_status,
        diagnostics.registries_status,
    )


def configuration_diagnostics_for_base_url_error(
    settings: WebSettings,
    *,
    error: ValueError,
    checked_at: str,
    checked_monotonic: float,
    sanitize_detail: SanitizeDetail,
) -> WudApiConfigurationSnapshot:
    health = _diagnostic_endpoint_status(
        "error",
        available=False,
        checked_at=checked_at,
        detail=f"invalid WUD API base URL: {error}",
        settings=settings,
        sanitize_detail=sanitize_detail,
    )
    return WudApiConfigurationSnapshot(
        diagnostics=_configuration_diagnostics_blocked_by_health(
            health,
            checked_at,
        ),
        checked_monotonic=checked_monotonic,
    )


def refresh_configuration_diagnostics(
    settings: WebSettings,
    *,
    normalized_base_url: str,
    checked_at: str,
    checked_monotonic: float,
    request_json: RequestJson,
    join_url: JoinUrl,
    sanitize_detail: SanitizeDetail,
) -> WudApiConfigurationSnapshot:
    context = WudApiConfigurationContext(
        settings=settings,
        normalized_base_url=normalized_base_url,
        checked_at=checked_at,
        request_json=request_json,
        join_url=join_url,
        sanitize_detail=sanitize_detail,
    )
    health = _wud_api_health_diagnostic(context)
    if health.state != "ready":
        return WudApiConfigurationSnapshot(
            diagnostics=_configuration_diagnostics_blocked_by_health(
                health,
                checked_at,
            ),
            checked_monotonic=checked_monotonic,
        )

    diagnostics = WudApiConfigurationDiagnostics(health=health)
    diagnostics.app = _fetch_app_diagnostics(context)
    diagnostics.log = _fetch_log_diagnostics(context)
    diagnostics.store = _fetch_store_diagnostics(context)
    diagnostics.watchers_status, diagnostics.watchers = _fetch_watchers_diagnostics(
        context,
    )
    (
        diagnostics.registries_status,
        diagnostics.registries,
    ) = _fetch_registries_diagnostics(context)
    return WudApiConfigurationSnapshot(
        diagnostics=diagnostics,
        checked_monotonic=checked_monotonic,
    )


def _wud_api_health_diagnostic(
    context: WudApiConfigurationContext,
) -> WudApiDiagnosticEndpointStatus:
    try:
        context.request_json(context.join_url(context.normalized_base_url, "/health"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return _diagnostic_endpoint_status(
                "auth_required",
                available=True,
                checked_at=context.checked_at,
                detail=_auth_required_detail(
                    context.settings,
                    "WUD API requires authentication",
                ),
                settings=context.settings,
                sanitize_detail=context.sanitize_detail,
            )
        return _diagnostic_endpoint_status(
            "unavailable",
            available=False,
            checked_at=context.checked_at,
            detail=f"WUD API health check returned HTTP {exc.code}",
            settings=context.settings,
            sanitize_detail=context.sanitize_detail,
        )
    except (OSError, ValueError) as exc:
        return _diagnostic_endpoint_status(
            "unavailable",
            available=False,
            checked_at=context.checked_at,
            detail=f"WUD API is unavailable: {exc}",
            settings=context.settings,
            sanitize_detail=context.sanitize_detail,
        )
    return _diagnostic_endpoint_status(
        "ready",
        available=True,
        checked_at=context.checked_at,
        detail="WUD API is reachable",
        settings=context.settings,
        sanitize_detail=context.sanitize_detail,
    )


def _configuration_diagnostics_blocked_by_health(
    health: WudApiDiagnosticEndpointStatus,
    checked_at: str,
) -> WudApiConfigurationDiagnostics:
    endpoint_statuses = {
        name: WudApiDiagnosticEndpointStatus(
            state=health.state,
            available=health.available,
            last_checked_at=checked_at,
            detail=(
                f"WUD API health check blocked {label}: {health.detail}"
                if health.detail
                else f"WUD API health check blocked {label}"
            ),
        )
        for name, _path, label in WUD_API_CONFIG_ENDPOINTS
    }
    return WudApiConfigurationDiagnostics(
        health=health,
        app=WudApiAppDiagnostics(status=endpoint_statuses["app"]),
        log=WudApiLogDiagnostics(status=endpoint_statuses["log"]),
        store=WudApiStoreDiagnostics(status=endpoint_statuses["store"]),
        watchers_status=endpoint_statuses["watchers"],
        registries_status=endpoint_statuses["registries"],
    )


def _fetch_app_diagnostics(
    context: WudApiConfigurationContext,
) -> WudApiAppDiagnostics:
    status, payload = _request_config_payload(
        context,
        "/api/app",
        APP_CONFIGURATION_LABEL,
    )
    if status.state != "ready":
        return WudApiAppDiagnostics(status=status)
    if not isinstance(payload, dict):
        return WudApiAppDiagnostics(
            status=_malformed_config_status(
                context,
                APP_CONFIGURATION_LABEL,
                "object",
            )
        )
    return WudApiAppDiagnostics(
        status=status,
        name=_sanitized_wud_config_string(context, payload.get("name"), key="name"),
        version=_sanitized_wud_config_string(
            context,
            payload.get("version"),
            key="version",
        ),
    )


def _fetch_log_diagnostics(
    context: WudApiConfigurationContext,
) -> WudApiLogDiagnostics:
    status, payload = _request_config_payload(
        context,
        "/api/log",
        LOG_CONFIGURATION_LABEL,
    )
    if status.state != "ready":
        return WudApiLogDiagnostics(status=status)
    if not isinstance(payload, dict):
        return WudApiLogDiagnostics(
            status=_malformed_config_status(
                context,
                LOG_CONFIGURATION_LABEL,
                "object",
            )
        )
    return WudApiLogDiagnostics(
        status=status,
        level=_sanitized_wud_config_string(context, payload.get("level"), key="level"),
    )


def _fetch_store_diagnostics(
    context: WudApiConfigurationContext,
) -> WudApiStoreDiagnostics:
    status, payload = _request_config_payload(
        context,
        "/api/store",
        STORE_CONFIGURATION_LABEL,
    )
    if status.state != "ready":
        return WudApiStoreDiagnostics(status=status)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("configuration"),
        dict,
    ):
        return WudApiStoreDiagnostics(
            status=_malformed_config_status(
                context,
                STORE_CONFIGURATION_LABEL,
                "configuration object",
            )
        )
    configuration = _sanitized_wud_config_mapping(
        context,
        payload["configuration"],
    )
    return WudApiStoreDiagnostics(
        status=status,
        path=_config_string(configuration, "path"),
        file=_config_string(configuration, "file"),
        configuration=configuration,
    )


def _fetch_watchers_diagnostics(
    context: WudApiConfigurationContext,
) -> tuple[WudApiDiagnosticEndpointStatus, list[WudApiWatcherDiagnostics]]:
    status, payload = _request_config_payload(
        context,
        "/api/watchers",
        WATCHER_CONFIGURATION_LABEL,
    )
    if status.state != "ready":
        return status, []
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        return (
            _malformed_config_status(
                context,
                WATCHER_CONFIGURATION_LABEL,
                "list of objects",
            ),
            [],
        )
    return status, [_parse_watcher_diagnostics(context, item) for item in payload]


def _fetch_registries_diagnostics(
    context: WudApiConfigurationContext,
) -> tuple[WudApiDiagnosticEndpointStatus, list[WudApiRegistryDiagnostics]]:
    status, payload = _request_config_payload(
        context,
        "/api/registries",
        REGISTRY_CONFIGURATION_LABEL,
    )
    if status.state != "ready":
        return status, []
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        return (
            _malformed_config_status(
                context,
                REGISTRY_CONFIGURATION_LABEL,
                "list of objects",
            ),
            [],
        )
    return status, [_parse_registry_diagnostics(context, item) for item in payload]


def _request_config_payload(
    context: WudApiConfigurationContext,
    path: str,
    label: str,
) -> tuple[WudApiDiagnosticEndpointStatus, object | None]:
    try:
        payload = context.request_json(context.join_url(context.normalized_base_url, path))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return (
                _diagnostic_endpoint_status(
                    "auth_required",
                    available=True,
                    checked_at=context.checked_at,
                    detail=_auth_required_detail(
                        context.settings,
                        f"WUD API {label} requires authentication",
                    ),
                    settings=context.settings,
                    sanitize_detail=context.sanitize_detail,
                ),
                None,
            )
        return (
            _diagnostic_endpoint_status(
                "error",
                available=True,
                checked_at=context.checked_at,
                detail=f"WUD API {label} returned HTTP {exc.code}",
                settings=context.settings,
                sanitize_detail=context.sanitize_detail,
            ),
            None,
        )
    except (OSError, ValueError) as exc:
        return (
            _diagnostic_endpoint_status(
                "error",
                available=True,
                checked_at=context.checked_at,
                detail=f"WUD API {label} is unavailable: {exc}",
                settings=context.settings,
                sanitize_detail=context.sanitize_detail,
            ),
            None,
        )
    return (
        _diagnostic_endpoint_status(
            "ready",
            available=True,
            checked_at=context.checked_at,
            detail=f"WUD API {label} available",
            settings=context.settings,
            sanitize_detail=context.sanitize_detail,
        ),
        payload,
    )


def _parse_watcher_diagnostics(
    context: WudApiConfigurationContext,
    raw: Mapping[str, object],
) -> WudApiWatcherDiagnostics:
    configuration = _sanitized_wud_config_mapping(
        context,
        raw.get("configuration"),
    )
    watch_by_default = configuration.get("watchbydefault")
    return WudApiWatcherDiagnostics(
        id=_sanitized_wud_config_string(context, raw.get("id"), key="id"),
        type=_sanitized_wud_config_string(context, raw.get("type"), key="type"),
        name=_sanitized_wud_config_string(context, raw.get("name"), key="name"),
        cron=_config_string(configuration, "cron"),
        watch_by_default=watch_by_default
        if isinstance(watch_by_default, bool)
        else None,
        configuration=configuration,
    )


def _parse_registry_diagnostics(
    context: WudApiConfigurationContext,
    raw: Mapping[str, object],
) -> WudApiRegistryDiagnostics:
    return WudApiRegistryDiagnostics(
        id=_sanitized_wud_config_string(context, raw.get("id"), key="id"),
        type=_sanitized_wud_config_string(context, raw.get("type"), key="type"),
        name=_sanitized_wud_config_string(context, raw.get("name"), key="name"),
        configuration=_sanitized_wud_config_mapping(
            context,
            raw.get("configuration"),
        ),
    )


def _malformed_config_status(
    context: WudApiConfigurationContext,
    label: str,
    expected: str,
) -> WudApiDiagnosticEndpointStatus:
    return _diagnostic_endpoint_status(
        "error",
        available=True,
        checked_at=context.checked_at,
        detail=f"WUD API {label} payload was not a {expected}",
        settings=context.settings,
        sanitize_detail=context.sanitize_detail,
    )


def _diagnostic_endpoint_status(
    state: WudApiState,
    *,
    available: bool,
    checked_at: str,
    detail: str,
    settings: WebSettings,
    sanitize_detail: SanitizeDetail,
) -> WudApiDiagnosticEndpointStatus:
    return WudApiDiagnosticEndpointStatus(
        state=state,
        available=available,
        last_checked_at=checked_at,
        detail=sanitize_detail(settings, detail),
    )


def _auth_required_detail(settings: WebSettings, fallback: str) -> str:
    if settings.wud_api_client.configured:
        return "configured WUD API credentials were rejected"
    return fallback


def _sanitized_wud_config_mapping(
    context: WudApiConfigurationContext,
    value: object,
) -> dict[str, Any]:
    sanitized = _sanitize_wud_configuration_value(context, value)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitized_wud_config_string(
    context: WudApiConfigurationContext,
    value: object,
    *,
    key: str,
) -> str:
    sanitized = _sanitize_wud_configuration_value(context, _string(value), key=key)
    return sanitized if isinstance(sanitized, str) else ""


def _sanitize_wud_configuration_value(
    context: WudApiConfigurationContext,
    value: object,
    *,
    key: str = "",
) -> Any:
    if _sensitive_mapping_key(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_wud_configuration_value(
                context,
                item_value,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_wud_configuration_value(context, item, key=key) for item in value
        ]
    if isinstance(value, str):
        return context.sanitize_detail(context.settings, value)
    return value

def _config_string(configuration: Mapping[str, object], key: str) -> str:
    value = configuration.get(key)
    return value if isinstance(value, str) else ""


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
