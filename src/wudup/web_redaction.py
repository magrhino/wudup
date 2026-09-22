"""Shared WebUI operator-error and support-bundle redaction."""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .web_models import WebSettings

SENSITIVE_ENV_KEYS = (
    "WUD_WEB_TOKEN",
    "WUD_API_AUTH_BEARER_TOKEN",
    "WUD_API_AUTH_BEARER_TOKEN_FILE",
    "WUD_API_AUTH_BASIC_PASSWORD",
    "WUD_API_AUTH_BASIC_PASSWORD_FILE",
    "GITHUB_TOKEN",
    "DISCORD_WEBHOOK",
    "DISCORD_RELEASES_WEBHOOK",
    "ADMIN_WEBHOOK",
)


SENSITIVE_FIELD_KEY_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "header",
        "key",
        "pass",
        "password",
        "secret",
        "token",
        "webhook",
    }
)


def strip_validation_inputs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_validation_inputs(item)
            for key, item in value.items()
            if key != "input"
        }
    if isinstance(value, list):
        return [strip_validation_inputs(item) for item in value]
    return value


def redact_sensitive_text(
    settings: WebSettings,
    value: str,
    extra_secrets: Sequence[str] = (),
) -> str:
    redacted = value
    for secret in _sensitive_redaction_values(settings, extra_secrets):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def sensitive_mapping_key(key: str) -> bool:
    normalized = key.lower().replace("-", "").replace("_", "")
    return any(part in normalized for part in SENSITIVE_FIELD_KEY_PARTS)


def sanitize_support_bundle_value(settings: WebSettings, value: Any) -> Any:
    return sanitize_support_bundle_value_with_secrets(settings, value, ())


def sanitize_support_bundle_value_with_secrets(
    settings: WebSettings,
    value: Any,
    extra_secrets: Sequence[str],
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_support_bundle_value_with_secrets(
                settings,
                item,
                extra_secrets,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_support_bundle_value_with_secrets(settings, item, extra_secrets)
            for item in value
        ]
    if isinstance(value, str):
        return sanitize_support_bundle_text(settings, value, extra_secrets)
    return value


def sanitize_support_bundle_text(
    settings: WebSettings,
    value: str,
    extra_secrets: Sequence[str] = (),
) -> str:
    replacements = _support_bundle_path_replacements(settings)
    redacted = redact_sensitive_text(settings, value, extra_secrets=extra_secrets)
    for source, target in replacements:
        redacted = redacted.replace(source, target)
    return redact_unknown_absolute_paths(redacted)


def _support_bundle_path_replacements(settings: WebSettings) -> list[tuple[str, str]]:
    config = settings.config
    exact_paths: list[tuple[Path, str]] = [
        (config.wud_out_file, "<WUD_OUT_FILE>"),
        (config.log_dir, "<WUD_LOG_DIR>"),
        (config.db_path, "<WUD_DB_PATH>"),
    ]
    root_paths: list[tuple[Path, str]] = [(config.docker_base, "<DOCKER_BASE>")]
    if settings.host_docker_base is not None:
        root_paths.append((settings.host_docker_base, "<HOST_DOCKER_BASE>"))

    replacements: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, label in (*exact_paths, *root_paths):
        text = str(path)
        if text and text not in seen:
            seen.add(text)
            replacements.append((text, label))

    for root, label in root_paths:
        root_text = str(root).rstrip("/")
        if not root_text or root_text in seen:
            continue
        seen.add(root_text)
        replacements.append((root_text, label))

    return sorted(replacements, key=lambda item: len(item[0]), reverse=True)


def redact_unknown_absolute_paths(value: str) -> str:
    absolute_path_pattern = (
        r"(?<![:/<>\w-])/(?:[^\s\"'`,;)\]}]+)"
        r"|(?<![\w<])(?:[A-Za-z]:[\\/][^\s\"'`,;)\]}]+"
        r"|\\\\[^\s\"'`,;)\]}]+)"
    )
    return re.sub(
        absolute_path_pattern,
        "[REDACTED_PATH]",
        value,
    )


def _sensitive_redaction_values(
    settings: WebSettings,
    extra_secrets: Sequence[str],
) -> list[str]:
    values: list[str] = []
    env = settings.command_env or {}
    values.extend(extra_secrets)
    values.append(settings.auth_token)
    values.extend(settings.wud_api_client.secret_values)
    values.extend(env.get(key, "") for key in SENSITIVE_ENV_KEYS)

    expanded: list[str] = []
    for value in values:
        if value:
            expanded.append(value)
            expanded.extend(_secret_url_fragments(value))

    seen: set[str] = set()
    result: list[str] = []
    for value in sorted(expanded, key=len, reverse=True):
        if len(value) < 4 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _secret_url_fragments(value: str) -> list[str]:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return []
    fragments: list[str] = []
    path = parsed.path.strip("/")
    if len(path) >= 8:
        fragments.append(path)
    fragments.extend(segment for segment in path.split("/") if len(segment) >= 8)
    if parsed.query and len(parsed.query) >= 8:
        fragments.append(parsed.query)
    return fragments


def safe_exception_detail(
    settings: WebSettings,
    message: str,
    exc: BaseException,
) -> str:
    detail = redact_sensitive_text(settings, str(exc))
    return f"{message}: {redact_unknown_absolute_paths(detail)}"
