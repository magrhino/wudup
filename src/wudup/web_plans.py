"""WebUI plan creation and apply-job route handlers."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import asdict, replace

from fastapi import HTTPException, Request

from . import (
    web_database,
    web_diagnostics,
    web_file_selection_store,
    web_jobs,
    web_pending_sources,
    web_scheduler,
    web_wud_refresh,
)
from .config import ConfigError, UpdaterConfig
from .images import tag_value_valid
from .locks import DirectoryLock
from .plan_matching import pending_target_key
from .plans import (
    DryRunPlan,
    PlanFileMissing,
    PlanInputError,
    _PlanSelectionScope,
    build_dry_run_plan_from_pending_source,
)
from .updater_models import (
    DigestPinLabelRewriteApproval,
    TagOverride,
    TagStreamDecision,
    TagStreamLabelRewriteApproval,
    UpdateSelection,
)
from .web_models import (
    ApplyJobResponse,
    ApplyPlanRequest,
    PlanRequest,
    PlanResponse,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_request_context import request_settings as _settings

EffectiveConfigLoader = Callable[[WebSettings], UpdaterConfig]
_effective_config_loader: EffectiveConfigLoader | None = None
_PLAN_CREATE_ERROR = "could not create plan"
_PLAN_REVALIDATION_ERROR = "could not revalidate plan"


def configure(*, effective_config_loader: EffectiveConfigLoader) -> None:
    global _effective_config_loader
    _effective_config_loader = effective_config_loader


def api_create_plan(payload: PlanRequest, request: Request) -> PlanResponse:
    settings = _settings(request)
    try:
        plan = build_web_plan(settings, payload)
    except PlanInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PlanFileMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, _PLAN_CREATE_ERROR, exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, _PLAN_CREATE_ERROR, exc),
        ) from exc
    return plan_response(plan, settings, request)


def api_create_job(payload: ApplyPlanRequest, request: Request) -> ApplyJobResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    wud_lock: DirectoryLock | None = None
    try:
        pending_source = _resolve_pending_source_for_apply(settings)
        if pending_source.active == "file":
            wud_lock = web_jobs._acquire_apply_wud_lock(settings)
            pending_source = _resolve_pending_source_for_apply(settings)
            if pending_source.active != "file":
                wud_lock.close()
                wud_lock = None
        try:
            plan = build_web_plan(
                settings,
                PlanRequest(
                    line_numbers=payload.line_numbers,
                    selections=payload.selections,
                    allow_tag_updates=payload.allow_tag_updates,
                    tag_overrides=payload.tag_overrides,
                    tag_stream_decisions=payload.tag_stream_decisions,
                    tag_stream_label_rewrite_approvals=(
                        payload.tag_stream_label_rewrite_approvals
                    ),
                    digest_pin_label_rewrite_approvals=(
                        payload.digest_pin_label_rewrite_approvals
                    ),
                ),
                pending_source=pending_source,
            )
        except (PlanInputError, PlanFileMissing) as exc:
            raise HTTPException(status_code=409, detail="plan is stale") from exc
        except ConfigError as exc:
            raise HTTPException(
                status_code=409,
                detail=_safe_exception_detail(
                    settings,
                    _PLAN_REVALIDATION_ERROR,
                    exc,
                ),
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    _PLAN_REVALIDATION_ERROR,
                    exc,
                ),
            ) from exc

        if not secrets.compare_digest(plan.plan_id, payload.plan_id):
            raise HTTPException(status_code=409, detail="plan is stale")
        if not plan_can_apply(plan, settings):
            raise HTTPException(status_code=409, detail="plan is not ready to apply")
        apply_preflight = web_diagnostics.apply_preflight_response(
            settings,
            request,
            plan,
        )
        if not apply_preflight.ok:
            raise HTTPException(status_code=409, detail="apply preflight failed")
        return submit_apply_job(
            request,
            settings,
            plan,
            payload,
            wud_lock,
            pending_source=pending_source,
        )
    except Exception:
        if wud_lock is not None:
            wud_lock.close()
        raise


def api_apply_plan(payload: ApplyPlanRequest, request: Request) -> ApplyJobResponse:
    return api_create_job(payload, request)


def _resolve_pending_source_for_apply(
    settings: WebSettings,
) -> web_pending_sources.PendingSourceResult:
    try:
        return web_wud_refresh.refresh_wud_pending_source(
            settings,
            include_wud_metadata=False,
            force=True,
        ).source
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                _PLAN_REVALIDATION_ERROR,
                exc,
            ),
        ) from exc


def build_web_plan(
    settings: WebSettings,
    payload: PlanRequest,
    *,
    update_mode_override: str | None = None,
    pending_source: web_pending_sources.PendingSourceResult | None = None,
    force_api: bool = False,
) -> DryRunPlan:
    base_config = _effective_config(settings)
    config: UpdaterConfig = (
        base_config
        if update_mode_override is None
        else replace(base_config, update_mode=update_mode_override)
    )
    source = pending_source or web_wud_refresh.refresh_wud_pending_source(
        settings,
        include_wud_metadata=False,
        force=force_api,
    ).source
    if source.active == "file" and not source.exists:
        raise PlanFileMissing(f"WUD file not found: {settings.config.wud_out_file}")
    completed_update_selections = (
        web_file_selection_store.load_completed_update_selections(
            settings.config.db_path,
            pending_file=settings.config.wud_out_file,
            pending_target_keys={
                pending_target_key(target.raw)
                for target in source.parsed.targets
            },
        )
        if source.active == "file" and payload.selections
        else ()
    )
    return build_dry_run_plan_from_pending_source(
        config,
        source.parsed,
        source_file=source.source_file,
        source_hash=source.source_hash,
        source=source.plan_source(),
        line_numbers=payload.line_numbers,
        selection_scope=_PlanSelectionScope(
            update_selections=update_selections_from_payload(payload),
            completed_update_selections=completed_update_selections,
        ),
        allow_tag_updates=payload.allow_tag_updates,
        tag_overrides=tag_overrides_from_payload(payload),
        tag_stream_decisions=tag_stream_decisions_from_payload(payload),
        tag_stream_label_rewrite_approvals=(
            tag_stream_label_rewrite_approvals_from_payload(payload)
        ),
        digest_pin_label_rewrite_approvals=(
            digest_pin_label_rewrite_approvals_from_payload(payload)
        ),
        host_docker_base=settings.host_docker_base,
        environ=settings.command_env,
        known_digest_provenance_by_service=(
            web_database.known_digest_provenance_by_service(settings)
        ),
    )


def tag_overrides_from_payload(
    payload: PlanRequest | ApplyPlanRequest,
) -> tuple[TagOverride, ...]:
    overrides: list[TagOverride] = []
    seen: set[int] = set()
    for item in payload.tag_overrides:
        line_no = item.line_no
        if line_no in seen:
            raise PlanInputError(
                f"tag_overrides line {line_no} was provided more than once"
            )
        if not tag_value_valid(item.tag):
            raise PlanInputError(
                f"tag_overrides line {line_no} has invalid tag: {item.tag}"
            )
        overrides.append(TagOverride(line_no=line_no, tag=item.tag))
        seen.add(line_no)
    return tuple(overrides)


def update_selections_from_payload(
    payload: PlanRequest | ApplyPlanRequest,
) -> tuple[UpdateSelection, ...]:
    return tuple(
        UpdateSelection(
            line_no=item.line_no,
            selection_id=item.selection_id,
        )
        for item in payload.selections
    )


def tag_stream_decisions_from_payload(
    payload: PlanRequest | ApplyPlanRequest,
) -> tuple[TagStreamDecision, ...]:
    decisions: list[TagStreamDecision] = []
    seen: set[int] = set()
    for item in payload.tag_stream_decisions:
        if item.line_no in seen:
            raise PlanInputError(
                f"tag_stream_decisions line {item.line_no} was provided more than once"
            )
        decisions.append(
            TagStreamDecision(line_no=item.line_no, decision=item.decision)
        )
        seen.add(item.line_no)
    return tuple(decisions)


def tag_stream_label_rewrite_approvals_from_payload(
    payload: PlanRequest | ApplyPlanRequest,
) -> tuple[TagStreamLabelRewriteApproval, ...]:
    approvals: list[TagStreamLabelRewriteApproval] = []
    seen: set[tuple[int, str, str, str, str, str, str, str, str]] = set()
    for item in payload.tag_stream_label_rewrite_approvals:
        key = (
            item.line_no,
            item.stack,
            item.stack_directory,
            item.compose_file,
            item.service,
            item.label_key,
            item.current_label_value,
            item.selected_tag,
            item.proposed_label_value,
        )
        if key in seen:
            raise PlanInputError(
                "tag_stream_label_rewrite_approvals contains a duplicate approval"
            )
        if item.label_key != "wud.tag.include":
            raise PlanInputError(
                "tag_stream_label_rewrite_approvals can only approve wud.tag.include"
            )
        if not tag_value_valid(item.selected_tag):
            raise PlanInputError(
                "tag_stream_label_rewrite_approvals has an invalid selected tag"
            )
        approvals.append(
            TagStreamLabelRewriteApproval(
                line_no=item.line_no,
                stack=item.stack,
                stack_directory=item.stack_directory,
                compose_file=item.compose_file,
                service=item.service,
                label_key=item.label_key,
                current_label_value=item.current_label_value,
                selected_tag=item.selected_tag,
                proposed_label_value=item.proposed_label_value,
            )
        )
        seen.add(key)
    return tuple(approvals)


def digest_pin_label_rewrite_approvals_from_payload(
    payload: PlanRequest | ApplyPlanRequest,
) -> tuple[DigestPinLabelRewriteApproval, ...]:
    approvals: list[DigestPinLabelRewriteApproval] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for item in payload.digest_pin_label_rewrite_approvals:
        key = (
            item.stack,
            item.service,
            item.label_key,
            item.current_label_value,
            item.planned_tag,
            item.proposed_label_value,
        )
        if key in seen:
            raise PlanInputError(
                "digest_pin_label_rewrite_approvals contains a duplicate approval"
            )
        if item.label_key != "wud.tag.include":
            raise PlanInputError(
                "digest_pin_label_rewrite_approvals can only approve wud.tag.include"
            )
        if not tag_value_valid(item.planned_tag):
            raise PlanInputError(
                "digest_pin_label_rewrite_approvals has an invalid planned tag"
            )
        approvals.append(
            DigestPinLabelRewriteApproval(
                stack=item.stack,
                service=item.service,
                label_key=item.label_key,
                current_label_value=item.current_label_value,
                planned_tag=item.planned_tag,
                proposed_label_value=item.proposed_label_value,
            )
        )
        seen.add(key)
    return tuple(approvals)


def plan_can_apply(plan: DryRunPlan, settings: WebSettings) -> bool:
    return (
        settings.mutations_enabled
        and plan.status == "ready"
        and all(status == "fresh" for status in plan.selected_metadata_statuses())
        and not plan.skipped
        and not any(issue.severity == "error" for issue in plan.issues)
    )


def plan_response(
    plan: DryRunPlan,
    settings: WebSettings,
    request: Request,
) -> PlanResponse:
    apply_preflight = web_diagnostics.apply_preflight_response(
        settings,
        request,
        plan,
    )
    payload = asdict(plan)
    for target in payload["targets"]:
        target["metadata_status"] = plan.metadata_status_for_line(target["line_no"])
    for stack in payload["stacks"]:
        for line in stack["lines"]:
            line["metadata_status"] = plan.metadata_status_for_line(line["line_no"])
    payload["can_apply"] = plan_can_apply(plan, settings) and apply_preflight.ok
    payload["cleanup"]["can_remove_unmatched"] = (
        settings.mutations_enabled
        and plan.source.active == "file"
        and bool(plan.cleanup.items)
    )
    payload["apply_preflight"] = apply_preflight.model_dump()
    return PlanResponse.model_validate(payload)


def submit_apply_job(
    request: Request,
    settings: WebSettings,
    plan: DryRunPlan,
    payload: ApplyPlanRequest,
    wud_lock: DirectoryLock | None,
    *,
    pending_source: web_pending_sources.PendingSourceResult,
) -> ApplyJobResponse:
    tag_stream_updates = web_jobs.tag_stream_updates_from_plan(plan)
    return web_jobs._submit_apply_job_state(
        request.app.state,
        settings,
        plan,
        allow_tag_updates=payload.allow_tag_updates,
        tag_overrides=tag_overrides_from_payload(payload),
        tag_stream_updates=tag_stream_updates,
        digest_pin_label_rewrite_approvals=(
            digest_pin_label_rewrite_approvals_from_payload(payload)
        ),
        wud_lock=wud_lock,
        effective_config_loader=_effective_config,
        auto_update_schedule_run_updater=(
            web_scheduler._safe_update_auto_update_schedule_runs
        ),
        run_context=web_jobs.ApplyJobRunContext(
            metadata_extra={
                "pending_source": plan.source.active,
                "pending_source_configured": plan.source.configured,
                "pending_source_degraded": plan.source.degraded,
                "pending_source_label": plan.source.label,
            },
            pending_source_text=(
                pending_source.text if pending_source.active == "api" else None
            ),
            pending_source_active=pending_source.active,
            pending_source_label=pending_source.label,
            pending_source_container_ids=(
                web_pending_sources.container_ids_for_lines(
                    pending_source,
                    plan.selected_line_numbers,
                )
            ),
        ),
    )


def _effective_config(settings: WebSettings) -> UpdaterConfig:
    if _effective_config_loader is None:
        return settings.config
    return _effective_config_loader(settings)


# Compatibility aliases for callers that imported private helpers from web.py.
_build_web_plan = build_web_plan
_tag_overrides_from_payload = tag_overrides_from_payload
_digest_pin_label_rewrite_approvals_from_payload = (
    digest_pin_label_rewrite_approvals_from_payload
)
_plan_can_apply = plan_can_apply
_plan_response = plan_response
_submit_apply_job = submit_apply_job
