"""WUD client configuration and synchronous HTTP transport.

Cache and watcher lifecycles belong to web_wud_cache. This module
owns credential loading, header validation, URL handling, and request execution.
"""

from __future__ import annotations

import base64
import json
import math
import re
import secrets
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

from .web_auth import (
    WebConfigError,
    _redact_sensitive_text,
    _redact_unknown_absolute_paths,
)
from .web_models import WebSettings, WudApiClientConfig

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
WUD_API_USER_AGENT = "wudup-webui-wud-api/1.0"
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


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



_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


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
