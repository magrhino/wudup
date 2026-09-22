"""WebUI health, readiness, and doctor route behavior."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

from fastapi import Request, Response

from . import __version__, web_wud_api
from .config import (
    COMPOSE_IGNORE_PATHS_ENV,
    DIGEST_PIN_UPDATES_ENV,
    UpdaterConfig,
    format_compose_ignore_paths,
)
from .doctor import (
    Doctor,
    DoctorConfigError,
)
from .doctor import (
    DoctorCheck as DoctorDataCheck,
)
from .doctor import (
    DoctorOptions as DoctorDataOptions,
)
from .doctor import (
    DoctorResult as DoctorDataResult,
)
from .doctor import (
    DoctorSuggestion as DoctorDataSuggestion,
)
from .doctor import (
    options_from_namespace as doctor_options_from_namespace,
)
from .web_auth import (
    WebConfigError,
    _parse_public_origin,
    _secure_cookie,
)
from .web_database import database_ready
from .web_models import (
    DoctorCheckResponse,
    DoctorCheckStatus,
    DoctorResponse,
    DoctorSuggestionResponse,
    HealthResponse,
    ReadyResponse,
    WebSettings,
    WudApiDiagnosticEndpointStatus,
    WudApiRegistryDiagnostics,
    WudApiWatcherDiagnostics,
)
from .web_redaction import redact_sensitive_text as _redact_sensitive_text
from .web_request_context import effective_origin as _effective_origin
from .web_request_context import host_from_origin as _host_from_origin
from .web_request_context import raw_client_is_loopback as _raw_client_is_loopback
from .web_request_context import request_settings as _settings
from .web_request_context import trusted_forwarded_origin as _trusted_forwarded_origin

READINESS_DOCKER_ENDPOINT_CODES = frozenset({"docker-endpoint", "docker-socket"})
READINESS_REQUIRED_CODES = frozenset(
    {
        "docker-daemon-version",
        "docker-daemon-info",
        "docker-container-listing",
        "wud-out-file-directory",
        "wud-out-file",
        "webui-database",
    }
)
READINESS_INCLUDED_CODES = (
    READINESS_DOCKER_ENDPOINT_CODES
    | READINESS_REQUIRED_CODES
    | {"configuration", "wud-api"}
)

_EffectiveConfigLoader = Callable[[WebSettings], UpdaterConfig]
_StaticSpaAvailableChecker = Callable[[WebSettings], bool]

_effective_config_loader: _EffectiveConfigLoader | None = None
_static_spa_available_checker: _StaticSpaAvailableChecker | None = None


def configure(
    *,
    effective_config_loader: _EffectiveConfigLoader,
    static_spa_available_checker: _StaticSpaAvailableChecker,
) -> None:
    global _effective_config_loader, _static_spa_available_checker
    _effective_config_loader = effective_config_loader
    _static_spa_available_checker = static_spa_available_checker


def api_healthz() -> HealthResponse:
    return HealthResponse(ok=True, version=__version__)


def api_readyz(request: Request, response: Response) -> ReadyResponse | Response:
    if not _raw_client_is_loopback(request):
        return Response(status_code=404)
    return ready_response(_settings(request), response)


def api_ready(request: Request, response: Response) -> ReadyResponse:
    return ready_response(_settings(request), response)


def api_doctor(request: Request) -> DoctorResponse:
    settings = _settings(request)
    return doctor_response(settings, web_doctor_result(settings, request))


def ready_response(
    settings: WebSettings,
    response: Response,
) -> ReadyResponse:
    doctor = doctor_response(settings, web_readiness_result(settings))
    checks = [
        check for check in doctor.checks if check.code in READINESS_INCLUDED_CODES
    ]
    missing = missing_readiness_checks(checks)
    if missing:
        checks.append(
            DoctorCheckResponse(
                status="FAIL",
                code="readiness-missing-checks",
                category="webui",
                name="readiness checks",
                detail="missing required check(s): " + ", ".join(missing),
            )
        )
    ok = all(check.status != "FAIL" for check in checks)
    if not ok:
        response.status_code = 503
    return ReadyResponse(ok=ok, version=__version__, checks=checks)


def missing_readiness_checks(checks: Sequence[DoctorCheckResponse]) -> list[str]:
    present = {check.code for check in checks}
    missing = [
        code.replace("-", " ")
        for code in sorted(READINESS_REQUIRED_CODES)
        if code not in present
    ]
    if not present.intersection(READINESS_DOCKER_ENDPOINT_CODES):
        missing.insert(0, "docker socket or endpoint")
    return missing


def web_doctor_result(settings: WebSettings, request: Request) -> DoctorDataResult:
    try:
        options, env = _web_doctor_options_and_env(settings)
        result = Doctor(options, environ=env).run_result()
    except DoctorConfigError as exc:
        result = _doctor_configuration_result(exc)
    return DoctorDataResult(
        checks=(*result.checks, *_web_doctor_checks(settings, request))
    )


def web_readiness_result(
    settings: WebSettings,
) -> DoctorDataResult:
    try:
        options, env = _web_doctor_options_and_env(settings)
        result = Doctor(options, environ=env).run_readiness_result()
    except DoctorConfigError as exc:
        result = _doctor_configuration_result(exc)
    return DoctorDataResult(
        checks=(
            *result.checks,
            _web_database_doctor_check(settings),
            _web_wud_api_doctor_check(settings),
        )
    )


def doctor_response(
    settings: WebSettings,
    result: DoctorDataResult,
) -> DoctorResponse:
    return DoctorResponse(
        ok=result.ok,
        failures=result.failures,
        warnings=result.warnings,
        checks=[
            DoctorCheckResponse(
                status=check.status,  # type: ignore[arg-type]
                code=check.code,
                category=check.category,
                name=check.name,
                detail=_redact_sensitive_text(settings, check.detail),
                target=_redact_sensitive_text(settings, check.target),
                suggestions=[
                    DoctorSuggestionResponse(
                        label=suggestion.label,
                        description=_redact_sensitive_text(
                            settings,
                            suggestion.description,
                        ),
                        snippet=_redact_sensitive_text(settings, suggestion.snippet),
                    )
                    for suggestion in check.suggestions
                ],
            )
            for check in result.checks
        ],
    )


def _web_doctor_options_and_env(
    settings: WebSettings,
) -> tuple[DoctorDataOptions, dict[str, str]]:
    env = _doctor_command_env(settings)
    args = SimpleNamespace(
        base=str(settings.config.docker_base),
        file=str(settings.config.wud_out_file),
        log_dir=str(settings.config.log_dir),
        scripts_dir=env.get("WUD_SCRIPTS_DIR", ""),
        no_color=True,
    )
    return (
        doctor_options_from_namespace(
            args,
            repo_root=Path(__file__).resolve().parents[2],
            environ=env,
        ),
        env,
    )


def _doctor_configuration_result(exc: DoctorConfigError) -> DoctorDataResult:
    return DoctorDataResult(
        checks=(
            DoctorDataCheck(
                status="FAIL",
                name="configuration",
                detail=str(exc),
                code="configuration",
                category="configuration",
                suggestions=(
                    DoctorDataSuggestion(
                        label="Fix environment value",
                        description=(
                            "Set the reported variable to an accepted value "
                            "before running doctor again."
                        ),
                    ),
                ),
            ),
        )
    )


def _doctor_command_env(settings: WebSettings) -> dict[str, str]:
    config = _effective_config(settings)
    env = dict(settings.command_env or {})
    env["DOCKER_BASE"] = str(config.docker_base)
    env["WUD_OUT_FILE"] = str(config.wud_out_file)
    env["WUD_LOG_DIR"] = str(config.log_dir)
    env[COMPOSE_IGNORE_PATHS_ENV] = format_compose_ignore_paths(
        config.compose_ignore_paths
    )
    env[DIGEST_PIN_UPDATES_ENV] = _format_bool(config.digest_pin_updates)
    return env


def _web_doctor_checks(
    settings: WebSettings,
    request: Request,
) -> tuple[DoctorDataCheck, ...]:
    checks: list[DoctorDataCheck] = []
    checks.append(_web_database_doctor_check(settings))
    checks.append(
        _web_doctor_check(
            "WARN" if settings.dev_no_auth else "PASS",
            "WebUI authentication",
            "development auth bypass is enabled"
            if settings.dev_no_auth
            else "authentication is required",
            code="webui-authentication",
            suggestions=()
            if not settings.dev_no_auth
            else (
                DoctorDataSuggestion(
                    label="Require browser authentication",
                    description=(
                        "Disable the local development auth bypass before exposing "
                        "the WebUI."
                    ),
                    snippet="WUD_WEB_DEV_NO_AUTH=false",
                ),
            ),
        )
    )
    checks.append(
        _web_doctor_check(
            "WARN" if settings.mutations_enabled else "PASS",
            "WebUI mutation gate",
            "browser mutations are enabled"
            if settings.mutations_enabled
            else "browser mutations are disabled",
            code="webui-mutation-gate",
            suggestions=()
            if not settings.mutations_enabled
            else (
                DoctorDataSuggestion(
                    label="Return to read-only mode",
                    description=(
                        "Leave browser mutations disabled unless this deployment "
                        "is intentionally allowed to apply updates."
                    ),
                    snippet="WUD_WEB_MUTATIONS_ENABLED=false",
                ),
            ),
        )
    )
    checks.append(
        _web_doctor_check(
            "PASS" if settings.allowed_hosts else "FAIL",
            "WebUI allowed hosts",
            _format_sequence(sorted(settings.allowed_hosts)) or "none configured",
            code="webui-allowed-hosts",
            suggestions=()
            if settings.allowed_hosts
            else (
                DoctorDataSuggestion(
                    label="Configure allowed hosts",
                    description=(
                        "Set the hostnames clients use to reach the WebUI."
                    ),
                    snippet="WUD_WEB_ALLOWED_HOSTS=localhost,127.0.0.1",
                ),
            ),
        )
    )
    checks.append(_web_wud_api_doctor_check(settings))
    checks.extend(_web_wud_api_diagnostic_checks(settings))
    effective_origin = _effective_origin(request, settings)
    checks.append(
        _web_doctor_check(
            "PASS" if settings.public_origin else "WARN",
            "WebUI public origin",
            settings.public_origin
            if settings.public_origin
            else f"derived from request as {effective_origin}",
            code="webui-public-origin",
            suggestions=()
            if settings.public_origin
            else _public_origin_suggestions(settings, request, effective_origin),
        )
    )
    secure_cookie = _secure_cookie(settings, request)
    checks.append(
        _web_doctor_check(
            "PASS" if secure_cookie else "WARN",
            "WebUI secure cookies",
            f"{settings.secure_cookies} mode resolves to {_format_bool(secure_cookie)}",
            code="webui-secure-cookies",
            suggestions=()
            if secure_cookie
            else (
                DoctorDataSuggestion(
                    label="Use HTTPS public origin",
                    description=(
                        "Set a HTTPS public origin or force secure cookies for "
                        "reverse-proxy deployments."
                    ),
                    snippet="WUD_WEB_PUBLIC_ORIGIN=https://wud.example.test",
                ),
            ),
        )
    )
    checks.append(
        _web_doctor_check(
            "PASS",
            "WebUI trusted proxies",
            _format_sequence(str(network) for network in settings.trusted_proxies)
            or "not configured",
            code="webui-trusted-proxies",
        )
    )
    static_available = _static_spa_available(settings)
    checks.append(
        _web_doctor_check(
            "PASS" if static_available else "WARN",
            "WebUI static SPA",
            "static assets are available"
            if static_available
            else "static assets are not mounted; API-only mode is active",
            code="webui-static-spa",
        )
    )
    return tuple(checks)


def _web_database_doctor_check(settings: WebSettings) -> DoctorDataCheck:
    db_ready, db_warning = database_ready(settings)
    return _web_doctor_check(
        "PASS" if db_ready else "FAIL",
        "WebUI database",
        str(settings.config.db_path) if db_ready else db_warning,
        code="webui-database",
        suggestions=()
        if db_ready
        else (
            DoctorDataSuggestion(
                label="Persist WebUI database",
                description=(
                    "Mount a writable persistent directory and set WUD_DB_PATH "
                    "inside it."
                ),
                snippet="WUD_DB_PATH=/logs/wudup.sqlite",
            ),
        ),
    )


def _web_wud_api_doctor_check(settings: WebSettings) -> DoctorDataCheck:
    snapshot = web_wud_api.get_snapshot(settings, include_containers=True)
    status = (
        "PASS"
        if snapshot.status.metadata_available
        and snapshot.degraded_container_count == 0
        else "WARN"
    )
    if snapshot.status.state == "auth_required":
        detail = snapshot.status.detail or "WUD API metadata requires authentication"
        suggestions = (_wud_api_auth_suggestion(settings),)
    elif snapshot.status.available:
        detail = snapshot.status.detail or "WUD API is reachable"
        suggestions = ()
    else:
        detail = snapshot.status.detail or "WUD API is unavailable"
        suggestions = (
            DoctorDataSuggestion(
                label="Connect WUD internally",
                description=(
                    "Place the wud and wudup services on an internal "
                    "Docker network, or set WUD_API_BASE_URL to the internal "
                    "WUD HTTP endpoint."
                ),
                snippet=f"{web_wud_api.WUD_API_BASE_URL_ENV}={web_wud_api.DEFAULT_WUD_API_BASE_URL}",
            ),
        )
    return _web_doctor_check(
        status,  # type: ignore[arg-type]
        "WUD API discovery",
        detail,
        code="wud-api",
        suggestions=suggestions,
    )


def _wud_api_auth_suggestion(settings: WebSettings) -> DoctorDataSuggestion:
    if settings.wud_api_client.configured:
        return DoctorDataSuggestion(
            label="Verify WUD API credentials",
            description=(
                "Configured WUD API credentials were rejected. Update "
                f"{web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV}, "
                f"{web_wud_api.WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV}, or "
                f"{web_wud_api.WUD_API_HEADERS_FILE_ENV} to match the proxy "
                "protecting WUD."
            ),
            snippet=(
                f"{web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV}="
                "/run/secrets/wud_api_token"
            ),
        )
    return DoctorDataSuggestion(
        label="Configure WUD API credentials",
        description=(
            "Set bearer, basic, or static header credentials for WUDup outbound "
            f"WUD API calls with {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV}, "
            f"{web_wud_api.WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV}, or "
            f"{web_wud_api.WUD_API_HEADERS_FILE_ENV}. Keep WUD_PENDING_SOURCE=file "
            "to rely only on the callback todo file."
        ),
        snippet=(
            f"{web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV}="
            "/run/secrets/wud_api_token"
        ),
    )


def _web_wud_api_diagnostic_checks(settings: WebSettings) -> tuple[DoctorDataCheck, ...]:
    diagnostics = web_wud_api.get_configuration_diagnostics(settings)
    return (
        _web_wud_api_diagnostic_check(
            "wud-api-app",
            "WUD API app configuration",
            diagnostics.app.status,
            _wud_api_app_detail(diagnostics.app.name, diagnostics.app.version),
        ),
        _web_wud_api_diagnostic_check(
            "wud-api-log",
            "WUD API log configuration",
            diagnostics.log.status,
            f"log level {diagnostics.log.level}" if diagnostics.log.level else "",
        ),
        _web_wud_api_diagnostic_check(
            "wud-api-store",
            "WUD API store configuration",
            diagnostics.store.status,
            _wud_api_store_detail(diagnostics.store.path, diagnostics.store.file),
        ),
        _web_wud_api_diagnostic_check(
            "wud-api-watchers",
            "WUD API watcher configuration",
            diagnostics.watchers_status,
            _wud_api_watchers_detail(diagnostics.watchers),
        ),
        _web_wud_api_diagnostic_check(
            "wud-api-registries",
            "WUD API registry configuration",
            diagnostics.registries_status,
            _wud_api_registries_detail(diagnostics.registries),
        ),
    )


def _web_wud_api_diagnostic_check(
    code: str,
    name: str,
    endpoint: WudApiDiagnosticEndpointStatus,
    ready_detail: str,
) -> DoctorDataCheck:
    detail = ready_detail if endpoint.state == "ready" else ""
    return _web_doctor_check(
        "PASS" if endpoint.state == "ready" else "WARN",
        name,
        detail or endpoint.detail,
        code=code,
        category="wud-api",
    )


def _wud_api_app_detail(name: str, version: str) -> str:
    if name and version:
        return f"{name} {version}"
    return name or version


def _wud_api_store_detail(path: str, file: str) -> str:
    if path and file:
        return f"path {path}, file {file}"
    if path:
        return f"path {path}"
    if file:
        return f"file {file}"
    return ""


def _wud_api_watchers_detail(watchers: Sequence[WudApiWatcherDiagnostics]) -> str:
    return _wud_api_named_items_detail(
        "watcher",
        [_wud_api_watcher_label(watcher) for watcher in watchers],
    )


def _wud_api_registries_detail(
    registries: Sequence[WudApiRegistryDiagnostics],
) -> str:
    return _wud_api_named_items_detail(
        "registry",
        [_wud_api_typed_name(registry.id, registry.type, registry.name) for registry in registries],
    )


def _wud_api_named_items_detail(kind: str, labels: Sequence[str]) -> str:
    count = len(labels)
    noun = kind if count == 1 else f"{kind}s"
    if not labels:
        return f"0 {noun} configured"
    preview = ", ".join(labels[:5])
    if count > 5:
        preview = f"{preview}, +{count - 5} more"
    return f"{count} {noun}: {preview}"


def _wud_api_watcher_label(watcher: WudApiWatcherDiagnostics) -> str:
    label = _wud_api_typed_name(watcher.id, watcher.type, watcher.name)
    details: list[str] = []
    if watcher.cron:
        details.append(f"cron {watcher.cron}")
    if watcher.watch_by_default is not None:
        details.append(f"watch-by-default {_format_bool(watcher.watch_by_default)}")
    if details:
        return f"{label} ({', '.join(details)})"
    return label


def _wud_api_typed_name(item_id: str, item_type: str, item_name: str) -> str:
    type_name = "/".join(part for part in (item_type, item_name) if part)
    if item_id and type_name:
        return f"{item_id} ({type_name})"
    return item_id or type_name or "unnamed"


def _public_origin_suggestions(
    settings: WebSettings,
    request: Request,
    effective_origin: str,
) -> tuple[DoctorDataSuggestion, ...]:
    observed_origin = _observed_lan_origin(settings, request, effective_origin)
    if observed_origin:
        return (
            DoctorDataSuggestion(
                label="Set observed public origin",
                description=(
                    "Persist the browser-visible origin that reached this "
                    "request in the Compose env file."
                ),
                snippet=f"WUD_WEB_PUBLIC_ORIGIN={observed_origin}",
            ),
        )
    return (
        DoctorDataSuggestion(
            label="Set reverse proxy origin",
            description=(
                "Set WUD_WEB_PUBLIC_ORIGIN when the WebUI is served behind a "
                "LAN address or reverse proxy."
            ),
            snippet="WUD_WEB_PUBLIC_ORIGIN=https://wud.example.test",
        ),
    )


def _observed_lan_origin(
    settings: WebSettings,
    request: Request,
    effective_origin: str,
) -> str:
    forwarded_origin = _trusted_forwarded_origin(request, settings)
    candidate = forwarded_origin or effective_origin
    try:
        origin = _parse_public_origin(candidate)
    except WebConfigError:
        return ""
    host = _host_from_origin(origin)
    if not host or _host_is_loopback(host):
        return ""
    if not forwarded_origin and host not in settings.allowed_hosts:
        return ""
    return origin


def _host_is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _web_doctor_check(
    status: DoctorCheckStatus,
    name: str,
    detail: str,
    *,
    code: str,
    category: str = "webui",
    suggestions: Sequence[DoctorDataSuggestion] = (),
) -> DoctorDataCheck:
    return DoctorDataCheck(
        status=status,
        name=name,
        detail=detail,
        code=code,
        category=category,
        suggestions=tuple(suggestions),
    )


def _effective_config(settings: WebSettings) -> UpdaterConfig:
    if _effective_config_loader is None:
        raise RuntimeError("web_health.configure() was not called")
    return _effective_config_loader(settings)


def _static_spa_available(settings: WebSettings) -> bool:
    if _static_spa_available_checker is None:
        raise RuntimeError("web_health.configure() was not called")
    return _static_spa_available_checker(settings)


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_sequence(values: Sequence[str] | Iterator[str]) -> str:
    return ", ".join(item for item in values if item)
