"""App-wide effective configuration resolved from environment and managed settings."""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from fastapi import HTTPException

from .config import (
    COMPOSE_IGNORE_PATHS_ENV,
    DEFAULT_COMPOSE_IGNORE_PATHS,
    DEFAULT_DIGEST_PIN_UPDATES,
    DIGEST_PIN_UPDATES_ENV,
    ConfigError,
    UpdaterConfig,
    parse_bool_env,
    parse_compose_ignore_paths,
)
from .db import DatabaseError
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_database import web_setting as _web_setting
from .web_database import web_setting_or_none as _web_setting_or_none
from .web_models import WebSettings
from .web_onboarding import ONBOARDING_DISMISSED_AT_KEY
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_release_notification_state import (
    DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE,
    RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES,
    ReleaseNotificationConfig,
)

LOGGER = logging.getLogger(__name__)

MANAGED_THEME_PREFERENCE_KEY = "theme_preference"
MANAGED_THEME_PREFERENCE_DB_KEY = "ui.theme_preference"
MANAGED_ONBOARDING_CHECKLIST_KEY = "onboarding_checklist"
MANAGED_COMPOSE_IGNORE_PATHS_KEY = "compose_ignore_paths"
MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY = "compose.ignore_paths"
MANAGED_DIGEST_PIN_UPDATES_KEY = "digest_pin_updates"
MANAGED_DIGEST_PIN_UPDATES_DB_KEY = "compose.digest_pin_updates"
MANAGED_RETAG_DIGEST_PINS_KEY = "retag_digest_pins"
MANAGED_RETAG_DIGEST_PINS_DB_KEY = "retag.digest_pins"
RELEASE_NOTES_ENABLED_ENV = "WUD_RELEASE_NOTES_ENABLED"
MANAGED_RELEASE_NOTES_ENABLED_KEY = "release_notes_enabled"
MANAGED_RELEASE_NOTES_ENABLED_DB_KEY = "release_notes.enabled"
MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_KEY = (
    "release_notifications_delivery_mode"
)
MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_DB_KEY = (
    "release_notifications.delivery_mode"
)
MANAGED_RELEASE_NOTIFICATIONS_MODE_KEY = "release_notifications_mode"
MANAGED_RELEASE_NOTIFICATIONS_MODE_DB_KEY = "release_notifications.mode"
MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_KEY = (
    "release_notifications_resend_policy"
)
MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_DB_KEY = (
    "release_notifications.resend_policy"
)
MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_KEY = (
    "release_notifications_cooldown_seconds"
)
MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_DB_KEY = (
    "release_notifications.cooldown_seconds"
)
MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY = (
    "release_notifications_discord_webhook"
)
MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY = (
    "release_notifications.discord_webhook"
)
MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_KEY = "release_notifications_verbosity"
MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_DB_KEY = "release_notifications.verbosity"
DISCORD_WEBHOOK_ENV_NAMES = ("DISCORD_WEBHOOK",)
THEME_PREFERENCE_VALUES = ("system", "light", "dark")
ONBOARDING_CHECKLIST_VALUES = ("visible", "dismissed")
DIGEST_PIN_UPDATES_VALUES = ("false", "true")
RETAG_DIGEST_PINS_VALUES = ("false", "true")
RELEASE_NOTES_ENABLED_VALUES = ("false", "true")
RELEASE_NOTIFICATIONS_MODE_VALUES = ("digest", "per_container")
RELEASE_NOTIFICATIONS_RESEND_POLICY_VALUES = ("remote_change", "cooldown")
RELEASE_NOTIFICATIONS_VERBOSITY_VALUES = ("summary", "full")
DEFAULT_RELEASE_NOTES_ENABLED = False
DEFAULT_RETAG_DIGEST_PINS = False
DEFAULT_RELEASE_NOTIFICATIONS_MODE = "digest"
DEFAULT_RELEASE_NOTIFICATIONS_RESEND_POLICY = "remote_change"
DEFAULT_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS = 86_400
DEFAULT_RELEASE_NOTIFICATIONS_VERBOSITY = "summary"
_DEFAULT_DISCORD_WEBHOOK_ALLOWED_HOSTS = (
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
)
_DEFAULT_DISCORD_WEBHOOK_PATH_PREFIX = "/api/webhooks/"


def _load_discord_webhook_policy() -> tuple[frozenset[str], str]:
    try:
        policy = json.loads(
            files("wudup").joinpath("discord_webhook_policy.json").read_text(
                encoding="utf-8"
            )
        )
        allowed_hosts = policy["allowed_hosts"]
        path_prefix = policy["path_prefix"]
        if (
            not isinstance(allowed_hosts, list)
            or not allowed_hosts
            or not all(isinstance(host, str) and host.strip() for host in allowed_hosts)
            or not isinstance(path_prefix, str)
            or not path_prefix.startswith("/")
        ):
            raise ValueError("invalid Discord webhook policy shape")
        return frozenset(host.strip().lower() for host in allowed_hosts), path_prefix
    except (OSError, KeyError, TypeError, ValueError) as exc:
        LOGGER.warning(
            "using fallback Discord webhook policy; packaged policy load failed: %s",
            type(exc).__name__,
        )
        return (
            frozenset(_DEFAULT_DISCORD_WEBHOOK_ALLOWED_HOSTS),
            _DEFAULT_DISCORD_WEBHOOK_PATH_PREFIX,
        )


DISCORD_WEBHOOK_ALLOWED_HOSTS, DISCORD_WEBHOOK_PATH_PREFIX = (
    _load_discord_webhook_policy()
)

def _effective_config(settings: WebSettings) -> UpdaterConfig:
    return replace(
        settings.config,
        compose_ignore_paths=_effective_compose_ignore_paths(settings),
        digest_pin_updates=_effective_digest_pin_updates(settings),
    )


def _effective_compose_ignore_paths(settings: WebSettings) -> tuple[Path, ...]:
    if _compose_ignore_env_configured(settings):
        return settings.config.compose_ignore_paths
    return _stored_compose_ignore_paths(settings)


def _stored_compose_ignore_paths(settings: WebSettings) -> tuple[Path, ...]:
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            value = _web_setting_or_none(conn, MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY)
    except ReadOnlyDatabaseMissing:
        return DEFAULT_COMPOSE_IGNORE_PATHS
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read compose ignore paths",
                exc,
            ),
        ) from exc

    try:
        return parse_compose_ignore_paths(
            value,
            name=MANAGED_COMPOSE_IGNORE_PATHS_KEY,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"stored {MANAGED_COMPOSE_IGNORE_PATHS_KEY} is invalid",
                exc,
            ),
        ) from exc


def _compose_ignore_env_configured(settings: WebSettings) -> bool:
    return COMPOSE_IGNORE_PATHS_ENV in _settings_env(settings)


def _compose_ignore_paths_disabled_reason(settings: WebSettings) -> str:
    if not _compose_ignore_env_configured(settings):
        return ""
    return (
        f"{COMPOSE_IGNORE_PATHS_ENV} is configured in the server environment. "
        "Unset it to manage compose ignore paths in the WebUI."
    )


def _effective_digest_pin_updates(settings: WebSettings) -> bool:
    if _digest_pin_env_configured(settings):
        return settings.config.digest_pin_updates
    return _stored_digest_pin_updates(settings)


def _stored_digest_pin_updates(settings: WebSettings) -> bool:
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            value = _web_setting(conn, MANAGED_DIGEST_PIN_UPDATES_DB_KEY)
    except ReadOnlyDatabaseMissing:
        return DEFAULT_DIGEST_PIN_UPDATES
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read digest-pin setting",
                exc,
            ),
        ) from exc
    try:
        return parse_bool_env(
            MANAGED_DIGEST_PIN_UPDATES_KEY,
            value,
            default=DEFAULT_DIGEST_PIN_UPDATES,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"stored {MANAGED_DIGEST_PIN_UPDATES_KEY} is invalid",
                exc,
            ),
        ) from exc


def _effective_retag_digest_pins(settings: WebSettings) -> bool:
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            value = _web_setting(conn, MANAGED_RETAG_DIGEST_PINS_DB_KEY)
    except ReadOnlyDatabaseMissing:
        return DEFAULT_RETAG_DIGEST_PINS
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read retag digest-pin setting",
                exc,
            ),
        ) from exc
    try:
        return parse_bool_env(
            MANAGED_RETAG_DIGEST_PINS_KEY,
            value,
            default=DEFAULT_RETAG_DIGEST_PINS,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"stored {MANAGED_RETAG_DIGEST_PINS_KEY} is invalid",
                exc,
            ),
        ) from exc


def _digest_pin_env_configured(settings: WebSettings) -> bool:
    return DIGEST_PIN_UPDATES_ENV in _settings_env(settings)


def _digest_pin_disabled_reason(settings: WebSettings) -> str:
    if not _digest_pin_env_configured(settings):
        return ""
    return (
        f"{DIGEST_PIN_UPDATES_ENV} is configured in the server environment. "
        "Unset it to manage digest-pin updates in the WebUI."
    )


def effective_release_notes_enabled(settings: WebSettings) -> bool:
    enabled, _configured = _effective_release_notes_enabled_state(settings)
    return enabled


def effective_release_notification_config(
    settings: WebSettings,
) -> ReleaseNotificationConfig:
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            values = _managed_settings_db_values(conn)
    except ReadOnlyDatabaseMissing:
        values = {}
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release notification settings",
                exc,
            ),
        ) from exc
    try:
        return _release_notification_config_from_values(values)
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release notification settings",
                exc,
            ),
        ) from exc


def effective_release_notification_webhook(settings: WebSettings) -> tuple[str, str]:
    env_webhook = _discord_webhook_env(settings)
    if env_webhook[0]:
        return env_webhook
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            value = _web_setting_or_none(
                conn,
                MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
            )
    except ReadOnlyDatabaseMissing:
        return "", ""
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read Discord webhook setting",
                exc,
            ),
        ) from exc
    if not value:
        return "", ""
    try:
        return _validated_discord_webhook(value), "WebUI settings"
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"stored {MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY} is invalid",
                exc,
            ),
        ) from exc


def release_notification_webhook_redaction_values(
    settings: WebSettings,
) -> tuple[str, ...]:
    values = [_discord_webhook_env(settings)[0]]
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            values.append(
                _web_setting_or_none(
                    conn,
                    MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
                )
                or ""
            )
    except (ReadOnlyDatabaseMissing, OSError, sqlite3.Error, DatabaseError):
        pass
    return tuple(value for value in values if value)


def _effective_release_notes_enabled_state(settings: WebSettings) -> tuple[bool, bool]:
    if _release_notes_enabled_env_configured(settings):
        return bool(settings.release_notes_enabled_env), True
    return _stored_release_notes_enabled_state(settings)


def _stored_release_notes_enabled(settings: WebSettings) -> bool:
    enabled, _configured = _stored_release_notes_enabled_state(settings)
    return enabled


def _stored_release_notes_enabled_state(settings: WebSettings) -> tuple[bool, bool]:
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            value = _web_setting_or_none(conn, MANAGED_RELEASE_NOTES_ENABLED_DB_KEY)
    except ReadOnlyDatabaseMissing:
        return DEFAULT_RELEASE_NOTES_ENABLED, False
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release-note setting",
                exc,
            ),
        ) from exc
    try:
        return (
            parse_bool_env(
                MANAGED_RELEASE_NOTES_ENABLED_KEY,
                value or "",
                default=DEFAULT_RELEASE_NOTES_ENABLED,
            ),
            value is not None,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"stored {MANAGED_RELEASE_NOTES_ENABLED_KEY} is invalid",
                exc,
            ),
        ) from exc


def _release_notes_enabled_env_configured(settings: WebSettings) -> bool:
    return RELEASE_NOTES_ENABLED_ENV in _settings_env(settings)


def _release_notes_enabled_disabled_reason(settings: WebSettings) -> str:
    if not _release_notes_enabled_env_configured(settings):
        return ""
    return (
        f"{RELEASE_NOTES_ENABLED_ENV} is configured in the server environment. "
        "Unset it to manage release-note notifications in the WebUI."
    )


def _discord_webhook_env(settings: WebSettings) -> tuple[str, str]:
    env = _settings_env(settings)
    for name in DISCORD_WEBHOOK_ENV_NAMES:
        value = env.get(name, "").strip()
        if value:
            return value, name
    return "", ""


def _discord_webhook_disabled_reason(settings: WebSettings) -> str:
    _value, source = _discord_webhook_env(settings)
    if not source:
        return ""
    return (
        f"{source} is configured in the server environment. "
        "Unset it to manage the Discord webhook in the WebUI."
    )


def _managed_settings_db_values(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT key, value
        FROM web_settings
        WHERE key IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            MANAGED_THEME_PREFERENCE_DB_KEY,
            ONBOARDING_DISMISSED_AT_KEY,
            MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY,
            MANAGED_DIGEST_PIN_UPDATES_DB_KEY,
            MANAGED_RETAG_DIGEST_PINS_DB_KEY,
            MANAGED_RELEASE_NOTES_ENABLED_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_MODE_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
            MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_DB_KEY,
        ),
    ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _release_notification_config_from_values(
    values: Mapping[str, str],
) -> ReleaseNotificationConfig:
    delivery_mode = _managed_choice_setting_value(
        values,
        MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_DB_KEY,
        DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE,
        MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_KEY,
        RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES,
    )
    mode = _managed_choice_setting_value(
        values,
        MANAGED_RELEASE_NOTIFICATIONS_MODE_DB_KEY,
        DEFAULT_RELEASE_NOTIFICATIONS_MODE,
        MANAGED_RELEASE_NOTIFICATIONS_MODE_KEY,
        RELEASE_NOTIFICATIONS_MODE_VALUES,
    )
    resend_policy = _managed_choice_setting_value(
        values,
        MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_DB_KEY,
        DEFAULT_RELEASE_NOTIFICATIONS_RESEND_POLICY,
        MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_KEY,
        RELEASE_NOTIFICATIONS_RESEND_POLICY_VALUES,
    )
    cooldown_seconds = _parse_positive_int_setting(
        MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_KEY,
        values.get(MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_DB_KEY, ""),
        DEFAULT_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS,
    )
    verbosity = _managed_choice_setting_value(
        values,
        MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_DB_KEY,
        DEFAULT_RELEASE_NOTIFICATIONS_VERBOSITY,
        MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_KEY,
        RELEASE_NOTIFICATIONS_VERBOSITY_VALUES,
    )
    return ReleaseNotificationConfig(
        delivery_mode=delivery_mode,
        mode=mode,
        resend_policy=resend_policy,
        cooldown_seconds=cooldown_seconds,
        verbosity=verbosity,
    )


def _managed_choice_setting_value(
    values: Mapping[str, str],
    db_key: str,
    default: str,
    setting_key: str,
    allowed_values: Sequence[str],
) -> str:
    value = values.get(db_key, default)
    if value not in allowed_values:
        options = ", ".join(allowed_values)
        raise ConfigError(f"{setting_key} must be one of: {options}")
    return value


def _parse_positive_int_setting(name: str, value: str, default: int) -> int:
    raw_value = value.strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return parsed


def _validated_discord_webhook(value: str) -> str:
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    webhook_segments = (
        parsed.path.removeprefix(DISCORD_WEBHOOK_PATH_PREFIX).split("/")
        if parsed.path.startswith(DISCORD_WEBHOOK_PATH_PREFIX)
        else []
    )
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or host not in DISCORD_WEBHOOK_ALLOWED_HOSTS
        or len(webhook_segments) != 2
        or not all(webhook_segments)
    ):
        raise ConfigError(
            f"{MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY} must be a Discord webhook URL"
        )
    return candidate


def _settings_env(settings: WebSettings) -> Mapping[str, str]:
    return settings.command_env or {}
