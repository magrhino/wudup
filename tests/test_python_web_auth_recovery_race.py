from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from tests.web_test_helpers import (
    _assert_generic_auth_failed,
    _client,
    _csrf_headers,
    _setup_admin,
)

from wudup import web_auth as auth
from wudup.db import open_db

OLD_PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "new correct horse battery"


@pytest.mark.parametrize("entrypoint", ["login", "setup", "recovery"])
@pytest.mark.parametrize("redeem", [False, True])
def test_recovery_before_session_insert_rejects_stale_credentials(
    tmp_path: Path, monkeypatch, entrypoint: str, redeem: bool
) -> None:
    setup_client = _client(tmp_path)
    settings = setup_client.app.state.web_settings
    if entrypoint != "setup":
        _setup_admin(setup_client)
    client = _client(tmp_path)
    payload = {"username": "admin", "password": OLD_PASSWORD}
    endpoint = "/api/v1/auth/login"
    if entrypoint == "setup":
        endpoint = "/api/v1/setup/claim"
        payload["claim"] = client.app.state.web_setup_claim
    elif entrypoint == "recovery":
        endpoint = "/api/v1/auth/reset-admin/claim"
        payload["claim"] = auth.issue_admin_recovery_claim(settings, "admin").claim

    recovery = None
    original_user_agent_hash = auth._user_agent_hash

    def recover_before_insert(request):
        nonlocal recovery
        # This runs while preparing INSERT arguments, after credential checks.
        # Recovery commits on a separate connection before the insert resumes.
        recovery = auth.issue_admin_recovery_claim(settings, "admin")
        if redeem:
            auth._redeem_admin_recovery_claim(
                settings, claim=recovery.claim, username="admin", password=NEW_PASSWORD
            )
        return original_user_agent_hash(request)

    with monkeypatch.context() as patch:
        patch.setattr(auth, "_user_agent_hash", recover_before_insert)
        response = client.post(endpoint, json=payload, headers=_csrf_headers(client))

    assert recovery is not None
    _assert_generic_auth_failed(response)
    assert client.cookies.get(auth.SESSION_COOKIE) is None
    assert client.get("/api/v1/auth/session").json()["authenticated"] is False
    assert client.get("/api/v1/status").status_code == 401
    with open_db(settings.config.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NULL"
        ).fetchone()[0] == 0
    if entrypoint == "login":
        assert sum(
            entry.failures for entry in client.app.state.web_login_throttle.values()
        ) == 1

    if not redeem:
        recovered = client.post(
            "/api/v1/auth/reset-admin/claim",
            json={"claim": recovery.claim, "username": "admin", "password": NEW_PASSWORD},
            headers=_csrf_headers(client),
        )
        assert recovered.status_code == 200
        assert client.get("/api/v1/status").status_code == 200
    normal_client = _client(tmp_path)
    assert normal_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": OLD_PASSWORD},
        headers=_csrf_headers(normal_client),
    ).status_code == 401
    assert normal_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": NEW_PASSWORD},
        headers=_csrf_headers(normal_client),
    ).status_code == 200
    assert normal_client.get("/api/v1/status").status_code == 200


@pytest.mark.parametrize("redeem", [False, True])
def test_recovery_during_rehash_cannot_restore_old_password(
    tmp_path: Path, monkeypatch, redeem: bool
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    settings = setup_client.app.state.web_settings
    client = _client(tmp_path)
    hasher = PasswordHasher(time_cost=2)
    recovered_hash = None

    def recover_before_rehash_update(password):
        nonlocal recovered_hash
        result = hasher.hash(password)
        if password == OLD_PASSWORD:
            recovery = auth.issue_admin_recovery_claim(settings, "admin")
            if redeem:
                auth._redeem_admin_recovery_claim(
                    settings,
                    claim=recovery.claim,
                    username="admin",
                    password=NEW_PASSWORD,
                )
            with open_db(settings.config.db_path) as conn:
                recovered_hash = conn.execute(
                    "SELECT password_hash FROM web_users WHERE username = 'admin'"
                ).fetchone()[0]
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            auth,
            "PASSWORD_HASHER",
            SimpleNamespace(
                verify=hasher.verify,
                check_needs_rehash=hasher.check_needs_rehash,
                hash=recover_before_rehash_update,
            ),
        )
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": OLD_PASSWORD},
            headers=_csrf_headers(client),
        )

    assert recovered_hash is not None
    _assert_generic_auth_failed(response)
    assert client.cookies.get(auth.SESSION_COOKIE) is None
    assert client.get("/api/v1/status").status_code == 401
    with open_db(settings.config.db_path) as conn:
        assert conn.execute(
            "SELECT password_hash FROM web_users WHERE username = 'admin'"
        ).fetchone()[0] == recovered_hash
    assert client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": OLD_PASSWORD},
        headers=_csrf_headers(client),
    ).status_code == 401
    if redeem:
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": NEW_PASSWORD},
            headers=_csrf_headers(client),
        ).status_code == 200
        assert client.get("/api/v1/status").status_code == 200


def test_login_rehash_preserves_password_and_issues_usable_session(
    tmp_path: Path, monkeypatch
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    settings = setup_client.app.state.web_settings
    hasher = PasswordHasher(time_cost=2)
    monkeypatch.setattr(auth, "PASSWORD_HASHER", hasher)
    client = _client(tmp_path)
    with open_db(settings.config.db_path) as conn:
        old_hash = conn.execute("SELECT password_hash FROM web_users").fetchone()[0]
    assert hasher.check_needs_rehash(old_hash)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": OLD_PASSWORD},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert client.get("/api/v1/status").status_code == 200
    with open_db(settings.config.db_path) as conn:
        new_hash = conn.execute("SELECT password_hash FROM web_users").fetchone()[0]
    assert new_hash != old_hash
    assert hasher.verify(new_hash, OLD_PASSWORD)
    assert not hasher.check_needs_rehash(new_hash)
    assert setup_client.get("/api/v1/status").status_code == 200


def test_concurrent_logins_can_rehash_the_same_password(tmp_path: Path, monkeypatch) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    first_client = _client(tmp_path)
    second_client = _client(tmp_path)
    second_headers = _csrf_headers(second_client)
    hasher = PasswordHasher(time_cost=2)
    second_response = None
    hashing = False

    def login_before_rehash_update(password):
        nonlocal second_response, hashing
        result = hasher.hash(password)
        if not hashing:
            hashing = True
            second_response = second_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": OLD_PASSWORD},
                headers=second_headers,
            )
        return result

    monkeypatch.setattr(
        auth,
        "PASSWORD_HASHER",
        SimpleNamespace(
            verify=hasher.verify,
            check_needs_rehash=hasher.check_needs_rehash,
            hash=login_before_rehash_update,
        ),
    )
    first_response = first_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": OLD_PASSWORD},
        headers=_csrf_headers(first_client),
    )

    assert second_response is not None
    assert second_response.status_code == 200
    assert first_response.status_code == 200
    for client in (first_client, second_client):
        assert client.get("/api/v1/status").status_code == 200
        assert not client.app.state.web_login_throttle
