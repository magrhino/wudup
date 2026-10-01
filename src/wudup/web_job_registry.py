"""Shared WebUI job registry and one-mutation-at-a-time gate for WUDup."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Lock
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request

from .db import utc_timestamp
from .updater_models import UpdaterProgressEvent
from .web_models import (
    APPLY_JOB_PROGRESS_STATUSES,
    TERMINAL_APPLY_JOB_STATUSES,
    ApplyJobProgressEvent,
    ApplyJobProgressStatus,
    ApplyJobResponse,
    WebApplyJob,
    WebApplyJobProgressEvent,
    WebSettings,
)

if TYPE_CHECKING:
    from .plans import DryRunPlan

WEB_APPLY_EXECUTOR_MAX_WORKERS = 1
# Finished apply, retag, and tracking-repair jobs stay pollable in memory until
# this many jobs are registered; older finished runs remain in run history.
WEB_APPLY_JOB_LIMIT = 20


def initialize_apply_job_state(state: Any) -> None:
    state.web_apply_executor = ThreadPoolExecutor(
        max_workers=WEB_APPLY_EXECUTOR_MAX_WORKERS
    )
    state.web_apply_lock = Lock()
    state.web_apply_condition = Condition(state.web_apply_lock)
    state.web_apply_jobs = {}
    state.web_self_update_running = False
    state.web_self_update_plans = {}


def shutdown_apply_job_state(state: Any) -> None:
    executor: ThreadPoolExecutor = state.web_apply_executor
    executor.shutdown(wait=False, cancel_futures=True)


def _register_apply_job_unlocked(
    jobs: dict[str, WebApplyJob], job: WebApplyJob
) -> None:
    """Add a job and evict the oldest finished jobs beyond WEB_APPLY_JOB_LIMIT.

    Callers must hold web_apply_condition. Queued and running jobs are never
    evicted.
    """
    terminal_ids = [
        job_id
        for job_id, existing in jobs.items()
        if existing.status in TERMINAL_APPLY_JOB_STATUSES
    ]
    for job_id in terminal_ids[: max(0, len(jobs) - WEB_APPLY_JOB_LIMIT + 1)]:
        jobs.pop(job_id, None)
    jobs[job.id] = job


def plan_can_apply(plan: DryRunPlan, settings: WebSettings) -> bool:
    return (
        settings.mutations_enabled
        and plan.status == "ready"
        and all(status == "fresh" for status in plan.selected_metadata_statuses())
        and not plan.skipped
        and not any(issue.severity == "error" for issue in plan.issues)
    )


def _active_apply_job_exists_in_state(state: Any) -> bool:
    return _active_mutation_error_in_state(state) != ""


def _active_mutation_error(
    request: Request,
    *,
    include_security_scan_jobs: bool = True,
) -> str:
    return _active_mutation_error_in_state(
        request.app.state,
        include_security_scan_jobs=include_security_scan_jobs,
    )


def _active_mutation_error_in_state(
    state: Any,
    *,
    include_security_scan_jobs: bool = True,
) -> str:
    apply_lock: Lock = state.web_apply_lock
    with apply_lock:
        return _active_mutation_error_unlocked(
            state,
            include_security_scan_jobs=include_security_scan_jobs,
        )


def _active_mutation_error_unlocked(
    state: Any,
    *,
    include_security_scan_jobs: bool = True,
) -> str:
    jobs: dict[str, WebApplyJob] = state.web_apply_jobs
    if any(job.status in {"queued", "running"} for job in jobs.values()):
        return "an apply job is already running"
    if bool(getattr(state, "web_self_update_running", False)):
        return "self-update is already running"
    if not include_security_scan_jobs:
        return ""
    security_scan_error = _active_security_scan_error_in_state(state)
    if security_scan_error:
        return security_scan_error
    return ""


def _active_security_scan_error_in_state(state: Any) -> str:
    security_scan_jobs = getattr(state, "web_security_scan_jobs", {})
    security_scan_lock: Lock | None = getattr(state, "web_security_scan_lock", None)
    if security_scan_lock is None:
        active_jobs = tuple(security_scan_jobs.values())
    else:
        with security_scan_lock:
            active_jobs = tuple(security_scan_jobs.values())
    if any(getattr(job, "status", "") in {"queued", "running"} for job in active_jobs):
        return "security scan refresh is already running"
    return ""


def _reserve_mutation_state(
    state: Any,
    reserve: Callable[[], None],
    *,
    include_security_scan_jobs: bool = True,
) -> str:
    """Acquire web_apply_condition before nested job-family locks."""
    apply_condition: Condition = state.web_apply_condition
    with apply_condition:
        active_error = _active_mutation_error_unlocked(
            state,
            include_security_scan_jobs=include_security_scan_jobs,
        )
        if active_error:
            return active_error
        # Lock order: reserve() runs while web_apply_condition is held and may
        # take job-family locks. Future callbacks must preserve this order to
        # avoid opposite-order deadlocks with active-job checks.
        reserve()
    return ""


def _reserve_self_update(state: Any) -> str:
    def reserve() -> None:
        state.web_self_update_running = True

    return _reserve_mutation_state(state, reserve)


def _release_self_update(state: Any) -> None:
    apply_lock: Lock = state.web_apply_lock
    with apply_lock:
        state.web_self_update_running = False


def _require_apply_job(job_id: str, request: Request) -> WebApplyJob:
    apply_lock: Lock = request.app.state.web_apply_lock
    jobs: dict[str, WebApplyJob] = request.app.state.web_apply_jobs
    with apply_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="apply job not found")
        return job


def _apply_job_response_for_request(job_id: str, request: Request) -> ApplyJobResponse:
    apply_lock: Lock = request.app.state.web_apply_lock
    jobs: dict[str, WebApplyJob] = request.app.state.web_apply_jobs
    with apply_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="apply job not found")
        return _apply_job_response(job)


def _update_apply_job(
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    **changes: object,
) -> None:
    with apply_condition:
        job = jobs.get(job_id)
        if job is None:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        job.version += 1
        apply_condition.notify_all()


def _append_apply_job_progress(
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    event: UpdaterProgressEvent,
) -> None:
    with apply_condition:
        job = jobs.get(job_id)
        if job is None:
            return
        status = (
            event.status
            if event.status in APPLY_JOB_PROGRESS_STATUSES
            else "running"
        )
        job.progress = (
            *job.progress,
            WebApplyJobProgressEvent(
                phase=event.phase,
                status=cast(ApplyJobProgressStatus, status),
                message=event.message,
                created_at=utc_timestamp(),
                stack=event.stack,
                services=event.services,
                line_numbers=event.line_numbers,
            ),
        )
        apply_condition.notify_all()


def _apply_job_response(job: WebApplyJob) -> ApplyJobResponse:
    return ApplyJobResponse(
        job_id=job.id,
        status=job.status,
        run_id=job.run_id,
        log_file=job.log_file,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        selected_line_numbers=list(job.selected_line_numbers),
        progress=[
            ApplyJobProgressEvent(
                job_id=job.id,
                phase=event.phase,
                status=event.status,
                message=event.message,
                created_at=event.created_at,
                stack=event.stack,
                services=list(event.services),
                line_numbers=list(event.line_numbers),
            )
            for event in job.progress
        ],
    )
