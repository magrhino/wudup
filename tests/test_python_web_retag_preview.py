from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from tests.web_retag_test_helpers import _audit_settings

from wudup import web_retag_preview as preview
from wudup import web_retags as routes
from wudup.web_models import RetagPlanRequest, RetagPlanResponse
from wudup.web_retag_plans import RetagPlanBuild


def _preview_request(tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        web_settings=_audit_settings(tmp_path),
        web_retag_preview_lock=Lock(),
        web_retag_preview_jobs={},
        web_retag_preview_executor=Mock(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _payload() -> RetagPlanRequest:
    return RetagPlanRequest.model_validate(
        {"choices": [{"service_key": "stack/app", "choice": "keep-current"}]}
    )


def test_preview_executor_initialization_and_shutdown(monkeypatch) -> None:
    constructor = Mock()
    monkeypatch.setattr(preview, "ThreadPoolExecutor", constructor)
    state = SimpleNamespace()

    preview.initialize_retag_preview_state(state)

    constructor.assert_called_once_with(max_workers=1)
    assert state.web_retag_preview_executor is constructor.return_value
    assert state.web_retag_preview_jobs == {}
    assert state.web_retag_preview_lock.acquire(blocking=False)
    state.web_retag_preview_lock.release()
    preview.shutdown_retag_preview_state(state)
    constructor.return_value.shutdown.assert_called_once_with(
        wait=False, cancel_futures=True,
    )


def test_preview_retention_removes_oldest_terminal_jobs(tmp_path: Path) -> None:
    state = _preview_request(tmp_path).app.state
    for index in range(25):
        preview._store_retag_preview_job(
            state,
            preview._RetagPreviewJob(
                id=str(index), status="success" if index % 2 else "failure",
            ),
        )

    assert list(state.web_retag_preview_jobs) == [str(index) for index in range(5, 25)]
    preview._store_retag_preview_job(
        state, preview._RetagPreviewJob(id="new", status="queued"),
    )
    assert list(state.web_retag_preview_jobs) == [
        *(str(index) for index in range(6, 25)), "new",
    ]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_preview_rejects_another_active_job(tmp_path: Path, status: str) -> None:
    state = _preview_request(tmp_path).app.state
    first = preview._RetagPreviewJob(id="first", status=status)
    preview._store_retag_preview_job(state, first)

    with pytest.raises(HTTPException) as caught:
        preview._store_retag_preview_job(
            state, preview._RetagPreviewJob(id="second", status="queued"),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "retag preview is already running"
    assert state.web_retag_preview_jobs == {"first": first}


def test_preview_submission_failure_cleans_up_job(tmp_path: Path) -> None:
    request = _preview_request(tmp_path)
    state = request.app.state
    error = RuntimeError("executor stopped")
    state.web_retag_preview_executor.submit.side_effect = error

    with pytest.raises(RuntimeError) as caught:
        routes.api_start_retag_plan_preview(_payload(), request)

    assert caught.value is error
    assert state.web_retag_preview_jobs == {}


def test_preview_success_preserves_progress_timestamps_and_response(
    tmp_path: Path, monkeypatch,
) -> None:
    request = _preview_request(tmp_path)
    state = request.app.state
    payload = _payload()
    plan = RetagPlanResponse(
        plan_id="selected-plan", status="empty", can_apply=False,
        warnings=["selected warning"],
    )
    build = Mock(return_value=RetagPlanBuild(response=plan, updates=()))
    monkeypatch.setattr(routes, "_build_current_retag_plan", build)
    monkeypatch.setattr(
        preview, "utc_timestamp", Mock(side_effect=["started-at", "finished-at"]),
    )

    queued = routes.api_start_retag_plan_preview(payload, request)
    assert queued.status == "queued"
    assert queued.plan is None
    assert queued.progress == []
    worker, *args = state.web_retag_preview_executor.submit.call_args.args
    worker(*args)
    result = routes.api_retag_plan_preview_job(queued.preview_job_id, request)

    build.assert_called_once_with(state.web_settings, payload)
    assert result.status == "success"
    assert result.plan == plan
    assert result.warnings == ["selected warning"]
    assert result.error == ""
    assert [event.job_id for event in result.progress] == [queued.preview_job_id] * 2
    assert [event.status for event in result.progress] == ["running", "success"]
    assert [event.created_at for event in result.progress] == ["started-at", "finished-at"]
    assert [event.message for event in result.progress] == [
        "Building the retag preview from the selected candidates.",
        "Retag preview is ready.",
    ]


def test_preview_failure_redacts_error_and_progress(tmp_path: Path, monkeypatch) -> None:
    request = _preview_request(tmp_path)
    state = request.app.state
    state.web_settings = replace(state.web_settings, auth_token="example-secret")
    build = Mock(side_effect=RuntimeError(f"{tmp_path}/private.yml example-secret"))
    monkeypatch.setattr(routes, "_build_current_retag_plan", build)

    queued = routes.api_start_retag_plan_preview(_payload(), request)
    worker, *args = state.web_retag_preview_executor.submit.call_args.args
    worker(*args)
    result = routes.api_retag_plan_preview_job(queued.preview_job_id, request)

    assert result.status == "failure"
    assert result.plan is None
    assert result.error.startswith("retag preview failed: ")
    assert "example-secret" not in result.error
    assert str(tmp_path) not in result.error
    assert [event.status for event in result.progress] == ["running", "failure"]
    assert result.progress[-1].message == result.error


def test_deleted_preview_completion_cannot_change_new_job(tmp_path: Path, monkeypatch) -> None:
    request = _preview_request(tmp_path)
    state = request.app.state
    monkeypatch.setattr(
        routes, "_build_current_retag_plan",
        Mock(return_value=RetagPlanBuild(
            response=RetagPlanResponse(plan_id="old-plan", status="empty", can_apply=False),
            updates=(),
        )),
    )
    first = routes.api_start_retag_plan_preview(_payload(), request)
    worker, *args = state.web_retag_preview_executor.submit.call_args.args
    preview._delete_retag_preview_job(state, first.preview_job_id)
    second = routes.api_start_retag_plan_preview(_payload(), request)

    worker(*args)

    with pytest.raises(HTTPException) as caught:
        routes.api_retag_plan_preview_job(first.preview_job_id, request)
    assert caught.value.status_code == 404
    assert caught.value.detail == "retag preview job not found"
    current = routes.api_retag_plan_preview_job(second.preview_job_id, request)
    assert current.status == "queued"
    assert current.plan is None
    assert current.progress == []
    assert list(state.web_retag_preview_jobs) == [second.preview_job_id]


def test_preview_read_only_gate_does_not_store_or_submit(tmp_path: Path) -> None:
    request = _preview_request(tmp_path)
    state = request.app.state
    state.web_settings = replace(state.web_settings, mutations_enabled=False)

    with pytest.raises(HTTPException) as caught:
        routes.api_start_retag_plan_preview(_payload(), request)

    assert caught.value.status_code == 403
    assert caught.value.detail == "mutations are disabled"
    assert state.web_retag_preview_jobs == {}
    state.web_retag_preview_executor.submit.assert_not_called()
