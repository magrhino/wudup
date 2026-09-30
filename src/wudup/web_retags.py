"""WebUI retag review route handlers."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace

from fastapi import HTTPException, Request

from . import web_database, web_job_registry, web_retag_apply, web_retag_preview
from .compose import (
    ComposeStack,
    ServiceImage,
)
from .compose_rewrite import (
    WUD_TAG_INCLUDE_LABEL,
    compose_escape_dollars,
    render_compose_digest_pins,
    render_compose_retag_updates,
)
from .db import (
    DatabaseError,
    init_db,
    open_db,
)
from .digest_provenance import DigestTagProvenance
from .digest_verifier import DigestVerifier
from .docker_cli import DockerCli
from .images import (
    image_has_tag,
    image_key,
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
from .tag_streams import retag_tag_include_regex
from .updater_digest_pin import digest_pin_update_from_values
from .updater_models import (
    AppliedDigestPinUpdate,
    DigestPinUpdate,
    ResolvedTagMarkerConflictError,
)
from .web_database import ReadOnlyDatabaseMissing
from .web_models import (
    ApplyJobResponse,
    RetagApplyRequest,
    RetagChoiceRequest,
    RetagPlanIssue,
    RetagPlanLabelRewrite,
    RetagPlanRequest,
    RetagPlanResponse,
    RetagPreviewJobResponse,
    RetagTargetItem,
    RetagTargetsResponse,
    WebSettings,
)
from .web_redaction import redact_sensitive_text as _redact_sensitive_text
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_release_notes import release_note_source_resolver
from .web_request_context import request_settings as _settings
from .web_retag_choices import validated_retag_choice_map
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
from .web_retag_plans import retag_config_transforms
from .web_retag_plans import (
    retag_plan_id as _retag_plan_id,
)
from .web_retag_plans import (
    retag_plan_stacks as _retag_plan_stacks,
)
from .web_retag_plans import (
    retag_plan_status as _retag_plan_status,
)
from .web_retag_plans import retag_update_identity as _retag_update_identity
from .web_retag_plans import (
    retag_update_service as _retag_update_service,
)
from .web_retag_preview import (
    initialize_retag_preview_state,  # noqa: F401 - application lifecycle compatibility
    shutdown_retag_preview_state,  # noqa: F401 - application lifecycle compatibility
)
from .web_retag_runtime import _command_runner
from .web_retag_targets import (
    GITHUB_LATEST_MISSING_CACHE_WARNING,
    KEEP_CURRENT_CHOICE,
    _discover_retag_stacks,
    _effective_config,
    _known_state_for_retag_target,
    _label_value,
    _retag_digest_pins,
    _retag_service_counts,
    _retag_service_key,
    _retag_target_id,
    _retag_target_records,
    _RetagTargetRecord,
    _tracking_tag,
)
from .wud_file import WudTarget

MUTATIONS_DISABLED_DETAIL = "mutations are disabled"


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
    return web_retag_preview.start_retag_plan_preview(
        request.app.state, settings, payload, build_plan=_build_current_retag_plan,
    )


def api_retag_plan_preview_job(
    preview_job_id: str,
    request: Request,
) -> RetagPreviewJobResponse:
    return web_retag_preview.retag_preview_job_response(
        request.app.state, preview_job_id,
    )


def api_apply_retag_plan(
    payload: RetagApplyRequest,
    request: Request,
) -> ApplyJobResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    active_error = web_job_registry._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    return web_retag_apply.submit_retag_apply_job(
        request.app.state, settings, payload,
        build_plan=_build_current_retag_plan,
        effective_config=_effective_config,
    )


def retag_targets_response(
    settings: WebSettings,
    *,
    github_latest_fallback: bool = False,
) -> RetagTargetsResponse:
    discovery = _retag_target_records(
        settings,
        github_latest_fallback=github_latest_fallback,
        github_latest_resolver=_cached_github_latest_fallback_by_target,
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


def _build_current_retag_plan(
    settings: WebSettings,
    payload: RetagPlanRequest,
) -> _RetagPlanBuild:
    return build_retag_plan(settings, payload)


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
        github_latest_resolver=_cached_github_latest_fallback_by_target,
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
        update = _selected_tag_retag_update(update)
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
        update = _selected_tag_retag_update(update)
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


def _selected_tag_retag_update(update: DigestPinUpdate) -> DigestPinUpdate:
    return replace(
        update,
        final_image=update.resolved_image,
        marker="",
        label_value=compose_escape_dollars(
            retag_tag_include_regex(update.watch_tag)
        ),
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
        if stack_updates[0].digest_pin:
            _rendered, applied = render_compose_digest_pins(
                stack.directory / stack.file,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
            )
        else:
            _rendered, applied = render_compose_retag_updates(
                stack.directory / stack.file,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
                config_transforms=retag_config_transforms(stack),
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
        transform_label_value=dict(applied_item.added_transforms).get(
            item.update.services[0] if item.update.services else "", ""
        ),
    )
