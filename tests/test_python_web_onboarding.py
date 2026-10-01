from __future__ import annotations

import json
from pathlib import Path

from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _doctor_client,
    _setup_admin,
)

from wudup import web_onboarding
from wudup.db import open_db


def _csrf_headers_for_origin(
    client,
    *,
    host: str,
    origin: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    base_headers = {"Host": host, **(extra_headers or {})}
    response = client.get("/api/v1/auth/csrf", headers=base_headers)
    assert response.status_code == 200
    return {
        **base_headers,
        "Origin": origin,
        "x-wud-csrf-token": response.json()["csrf_token"],
    }


def test_onboarding_endpoints_enforce_auth_csrf_and_post(
    tmp_path: Path,
) -> None:
    unauthenticated = _client(tmp_path)
    post_setup = _client(tmp_path)
    client = _doctor_client(tmp_path)

    auth_response = unauthenticated.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(unauthenticated),
    )
    _setup_admin(post_setup)
    post_setup.cookies.clear()
    post_setup_auth_response = post_setup.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(post_setup),
    )
    missing_csrf = client.post("/api/v1/onboarding/checklist")
    dismiss_missing_csrf = client.post("/api/v1/onboarding/dismiss")
    get_response = client.get("/api/v1/onboarding/checklist")
    dismiss_get_response = client.get("/api/v1/onboarding/dismiss")

    assert auth_response.status_code == 403
    assert auth_response.json()["detail"] == "setup required"
    assert post_setup_auth_response.status_code == 401
    assert post_setup_auth_response.json()["detail"] == "authentication required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert dismiss_missing_csrf.status_code == 403
    assert dismiss_missing_csrf.json()["detail"] == "origin header is required"
    assert get_response.status_code == 405
    assert get_response.headers["allow"] == "POST"
    assert dismiss_get_response.status_code == 405
    assert dismiss_get_response.headers["allow"] == "POST"

def test_onboarding_checklist_returns_redacted_setup_items(
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

    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )
    body = response.json()
    items = {item["key"]: item for item in body["items"]}
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert body["dismissed"] is False
    assert body["visible"] is True
    assert body["all_passed"] is False
    assert {
        "admin-setup",
        "wud-output",
        "docker-access",
        "compose-discovery",
        "persistence",
        "browser-access",
        "mutation-mode",
    }.issubset(items)
    assert items["docker-access"]["status"] == "FAIL"
    assert items["docker-access"]["suggestions"]
    assert "docker-daemon-info" in items["docker-access"]["check_codes"]
    assert items["mutation-mode"]["status"] == "PASS"
    assert secret not in serialized
    assert "<redacted>" in serialized

def test_onboarding_checklist_searches_old_directory_by_default(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path)
    docker_base = client.app.state.web_settings.config.docker_base
    ignored_stack = docker_base / "old" / "ignored"
    ignored_stack.mkdir(parents=True)
    (ignored_stack / "docker-compose.yml").write_text(
        "services:\n  ignored:\n    image: repo/ignored:latest\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )
    body = response.json()
    compose_item = next(
        item for item in body["items"] if item["key"] == "compose-discovery"
    )

    assert response.status_code == 200
    assert compose_item["status"] == "PASS"
    check_codes = compose_item.get("check_codes") or []
    assert any("old-ignored" in code for code in check_codes)

def test_onboarding_dismissal_persists_in_sqlite(
    tmp_path: Path,
) -> None:
    client = _doctor_client(tmp_path)

    before = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )
    dismiss = client.post(
        "/api/v1/onboarding/dismiss",
        headers=_csrf_headers(client),
    )
    after = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )

    assert before.status_code == 200
    assert before.json()["visible"] is True
    assert dismiss.status_code == 200
    assert dismiss.json()["dismissed"] is True
    assert dismiss.json()["dismissed_at"]
    assert after.status_code == 200
    assert after.json()["dismissed"] is True
    assert after.json()["visible"] is False
    assert after.json()["dismissed_at"] == dismiss.json()["dismissed_at"]

def test_onboarding_checklist_skips_doctor_after_dismissal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _doctor_client(tmp_path)
    dismiss = client.post(
        "/api/v1/onboarding/dismiss",
        headers=_csrf_headers(client),
    )

    def fail_doctor(*_args, **_kwargs):
        raise AssertionError("dismissed onboarding should not run doctor")

    monkeypatch.setattr(web_onboarding, "web_doctor_result", fail_doctor)
    after = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )

    assert dismiss.status_code == 200
    assert after.status_code == 200
    assert after.json()["dismissed"] is True
    assert after.json()["visible"] is False
    assert after.json()["items"] == []

def test_onboarding_checklist_hides_when_required_items_pass(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "false",
            "WUD_WEB_PUBLIC_ORIGIN": "http://testserver",
        },
    )
    _setup_admin(client)

    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )
    body = response.json()
    required = {
        item["key"]: item["status"]
        for item in body["items"]
        if item["key"] != "mutation-mode"
    }

    assert response.status_code == 200
    assert body["dismissed"] is False
    assert body["all_passed"] is True
    assert body["visible"] is False
    assert set(required.values()) == {"PASS"}


def test_onboarding_suggests_observed_lan_public_origin(
    tmp_path: Path,
) -> None:
    observed_host = "192.0.2.44"
    client = _doctor_client(
        tmp_path,
        {"WUD_WEB_ALLOWED_HOSTS": observed_host},
    )
    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers_for_origin(
            client,
            host=observed_host,
            origin=f"http://{observed_host}",
        ),
    )
    body = response.json()
    browser_item = next(item for item in body["items"] if item["key"] == "browser-access")
    snippets = {suggestion["snippet"] for suggestion in browser_item["suggestions"]}

    assert response.status_code == 200
    assert browser_item["status"] == "WARN"
    assert f"WUD_WEB_PUBLIC_ORIGIN=http://{observed_host}" in snippets


def test_onboarding_does_not_suggest_loopback_public_origin(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {"WUD_WEB_ALLOWED_HOSTS": "127.0.0.1"},
        client=("127.0.0.1", 50000),
    )
    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers_for_origin(
            client,
            host="127.0.0.1",
            origin="http://127.0.0.1",
        ),
    )
    body = response.json()
    browser_item = next(item for item in body["items"] if item["key"] == "browser-access")
    snippets = {suggestion["snippet"] for suggestion in browser_item["suggestions"]}

    assert response.status_code == 200
    assert browser_item["status"] == "PASS"
    assert all(
        not snippet.startswith("WUD_WEB_PUBLIC_ORIGIN=http://127.0.0.1")
        for snippet in snippets
    )


def test_onboarding_ignores_untrusted_forwarded_origin_snippet(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {
            "WUD_WEB_ALLOWED_HOSTS": "internal.test,wud.example.test",
            "WUD_WEB_TRUSTED_PROXIES": "10.0.0.1/32",
        },
        client=("192.0.2.1", 50000),
    )
    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers_for_origin(
            client,
            host="internal.test",
            origin="http://internal.test",
            extra_headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "wud.example.test",
            },
        ),
    )
    body = response.json()
    browser_item = next(item for item in body["items"] if item["key"] == "browser-access")
    observed_suggestion = next(
        suggestion
        for suggestion in browser_item["suggestions"]
        if suggestion["label"] == "Set observed public origin"
    )

    assert response.status_code == 200
    assert observed_suggestion["snippet"] == "WUD_WEB_PUBLIC_ORIGIN=http://internal.test"


def test_onboarding_uses_trusted_forwarded_origin_snippet(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {
            "WUD_WEB_ALLOWED_HOSTS": "internal.test",
            "WUD_WEB_TRUSTED_PROXIES": "10.0.0.1/32",
            "WUD_WEB_SECURE_COOKIES": "false",
        },
        client=("10.0.0.1", 50000),
    )
    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers_for_origin(
            client,
            host="internal.test",
            origin="https://wud.example.test",
            extra_headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "wud.example.test",
            },
        ),
    )
    body = response.json()
    browser_item = next(item for item in body["items"] if item["key"] == "browser-access")
    snippets = {suggestion["snippet"] for suggestion in browser_item["suggestions"]}

    assert response.status_code == 200
    assert "WUD_WEB_PUBLIC_ORIGIN=https://wud.example.test" in snippets


def test_onboarding_checklist_stays_visible_when_mutations_enabled(
    tmp_path: Path,
) -> None:
    client = _doctor_client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "false",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_PUBLIC_ORIGIN": "http://testserver",
        },
    )
    _setup_admin(client)

    response = client.post(
        "/api/v1/onboarding/checklist",
        headers=_csrf_headers(client),
    )
    body = response.json()
    items = {item["key"]: item for item in body["items"]}

    assert response.status_code == 200
    assert items["mutation-mode"]["status"] == "WARN"
    assert body["all_passed"] is False
    assert body["visible"] is True

def test_core_update_tour_endpoint_enforces_auth_csrf_and_post(
    tmp_path: Path,
) -> None:
    unauthenticated_root = tmp_path / "unauthenticated"
    post_setup_root = tmp_path / "post-setup"
    read_only_root = tmp_path / "read-only"
    unauthenticated_root.mkdir()
    post_setup_root.mkdir()
    read_only_root.mkdir()
    unauthenticated = _client(unauthenticated_root)
    post_setup = _client(post_setup_root)
    read_only = _client(
        read_only_root,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "false",
        },
    )

    auth_response = unauthenticated.get("/api/v1/onboarding/core-update-tour")
    _setup_admin(post_setup)
    post_setup.cookies.clear()
    post_setup_auth_response = post_setup.get("/api/v1/onboarding/core-update-tour")
    missing_csrf = read_only.post(
        "/api/v1/onboarding/core-update-tour",
        json={"status": "in_progress", "step": "dashboard"},
    )
    origin_without_csrf = read_only.post(
        "/api/v1/onboarding/core-update-tour",
        json={"status": "in_progress", "step": "dashboard"},
        headers={"Origin": "http://testserver"},
    )
    csrf_headers = _csrf_headers(read_only)
    bad_origin = read_only.post(
        "/api/v1/onboarding/core-update-tour",
        json={"status": "in_progress", "step": "dashboard"},
        headers={**csrf_headers, "Origin": "http://evil.example"},
    )
    bad_host = read_only.post(
        "/api/v1/onboarding/core-update-tour",
        json={"status": "in_progress", "step": "dashboard"},
        headers={**csrf_headers, "Host": "evil.test"},
    )
    get_response = read_only.get("/api/v1/onboarding/core-update-tour")

    assert auth_response.status_code == 403
    assert auth_response.json()["detail"] == "setup required"
    assert post_setup_auth_response.status_code == 401
    assert post_setup_auth_response.json()["detail"] == "authentication required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert origin_without_csrf.status_code == 403
    assert origin_without_csrf.json()["detail"] == "csrf token is required"
    assert bad_origin.status_code == 403
    assert bad_origin.json()["detail"] == "origin is not allowed"
    assert bad_host.status_code == 400
    assert bad_host.json()["detail"] == "host is not allowed"
    assert get_response.status_code == 200
    assert get_response.json() == {
        "status": "not_started",
        "step": "dashboard",
        "updated_at": "",
    }
    assert not (read_only_root / "state" / "wud.sqlite").exists()

def test_core_update_tour_persists_in_read_only_mode(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "false",
        },
    )
    db_path = tmp_path / "state" / "wud.sqlite"

    before = client.get("/api/v1/onboarding/core-update-tour")
    update = client.post(
        "/api/v1/onboarding/core-update-tour",
        json={"status": "in_progress", "step": "pending_preflight"},
        headers=_csrf_headers(client),
    )
    after = client.get("/api/v1/onboarding/core-update-tour")

    assert before.status_code == 200
    assert before.json()["status"] == "not_started"
    assert update.status_code == 200
    assert update.json()["status"] == "in_progress"
    assert update.json()["step"] == "pending_preflight"
    assert update.json()["updated_at"]
    assert after.json() == update.json()
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM web_settings WHERE key = 'onboarding_core_update_tour'"
        ).fetchone()
    assert row is not None, "expected onboarding_core_update_tour row in web_settings"
    stored = json.loads(row["value"])
    assert stored == {"status": "in_progress", "step": "pending_preflight"}
