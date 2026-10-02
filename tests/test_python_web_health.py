from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from tests.web_test_helpers import (
    WUD_API_AUTH_CONFIG_KEY,
    _client,
    _csrf_headers,
    _doctor_client,
    _install_wud_api,
    _wud_api_container,
)

from wudup import web as web_module
from wudup.doctor import DoctorConfigError


def test_healthz_is_unauthenticated_before_setup(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/healthz")

    assert response.status_code == 200

def test_healthz_response_shape_is_minimal(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/healthz")
    body = response.json()

    assert response.status_code == 200
    assert body == {"ok": True, "version": web_module.__version__}
    assert set(body) == {"ok", "version"}
    sensitive_keys = {
        "wud_file",
        "db_path",
        "pending_count",
        "auth_required",
        "dev_auth_bypass",
        "setup_required",
        "mutations_enabled",
        "public_origin",
        "trusted_proxies",
    }
    assert sensitive_keys.isdisjoint(body)

def test_readyz_is_unauthenticated_and_reports_local_readiness(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path, client=("127.0.0.1", 50000))

    response = client.get("/readyz")
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}
    codes = set(checks)

    assert response.status_code == 200
    assert body["ok"] is True
    assert set(body) == {"ok", "version", "checks"}
    assert {
        "docker-endpoint",
        "docker-daemon-version",
        "docker-daemon-info",
        "docker-container-listing",
        "wud-out-file-directory",
        "wud-out-file",
        "webui-database",
        "wud-api",
    }.issubset(codes)
    assert checks["wud-api"]["status"] == "WARN"
    sensitive_keys = {
        "wud_file",
        "db_path",
        "pending_count",
        "auth_required",
        "dev_auth_bypass",
        "setup_required",
        "mutations_enabled",
        "public_origin",
        "trusted_proxies",
    }
    assert sensitive_keys.isdisjoint(body)

def test_readyz_rejects_non_loopback_client(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path, client=("203.0.113.10", 50000))

    response = client.get("/readyz")

    assert response.status_code == 404
    assert response.content == b""

def test_readyz_ignores_forwarded_loopback_from_trusted_proxy(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {"WUD_WEB_TRUSTED_PROXIES": "203.0.113.10/32"},
        client=("203.0.113.10", 50000),
    )

    response = client.get("/readyz", headers={"X-Forwarded-For": "127.0.0.1"})

    assert response.status_code == 404
    assert response.content == b""

def test_readyz_rejects_loopback_requests_forwarded_by_a_proxy(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path, client=("127.0.0.1", 50000))

    for headers in (
        {"X-Forwarded-For": "203.0.113.9"},
        {"X-Forwarded-Proto": "https"},
        {"X-Real-IP": "203.0.113.9"},
        {"Forwarded": "for=203.0.113.9"},
    ):
        response = client.get("/readyz", headers=headers)

        assert response.status_code == 404, headers
        assert response.content == b""


def test_ready_api_requires_auth_and_returns_local_readiness(
    tmp_path: Path,
) -> None:
    unauthenticated = _client(tmp_path)
    client = _doctor_client(tmp_path, client=("203.0.113.10", 50000))

    auth_response = unauthenticated.get("/api/v1/ready")
    ready_response = client.get("/api/v1/ready")

    assert auth_response.status_code == 403
    assert auth_response.json()["detail"] == "setup required"
    assert ready_response.status_code == 200
    body = ready_response.json()
    codes = {check["code"] for check in body["checks"]}
    assert body["ok"] is True
    assert "docker-daemon-info" in codes
    assert "webui-database" in codes
    assert "wud-api" in codes

def test_readyz_fails_when_required_local_check_fails(
    tmp_path: Path,
) -> None:
    secret = "docker-info-secret"
    client = _doctor_client(
        tmp_path,
        {
            "GITHUB_TOKEN": secret,
            "FAKE_DOCKER_INFO_SECRET": secret,
        },
        client=("127.0.0.1", 50000),
    )

    response = client.get("/readyz")
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}

    assert response.status_code == 503
    assert body["ok"] is False
    assert checks["docker-daemon-info"]["status"] == "FAIL"
    assert checks["docker-daemon-info"]["detail"] == "exit 17: info failed: <redacted>"
    assert secret not in json.dumps(body)

def test_readyz_fails_when_required_checks_are_missing(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path, client=("127.0.0.1", 50000))

    with mock.patch(
        "wudup.web_health.doctor_options_from_namespace",
        side_effect=DoctorConfigError("invalid doctor configuration"),
    ):
        response = client.get("/readyz")
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}

    assert response.status_code == 503
    assert body["ok"] is False
    assert checks["configuration"]["status"] == "FAIL"
    assert checks["readiness-missing-checks"]["status"] == "FAIL"
    assert "docker socket or endpoint" in checks["readiness-missing-checks"]["detail"]

def test_doctor_endpoint_enforces_auth_csrf_and_post(
    tmp_path: Path,
) -> None:
    unauthenticated = _client(tmp_path)
    doctor = _doctor_client(tmp_path)

    auth_response = unauthenticated.post(
        "/api/v1/doctor",
        headers=_csrf_headers(unauthenticated),
    )
    missing_csrf = doctor.post("/api/v1/doctor")
    get_response = doctor.get("/api/v1/doctor")

    assert auth_response.status_code == 403
    assert auth_response.json()["detail"] == "setup required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert get_response.status_code == 405
    assert get_response.headers["allow"] == "POST"

def test_doctor_endpoint_returns_structured_redacted_results(
    tmp_path: Path,
) -> None:
    secret = "github-token-secret"
    client = _doctor_client(
        tmp_path,
        {
            "GITHUB_TOKEN": secret,
            "FAKE_DOCKER_INFO_SECRET": secret,
        },
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert body["ok"] is False
    assert body["failures"] >= 1
    assert body["warnings"] >= 1
    assert checks["docker-daemon-info"]["status"] == "FAIL"
    assert checks["docker-daemon-info"]["detail"] == "exit 17: info failed: <redacted>"
    assert checks["docker-daemon-info"]["suggestions"]
    assert checks["webui-database"]["status"] == "PASS"
    assert checks["webui-authentication"]["status"] == "WARN"
    assert checks["wud-api"]["status"] == "WARN"
    assert secret not in serialized
    assert "<redacted>" in serialized


def test_doctor_endpoint_suggests_wud_api_auth_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=(401, {"error": "authentication required"}))
    client = _doctor_client(
        tmp_path,
        {"WUD_API_BASE_URL": "https://wud.doctor-auth-missing.test:3000"},
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    checks = {check["code"]: check for check in response.json()["checks"]}
    suggestions = checks["wud-api"]["suggestions"]

    assert response.status_code == 200
    assert checks["wud-api"]["status"] == "WARN"
    assert suggestions[0]["label"] == "Configure WUD API credentials"
    assert "WUD_API_AUTH_BEARER_TOKEN_FILE" in suggestions[0]["description"]
    assert "WUD_API_AUTH_BASIC_PASSWORD_FILE" in suggestions[0]["description"]
    assert "WUD_API_HEADERS_FILE" in suggestions[0]["description"]
    assert "authentication support is added" not in suggestions[0]["description"]


def test_doctor_warns_when_wud_metadata_contains_degraded_observations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = _wud_api_container(
        name="bazarr",
        image="ghcr.io/linuxserver/bazarr",
        update_available=False,
    )
    degraded["result"] = None
    degraded["updateKind"] = {
        "kind": "unknown",
        "localValue": None,
        "remoteValue": None,
    }
    degraded["error"] = {"message": "Request failed with status code 429"}
    _install_wud_api(monkeypatch, containers=[degraded])
    client = _doctor_client(
        tmp_path,
        {"WUD_API_BASE_URL": "https://wud.doctor-degraded.test:3000"},
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    check = {
        item["code"]: item for item in response.json()["checks"]
    }["wud-api"]

    assert response.status_code == 200
    assert check["status"] == "WARN"
    assert "The last WUD update check failed for 1 container" in check["detail"]


def test_doctor_endpoint_suggests_verifying_rejected_wud_api_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=(401, {"error": "authentication required"}))
    client = _doctor_client(
        tmp_path,
        {
            "WUD_API_BASE_URL": "https://wud.doctor-auth-rejected.test:3000",
            "WUD_API_AUTH_BEARER_TOKEN": "rejected-wud-api-token",
        },
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}
    suggestions = checks["wud-api"]["suggestions"]
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert checks["wud-api"]["detail"] == "configured WUD API credentials were rejected"
    assert suggestions[0]["label"] == "Verify WUD API credentials"
    assert "were rejected" in suggestions[0]["description"]
    assert "WUD_API_HEADERS_FILE" in suggestions[0]["description"]
    assert "rejected-wud-api-token" not in serialized


def test_doctor_endpoint_reports_wud_api_configuration_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redaction_value = "registry-redaction-value"
    _install_wud_api(
        monkeypatch,
        registries=(
            200,
            [
                {
                    "id": "hub.private",
                    "type": "hub",
                    "name": "private",
                    "configuration": {WUD_API_AUTH_CONFIG_KEY: redaction_value},
                }
            ],
        ),
    )
    client = _doctor_client(
        tmp_path,
        {"WUD_API_BASE_URL": "https://wud.doctor-config.test:3000"},
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    body = response.json()
    checks = {check["code"]: check for check in body["checks"]}
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert checks["wud-api-app"]["status"] == "PASS"
    assert checks["wud-api-app"]["detail"] == "wud 5.0.0"
    assert checks["wud-api-log"]["detail"] == "log level debug"
    assert checks["wud-api-store"]["detail"] == "path .store, file wud.json"
    assert checks["wud-api-watchers"]["category"] == "wud-api"
    assert "docker.local" in checks["wud-api-watchers"]["detail"]
    assert "watch-by-default true" in checks["wud-api-watchers"]["detail"]
    assert "hub.private" in checks["wud-api-registries"]["detail"]
    assert redaction_value not in serialized


def test_doctor_endpoint_warns_for_wud_api_configuration_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        app=(200, []),
        log=(500, {}),
        watchers=(200, []),
        registries=(401, {}),
    )
    client = _doctor_client(
        tmp_path,
        {"WUD_API_BASE_URL": "https://wud.doctor-config-warn.test:3000"},
    )

    response = client.post("/api/v1/doctor", headers=_csrf_headers(client))
    checks = {check["code"]: check for check in response.json()["checks"]}

    assert response.status_code == 200
    assert checks["wud-api-app"]["status"] == "WARN"
    assert checks["wud-api-log"]["status"] == "WARN"
    assert checks["wud-api-registries"]["status"] == "WARN"
