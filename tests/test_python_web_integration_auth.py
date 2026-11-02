from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.web_test_helpers import (
    TEST_FILE_PENDING_SOURCE_VALUE,
    _csrf_headers,
    _setup_admin,
    _web_env,
)

from wudup import web as web_module
from wudup import web_integration_auth as token_module
from wudup.cli import build_parser
from wudup.web import create_app
from wudup.web_auth import (
    LOGIN_THROTTLE_COOLDOWN_SECONDS,
    LOGIN_THROTTLE_MAX_FAILURES,
    WebConfigError,
)
from wudup.web_redaction import redact_sensitive_text

ADMIN_TOKEN = "a" * 40
ENV_TOKEN = "e" * 40
SUMMARY = "/api/integrations/v1/summary"
ROTATE = "/api/v1/settings/readonly-token"


def _app(tmp_path: Path, env: dict[str, str] | None = None, *, setup: bool = True):
    values = _web_env(
        tmp_path,
        {"WUD_PENDING_SOURCE": TEST_FILE_PENDING_SOURCE_VALUE, **(env or {})},
    )
    app = create_app(environ=values)
    admin = TestClient(app)
    if setup:
        _setup_admin(admin)
    return app, admin


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rotate(admin: TestClient):
    return admin.post(ROTATE, json={"action": "rotate"}, headers=_csrf_headers(admin))


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "wud.sqlite"


def _api_routes(routes) -> list[APIRoute]:
    # Included routers wrap their routes; walk them to see every endpoint.
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(_api_routes(route.original_router.routes))
    return found


def _mutations_app(tmp_path: Path, env: dict[str, str] | None = None):
    return _app(tmp_path, {"WUD_WEB_MUTATIONS_ENABLED": "true", **(env or {})})


def test_env_token_and_file_form_are_accepted(tmp_path: Path) -> None:
    token_file = tmp_path / "readonly-token"
    token_file.write_text(f"{ENV_TOKEN}\n", encoding="utf-8")
    for env in (
        {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN},
        {"WUD_WEB_READONLY_TOKEN_FILE": str(token_file)},
    ):
        root = tmp_path / next(iter(env))
        root.mkdir()
        app, _admin = _app(root, env)
        response = TestClient(app).get(SUMMARY, headers=_bearer(ENV_TOKEN))
        assert response.status_code == 200
        assert response.json()["v"] == 1


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"WUD_WEB_READONLY_TOKEN": "short"}, "at least 32 characters"),
        (
            {"WUD_WEB_READONLY_TOKEN": ADMIN_TOKEN, "WUD_WEB_TOKEN": ADMIN_TOKEN},
            "must differ from WUD_WEB_TOKEN",
        ),
        (
            {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN, "WUD_WEB_READONLY_TOKEN_FILE": "/x"},
            "cannot both be set",
        ),
    ],
)
def test_invalid_env_token_stops_startup(
    tmp_path: Path,
    env: dict[str, str],
    message: str,
) -> None:
    environ = _web_env(tmp_path, env)

    with pytest.raises(WebConfigError, match=message):
        create_app(environ=environ)


def test_env_token_locks_webui_rotation(tmp_path: Path) -> None:
    _app_instance, admin = _mutations_app(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN})

    status = admin.get("/api/v1/settings").json()["readonly_token"]
    response = _rotate(admin)

    assert status["source"] == "env"
    assert status["editable"] is False
    assert "container config" in status["disabled_reason"]
    assert status["fingerprint"] == hashlib.sha256(ENV_TOKEN.encode()).hexdigest()[:8]
    assert response.status_code == 409
    assert "WUD_WEB_READONLY_TOKEN" in response.json()["detail"]


def test_webui_rotation_requires_mutations(tmp_path: Path) -> None:
    _app_instance, admin = _app(tmp_path)

    status = admin.get("/api/v1/settings").json()["readonly_token"]
    response = _rotate(admin)

    assert status == {
        "source": "none",
        "fingerprint": "",
        "created_at": "",
        "editable": False,
        "revocable": False,
        "stored_fingerprint": "",
        "disabled_reason": token_module.MUTATIONS_DISABLED_REASON,
    }
    assert response.status_code == 403
    assert response.json() == {"detail": "mutations are disabled"}


def test_rotation_needs_admin_auth_and_csrf(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path)
    anonymous = TestClient(app)

    no_csrf = admin.post(ROTATE, json={"action": "rotate"})
    no_auth = anonymous.post(
        ROTATE,
        json={"action": "rotate"},
        headers=_csrf_headers(anonymous),
    )

    assert no_csrf.status_code == 403
    assert no_auth.status_code == 401


def test_rotate_stores_only_a_hash_and_replaces_the_old_token(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path)
    reader = TestClient(app)

    first = _rotate(admin)
    second = _rotate(admin)

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    first_token = first.json()["token"]
    second_token = second.json()["token"]
    assert first_token.startswith("wudup_ro_")
    assert first_token != second_token
    assert reader.get(SUMMARY, headers=_bearer(first_token)).status_code == 401
    assert reader.get(SUMMARY, headers=_bearer(second_token)).status_code == 200

    digest = hashlib.sha256(second_token.encode()).hexdigest()
    with sqlite3.connect(_db_path(tmp_path)) as conn:
        settings_rows = dict(
            conn.execute(
                "SELECT key, value FROM web_settings WHERE key LIKE 'readonly_token.%'"
            ).fetchall()
        )
        audit = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT metadata_json FROM update_events WHERE image = 'readonly-token'"
            ).fetchall()
        ]
    assert settings_rows["readonly_token.hash"] == digest
    assert settings_rows["readonly_token.fingerprint"] == digest[:8]
    dumped = json.dumps([settings_rows, audit])
    assert first_token not in dumped
    assert second_token not in dumped
    assert [event["operation"] for event in audit] == ["rotate_readonly_token"] * 2
    assert audit[-1]["after"] == {"fingerprint": digest[:8]}
    assert digest not in json.dumps(audit)
    status = second.json()["status"]
    assert status["source"] == "webui"
    assert status["fingerprint"] == digest[:8]


def test_revoke_stops_the_token_immediately(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path)
    token = _rotate(admin).json()["token"]

    response = admin.post(ROTATE, json={"action": "revoke"}, headers=_csrf_headers(admin))

    assert response.status_code == 200
    assert response.json()["token"] == ""
    assert response.json()["status"]["source"] == "none"
    assert TestClient(app).get(SUMMARY, headers=_bearer(token)).status_code == 401


def test_cli_revoke_works_with_mutations_off(tmp_path: Path, monkeypatch) -> None:
    app, admin = _mutations_app(tmp_path)
    token = _rotate(admin).json()["token"]
    for key, value in _web_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    args = build_parser().parse_args(["web", "readonly-token", "revoke"])

    exit_code = web_module.run_web_from_namespace(args)

    assert exit_code == 0
    assert TestClient(app).get(SUMMARY, headers=_bearer(token)).status_code == 401
    with sqlite3.connect(_db_path(tmp_path)) as conn:
        source = conn.execute(
            "SELECT json_extract(metadata_json, '$.source') FROM update_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert source == "cli"


def test_cli_revoke_with_nothing_stored_changes_nothing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _app(tmp_path)
    for key, value in _web_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    exit_code = web_module.run_web_readonly_token_from_namespace(
        SimpleNamespace(token_action="revoke")
    )

    assert exit_code == 0
    assert "nothing to revoke" in capsys.readouterr().out
    with sqlite3.connect(_db_path(tmp_path)) as conn:
        audits = conn.execute(
            "SELECT COUNT(*) FROM update_events WHERE image = 'readonly-token'"
        ).fetchone()[0]
    assert audits == 0


def test_webui_revoke_with_nothing_stored_is_not_found(tmp_path: Path) -> None:
    _app_instance, admin = _mutations_app(tmp_path)

    response = admin.post(ROTATE, json={"action": "revoke"}, headers=_csrf_headers(admin))

    assert response.status_code == 404
    assert "nothing to revoke" in response.json()["detail"]


def test_stored_token_stays_visible_and_revocable_under_env_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _first_app, first_admin = _mutations_app(tmp_path)
    stored = _rotate(first_admin).json()["token"]
    stored_fingerprint = hashlib.sha256(stored.encode()).hexdigest()[:8]
    env = {"WUD_WEB_MUTATIONS_ENABLED": "true", "WUD_WEB_READONLY_TOKEN": ENV_TOKEN}
    app = create_app(
        environ=_web_env(tmp_path, {"WUD_PENDING_SOURCE": TEST_FILE_PENDING_SOURCE_VALUE, **env})
    )
    admin = TestClient(app)
    admin.cookies.update(first_admin.cookies)
    reader = TestClient(app)

    status = admin.get("/api/v1/settings").json()["readonly_token"]
    rotate = _rotate(admin)
    revoke = admin.post(ROTATE, json={"action": "revoke"}, headers=_csrf_headers(admin))

    # Container config wins while set; the dormant WebUI token is not accepted.
    assert reader.get(SUMMARY, headers=_bearer(stored)).status_code == 401
    assert reader.get(SUMMARY, headers=_bearer(ENV_TOKEN)).status_code == 200
    assert status["source"] == "env"
    assert status["stored_fingerprint"] == stored_fingerprint
    assert status["revocable"] is True
    assert token_module.DORMANT_STORED_REASON in status["disabled_reason"]
    assert rotate.status_code == 409
    assert revoke.status_code == 200
    assert revoke.json()["status"]["stored_fingerprint"] == ""
    assert revoke.json()["status"]["revocable"] is False

    # Without the container setting, the revoked WebUI token stays dead.
    plain_app, _plain_admin = _app(tmp_path, setup=False)
    assert TestClient(plain_app).get(SUMMARY, headers=_bearer(stored)).status_code == 401


def test_cli_revoke_clears_a_dormant_token_under_env(tmp_path: Path, monkeypatch) -> None:
    _app_instance, admin = _mutations_app(tmp_path)
    _rotate(admin)
    for key, value in _web_env(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN}).items():
        monkeypatch.setenv(key, value)

    exit_code = web_module.run_web_readonly_token_from_namespace(
        SimpleNamespace(token_action="revoke")
    )

    assert exit_code == 0
    with sqlite3.connect(_db_path(tmp_path)) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM web_settings WHERE key = 'readonly_token.hash'"
        ).fetchone()[0]
    assert stored == 0


def test_locked_out_client_skips_the_token_lookup(tmp_path: Path, monkeypatch) -> None:
    app, _admin = _app(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN})
    client = TestClient(app, client=("198.51.100.40", 50000))
    for index in range(LOGIN_THROTTLE_MAX_FAILURES):
        client.get(SUMMARY, headers=_bearer(f"guess-{index}"))

    def fail_lookup(_settings):
        raise AssertionError("locked-out requests must not read the token")

    monkeypatch.setattr(token_module, "_active_token_hash", fail_lookup)

    assert client.get(SUMMARY, headers=_bearer(ENV_TOKEN)).status_code == 401


def test_dormant_reason_points_to_cli_when_webui_changes_are_off(tmp_path: Path) -> None:
    _first_app, first_admin = _mutations_app(tmp_path)
    _rotate(first_admin)
    app = create_app(
        environ=_web_env(
            tmp_path,
            {
                "WUD_PENDING_SOURCE": TEST_FILE_PENDING_SOURCE_VALUE,
                "WUD_WEB_READONLY_TOKEN": ENV_TOKEN,
            },
        )
    )
    admin = TestClient(app)
    admin.cookies.update(first_admin.cookies)

    status = admin.get("/api/v1/settings").json()["readonly_token"]
    revoke = admin.post(ROTATE, json={"action": "revoke"}, headers=_csrf_headers(admin))

    assert status["revocable"] is False
    assert token_module.DORMANT_STORED_CLI_REASON in status["disabled_reason"]
    assert revoke.status_code == 403


def test_throttle_compares_and_records_under_one_lock(tmp_path: Path, monkeypatch) -> None:
    app, _admin = _app(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN})
    lock = app.state.web_login_throttle_lock
    original = token_module.secrets.compare_digest
    held: list[bool] = []

    def compare_digest(left, right):
        held.append(lock.locked())
        return original(left, right)

    monkeypatch.setattr(token_module.secrets, "compare_digest", compare_digest)

    TestClient(app).get(SUMMARY, headers=_bearer("wrong"))

    assert held == [True]


def test_readonly_token_cannot_use_any_admin_route(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path, {"WUD_WEB_TOKEN": ADMIN_TOKEN})
    token = _rotate(admin).json()["token"]
    # A separate address each probe keeps the wrong-token throttle out of the way.
    checked = 0
    for index, route in enumerate(_api_routes(app.routes)):
        if not route.path.startswith("/api/v1/"):
            continue
        if route.path.startswith(("/api/v1/setup/", "/api/v1/auth/")):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        client = TestClient(app, client=(f"198.51.100.{index % 250}", 40000 + index))
        for method in route.methods:
            headers = {**_bearer(token), **_csrf_headers(client)}
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 401, (method, route.path, response.text)
            checked += 1
    assert checked > 40


def test_integration_routes_reject_admin_token_session_and_query_token(
    tmp_path: Path,
) -> None:
    app, admin = _mutations_app(tmp_path, {"WUD_WEB_TOKEN": ADMIN_TOKEN})
    token = _rotate(admin).json()["token"]

    assert admin.get(SUMMARY).status_code == 401
    assert TestClient(app).get(SUMMARY, headers=_bearer(ADMIN_TOKEN)).status_code == 401
    assert TestClient(app).get(f"{SUMMARY}?token={token}").status_code == 401


def test_integration_routes_are_read_only(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path)
    token = _rotate(admin).json()["token"]
    reader = TestClient(app)

    integration_routes = [
        route
        for route in _api_routes(app.routes)
        if route.path.startswith("/api/integrations/")
    ]

    assert {route.path for route in integration_routes} == {
        "/api/integrations/v1/summary",
        "/api/integrations/v1/services/{stack}/{service}",
    }
    assert all(route.methods <= {"GET", "HEAD"} for route in integration_routes)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert reader.request(method, SUMMARY, headers=_bearer(token)).status_code == 405


def test_readonly_token_dependency_rejects_non_read_methods() -> None:
    request = SimpleNamespace(method="POST")
    with pytest.raises(HTTPException) as raised:
        token_module.require_readonly_token(request, authorization="Bearer x")  # type: ignore[arg-type]
    assert raised.value.status_code == 405


def test_integration_routes_need_setup(tmp_path: Path) -> None:
    app, _admin = _app(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN}, setup=False)

    response = TestClient(app).get(SUMMARY, headers=_bearer(ENV_TOKEN))

    assert response.status_code == 403
    assert response.json() == {"detail": "setup required"}


def test_wrong_readonly_tokens_lock_only_the_readonly_bucket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = {"value": 1_000.0}
    monkeypatch.setattr(
        token_module, "time", SimpleNamespace(monotonic=lambda: now["value"])
    )
    app, _admin = _app(
        tmp_path,
        {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN, "WUD_WEB_TOKEN": ADMIN_TOKEN},
    )
    client = TestClient(app, client=("198.51.100.30", 50000))
    other = TestClient(app, client=("198.51.100.31", 50000))

    for index in range(LOGIN_THROTTLE_MAX_FAILURES):
        assert client.get(SUMMARY, headers=_bearer(f"guess-{index}")).status_code == 401

    assert client.get(SUMMARY, headers=_bearer(ENV_TOKEN)).status_code == 401
    assert other.get(SUMMARY, headers=_bearer(ENV_TOKEN)).status_code == 200
    assert client.get("/api/v1/status", headers=_bearer(ADMIN_TOKEN)).status_code == 200
    now["value"] += LOGIN_THROTTLE_COOLDOWN_SECONDS
    assert client.get(SUMMARY, headers=_bearer(ENV_TOKEN)).status_code == 200


def test_tokens_are_redacted_and_never_listed(tmp_path: Path) -> None:
    app, admin = _mutations_app(tmp_path, {"WUD_WEB_READONLY_TOKEN": ENV_TOKEN})
    settings = app.state.web_settings
    generated = "wudup_ro_" + "x" * 43

    redacted = redact_sensitive_text(settings, f"failed with {ENV_TOKEN} and {generated}")
    body = admin.get("/api/v1/settings").text

    assert ENV_TOKEN not in redacted
    assert generated not in redacted
    assert ENV_TOKEN not in body
    secrets = {
        item["name"]: item["configured"]
        for item in admin.get("/api/v1/settings").json()["secrets"]
    }
    assert secrets["WUD_WEB_READONLY_TOKEN"] is True
    assert secrets["WUD_WEB_READONLY_TOKEN_FILE"] is False
