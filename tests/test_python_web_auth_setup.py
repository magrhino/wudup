from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

from fastapi import HTTPException
from tests.web_test_helpers import (
    _client,
    _contains_key,
    _csrf_headers,
)

from wudup import web_auth as web_auth_module
from wudup.db import (
    open_db,
)


def test_first_run_setup_claim_creates_admin_and_burns_claim(tmp_path: Path) -> None:
    client = _client(tmp_path)
    claim = client.app.state.web_setup_claim

    before = client.get("/api/v1/setup/status")
    setup_response = client.post(
        "/api/v1/setup/claim",
        json={
            "claim": claim,
            "username": "admin",
            "password": "correct horse battery staple",
        },
        headers=_csrf_headers(client),
    )
    status_response = client.get("/api/v1/status")
    replay_response = client.post(
        "/api/v1/setup/claim",
        json={
            "claim": claim,
            "username": "other",
            "password": "correct horse battery staple",
        },
        headers=_csrf_headers(client),
    )
    after = client.get("/api/v1/setup/status")

    assert before.status_code == 200
    assert before.json()["setup_required"] is True
    assert claim not in before.text
    assert setup_response.status_code == 200
    assert setup_response.json()["authenticated"] is True
    assert setup_response.json()["setup_required"] is False
    assert status_response.status_code == 200
    assert replay_response.status_code == 409
    assert after.json()["setup_required"] is False


def test_first_run_setup_claim_serializes_concurrent_claims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path)
    settings = client.app.state.web_settings
    claim = client.app.state.web_setup_claim
    db_path = tmp_path / "state" / "wud.sqlite"

    class SlowPasswordHasher:
        def __init__(self) -> None:
            self.calls = 0
            self.first_hash_started = Event()
            self.release_first_hash = Event()
            self.lock = Lock()

        def hash(self, password: str) -> str:
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                self.first_hash_started.set()
                assert self.release_first_hash.wait(timeout=5)
            return f"hashed-{password}-{call}"

    hasher = SlowPasswordHasher()
    monkeypatch.setattr(web_auth_module, "PASSWORD_HASHER", hasher)

    def claim_admin(username: str) -> tuple[str, int | str]:
        try:
            user_id, _password_hash = web_auth_module._claim_initial_admin(
                settings,
                claim,
                username,
                "correct horse battery staple",
            )
        except HTTPException as exc:
            return ("error", exc.status_code)
        return ("ok", user_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim_admin, "admin-a")
        assert hasher.first_hash_started.wait(timeout=5)
        second = executor.submit(claim_admin, "admin-b")
        time.sleep(0.1)
        hasher.release_first_hash.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    with open_db(db_path) as conn:
        users = conn.execute("SELECT username FROM web_users").fetchall()

    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert ("error", 409) in results
    assert [row["username"] for row in users] == ["admin-a"]
    assert hasher.calls == 1


def test_setup_claim_rejects_expired_secret(tmp_path: Path) -> None:
    client = _client(tmp_path)
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE web_settings
                SET value = '2000-01-01T00:00:00+00:00'
                WHERE key = 'setup_claim_expires_at'
                """
            )

    response = client.post(
        "/api/v1/setup/claim",
        json={
            "claim": client.app.state.web_setup_claim,
            "username": "admin",
            "password": "correct horse battery staple",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "setup claim expired"


def test_setup_claim_validation_redacts_submitted_inputs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    submitted_password = "tinysecret!"

    response = client.post(
        "/api/v1/setup/claim",
        json={
            "claim": client.app.state.web_setup_claim,
            "username": "admin",
            "password": submitted_password,
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert submitted_password not in response.text
    assert not _contains_key(response.json(), "input")


def test_invalid_setup_claim_does_not_hash_password(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path)

    class ExplodingPasswordHasher:
        def hash(self, _password: str) -> str:
            raise AssertionError("password hash should not be called")

    monkeypatch.setattr(web_auth_module, "PASSWORD_HASHER", ExplodingPasswordHasher())

    response = client.post(
        "/api/v1/setup/claim",
        json={
            "claim": "not-the-claim",
            "username": "admin",
            "password": "correct horse battery staple",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "setup claim is invalid"


def test_safe_exception_detail_redacts_secrets_and_absolute_paths(
    tmp_path: Path,
) -> None:
    secret = "secret-token-value"
    discord_webhook = "discord-webhook-secret"
    legacy_webhook = "legacy-webhook-secret"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_TOKEN": secret,
            "DISCORD_WEBHOOK": discord_webhook,
            "DISCORD_RELEASES_WEBHOOK": legacy_webhook,
        },
    )
    settings = client.app.state.web_settings
    exc = OSError(
        "open failed for "
        f"{tmp_path / 'state' / 'wud.sqlite'}, "
        r"C:\Users\admin\wud.sqlite, "
        r"\\server\share\wud.sqlite "
        f"with token {secret} and webhooks {discord_webhook} {legacy_webhook}"
    )

    detail = web_auth_module._safe_exception_detail(settings, "could not read", exc)

    assert detail.startswith("could not read: open failed for ")
    assert secret not in detail
    assert discord_webhook not in detail
    assert legacy_webhook not in detail
    assert "<redacted>" in detail
    assert str(tmp_path) not in detail
    assert "C:" not in detail
    assert r"\\server" not in detail
    assert "[REDACTED_PATH]" in detail
