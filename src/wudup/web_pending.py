"""WebUI pending-update and update-target route handlers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Protocol

from fastapi import HTTPException, Request

from . import (
    web_database,
    web_file_selection_store,
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
from .docker_cli import DockerCli
from .images import image_tag, repo_key
from .plan_matching import pending_target_key
from .plans import (
    resolve_pending_groups,
)
from .tag_streams import pending_tag_stream_hint
from .updater_models import CompletedUpdateSelection
from .web_models import (
    PendingDiagnostic,
    PendingGroupedItem,
    PendingGrouping,
    PendingItem,
    PendingMetadataRefreshItem,
    PendingMetadataRefreshRequest,
    PendingMetadataRefreshResponse,
    PendingMetadataStatus,
    PendingResponse,
    PendingStackGroup,
    PendingTagStream,
    UpdateTargetItem,
    UpdateTargetsResponse,
    WebSettings,
    WudApiStatus,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_request_context import request_settings as _settings
from .wud_file import (
    ParsedWudFile,
    parse_wud_file,
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
