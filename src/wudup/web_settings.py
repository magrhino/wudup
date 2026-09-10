"""WebUI settings response and managed preference behavior."""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import cast

from fastapi import HTTPException, Request

from . import web_pending_sources, web_wud_api
from .config import (
    COMPOSE_IGNORE_PATHS_ENV,
    DEFAULT_COMPOSE_IGNORE_PATHS,
    DEFAULT_DIGEST_PIN_UPDATES,
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_MAX_WAIT,
    DEFAULT_TIMEZONE,
    DEFAULT_UPDATE_MODE,
    DIGEST_PIN_UPDATES_ENV,
    ConfigError,
    UpdaterConfig,
    format_compose_ignore_paths,
    load_config,
    parse_bool_env,
    parse_compose_ignore_paths,
)
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .web_auth import (
    SENSITIVE_ENV_KEYS,
    _delete_web_setting,
    _parse_allowed_hosts,
    _safe_exception_detail,
    _secure_cookie,
    _set_web_setting,
    _settings,
    _web_setting,
    _web_setting_or_none,
)
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_models import (
    ManagedSettingEntry,
    ManagedSettingsUpdateRequest,
    ManagedSettingsUpdateResponse,
    SecretSettingStatus,
    SettingsEntry,
    SettingsEntrySource,
    SettingsResponse,
    WebSettings,
)
from .web_onboarding import ONBOARDING_DISMISSED_AT_KEY
from .web_release_notification_state import (
    DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE,
    RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES,
    ReleaseNotificationConfig,
)
from .web_state import _insert_managed_settings_audit
from .web_static import (
    resolve_static_dir as _resolve_static_dir,
)
from .web_static import (
    static_spa_available as _static_spa_available,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_WEB_HOST = "127.0.0.1"
LEGACY_SCRIPTS_ENV = "WUDUP_LEGACY_SCRIPTS"
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
_MANAGED_SETTING_ALLOWED_VALUES = {
    MANAGED_THEME_PREFERENCE_KEY: THEME_PREFERENCE_VALUES,
    MANAGED_ONBOARDING_CHECKLIST_KEY: ONBOARDING_CHECKLIST_VALUES,
    MANAGED_DIGEST_PIN_UPDATES_KEY: DIGEST_PIN_UPDATES_VALUES,
    MANAGED_RETAG_DIGEST_PINS_KEY: RETAG_DIGEST_PINS_VALUES,
    MANAGED_RELEASE_NOTES_ENABLED_KEY: RELEASE_NOTES_ENABLED_VALUES,
    MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_KEY: (
        RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES
    ),
    MANAGED_RELEASE_NOTIFICATIONS_MODE_KEY: RELEASE_NOTIFICATIONS_MODE_VALUES,
    MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_KEY: (
        RELEASE_NOTIFICATIONS_RESEND_POLICY_VALUES
    ),
    MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_KEY: (
        RELEASE_NOTIFICATIONS_VERBOSITY_VALUES
    ),
}
_MANAGED_SETTING_DB_KEYS = {
    MANAGED_THEME_PREFERENCE_KEY: MANAGED_THEME_PREFERENCE_DB_KEY,
    MANAGED_COMPOSE_IGNORE_PATHS_KEY: MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY,
    MANAGED_DIGEST_PIN_UPDATES_KEY: MANAGED_DIGEST_PIN_UPDATES_DB_KEY,
    MANAGED_RETAG_DIGEST_PINS_KEY: MANAGED_RETAG_DIGEST_PINS_DB_KEY,
    MANAGED_RELEASE_NOTES_ENABLED_KEY: MANAGED_RELEASE_NOTES_ENABLED_DB_KEY,
    MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_KEY: (
        MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_DB_KEY
    ),
    MANAGED_RELEASE_NOTIFICATIONS_MODE_KEY: MANAGED_RELEASE_NOTIFICATIONS_MODE_DB_KEY,
    MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_KEY: (
        MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_DB_KEY
    ),
    MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_KEY: (
        MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_DB_KEY
    ),
    MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_KEY: (
        MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_DB_KEY
    ),
}
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


def api_settings(request: Request) -> SettingsResponse:
    settings = _settings(request)
    return settings_response(settings, request)


def settings_response(settings: WebSettings, request: Request) -> SettingsResponse:
    return SettingsResponse(
        updater=_updater_settings_entries(settings),
        webui=_webui_settings_entries(settings, request),
        secrets=_secret_settings(settings),
        managed=_managed_settings_entries(settings),
    )


def api_update_managed_settings(
    payload: ManagedSettingsUpdateRequest,
    request: Request,
) -> ManagedSettingsUpdateResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")

    updates = _validated_managed_setting_updates(payload, settings)
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with conn:
                before = _managed_settings_entries_from_conn(conn, settings)
                _apply_managed_setting_updates(conn, updates)
                after = _managed_settings_entries_from_conn(conn, settings)
                audit_run_id = _insert_managed_settings_audit(
                    conn,
                    settings,
                    request,
                    updated_keys=tuple(updates),
                    before=_managed_settings_audit_values(before),
                    after=_managed_settings_audit_values(after),
                )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not update managed settings",
                exc,
            ),
        ) from exc

    return ManagedSettingsUpdateResponse(managed=after, audit_run_id=audit_run_id)


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


def _managed_settings_entries(settings: WebSettings) -> list[ManagedSettingEntry]:
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
                "could not read managed settings",
                exc,
            ),
        ) from exc
    try:
        return _managed_settings_entries_from_values(values, settings)
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read managed settings",
                exc,
            ),
        ) from exc


def _managed_settings_entries_from_conn(
    conn: sqlite3.Connection,
    settings: WebSettings,
) -> list[ManagedSettingEntry]:
    try:
        return _managed_settings_entries_from_values(
            _managed_settings_db_values(conn),
            settings,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read managed settings",
                exc,
            ),
        ) from exc


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


def _managed_settings_entries_from_values(
    values: Mapping[str, str],
    settings: WebSettings,
) -> list[ManagedSettingEntry]:
    return [
        _theme_preference_entry(values),
        _onboarding_checklist_entry(values),
        _compose_ignore_paths_entry(values, settings),
        _digest_pin_updates_entry(values, settings),
        _retag_digest_pins_entry(values),
        _release_notes_enabled_entry(values, settings),
        *_release_notification_entries(values, settings),
    ]


def _theme_preference_entry(values: Mapping[str, str]) -> ManagedSettingEntry:
    theme_value = values.get(MANAGED_THEME_PREFERENCE_DB_KEY, "")
    theme_configured = theme_value in THEME_PREFERENCE_VALUES
    return ManagedSettingEntry(
        key=MANAGED_THEME_PREFERENCE_KEY,
        value=theme_value if theme_configured else "system",
        default_value="system",
        source="configured" if theme_configured else "default",
        editable=True,
        allowed_values=list(THEME_PREFERENCE_VALUES),
        restart_required=False,
    )


def _onboarding_checklist_entry(values: Mapping[str, str]) -> ManagedSettingEntry:
    onboarding_dismissed_at = values.get(ONBOARDING_DISMISSED_AT_KEY, "")
    return ManagedSettingEntry(
        key=MANAGED_ONBOARDING_CHECKLIST_KEY,
        value="dismissed" if onboarding_dismissed_at else "visible",
        default_value="visible",
        source="configured" if onboarding_dismissed_at else "default",
        editable=True,
        allowed_values=list(ONBOARDING_CHECKLIST_VALUES),
        restart_required=False,
    )


def _compose_ignore_paths_entry(
    values: Mapping[str, str],
    settings: WebSettings,
) -> ManagedSettingEntry:
    compose_disabled_reason = _compose_ignore_paths_disabled_reason(settings)
    if _compose_ignore_env_configured(settings):
        compose_ignore_paths = settings.config.compose_ignore_paths
        compose_configured = True
    else:
        compose_configured = MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY in values
        compose_value = (
            values.get(MANAGED_COMPOSE_IGNORE_PATHS_DB_KEY, "")
            if compose_configured
            else None
        )
        compose_ignore_paths = parse_compose_ignore_paths(
            compose_value,
            name=MANAGED_COMPOSE_IGNORE_PATHS_KEY,
        )
    return ManagedSettingEntry(
        key=MANAGED_COMPOSE_IGNORE_PATHS_KEY,
        value=format_compose_ignore_paths(compose_ignore_paths),
        default_value=format_compose_ignore_paths(DEFAULT_COMPOSE_IGNORE_PATHS),
        source="configured" if compose_configured else "default",
        editable=not compose_disabled_reason,
        allowed_values=[],
        restart_required=False,
        disabled_reason=compose_disabled_reason,
    )


def _digest_pin_updates_entry(
    values: Mapping[str, str],
    settings: WebSettings,
) -> ManagedSettingEntry:
    digest_disabled_reason = _digest_pin_disabled_reason(settings)
    if _digest_pin_env_configured(settings):
        digest_pin_updates = settings.config.digest_pin_updates
        digest_configured = True
    else:
        digest_configured = MANAGED_DIGEST_PIN_UPDATES_DB_KEY in values
        digest_pin_updates = parse_bool_env(
            MANAGED_DIGEST_PIN_UPDATES_KEY,
            values.get(MANAGED_DIGEST_PIN_UPDATES_DB_KEY, ""),
            default=DEFAULT_DIGEST_PIN_UPDATES,
        )
    return ManagedSettingEntry(
        key=MANAGED_DIGEST_PIN_UPDATES_KEY,
        value=_format_bool(digest_pin_updates),
        default_value=_format_bool(DEFAULT_DIGEST_PIN_UPDATES),
        source="configured" if digest_configured else "default",
        editable=not digest_disabled_reason,
        allowed_values=list(DIGEST_PIN_UPDATES_VALUES),
        restart_required=False,
        disabled_reason=digest_disabled_reason,
    )


def _retag_digest_pins_entry(
    values: Mapping[str, str],
) -> ManagedSettingEntry:
    configured = MANAGED_RETAG_DIGEST_PINS_DB_KEY in values
    enabled = parse_bool_env(
        MANAGED_RETAG_DIGEST_PINS_KEY,
        values.get(MANAGED_RETAG_DIGEST_PINS_DB_KEY, ""),
        default=DEFAULT_RETAG_DIGEST_PINS,
    )
    return ManagedSettingEntry(
        key=MANAGED_RETAG_DIGEST_PINS_KEY,
        value=_format_bool(enabled),
        default_value=_format_bool(DEFAULT_RETAG_DIGEST_PINS),
        source="configured" if configured else "default",
        editable=True,
        allowed_values=list(RETAG_DIGEST_PINS_VALUES),
        restart_required=False,
    )


def _release_notes_enabled_entry(
    values: Mapping[str, str],
    settings: WebSettings,
) -> ManagedSettingEntry:
    release_notes_disabled_reason = _release_notes_enabled_disabled_reason(settings)
    if _release_notes_enabled_env_configured(settings):
        release_notes_enabled = bool(settings.release_notes_enabled_env)
        release_notes_configured = True
    else:
        release_notes_configured = MANAGED_RELEASE_NOTES_ENABLED_DB_KEY in values
        release_notes_enabled = parse_bool_env(
            MANAGED_RELEASE_NOTES_ENABLED_KEY,
            values.get(MANAGED_RELEASE_NOTES_ENABLED_DB_KEY, ""),
            default=DEFAULT_RELEASE_NOTES_ENABLED,
        )
    return ManagedSettingEntry(
        key=MANAGED_RELEASE_NOTES_ENABLED_KEY,
        value=_format_bool(release_notes_enabled),
        default_value=_format_bool(DEFAULT_RELEASE_NOTES_ENABLED),
        source="configured" if release_notes_configured else "default",
        editable=not release_notes_disabled_reason,
        allowed_values=list(RELEASE_NOTES_ENABLED_VALUES),
        restart_required=False,
        disabled_reason=release_notes_disabled_reason,
    )


def _release_notification_entries(
    values: Mapping[str, str],
    settings: WebSettings,
) -> list[ManagedSettingEntry]:
    release_notification_config = _release_notification_config_from_values(values)
    return [
        ManagedSettingEntry(
            key=MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_KEY,
            value=release_notification_config.delivery_mode,
            default_value=DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE,
            source=(
                "configured"
                if MANAGED_RELEASE_NOTIFICATIONS_DELIVERY_MODE_DB_KEY in values
                else "default"
            ),
            editable=True,
            allowed_values=list(RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES),
            restart_required=False,
        ),
        ManagedSettingEntry(
            key=MANAGED_RELEASE_NOTIFICATIONS_MODE_KEY,
            value=release_notification_config.mode,
            default_value=DEFAULT_RELEASE_NOTIFICATIONS_MODE,
            source=(
                "configured"
                if MANAGED_RELEASE_NOTIFICATIONS_MODE_DB_KEY in values
                else "default"
            ),
            editable=True,
            allowed_values=list(RELEASE_NOTIFICATIONS_MODE_VALUES),
            restart_required=False,
        ),
        ManagedSettingEntry(
            key=MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_KEY,
            value=release_notification_config.resend_policy,
            default_value=DEFAULT_RELEASE_NOTIFICATIONS_RESEND_POLICY,
            source=(
                "configured"
                if MANAGED_RELEASE_NOTIFICATIONS_RESEND_POLICY_DB_KEY in values
                else "default"
            ),
            editable=True,
            allowed_values=list(RELEASE_NOTIFICATIONS_RESEND_POLICY_VALUES),
            restart_required=False,
        ),
        ManagedSettingEntry(
            key=MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_KEY,
            value=str(release_notification_config.cooldown_seconds),
            default_value=str(DEFAULT_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS),
            source=(
                "configured"
                if MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_DB_KEY in values
                else "default"
            ),
            editable=True,
            allowed_values=[],
            restart_required=False,
        ),
        _discord_webhook_entry(values, settings),
        ManagedSettingEntry(
            key=MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_KEY,
            value=release_notification_config.verbosity,
            default_value=DEFAULT_RELEASE_NOTIFICATIONS_VERBOSITY,
            source=(
                "configured"
                if MANAGED_RELEASE_NOTIFICATIONS_VERBOSITY_DB_KEY in values
                else "default"
            ),
            editable=True,
            allowed_values=list(RELEASE_NOTIFICATIONS_VERBOSITY_VALUES),
            restart_required=False,
        ),
    ]


def _discord_webhook_entry(
    values: Mapping[str, str],
    settings: WebSettings,
) -> ManagedSettingEntry:
    webhook_disabled_reason = _discord_webhook_disabled_reason(settings)
    env_webhook, _env_webhook_source = _discord_webhook_env(settings)
    stored_webhook = values.get(
        MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
        "",
    )
    webhook_configured = bool(env_webhook or stored_webhook)
    if stored_webhook and not env_webhook:
        _validated_discord_webhook(stored_webhook)
    return ManagedSettingEntry(
        key=MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY,
        value="",
        default_value="",
        source="configured" if webhook_configured else "default",
        editable=not webhook_disabled_reason,
        allowed_values=[],
        restart_required=False,
        disabled_reason=webhook_disabled_reason,
        configured=webhook_configured,
        sensitive=True,
    )


def _validated_managed_setting_updates(
    payload: ManagedSettingsUpdateRequest,
    settings: WebSettings,
) -> dict[str, str]:
    if not payload.values:
        raise HTTPException(
            status_code=422,
            detail="at least one managed setting is required",
        )

    updates: dict[str, str] = {}
    for key, raw_value in payload.values.items():
        updates[key] = _validated_managed_setting_update(key, raw_value, settings)
    return updates


def _validated_managed_setting_update(
    key: str,
    raw_value: str,
    settings: WebSettings,
) -> str:
    if key == MANAGED_COMPOSE_IGNORE_PATHS_KEY:
        return _validated_compose_ignore_paths_update(raw_value, settings)
    _raise_if_setting_locked_by_env(key, settings)
    if key == MANAGED_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS_KEY:
        return _validated_cooldown_seconds_update(key, raw_value)
    if key == MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY:
        return _validated_discord_webhook_update(raw_value, settings)
    return _validated_choice_setting_update(key, raw_value)


def _validated_compose_ignore_paths_update(
    raw_value: str,
    settings: WebSettings,
) -> str:
    if _compose_ignore_env_configured(settings):
        raise HTTPException(
            status_code=422,
            detail=_compose_ignore_paths_disabled_reason(settings),
        )
    try:
        return format_compose_ignore_paths(
            parse_compose_ignore_paths(
                raw_value.strip(),
                name=MANAGED_COMPOSE_IGNORE_PATHS_KEY,
            )
        )
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _raise_if_setting_locked_by_env(key: str, settings: WebSettings) -> None:
    if key == MANAGED_DIGEST_PIN_UPDATES_KEY and _digest_pin_env_configured(settings):
        raise HTTPException(
            status_code=422,
            detail=_digest_pin_disabled_reason(settings),
        )
    if (
        key == MANAGED_RELEASE_NOTES_ENABLED_KEY
        and _release_notes_enabled_env_configured(settings)
    ):
        raise HTTPException(
            status_code=422,
            detail=_release_notes_enabled_disabled_reason(settings),
        )


def _validated_cooldown_seconds_update(key: str, raw_value: str) -> str:
    try:
        return str(
            _parse_positive_int_setting(
                key,
                raw_value,
                DEFAULT_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS,
            )
        )
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validated_discord_webhook_update(
    raw_value: str,
    settings: WebSettings,
) -> str:
    disabled_reason = _discord_webhook_disabled_reason(settings)
    if disabled_reason:
        raise HTTPException(status_code=422, detail=disabled_reason)
    value = raw_value.strip()
    try:
        return "" if not value else _validated_discord_webhook(value)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validated_choice_setting_update(key: str, raw_value: str) -> str:
    allowed_values = _MANAGED_SETTING_ALLOWED_VALUES.get(key)
    if allowed_values is None:
        raise HTTPException(
            status_code=422,
            detail=f"managed setting is not editable: {key}",
        )
    value = raw_value.strip()
    if value not in allowed_values:
        options = ", ".join(allowed_values)
        raise HTTPException(
            status_code=422,
            detail=f"{key} must be one of: {options}",
        )
    return value


def _apply_managed_setting_updates(
    conn: sqlite3.Connection,
    updates: Mapping[str, str],
) -> None:
    for key, value in updates.items():
        _apply_managed_setting_update(conn, key, value)


def _apply_managed_setting_update(
    conn: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    if key == MANAGED_ONBOARDING_CHECKLIST_KEY:
        _apply_onboarding_checklist_update(conn, value)
    elif key == MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_KEY:
        _apply_discord_webhook_update(conn, value)
    elif db_key := _MANAGED_SETTING_DB_KEYS.get(key):
        _set_web_setting(conn, db_key, value)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"managed setting has no storage mapping: {key}",
        )


def _apply_onboarding_checklist_update(conn: sqlite3.Connection, value: str) -> None:
    if value == "dismissed":
        current = _web_setting(conn, ONBOARDING_DISMISSED_AT_KEY)
        _set_web_setting(
            conn,
            ONBOARDING_DISMISSED_AT_KEY,
            current or utc_timestamp(),
        )
    else:
        _delete_web_setting(conn, ONBOARDING_DISMISSED_AT_KEY)


def _apply_discord_webhook_update(conn: sqlite3.Connection, value: str) -> None:
    if value:
        _set_web_setting(
            conn,
            MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
            value,
        )
    else:
        _delete_web_setting(
            conn,
            MANAGED_RELEASE_NOTIFICATIONS_DISCORD_WEBHOOK_DB_KEY,
        )


def _managed_settings_audit_values(
    entries: Sequence[ManagedSettingEntry],
) -> dict[str, str]:
    return {
        entry.key: (
            "configured" if entry.sensitive and entry.configured else entry.value
        )
        for entry in entries
    }


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


def _updater_settings_entries(settings: WebSettings) -> list[SettingsEntry]:
    config = settings.config
    release_notes_enabled, release_notes_configured = (
        _effective_release_notes_enabled_state(settings)
    )
    return [
        _config_setting_entry(settings, "DOCKER_BASE", str(config.docker_base)),
        _settings_entry(
            "HOST_DOCKER_BASE",
            "" if settings.host_docker_base is None else str(settings.host_docker_base),
            "",
            _env_configured(settings, "HOST_DOCKER_BASE"),
        ),
        _config_setting_entry(settings, "WUD_OUT_FILE", str(config.wud_out_file)),
        _config_setting_entry(settings, "WUD_LOG_DIR", str(config.log_dir)),
        _config_setting_entry(settings, "WUD_DB_PATH", str(config.db_path)),
        _config_setting_entry(settings, "WUD_UPDATE_MODE", config.update_mode),
        _config_setting_entry(settings, "WUD_MAX_WAIT", str(config.max_wait)),
        _config_setting_entry(settings, "WUD_LOCK_TIMEOUT", str(config.lock_timeout)),
        _config_setting_entry(settings, "WUD_TIMEZONE", config.timezone_name),
        _config_setting_entry(
            settings,
            COMPOSE_IGNORE_PATHS_ENV,
            format_compose_ignore_paths(config.compose_ignore_paths),
        ),
        _config_setting_entry(
            settings,
            DIGEST_PIN_UPDATES_ENV,
            _format_bool(config.digest_pin_updates),
        ),
        _settings_entry(
            RELEASE_NOTES_ENABLED_ENV,
            _format_bool(release_notes_enabled),
            _format_bool(DEFAULT_RELEASE_NOTES_ENABLED),
            release_notes_configured,
        ),
    ]


def _webui_settings_entries(
    settings: WebSettings,
    request: Request,
) -> list[SettingsEntry]:
    env = _settings_env(settings)
    bind_host = env.get("WUD_WEB_HOST", DEFAULT_WEB_HOST)
    default_allowed_hosts = _parse_allowed_hosts(
        "",
        public_origin=settings.public_origin,
        bind_host=bind_host,
    )
    default_static_settings = cast(
        WebSettings,
        replace(
            settings,
            static_dir=_resolve_static_dir(None),
        ),
    )
    default_secure_settings = cast(
        WebSettings,
        replace(settings, secure_cookies="auto"),
    )
    return [
        _settings_entry(
            "WUD_WEB_AUTH_REQUIRED",
            _format_bool(settings.auth_required),
            "true",
            False,
            source="derived",
        ),
        _settings_entry(
            "WUD_WEB_DEV_NO_AUTH",
            _format_bool(settings.dev_no_auth),
            "false",
            _env_configured(settings, "WUD_WEB_DEV_NO_AUTH"),
        ),
        _settings_entry(
            "WUD_WEB_PUBLIC_ORIGIN",
            settings.public_origin,
            "",
            _env_configured(settings, "WUD_WEB_PUBLIC_ORIGIN"),
        ),
        _settings_entry(
            "WUD_WEB_ALLOWED_ORIGINS",
            _format_sequence(sorted(settings.allowed_origins)),
            "",
            _env_configured(settings, "WUD_WEB_ALLOWED_ORIGINS"),
        ),
        _settings_entry(
            "WUD_WEB_ALLOWED_HOSTS",
            _format_sequence(sorted(settings.allowed_hosts)),
            _format_sequence(sorted(default_allowed_hosts)),
            _env_configured(settings, "WUD_WEB_ALLOWED_HOSTS"),
            source="derived",
        ),
        _settings_entry(
            "WUD_WEB_TRUSTED_PROXIES",
            _format_sequence(str(network) for network in settings.trusted_proxies),
            "",
            _env_configured(settings, "WUD_WEB_TRUSTED_PROXIES"),
        ),
        _settings_entry(
            "WUD_WEB_SECURE_COOKIES",
            settings.secure_cookies,
            "auto",
            _env_configured(settings, "WUD_WEB_SECURE_COOKIES"),
        ),
        _settings_entry(
            "WUD_WEB_SECURE_COOKIES_EFFECTIVE",
            _format_bool(_secure_cookie(settings, request)),
            _format_bool(_secure_cookie(default_secure_settings, request)),
            False,
            source="request",
        ),
        _settings_entry(
            "WUD_WEB_STATIC_SPA_AVAILABLE",
            _format_bool(_static_spa_available(settings)),
            _format_bool(_static_spa_available(default_static_settings)),
            _env_configured(settings, "WUD_WEB_STATIC_DIR"),
            source="derived",
        ),
        _settings_entry(
            web_wud_api.WUD_API_BASE_URL_ENV,
            settings.wud_api_base_url,
            web_wud_api.DEFAULT_WUD_API_BASE_URL,
            _env_configured(settings, web_wud_api.WUD_API_BASE_URL_ENV),
        ),
        _settings_entry(
            web_wud_api.WUD_API_STARTUP_WAIT_SECONDS_ENV,
            web_wud_api.format_startup_wait_seconds(
                settings.wud_api_startup_wait_seconds
            ),
            web_wud_api.format_startup_wait_seconds(
                web_wud_api.DEFAULT_WUD_API_STARTUP_WAIT_SECONDS
            ),
            _env_configured(
                settings,
                web_wud_api.WUD_API_STARTUP_WAIT_SECONDS_ENV,
            ),
        ),
        _settings_entry(
            web_wud_api.WUD_API_AUTH_BASIC_USER_ENV,
            env.get(web_wud_api.WUD_API_AUTH_BASIC_USER_ENV, ""),
            "",
            _env_configured(settings, web_wud_api.WUD_API_AUTH_BASIC_USER_ENV),
        ),
        _settings_entry(
            web_wud_api.WUD_API_HEADERS_FILE_ENV,
            env.get(web_wud_api.WUD_API_HEADERS_FILE_ENV, ""),
            "",
            _env_configured(settings, web_wud_api.WUD_API_HEADERS_FILE_ENV),
        ),
        _settings_entry(
            web_pending_sources.PENDING_SOURCE_ENV,
            settings.pending_source,
            web_pending_sources.DEFAULT_PENDING_SOURCE,
            _env_configured(settings, web_pending_sources.PENDING_SOURCE_ENV),
            source="derived" if not settings.legacy_scripts_enabled else None,
        ),
        _settings_entry(
            LEGACY_SCRIPTS_ENV,
            _format_bool(settings.legacy_scripts_enabled),
            "true",
            _env_configured(settings, LEGACY_SCRIPTS_ENV),
        ),
        _settings_entry(
            "WUD_WEB_MUTATIONS_ENABLED",
            _format_bool(settings.mutations_enabled),
            "false",
            _env_configured(settings, "WUD_WEB_MUTATIONS_ENABLED"),
        ),
        _settings_entry(
            "WUD_WEB_RESTART_CONTAINER",
            settings.restart_container,
            "",
            _env_configured(settings, "WUD_WEB_RESTART_CONTAINER"),
            source="derived" if settings.restart_container else None,
        ),
        _settings_entry(
            "WUD_WEB_AUTO_UPDATE_SCHEDULER_ENABLED",
            _format_bool(settings.mutations_enabled),
            "false",
            False,
            source="derived",
        ),
    ]


def _secret_settings(settings: WebSettings) -> list[SecretSettingStatus]:
    env = _settings_env(settings)
    return [
        SecretSettingStatus(
            name=name,
            configured=bool(settings.auth_token.strip())
            if name == "WUD_WEB_TOKEN"
            else bool(env.get(name, "").strip()),
        )
        for name in SENSITIVE_ENV_KEYS
    ]


def _config_setting_entry(
    settings: WebSettings,
    name: str,
    value: str,
) -> SettingsEntry:
    configured = _env_configured(settings, name)
    return _settings_entry(
        name,
        value,
        _config_default_value(settings, name),
        configured,
    )


def _settings_entry(
    name: str,
    value: str,
    default_value: str,
    configured: bool,
    *,
    source: SettingsEntrySource | None = None,
) -> SettingsEntry:
    return SettingsEntry(
        name=name,
        value=value,
        default_value=default_value,
        configured=configured,
        source="configured" if configured else source or "default",
    )


def _config_default_value(settings: WebSettings, name: str) -> str:
    env = dict(_settings_env(settings))
    env.pop(name, None)
    try:
        config = load_config(env)
    except ConfigError:
        return _static_config_default(name)
    return _config_value(config, name)


def _static_config_default(name: str) -> str:
    defaults = {
        "WUD_UPDATE_MODE": DEFAULT_UPDATE_MODE,
        "WUD_MAX_WAIT": str(DEFAULT_MAX_WAIT),
        "WUD_LOCK_TIMEOUT": str(DEFAULT_LOCK_TIMEOUT),
        "WUD_TIMEZONE": DEFAULT_TIMEZONE,
        COMPOSE_IGNORE_PATHS_ENV: format_compose_ignore_paths(
            DEFAULT_COMPOSE_IGNORE_PATHS
        ),
        DIGEST_PIN_UPDATES_ENV: _format_bool(DEFAULT_DIGEST_PIN_UPDATES),
    }
    return defaults.get(name, "")


def _config_value(config: UpdaterConfig, name: str) -> str:
    values = {
        "DOCKER_BASE": str(config.docker_base),
        "WUD_OUT_FILE": str(config.wud_out_file),
        "WUD_LOG_DIR": str(config.log_dir),
        "WUD_DB_PATH": str(config.db_path),
        "WUD_UPDATE_MODE": config.update_mode,
        "WUD_MAX_WAIT": str(config.max_wait),
        "WUD_LOCK_TIMEOUT": str(config.lock_timeout),
        "WUD_TIMEZONE": config.timezone_name,
        COMPOSE_IGNORE_PATHS_ENV: format_compose_ignore_paths(
            config.compose_ignore_paths
        ),
        DIGEST_PIN_UPDATES_ENV: _format_bool(config.digest_pin_updates),
    }
    return values.get(name, "")


def _settings_env(settings: WebSettings) -> Mapping[str, str]:
    return settings.command_env or {}


def _env_configured(settings: WebSettings, name: str) -> bool:
    if name in {COMPOSE_IGNORE_PATHS_ENV, DIGEST_PIN_UPDATES_ENV}:
        return name in _settings_env(settings)
    return bool(_settings_env(settings).get(name, "").strip())


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_sequence(values: Sequence[str] | Iterator[str]) -> str:
    return ", ".join(item for item in values if item)
