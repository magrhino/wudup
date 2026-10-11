from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from tests.web_scheduler_test_helpers import _auto_update_tick
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_env,
    _make_fake_stack,
)

from wudup import web_jobs, web_scheduler
from wudup.db import open_db

SCHEDULED = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)
APP_KEY = "stack/app|2026-05-30|09:30|America/Chicago"


@pytest.fixture
def scheduler(tmp_path: Path, monkeypatch):
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
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/worker:latest", "cid-worker"),
        ],
    )
    client.app.state.web_auto_update_started_at = SCHEDULED - timedelta(minutes=30)
    busy = {"value": False}
    submitted: list[tuple[str, tuple[str, ...]]] = []

    def fake_submit(_state, _settings, plan, **kwargs):
        run_context = kwargs["run_context"]
        submitted.append(
            (
                run_context.update_mode_override,
                tuple(run_context.auto_update_schedule_keys),
            )
        )
        return web_scheduler.ApplyJobResponse(
            job_id=f"job-{len(submitted)}",
            status="queued",
            selected_line_numbers=list(plan.selected_line_numbers),
        )

    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", fake_submit)
    monkeypatch.setattr(
        web_scheduler.web_job_registry,
        "_active_apply_job_exists_in_state",
        lambda _state: busy["value"],
    )
    return client, tmp_path / "state" / "images.todo", busy, submitted


def _set_policy(client, service: str, mode: str, *, auto_update: bool = True) -> None:
    response = client.post(
        "/api/v1/state/operations",
        json={
            "kind": "upsert_service_policy",
            "service_key": f"stack/{service}",
            "update_mode": mode,
            "auto_update": auto_update,
            "auto_update_time": "09:30",
            "auto_update_days": ["sat"],
        },
        headers=_csrf_headers(client),
    )
    assert response.status_code == 200


def _missed_runs(tmp_path: Path) -> list[dict]:
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        rows = conn.execute(
            """
            SELECT status, metadata_json
            FROM update_runs
            WHERE mode = ?
            ORDER BY id
            """,
            (web_scheduler.AUTO_UPDATE_MISSED_MODE,),
        ).fetchall()
    return [
        {"status": row["status"], **json.loads(row["metadata_json"])} for row in rows
    ]


def test_mixed_mode_slot_runs_after_busy_job_outlasts_grace_window(
    scheduler,
    tmp_path: Path,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\nrepo/worker:latest\n", encoding="utf-8")
    _set_policy(client, "app", "stop")
    _set_policy(client, "worker", "live")

    assert _auto_update_tick(client, SCHEDULED) is not None
    busy["value"] = True
    for minute in range(1, 10):
        assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=minute)) is None
    busy["value"] = False
    response = _auto_update_tick(client, SCHEDULED + timedelta(minutes=10))

    assert response is not None
    assert submitted == [
        ("live", ("stack/worker|2026-05-30|09:30|America/Chicago",)),
        ("stop", (APP_KEY,)),
    ]
    assert _missed_runs(tmp_path) == []


def test_held_slot_is_recorded_once_as_missed_after_late_limit(
    scheduler,
    tmp_path: Path,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")
    late_limit = SCHEDULED + timedelta(
        seconds=web_scheduler.AUTO_UPDATE_MAX_LATE_SECONDS
    )

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    assert _auto_update_tick(client, late_limit - timedelta(minutes=1)) is None
    assert _missed_runs(tmp_path) == []
    assert _auto_update_tick(client, late_limit) is None
    busy["value"] = False
    assert _auto_update_tick(client, late_limit + timedelta(minutes=1)) is None

    assert submitted == []
    missed = _missed_runs(tmp_path)
    assert len(missed) == 1
    assert missed[0]["status"] == "missed"
    assert missed[0]["source"] == "webui-auto"
    assert missed[0]["service_keys"] == ["stack/app"]
    assert missed[0]["slots"] == [
        {
            "service_key": "stack/app",
            "schedule_key": APP_KEY,
            "scheduled_for": "2026-05-30T14:30:00+00:00",
            "update_mode": "live",
            "reason": web_scheduler.AUTO_UPDATE_HOLD_BUSY,
        }
    ]
    runs = client.get("/api/v1/runs").json()
    assert any(
        run["mode"] == web_scheduler.AUTO_UPDATE_MISSED_MODE
        and run["status"] == "missed"
        for run in runs
    )


def test_held_slot_with_nothing_to_apply_is_released_without_record(
    scheduler,
    tmp_path: Path,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("", encoding="utf-8")
    _set_policy(client, "app", "live")

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    busy["value"] = False
    assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=10)) is None
    # An update that appears after the check does not run late.
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=11)) is None
    assert _auto_update_tick(client, SCHEDULED + timedelta(hours=2)) is None

    assert submitted == []
    assert _missed_runs(tmp_path) == []


def test_held_slot_is_released_without_record_when_policy_is_disabled(
    scheduler,
    tmp_path: Path,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    _set_policy(client, "app", "live", auto_update=False)
    assert _auto_update_tick(client, SCHEDULED + timedelta(hours=2)) is None

    assert submitted == []
    assert _missed_runs(tmp_path) == []
    assert client.app.state.web_auto_update_held == {}


def test_blocked_plan_slot_is_retried_then_recorded_with_plan_reason(
    scheduler,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, wud_file, _busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")
    plan_checks: list[object] = []

    def blocked_plan(plan, _settings):
        plan_checks.append(plan)
        return False

    monkeypatch.setattr(web_scheduler, "plan_can_apply", blocked_plan)
    late_limit = SCHEDULED + timedelta(
        seconds=web_scheduler.AUTO_UPDATE_MAX_LATE_SECONDS
    )

    assert _auto_update_tick(client, SCHEDULED) is None
    assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=20)) is None
    assert _auto_update_tick(client, late_limit) is None

    assert submitted == []
    assert len(plan_checks) == 2
    missed = _missed_runs(tmp_path)
    assert len(missed) == 1
    assert missed[0]["slots"][0]["reason"] == (
        web_scheduler.AUTO_UPDATE_HOLD_PLAN_BLOCKED
    )


def test_held_slot_survives_a_tick_where_grouping_is_not_ready(
    scheduler,
    monkeypatch,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")
    resolve_pending_groups = web_scheduler.resolve_pending_groups

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    busy["value"] = False
    monkeypatch.setattr(
        web_scheduler,
        "resolve_pending_groups",
        lambda *_args, **_kwargs: SimpleNamespace(status="blocked"),
    )
    assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=10)) is None
    monkeypatch.setattr(
        web_scheduler, "resolve_pending_groups", resolve_pending_groups
    )
    response = _auto_update_tick(client, SCHEDULED + timedelta(minutes=11))

    assert response is not None
    assert submitted == [("live", (APP_KEY,))]


def test_held_slot_survives_a_rejected_submission(
    scheduler,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")
    accepting_submit = web_jobs._submit_apply_job_state

    def rejected_submit(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="another job is running")

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    busy["value"] = False
    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", rejected_submit)
    rejected_at = SCHEDULED + timedelta(minutes=10)
    with pytest.raises(HTTPException):
        _auto_update_tick(client, rejected_at)
    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", accepting_submit)
    response = _auto_update_tick(client, SCHEDULED + timedelta(minutes=11))

    assert response is not None
    assert submitted == [("live", (APP_KEY,))]
    assert _missed_runs(tmp_path) == []


def test_blocked_plan_does_not_starve_other_update_modes(
    scheduler,
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, wud_file, _busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\nrepo/worker:latest\n", encoding="utf-8")
    _set_policy(client, "app", "stop")
    _set_policy(client, "worker", "live")
    can_apply = web_scheduler.plan_can_apply
    worker_key = "stack/worker|2026-05-30|09:30|America/Chicago"

    def block_worker(plan, settings):
        return tuple(plan.selected_line_numbers) != (2,) and can_apply(plan, settings)

    monkeypatch.setattr(web_scheduler, "plan_can_apply", block_worker)
    late_limit = SCHEDULED + timedelta(
        seconds=web_scheduler.AUTO_UPDATE_MAX_LATE_SECONDS
    )

    assert _auto_update_tick(client, SCHEDULED) is not None
    assert submitted == [("stop", (APP_KEY,))]
    assert client.app.state.web_auto_update_held == {
        worker_key: web_scheduler.AUTO_UPDATE_HOLD_PLAN_BLOCKED
    }
    assert _auto_update_tick(client, late_limit) is None

    missed = _missed_runs(tmp_path)
    assert len(missed) == 1
    assert missed[0]["slots"][0]["schedule_key"] == worker_key
    assert missed[0]["slots"][0]["reason"] == (
        web_scheduler.AUTO_UPDATE_HOLD_PLAN_BLOCKED
    )


def test_held_slot_survives_a_plan_build_error(
    scheduler,
    monkeypatch,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")
    build_plan = web_scheduler._build_auto_update_plan

    def failing_build(*_args, **_kwargs):
        raise RuntimeError("docker inspect failed")

    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED) is None
    busy["value"] = False
    monkeypatch.setattr(web_scheduler, "_build_auto_update_plan", failing_build)
    failed_at = SCHEDULED + timedelta(minutes=10)
    with pytest.raises(RuntimeError):
        _auto_update_tick(client, failed_at)
    monkeypatch.setattr(web_scheduler, "_build_auto_update_plan", build_plan)
    response = _auto_update_tick(client, SCHEDULED + timedelta(minutes=11))

    assert response is not None
    assert submitted == [("live", (APP_KEY,))]


def test_missed_slot_keeps_its_original_reason_through_a_busy_job(
    scheduler,
    tmp_path: Path,
) -> None:
    client, wud_file, busy, submitted = scheduler
    wud_file.write_text("repo/app:latest\nrepo/worker:latest\n", encoding="utf-8")
    _set_policy(client, "app", "stop")
    _set_policy(client, "worker", "live")
    late_limit = SCHEDULED + timedelta(
        seconds=web_scheduler.AUTO_UPDATE_MAX_LATE_SECONDS
    )

    assert _auto_update_tick(client, SCHEDULED) is not None
    busy["value"] = True
    assert _auto_update_tick(client, SCHEDULED + timedelta(minutes=1)) is None
    assert _auto_update_tick(client, late_limit) is None

    assert len(submitted) == 1
    missed = _missed_runs(tmp_path)
    assert len(missed) == 1
    assert missed[0]["slots"][0]["schedule_key"] == APP_KEY
    assert missed[0]["slots"][0]["reason"] == (
        web_scheduler.AUTO_UPDATE_HOLD_OTHER_MODE
    )


def test_failed_submission_is_held_with_a_plain_reason(
    scheduler,
    monkeypatch,
) -> None:
    client, wud_file, _busy, _submitted = scheduler
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _set_policy(client, "app", "live")

    def failing_submit(*_args, **_kwargs):
        raise RuntimeError("executor is shut down")

    monkeypatch.setattr(web_jobs, "_submit_apply_job_state", failing_submit)
    with pytest.raises(RuntimeError):
        _auto_update_tick(client, SCHEDULED)

    assert client.app.state.web_auto_update_held == {
        APP_KEY: web_scheduler.AUTO_UPDATE_HOLD_SUBMIT_FAILED
    }
