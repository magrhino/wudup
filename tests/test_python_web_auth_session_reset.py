from __future__ import annotations

import json
from pathlib import Path

from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _setup_admin,
    _web_env,
)

from wudup import web_auth as web_auth_module
from wudup import web_config
from wudup.db import (
    init_db,
    open_db,
)


def test_session_endpoint_reports_cookie_auth_state(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)
    before = client.get("/api/v1/auth/session")

    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(client),
    )
    after = client.get("/api/v1/auth/session")

    assert before.status_code == 200
    assert before.json()["authenticated"] is False
    assert after.status_code == 200
    assert after.json()["authenticated"] is True


def test_authenticated_get_does_not_touch_session_last_seen(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)
    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(client),
    )
    assert login_response.status_code == 200
    session_cookie = client.cookies.get(web_auth_module.SESSION_COOKIE)
    assert session_cookie

    db_path = tmp_path / "state" / "wud.sqlite"
    sentinel = "2000-01-01T00:00:00+00:00"
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            conn.execute("UPDATE web_sessions SET last_seen_at = ?", (sentinel,))

    status_response = client.get("/api/v1/status")

    with open_db(db_path) as conn:
        last_seen = conn.execute(
            "SELECT last_seen_at FROM web_sessions WHERE id_hash = ?",
            (web_auth_module._secret_hash(session_cookie),),
        ).fetchone()["last_seen_at"]

    assert status_response.status_code == 200
    assert last_seen == sentinel


def test_logout_clears_session_and_csrf_cookies(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)
    headers = _csrf_headers(client)
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=headers,
    )

    response = client.post("/api/v1/auth/logout", headers=headers)
    status_response = client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert client.cookies.get("wud_session") is None
    assert client.cookies.get("wud_csrf_token") is None
    assert status_response.status_code == 401


def test_admin_reset_claim_revokes_sessions_invalidates_password_and_audits(
    tmp_path: Path,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    logged_in_client = _client(tmp_path)
    login_response = logged_in_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(logged_in_client),
    )
    assert login_response.status_code == 200

    recovery = web_auth_module.issue_admin_recovery_claim(
        setup_client.app.state.web_settings,
        "admin",
    )
    status_response = logged_in_client.get("/api/v1/status")
    old_login_client = _client(tmp_path)
    old_login_response = old_login_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(old_login_client),
    )

    assert recovery.claim
    assert recovery.username == "admin"
    assert recovery.revoked_sessions >= 2
    assert status_response.status_code == 401
    assert old_login_response.status_code == 401
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        reset_hash = web_auth_module._web_setting(
            conn,
            web_auth_module.RESET_ADMIN_CLAIM_HASH_KEY,
        )
        sessions = conn.execute("SELECT revoked_at FROM web_sessions").fetchall()
        audit_rows = conn.execute(
            """
            SELECT metadata_json
            FROM update_runs
            WHERE mode = 'web-auth'
            ORDER BY id
            """
        ).fetchall()

    assert reset_hash
    assert reset_hash != recovery.claim
    assert all(row["revoked_at"] for row in sessions)
    audit = [json.loads(row["metadata_json"]) for row in audit_rows]
    assert audit[-1]["operation"] == "admin_reset_claim_issued"
    assert audit[-1]["source"] == "cli"
    assert audit[-1]["target"]["username"] == "admin"
    assert recovery.claim not in json.dumps(audit)


def test_admin_reset_claim_redeems_once_and_allows_new_password(
    tmp_path: Path,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    recovery = web_auth_module.issue_admin_recovery_claim(
        setup_client.app.state.web_settings,
        "admin",
    )
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": recovery.claim,
            "username": "admin",
            "password": "new correct horse battery",
        },
        headers=_csrf_headers(client),
    )
    replay_response = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": recovery.claim,
            "username": "admin",
            "password": "new correct horse battery",
        },
        headers=_csrf_headers(client),
    )
    old_login_client = _client(tmp_path)
    old_login_response = old_login_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers=_csrf_headers(old_login_client),
    )
    new_login_client = _client(tmp_path)
    new_login_response = new_login_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "new correct horse battery"},
        headers=_csrf_headers(new_login_client),
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert "wud_session=" in response.headers["set-cookie"]
    assert replay_response.status_code == 403
    assert replay_response.json()["detail"] == "admin recovery claim is invalid"
    assert old_login_response.status_code == 401
    assert new_login_response.status_code == 200
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        assert (
            web_auth_module._web_setting(
                conn,
                web_auth_module.RESET_ADMIN_CLAIM_HASH_KEY,
            )
            == ""
        )
        audit_rows = conn.execute(
            """
            SELECT metadata_json
            FROM update_runs
            WHERE mode = 'web-auth'
            ORDER BY id
            """
        ).fetchall()
    audit = [json.loads(row["metadata_json"]) for row in audit_rows]
    assert [row["operation"] for row in audit] == [
        "admin_reset_claim_issued",
        "admin_reset_password_changed",
    ]


def test_admin_reset_rejects_invalid_claim_without_burning_valid_claim(
    tmp_path: Path,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    recovery = web_auth_module.issue_admin_recovery_claim(
        setup_client.app.state.web_settings,
        "admin",
    )
    client = _client(tmp_path)

    invalid = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": "not-the-claim",
            "username": "admin",
            "password": "new correct horse battery",
        },
        headers=_csrf_headers(client),
    )
    valid = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": recovery.claim,
            "username": "admin",
            "password": "new correct horse battery",
        },
        headers=_csrf_headers(client),
    )

    assert invalid.status_code == 403
    assert invalid.json()["detail"] == "admin recovery claim is invalid"
    assert valid.status_code == 200


def test_admin_reset_rejects_expired_claim_without_creating_session(
    tmp_path: Path,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    recovery = web_auth_module.issue_admin_recovery_claim(
        setup_client.app.state.web_settings,
        "admin",
    )
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                UPDATE web_settings
                SET value = '2000-01-01T00:00:00+00:00'
                WHERE key = ?
                """,
                (web_auth_module.RESET_ADMIN_CLAIM_EXPIRES_KEY,),
            )
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": recovery.claim,
            "username": "admin",
            "password": "new correct horse battery",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "admin recovery claim expired"
    assert client.cookies.get("wud_session") is None


def test_admin_reset_claim_requires_csrf_origin_headers(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    recovery = web_auth_module.issue_admin_recovery_claim(
        setup_client.app.state.web_settings,
        "admin",
    )
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/auth/reset-admin/claim",
        json={
            "claim": recovery.claim,
            "username": "admin",
            "password": "new correct horse battery",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "origin header is required"


def test_admin_reset_command_errors_for_missing_setup_and_unknown_user(
    tmp_path: Path,
) -> None:
    missing_settings = web_config.load_web_settings(_web_env(tmp_path))
    try:
        web_auth_module.issue_admin_recovery_claim(missing_settings, "admin")
        raise AssertionError("missing database should fail")
    except web_auth_module.WebAdminResetError as exc:
        assert "database file does not exist" in str(exc)
    assert not (tmp_path / "state" / "wud.sqlite").exists()

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
    try:
        web_auth_module.issue_admin_recovery_claim(missing_settings, "admin")
        raise AssertionError("incomplete setup should fail")
    except web_auth_module.WebAdminResetError as exc:
        assert "setup is not complete" in str(exc)

    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    try:
        web_auth_module.issue_admin_recovery_claim(
            setup_client.app.state.web_settings,
            "other",
        )
        raise AssertionError("unknown user should fail")
    except web_auth_module.WebAdminResetError as exc:
        assert "active admin user not found" in str(exc)
