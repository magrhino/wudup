from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.web_test_helpers import (
    WUD_API_AUTH_CONFIG_KEY,
    WUD_API_AUTHORIZATION_HEADER,
    _client,
    _doctor_client,
    _insert_run,
    _install_wud_api,
    _store_web_setting,
    _wud_api_container,
)

from wudup import web_pending, web_settings, web_wud_api, web_wud_cache
from wudup.db import open_db
from wudup.web_models import (
    WudApiObservationCounts,
    WudApiObservationDiagnostic,
    WudApiObservationDiagnostics,
)


def _build_support_bundle(
    tmp_path: Path,
    *,
    wud_api_base_url: str = "https://wud.support-config.test:3000",
) -> tuple[dict[str, Any], set[str], str]:
    client = _doctor_client(
        tmp_path,
        {"WUD_API_BASE_URL": wud_api_base_url},
    )

    response = client.get("/api/v1/diagnostics/support-bundle")
    body = response.json()
    serialized = json.dumps(body)
    doctor_codes = {check["code"] for check in body["doctor_result"]["checks"]}

    assert response.status_code == 200
    return body, doctor_codes, serialized


def test_diagnostics_support_bundle_returns_semantically_redacted_payload(
    tmp_path: Path,
) -> None:
    secret = "github-token-secret"
    wud_api_header_secret = "wud-api-header-secret"
    wud_api_headers_file = tmp_path / "wud-api-headers.json"
    wud_api_headers_file.write_text(
        json.dumps({"X-Api-Key": wud_api_header_secret}),
        encoding="utf-8",
    )
    client = _doctor_client(
        tmp_path,
        {
            "GITHUB_TOKEN": secret,
            "FAKE_DOCKER_INFO_SECRET": secret,
            "WUD_API_HEADERS_FILE": str(wud_api_headers_file),
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    log_file = tmp_path / "state" / "logs" / "run.log"
    log_file.write_text(
        (
            f"checking {tmp_path / 'docker' / 'app' / 'compose.yml'}\n"
            f"wud file {wud_file}\n"
            f"log file {log_file}\n"
            f"secret {secret}\n"
            f"wud api header {wud_api_header_secret}\n"
        ),
        encoding="utf-8",
    )
    _insert_run(tmp_path, log_file="run.log")

    response = client.get("/api/v1/diagnostics/support-bundle")
    body = response.json()
    serialized = json.dumps(body)
    doctor_codes = {check["code"] for check in body["doctor_result"]["checks"]}

    assert response.status_code == 200
    assert str(tmp_path) not in serialized
    assert secret not in serialized
    assert wud_api_header_secret not in serialized
    assert "<redacted>" in serialized
    assert "<DOCKER_BASE>/app/compose.yml" in serialized
    assert "<WUD_OUT_FILE>" in serialized
    assert "<WUD_LOG_DIR>/run.log" in serialized
    assert "wud-out-file" in doctor_codes
    assert "compose-discovery" in doctor_codes
    assert "wud_api_diagnostics" in body
    assert body["pending_summary"]["source_file"] == "<WUD_OUT_FILE>"
    assert body["log_tail"]["exists"] is True


def test_diagnostics_support_bundle_redacts_stored_discord_webhook(
    tmp_path: Path,
) -> None:
    webhook = "https://discord.com/api/webhooks/123/token-secret"
    (tmp_path / "state").mkdir()
    _store_web_setting(
        tmp_path,
        "release_notifications.discord_webhook",
        webhook,
    )
    run_id = _insert_run(tmp_path)
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        with conn:
            conn.execute(
                """
                UPDATE update_runs
                SET metadata_json = ?
                WHERE id = ?
                """,
                (json.dumps({"webhook": webhook}), run_id),
            )
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/diagnostics/support-bundle")
    serialized = json.dumps(response.json())

    assert response.status_code == 200
    assert webhook not in serialized
    assert "token-secret" not in serialized
    assert "<redacted>" in serialized


def test_diagnostics_support_bundle_includes_sanitized_wud_api_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redaction_value = "registry-redaction-value"
    _install_wud_api(
        monkeypatch,
        watchers=(
            200,
            [
                {
                    "id": "docker.local",
                    "type": "docker",
                    "name": "local",
                    "configuration": {
                        "socket": "/var/run/docker.sock",
                        "headers": {WUD_API_AUTHORIZATION_HEADER: redaction_value},
                        "cron": "0 * * * *",
                        "watchbydefault": True,
                    },
                }
            ],
        ),
        registries=(
            200,
            [
                {
                    "id": "hub.private",
                    "type": "hub",
                    "name": "private",
                    "configuration": {
                        "region": "eu-west-1",
                        WUD_API_AUTH_CONFIG_KEY: redaction_value,
                    },
                }
            ],
        ),
    )

    body, _doctor_codes, serialized = _build_support_bundle(tmp_path)
    diagnostics = body["wud_api_diagnostics"]

    assert diagnostics["app"]["name"] == "wud"
    assert diagnostics["watchers"][0]["configuration"]["socket"] == "[REDACTED_PATH]"
    assert diagnostics["watchers"][0]["configuration"]["headers"] == "<redacted>"
    assert diagnostics["registries"][0]["configuration"]["region"] == "eu-west-1"
    assert (
        diagnostics["registries"][0]["configuration"][WUD_API_AUTH_CONFIG_KEY]
        == "<redacted>"
    )
    assert redaction_value not in serialized


def test_diagnostics_support_bundle_includes_wud_observation_issue_dump(
    tmp_path: Path,
    monkeypatch,
) -> None:
    available = [
        _wud_api_container(
            name=f"available-{index}",
            image=f"registry.example/acme/available-{index}",
        )
        for index in range(7)
    ]
    degraded = []
    for index in range(12):
        row = _wud_api_container(
            name=f"degraded-{index}",
            image=f"ghcr.io/acme/degraded-{index}",
            update_available=False,
            update_kind="unknown",
        )
        row["result"] = None
        row["error"] = {"message": "Request failed with status code 429"}
        row["labels"] = {"debug.raw": "raw-payload-marker"}
        row["rawOnly"] = "raw-payload-marker"
        degraded.append(row)
    unsupported = []
    for index in range(9):
        row = _wud_api_container(
            name=f"unsupported-{index}",
            image=f"registry-{index}.example/acme/unsupported",
            update_available=False,
            update_kind="unknown",
        )
        row["result"] = None
        row["error"] = {"message": "Unsupported Registry unknown"}
        unsupported.append(row)
    _install_wud_api(
        monkeypatch,
        containers=[*available, *degraded, *unsupported],
    )

    body, _doctor_codes, serialized = _build_support_bundle(
        tmp_path,
        wud_api_base_url="https://wud.support-observations.test:3000",
    )

    observations = body["wud_api_observations"]
    assert observations["counts"] == {
        "available": 7,
        "degraded": 12,
        "retained": 0,
        "recovered": 0,
        "unresolved": 12,
        "unsupported_ignored": 9,
    }
    assert len(observations["items"]) == 21
    assert [item["outcome"] for item in observations["items"]].count(
        "unresolved"
    ) == 12
    assert [item["outcome"] for item in observations["items"]].count(
        "unsupported_ignored"
    ) == 9
    assert observations["items"][0] == {
        "outcome": "unresolved",
        "reason_code": "reported_error",
        "container_id": "docker.local.degraded-0",
        "name": "degraded-0",
        "image": "ghcr.io/acme/degraded-0:1.0",
        "registry": "ghcr.io",
        "watcher": "local",
        "update_available": False,
        "usable_result": False,
        "retryable": True,
        "error": (
            "The registry rate-limited the last WUD update check "
            "(HTTP 429: too many requests). Wait before rescanning; "
            "a successful check clears this error."
        ),
    }
    assert body["pending_summary"]["wud_api"]["detail"] == (
        "7 updates are available. "
        "The last WUD update check failed for 12 containers. "
        "Update status is unknown for 12 containers. "
        "WUD skipped 9 containers because their registries are unsupported."
    )
    assert "raw-payload-marker" not in serialized
    assert "rawOnly" not in serialized
    assert "labels" not in observations["items"][0]


def test_diagnostics_support_bundle_redacts_wud_observation_details(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "wud-observation-secret"
    headers_file = tmp_path / "wud-api-headers.json"
    headers_file.write_text(json.dumps({"X-Api-Key": secret}), encoding="utf-8")
    degraded = _wud_api_container(
        name="degraded",
        image="private.example/acme/degraded",
        update_available=False,
        update_kind="unknown",
    )
    degraded["id"] = f"docker.local.{secret}"
    degraded["result"] = None
    degraded["error"] = {
        "message": (
            "Authorization: Bearer wud-owned-bearer-secret; "
            "Proxy-Authorization: Basic d3VkOnNlY3JldA==; "
            f"Request failed with status code 401 for {secret} at "
            f"{tmp_path / 'private' / 'registry'} via "
            "https://alice:s3cr3t@registry.example/v2?token=opaque-secret"
        )
    }
    degraded["labels"] = {"debug.raw": "observation-label-marker"}
    custom_credentials = _wud_api_container(
        name="custom-credentials",
        image="private.example/acme/custom-credentials",
        update_available=False,
        update_kind="unknown",
    )
    custom_credentials["result"] = None
    custom_credentials["error"] = {
        "message": (
            "registry failure api_key=unknown-api-key "
            "token: unknown-token password=unknown-password "
            "X-Custom-Auth: unknown-header-secret"
        )
    }
    _install_wud_api(monkeypatch, containers=[degraded, custom_credentials])
    client = _doctor_client(
        tmp_path,
        {
            "WUD_API_BASE_URL": "https://wud.support-redaction.test:3000",
            "WUD_API_HEADERS_FILE": str(headers_file),
        },
    )

    response = client.get("/api/v1/diagnostics/support-bundle")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body["wud_api_observations"])
    item = body["wud_api_observations"]["items"][0]
    assert item["reason_code"] == "reported_error"
    assert secret not in serialized
    assert str(tmp_path) not in serialized
    assert "observation-label-marker" not in serialized
    assert "alice" not in serialized
    assert "s3cr3t" not in serialized
    assert "opaque-secret" not in serialized
    assert "wud-owned-bearer-secret" not in serialized
    assert "d3VkOnNlY3JldA==" not in serialized
    assert "unknown-api-key" not in serialized
    assert "unknown-token" not in serialized
    assert "unknown-password" not in serialized
    assert "unknown-header-secret" not in serialized
    assert item["error"] == (
        "The last WUD update check failed: "
        "registry request returned HTTP status 401. Check WUD logs for details."
    )
    assert body["wud_api_observations"]["items"][1]["error"] == (
        "The last WUD update check failed. Check WUD logs for details."
    )
    assert "<redacted>" in serialized


def test_diagnostics_support_bundle_preserves_observation_literals_during_redaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    headers_file = tmp_path / "wud-api-headers.json"
    headers_file.write_text(
        json.dumps({"X-Outcome": "retained", "X-Reason": "error"}),
        encoding="utf-8",
    )
    _install_wud_api(monkeypatch)
    observations = WudApiObservationDiagnostics(
        counts=WudApiObservationCounts(
            available=1,
            degraded=1,
            retained=1,
        ),
        items=[
            WudApiObservationDiagnostic(
                outcome="retained",
                reason_code="reported_error",
                container_id="docker.local.retained",
                error="retained registry error",
            )
        ],
    )

    def observation_diagnostics(_settings, *, snapshot=None):
        assert snapshot is not None
        return observations

    monkeypatch.setattr(
        web_wud_api,
        "get_observation_diagnostics",
        observation_diagnostics,
    )
    client = _doctor_client(
        tmp_path,
        {
            "WUD_API_BASE_URL": "https://wud.literal-redaction.test:3000",
            "WUD_API_HEADERS_FILE": str(headers_file),
        },
    )

    response = client.get("/api/v1/diagnostics/support-bundle")

    assert response.status_code == 200
    item = response.json()["wud_api_observations"]["items"][0]
    assert item["outcome"] == "retained"
    assert item["reason_code"] == "reported_error"
    assert item["container_id"] == "docker.local.<redacted>"
    assert item["error"] == "<redacted> registry <redacted>"


def test_diagnostics_support_bundle_uses_one_wud_snapshot_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 0.0
    containers = [
        _wud_api_container(
            name="first-generation",
            image="registry.example/first-generation",
        )
    ]
    second_generation = [
        _wud_api_container(name="second-a", image="registry.example/second-a"),
        _wud_api_container(name="second-b", image="registry.example/second-b"),
    ]
    _install_wud_api(monkeypatch, containers=containers)
    monkeypatch.setattr(web_wud_cache.time, "monotonic", lambda: now)
    captured_snapshot = None
    original_pending_response = web_pending.pending_response_with_snapshot

    def expire_after_pending(*args, **kwargs):
        nonlocal captured_snapshot, now
        response, captured_snapshot = original_pending_response(*args, **kwargs)
        assert captured_snapshot is not None
        containers[:] = second_generation
        now = web_wud_api.WUD_API_CACHE_TTL_SECONDS + 0.1
        return response, captured_snapshot

    monkeypatch.setattr(
        web_pending,
        "pending_response_with_snapshot",
        expire_after_pending,
    )
    original_observation_diagnostics = web_wud_api.get_observation_diagnostics

    def tracked_observation_diagnostics(settings, *, snapshot=None):
        assert snapshot is captured_snapshot
        return original_observation_diagnostics(settings, snapshot=snapshot)

    monkeypatch.setattr(
        web_wud_api,
        "get_observation_diagnostics",
        tracked_observation_diagnostics,
    )
    client = _doctor_client(
        tmp_path,
        {
            "WUD_API_BASE_URL": "https://wud.single-generation.test:3000",
            "WUD_PENDING_SOURCE": "api",
        },
    )

    response = client.get("/api/v1/diagnostics/support-bundle")

    assert response.status_code == 200
    body = response.json()
    assert body["pending_summary"]["count"] == 1
    assert body["pending_summary"]["items"][0]["image"].endswith(
        "first-generation:1.0"
    )
    assert body["wud_api_observations"]["counts"]["available"] == 1


@pytest.mark.parametrize(
    ("health", "expected_state", "expected_available"),
    [
        ((401, {"error": "authentication required"}), "auth_required", True),
        (OSError("connection refused"), "unavailable", False),
    ],
)
def test_diagnostics_support_bundle_includes_degraded_wud_api_diagnostics(
    tmp_path: Path,
    monkeypatch,
    health,
    expected_state: str,
    expected_available: bool,
) -> None:
    _install_wud_api(monkeypatch, health=health)

    body, _doctor_codes, serialized = _build_support_bundle(
        tmp_path,
        wud_api_base_url=f"https://wud.support-{expected_state}.test:3000",
    )
    diagnostics = body["wud_api_diagnostics"]

    assert diagnostics["health"]["state"] == expected_state
    assert diagnostics["health"]["available"] is expected_available
    assert diagnostics["app"]["status"]["state"] == expected_state
    assert diagnostics["registries_status"]["state"] == expected_state
    assert isinstance(serialized, str)
    assert "wud_api_diagnostics" in serialized


def test_diagnostics_support_bundle_reuses_resolved_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    observed: list[bool] = []
    original_settings_response = web_settings.settings_response

    def wrapped_settings_response(settings, request):
        observed.append(settings is client.app.state.web_settings)
        return original_settings_response(settings, request)

    monkeypatch.setattr(web_settings, "settings_response", wrapped_settings_response)

    response = client.get("/api/v1/diagnostics/support-bundle")

    assert response.status_code == 200
    assert observed == [True]


def test_diagnostics_support_bundle_warns_for_log_file_outside_configured_dir(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    _insert_run(tmp_path, log_file=str(outside))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/diagnostics/support-bundle")
    body = response.json()

    assert response.status_code == 200
    assert body["log_tail"] is None
    assert body["diagnostics_warnings"] == [
        "log tail unavailable: log file is outside WUD_LOG_DIR"
    ]

def test_diagnostics_support_bundle_reports_last_run_metadata_errors(
    tmp_path: Path,
) -> None:
    run_id = _insert_run(tmp_path, log_file="run.log")
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE update_runs
                SET metadata_json = ?
                WHERE id = ?
                """,
                ("not-json", run_id),
            )
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/diagnostics/support-bundle")

    assert response.status_code == 200
    body = response.json()
    assert body["last_run_status"] is None
    assert body["log_tail"] is None
    assert body["diagnostics_warnings"] == [
        "last run status unavailable: invalid metadata JSON in database"
    ]
