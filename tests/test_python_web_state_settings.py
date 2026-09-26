from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _setup_admin,
    _store_web_setting,
)

from wudup import web_effective_settings as effective_settings_module
from wudup import web_settings as settings_module
from wudup import web_state as state_module
from wudup.db import (
    open_db,
)


def test_discord_webhook_policy_loader_falls_back_when_file_is_missing(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(effective_settings_module, "files", lambda _package: tmp_path)

    with caplog.at_level(logging.WARNING, logger=effective_settings_module.LOGGER.name):
        hosts, path_prefix = effective_settings_module._load_discord_webhook_policy()

    assert hosts == frozenset(effective_settings_module._DEFAULT_DISCORD_WEBHOOK_ALLOWED_HOSTS)
    assert path_prefix == effective_settings_module._DEFAULT_DISCORD_WEBHOOK_PATH_PREFIX
    assert "using fallback Discord webhook policy" in caplog.text


def test_discord_webhook_policy_loader_falls_back_when_file_is_malformed(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(effective_settings_module, "files", lambda _package: tmp_path)
    (tmp_path / "discord_webhook_policy.json").write_text("{", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=effective_settings_module.LOGGER.name):
        hosts, path_prefix = effective_settings_module._load_discord_webhook_policy()

    assert hosts == frozenset(effective_settings_module._DEFAULT_DISCORD_WEBHOOK_ALLOWED_HOSTS)
    assert path_prefix == effective_settings_module._DEFAULT_DISCORD_WEBHOOK_PATH_PREFIX
    assert "using fallback Discord webhook policy" in caplog.text


def test_state_read_database_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "github-secret-token"
    leaked_path = tmp_path / "state" / "wud.sqlite"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "GITHUB_TOKEN": secret,
        },
    )

    def fail_connect(_settings: object) -> object:
        raise OSError(f"cannot open {leaked_path} with {secret}")

    monkeypatch.setattr(state_module, "_connect_readonly_db", fail_connect)

    response = client.get("/api/v1/service-policies")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("could not read database: ")
    assert str(leaked_path) not in detail
    assert secret not in detail
    assert "[REDACTED_PATH]" in detail
    assert "<redacted>" in detail


def test_settings_rejects_unauthenticated_requests_without_dev_bypass(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/settings")

    assert response.status_code == 403
    assert response.json()["detail"] == "setup required"


def test_settings_reports_effective_non_secret_configuration(
    tmp_path: Path,
) -> None:
    wud_api_headers_file = tmp_path / "wud-api-headers.json"
    wud_api_headers_file.write_text(
        '{"X-Api-Key": "wud-api-header-secret"}',
        encoding="utf-8",
    )
    secret_values = {
        "WUD_WEB_TOKEN": "web-token-secret",
        "WUD_API_AUTH_BEARER_TOKEN": "wud-api-token-secret",
        "GITHUB_TOKEN": "github-token-secret",
        "DISCORD_WEBHOOK": "discord-webhook-secret",
        "ADMIN_WEBHOOK": "admin-webhook-secret",
    }
    client = _client(
        tmp_path,
        {
            "DOCKER_BASE": str(tmp_path / "docker-root"),
            "HOST_DOCKER_BASE": str(tmp_path / "docker-root"),
            "WUD_OUT_FILE": str(tmp_path / "state" / "custom.todo"),
            "WUD_LOG_DIR": str(tmp_path / "state" / "custom-logs"),
            "WUD_MAX_WAIT": "240",
            "WUD_WEB_PUBLIC_ORIGIN": "https://wud.example.test",
            "WUD_WEB_ALLOWED_ORIGINS": "http://testserver",
            "WUD_WEB_ALLOWED_HOSTS": "testserver,wud.example.test,updates.example.test",
            "WUD_WEB_TRUSTED_PROXIES": "127.0.0.1/32",
            "WUD_WEB_SECURE_COOKIES": "false",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            "WUD_API_BASE_URL": "http://wud.internal:3000",
            "WUD_API_STARTUP_WAIT_SECONDS": "5",
            "WUD_API_HEADERS_FILE": str(wud_api_headers_file),
            "WUD_PENDING_SOURCE": "auto",
            **secret_values,
        },
    )
    _setup_admin(client)

    response = client.get("/api/v1/settings")
    body = response.json()
    updater = {entry["name"]: entry for entry in body["updater"]}
    webui = {entry["name"]: entry for entry in body["webui"]}
    secrets = {entry["name"]: entry for entry in body["secrets"]}
    serialized = json.dumps(body)

    assert response.status_code == 200
    managed = {entry["key"]: entry for entry in body["managed"]}

    assert set(body) == {"updater", "webui", "secrets", "managed"}
    assert updater["DOCKER_BASE"]["value"] == str(tmp_path / "docker-root")
    assert updater["DOCKER_BASE"]["configured"] is True
    assert updater["DOCKER_BASE"]["source"] == "configured"
    assert updater["WUD_OUT_FILE"]["default_value"] == str(
        tmp_path / "docker-root" / "wud" / "out" / "images.todo"
    )
    assert updater["WUD_MAX_WAIT"]["value"] == "240"
    assert updater["WUD_MAX_WAIT"]["source"] == "configured"
    assert updater["WUD_LOCK_TIMEOUT"] == {
        "name": "WUD_LOCK_TIMEOUT",
        "value": "30",
        "default_value": "30",
        "configured": False,
        "source": "default",
    }
    assert updater["WUD_RELEASE_NOTES_ENABLED"] == {
        "name": "WUD_RELEASE_NOTES_ENABLED",
        "value": "false",
        "default_value": "false",
        "configured": False,
        "source": "default",
    }
    assert webui["WUD_WEB_PUBLIC_ORIGIN"]["value"] == "https://wud.example.test"
    assert webui["WUD_WEB_MUTATIONS_ENABLED"]["value"] == "true"
    assert webui["WUD_WEB_RESTART_CONTAINER"]["value"] == "wudup"
    assert webui["WUD_WEB_RESTART_CONTAINER"]["source"] == "configured"
    assert webui["WUD_API_BASE_URL"] == {
        "name": "WUD_API_BASE_URL",
        "value": "http://wud.internal:3000",
        "default_value": "http://wud:3000",
        "configured": True,
        "source": "configured",
    }
    assert webui["WUD_API_STARTUP_WAIT_SECONDS"] == {
        "name": "WUD_API_STARTUP_WAIT_SECONDS",
        "value": "5",
        "default_value": "0",
        "configured": True,
        "source": "configured",
    }
    assert webui["WUD_API_AUTH_BASIC_USER"] == {
        "name": "WUD_API_AUTH_BASIC_USER",
        "value": "",
        "default_value": "",
        "configured": False,
        "source": "default",
    }
    assert webui["WUD_API_HEADERS_FILE"] == {
        "name": "WUD_API_HEADERS_FILE",
        "value": str(wud_api_headers_file),
        "default_value": "",
        "configured": True,
        "source": "configured",
    }
    assert webui["WUD_PENDING_SOURCE"] == {
        "name": "WUD_PENDING_SOURCE",
        "value": "auto",
        "default_value": "api",
        "configured": True,
        "source": "configured",
    }
    assert webui["WUDUP_LEGACY_SCRIPTS"] == {
        "name": "WUDUP_LEGACY_SCRIPTS",
        "value": "true",
        "default_value": "true",
        "configured": False,
        "source": "default",
    }
    assert webui["WUD_WEB_SECURE_COOKIES"]["value"] == "false"
    assert webui["WUD_WEB_SECURE_COOKIES_EFFECTIVE"]["value"] == "false"
    assert webui["WUD_WEB_SECURE_COOKIES_EFFECTIVE"]["source"] == "request"
    assert webui["WUD_WEB_AUTO_UPDATE_SCHEDULER_ENABLED"]["value"] == "true"
    assert secrets["WUD_WEB_TOKEN"]["configured"] is True
    assert secrets["WUD_API_AUTH_BEARER_TOKEN"]["configured"] is True
    assert secrets["WUD_API_AUTH_BEARER_TOKEN_FILE"]["configured"] is False
    assert "WUDUP_TRIGGER_TOKEN" not in secrets
    assert "WUDUP_TRIGGER_TOKEN_FILE" not in secrets
    assert "WUD_API_AUTH_BASIC_USER" not in secrets
    assert secrets["WUD_API_AUTH_BASIC_PASSWORD"]["configured"] is False
    assert secrets["WUD_API_AUTH_BASIC_PASSWORD_FILE"]["configured"] is False
    assert "WUD_API_HEADERS_FILE" not in secrets
    assert secrets["GITHUB_TOKEN"]["configured"] is True
    assert secrets["DISCORD_WEBHOOK"]["configured"] is True
    assert secrets["ADMIN_WEBHOOK"]["configured"] is True
    assert managed["theme_preference"] == {
        "key": "theme_preference",
        "value": "system",
        "default_value": "system",
        "source": "default",
        "editable": True,
        "allowed_values": ["system", "light", "dark"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["onboarding_checklist"] == {
        "key": "onboarding_checklist",
        "value": "visible",
        "default_value": "visible",
        "source": "default",
        "editable": True,
        "allowed_values": ["visible", "dismissed"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["compose_ignore_paths"] == {
        "key": "compose_ignore_paths",
        "value": "",
        "default_value": "",
        "source": "default",
        "editable": True,
        "allowed_values": [],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["digest_pin_updates"] == {
        "key": "digest_pin_updates",
        "value": "false",
        "default_value": "false",
        "source": "default",
        "editable": True,
        "allowed_values": ["false", "true"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["retag_digest_pins"] == {
        "key": "retag_digest_pins",
        "value": "false",
        "default_value": "false",
        "source": "default",
        "editable": True,
        "allowed_values": ["false", "true"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["release_notes_enabled"] == {
        "key": "release_notes_enabled",
        "value": "false",
        "default_value": "false",
        "source": "default",
        "editable": True,
        "allowed_values": ["false", "true"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    assert managed["release_notifications_discord_webhook"] == {
        "key": "release_notifications_discord_webhook",
        "value": "",
        "default_value": "",
        "source": "configured",
        "editable": False,
        "allowed_values": [],
        "restart_required": False,
        "disabled_reason": (
            "DISCORD_WEBHOOK is configured in the server environment. "
            "Unset it to manage the Discord webhook in the WebUI."
        ),
        "configured": True,
        "sensitive": True,
    }
    assert managed["release_notifications_verbosity"] == {
        "key": "release_notifications_verbosity",
        "value": "summary",
        "default_value": "summary",
        "source": "default",
        "editable": True,
        "allowed_values": ["summary", "full"],
        "restart_required": False,
        "disabled_reason": "",
        "configured": False,
        "sensitive": False,
    }
    for value in (*secret_values.values(), "wud-api-header-secret"):
        assert value not in serialized


def test_settings_release_notes_entry_reports_managed_source(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    _store_web_setting(tmp_path, "release_notes.enabled", "true")

    response = client.get("/api/v1/settings")
    updater = {entry["name"]: entry for entry in response.json()["updater"]}

    assert response.status_code == 200
    assert updater["WUD_RELEASE_NOTES_ENABLED"] == {
        "name": "WUD_RELEASE_NOTES_ENABLED",
        "value": "true",
        "default_value": "false",
        "configured": True,
        "source": "configured",
    }


def test_settings_wraps_invalid_managed_setting_config_error(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    _store_web_setting(tmp_path, "compose.digest_pin_updates", "maybe")

    response = client.get("/api/v1/settings")
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not read managed settings: ")
    assert "true or false" in detail


def test_managed_settings_endpoint_enforces_auth_csrf_read_only_and_post(
    tmp_path: Path,
) -> None:
    payload = {"values": {"theme_preference": "dark"}}
    unauthenticated = _client(tmp_path, {"WUD_WEB_MUTATIONS_ENABLED": "true"})
    read_only = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    mutating = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )

    unauthenticated_response = unauthenticated.post(
        "/api/v1/settings/managed",
        json=payload,
        headers=_csrf_headers(unauthenticated),
    )
    missing_csrf = mutating.post("/api/v1/settings/managed", json=payload)
    read_only_response = read_only.post(
        "/api/v1/settings/managed",
        json=payload,
        headers=_csrf_headers(read_only),
    )
    get_response = mutating.get("/api/v1/settings/managed")

    assert unauthenticated_response.status_code == 403
    assert unauthenticated_response.json()["detail"] == "setup required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"
    assert get_response.status_code == 405


def test_managed_settings_rejects_uneditable_or_invalid_values_without_partial_write(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)

    invalid_key = client.post(
        "/api/v1/settings/managed",
        json={"values": {"theme_preference": "dark", "WUD_WEB_TOKEN": "secret"}},
        headers=headers,
    )
    path_key = client.post(
        "/api/v1/settings/managed",
        json={"values": {"DOCKER_BASE": "/srv/docker"}},
        headers=headers,
    )
    command_value = client.post(
        "/api/v1/settings/managed",
        json={"values": {"theme_preference": "dark; rm -rf /"}},
        headers=headers,
    )
    invalid_compose_ignore = client.post(
        "/api/v1/settings/managed",
        json={"values": {"compose_ignore_paths": "old,,archive"}},
        headers=headers,
    )
    invalid_release_notes = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notes_enabled": "maybe"}},
        headers=headers,
    )
    invalid_notification_delivery_mode = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notifications_delivery_mode": "trigger"}},
        headers=headers,
    )
    invalid_notification_mode = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notifications_mode": "automatic"}},
        headers=headers,
    )
    invalid_notification_cooldown = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notifications_cooldown_seconds": "0"}},
        headers=headers,
    )
    invalid_notification_webhook = client.post(
        "/api/v1/settings/managed",
        json={
            "values": {
                "release_notifications_discord_webhook": (
                    "https://discord.com/api/not-webhooks/123/token-secret"
                )
            }
        },
        headers=headers,
    )
    partial_notification_webhook = client.post(
        "/api/v1/settings/managed",
        json={
            "values": {
                "release_notifications_discord_webhook": (
                    "https://discord.com/api/webhooks/123"
                )
            }
        },
        headers=headers,
    )
    prefix_notification_webhook = client.post(
        "/api/v1/settings/managed",
        json={
            "values": {
                "release_notifications_discord_webhook": (
                    "https://discord.com/api/webhooks/"
                )
            }
        },
        headers=headers,
    )
    invalid_notification_verbosity = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notifications_verbosity": "verbose"}},
        headers=headers,
    )
    empty_payload = client.post(
        "/api/v1/settings/managed",
        json={"values": {}},
        headers=headers,
    )
    settings_response = client.get("/api/v1/settings")
    managed = {entry["key"]: entry for entry in settings_response.json()["managed"]}

    assert invalid_key.status_code == 422
    assert invalid_key.json()["detail"] == "managed setting is not editable: WUD_WEB_TOKEN"
    assert path_key.status_code == 422
    assert path_key.json()["detail"] == "managed setting is not editable: DOCKER_BASE"
    assert command_value.status_code == 422
    assert command_value.json()["detail"] == (
        "theme_preference must be one of: system, light, dark"
    )
    assert invalid_compose_ignore.status_code == 422
    assert "non-empty relative paths" in invalid_compose_ignore.json()["detail"]
    assert invalid_release_notes.status_code == 422
    assert invalid_release_notes.json()["detail"] == (
        "release_notes_enabled must be one of: false, true"
    )
    assert invalid_notification_delivery_mode.status_code == 422
    assert invalid_notification_delivery_mode.json()["detail"] == (
        "release_notifications_delivery_mode must be one of: "
        f"{', '.join(settings_module.RELEASE_NOTIFICATIONS_DELIVERY_MODE_VALUES)}"
    )
    assert invalid_notification_mode.status_code == 422
    assert invalid_notification_mode.json()["detail"] == (
        "release_notifications_mode must be one of: digest, per_container"
    )
    assert invalid_notification_cooldown.status_code == 422
    assert invalid_notification_cooldown.json()["detail"] == (
        "release_notifications_cooldown_seconds must be a positive integer"
    )
    assert invalid_notification_webhook.status_code == 422
    assert invalid_notification_webhook.json()["detail"] == (
        "release_notifications_discord_webhook must be a Discord webhook URL"
    )
    assert partial_notification_webhook.status_code == 422
    assert partial_notification_webhook.json()["detail"] == (
        "release_notifications_discord_webhook must be a Discord webhook URL"
    )
    assert prefix_notification_webhook.status_code == 422
    assert prefix_notification_webhook.json()["detail"] == (
        "release_notifications_discord_webhook must be a Discord webhook URL"
    )
    assert invalid_notification_verbosity.status_code == 422
    assert invalid_notification_verbosity.json()["detail"] == (
        "release_notifications_verbosity must be one of: summary, full"
    )
    assert empty_payload.status_code == 422
    assert empty_payload.json()["detail"] == "at least one managed setting is required"
    assert managed["theme_preference"]["value"] == "system"
    assert managed["theme_preference"]["source"] == "default"


def test_managed_settings_endpoint_uses_settings_module_validation_seam(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)

    def fail_validation(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise HTTPException(status_code=409, detail="web validation seam used")

    monkeypatch.setattr(
        settings_module,
        "_validated_managed_setting_updates",
        fail_validation,
    )

    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"theme_preference": "dark"}},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "web validation seam used"


def test_managed_settings_update_wraps_invalid_existing_config_error(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    _store_web_setting(tmp_path, "compose.digest_pin_updates", "maybe")

    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"theme_preference": "dark"}},
        headers=_csrf_headers(client),
    )
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not read managed settings: ")
    assert "true or false" in detail


def test_managed_settings_update_fails_on_unmapped_editable_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    monkeypatch.setitem(
        settings_module._MANAGED_SETTING_ALLOWED_VALUES,
        "future_setting",
        ("enabled",),
    )

    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"future_setting": "enabled"}},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "managed setting has no storage mapping: future_setting"
    )


def test_effective_config_wraps_invalid_stored_compose_ignore_paths_error(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    _store_web_setting(tmp_path, "compose.ignore_paths", "old,,archive")

    try:
        effective_settings_module._effective_config(client.app.state.web_settings)
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail.startswith("stored compose_ignore_paths is invalid: ")
        assert "non-empty relative paths" in exc.detail
    else:
        raise AssertionError("expected invalid stored compose ignore paths to fail")


def test_effective_config_wraps_invalid_stored_digest_pin_error(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    _store_web_setting(tmp_path, "compose.digest_pin_updates", "maybe")

    try:
        effective_settings_module._effective_config(client.app.state.web_settings)
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail.startswith("stored digest_pin_updates is invalid: ")
        assert "true or false" in exc.detail
    else:
        raise AssertionError("expected invalid stored digest-pin setting to fail")


def test_managed_digest_pin_updates_env_guard_disables_webui_edit(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_DIGEST_PIN_UPDATES": "true",
        },
    )
    headers = _csrf_headers(client)

    settings_response = client.get("/api/v1/settings")
    managed = {entry["key"]: entry for entry in settings_response.json()["managed"]}
    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"digest_pin_updates": "false"}},
        headers=headers,
    )

    assert managed["digest_pin_updates"]["value"] == "true"
    assert managed["digest_pin_updates"]["editable"] is False
    assert "Unset it to manage digest-pin updates" in managed[
        "digest_pin_updates"
    ]["disabled_reason"]
    assert response.status_code == 422
    assert response.json()["detail"] == managed["digest_pin_updates"]["disabled_reason"]


def test_managed_compose_ignore_paths_env_guard_disables_webui_edit(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_COMPOSE_IGNORE_PATHS": "old",
        },
    )
    headers = _csrf_headers(client)

    settings_response = client.get("/api/v1/settings")
    managed = {entry["key"]: entry for entry in settings_response.json()["managed"]}
    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"compose_ignore_paths": "archive"}},
        headers=headers,
    )

    assert managed["compose_ignore_paths"]["value"] == "old"
    assert managed["compose_ignore_paths"]["editable"] is False
    assert "Unset it to manage compose ignore paths" in managed[
        "compose_ignore_paths"
    ]["disabled_reason"]
    assert response.status_code == 422
    assert response.json()["detail"] == managed["compose_ignore_paths"]["disabled_reason"]


def test_managed_release_notes_env_guard_disables_webui_edit(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)

    settings_response = client.get("/api/v1/settings")
    managed = {entry["key"]: entry for entry in settings_response.json()["managed"]}
    response = client.post(
        "/api/v1/settings/managed",
        json={"values": {"release_notes_enabled": "false"}},
        headers=headers,
    )

    assert managed["release_notes_enabled"]["value"] == "true"
    assert managed["release_notes_enabled"]["editable"] is False
    assert "Unset it to manage release-note notifications" in managed[
        "release_notes_enabled"
    ]["disabled_reason"]
    assert response.status_code == 422
    assert response.json()["detail"] == managed["release_notes_enabled"][
        "disabled_reason"
    ]


def test_managed_discord_webhook_env_guard_disables_webui_edit(
    tmp_path: Path,
) -> None:
    webhook = "https://discord.com/api/webhooks/env/token-secret"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": webhook,
        },
    )
    headers = _csrf_headers(client)

    settings_response = client.get("/api/v1/settings")
    managed = {entry["key"]: entry for entry in settings_response.json()["managed"]}
    response = client.post(
        "/api/v1/settings/managed",
        json={
            "values": {
                "release_notifications_discord_webhook": (
                    "https://discord.com/api/webhooks/db/token-secret"
                )
            }
        },
        headers=headers,
    )
    webhook_entry = managed["release_notifications_discord_webhook"]

    assert webhook_entry["value"] == ""
    assert webhook_entry["configured"] is True
    assert webhook_entry["sensitive"] is True
    assert webhook_entry["editable"] is False
    assert "Unset it to manage the Discord webhook" in webhook_entry[
        "disabled_reason"
    ]
    assert webhook not in settings_response.text
    assert response.status_code == 422
    assert response.json()["detail"] == webhook_entry["disabled_reason"]


def test_managed_settings_persist_and_write_audit_records(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)
    webhook = "https://discord.com/api/webhooks/123/token-secret"

    response = client.post(
        "/api/v1/settings/managed",
        json={
            "values": {
                "theme_preference": "dark",
                "onboarding_checklist": "dismissed",
                "compose_ignore_paths": "old, archive/disabled",
                "digest_pin_updates": "true",
                "retag_digest_pins": "true",
                "release_notes_enabled": "true",
                "release_notifications_delivery_mode": (
                    settings_module.DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE
                ),
                "release_notifications_mode": "per_container",
                "release_notifications_resend_policy": "cooldown",
                "release_notifications_cooldown_seconds": "60",
                "release_notifications_discord_webhook": webhook,
                "release_notifications_verbosity": "full",
            }
        },
        headers=headers,
    )
    body = response.json()
    managed = {entry["key"]: entry for entry in body["managed"]}

    assert response.status_code == 200
    assert body["audit_run_id"]
    assert managed["theme_preference"]["value"] == "dark"
    assert managed["theme_preference"]["source"] == "configured"
    assert managed["onboarding_checklist"]["value"] == "dismissed"
    assert managed["onboarding_checklist"]["source"] == "configured"
    assert managed["compose_ignore_paths"]["value"] == "old, archive/disabled"
    assert managed["compose_ignore_paths"]["source"] == "configured"
    assert managed["digest_pin_updates"]["value"] == "true"
    assert managed["digest_pin_updates"]["source"] == "configured"
    assert managed["retag_digest_pins"]["value"] == "true"
    assert managed["retag_digest_pins"]["source"] == "configured"
    assert managed["release_notes_enabled"]["value"] == "true"
    assert managed["release_notes_enabled"]["source"] == "configured"
    assert managed["release_notifications_delivery_mode"]["value"] == (
        settings_module.DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE
    )
    assert managed["release_notifications_mode"]["value"] == "per_container"
    assert managed["release_notifications_resend_policy"]["value"] == "cooldown"
    assert managed["release_notifications_cooldown_seconds"]["value"] == "60"
    assert managed["release_notifications_discord_webhook"]["value"] == ""
    assert managed["release_notifications_discord_webhook"]["source"] == "configured"
    assert managed["release_notifications_discord_webhook"]["configured"] is True
    assert managed["release_notifications_discord_webhook"]["sensitive"] is True
    assert managed["release_notifications_verbosity"]["value"] == "full"
    assert webhook not in response.text

    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        theme = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'ui.theme_preference'"
        ).fetchone()
        onboarding = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'onboarding_checklist_dismissed_at'"
        ).fetchone()
        compose_ignore_paths = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'compose.ignore_paths'"
        ).fetchone()
        digest_pin_updates = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'compose.digest_pin_updates'"
        ).fetchone()
        retag_digest_pins = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'retag.digest_pins'"
        ).fetchone()
        release_notes_enabled = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'release_notes.enabled'"
        ).fetchone()
        release_notifications_delivery_mode = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = 'release_notifications.delivery_mode'
            """
        ).fetchone()
        release_notifications_mode = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'release_notifications.mode'"
        ).fetchone()
        release_notifications_resend_policy = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = 'release_notifications.resend_policy'
            """
        ).fetchone()
        release_notifications_cooldown = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = 'release_notifications.cooldown_seconds'
            """
        ).fetchone()
        release_notifications_webhook = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = 'release_notifications.discord_webhook'
            """
        ).fetchone()
        release_notifications_verbosity = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = 'release_notifications.verbosity'
            """
        ).fetchone()
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (body["audit_run_id"],),
        ).fetchone()

    run_metadata = json.loads(run["metadata_json"])
    event_metadata = json.loads(event["metadata_json"])
    assert theme["value"] == "dark"
    assert onboarding["value"]
    assert compose_ignore_paths["value"] == "old, archive/disabled"
    assert digest_pin_updates["value"] == "true"
    assert retag_digest_pins["value"] == "true"
    assert release_notes_enabled["value"] == "true"
    assert release_notifications_delivery_mode["value"] == (
        settings_module.DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE
    )
    assert release_notifications_mode["value"] == "per_container"
    assert release_notifications_resend_policy["value"] == "cooldown"
    assert release_notifications_cooldown["value"] == "60"
    assert release_notifications_webhook["value"] == webhook
    assert release_notifications_verbosity["value"] == "full"
    assert run["mode"] == "web-settings"
    assert run_metadata["operation"] == "update_managed_settings"
    assert run_metadata["target"] == {
        "keys": [
            "compose_ignore_paths",
            "digest_pin_updates",
            "onboarding_checklist",
            "release_notes_enabled",
            "release_notifications_cooldown_seconds",
            "release_notifications_delivery_mode",
            "release_notifications_discord_webhook",
            "release_notifications_mode",
            "release_notifications_resend_policy",
            "release_notifications_verbosity",
            "retag_digest_pins",
            "theme_preference",
        ]
    }
    assert event_metadata["before"] == {
        "theme_preference": "system",
        "onboarding_checklist": "visible",
        "compose_ignore_paths": "",
        "digest_pin_updates": "false",
        "retag_digest_pins": "false",
        "release_notes_enabled": "false",
        "release_notifications_delivery_mode": (
            settings_module.DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE
        ),
        "release_notifications_mode": "digest",
        "release_notifications_resend_policy": "remote_change",
        "release_notifications_cooldown_seconds": "86400",
        "release_notifications_discord_webhook": "",
        "release_notifications_verbosity": "summary",
    }
    assert event_metadata["after"] == {
        "theme_preference": "dark",
        "onboarding_checklist": "dismissed",
        "compose_ignore_paths": "old, archive/disabled",
        "digest_pin_updates": "true",
        "retag_digest_pins": "true",
        "release_notes_enabled": "true",
        "release_notifications_delivery_mode": (
            settings_module.DEFAULT_RELEASE_NOTIFICATIONS_DELIVERY_MODE
        ),
        "release_notifications_mode": "per_container",
        "release_notifications_resend_policy": "cooldown",
        "release_notifications_cooldown_seconds": "60",
        "release_notifications_discord_webhook": "configured",
        "release_notifications_verbosity": "full",
    }
    assert webhook not in run["metadata_json"]
    assert webhook not in event["metadata_json"]

    reset = client.post(
        "/api/v1/settings/managed",
        json={"values": {"onboarding_checklist": "visible"}},
        headers=headers,
    )
    reset_managed = {entry["key"]: entry for entry in reset.json()["managed"]}
    assert reset.status_code == 200
    assert reset_managed["onboarding_checklist"]["value"] == "visible"
    with open_db(db_path) as conn:
        onboarding_row = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'onboarding_checklist_dismissed_at'"
        ).fetchone()
    assert onboarding_row is None
