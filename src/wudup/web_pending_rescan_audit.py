"""Audit helpers for WebUI WUD rescan requests."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from fastapi import Request

from .db import DatabaseError, init_db, open_db, utc_timestamp
from .web_auth import _immediate_transaction, _request_actor_type
from .web_metadata import json_object as _json_object
from .web_models import (
    PendingRescanResponse,
    PendingRescanSkippedLine,
    WebSettings,
    WudApiStatus,
)

LOGGER = logging.getLogger(__name__)


def insert_pending_rescan_audit_start(
    settings: WebSettings,
    request: Request,
    *,
    scope: str,
    requested_count: int,
    line_numbers: Sequence[int],
) -> int:
    now = utc_timestamp()
    metadata = _pending_rescan_audit_metadata(
        settings,
        request,
        scope=scope,
        status="running",
        requested_count=requested_count,
        watched_count=0,
        line_numbers=line_numbers,
    )
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with _immediate_transaction(conn):
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
                VALUES (?, NULL, 'running', 0, 'web-wud-rescan', ?, '', ?)
                """,
                (
                    now,
                    str(settings.config.wud_out_file),
                    _json_object(metadata),
                ),
            )
            return int(cursor.lastrowid)


def safe_update_pending_rescan_audit_response(
    settings: WebSettings,
    request: Request,
    run_id: int,
    *,
    response: PendingRescanResponse,
    line_numbers: Sequence[int],
) -> None:
    try:
        _update_pending_rescan_audit_response(
            settings,
            request,
            run_id,
            response=response,
            line_numbers=line_numbers,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to update WebUI WUD rescan audit")


def safe_update_pending_rescan_audit_error(
    settings: WebSettings,
    request: Request,
    run_id: int,
    *,
    scope: str,
    requested_count: int,
    line_numbers: Sequence[int],
    error: str,
) -> None:
    try:
        _update_pending_rescan_audit_error(
            settings,
            request,
            run_id,
            scope=scope,
            requested_count=requested_count,
            line_numbers=line_numbers,
            error=error,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to update WebUI WUD rescan audit")


def _update_pending_rescan_audit_response(
    settings: WebSettings,
    request: Request,
    run_id: int,
    *,
    response: PendingRescanResponse,
    line_numbers: Sequence[int],
) -> None:
    metadata = _pending_rescan_audit_metadata(
        settings,
        request,
        scope=response.scope,
        status=response.status,
        requested_count=response.requested_count,
        watched_count=response.watched_count,
        line_numbers=line_numbers,
        skipped=response.skipped,
        wud_api=response.wud_api,
    )
    _update_pending_rescan_audit(
        settings,
        run_id,
        run_status="failure" if response.status == "blocked" else "success",
        metadata=metadata,
    )


def _update_pending_rescan_audit_error(
    settings: WebSettings,
    request: Request,
    run_id: int,
    *,
    scope: str,
    requested_count: int,
    line_numbers: Sequence[int],
    error: str,
) -> None:
    metadata = _pending_rescan_audit_metadata(
        settings,
        request,
        scope=scope,
        status="failure",
        requested_count=requested_count,
        watched_count=0,
        line_numbers=line_numbers,
        error=error,
    )
    _update_pending_rescan_audit(
        settings,
        run_id,
        run_status="failure",
        metadata=metadata,
    )


def _update_pending_rescan_audit(
    settings: WebSettings,
    run_id: int,
    *,
    run_status: str,
    metadata: dict[str, Any],
) -> None:
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                UPDATE update_runs
                SET finished_at = ?,
                    status = ?,
                    metadata_json = ?
                WHERE id = ?
                  AND mode = 'web-wud-rescan'
                """,
                (utc_timestamp(), run_status, _json_object(metadata), run_id),
            )


def _pending_rescan_audit_metadata(
    settings: WebSettings,
    request: Request,
    *,
    scope: str,
    status: str,
    requested_count: int,
    watched_count: int,
    line_numbers: Sequence[int],
    skipped: Sequence[PendingRescanSkippedLine] = (),
    wud_api: WudApiStatus | None = None,
    error: str = "",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "webui",
        "operation": "rescan_wud",
        "actor_type": _request_actor_type(settings, request),
        "scope": scope,
        "status": status,
        "requested_count": requested_count,
        "watched_count": watched_count,
        "line_numbers": list(line_numbers),
        "skipped": [item.model_dump(mode="json") for item in skipped],
    }
    if wud_api is not None:
        metadata["wud_api"] = wud_api.model_dump(mode="json")
    if error:
        metadata["error"] = error
    return metadata
