"""WebUI retag review route handlers."""

from __future__ import annotations

import re
import secrets
import shutil
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Condition, Lock
from typing import Any, Protocol

from fastapi import HTTPException, Request

from . import web_database, web_jobs
from .command import CommandError, CommandRunner
from .compose import (
    COMPOSE_RUNTIME_FORMAT,
    ComposeCli,
    ComposeDiscoveryError,
    ComposeStack,
    ServiceImage,
    compose_runtime_service_key,
    compose_runtime_service_keys,
)
from .compose_rewrite import (
    WUD_TAG_INCLUDE_LABEL,
    _backup_compose,
    apply_compose_digest_pins,
    apply_compose_retag_updates,
    compose_unescape_dollars,
    render_compose_digest_pins,
    render_compose_retag_updates,
)
from .config import ConfigError, UpdaterConfig
from .db import (
    DatabaseError,
    init_db,
    insert_update_event,
    insert_update_run,
    open_db,
    upsert_known_image,
    utc_timestamp,
)
from .digest_provenance import DigestTagProvenance
from .digest_verifier import DigestVerifier
from .docker_cli import DockerCli
from .images import (
    image_has_tag,
    image_key,
    image_repo_ref,
    image_tag,
    image_with_digest,
    image_with_tag,
    repo_key,
    tag_value_valid,
)
from .release_notes import (
    GitHubClient,
    ReleaseNoteInfo,
    cached_release_notes,
    github_latest_candidate_from_info,
    refresh_release_notes,
)
from .updater_digest_pin import digest_pin_update_from_values
from .updater_lifecycle_health import (
    CONTAINER_SUMMARY_FORMAT,
    HEALTH_LOG_FORMAT,
    _cid_is_ok,
)
from .updater_models import (
    AppliedDigestPinUpdate,
    ResolvedTagMarkerConflictError,
    UpdaterProgressEvent,
)
from .web_auth import _redact_sensitive_text, _safe_exception_detail, _settings
from .web_database import ReadOnlyDatabaseMissing
from .web_metadata import json_object as _json_object
from .web_models import (
    ApplyJobProgressEvent,
    ApplyJobResponse,
    RetagApplyRequest,
    RetagChoiceRequest,
    RetagPlanIssue,
    RetagPlanLabelRewrite,
    RetagPlanRequest,
    RetagPlanResponse,
    RetagPreviewJobResponse,
    RetagRuntimeState,
    RetagTargetItem,
    RetagTargetsResponse,
    WebApplyJob,
    WebSettings,
)
from .web_release_notes import release_note_source_resolver
from .web_retag_choices import validated_retag_choice_map
from .web_retag_identity import retag_target_id as _retag_target_id_from_values
from .web_retag_plans import (
    RetagPlanBuild as _RetagPlanBuild,
)
from .web_retag_plans import (
    RetagPlanUpdate as _RetagPlanUpdate,
)
from .web_retag_plans import (
    ordered_retag_stacks as _ordered_retag_stacks,
)
from .web_retag_plans import (
    retag_compose_hashes as _compose_hashes,
)
from .web_retag_plans import (
    retag_plan_digest_update as _retag_plan_digest_update,
)
from .web_retag_plans import (
    retag_plan_id as _retag_plan_id,
)
from .web_retag_plans import (
    retag_plan_stacks as _retag_plan_stacks,
)
from .web_retag_plans import (
    retag_plan_status as _retag_plan_status,
)
from .web_retag_plans import (
    retag_plan_tag_update as _retag_plan_tag_update,
)
from .web_retag_plans import (
    retag_update_service as _retag_update_service,
)
from .wud_file import WudTarget

KEEP_CURRENT_CHOICE = "keep-current"
SWITCH_TO_CONCRETE_CHOICE = "switch-to-concrete"
MUTATIONS_DISABLED_DETAIL = "mutations are disabled"
GITHUB_LATEST_MISSING_CACHE_WARNING = (
    "GitHub latest fallback is enabled, but no cached GitHub release "
    "metadata is available. Refresh candidates and try again."
)
_REGEX_SPECIAL_CHARS = "\\^$.*+?()[]{}|"
RETAG_PREVIEW_EXECUTOR_MAX_WORKERS = 1
RETAG_PREVIEW_JOB_LIMIT = 20
RETAG_PREVIEW_ACTIVE_STATUSES = frozenset({"queued", "running"})
_GHCR_GITHUB_REPO_RE = re.compile(
    r"^ghcr[.]io/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})$",
    re.ASCII,
)
@dataclass(frozen=True)
class _RetagTargetRecord:
    item: RetagTargetItem
    stack: ComposeStack
    service_image: ServiceImage
    known_image: str
    provenance: DigestTagProvenance | None
    service_key_ambiguous: bool


@dataclass(frozen=True)
class _RetagGitHubLatestFallback:
    provenance: DigestTagProvenance | None = None
    proposed_tag: str = ""
    warning: str = ""
    link_label: str = ""
    link_url: str = ""


@dataclass(frozen=True)
class _RetagGitHubLatestTarget:
    target_id: str
    service_key: str
    service_image: ServiceImage
    target: WudTarget


@dataclass
class _RetagPreviewJob:
    id: str
    status: str
    plan: RetagPlanResponse | None = None
    warnings: tuple[str, ...] = ()
    error: str = ""
    progress: tuple[ApplyJobProgressEvent, ...] = ()


class _RetagApplyFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        successful_updates: Sequence[_RetagPlanUpdate],
    ) -> None:
        super().__init__(message)
        self.successful_updates = tuple(successful_updates)


class EffectiveConfigLoader(Protocol):
    def __call__(self, settings: WebSettings) -> UpdaterConfig: ...


class RetagDigestPinsLoader(Protocol):
    def __call__(self, settings: WebSettings) -> bool: ...


_effective_config_loader: EffectiveConfigLoader | None = None
_retag_digest_pins_loader: RetagDigestPinsLoader | None = None


def configure(
    *,
    effective_config_loader: EffectiveConfigLoader,
    retag_digest_pins_loader: RetagDigestPinsLoader,
) -> None:
    global _effective_config_loader, _retag_digest_pins_loader
    _effective_config_loader = effective_config_loader
    _retag_digest_pins_loader = retag_digest_pins_loader


def initialize_retag_preview_state(state: Any) -> None:
    state.web_retag_preview_executor = ThreadPoolExecutor(
        max_workers=RETAG_PREVIEW_EXECUTOR_MAX_WORKERS
    )
    state.web_retag_preview_lock = Lock()
    state.web_retag_preview_jobs = {}


def shutdown_retag_preview_state(state: Any) -> None:
    executor: ThreadPoolExecutor = state.web_retag_preview_executor
    executor.shutdown(wait=False, cancel_futures=True)


def api_retag_targets(
    request: Request,
    github_latest_fallback: bool = False,
) -> RetagTargetsResponse:
    return retag_targets_response(
        _settings(request),
        github_latest_fallback=github_latest_fallback,
    )


def api_refresh_retag_github_latest(request: Request) -> RetagTargetsResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    response = _refresh_retag_github_latest_candidates(settings)
    if response is not None:
        return response
    return retag_targets_response(settings, github_latest_fallback=True)


def api_create_retag_plan(
    payload: RetagPlanRequest,
    request: Request,
) -> RetagPlanResponse:
    return build_retag_plan(_settings(request), payload).response


def api_start_retag_plan_preview(
    payload: RetagPlanRequest,
    request: Request,
) -> RetagPreviewJobResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    state = request.app.state
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
        )
    except Exception:
        _delete_retag_preview_job(state, job.id)
        raise
    return _retag_preview_job_response(job)


def api_retag_plan_preview_job(
    preview_job_id: str,
    request: Request,
) -> RetagPreviewJobResponse:
    return _retag_preview_job_response(
        _require_retag_preview_job(request.app.state, preview_job_id)
    )


def api_apply_retag_plan(
    payload: RetagApplyRequest,
    request: Request,
) -> ApplyJobResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    return _submit_retag_apply_job(request, settings, payload)


def retag_targets_response(
    settings: WebSettings,
    *,
    github_latest_fallback: bool = False,
) -> RetagTargetsResponse:
    discovery = _retag_target_records(
        settings,
        github_latest_fallback=github_latest_fallback,
    )
    if isinstance(discovery, RetagTargetsResponse):
        return discovery
    items = [record.item for record in discovery]
    return RetagTargetsResponse(
        status="ready",
        count=len(items),
        items=items,
        warnings=[],
    )


def _run_retag_plan_preview_job(
    state: Any,
    settings: WebSettings,
    payload: RetagPlanRequest,
    job_id: str,
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
        build = _build_current_retag_plan(settings, payload)
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


def _build_current_retag_plan(
    settings: WebSettings,
    payload: RetagPlanRequest,
) -> _RetagPlanBuild:
    return build_retag_plan(settings, payload)


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


def build_retag_plan(
    settings: WebSettings,
    payload: RetagPlanRequest,
    *,
    github_latest_by_target_id: Mapping[str, _RetagGitHubLatestFallback] | None = None,
    extra_warnings: Sequence[str] = (),
) -> _RetagPlanBuild:
    records_or_response = _retag_target_records(
        settings,
        github_latest_fallback=payload.github_latest_fallback,
        github_latest_by_target_id=github_latest_by_target_id,
    )
    if isinstance(records_or_response, RetagTargetsResponse):
        plan = RetagPlanResponse(
            plan_id="",
            status="unavailable",
            can_apply=False,
            warnings=[*records_or_response.warnings, *extra_warnings],
            issues=[
                RetagPlanIssue(
                    severity="error",
                    code="retag-targets-unavailable",
                    message="Retag targets are unavailable.",
                    hint="Resolve Compose discovery warnings, then refresh retag targets.",
                )
            ],
        )
        plan.plan_id = _retag_plan_id(plan, updates=(), compose_hashes={})
        return _RetagPlanBuild(response=plan, updates=())

    records_by_target_id = {
        record.item.target_id: record for record in records_or_response
    }
    service_key_by_target_id = {
        target_id: record.item.service_key
        for target_id, record in records_by_target_id.items()
    }
    choices = validated_retag_choice_map(
        payload.choices,
        service_key_by_target_id=service_key_by_target_id,
    )

    keep_current_count = sum(
        1 for choice in choices.values() if choice.choice == KEEP_CURRENT_CHOICE
    )
    selected, issues = _selected_retag_plan_updates(
        settings,
        choices,
        records_by_target_id,
        digest_pins=_retag_digest_pins(settings),
    )

    selected, preview_issues = _preview_retag_updates(settings, selected)
    issues.extend(preview_issues)
    compose_hashes = _compose_hashes(selected)
    status = _retag_plan_status(selected, choices, issues)
    stacks = _retag_plan_stacks(selected)
    plan = RetagPlanResponse(
        plan_id="",
        status=status,
        can_apply=status == "ready" and bool(selected),
        selected_count=len(selected),
        keep_current_count=keep_current_count,
        stacks=stacks,
        issues=issues,
        warnings=list(extra_warnings),
    )
    plan.plan_id = _retag_plan_id(plan, updates=selected, compose_hashes=compose_hashes)
    return _RetagPlanBuild(response=plan, updates=tuple(selected))


def _selected_retag_plan_updates(
    settings: WebSettings,
    choices: Mapping[str, RetagChoiceRequest],
    records_by_target_id: Mapping[str, _RetagTargetRecord],
    *,
    digest_pins: bool,
) -> tuple[list[_RetagPlanUpdate], list[RetagPlanIssue]]:
    selected: list[_RetagPlanUpdate] = []
    issues: list[RetagPlanIssue] = []
    for target_id in sorted(
        choices,
        key=lambda value: _retag_record_sort_key(records_by_target_id[value]),
    ):
        choice = choices[target_id]
        if choice.choice == KEEP_CURRENT_CHOICE:
            continue
        record = records_by_target_id[target_id]
        service_key = record.item.service_key
        if record.item.runtime_state != "running" and not choice.allow_start:
            issues.append(_retag_runtime_consent_issue(record.item))
            continue
        update, issue = _retag_plan_update_for_choice(
            settings,
            service_key,
            record,
            target_tag=choice.target_tag,
            allow_start=choice.allow_start,
            digest_pins=digest_pins,
        )
        if issue is not None:
            issues.append(issue)
            continue
        if update is not None:
            selected.append(update)
    return selected, issues


def _retag_record_sort_key(
    record: _RetagTargetRecord,
) -> tuple[str, str, str, str, str, str]:
    stack = record.stack
    return (
        record.item.service_key,
        str(stack.directory),
        stack.file,
        "" if stack.project_directory is None else str(stack.project_directory),
        record.item.service,
        record.item.target_id,
    )


def _retag_plan_update_for_choice(
    settings: WebSettings,
    service_key: str,
    record: _RetagTargetRecord,
    *,
    target_tag: str | None = None,
    allow_start: bool = False,
    digest_pins: bool,
) -> tuple[_RetagPlanUpdate | None, RetagPlanIssue | None]:
    item = record.item
    provenance = record.provenance
    if target_tag is not None:
        manual_tag = target_tag.strip()
        if not manual_tag:
            return None, _manual_retag_issue(
                item,
                code="retag-manual-empty-tag",
                message=f"{service_key} manual retag target cannot be empty.",
                hint="Enter a concrete Docker tag from the release or repository page.",
            )
        if provenance is None or manual_tag != item.proposed_tag:
            return _manual_retag_plan_update_for_choice(
                settings,
                service_key,
                record,
                target_tag=manual_tag,
                allow_start=allow_start,
                digest_pins=digest_pins,
            )
    if not item.retag_available or provenance is None:
        return None, RetagPlanIssue(
            severity="error",
            code="retag-target-not-eligible",
            message=(
                f"{service_key} cannot switch to concrete tracking: "
                f"{item.retag_reason}"
            ),
            service_key=service_key,
            stack=item.stack,
            service=item.service,
        )
    update = digest_pin_update_from_values(
        old_image=item.image,
        resolved_tag=provenance.resolved_tag,
        planned_digest=provenance.target_digest,
        services=(item.service,),
    )
    if update.final_image != provenance.final_image:
        return None, RetagPlanIssue(
            severity="error",
            code="retag-provenance-mismatch",
            message=(
                f"{service_key} stored provenance does not match the "
                "planned digest-pinned image."
            ),
            service_key=service_key,
            stack=item.stack,
            service=item.service,
        )
    if not digest_pins:
        update = replace(
            update,
            final_image=update.resolved_image,
            marker="",
        )
    return (
        _RetagPlanUpdate(
            target_id=item.target_id,
            service_key=service_key,
            stack=record.stack,
            update=update,
            provenance=provenance,
            runtime_state=item.runtime_state,
            allow_start=allow_start,
            known_image_service_key_ambiguous=record.service_key_ambiguous,
            digest_pin=digest_pins,
        ),
        None,
    )


def _manual_retag_plan_update_for_choice(
    settings: WebSettings,
    service_key: str,
    record: _RetagTargetRecord,
    *,
    target_tag: str,
    allow_start: bool = False,
    digest_pins: bool,
) -> tuple[_RetagPlanUpdate | None, RetagPlanIssue | None]:
    item = record.item
    if target_tag == "latest":
        return None, _manual_retag_issue(
            item,
            code="retag-manual-latest-tag",
            message=f"{service_key} manual retag target cannot be latest.",
            hint="Enter a concrete Docker tag from the release or repository page.",
        )
    if not tag_value_valid(target_tag):
        return None, _manual_retag_issue(
            item,
            code="retag-manual-invalid-tag",
            message=f"{service_key} manual retag target is not a valid Docker tag.",
            hint="Use a Docker tag value such as 1.2.3, v1.2.3, or 2026.6.0.",
        )
    target_image = image_with_tag(record.service_image.image, target_tag)
    try:
        result = DigestVerifier(
            DockerCli(runner=_command_runner(settings)),
        ).resolve_tag_digest(target_image)
    except Exception as exc:  # noqa: BLE001 - resolver failures become preview issues.
        return None, _manual_retag_issue(
            item,
            code="retag-manual-digest-error",
            message=_safe_exception_detail(
                settings,
                f"Could not resolve manual retag target for {service_key}",
                exc,
            ),
            hint="Confirm the tag exists for the image repository, then preview again.",
        )
    if not result.ok or not result.digest:
        reason = result.reason or result.status or "digest resolution failed"
        return None, _manual_retag_issue(
            item,
            code="retag-manual-digest-unavailable",
            message=(
                f"Could not resolve manual retag target {target_image}: {reason}"
            ),
            hint="Confirm the tag exists for the image repository, then preview again.",
        )
    digest = result.digest
    provenance = DigestTagProvenance(
        source_image=record.service_image.image,
        resolved_tag=target_tag,
        watch_tag=target_tag,
        target_digest=digest,
        final_image=image_with_digest(record.service_image.image, digest),
        provenance_source="manual",
        provenance_confidence="verified",
    )
    update = digest_pin_update_from_values(
        old_image=item.image,
        resolved_tag=target_tag,
        planned_digest=digest,
        services=(item.service,),
    )
    if not digest_pins:
        update = replace(
            update,
            final_image=update.resolved_image,
            marker="",
        )
    return (
        _RetagPlanUpdate(
            target_id=item.target_id,
            service_key=service_key,
            stack=record.stack,
            update=update,
            provenance=provenance,
            runtime_state=item.runtime_state,
            allow_start=allow_start,
            known_image_service_key_ambiguous=record.service_key_ambiguous,
            digest_pin=digest_pins,
        ),
        None,
    )


def _manual_retag_issue(
    item: RetagTargetItem,
    *,
    code: str,
    message: str,
    hint: str,
) -> RetagPlanIssue:
    return RetagPlanIssue(
        severity="error",
        code=code,
        message=message,
        service_key=item.service_key,
        stack=item.stack,
        service=item.service,
        hint=hint,
    )


def _retag_runtime_consent_issue(item: RetagTargetItem) -> RetagPlanIssue:
    if item.runtime_state == "not-running":
        message = (
            f"{item.service_key} is not running; applying this retag would start it."
        )
    else:
        message = (
            f"{item.service_key} runtime state is unknown; applying this retag may "
            "start it."
        )
    return RetagPlanIssue(
        severity="error",
        code="retag-start-not-approved",
        message=message,
        service_key=item.service_key,
        stack=item.stack,
        service=item.service,
        hint=(
            "Refresh retag targets, then select this service individually to approve "
            "starting it."
        ),
        details={"runtime_state": item.runtime_state},
    )


def _retag_target_records(
    settings: WebSettings,
    *,
    github_latest_fallback: bool = False,
    github_latest_by_target_id: Mapping[str, _RetagGitHubLatestFallback] | None = None,
) -> tuple[_RetagTargetRecord, ...] | RetagTargetsResponse:
    stacks_or_response = _discover_retag_stacks(settings)
    if isinstance(stacks_or_response, RetagTargetsResponse):
        return stacks_or_response
    stacks = stacks_or_response

    known_by_service = web_database.known_digest_state_by_service(settings)
    service_counts = _retag_service_counts(stacks)
    if github_latest_by_target_id is not None:
        active_github_latest_by_target_id = dict(github_latest_by_target_id)
    elif github_latest_fallback:
        active_github_latest_by_target_id = _cached_github_latest_fallback_by_target(
            settings,
            stacks,
            known_by_service,
        )
    else:
        active_github_latest_by_target_id = {}
    records: list[_RetagTargetRecord] = []
    digest_pins = _retag_digest_pins(settings)
    running_service_keys = _running_retag_compose_service_keys(settings)
    for stack in stacks:
        for service_image in stack.service_images:
            service_key = _retag_service_key(stack.name, service_image.service)
            target_id = _retag_target_id(stack, service_image)
            service_key_ambiguous = service_counts[service_key] > 1
            known = _known_state_for_retag_target(
                known_by_service.get(service_key),
                service_image.image,
                service_key_ambiguous=service_key_ambiguous,
            )
            runtime_state: RetagRuntimeState = "unknown"
            if running_service_keys is not None:
                runtime_state = (
                    "running"
                    if _retag_compose_service_key(stack, service_image.service)
                    in running_service_keys
                    else "not-running"
                )
            records.append(
                _retag_target_record(
                    stack,
                    service_image,
                    target_id,
                    known,
                    active_github_latest_by_target_id.get(target_id),
                    runtime_state=runtime_state,
                    service_key_ambiguous=service_key_ambiguous,
                    github_latest_fallback=github_latest_fallback,
                    digest_pins=digest_pins,
                )
            )

    return tuple(records)


def _running_retag_compose_service_keys(
    settings: WebSettings,
) -> set[tuple[frozenset[Path], str, str]] | None:
    docker = DockerCli(runner=_command_runner(settings))
    try:
        rows = docker.ps_format(COMPOSE_RUNTIME_FORMAT)
    except CommandError:
        return None
    return compose_runtime_service_keys(rows)


def _retag_compose_service_key(
    stack: ComposeStack,
    service: str,
    *,
    project_name: str | None = None,
) -> tuple[frozenset[Path], str, str]:
    project_directory = stack.project_directory or stack.directory
    return compose_runtime_service_key(
        project_directory,
        stack.file,
        stack.project_name if project_name is None else project_name,
        service,
    )


def _discover_retag_stacks(
    settings: WebSettings,
) -> tuple[ComposeStack, ...] | RetagTargetsResponse:
    config = _effective_config(settings)
    compose = ComposeCli(runner=_command_runner(settings))
    try:
        return tuple(
            compose.discover_stacks(
                config.docker_base,
                project_base=settings.host_docker_base,
                ignore_paths=config.compose_ignore_paths,
            )
        )
    except ComposeDiscoveryError as exc:
        return RetagTargetsResponse(
            status="unavailable",
            count=0,
            warnings=[
                _safe_exception_detail(
                    settings,
                    "could not discover retag targets",
                    exc,
                )
            ],
        )


def _refresh_retag_github_latest_candidates(
    settings: WebSettings,
    *,
    force: bool = True,
) -> RetagTargetsResponse | None:
    stacks_or_response = _discover_retag_stacks(settings)
    if isinstance(stacks_or_response, RetagTargetsResponse):
        return stacks_or_response
    known_by_service = web_database.known_digest_state_by_service(settings)
    targets = [
        row.target
        for row in _github_latest_fallback_targets(
            stacks_or_response,
            known_by_service,
        )
    ]
    if not targets:
        return None
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            refresh_release_notes(
                conn,
                targets,
                settings.command_env or {},
                client=GitHubClient(
                    token=(settings.command_env or {}).get("GITHUB_TOKEN", ""),
                ),
                source_resolver=release_note_source_resolver(settings),
                redact_error=lambda value: _redact_sensitive_text(settings, value),
                force=force,
            )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not refresh retag GitHub latest candidates",
                exc,
            ),
        ) from exc
    return None


def _cached_github_latest_fallback_by_target(
    settings: WebSettings,
    stacks: Sequence[ComposeStack],
    known_by_service: Mapping[str, web_database.KnownDigestState],
) -> dict[str, _RetagGitHubLatestFallback]:
    target_rows = _github_latest_fallback_targets(stacks, known_by_service)
    if not target_rows:
        return {}
    missing_cache = _RetagGitHubLatestFallback(
        warning=GITHUB_LATEST_MISSING_CACHE_WARNING
    )
    try:
        with closing(web_database.connect_readonly_db(settings)) as conn:
            infos = cached_release_notes(
                conn,
                [row.target for row in target_rows],
                settings.command_env or {},
                source_resolver=release_note_source_resolver(settings),
            )
    except ReadOnlyDatabaseMissing:
        return dict.fromkeys(
            (row.target_id for row in target_rows),
            missing_cache,
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        warning = _safe_exception_detail(
            settings,
            "could not read cached GitHub latest candidates",
            exc,
        )
        return {
            row.target_id: _RetagGitHubLatestFallback(warning=warning)
            for row in target_rows
        }

    result: dict[str, _RetagGitHubLatestFallback] = {}
    for row, info in zip(target_rows, infos, strict=True):
        result[row.target_id] = _fallback_from_release_info(
            settings,
            row.service_image,
            info,
        )
    return result


def _github_latest_fallback_targets(
    stacks: Sequence[ComposeStack],
    known_by_service: Mapping[str, web_database.KnownDigestState],
) -> list[_RetagGitHubLatestTarget]:
    rows: list[_RetagGitHubLatestTarget] = []
    service_counts = _retag_service_counts(stacks)
    for stack in stacks:
        for service_image in stack.service_images:
            service_key = _retag_service_key(stack.name, service_image.service)
            service_key_ambiguous = service_counts[service_key] > 1
            known = _known_state_for_retag_target(
                known_by_service.get(service_key),
                service_image.image,
                service_key_ambiguous=service_key_ambiguous,
            )
            if known is not None:
                continue
            label_value = _label_value(service_image.labels, WUD_TAG_INCLUDE_LABEL)
            tracking_tag, tracking_tag_source = _tracking_tag(
                service_image.image,
                label_value=label_value,
                provenance=None,
            )
            if tracking_tag_source == "unsupported-label" or tracking_tag != "latest":
                continue
            rows.append(
                _RetagGitHubLatestTarget(
                    target_id=_retag_target_id(stack, service_image),
                    service_key=service_key,
                    service_image=service_image,
                    target=_release_note_target_for_service(
                        stack.index,
                        service_image.image,
                    ),
                )
            )
    return rows


def _fallback_from_release_info(
    settings: WebSettings,
    service_image: ServiceImage,
    info: ReleaseNoteInfo,
) -> _RetagGitHubLatestFallback:
    candidate = github_latest_candidate_from_info(info)
    if candidate is None:
        return _RetagGitHubLatestFallback(
            warning=_github_latest_info_warning(info),
        )
    tag_candidates = _retag_candidate_tags(candidate.release_tag)
    valid_tag_candidates = tuple(tag for tag in tag_candidates if tag_value_valid(tag))
    if not valid_tag_candidates:
        return _RetagGitHubLatestFallback(
            proposed_tag=candidate.release_tag,
            warning=(
                f"GitHub latest release tag {candidate.release_tag} is not a "
                "valid Docker tag value."
            ),
            link_label=candidate.link_label,
            link_url=candidate.link_url,
        )
    verifier = DigestVerifier(
        DockerCli(runner=_command_runner(settings)),
    )
    failed: list[str] = []
    for proposed_tag in valid_tag_candidates:
        resolved_image = image_with_tag(service_image.image, proposed_tag)
        digest_result = verifier.resolve_tag_digest(resolved_image)
        if digest_result.ok and digest_result.digest:
            digest = digest_result.digest
            warning = (
                "GitHub latest fallback will update latest tracking to "
                f"{proposed_tag}."
            )
            if proposed_tag != candidate.release_tag:
                warning = (
                    f"GitHub latest release tag {candidate.release_tag} resolved "
                    f"as Docker tag {proposed_tag}. "
                    "GitHub latest fallback will update latest tracking to "
                    f"{proposed_tag}."
                )
            return _RetagGitHubLatestFallback(
                provenance=DigestTagProvenance(
                    source_image=service_image.image,
                    resolved_tag=proposed_tag,
                    watch_tag="latest",
                    target_digest=digest,
                    final_image=image_with_digest(service_image.image, digest),
                    provenance_source="github-latest",
                    provenance_confidence="recovered",
                ),
                proposed_tag=proposed_tag,
                warning=warning,
                link_label=candidate.link_label,
                link_url=candidate.link_url,
            )
        reason = digest_result.reason or digest_result.status
        failed.append(f"{proposed_tag}: {reason}")
    return _RetagGitHubLatestFallback(
        proposed_tag=candidate.release_tag,
        warning=(
            f"GitHub latest release tag {candidate.release_tag} was found, "
            "but no Docker tag candidate digest could be resolved: "
            f"{'; '.join(failed)}."
        ),
        link_label=candidate.link_label,
        link_url=candidate.link_url,
    )


def _retag_candidate_tags(release_tag: str) -> tuple[str, ...]:
    candidates = [release_tag]
    if len(release_tag) > 1 and release_tag[0] == "v" and release_tag[1].isdigit():
        candidates.append(release_tag[1:])
    elif release_tag and release_tag[0].isdigit():
        candidates.append(f"v{release_tag}")
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _github_latest_info_warning(info: ReleaseNoteInfo) -> str:
    provider = str(getattr(info, "provider", ""))
    status = str(getattr(info, "status", ""))
    error = str(getattr(info, "error", ""))
    if status == "missing":
        return GITHUB_LATEST_MISSING_CACHE_WARNING
    if provider == "lsio":
        return "LSIO latest release metadata did not include a Docker tag candidate."
    if status == "unsupported":
        return error or "No supported GitHub release source was found."
    if status == "not_found":
        return "No GitHub latest release was found for this image source."
    if status == "error":
        return error or "GitHub latest release metadata could not be refreshed."
    return "GitHub latest release metadata is not ready for this service."


def _release_note_target_for_service(line_no: int, image: str) -> WudTarget:
    return WudTarget(
        line_no=line_no,
        raw=image,
        first=image,
        key=image_key(image),
        repo=repo_key(image),
        has_tag=image_has_tag(image),
        allow_repo=False,
        digest="",
        desired_tag="",
    )


def _command_runner(settings: WebSettings) -> CommandRunner:
    if settings.command_env is not None:
        return CommandRunner(env=settings.command_env)
    return CommandRunner()


def _retag_service_key(stack_name: str, service_name: str) -> str:
    return f"{stack_name}/{service_name}"


def _retag_service_counts(stacks: Sequence[ComposeStack]) -> Counter[str]:
    return Counter(
        _retag_service_key(stack.name, service_image.service)
        for stack in stacks
        for service_image in stack.service_images
    )


def _retag_target_id(stack: ComposeStack, service_image: ServiceImage) -> str:
    return _retag_target_id_from_values(
        stack.directory,
        stack.file,
        stack.project_directory,
        stack.name,
        service_image.service,
    )


def _known_state_for_retag_target(
    known: web_database.KnownDigestState | None,
    current_image: str,
    *,
    service_key_ambiguous: bool,
) -> web_database.KnownDigestState | None:
    if known is None or not service_key_ambiguous:
        return known
    if _known_state_matches_image(known, current_image):
        return known
    return None


def _known_state_matches_image(
    known: web_database.KnownDigestState,
    current_image: str,
) -> bool:
    return current_image in {
        known.image,
        known.digest_provenance.final_image,
    }


def _retag_target_record(
    stack: ComposeStack,
    service_image: ServiceImage,
    target_id: str,
    known: web_database.KnownDigestState | None,
    github_latest: _RetagGitHubLatestFallback | None,
    *,
    runtime_state: RetagRuntimeState,
    service_key_ambiguous: bool,
    github_latest_fallback: bool,
    digest_pins: bool,
) -> _RetagTargetRecord:
    service_key = _retag_service_key(stack.name, service_image.service)
    provenance = None if known is None else known.digest_provenance
    github_latest_provenance = (
        provenance is None
        and github_latest is not None
        and github_latest.provenance is not None
    )
    if provenance is None and github_latest is not None:
        provenance = github_latest.provenance
    known_image = "" if known is None else known.image
    item = _retag_target_item(
        target_id=target_id,
        service_key=service_key,
        stack=stack.name,
        service_image=service_image,
        directory=str(stack.directory),
        compose_file=stack.file,
        project_directory=(
            "" if stack.project_directory is None else str(stack.project_directory)
        ),
        known_image=known_image,
        provenance=provenance,
        github_latest=github_latest,
        github_latest_fallback=github_latest_fallback,
        allow_source_image_match=github_latest_provenance,
        runtime_state=runtime_state,
        digest_pins=digest_pins,
    )
    return _RetagTargetRecord(
        item=item,
        stack=stack,
        service_image=service_image,
        known_image=known_image,
        provenance=provenance,
        service_key_ambiguous=service_key_ambiguous,
    )


def _preview_retag_updates(
    settings: WebSettings,
    selected: Sequence[_RetagPlanUpdate],
) -> tuple[tuple[_RetagPlanUpdate, ...], list[RetagPlanIssue]]:
    issues: list[RetagPlanIssue] = []
    updated_by_key = {_retag_update_identity(item): item for item in selected}
    for stack in _ordered_retag_stacks(selected):
        stack_updates = [item for item in selected if item.stack.index == stack.index]
        updated, stack_issues = _preview_retag_stack(settings, stack, stack_updates)
        issues.extend(stack_issues)
        for item in updated:
            updated_by_key[_retag_update_identity(item)] = item
    return (
        tuple(updated_by_key[_retag_update_identity(item)] for item in selected),
        issues,
    )


def _preview_retag_stack(
    settings: WebSettings,
    stack: ComposeStack,
    stack_updates: Sequence[_RetagPlanUpdate],
) -> tuple[list[_RetagPlanUpdate], list[RetagPlanIssue]]:
    issues: list[RetagPlanIssue] = []
    updated: list[_RetagPlanUpdate] = []
    try:
        renderer = (
            render_compose_digest_pins
            if stack_updates[0].digest_pin
            else render_compose_retag_updates
        )
        _rendered, applied = renderer(
            stack.directory / stack.file,
            tuple(item.update for item in stack_updates),
            stack_name=stack.name,
        )
    except Exception as exc:  # noqa: BLE001 - renderer failures become preview issues.
        return [], _retag_preview_failed_issues(settings, stack, stack_updates, exc)

    applied_by_service = {
        _retag_service_key(stack.name, service): applied_item
        for applied_item in applied
        for service in applied_item.services
    }
    for item in stack_updates:
        applied_item = applied_by_service.get(item.service_key)
        if applied_item is None:
            issues.append(_retag_preview_empty_issue(stack, item))
            continue
        updated.append(_retag_update_with_label_rewrites(item, applied_item))
    return updated, issues


def _retag_preview_failed_issues(
    settings: WebSettings,
    stack: ComposeStack,
    stack_updates: Sequence[_RetagPlanUpdate],
    exc: Exception,
) -> list[RetagPlanIssue]:
    return [
        RetagPlanIssue(
            severity="error",
            code="retag-compose-preview-failed",
            message=_safe_exception_detail(
                settings,
                f"Could not safely preview retag for {item.service_key}",
                exc,
            ),
            service_key=item.service_key,
            stack=stack.name,
            service=_retag_update_service(item),
            hint=(
                "Remove stale wudup.resolved-tag comments from this Compose "
                "service, then preview again."
                if isinstance(exc, ResolvedTagMarkerConflictError)
                and exc.service == _retag_update_service(item)
                else ""
            ),
        )
        for item in stack_updates
    ]


def _retag_preview_empty_issue(
    stack: ComposeStack,
    item: _RetagPlanUpdate,
) -> RetagPlanIssue:
    return RetagPlanIssue(
        severity="error",
        code="retag-compose-preview-empty",
        message=f"Could not preview a Compose rewrite for {item.service_key}.",
        service_key=item.service_key,
        stack=stack.name,
        service=_retag_update_service(item),
    )


def _retag_update_with_label_rewrites(
    item: _RetagPlanUpdate,
    applied_item: AppliedDigestPinUpdate,
) -> _RetagPlanUpdate:
    return _RetagPlanUpdate(
        target_id=item.target_id,
        service_key=item.service_key,
        stack=item.stack,
        update=item.update,
        provenance=item.provenance,
        runtime_state=item.runtime_state,
        allow_start=item.allow_start,
        known_image_service_key_ambiguous=item.known_image_service_key_ambiguous,
        digest_pin=item.digest_pin,
        label_rewrites=tuple(
            RetagPlanLabelRewrite(
                service=rewrite.service,
                label_key=rewrite.label_key,
                current_label_value=rewrite.current_label_value,
                planned_tag=rewrite.planned_tag,
                proposed_label_value=rewrite.proposed_label_value,
                proposed_label_regex=rewrite.proposed_label_regex,
                approved=rewrite.approved,
                reason=rewrite.reason,
            )
            for rewrite in applied_item.label_rewrites
        ),
    )


def _retag_update_identity(item: _RetagPlanUpdate) -> str:
    return item.target_id


def _submit_retag_apply_job(
    request: Request,
    settings: WebSettings,
    payload: RetagApplyRequest,
) -> ApplyJobResponse:
    state = request.app.state
    apply_condition: Condition = state.web_apply_condition
    jobs: dict[str, WebApplyJob] = state.web_apply_jobs
    executor = state.web_apply_executor
    with apply_condition:
        active_error = web_jobs._active_mutation_error_unlocked(state)
        if active_error:
            raise HTTPException(status_code=409, detail=active_error)
        job = WebApplyJob(
            id=secrets.token_urlsafe(18),
            status="queued",
            selected_line_numbers=(),
        )
        jobs[job.id] = job
        response = web_jobs._apply_job_response(job)
        apply_condition.notify_all()
        try:
            executor.submit(
                _run_retag_apply_job,
                settings,
                payload,
                jobs,
                apply_condition,
                job.id,
            )
        except Exception:
            del jobs[job.id]
            apply_condition.notify_all()
            raise
        return response


def _run_retag_apply_job(
    settings: WebSettings,
    payload: RetagApplyRequest,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> None:
    web_jobs._update_apply_job(
        jobs,
        apply_condition,
        job_id,
        status="running",
        started_at=utc_timestamp(),
    )
    run_id: int | None = None
    build: _RetagPlanBuild | None = None
    wud_lock: object | None = None
    preflight = True
    successful_updates: tuple[_RetagPlanUpdate, ...] = ()
    web_jobs._append_apply_job_progress(
        jobs,
        apply_condition,
        job_id,
        UpdaterProgressEvent(
            phase="preflight",
            status="running",
            message="Revalidating the selected retag plan.",
        ),
    )
    try:
        wud_lock = web_jobs._acquire_apply_wud_lock(settings)
        build = _build_current_retag_plan(
            settings,
            RetagPlanRequest(
                choices=payload.choices,
                github_latest_fallback=payload.github_latest_fallback,
            ),
        )
        plan = build.response
        if not secrets.compare_digest(plan.plan_id, payload.plan_id):
            raise RuntimeError("retag plan is stale")
        if not plan.can_apply or not build.updates:
            raise RuntimeError("retag plan is not ready to apply")
        web_jobs._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="preflight",
                status="success",
                message="Selected retag plan revalidated.",
            ),
        )
        preflight = False
        run_id = _insert_retag_audit_run(settings, build, status="running")
        successful_updates = _apply_retag_updates(
            settings,
            build,
            jobs,
            apply_condition,
            job_id,
        )
        _finish_retag_audit_run(settings, run_id, build, status="success")
        web_jobs._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="completion",
                status="success",
                message="Retag changes applied.",
            ),
        )
        web_jobs._update_apply_job(
            jobs,
            apply_condition,
            job_id,
            status="success",
            run_id=run_id,
            finished_at=utc_timestamp(),
        )
    except Exception as exc:  # noqa: BLE001 - the apply job records all failures.
        if isinstance(exc, _RetagApplyFailed):
            successful_updates = exc.successful_updates
        safe_error = _safe_retag_apply_error(settings, exc)
        web_jobs._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="preflight" if preflight else "completion",
                status="failure",
                message=safe_error,
            ),
        )
        web_jobs._update_apply_job(
            jobs,
            apply_condition,
            job_id,
            status="failure",
            run_id=run_id,
            finished_at=utc_timestamp(),
            error=safe_error,
        )
        if run_id is not None and build is not None:
            _finish_retag_audit_run(
                settings,
                run_id,
                build,
                status="failure",
                error=safe_error,
                successful_updates=successful_updates,
            )
    finally:
        close = getattr(wud_lock, "close", None) if wud_lock is not None else None
        if close is not None:
            close()


def _apply_retag_updates(
    settings: WebSettings,
    build: _RetagPlanBuild,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> tuple[_RetagPlanUpdate, ...]:
    env = settings.command_env
    runner = CommandRunner(env=env) if env is not None else CommandRunner()
    compose = ComposeCli(runner=runner)
    docker = DockerCli(runner=runner)
    config = _effective_config(settings)
    successful_updates: list[_RetagPlanUpdate] = []
    for stack in _ordered_retag_stacks(build.updates):
        stack_updates = [item for item in build.updates if item.stack.index == stack.index]
        services = tuple(
            sorted(
                {
                    service
                    for item in stack_updates
                    for service in item.update.services
                }
            )
        )
        backup: Path | None = None
        try:
            _revalidate_retag_runtime_before_apply(settings, compose, stack_updates)
            _progress(
                jobs,
                apply_condition,
                job_id,
                "compose-retag",
                "running",
                f"[{stack.name}] Writing selected retag to Compose.",
                stack=stack.name,
                services=services,
            )
            compose_path = stack.directory / stack.file
            backup = _backup_compose(compose_path)
            applier = (
                apply_compose_digest_pins
                if stack_updates[0].digest_pin
                else apply_compose_retag_updates
            )
            applied = applier(
                compose_path,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
            )
            if not applied:
                raise RuntimeError("no Compose image lines were retagged")
            _progress(
                jobs,
                apply_condition,
                job_id,
                "compose-retag",
                "success",
                f"[{stack.name}] Selected retag was written to Compose.",
                stack=stack.name,
                services=services,
            )

            _progress(
                jobs,
                apply_condition,
                job_id,
                "pull",
                "running",
                f"[{stack.name}] Pulling retagged service image(s).",
                stack=stack.name,
                services=services,
            )
            compose.pull(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
            _progress(
                jobs,
                apply_condition,
                job_id,
                "pull",
                "success",
                f"[{stack.name}] Retagged service image(s) pulled.",
                stack=stack.name,
                services=services,
            )

            _recreate_retag_services(
                compose,
                docker,
                config,
                stack,
                services,
                jobs,
                apply_condition,
                job_id,
            )
            _record_successful_retag_known_images(settings, stack_updates)
            if backup is not None:
                _delete_path(backup)
                backup = None
            successful_updates.extend(stack_updates)
        except Exception as exc:
            if backup is not None:
                try:
                    _restore_retag_compose(
                        compose,
                        docker,
                        config,
                        stack,
                        services,
                        backup,
                        jobs,
                        apply_condition,
                        job_id,
                        original_error=str(exc),
                    )
                except Exception as restore_exc:
                    raise _RetagApplyFailed(
                        str(restore_exc),
                        successful_updates,
                    ) from restore_exc
                backup = None
            raise _RetagApplyFailed(str(exc), successful_updates) from exc
    return tuple(successful_updates)


def _revalidate_retag_runtime_before_apply(
    settings: WebSettings,
    compose: ComposeCli,
    updates: Sequence[_RetagPlanUpdate],
) -> None:
    if not updates:
        return
    stack = updates[0].stack
    project_name = compose.try_config_project_name(
        stack.directory,
        stack.file,
        project_directory=stack.project_directory,
    )
    if not project_name:
        raise RuntimeError("retag Compose project could not be revalidated")
    if project_name != stack.project_name:
        raise RuntimeError("retag Compose project changed before apply")
    running_service_keys = _running_retag_compose_service_keys(settings)
    if running_service_keys is None:
        raise RuntimeError("retag runtime state could not be revalidated")
    for item in updates:
        if item.allow_start:
            continue
        service = _retag_update_service(item)
        if (
            _retag_compose_service_key(
                item.stack,
                service,
                project_name=project_name,
            )
            not in running_service_keys
        ):
            raise RuntimeError(
                f"{item.service_key} is no longer running in the expected Compose project"
            )


def _recreate_retag_services(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    services: Sequence[str],
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> None:
    _progress(
        jobs,
        apply_condition,
        job_id,
        "recreate",
        "running",
        f"[{stack.name}] Recreating retagged service container(s).",
        stack=stack.name,
        services=services,
    )
    pre_up_error: CommandError | None = None
    if config.update_mode == "pause":
        try:
            compose.pause(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError:
            pass
    elif config.update_mode == "stop":
        try:
            compose.stop(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            pre_up_error = exc

    if config.update_mode == "pause":
        try:
            wait_handled = _compose_up_retag_services(compose, stack, services, config)
        except Exception:
            compose.unpause(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
            raise
        compose.unpause(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
    else:
        wait_handled = _compose_up_retag_services(compose, stack, services, config)
    if pre_up_error is not None:
        raise pre_up_error

    _progress(
        jobs,
        apply_condition,
        job_id,
        "recreate",
        "success",
        f"[{stack.name}] Retagged service container(s) recreated.",
        stack=stack.name,
        services=services,
    )
    if wait_handled:
        _progress(
            jobs,
            apply_condition,
            job_id,
            "health",
            "success",
            f"[{stack.name}] Compose reported healthy retagged service container(s).",
            stack=stack.name,
            services=services,
        )
        return
    _wait_for_retag_health(
        compose,
        docker,
        config,
        stack,
        services,
        jobs,
        apply_condition,
        job_id,
    )


def _compose_up_retag_services(
    compose: ComposeCli,
    stack: ComposeStack,
    services: Sequence[str],
    config: UpdaterConfig,
) -> bool:
    wait = (
        config.update_mode != "pause"
        and compose.up_wait_supported(
            stack.directory,
            stack.file,
            project_directory=stack.project_directory,
        )
    )
    compose.up(
        stack.directory,
        stack.file,
        services,
        wait=wait,
        wait_timeout=config.max_wait if wait else None,
        force_recreate=True,
        no_deps=True,
        project_directory=stack.project_directory,
    )
    return wait


def _wait_for_retag_health(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    services: Sequence[str],
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> None:
    _progress(
        jobs,
        apply_condition,
        job_id,
        "health",
        "running",
        f"[{stack.name}] Waiting up to {config.max_wait}s for retagged service health.",
        stack=stack.name,
        services=services,
    )
    start = time.monotonic()
    if config.max_wait > 0:
        time.sleep(2)
    while True:
        cids = compose.ps_quiet(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
        ok = bool(cids)
        for cid in cids:
            summary = _first_nonblank(
                docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT)
            )
            if not summary or not _cid_is_ok(summary):
                ok = False
        elapsed = int(time.monotonic() - start)
        if ok:
            _progress(
                jobs,
                apply_condition,
                job_id,
                "health",
                "success",
                f"[{stack.name}] Retagged service health wait succeeded in {elapsed}s.",
                stack=stack.name,
                services=services,
            )
            return
        if elapsed >= config.max_wait:
            detail = _retag_health_details(compose, docker, stack, services)
            raise RuntimeError(
                f"[{stack.name}] retagged service health wait failed after {elapsed}s"
                + (f": {detail}" if detail else "")
            )
        time.sleep(2)


def _restore_retag_compose(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    services: Sequence[str],
    backup: Path,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    *,
    original_error: str,
) -> None:
    try:
        shutil.copy2(backup, stack.directory / stack.file)
        wait_handled = _compose_up_retag_services(compose, stack, services, config)
        if not wait_handled:
            _wait_for_retag_health(
                compose,
                docker,
                config,
                stack,
                services,
                jobs,
                apply_condition,
                job_id,
            )
        _delete_path(backup)
    except Exception as rollback_exc:
        raise RuntimeError(
            f"{original_error}; compose rollback failed: {rollback_exc}; "
            f"backup retained at {backup}"
        ) from rollback_exc


def _record_successful_retag_known_images(
    settings: WebSettings,
    updates: Sequence[_RetagPlanUpdate],
) -> None:
    if not updates:
        return
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        for item in updates:
            if item.known_image_service_key_ambiguous:
                continue
            upsert_known_image(
                conn,
                service_key=item.service_key,
                image=item.update.final_image,
                digest=item.update.planned_digest,
                metadata_json=_json_object(
                    {
                        "source": "webui",
                        "operation": "retag",
                    }
                ),
                digest_provenance=_retag_digest_provenance(
                    item,
                    confidence="verified",
                ),
            )


def _insert_retag_audit_run(
    settings: WebSettings,
    build: _RetagPlanBuild,
    *,
    status: str,
) -> int:
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        return insert_update_run(
            conn,
            status=status,
            dry_run=False,
            mode="web-retag",
            wud_file=str(settings.config.wud_out_file),
            metadata_json=_json_object(_retag_audit_metadata(build, status=status)),
        )


def _finish_retag_audit_run(
    settings: WebSettings,
    run_id: int,
    build: _RetagPlanBuild,
    *,
    status: str,
    error: str = "",
    successful_updates: Sequence[_RetagPlanUpdate] = (),
) -> None:
    metadata = _retag_audit_metadata(build, status=status, error=error)
    successful_update_ids = {
        _retag_update_identity(item) for item in successful_updates
    }
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        now = utc_timestamp()
        for item in build.updates:
            item_status = (
                "success"
                if status == "success"
                or _retag_update_identity(item) in successful_update_ids
                else "failure"
            )
            event_metadata: dict[str, object] = {
                "source": "webui",
                "operation": "retag",
                "service_key": item.service_key,
                "target_id": item.target_id,
                "resolved_tag": item.update.resolved_tag,
                "watch_tag": item.update.watch_tag,
                "known_image_recorded": (
                    item_status == "success"
                    and not item.known_image_service_key_ambiguous
                ),
            }
            if item_status == "success" and item.known_image_service_key_ambiguous:
                event_metadata["known_image_skip_reason"] = (
                    "duplicate service_key"
                )
            insert_update_event(
                conn,
                run_id=run_id,
                created_at=now,
                service_name=item.update.services[0] if item.update.services else "",
                stack_name=item.stack.name,
                image=item.update.old_image,
                target_image=item.update.final_image,
                status=item_status,
                metadata_json=_json_object(event_metadata),
                digest_provenance=_retag_digest_provenance(
                    item,
                    confidence=(
                        "verified" if item_status == "success" else "planned"
                    ),
                ),
            )
        conn.execute(
            """
            UPDATE update_runs
            SET finished_at = ?, status = ?, metadata_json = ?
            WHERE id = ?
            """,
            (now, status, _json_object(metadata), run_id),
        )
        conn.commit()


def _retag_audit_metadata(
    build: _RetagPlanBuild,
    *,
    status: str,
    error: str = "",
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source": "webui",
        "operation": "retag",
        "status": status,
        "plan_id": build.response.plan_id,
        "services": [item.service_key for item in build.updates],
        "targets": [item.target_id for item in build.updates],
        "external_recreate_required": False,
        "retag_digest_pins": any(item.digest_pin for item in build.updates),
        "tag_updates": [
            _retag_plan_tag_update(item).model_dump(mode="json")
            for item in build.updates
            if not item.digest_pin
        ],
        "digest_pin_updates": [
            _retag_plan_digest_update(item).model_dump(mode="json")
            for item in build.updates
            if item.digest_pin
        ],
    }
    if error:
        metadata["error"] = error
    return metadata


def _retag_digest_provenance(
    item: _RetagPlanUpdate,
    *,
    confidence: str,
) -> DigestTagProvenance | None:
    if not item.digest_pin:
        return None
    return DigestTagProvenance(
        source_image=item.update.old_image,
        resolved_tag=item.update.resolved_tag,
        watch_tag=item.update.watch_tag,
        target_digest=item.update.planned_digest,
        final_image=item.update.final_image,
        provenance_source="retag",
        provenance_confidence=confidence,
    )


def _safe_retag_apply_error(settings: WebSettings, exc: BaseException) -> str:
    return _safe_exception_detail(settings, "retag apply failed", exc)


def _progress(
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    phase: str,
    status: str,
    message: str,
    *,
    stack: str = "",
    services: Sequence[str] = (),
) -> None:
    web_jobs._append_apply_job_progress(
        jobs,
        apply_condition,
        job_id,
        UpdaterProgressEvent(
            phase=phase,
            status=status,
            message=message,
            stack=stack,
            services=tuple(services),
        ),
    )


def _first_nonblank(values: Sequence[str]) -> str:
    for value in values:
        if value.strip():
            return value.strip()
    return ""


def _retag_health_details(
    compose: ComposeCli,
    docker: DockerCli,
    stack: ComposeStack,
    services: Sequence[str],
) -> str:
    cids = compose.ps_quiet(
        stack.directory,
        stack.file,
        services,
        project_directory=stack.project_directory,
    )
    details: list[str] = []
    if not cids:
        details.append("docker compose ps -q returned no containers")
    for cid in cids:
        summary = _first_nonblank(docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT))
        if summary:
            details.append(summary)
        for output in docker.try_inspect(cid, HEALTH_LOG_FORMAT):
            if output.strip():
                details.append(output.strip())
    return "; ".join(details)


def _delete_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _effective_config(settings: WebSettings) -> UpdaterConfig:
    if _effective_config_loader is None:
        return settings.config
    try:
        return _effective_config_loader(settings)
    except ConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail=_safe_exception_detail(
                settings,
                "could not read effective config",
                exc,
            ),
        ) from exc


def _retag_digest_pins(settings: WebSettings) -> bool:
    if _retag_digest_pins_loader is None:
        return False
    return _retag_digest_pins_loader(settings)


def _retag_target_item(
    *,
    target_id: str,
    service_key: str,
    stack: str,
    service_image: ServiceImage,
    directory: str,
    compose_file: str,
    project_directory: str,
    known_image: str,
    provenance: DigestTagProvenance | None,
    github_latest: _RetagGitHubLatestFallback | None,
    github_latest_fallback: bool,
    allow_source_image_match: bool,
    runtime_state: RetagRuntimeState,
    digest_pins: bool,
) -> RetagTargetItem:
    label_value = _label_value(service_image.labels, WUD_TAG_INCLUDE_LABEL)
    tracking_tag, tracking_tag_source = _tracking_tag(
        service_image.image,
        label_value=label_value,
        provenance=provenance,
    )
    retag_available, retag_reason = _retag_eligibility(
        service_image.image,
        known_image=known_image,
        tracking_tag=tracking_tag,
        tracking_tag_source=tracking_tag_source,
        label_value=label_value,
        provenance=provenance,
        allow_source_image_match=allow_source_image_match,
    )
    choices = [KEEP_CURRENT_CHOICE]
    if retag_available:
        choices.append(SWITCH_TO_CONCRETE_CHOICE)
    proposed_tag = "" if provenance is None else provenance.resolved_tag
    final_image = ""
    if provenance is not None:
        final_image = (
            provenance.final_image
            if digest_pins
            else image_with_tag(service_image.image, provenance.resolved_tag)
        )
    candidate_source = "provenance" if provenance is not None else ""
    candidate_warning = ""
    candidate_link_label = ""
    candidate_link_url = ""
    if github_latest is not None:
        candidate_source = "github-latest"
        candidate_warning = github_latest.warning
        candidate_link_label = github_latest.link_label
        candidate_link_url = github_latest.link_url
        if not proposed_tag:
            proposed_tag = github_latest.proposed_tag
    elif (
        github_latest_fallback
        and provenance is None
        and tracking_tag == "latest"
        and retag_reason == "missing-provenance"
    ):
        candidate_source = "github-latest"
        candidate_warning = GITHUB_LATEST_MISSING_CACHE_WARNING
    if not candidate_link_url:
        candidate_link_label, candidate_link_url = _inferred_candidate_link(
            service_image.image
        )
    return RetagTargetItem(
        target_id=target_id,
        service_key=service_key,
        stack=stack,
        service=service_image.service,
        image=service_image.image,
        image_repo=repo_key(service_image.image),
        current_tag=image_tag(service_image.image),
        tracking_tag=tracking_tag,
        tracking_tag_source=tracking_tag_source,
        proposed_tag=proposed_tag,
        final_image=final_image,
        candidate_source=candidate_source,
        candidate_warning=candidate_warning,
        candidate_link_label=candidate_link_label,
        candidate_link_url=candidate_link_url,
        runtime_state=runtime_state,
        retag_available=retag_available,
        retag_reason=retag_reason,
        choices=choices,
        label_key=WUD_TAG_INCLUDE_LABEL,
        label_value=label_value,
        directory=directory,
        compose_file=compose_file,
        project_directory=project_directory,
        digest_provenance=(
            None if provenance is None else asdict(provenance)
        ),
    )


def _inferred_candidate_link(image: str) -> tuple[str, str]:
    repo_ref = image_repo_ref(image)
    match = _GHCR_GITHUB_REPO_RE.fullmatch(repo_ref)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
        if repo not in {".", ".."}:
            return "GitHub tags", f"https://github.com/{owner}/{repo}/tags"
    return "", ""


def _tracking_tag(
    image: str,
    *,
    label_value: str,
    provenance: DigestTagProvenance | None,
) -> tuple[str, str]:
    if label_value:
        label_tag = _single_exact_tag(label_value)
        if label_tag:
            return label_tag, "label"
        return "", "unsupported-label"
    if provenance is not None and provenance.watch_tag:
        return provenance.watch_tag, "provenance"
    tag = image_tag(image)
    if tag:
        return tag, "image"
    return "", ""


def _retag_eligibility(
    image: str,
    *,
    known_image: str,
    tracking_tag: str,
    tracking_tag_source: str,
    label_value: str,
    provenance: DigestTagProvenance | None,
    allow_source_image_match: bool = False,
) -> tuple[bool, str]:
    if tracking_tag_source == "unsupported-label":
        return False, "unsupported-tracking-label"
    if tracking_tag != "latest":
        return False, "not-latest-tracking"
    if provenance is None:
        return False, "missing-provenance"
    if not _provenance_matches_image(
        image,
        known_image=known_image,
        provenance=provenance,
        allow_source_image_match=allow_source_image_match,
    ):
        return False, "stale-provenance"
    if not provenance.resolved_tag or provenance.resolved_tag == "latest":
        return False, "missing-concrete-tag"
    if not tag_value_valid(provenance.resolved_tag):
        return False, "invalid-candidate-tag"
    if not provenance.target_digest or not provenance.final_image:
        return False, "missing-final-image"
    if label_value and not _single_exact_tag(label_value):
        return False, "unsupported-tracking-label"
    return True, "eligible"


def _provenance_matches_image(
    image: str,
    *,
    known_image: str,
    provenance: DigestTagProvenance,
    allow_source_image_match: bool = False,
) -> bool:
    candidates = {known_image, provenance.final_image}
    if allow_source_image_match:
        candidates.add(provenance.source_image)
    return image in candidates


def _label_value(labels: tuple[tuple[str, str], ...], key: str) -> str:
    values: Mapping[str, str] = dict(labels)
    return values.get(key, "")


def _single_exact_tag(value: str) -> str:
    normalized = compose_unescape_dollars(value)
    if tag_value_valid(normalized):
        return normalized
    if not normalized.startswith("^") or not normalized.endswith("$"):
        return ""
    tag_chars: list[str] = []
    index = 1
    end = len(normalized) - 1
    while index < end:
        char = normalized[index]
        if char == "\\":
            index += 1
            if index >= end:
                return ""
            escaped = normalized[index]
            if escaped not in _REGEX_SPECIAL_CHARS:
                return ""
            tag_chars.append(escaped)
            index += 1
            continue
        if char in _REGEX_SPECIAL_CHARS:
            return ""
        tag_chars.append(char)
        index += 1
    tag = "".join(tag_chars)
    return tag if tag_value_valid(tag) else ""
