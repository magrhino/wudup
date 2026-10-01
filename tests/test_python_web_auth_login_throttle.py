from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from tests.web_test_helpers import (
    _assert_generic_auth_failed,
    _client,
    _contains_key,
    _csrf_headers,
    _setup_admin,
    _web_env,
)

from wudup import web_auth as web_auth_module
from wudup.web import create_app


def test_login_validation_redacts_submitted_inputs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    submitted_password = "x" * 1025

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": submitted_password},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert submitted_password not in response.text
    assert not _contains_key(response.json(), "input")


def test_login_sets_http_only_session_cookie(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)

    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(client),
    )
    status_response = client.get("/api/v1/status")

    assert login_response.status_code == 200
    assert login_response.json()["authenticated"] is True
    assert "wud_session=" in login_response.headers["set-cookie"]
    assert "HttpOnly" in login_response.headers["set-cookie"]
    assert status_response.status_code == 200


def test_login_rejects_wrong_password(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 401
    assert client.cookies.get("wud_session") is None


def test_login_throttle_locks_after_repeated_failures_and_expires(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: now)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client = TestClient(app)
    headers = _csrf_headers(client)

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers=headers,
        )
        _assert_generic_auth_failed(response)

    locked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )
    now += web_auth_module.LOGIN_THROTTLE_COOLDOWN_SECONDS + 0.1
    unlocked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    _assert_generic_auth_failed(locked_response)
    assert client.cookies.get("wud_session") is not None
    assert unlocked_response.status_code == 200
    assert unlocked_response.json()["authenticated"] is True


def test_login_throttle_entry_cap_does_not_evict_locked_user(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: now)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client = TestClient(app, client=("203.0.113.10", 50000))
    headers = _csrf_headers(client)

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=headers,
            )
        )

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_ENTRIES + 1):
        _assert_generic_auth_failed(
            client.post(
                "/api/v1/auth/login",
                json={"username": f"filler-{index}", "password": "wrong"},
                headers=headers,
            )
        )

    locked_after_fill = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )
    now += web_auth_module.LOGIN_THROTTLE_COOLDOWN_SECONDS + 0.1
    unlocked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    _assert_generic_auth_failed(locked_after_fill)
    assert unlocked_response.status_code == 200
    assert unlocked_response.json()["authenticated"] is True


def test_login_throttle_entry_cap_evicts_unlocked_entries_without_global_lockout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: now)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    request = SimpleNamespace(
        app=app,
        client=SimpleNamespace(host="203.0.113.10"),
        headers={},
    )
    settings = app.state.web_settings

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_ENTRIES):
        request.client.host = f"client-{index}.test"
        web_auth_module._record_login_failure(request, settings, f"filler-{index}")
    request.client.host = "client-overflow.test"
    web_auth_module._record_login_failure(request, settings, "overflow")

    throttle = app.state.web_login_throttle
    assert len(throttle) == web_auth_module.LOGIN_THROTTLE_MAX_ENTRIES
    assert ("filler-0", "client-0.test") not in throttle
    assert ("overflow", "client-overflow.test") in throttle

    client = TestClient(app, client=("203.0.113.10", 50000))
    headers = _csrf_headers(client)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def test_login_throttle_entry_cap_records_client_overflow_when_all_entries_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: now)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_ENTRIES", 2)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_FAILURES", 1)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    settings = app.state.web_settings

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_ENTRIES):
        request = SimpleNamespace(
            app=app,
            client=SimpleNamespace(host=f"203.0.113.{index}"),
            headers={},
        )
        web_auth_module._record_login_failure(request, settings, f"filler-{index}")
    overflow_request = SimpleNamespace(
        app=app,
        client=SimpleNamespace(host="203.0.113.99"),
        headers={},
    )
    web_auth_module._record_login_failure(overflow_request, settings, "overflow")

    client = TestClient(app, client=("203.0.113.99", 50000))
    headers = _csrf_headers(client)
    locked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    throttle = app.state.web_login_throttle
    client_throttle = app.state.web_login_client_throttle
    assert len(throttle) == web_auth_module.LOGIN_THROTTLE_MAX_ENTRIES
    assert ("overflow", "203.0.113.99") not in throttle
    assert client_throttle["203.0.113.99"].locked_until > now
    _assert_generic_auth_failed(locked_response)

    now += web_auth_module.LOGIN_THROTTLE_COOLDOWN_SECONDS + 0.1
    unlocked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    assert unlocked_response.status_code == 200
    assert unlocked_response.json()["authenticated"] is True


def test_login_throttle_client_overflow_does_not_lock_out_other_clients(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_ENTRIES", 2)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_FAILURES", 1)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    abusive_client = TestClient(app, client=("203.0.113.99", 50000))
    other_client = TestClient(app, client=("203.0.113.100", 50000))
    abusive_headers = _csrf_headers(abusive_client)
    other_headers = _csrf_headers(other_client)

    for username in ("filler-0", "filler-1", "overflow"):
        _assert_generic_auth_failed(
            abusive_client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": "wrong"},
                headers=abusive_headers,
            )
        )
    abusive_login = abusive_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=abusive_headers,
    )
    other_login = other_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=other_headers,
    )

    _assert_generic_auth_failed(abusive_login)
    assert other_login.status_code == 200
    assert other_login.json()["authenticated"] is True


def test_login_throttle_client_overflow_cap_evicts_oldest_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_ENTRIES", 1)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_FAILURES", 1)
    monkeypatch.setattr(web_auth_module, "LOGIN_THROTTLE_MAX_CLIENT_ENTRIES", 2)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    settings = app.state.web_settings
    seed_request = SimpleNamespace(
        app=app,
        client=SimpleNamespace(host="203.0.113.9"),
        headers={},
    )
    web_auth_module._record_login_failure(seed_request, settings, "seed")

    for index in range(3):
        request = SimpleNamespace(
            app=app,
            client=SimpleNamespace(host=f"203.0.113.{index + 10}"),
            headers={},
        )
        web_auth_module._record_login_failure(request, settings, f"overflow-{index}")

    client_throttle = app.state.web_login_client_throttle
    assert len(client_throttle) == web_auth_module.LOGIN_THROTTLE_MAX_CLIENT_ENTRIES
    assert "203.0.113.10" not in client_throttle
    assert "203.0.113.11" in client_throttle
    assert "203.0.113.12" in client_throttle


def test_login_throttle_locks_client_across_usernames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client_a = TestClient(app, client=("203.0.113.10", 50000))
    client_b = TestClient(app, client=("203.0.113.11", 50000))
    headers_a = _csrf_headers(client_a)
    headers_b = _csrf_headers(client_b)

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client_a.post(
                "/api/v1/auth/login",
                json={"username": "missing", "password": "wrong"},
                headers=headers_a,
            )
        )
    same_address_different_user = client_a.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers_a,
    )

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client_a.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=headers_a,
            )
        )
    different_address_same_user = client_b.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers_b,
    )

    _assert_generic_auth_failed(same_address_different_user)
    assert different_address_same_user.status_code == 200


def test_login_throttle_uses_trusted_forwarded_client_address(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(
        environ=_web_env(
            tmp_path,
            {"WUD_WEB_TRUSTED_PROXIES": "10.0.0.1/32"},
        )
    )
    setup_client = TestClient(app, client=("10.0.0.1", 50000))
    _setup_admin(setup_client)
    proxy_client = TestClient(app, client=("10.0.0.1", 50000))
    headers = _csrf_headers(proxy_client)
    forwarded_a = {**headers, "X-Forwarded-For": "198.51.100.10"}
    forwarded_b = {**headers, "X-Forwarded-For": "198.51.100.11"}

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            proxy_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=forwarded_a,
            )
        )
    different_forwarded_address = proxy_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=forwarded_b,
    )

    assert different_forwarded_address.status_code == 200


def test_login_throttle_prefers_proxy_x_forwarded_for_over_client_forwarded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(
        environ=_web_env(
            tmp_path,
            {"WUD_WEB_TRUSTED_PROXIES": "10.0.0.1/32"},
        )
    )
    setup_client = TestClient(app, client=("10.0.0.1", 50000))
    _setup_admin(setup_client)
    proxy_client = TestClient(app, client=("10.0.0.1", 50000))
    headers = {**_csrf_headers(proxy_client), "X-Forwarded-For": "198.51.100.10"}

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            proxy_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={**headers, "Forwarded": f"for=203.0.113.{index}"},
            )
        )
    locked_response = proxy_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers={**headers, "Forwarded": "for=203.0.113.99"},
    )

    _assert_generic_auth_failed(locked_response)


def test_login_throttle_ignores_untrusted_forwarded_client_address(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app, client=("203.0.113.10", 50000))
    _setup_admin(setup_client)
    client = TestClient(app, client=("203.0.113.10", 50000))
    headers = _csrf_headers(client)

    for index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES):
        _assert_generic_auth_failed(
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers={
                    **headers,
                    "X-Forwarded-For": f"198.51.100.{index + 10}",
                },
            )
        )
    locked_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers={**headers, "X-Forwarded-For": "198.51.100.99"},
    )

    _assert_generic_auth_failed(locked_response)


def test_successful_login_clears_failed_login_throttle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client = TestClient(app)
    headers = _csrf_headers(client)

    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES - 1):
        _assert_generic_auth_failed(
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=headers,
            )
        )
    first_success = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )
    one_failure_after_success = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
        headers=headers,
    )
    second_success = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    assert first_success.status_code == 200
    _assert_generic_auth_failed(one_failure_after_success)
    assert second_success.status_code == 200


def test_login_failures_use_same_generic_response(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)
    headers = _csrf_headers(client)

    unknown_user = client.post(
        "/api/v1/auth/login",
        json={"username": "missing", "password": "wrong"},
        headers=headers,
    )
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
        headers=headers,
    )
    for _index in range(web_auth_module.LOGIN_THROTTLE_MAX_FAILURES - 1):
        _assert_generic_auth_failed(
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=headers,
            )
        )
    throttled = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    _assert_generic_auth_failed(unknown_user)
    _assert_generic_auth_failed(wrong_password)
    _assert_generic_auth_failed(throttled)


def test_login_requires_csrf_origin_headers(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "origin header is required"


def test_concurrent_login_attempts_cannot_exceed_failure_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_auth_module.time, "monotonic", lambda: 1_000.0)
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client = TestClient(app)
    headers = _csrf_headers(client)
    attempts = 20
    verified = []
    release = threading.Event()

    def slow_failed_verify(_settings, _username, _password):
        verified.append(1)
        release.wait(timeout=10)

    monkeypatch.setattr(web_auth_module, "_verify_web_user", slow_failed_verify)
    responses = []

    def attempt() -> None:
        responses.append(
            client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
                headers=headers,
            )
        )

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    # Requests over the limit are refused without waiting on verification.
    for _ in range(1000):
        if len(responses) >= attempts - web_auth_module.LOGIN_THROTTLE_MAX_FAILURES:
            break
        threading.Event().wait(0.01)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert len(verified) == web_auth_module.LOGIN_THROTTLE_MAX_FAILURES
    assert len(responses) == attempts
    for response in responses:
        _assert_generic_auth_failed(response)
    assert not app.state.web_login_pending
    assert not app.state.web_login_client_pending


def test_login_attempt_reservation_is_released_after_server_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(environ=_web_env(tmp_path))
    setup_client = TestClient(app)
    _setup_admin(setup_client)
    client = TestClient(app, raise_server_exceptions=False)

    def failing_verify(_settings, _username, _password):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(web_auth_module, "_verify_web_user", failing_verify)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert not app.state.web_login_pending
    assert not app.state.web_login_client_pending


def test_unknown_username_runs_the_same_password_hash_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    settings = setup_client.app.state.web_settings
    real_hasher = web_auth_module.PASSWORD_HASHER
    verified_hashes = []

    class RecordingHasher:
        def verify(self, password_hash, password):
            verified_hashes.append(password_hash)
            return real_hasher.verify(password_hash, password)

        def hash(self, password):
            return real_hasher.hash(password)

        def check_needs_rehash(self, password_hash):
            return real_hasher.check_needs_rehash(password_hash)

    monkeypatch.setattr(web_auth_module, "_password_hasher", RecordingHasher)

    assert web_auth_module._verify_web_user(settings, "missing", "wrong") is None
    assert web_auth_module._verify_web_user(settings, "admin", "wrong") is None

    assert len(verified_hashes) == 2
    assert verified_hashes[0] == web_auth_module._dummy_password_hash()
    assert verified_hashes[0].startswith("$argon2")
    assert verified_hashes[1] != verified_hashes[0]
