from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.web_test_helpers import (
    DEFAULT_CLAIM_PHRASE,
    _client,
    _csrf_headers,
    _setup_admin,
)

from wudup import web_state as state_module
from wudup.db import (
    init_db,
    open_db,
)


def test_state_read_endpoints_return_empty_without_creating_missing_database(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    db_path = root / "wud.sqlite"
    client = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
        create_root=False,
    )

    policies = client.get("/api/v1/service-policies")
    snoozes = client.get("/api/v1/snoozes?state=all")
    exclusions = client.get("/api/v1/tag-exclusions?status=all")

    assert policies.status_code == 200
    assert policies.json() == []
    assert snoozes.status_code == 200
    assert snoozes.json() == []
    assert exclusions.status_code == 200
    assert exclusions.json() == []
    assert not root.exists()
    assert not db_path.exists()


def test_state_read_endpoints_list_existing_sqlite_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "wud.sqlite"
    now = datetime.now(UTC).replace(microsecond=0)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO service_policy (
                    service_key,
                    update_mode,
                    auto_update,
                    snooze_default_seconds,
                    auto_update_time,
                    auto_update_days_json,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES (
                    'stack/app',
                    'stop',
                    0,
                    3600,
                    '09:30',
                    '["mon","wed"]',
                    '2026-05-28T12:00:00+00:00',
                    '2026-05-28T12:01:00+00:00',
                    '{"source":"test"}'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO snoozes (
                    service_key,
                    snoozed_until,
                    reason,
                    created_at,
                    metadata_json
                )
                VALUES ('stack/app', ?, 'maintenance', ?, '{}')
                """,
                (future, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO snoozes (
                    service_key,
                    snoozed_until,
                    reason,
                    created_at,
                    metadata_json
                )
                VALUES ('stack/old', ?, 'expired', ?, '{}')
                """,
                (past, past),
            )
            cursor = conn.execute(
                """
                INSERT INTO update_runs (
                    started_at,
                    finished_at,
                    status,
                    dry_run,
                    mode,
                    wud_file,
                    log_file,
                    metadata_json
                )
                VALUES (?, ?, 'success', 0, 'stop', '', '', '{}')
                """,
                (now.isoformat(), now.isoformat()),
            )
            run_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO update_events (
                    run_id,
                    created_at,
                    service_name,
                    stack_name,
                    image,
                    target_image,
                    status,
                    metadata_json
                )
                VALUES (?, ?, 'db', 'stack', 'repo/db:latest', '', 'success', '{}')
                """,
                (run_id, now.isoformat()),
            )
            conn.execute(
                """
                INSERT INTO dependency_snoozes (
                    service_key,
                    wait_for_service_key,
                    reason,
                    created_at,
                    metadata_json
                )
                VALUES ('stack/worker', 'stack/cache', 'wait for cache', ?, '{}')
                """,
                (past,),
            )
            conn.execute(
                """
                INSERT INTO dependency_snoozes (
                    service_key,
                    wait_for_service_key,
                    reason,
                    created_at,
                    metadata_json
                )
                VALUES ('stack/satisfied', 'stack/db', 'wait for db', ?, '{}')
                """,
                (past,),
            )
            conn.execute(
                """
                INSERT INTO tag_exclusion_rules (
                    scope,
                    image_repo,
                    service_key,
                    match_type,
                    tag,
                    regex_fragment,
                    status,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES
                    (
                        'image_repo',
                        'repo/app',
                        '',
                        'exact',
                        '2.0',
                        '2\\.0',
                        'active',
                        ?,
                        ?,
                        '{}'
                    ),
                    (
                        'service',
                        'repo/app',
                        'stack/app',
                        'exact',
                        '3.0',
                        '3\\.0',
                        'disabled',
                        ?,
                        ?,
                        '{}'
                    )
                """,
                (now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
            )
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    policies = client.get("/api/v1/service-policies")
    active_snoozes = client.get("/api/v1/snoozes")
    expired_snoozes = client.get("/api/v1/snoozes?state=expired")
    all_exclusions = client.get("/api/v1/tag-exclusions?status=all")
    disabled_exclusions = client.get("/api/v1/tag-exclusions?status=disabled")

    assert policies.status_code == 200
    assert policies.json()[0]["service_key"] == "stack/app"
    assert policies.json()[0]["auto_update"] is False
    assert policies.json()[0]["auto_update_time"] == "09:30"
    assert policies.json()[0]["auto_update_days"] == ["mon", "wed"]
    assert policies.json()[0]["metadata"] == {"source": "test"}
    assert active_snoozes.status_code == 200
    assert [row["service_key"] for row in active_snoozes.json()] == [
        "stack/app",
        "stack/worker",
    ]
    assert active_snoozes.json()[0]["active"] is True
    assert active_snoozes.json()[0]["kind"] == "time"
    assert expired_snoozes.status_code == 200
    assert [row["service_key"] for row in expired_snoozes.json()] == [
        "stack/old",
        "stack/satisfied",
    ]
    expired_by_service = {
        row["service_key"]: row
        for row in expired_snoozes.json()
    }
    assert expired_snoozes.json()[0]["active"] is False
    assert expired_by_service["stack/satisfied"]["active"] is False
    assert expired_by_service["stack/satisfied"]["kind"] == "dependency"
    assert expired_by_service["stack/satisfied"]["wait_for_service_key"] == "stack/db"
    active_by_service = {
        row["service_key"]: row
        for row in active_snoozes.json()
    }
    assert active_by_service["stack/worker"]["active"] is True
    assert active_by_service["stack/worker"]["kind"] == "dependency"
    assert active_by_service["stack/worker"]["wait_for_service_key"] == "stack/cache"
    assert all_exclusions.status_code == 200
    assert [row["status"] for row in all_exclusions.json()] == [
        "active",
        "disabled",
    ]
    assert disabled_exclusions.status_code == 200
    assert disabled_exclusions.json()[0]["service_key"] == "stack/app"


def test_state_operation_endpoint_enforces_auth_csrf_and_read_only(
    tmp_path: Path,
) -> None:
    payload = {
        "kind": "upsert_service_policy",
        "service_key": "stack/app",
        "update_mode": "stop",
    }
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
        "/api/v1/state/operations",
        json=payload,
        headers=_csrf_headers(unauthenticated),
    )
    missing_csrf = mutating.post("/api/v1/state/operations", json=payload)
    read_only_response = read_only.post(
        "/api/v1/state/operations",
        json=payload,
        headers=_csrf_headers(read_only),
    )

    assert unauthenticated_response.status_code == 403
    assert unauthenticated_response.json()["detail"] == "setup required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"


def test_state_operations_write_rows_and_audit_entries(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)
    future = (
        datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    ).isoformat()

    policy = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "pause",
            "auto_update": False,
            "snooze_default_seconds": 600,
            "auto_update_time": "09:30",
            "auto_update_days": ["mon", "wed"],
        },
        headers=headers,
    )
    deleted_policy = client.post(
        "/api/v1/state/operations",
        json={"kind": "delete_service_policy", "service_key": "stack/app"},
        headers=headers,
    )
    snooze = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "create_snooze",
            "service_key": "stack/app",
            "snoozed_until": future,
            "reason": "maintenance",
        },
        headers=headers,
    )
    snooze_id = snooze.json()["resource"]["id"]
    deleted_snooze = client.post(
        "/api/v1/state/operations",
        json={"kind": "delete_snooze", "snooze_id": snooze_id},
        headers=headers,
    )
    dependency_snooze = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "create_dependency_snooze",
            "service_key": "stack/app",
            "wait_for_service_key": "stack/db",
            "reason": "wait for db",
        },
        headers=headers,
    )
    dependency_snooze_id = dependency_snooze.json()["resource"]["id"]
    deleted_dependency_snooze = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "delete_dependency_snooze",
            "snooze_id": dependency_snooze_id,
        },
        headers=headers,
    )
    exclusion = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_tag_exclusion",
            "scope": "service",
            "image_repo": "repo/app",
            "service_key": "stack/app",
            "tag": "2.0",
        },
        headers=headers,
    )
    rule_id = exclusion.json()["resource"]["id"]
    disabled_exclusion = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "set_tag_exclusion_status",
            "rule_id": rule_id,
            "status": "disabled",
        },
        headers=headers,
    )

    assert policy.status_code == 200
    assert policy.json()["resource"]["auto_update"] is False
    assert policy.json()["resource"]["snooze_default_seconds"] == 600
    assert policy.json()["resource"]["auto_update_time"] == "09:30"
    assert policy.json()["resource"]["auto_update_days"] == ["mon", "wed"]
    assert deleted_policy.status_code == 200
    assert deleted_policy.json()["resource"] is None
    assert snooze.status_code == 200
    assert snooze.json()["resource"]["reason"] == "maintenance"
    assert snooze.json()["resource"]["kind"] == "time"
    assert deleted_snooze.status_code == 200
    assert deleted_snooze.json()["resource"] is None
    assert dependency_snooze.status_code == 200
    assert dependency_snooze.json()["resource"]["kind"] == "dependency"
    assert dependency_snooze.json()["resource"]["wait_for_service_key"] == "stack/db"
    assert dependency_snooze.json()["resource"]["active"] is True
    assert deleted_dependency_snooze.status_code == 200
    assert deleted_dependency_snooze.json()["resource"] is None
    assert exclusion.status_code == 200
    assert exclusion.json()["resource"]["regex_fragment"] == "2\\.0"
    assert disabled_exclusion.status_code == 200
    assert disabled_exclusion.json()["resource"]["status"] == "disabled"

    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        service_policies = conn.execute("SELECT * FROM service_policy").fetchall()
        snoozes = conn.execute("SELECT * FROM snoozes").fetchall()
        dependency_snoozes = conn.execute(
            "SELECT * FROM dependency_snoozes"
        ).fetchall()
        tag_exclusion = conn.execute(
            "SELECT * FROM tag_exclusion_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        runs = conn.execute(
            "SELECT * FROM update_runs ORDER BY id"
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM update_events ORDER BY id"
        ).fetchall()

    operation_kinds = [
        "upsert_service_policy",
        "delete_service_policy",
        "create_snooze",
        "delete_snooze",
        "create_dependency_snooze",
        "delete_dependency_snooze",
        "upsert_tag_exclusion",
        "set_tag_exclusion_status",
    ]
    run_metadata = [json.loads(row["metadata_json"]) for row in runs]
    event_metadata = [json.loads(row["metadata_json"]) for row in events]
    assert service_policies == []
    assert snoozes == []
    assert dependency_snoozes == []
    assert tag_exclusion["status"] == "disabled"
    assert [row["mode"] for row in runs] == ["web-state"] * 8
    assert [item["operation"] for item in run_metadata] == operation_kinds
    assert [item["actor_type"] for item in run_metadata] == ["dev"] * 8
    assert [item["operation"] for item in event_metadata] == operation_kinds
    assert event_metadata[0]["before"] is None
    assert event_metadata[1]["before"]["service_key"] == "stack/app"
    assert event_metadata[-1]["after"]["status"] == "disabled"


def test_state_operation_audit_records_bearer_and_session_actors(
    tmp_path: Path,
) -> None:
    env = {
        "WUD_WEB_TOKEN": "secret",
        "WUD_WEB_MUTATIONS_ENABLED": "true",
    }
    setup_client = _client(tmp_path, env)
    _setup_admin(setup_client)
    bearer_client = _client(tmp_path, env)
    session_client = _client(tmp_path, env)

    login = session_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": DEFAULT_CLAIM_PHRASE},
        headers=_csrf_headers(session_client),
    )
    bearer_response = bearer_client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/bearer",
            "update_mode": "stop",
        },
        headers={
            **_csrf_headers(bearer_client),
            "Authorization": "Bearer secret",
        },
    )
    session_response = session_client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/session",
            "update_mode": "pause",
        },
        headers=_csrf_headers(session_client),
    )

    assert login.status_code == 200
    assert bearer_response.status_code == 200
    assert session_response.status_code == 200

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        runs = conn.execute(
            """
            SELECT metadata_json
            FROM update_runs
            WHERE mode = 'web-state'
            ORDER BY id
            """
        ).fetchall()

    assert [json.loads(row["metadata_json"])["actor_type"] for row in runs] == [
        "bearer",
        "session",
    ]


def test_service_policy_upsert_preserves_omitted_existing_fields(
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

    created = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "stop",
            "auto_update": False,
            "snooze_default_seconds": 600,
            "auto_update_time": "09:30",
            "auto_update_days": ["mon", "wed"],
        },
        headers=headers,
    )
    mode_only_update = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "live",
        },
        headers=headers,
    )
    auto_only_update = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "auto_update": True,
        },
        headers=headers,
    )
    explicit_clear = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "snooze_default_seconds": None,
        },
        headers=headers,
    )

    assert created.status_code == 200
    assert mode_only_update.status_code == 200
    mode_resource = mode_only_update.json()["resource"]
    assert mode_resource["update_mode"] == "live"
    assert mode_resource["auto_update"] is False
    assert mode_resource["snooze_default_seconds"] == 600
    assert mode_resource["auto_update_time"] == "09:30"
    assert mode_resource["auto_update_days"] == ["mon", "wed"]
    assert auto_only_update.status_code == 200
    auto_resource = auto_only_update.json()["resource"]
    assert auto_resource["update_mode"] == "live"
    assert auto_resource["auto_update"] is True
    assert auto_resource["snooze_default_seconds"] == 600
    assert auto_resource["auto_update_time"] == "09:30"
    assert auto_resource["auto_update_days"] == ["mon", "wed"]
    assert explicit_clear.status_code == 200
    clear_resource = explicit_clear.json()["resource"]
    assert clear_resource["update_mode"] == "live"
    assert clear_resource["auto_update"] is True
    assert clear_resource["snooze_default_seconds"] is None
    assert clear_resource["auto_update_time"] == "09:30"
    assert clear_resource["auto_update_days"] == ["mon", "wed"]


def test_state_operation_rolls_back_when_audit_insert_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "github-secret-token"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "GITHUB_TOKEN": secret,
        },
    )
    headers = _csrf_headers(client)

    def fail_audit(*_args: object, **_kwargs: object) -> int:
        raise sqlite3.OperationalError(
            f"audit failed for {tmp_path / 'state' / 'wud.sqlite'} with {secret}"
        )

    monkeypatch.setattr(state_module, "_insert_state_audit", fail_audit)

    response = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "stop",
        },
        headers=headers,
    )

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        rows = conn.execute("SELECT * FROM service_policy").fetchall()
        runs = conn.execute("SELECT * FROM update_runs").fetchall()

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("could not update database: audit failed for ")
    assert str(tmp_path) not in detail
    assert secret not in detail
    assert "[REDACTED_PATH]" in detail
    assert "<redacted>" in detail
    assert rows == []
    assert runs == []


def test_state_operation_uses_state_module_audit_seam(
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

    def fail_audit(*_args: object, **_kwargs: object) -> int:
        raise sqlite3.OperationalError("web audit seam used")

    monkeypatch.setattr(state_module, "_insert_state_audit", fail_audit)

    response = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "stop",
        },
        headers=headers,
    )

    assert response.status_code == 500
    assert response.json()["detail"].startswith(
        "could not update database: web audit seam used"
    )


def test_state_operations_accept_utc_z_suffix_snooze(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)
    future = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)

    snooze = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "create_snooze",
            "service_key": "stack/app",
            # The WebUI sends Date.toISOString() values, which end in "Z".
            "snoozed_until": future.strftime("%Y-%m-%dT%H:%M:%S.123Z"),
        },
        headers=headers,
    )

    assert snooze.status_code == 200
    assert snooze.json()["resource"]["snoozed_until"] == future.isoformat()


def test_state_operations_validate_inputs(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)
    past = "2000-01-01T00:00:00+00:00"
    invalid_payloads = [
        {
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "restart",
        },
        {
            "kind": "create_snooze",
            "service_key": "stack/app",
            "snoozed_until": past,
        },
        {
            "kind": "create_dependency_snooze",
            "service_key": "stack/app",
            "wait_for_service_key": "stack/app",
        },
        {
            "kind": "upsert_tag_exclusion",
            "scope": "global",
            "image_repo": "repo/app",
            "tag": "2.0",
        },
        {
            "kind": "upsert_tag_exclusion",
            "scope": "service",
            "image_repo": "repo/app",
            "tag": "2.0",
        },
        {
            "kind": "upsert_tag_exclusion",
            "scope": "image_repo",
            "image_repo": "repo/app",
            "tag": "bad:value",
        },
        {
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "auto_update_time": "9:30",
        },
    ]

    responses = [
        client.post("/api/v1/state/operations", json=payload, headers=headers)
        for payload in invalid_payloads
    ]

    assert [response.status_code for response in responses] == [422] * len(
        invalid_payloads
    )
