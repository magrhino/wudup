from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.web_test_helpers import (
    _assert_generic_auth_failed,
    _csrf_headers,
    _setup_admin,
    _web_env,
)

from wudup import web_auth as web_auth_module
from wudup.web import create_app

WEB_TOKEN = "t" * web_auth_module.WEB_TOKEN_RECOMMENDED_MIN_LENGTH


def _token_app(tmp_path: Path, monkeypatch):
    now = {"value": 1_000.0}
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: now["value"])
    app = create_app(environ=_web_env(tmp_path, {"WUD_WEB_TOKEN": WEB_TOKEN}))
    _setup_admin(TestClient(app))
    return app, now


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_wrong_bearer_tokens_lock_out_bearer_auth_from_that_address(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, now = _token_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("198.51.100.20", 50000))
    other_client = TestClient(app, client=("198.51.100.21", 50000))

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client.get("/api/v1/status", headers=_bearer(f"guess-{index}"))
        )
    locked = client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))
    other_address = other_client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))
    now["value"] += web_auth_module.LOGIN_THROTTLE_COOLDOWN_SECONDS
    after_cooldown = client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))

    _assert_generic_auth_failed(locked)
    assert other_address.status_code == 200
    assert after_cooldown.status_code == 200


def test_correct_bearer_token_does_not_reset_failed_attempts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, _now = _token_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("198.51.100.20", 50000))

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES - 1):
        _assert_generic_auth_failed(
            client.get("/api/v1/status", headers=_bearer(f"guess-{index}"))
        )
    accepted = client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))
    _assert_generic_auth_failed(
        client.get("/api/v1/status", headers=_bearer("guess-last"))
    )
    locked = client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))

    assert accepted.status_code == 200
    _assert_generic_auth_failed(locked)


def test_bearer_lockout_applies_to_auth_status_endpoints(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, _now = _token_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("198.51.100.20", 50000))

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        assert (
            client.get(
                "/api/v1/auth/session", headers=_bearer(f"guess-{index}")
            ).json()["authenticated"]
            is False
        )
    session = client.get("/api/v1/auth/session", headers=_bearer(WEB_TOKEN))
    setup_status = client.get("/api/v1/setup/status", headers=_bearer(WEB_TOKEN))

    assert session.json()["authenticated"] is False
    assert setup_status.json()["authenticated"] is False


def test_bearer_lockout_does_not_block_password_login(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, _now = _token_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("198.51.100.20", 50000))

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client.get("/api/v1/status", headers=_bearer(f"guess-{index}"))
        )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(client),
    )

    assert login.status_code == 200
    assert not app.state.web_login_client_throttle


def test_requests_without_bearer_header_are_not_throttled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app, _now = _token_app(tmp_path, monkeypatch)
    client = TestClient(app, client=("198.51.100.20", 50000))

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES + 1):
        _assert_generic_auth_failed(client.get("/api/v1/status"))
    accepted = client.get("/api/v1/status", headers=_bearer(WEB_TOKEN))

    assert accepted.status_code == 200
    assert not app.state.web_bearer_throttle
