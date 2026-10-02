from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from tests.web_scheduler_test_helpers import _auto_update_tick
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_env,
    _make_fake_stack,
)

from wudup import web_job_registry, web_jobs, web_pending_sources, web_scheduler
from wudup.db import open_db

SCHEDULED = datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc)


class _SchedulerHarness:
    def __init__(self, tmp_path: Path, monkeypatch, policies: dict[str, str]) -> None:
        fake_env, fake_root = _fake_docker_env(tmp_path)
        self.tmp_path = tmp_path
        self.client = _client(
            tmp_path,
            {
                "WUD_WEB_DEV_NO_AUTH": "true",
                "WUD_WEB_MUTATIONS_ENABLED": "true",
                "WUD_TIMEZONE": "America/Chicago",
                **fake_env,
            },
        )
        self.wud_file = tmp_path / "state" / "images.todo"
        self.wud_file.write_text("", encoding="utf-8")
        _make_fake_stack(
            tmp_path,
            fake_root,
            "stack",
            [
                (service, f"repo/{service}:latest", f"cid-{service}")
                for service in policies
            ],
        )
        for service, update_mode in policies.items():
            response = self.client.post(
                "/api/v1/state/operations",
                json={
                    "kind": "upsert_service_policy",
                    "service_key": f"stack/{service}",
                    "update_mode": update_mode,
                    "auto_update": True,
                    "auto_update_time": "09:30",
                    "auto_update_days": ["sat"],
                },
                headers=_csrf_headers(self.client),
            )
            assert response.status_code == 200
        # Stop the app's real-clock scheduler thread so only this test's ticks
        # record when the scheduler last checked every due slot.
        web_scheduler.shutdown_auto_update_scheduler_state(self.client.app.state)
        self.client.app.state.web_auto_update_evaluated_at = None
        self.client.app.state.web_auto_update_started_at = SCHEDULED - timedelta(
            minutes=30
        )
        self.busy = False
        self.submit_error: Exception | None = None
        self.submitted: list[tuple[str | None, list[str]]] = []
        monkeypatch.setattr(
            web_job_registry,
            "_active_apply_job_exists_in_state",
            lambda _state: self.busy,
        )
        monkeypatch.setattr(web_jobs, "_submit_apply_job_state", self._submit)

    def _submit(self, _state, _settings, plan, **kwargs):
        if self.submit_error is not None:
            error, self.submit_error = self.submit_error, None
            raise error
        run_context = kwargs["run_context"]
        self.submitted.append(
            (
                run_context.update_mode_override,
                list(run_context.metadata_extra["auto_update_service_keys"]),
            )
        )
        return web_scheduler.ApplyJobResponse(
            job_id=f"job-{len(self.submitted)}",
            status="queued",
            selected_line_numbers=list(plan.selected_line_numbers),
        )

    def pending(self, *services: str) -> None:
        self.wud_file.write_text(
            "".join(f"repo/{service}:latest\n" for service in services),
            encoding="utf-8",
        )

    def tick(self, minutes: int, *, busy: bool = False):
        self.busy = busy
        return _auto_update_tick(self.client, SCHEDULED + timedelta(minutes=minutes))

    def schedule_keys(self) -> list[str]:
        with open_db(self.tmp_path / "state" / "wud.sqlite") as conn:
            rows = conn.execute(
                """
                SELECT schedule_key
                FROM auto_update_schedule_runs
                ORDER BY schedule_key
                """
            ).fetchall()
        return [row["schedule_key"] for row in rows]


def test_auto_update_scheduler_runs_other_mode_after_own_job_spans_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(
        tmp_path,
        monkeypatch,
        {"app": "stop", "worker": "live"},
    )
    harness.pending("app", "worker")

    first = harness.tick(0)
    blocked = [harness.tick(minute, busy=True) for minute in range(1, 7)]
    late = harness.tick(7)
    after = harness.tick(8)

    assert first is not None
    assert blocked == [None] * 6
    assert late is not None
    assert after is None
    assert harness.submitted == [
        ("live", ["stack/worker"]),
        ("stop", ["stack/app"]),
    ]
    assert harness.schedule_keys() == [
        "stack/app|2026-05-30|09:30|America/Chicago",
        "stack/worker|2026-05-30|09:30|America/Chicago",
    ]


def test_auto_update_scheduler_runs_slot_after_other_job_spans_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "live"})
    harness.pending("app")

    blocked = [harness.tick(minute, busy=True) for minute in range(-1, 7)]
    late = harness.tick(7)

    assert blocked == [None] * 8
    assert late is not None
    assert harness.submitted == [("live", ["stack/app"])]
    assert harness.schedule_keys() == [
        "stack/app|2026-05-30|09:30|America/Chicago",
    ]


def test_auto_update_scheduler_drops_closed_slot_after_idle_evaluation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "live"})

    blocked = [harness.tick(minute, busy=True) for minute in range(7)]
    idle = harness.tick(7)
    harness.pending("app")
    late = harness.tick(8)

    assert blocked == [None] * 7
    assert idle is None
    assert late is None
    assert harness.submitted == []
    assert harness.schedule_keys() == []


def test_auto_update_scheduler_ignores_job_started_after_window_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "live"})

    idle = [harness.tick(minute) for minute in range(5)]
    harness.pending("app")
    blocked = harness.tick(5, busy=True)
    late = harness.tick(6)

    assert idle == [None] * 5
    assert blocked is None
    assert late is None
    assert harness.submitted == []
    assert harness.schedule_keys() == []


def test_auto_update_scheduler_drops_slot_checked_before_job_spans_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "stop"})

    idle = [harness.tick(minute) for minute in range(4)]
    harness.pending("app")
    blocked = [harness.tick(minute, busy=True) for minute in range(4, 9)]
    late = harness.tick(9)

    assert idle == [None] * 4
    assert blocked == [None] * 5
    assert late is None
    assert harness.submitted == []
    assert harness.schedule_keys() == []


def test_auto_update_scheduler_keeps_late_slot_after_submit_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "live"})
    harness.pending("app")
    harness.submit_error = HTTPException(
        status_code=409,
        detail="Another update job is already running.",
    )

    blocked = [harness.tick(minute, busy=True) for minute in range(-1, 7)]
    with pytest.raises(HTTPException):
        harness.tick(7)
    late = harness.tick(8)

    assert blocked == [None] * 8
    assert late is not None
    assert harness.submitted == [("live", ["stack/app"])]
    assert harness.schedule_keys() == [
        "stack/app|2026-05-30|09:30|America/Chicago",
    ]


def test_auto_update_scheduler_keeps_late_slot_after_failed_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _SchedulerHarness(tmp_path, monkeypatch, {"app": "live"})
    harness.pending("app")
    resolve_pending_source = web_pending_sources.resolve_pending_source
    failures = [RuntimeError("WUD API unreachable")]

    def flaky_resolve_pending_source(*args, **kwargs):
        if failures:
            raise failures.pop()
        return resolve_pending_source(*args, **kwargs)

    monkeypatch.setattr(
        web_pending_sources,
        "resolve_pending_source",
        flaky_resolve_pending_source,
    )

    blocked = [harness.tick(minute, busy=True) for minute in range(-1, 7)]
    with pytest.raises(RuntimeError, match="WUD API unreachable"):
        harness.tick(7)
    late = harness.tick(8)

    assert blocked == [None] * 8
    assert late is not None
    assert harness.submitted == [("live", ["stack/app"])]
