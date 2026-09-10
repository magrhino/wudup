"""WebUI pending-update and update-target route handlers."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Protocol

from fastapi import HTTPException, Request

from . import (
    web_database,
    web_file_selection_store,
    web_jobs,
    web_pending_snoozes,
    web_pending_sources,
    web_wud_api,
    web_wud_refresh,
)
from .command import CommandError, CommandRunner
from .compose import (
    COMPOSE_RUNTIME_STATE_FORMAT,
    ComposeCli,
    ComposeDiscoveryError,
    ComposeRuntimeServiceState,
    compose_runtime_service_key,
    compose_runtime_service_key_matches,
    compose_runtime_service_states,
)
from .config import ConfigError, UpdaterConfig
from .db import (
    DatabaseError,
    init_db,
    open_db,
    utc_timestamp,
)
from .docker_cli import DockerCli
from .file_ops import OwnerConfig
from .images import image_tag, repo_key
from .plan_matching import pending_target_key
from .plans import (
    DryRunPlanCleanup,
    DryRunPlanCleanupItem,
    PlanFileMissing,
    PlanInputError,
    build_unmatched_cleanup,
    resolve_pending_groups,
)
from .tag_streams import pending_tag_stream_hint
from .updater_models import CompletedUpdateSelection
from .web_auth import (
    _immediate_transaction,
    _request_actor_type,
    _safe_exception_detail,
    _settings,
)
from .web_metadata import json_object as _json_object
from .web_models import (
    PendingCleanupLine,
    PendingCleanupRemovedLine,
    PendingCleanupRequest,
    PendingCleanupResponse,
    PendingDiagnostic,
    PendingGroupedItem,
    PendingGrouping,
    PendingItem,
    PendingMetadataRefreshItem,
    PendingMetadataRefreshRequest,
    PendingMetadataRefreshResponse,
    PendingMetadataStatus,
    PendingRemovalPlanLine,
    PendingRemovalPlanRequest,
    PendingRemovalPlanResponse,
    PendingRemovalRequest,
    PendingResponse,
    PendingStackGroup,
    PendingTagStream,
    UpdateTargetItem,
    UpdateTargetsResponse,
    WebSettings,
    WudApiStatus,
)
from .wud_file import (
    ParsedWudFile,
    parse_wud_file,
    remove_lines_before_run,
)

_PENDING_DOCKER_TIMEOUT_SECONDS = 10.0


class EffectiveConfigLoader(Protocol):
    def __call__(self, settings: WebSettings) -> UpdaterConfig: ...


_effective_config_loader: EffectiveConfigLoader | None = None


def configure(*, effective_config_loader: EffectiveConfigLoader) -> None:
    global _effective_config_loader
    _effective_config_loader = effective_config_loader


def api_pending(request: Request) -> PendingResponse:
    return pending_response(_settings(request), force_api=True)


def api_pending_metadata(
    payload: PendingMetadataRefreshRequest,
    request: Request,
) -> PendingMetadataRefreshResponse:
    return pending_metadata_response(_settings(request), payload)


def api_update_targets(request: Request) -> UpdateTargetsResponse:
    return update_targets_response(_settings(request))


def api_pending_cleanup(
    payload: PendingCleanupRequest,
    request: Request,
) -> PendingCleanupResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    _require_file_pending_source(settings, operation="cleanup")

    payload_lines = _cleanup_payload_lines(payload)
    wud_lock = web_jobs._acquire_apply_wud_lock(settings)
    try:
        try:
            parsed = parse_wud_file(settings.config.wud_out_file)
            cleanup = build_unmatched_cleanup(
                _effective_config(settings),
                line_numbers=[line.line_no for line in payload_lines],
                parsed=parsed,
                host_docker_base=settings.host_docker_base,
                environ=settings.command_env,
            )
        except (PlanInputError, PlanFileMissing) as exc:
            raise HTTPException(status_code=409, detail="cleanup is stale") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not revalidate cleanup",
                    exc,
                ),
            ) from exc

        removed = _validated_cleanup_lines(payload, payload_lines, cleanup)
        try:
            with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
                init_db(conn)
                with _immediate_transaction(conn):
                    audit_run_id = _insert_pending_cleanup_audit(
                        conn,
                        settings,
                        request,
                        removed,
                    )
                    try:
                        remove_lines_before_run(
                            settings.config.wud_out_file,
                            parsed,
                            [item.line_no for item in removed],
                            lock=wud_lock,
                            owner=_owner_config(settings),
                        )
                    except OSError as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=_safe_exception_detail(
                                settings,
                                "could not remove pending lines",
                                exc,
                            ),
                        ) from exc
        except HTTPException:
            raise
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not record cleanup audit",
                    exc,
                ),
            ) from exc

        return PendingCleanupResponse(
            status="success",
            audit_run_id=audit_run_id,
            removed_count=len(removed),
            removed=[
                PendingCleanupRemovedLine(
                    line_no=item.line_no,
                    raw=item.raw,
                    image=item.image,
                    reason=item.reason,
                )
                for item in removed
            ],
        )
    finally:
        wud_lock.close()


def api_pending_removal_plan(
    payload: PendingRemovalPlanRequest,
    request: Request,
) -> PendingRemovalPlanResponse:
    settings = _settings(request)
    _require_file_pending_source(settings, operation="removal")
    try:
        parsed = parse_wud_file(settings.config.wud_out_file)
        return pending_removal_plan(settings, payload.line_numbers, parsed=parsed)
    except PlanInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="WUD file not found") from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not create removal plan",
                exc,
            ),
        ) from exc


def api_pending_removal(
    payload: PendingRemovalRequest,
    request: Request,
) -> PendingCleanupResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    _require_file_pending_source(settings, operation="removal")

    payload_lines = _removal_payload_lines(payload)
    wud_lock = web_jobs._acquire_apply_wud_lock(settings)
    try:
        try:
            parsed = parse_wud_file(settings.config.wud_out_file)
            plan = pending_removal_plan(
                settings,
                [line.line_no for line in payload_lines],
                parsed=parsed,
            )
        except (PlanInputError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail="removal is stale") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not revalidate removal",
                    exc,
                ),
            ) from exc

        removed = _validated_removal_lines(payload, payload_lines, plan)
        try:
            with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
                init_db(conn)
                with _immediate_transaction(conn):
                    audit_run_id = _insert_pending_removal_audit(
                        conn,
                        settings,
                        request,
                        removed,
                    )
                    try:
                        remove_lines_before_run(
                            settings.config.wud_out_file,
                            parsed,
                            [item.line_no for item in removed],
                            lock=wud_lock,
                            owner=_owner_config(settings),
                        )
                    except OSError as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=_safe_exception_detail(
                                settings,
                                "could not remove pending lines",
                                exc,
                            ),
                        ) from exc
        except HTTPException:
            raise
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not record removal audit",
                    exc,
                ),
            ) from exc

        return PendingCleanupResponse(
            status="success",
            audit_run_id=audit_run_id,
            removed_count=len(removed),
            removed=[
                PendingCleanupRemovedLine(
                    line_no=item.line_no,
                    raw=item.raw,
                    image=item.image,
                    reason="selected",
                )
                for item in removed
            ],
        )
    finally:
        wud_lock.close()


def pending_response(
    settings: WebSettings,
    *,
    include_grouping: bool = True,
    include_wud_metadata: bool = True,
    force_api: bool = False,
) -> PendingResponse:
    response, _snapshot = pending_response_with_snapshot(
        settings,
        include_grouping=include_grouping,
        include_wud_metadata=include_wud_metadata,
        force_api=force_api,
    )
    return response


def pending_response_with_snapshot(
    settings: WebSettings,
    *,
    include_grouping: bool = True,
    include_wud_metadata: bool = True,
    force_api: bool = False,
) -> tuple[PendingResponse, web_wud_api.WudApiSnapshot | None]:
    try:
        source = web_wud_refresh.refresh_wud_pending_source(
            settings,
            include_wud_metadata=include_wud_metadata,
            force=force_api,
        ).source
        completed_update_selections = (
            web_file_selection_store.load_completed_update_selections(
                settings.config.db_path,
                pending_file=settings.config.wud_out_file,
                pending_target_keys={
                    pending_target_key(target.raw)
                    for target in source.parsed.targets
                },
            )
            if source.active == "file"
            else ()
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read pending source",
                exc,
            ),
        ) from exc
    parsed = source.parsed
    wud_metadata = dict(source.metadata_by_line or {})
    wud_metadata_by_line = web_wud_api.metadata_response_by_line(wud_metadata)
    source_ids_by_line = dict(source.source_ids_by_line or {})
    metadata_status_by_line = dict(source.metadata_status_by_line or {})
    grouping = (
        _pending_grouping_response(
            settings,
            parsed,
            wud_metadata_by_line=wud_metadata_by_line,
            source=source.active,
            source_ids_by_line=source_ids_by_line,
            metadata_status_by_line=metadata_status_by_line,
            completed_update_selections=completed_update_selections,
        )
        if include_grouping
        else PendingGrouping(status="unavailable")
    )
    snoozed_candidates = (
        web_pending_snoozes.pending_snoozed_candidates(
            settings,
            source,
            _effective_config(settings),
        )
        if include_grouping
        else []
    )
    provenance_by_line = _pending_grouping_provenance_by_line(grouping)
    items = [
        PendingItem(
            line_no=target.line_no,
            raw=target.raw,
            image=target.first,
            key=target.key,
            repo=target.repo,
            current_tag=image_tag(target.first),
            has_tag=target.has_tag,
            allow_repo=target.allow_repo,
            digest=target.digest,
            desired_tag=target.desired_tag,
            platform=target.platform_value,
            platform_os=target.platform.os if target.platform is not None else "",
            platform_architecture=(
                target.platform.architecture if target.platform is not None else ""
            ),
            platform_variant=(
                target.platform.variant if target.platform is not None else ""
            ),
            digest_provenance=provenance_by_line.get(target.line_no),
            wud_metadata=wud_metadata_by_line.get(target.line_no),
            source=source.active,
            source_id=source_ids_by_line.get(target.line_no, ""),
            tag_stream=_pending_tag_stream(
                target.repo,
                image_tag(target.first),
                target.desired_tag,
            ),
            metadata_status=metadata_status_by_line.get(target.line_no, "fresh"),
        )
        for target in parsed.targets
    ]
    return (
        PendingResponse(
            source_file=source.source_file,
            source=source.response_source(),
            source_hash=source.source_hash,
            exists=source.exists,
            count=len(items),
            items=items,
            grouping=grouping,
            snoozed_candidates=snoozed_candidates,
            wud_api=_wud_api_status(source.wud_snapshot),
            warnings=list(source.warnings),
        ),
        source.wud_snapshot,
    )


def pending_metadata_response(
    settings: WebSettings,
    payload: PendingMetadataRefreshRequest,
) -> PendingMetadataRefreshResponse:
    try:
        source = web_wud_refresh.refresh_wud_pending_source(
            settings,
            include_wud_metadata=True,
            force=False,
        ).source
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read pending source",
                exc,
            ),
        ) from exc

    if source.source_hash != payload.source_hash:
        return _stale_pending_metadata_response(source)

    targets_by_line = {target.line_no: target for target in source.parsed.targets}
    source_ids_by_line = dict(source.source_ids_by_line or {})
    metadata_status_by_line = dict(source.metadata_status_by_line or {})
    metadata_by_line = web_wud_api.metadata_response_by_line(
        dict(source.metadata_by_line or {})
    )
    items: list[PendingMetadataRefreshItem] = []
    for line in payload.lines:
        target = targets_by_line.get(line.line_no)
        if (
            target is None
            or target.raw != line.raw
            or source_ids_by_line.get(line.line_no, "") != line.source_id
        ):
            return _stale_pending_metadata_response(source)
        items.append(
            PendingMetadataRefreshItem(
                line_no=line.line_no,
                raw=target.raw,
                source_id=source_ids_by_line.get(line.line_no, ""),
                wud_metadata=metadata_by_line.get(line.line_no),
                metadata_status=metadata_status_by_line.get(line.line_no, "fresh"),
            )
        )

    return PendingMetadataRefreshResponse(
        status="ready",
        requires_pending_reload=False,
        source_hash=source.source_hash,
        source=source.response_source(),
        wud_api=_wud_api_status(source.wud_snapshot),
        items=items,
    )


def _stale_pending_metadata_response(
    source: web_pending_sources.PendingSourceResult,
) -> PendingMetadataRefreshResponse:
    return PendingMetadataRefreshResponse(
        status="stale",
        requires_pending_reload=True,
        source_hash=source.source_hash,
        source=source.response_source(),
        wud_api=_wud_api_status(source.wud_snapshot),
        items=[],
    )


def update_targets_response(settings: WebSettings) -> UpdateTargetsResponse:
    config = _effective_config(settings)
    runner = (
        CommandRunner(env=settings.command_env)
        if settings.command_env is not None
        else CommandRunner()
    )
    compose = ComposeCli(runner=runner)
    try:
        stacks = compose.discover_stacks(
            config.docker_base,
            project_base=settings.host_docker_base,
            ignore_paths=config.compose_ignore_paths,
        )
    except ComposeDiscoveryError as exc:
        return UpdateTargetsResponse(
            status="unavailable",
            count=0,
            warnings=[str(exc)],
        )

    items: list[UpdateTargetItem] = []
    for stack in stacks:
        project_directory = (
            "" if stack.project_directory is None else str(stack.project_directory)
        )
        for pair in stack.service_images:
            items.append(
                UpdateTargetItem(
                    service_key=f"{stack.name}/{pair.service}",
                    stack=stack.name,
                    service=pair.service,
                    image=pair.image,
                    image_repo=repo_key(pair.image),
                    current_tag=image_tag(pair.image),
                    directory=str(stack.directory),
                    compose_file=stack.file,
                    project_directory=project_directory,
                )
            )

    return UpdateTargetsResponse(
        status="ready",
        count=len(items),
        items=items,
        warnings=[],
    )


def pending_removal_plan(
    settings: WebSettings,
    line_numbers: Sequence[int],
    *,
    parsed: ParsedWudFile,
) -> PendingRemovalPlanResponse:
    selected = _selected_removal_line_numbers(line_numbers)
    targets_by_line = {target.line_no: target for target in parsed.targets}
    missing = [line_no for line_no in selected if line_no not in targets_by_line]
    if missing:
        raise PlanInputError(
            "line_numbers include non-pending line(s): "
            + ", ".join(str(line_no) for line_no in missing)
        )

    lines = [
        PendingRemovalPlanLine(
            line_no=target.line_no,
            raw=target.raw,
            image=target.first,
            desired_tag=target.desired_tag,
            digest=target.digest,
        )
        for target in (targets_by_line[line_no] for line_no in selected)
    ]
    return PendingRemovalPlanResponse(
        removal_id=_pending_removal_id(settings, lines),
        source_file=str(settings.config.wud_out_file),
        can_remove=settings.mutations_enabled and bool(lines),
        selected_line_numbers=list(selected),
        lines=lines,
    )


def parse_pending_file(settings: WebSettings) -> tuple[bool, ParsedWudFile]:
    path = settings.config.wud_out_file
    try:
        return True, parse_wud_file(path)
    except FileNotFoundError:
        return False, ParsedWudFile(lines=(), targets=(), warnings=())
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read WUD file", exc),
        ) from exc


def _require_file_pending_source(settings: WebSettings, *, operation: str) -> None:
    try:
        source = web_pending_sources.resolve_pending_source(settings)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                f"could not verify pending {operation} source",
                exc,
            ),
        ) from exc
    if source.active == "file":
        return
    raise HTTPException(
        status_code=409,
        detail=f"pending {operation} only supports WUD_OUT_FILE source",
    )


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


def _pending_grouping_response(
    settings: WebSettings,
    parsed: ParsedWudFile,
    *,
    wud_metadata_by_line: dict[int, Any],
    source: str,
    source_ids_by_line: dict[int, str],
    metadata_status_by_line: dict[int, PendingMetadataStatus],
    completed_update_selections: Sequence[CompletedUpdateSelection],
) -> PendingGrouping:
    grouping = resolve_pending_groups(
        _effective_config(settings),
        parsed,
        host_docker_base=settings.host_docker_base,
        environ=settings.command_env,
        known_digest_provenance_by_service=(
            web_database.known_digest_provenance_by_service(settings)
        ),
        completed_update_selections=completed_update_selections,
    )
    runtime_service_states = _pending_runtime_service_states(settings)
    return PendingGrouping(
        status=grouping.status,
        groups=[
            PendingStackGroup(
                name=group.name,
                directory=group.directory,
                compose_file=group.compose_file,
                project_directory=group.project_directory,
                project_name=group.project_name,
                services_label=group.services_label,
                services=list(group.services),
                line_numbers=list(group.line_numbers),
                items=[
                    _pending_grouped_item(
                        item,
                        wud_metadata_by_line,
                        source=source,
                        source_ids_by_line=source_ids_by_line,
                        metadata_status_by_line=metadata_status_by_line,
                        runtime=_pending_item_runtime(group, item, runtime_service_states),
                    )
                    for item in group.items
                ],
            )
            for group in grouping.groups
        ],
        unmatched=[
            _pending_grouped_item(
                item,
                wud_metadata_by_line,
                source=source,
                source_ids_by_line=source_ids_by_line,
                metadata_status_by_line=metadata_status_by_line,
                runtime=("unknown", (), ()),
            )
            for item in grouping.unmatched
        ],
        warnings=list(grouping.warnings),
    )


def _pending_grouped_item(
    item: Any,
    wud_metadata_by_line: dict[int, Any],
    *,
    source: str,
    source_ids_by_line: dict[int, str],
    metadata_status_by_line: dict[int, PendingMetadataStatus],
    runtime: tuple[str, tuple[str, ...], tuple[str, ...]],
) -> PendingGroupedItem:
    runtime_state, running_services, stopped_services = runtime
    return PendingGroupedItem(
        line_no=item.line_no,
        raw=item.raw,
        image=item.image,
        key=item.key,
        repo=item.repo,
        current_tag=image_tag(item.image),
        has_tag=item.has_tag,
        allow_repo=item.allow_repo,
        digest=item.digest,
        desired_tag=item.desired_tag,
        platform=item.platform,
        platform_os=item.platform_os,
        platform_architecture=item.platform_architecture,
        platform_variant=item.platform_variant,
        resolved_image=item.resolved_image,
        target_image=item.target_image,
        compose_images=list(item.compose_images),
        services=list(item.services),
        action=item.action,
        selection_id=item.selection_id,
        diagnostic=(
            None
            if item.diagnostic is None
            else PendingDiagnostic.model_validate(asdict(item.diagnostic))
        ),
        runtime_state=runtime_state,
        running_services=list(running_services),
        stopped_services=list(stopped_services),
        digest_provenance=(
            None
            if item.digest_provenance is None
            else asdict(item.digest_provenance)
        ),
        wud_metadata=wud_metadata_by_line.get(item.line_no),
        source=source,
        source_id=source_ids_by_line.get(item.line_no, ""),
        tag_stream=(
            None
            if item.tag_stream is None
            else PendingTagStream(
                current_stream=item.tag_stream.current_stream,
                reported_stream=item.tag_stream.reported_stream,
            )
        ),
        metadata_status=metadata_status_by_line.get(item.line_no, "fresh"),
    )


def _pending_runtime_service_states(
    settings: WebSettings,
) -> tuple[ComposeRuntimeServiceState, ...] | None:
    runner = CommandRunner(env=settings.command_env)
    try:
        rows = DockerCli(runner=runner).ps_format(
            COMPOSE_RUNTIME_STATE_FORMAT,
            all_containers=True,
            timeout_seconds=_PENDING_DOCKER_TIMEOUT_SECONDS,
        )
    except CommandError:
        return None
    return compose_runtime_service_states(rows)


def _pending_item_runtime(
    group: Any,
    item: Any,
    runtime_service_states: tuple[ComposeRuntimeServiceState, ...] | None,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    services = tuple(item.runtime_services)
    if runtime_service_states is None or not services or not group.project_name:
        return "unknown", (), ()

    project_directory = group.project_directory or group.directory
    running_services: list[str] = []
    stopped_services: list[str] = []
    for service in services:
        expected_paths, expected_project, expected_service = compose_runtime_service_key(
            project_directory,
            group.compose_file,
            group.project_name,
            service,
        )
        expected = expected_paths, expected_project, expected_service
        service_states = tuple(
            state
            for key, state in runtime_service_states
            if compose_runtime_service_key_matches(expected, key)
        )
        if len(service_states) > 1:
            return "unknown", (), ()
        states = set(service_states)
        if states == {"running"}:
            running_services.append(service)
        elif not states or states <= {"created", "dead", "exited"}:
            stopped_services.append(service)
        else:
            return "unknown", (), ()
    running = tuple(running_services)
    stopped = tuple(stopped_services)
    if not stopped:
        runtime_state = "running"
    elif not running:
        runtime_state = "not-running"
    else:
        runtime_state = "mixed"
    return runtime_state, running, stopped


def _pending_tag_stream(
    image_repo: str,
    current_tag: str,
    reported_tag: str,
) -> PendingTagStream | None:
    hint = pending_tag_stream_hint(
        image_repo=image_repo,
        current_tag=current_tag,
        reported_tag=reported_tag,
    )
    if hint is None:
        return None
    return PendingTagStream(
        current_stream=hint.current_stream,
        reported_stream=hint.reported_stream,
    )


def _pending_grouping_provenance_by_line(
    grouping: PendingGrouping,
) -> dict[int, Any]:
    by_line: dict[int, Any] = {}
    for group in grouping.groups:
        for item in group.items:
            if item.digest_provenance is not None:
                by_line[item.line_no] = item.digest_provenance
    for item in grouping.unmatched:
        if item.digest_provenance is not None:
            by_line[item.line_no] = item.digest_provenance
    return by_line


def _wud_api_status(
    snapshot: web_wud_api.WudApiSnapshot | None,
) -> WudApiStatus:
    if snapshot is not None:
        return snapshot.status
    return WudApiStatus(
        state="unavailable",
        available=False,
        metadata_available=False,
        last_checked_at="",
    )


def _cleanup_payload_lines(
    payload: PendingCleanupRequest,
) -> tuple[PendingCleanupLine, ...]:
    seen: set[int] = set()
    lines: list[PendingCleanupLine] = []
    for line in payload.lines:
        if line.line_no in seen:
            raise HTTPException(
                status_code=422,
                detail=f"cleanup line {line.line_no} was provided more than once",
            )
        if not line.raw:
            raise HTTPException(
                status_code=422,
                detail=f"cleanup line {line.line_no} raw value is required",
            )
        seen.add(line.line_no)
        lines.append(line)
    return tuple(lines)


def _removal_payload_lines(
    payload: PendingRemovalRequest,
) -> tuple[PendingCleanupLine, ...]:
    seen: set[int] = set()
    lines: list[PendingCleanupLine] = []
    for line in payload.lines:
        if line.line_no in seen:
            raise HTTPException(
                status_code=422,
                detail=f"removal line {line.line_no} was provided more than once",
            )
        if not line.raw:
            raise HTTPException(
                status_code=422,
                detail=f"removal line {line.line_no} raw value is required",
            )
        seen.add(line.line_no)
        lines.append(line)
    return tuple(lines)


def _validated_cleanup_lines(
    payload: PendingCleanupRequest,
    payload_lines: Sequence[PendingCleanupLine],
    cleanup: DryRunPlanCleanup,
) -> tuple[DryRunPlanCleanupItem, ...]:
    if not cleanup.items or not cleanup.cleanup_id:
        raise HTTPException(status_code=409, detail="cleanup is stale")
    if not secrets.compare_digest(cleanup.cleanup_id, payload.cleanup_id):
        raise HTTPException(status_code=409, detail="cleanup is stale")

    requested = {(line.line_no, line.raw) for line in payload_lines}
    available = {(item.line_no, item.raw): item for item in cleanup.items}
    if requested != set(available):
        raise HTTPException(status_code=409, detail="cleanup is stale")
    return tuple(available[key] for key in sorted(available))


def _selected_removal_line_numbers(line_numbers: Sequence[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    selected: list[int] = []
    for line_no in line_numbers:
        if line_no in seen:
            raise PlanInputError(
                f"line_numbers line {line_no} was provided more than once"
            )
        seen.add(line_no)
        selected.append(line_no)
    return tuple(sorted(selected))


def _pending_removal_id(
    settings: WebSettings,
    lines: Sequence[PendingRemovalPlanLine],
) -> str:
    payload = {
        "version": 1,
        "source_file": str(settings.config.wud_out_file),
        "lines": [
            {
                "line_no": item.line_no,
                "raw": item.raw,
                "image": item.image,
                "desired_tag": item.desired_tag,
                "digest": item.digest,
            }
            for item in lines
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_removal_lines(
    payload: PendingRemovalRequest,
    payload_lines: Sequence[PendingCleanupLine],
    plan: PendingRemovalPlanResponse,
) -> tuple[PendingRemovalPlanLine, ...]:
    if not plan.lines or not plan.removal_id:
        raise HTTPException(status_code=409, detail="removal is stale")
    if not secrets.compare_digest(plan.removal_id, payload.removal_id):
        raise HTTPException(status_code=409, detail="removal is stale")

    requested = {(line.line_no, line.raw) for line in payload_lines}
    available = {(item.line_no, item.raw): item for item in plan.lines}
    if requested != set(available):
        raise HTTPException(status_code=409, detail="removal is stale")
    return tuple(available[key] for key in sorted(available))


def _owner_config(settings: WebSettings) -> OwnerConfig:
    return OwnerConfig(
        uid=settings.config.out_uid,
        gid=settings.config.out_gid,
    )


def _insert_pending_cleanup_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    removed: Sequence[DryRunPlanCleanupItem],
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": "webui",
        "operation": "remove_unmatched_pending",
        "actor_type": _request_actor_type(settings, request),
        "line_numbers": [item.line_no for item in removed],
    }
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
        VALUES (?, ?, 'success', 0, 'web-pending-cleanup', ?, '', ?)
        """,
        (
            now,
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    for item in removed:
        item_metadata = {
            "source": "webui",
            "operation": "remove_unmatched_pending",
            "reason": item.reason,
            "diagnostic": (
                None if item.diagnostic is None else asdict(item.diagnostic)
            ),
        }
        conn.execute(
            """
            INSERT INTO pending_updates (
                run_id,
                line_no,
                raw,
                image,
                target_digest,
                desired_tag,
                service_key,
                stack_name,
                service_name,
                status,
                status_reason,
                created_at,
                updated_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 'resolved', 'removed-unmatched', ?, ?, ?)
            """,
            (
                run_id,
                item.line_no,
                item.raw,
                item.image,
                item.digest,
                item.desired_tag,
                "" if item.diagnostic is None else item.diagnostic.stack,
                "" if item.diagnostic is None else item.diagnostic.service,
                now,
                now,
                _json_object(item_metadata),
            ),
        )
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
            VALUES (?, ?, ?, ?, ?, '', 'success', ?)
            """,
            (
                run_id,
                now,
                (
                    item.diagnostic.service
                    if item.diagnostic is not None and item.diagnostic.service
                    else item.image
                ),
                "" if item.diagnostic is None else item.diagnostic.stack,
                item.image,
                _json_object(item_metadata),
            ),
        )
    return run_id


def _insert_pending_removal_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    removed: Sequence[PendingRemovalPlanLine],
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": "webui",
        "operation": "remove_selected_pending",
        "actor_type": _request_actor_type(settings, request),
        "line_numbers": [item.line_no for item in removed],
    }
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
        VALUES (?, ?, 'success', 0, 'web-pending-removal', ?, '', ?)
        """,
        (
            now,
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    for item in removed:
        item_metadata = {
            "source": "webui",
            "operation": "remove_selected_pending",
            "reason": "selected",
        }
        conn.execute(
            """
            INSERT INTO pending_updates (
                run_id,
                line_no,
                raw,
                image,
                target_digest,
                desired_tag,
                service_key,
                stack_name,
                service_name,
                status,
                status_reason,
                created_at,
                updated_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, '', '', '', 'resolved', 'removed-selected', ?, ?, ?)
            """,
            (
                run_id,
                item.line_no,
                item.raw,
                item.image,
                item.digest,
                item.desired_tag,
                now,
                now,
                _json_object(item_metadata),
            ),
        )
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
            VALUES (?, ?, ?, '', ?, '', 'success', ?)
            """,
            (
                run_id,
                now,
                item.image,
                item.image,
                _json_object(item_metadata),
            ),
        )
    return run_id
