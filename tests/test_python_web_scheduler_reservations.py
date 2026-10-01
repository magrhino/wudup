from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.web_scheduler_test_helpers import _auto_update_tick
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup import web_jobs, web_scheduler
from wudup.db import init_db, open_db


def test_auto_update_scheduler_submits_after_reservation_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_TIMEZONE": "America/Chicago",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    policy = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "live",
            "auto_update": True,
            "auto_update_time": "09:30",
            "auto_update_days": ["sat"],
        },
        headers=_csrf_headers(client),
    )
    observed: dict[str, object] = {}

    def submit_after_commit(*_args, **kwargs):
        with open_db(tmp_path / "state" / "wud.sqlite") as conn:
            row = conn.execute(
                """
                SELECT status
                FROM auto_update_schedule_runs
                WHERE schedule_key = ?
                """,
                ("stack/app|2026-05-30|09:30|America/Chicago",),
            ).fetchone()
        assert row is not None
        assert row["status"] == "reserved"
        observed["start_event"] = kwargs["run_context"].start_event
        return web_scheduler.ApplyJobResponse(
            job_id="job-after-commit",
            status="queued",
            selected_line_numbers=[1],
        )

    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", submit_after_commit)
    now = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
    client.app.state.web_auto_update_started_at = now - timedelta(minutes=30)
    response = _auto_update_tick(client, now)

    assert policy.status_code == 200
    assert response is not None
    assert response.job_id == "job-after-commit"
    assert observed["start_event"].is_set()
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        row = conn.execute(
            """
            SELECT status, metadata_json
            FROM auto_update_schedule_runs
            WHERE schedule_key = ?
            """,
            ("stack/app|2026-05-30|09:30|America/Chicago",),
        ).fetchone()

    assert row is not None
    assert row["status"] == "queued"
    metadata = json.loads(row["metadata_json"])
    assert metadata["job_id"] == "job-after-commit"
    assert metadata["status"] == "queued"


def test_auto_update_scheduler_records_failed_policy_run(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_TIMEZONE": "America/Chicago",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    (fake_root / "stacks" / "stack" / "pull_fail").write_text("", encoding="utf-8")
    policy = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "live",
            "auto_update": True,
            "auto_update_time": "09:30",
            "auto_update_days": ["sat"],
        },
        headers=_csrf_headers(client),
    )

    now = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
    client.app.state.web_auto_update_started_at = now - timedelta(minutes=30)
    response = _auto_update_tick(client, now)
    assert response is not None
    job = _wait_apply_job(client, response.job_id)

    assert policy.status_code == 200
    assert job["status"] == "failure"
    assert job["run_id"]
    assert wud_file.read_text(encoding="utf-8") == original
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        schedule_rows = conn.execute(
            """
            SELECT *
            FROM auto_update_schedule_runs
            ORDER BY schedule_key
            """
        ).fetchall()
        detail = client.get(f"/api/v1/runs/{job['run_id']}").json()

    assert len(schedule_rows) == 1
    assert schedule_rows[0]["schedule_key"] == (
        "stack/app|2026-05-30|09:30|America/Chicago"
    )
    assert schedule_rows[0]["status"] == "failure"
    assert schedule_rows[0]["run_id"] == job["run_id"]
    assert datetime.fromisoformat(schedule_rows[0]["updated_at"]) >= datetime.fromisoformat(
        schedule_rows[0]["created_at"]
    )
    schedule_metadata = json.loads(schedule_rows[0]["metadata_json"])
    assert schedule_metadata["status"] == "failure"
    assert schedule_metadata["run_id"] == job["run_id"]
    assert "updater exited with status 1" in schedule_metadata["error"]
    assert detail["status"] == "failure"


def test_auto_update_scheduler_skips_previously_reserved_schedule(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_TIMEZONE": "America/Chicago",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    policy = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "live",
            "auto_update": True,
            "auto_update_time": "09:30",
            "auto_update_days": ["sat"],
        },
        headers=_csrf_headers(client),
    )
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO auto_update_schedule_runs (
                    schedule_key,
                    service_key,
                    scheduled_for,
                    run_id,
                    status,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES (
                    'stack/app|2026-05-30|09:30|America/Chicago',
                    'stack/app',
                    '2026-05-30T14:30:00+00:00',
                    NULL,
                    'success',
                    '2026-05-30T14:30:00+00:00',
                    '2026-05-30T14:30:00+00:00',
                    '{}'
                )
                """
            )

    now = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
    client.app.state.web_auto_update_started_at = now - timedelta(minutes=30)
    response = _auto_update_tick(client, now)

    assert policy.status_code == 200
    assert response is None
    assert wud_file.read_text(encoding="utf-8") == original
    assert " up -d " not in _fake_docker_calls(fake_root)


def test_auto_update_scheduler_rolls_back_partial_schedule_reservations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_TIMEZONE": "America/Chicago",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/app:latest", "cid-worker"),
        ],
    )
    headers = _csrf_headers(client)
    for service_key in ("stack/app", "stack/worker"):
        policy = client.post(
            "/api/v1/state/operations",
            json={
                "kind": "upsert_service_policy",
                "service_key": service_key,
                "update_mode": "live",
                "auto_update": True,
                "auto_update_time": "09:30",
                "auto_update_days": ["sat"],
            },
            headers=headers,
        )
        assert policy.status_code == 200
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO auto_update_schedule_runs (
                    schedule_key,
                    service_key,
                    scheduled_for,
                    run_id,
                    status,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES (
                    'stack/worker|2026-05-30|09:30|America/Chicago',
                    'stack/worker',
                    '2026-05-30T14:30:00+00:00',
                    NULL,
                    'success',
                    '2026-05-30T14:30:00+00:00',
                    '2026-05-30T14:30:00+00:00',
                    '{}'
                )
                """
            )
    monkeypatch.setattr(
        web_scheduler,
        "_auto_update_schedule_recorded",
        lambda _conn, _schedule_key: False,
    )

    now = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
    client.app.state.web_auto_update_started_at = now - timedelta(minutes=30)
    response = _auto_update_tick(client, now)

    assert response is None
    assert wud_file.read_text(encoding="utf-8") == original
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        rows = conn.execute(
            """
            SELECT schedule_key
            FROM auto_update_schedule_runs
            ORDER BY schedule_key
            """
        ).fetchall()
    assert [row["schedule_key"] for row in rows] == [
        "stack/worker|2026-05-30|09:30|America/Chicago"
    ]


def test_auto_update_scheduler_rolls_back_reservation_when_queue_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_TIMEZONE": "America/Chicago",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    policy = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": "stack/app",
            "update_mode": "live",
            "auto_update": True,
            "auto_update_time": "09:30",
            "auto_update_days": ["sat"],
        },
        headers=_csrf_headers(client),
    )

    def fail_submit(*_args, **_kwargs):
        raise RuntimeError("queue failed")

    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", fail_submit)
    now = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
    client.app.state.web_auto_update_started_at = now - timedelta(minutes=30)
    try:
        _auto_update_tick(client, now)
    except RuntimeError as exc:
        assert str(exc) == "queue failed"
    else:
        raise AssertionError("queue failure was not raised")

    assert policy.status_code == 200
    assert wud_file.read_text(encoding="utf-8") == original
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        rows = conn.execute("SELECT * FROM auto_update_schedule_runs").fetchall()
    assert rows == []
