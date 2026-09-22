"""WebUI pending WUD rescan route handler."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from fastapi import HTTPException, Request

from . import (
    web_jobs,
    web_pending_rescan_audit,
    web_pending_rescan_payload,
    web_pending_sources,
    web_wud_api,
    web_wud_refresh,
)
from .db import DatabaseError
from .web_models import (
    PendingRescanLine,
    PendingRescanRequest,
    PendingRescanResponse,
    PendingRescanSkippedLine,
    PendingRescanStatus,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_request_context import request_settings as _settings


def api_pending_rescan(
    payload: PendingRescanRequest,
    request: Request,
) -> PendingRescanResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)

    if payload.scope == "selected" and not payload.lines:
        raise HTTPException(
            status_code=422,
            detail="selected rescan lines are required",
        )

    wud_lock = web_jobs._acquire_apply_wud_lock(settings)
    try:
        if payload.scope == "all":
            audit_line_numbers: tuple[int, ...] = ()
            requested_count = 1
            selected_lines: tuple[PendingRescanLine, ...] = ()
        else:
            selected_lines = web_pending_rescan_payload.rescan_payload_lines(payload)
            audit_line_numbers = tuple(line.line_no for line in selected_lines)
            requested_count = len(selected_lines)

        try:
            audit_run_id = (
                web_pending_rescan_audit.insert_pending_rescan_audit_start(
                    settings,
                    request,
                    scope=payload.scope,
                    requested_count=requested_count,
                    line_numbers=audit_line_numbers,
                )
            )
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not record WUD rescan audit",
                    exc,
                ),
            ) from exc

        try:
            if payload.scope == "all":
                response = _pending_rescan_all(settings)
            else:
                response = _pending_rescan_selected(settings, selected_lines)
        except HTTPException as exc:
            web_pending_rescan_audit.safe_update_pending_rescan_audit_error(
                settings,
                request,
                audit_run_id,
                scope=payload.scope,
                requested_count=requested_count,
                line_numbers=audit_line_numbers,
                error=str(exc.detail),
            )
            raise
        except Exception as exc:
            web_pending_rescan_audit.safe_update_pending_rescan_audit_error(
                settings,
                request,
                audit_run_id,
                scope=payload.scope,
                requested_count=requested_count,
                line_numbers=audit_line_numbers,
                error=_safe_exception_detail(settings, "WUD rescan failed", exc),
            )
            raise

        web_pending_rescan_audit.safe_update_pending_rescan_audit_response(
            settings,
            request,
            audit_run_id,
            response=response,
            line_numbers=audit_line_numbers,
        )
        return response.model_copy(update={"audit_run_id": audit_run_id})
    finally:
        wud_lock.close()


def _pending_rescan_all(settings: WebSettings) -> PendingRescanResponse:
    result = web_wud_api.watch_all(settings)
    return PendingRescanResponse(
        status=_pending_rescan_status(
            result,
            skipped=(),
        ),
        audit_run_id=0,
        scope="all",
        requested_count=result.requested_count,
        watched_count=result.watched_count,
        skipped=[],
        wud_api=result.snapshot.status,
    )


def _pending_rescan_selected(
    settings: WebSettings,
    selected: Sequence[PendingRescanLine],
) -> PendingRescanResponse:
    try:
        source = web_wud_refresh.refresh_wud_pending_source(
            settings,
            include_wud_metadata=False,
            force=True,
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

    _validate_selected_rescan_source(source, selected)
    snapshot = source.wud_snapshot or web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    if not snapshot.status.metadata_available:
        return PendingRescanResponse(
            status="blocked",
            audit_run_id=0,
            scope="selected",
            requested_count=len(selected),
            watched_count=0,
            skipped=[],
            wud_api=snapshot.status,
        )

    targets_by_line = {target.line_no: target for target in source.parsed.targets}
    container_ids_by_line = _selected_rescan_container_ids_by_line(
        settings,
        source,
        snapshot,
    )
    source_ids_by_line = dict(source.source_ids_by_line or {})
    skipped: list[PendingRescanSkippedLine] = []
    container_ids: list[str] = []
    seen_container_ids: set[str] = set()
    for line in selected:
        line_no = line.line_no
        target = targets_by_line.get(line_no)
        if target is None:
            raise HTTPException(status_code=409, detail="selected rescan is stale")
        if target.raw != line.raw:
            raise HTTPException(status_code=409, detail="selected rescan is stale")
        if source_ids_by_line.get(line_no, "") != line.source_id:
            raise HTTPException(status_code=409, detail="selected rescan is stale")
        if not line.container_id:
            skipped.append(
                PendingRescanSkippedLine(
                    line_no=line_no,
                    raw=target.raw,
                    reason="no-wud-container-id",
                )
            )
            continue
        line_container_ids = container_ids_by_line.get(line_no, ())
        if line.container_id not in line_container_ids:
            raise HTTPException(status_code=409, detail="selected rescan is stale")
        for container_id in line_container_ids:
            if container_id not in seen_container_ids:
                seen_container_ids.add(container_id)
                container_ids.append(container_id)

    if not container_ids:
        return PendingRescanResponse(
            status="blocked",
            audit_run_id=0,
            scope="selected",
            requested_count=len(selected),
            watched_count=0,
            skipped=skipped,
            wud_api=snapshot.status,
        )

    result = web_wud_api.watch_containers(settings, container_ids)
    return PendingRescanResponse(
        status=_pending_rescan_status(
            result,
            skipped=skipped,
        ),
        audit_run_id=0,
        scope="selected",
        requested_count=len(selected),
        watched_count=result.watched_count,
        skipped=skipped,
        wud_api=result.snapshot.status,
    )


def _selected_rescan_container_ids_by_line(
    settings: WebSettings,
    source: web_pending_sources.PendingSourceResult,
    snapshot: web_wud_api.WudApiSnapshot,
) -> dict[int, tuple[str, ...]]:
    if source.container_ids_by_line:
        return {
            line_no: tuple(container_id for container_id in container_ids if container_id)
            for line_no, container_ids in source.container_ids_by_line.items()
        }
    metadata_by_line = web_wud_api.metadata_by_target(
        settings,
        source.parsed.targets,
        snapshot=snapshot,
    )
    return {
        line_no: (container.id,)
        for line_no, container in metadata_by_line.items()
        if container.id
    }


def _pending_rescan_status(
    result: web_wud_api.WudApiWatchResult,
    *,
    skipped: Sequence[PendingRescanSkippedLine],
) -> PendingRescanStatus:
    if result.snapshot.status.state != "ready":
        if result.watched_count > 0:
            return "partial"
        return "blocked"
    if not result.watched and result.watched_count == 0:
        return "blocked"
    if not result.watched:
        return "partial"
    if result.watched_count < result.requested_count:
        return "partial"
    if result.remaining_degraded_container_ids:
        return "partial"
    if skipped:
        return "partial"
    return "success"


def _validate_selected_rescan_source(
    source: web_pending_sources.PendingSourceResult,
    selected: Sequence[PendingRescanLine],
) -> None:
    source_hashes = {line.source_hash for line in selected}
    if len(source_hashes) != 1 or source_hashes != {source.source_hash}:
        raise HTTPException(status_code=409, detail="selected rescan is stale")
