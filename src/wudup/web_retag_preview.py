"""Retag preview executor, bounded job storage, progress, and result lifecycle.

This module exclusively owns the existing app-state preview executor, lock, and
job dictionary. Routes supply the current plan builder; planning and apply never
move here. Every worker update stays correlated to its original job ID.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .db import utc_timestamp
from .web_auth import _safe_exception_detail
from .web_models import (
    ApplyJobProgressEvent,
    RetagPlanRequest,
    RetagPlanResponse,
    RetagPreviewJobResponse,
    WebSettings,
)
from .web_retag_plans import RetagPlanBuild

RetagPlanBuilder = Callable[[WebSettings, RetagPlanRequest], RetagPlanBuild]

RETAG_PREVIEW_EXECUTOR_MAX_WORKERS = 1
RETAG_PREVIEW_JOB_LIMIT = 20
RETAG_PREVIEW_ACTIVE_STATUSES = frozenset({"queued", "running"})


@dataclass
class _RetagPreviewJob:
    id: str
    status: str
    plan: RetagPlanResponse | None = None
    warnings: tuple[str, ...] = ()
    error: str = ""
    progress: tuple[ApplyJobProgressEvent, ...] = ()


def initialize_retag_preview_state(state: Any) -> None:
    state.web_retag_preview_executor = ThreadPoolExecutor(
        max_workers=RETAG_PREVIEW_EXECUTOR_MAX_WORKERS
    )
    state.web_retag_preview_lock = Lock()
    state.web_retag_preview_jobs = {}


def shutdown_retag_preview_state(state: Any) -> None:
    executor: ThreadPoolExecutor = state.web_retag_preview_executor
    executor.shutdown(wait=False, cancel_futures=True)


def start_retag_plan_preview(
    state: Any,
    settings: WebSettings,
    payload: RetagPlanRequest,
    *,
    build_plan: RetagPlanBuilder,
) -> RetagPreviewJobResponse:
    job = _RetagPreviewJob(id=secrets.token_urlsafe(18), status="queued")
    _store_retag_preview_job(state, job)
    executor: ThreadPoolExecutor = state.web_retag_preview_executor
    try:
        executor.submit(
            _run_retag_plan_preview_job,
            state,
            settings,
            payload,
            job.id,
            build_plan,
        )
    except Exception:
        _delete_retag_preview_job(state, job.id)
        raise
    return _retag_preview_job_response(job)


def retag_preview_job_response(state: Any, job_id: str) -> RetagPreviewJobResponse:
    return _retag_preview_job_response(_require_retag_preview_job(state, job_id))


def _run_retag_plan_preview_job(
    state: Any,
    settings: WebSettings,
    payload: RetagPlanRequest,
    job_id: str,
    build_plan: RetagPlanBuilder,
) -> None:
    _update_retag_preview_job(state, job_id, status="running")
    _append_retag_preview_progress(
        state,
        job_id,
        phase="preview",
        status="running",
        message="Building the retag preview from the selected candidates.",
    )
    try:
        build = build_plan(settings, payload)
        _append_retag_preview_progress(
            state,
            job_id,
            phase="preview",
            status="success",
            message="Retag preview is ready.",
        )
        _update_retag_preview_job(
            state,
            job_id,
            status="success",
            plan=build.response,
            warnings=tuple(build.response.warnings),
        )
    except Exception as exc:  # noqa: BLE001 - the preview job records all failures.
        safe_error = _safe_exception_detail(settings, "retag preview failed", exc)
        _append_retag_preview_progress(
            state,
            job_id,
            phase="preview",
            status="failure",
            message=safe_error,
        )
        _update_retag_preview_job(
            state,
            job_id,
            status="failure",
            error=safe_error,
        )


def _store_retag_preview_job(state: Any, job: _RetagPreviewJob) -> None:
    lock: Lock = state.web_retag_preview_lock
    jobs: dict[str, _RetagPreviewJob] = state.web_retag_preview_jobs
    with lock:
        if any(
            existing.status in RETAG_PREVIEW_ACTIVE_STATUSES
            for existing in jobs.values()
        ):
            raise HTTPException(status_code=409, detail="retag preview is already running")
        terminal_ids = [
            job_id
            for job_id, existing in jobs.items()
            if existing.status in {"success", "failure"}
        ]
        for job_id in terminal_ids[: max(0, len(jobs) - RETAG_PREVIEW_JOB_LIMIT + 1)]:
            jobs.pop(job_id, None)
        jobs[job.id] = job


def _delete_retag_preview_job(state: Any, job_id: str) -> None:
    lock: Lock = state.web_retag_preview_lock
    jobs: dict[str, _RetagPreviewJob] = state.web_retag_preview_jobs
    with lock:
        jobs.pop(job_id, None)


def _require_retag_preview_job(state: Any, job_id: str) -> _RetagPreviewJob:
    lock: Lock = state.web_retag_preview_lock
    jobs: dict[str, _RetagPreviewJob] = state.web_retag_preview_jobs
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="retag preview job not found")
        return job


def _update_retag_preview_job(
    state: Any,
    job_id: str,
    *,
    status: str | None = None,
    plan: RetagPlanResponse | None = None,
    warnings: tuple[str, ...] | None = None,
    error: str | None = None,
) -> None:
    lock: Lock = state.web_retag_preview_lock
    jobs: dict[str, _RetagPreviewJob] = state.web_retag_preview_jobs
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return
        if status is not None:
            job.status = status
        if plan is not None:
            job.plan = plan
        if warnings is not None:
            job.warnings = warnings
        if error is not None:
            job.error = error


def _append_retag_preview_progress(
    state: Any,
    job_id: str,
    *,
    phase: str,
    status: str,
    message: str,
) -> None:
    lock: Lock = state.web_retag_preview_lock
    jobs: dict[str, _RetagPreviewJob] = state.web_retag_preview_jobs
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job.progress = (
            *job.progress,
            ApplyJobProgressEvent(
                job_id=job_id,
                phase=phase,
                status=status,
                message=message,
                created_at=utc_timestamp(),
            ),
        )


def _retag_preview_job_response(
    job: _RetagPreviewJob,
) -> RetagPreviewJobResponse:
    return RetagPreviewJobResponse(
        preview_job_id=job.id,
        status=job.status,
        plan=job.plan,
        warnings=list(job.warnings),
        error=job.error,
        progress=list(job.progress),
    )
