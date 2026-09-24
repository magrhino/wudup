"""WebUI run history and log-tail route handlers."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query, Request

from .config import VALID_UPDATE_MODES
from .db import DatabaseError
from .digest_provenance import digest_provenance_from_row
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_models import (
    DigestTagProvenance,
    LogTail,
    PendingUpdateRecord,
    RunDetail,
    RunEventRecord,
    RunHistorySummary,
    RunLogResponse,
    RunSummary,
    RunVerificationSummary,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_redaction import (
    sanitize_support_bundle_value as _sanitize_support_bundle_value,
)
from .web_request_context import request_settings as _settings
from .web_run_verification import verification_from_run_records

DEFAULT_RUN_LIMIT = 50
DEFAULT_LOG_TAIL_BYTES = 262_144
MAX_LOG_TAIL_BYTES = 1_048_576


def api_runs(request: Request) -> list[RunHistorySummary]:
    settings = _settings(request)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM update_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (DEFAULT_RUN_LIMIT,),
            ).fetchall()

            run_ids = [row["id"] for row in rows]
            events_by_run: dict[int, list[RunEventRecord]] = {}
            pending_by_run: dict[int, list[PendingUpdateRecord]] = {}
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                event_rows = conn.execute(
                    f"""
                    SELECT *
                    FROM update_events
                    WHERE run_id IN ({placeholders})
                    ORDER BY id
                    """,
                    tuple(run_ids),
                ).fetchall()
                for e in event_rows:
                    event = _event_from_row(e)
                    events_by_run.setdefault(event.run_id, []).append(event)
                pending_rows = conn.execute(
                    f"""
                    SELECT * FROM pending_updates
                    WHERE run_id IN ({placeholders})
                    ORDER BY line_no, id
                    """,
                    tuple(run_ids),
                ).fetchall()
                for pending_row in pending_rows:
                    pending = _pending_update_from_row(pending_row)
                    pending_by_run.setdefault(pending.run_id, []).append(pending)
    except ReadOnlyDatabaseMissing:
        return []
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc
    summaries = []
    for row in rows:
        events = events_by_run.get(row["id"], [])
        summary = _run_summary_from_row(row, events=events)
        summaries.append(RunHistorySummary(
            **_sanitize_run_summary(settings, summary).model_dump(),
            verification=_verification_for_run(
                summary, pending_by_run.get(row["id"], []), events,
            ),
        ))
    return summaries


def api_run_detail(run_id: int, request: Request) -> RunDetail:
    settings = _settings(request)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            run = conn.execute(
                """
                SELECT *
                FROM update_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            pending = conn.execute(
                """
                SELECT *
                FROM pending_updates
                WHERE run_id = ?
                ORDER BY line_no, id
                """,
                (run_id,),
            ).fetchall()
            events = conn.execute(
                """
                SELECT *
                FROM update_events
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
    except ReadOnlyDatabaseMissing as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc

    run_events = [_event_from_row(row) for row in events]
    summary = _run_summary_from_row(run, events=run_events)
    pending_updates = [_pending_update_from_row(row) for row in pending]
    detail = RunDetail(
        **summary.model_dump(),
        pending_updates=pending_updates,
        verification=_verification_for_run(summary, pending_updates, run_events),
    )
    return _sanitize_run_detail(settings, detail)


def _verification_for_run(
    run: RunSummary,
    pending_updates: list[PendingUpdateRecord],
    events: list[RunEventRecord],
) -> RunVerificationSummary:
    if run.dry_run or run.mode not in VALID_UPDATE_MODES:
        return RunVerificationSummary()
    return verification_from_run_records(pending_updates, events)


def api_run_log(
    run_id: int,
    request: Request,
    tail_bytes: int = Query(default=DEFAULT_LOG_TAIL_BYTES, ge=1),
) -> RunLogResponse:
    settings = _settings(request)
    max_bytes = min(tail_bytes, MAX_LOG_TAIL_BYTES)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            run = conn.execute(
                """
                SELECT id, log_file
                FROM update_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
    except ReadOnlyDatabaseMissing as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc

    raw_log_file = str(run["log_file"] or "")
    log_path = _safe_log_path(settings, raw_log_file)
    if log_path is None:
        return RunLogResponse(
            run_id=run_id,
            log_file=raw_log_file,
            exists=False,
            content="",
            truncated=False,
            max_bytes=max_bytes,
        )
    return _run_log_response(run_id, raw_log_file, log_path, max_bytes)


def _run_summary_from_row(
    row: sqlite3.Row,
    events: list[RunEventRecord] | None = None,
) -> RunSummary:
    return RunSummary(
        id=int(row["id"]),
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
        status=str(row["status"]),
        dry_run=bool(row["dry_run"]),
        mode=str(row["mode"]),
        wud_file=str(row["wud_file"]),
        log_file=str(row["log_file"]),
        metadata=_metadata_from_row(row),
        events=events or [],
    )


def _pending_update_from_row(row: sqlite3.Row) -> PendingUpdateRecord:
    return PendingUpdateRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        line_no=int(row["line_no"]),
        raw=str(row["raw"]),
        image=str(row["image"]),
        target_digest=str(row["target_digest"]),
        desired_tag=str(row["desired_tag"]),
        service_key=str(row["service_key"]),
        stack_name=str(row["stack_name"]),
        service_name=str(row["service_name"]),
        status=str(row["status"]),
        status_reason=str(row["status_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metadata=_metadata_from_row(row),
        digest_provenance=_digest_provenance_from_row(row),
    )


def _event_from_row(row: sqlite3.Row) -> RunEventRecord:
    return RunEventRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        created_at=str(row["created_at"]),
        service_name=str(row["service_name"]),
        stack_name=str(row["stack_name"]),
        image=str(row["image"]),
        target_image=str(row["target_image"]),
        old_image_id=str(row["old_image_id"]),
        new_image_id=str(row["new_image_id"]),
        old_digest=str(row["old_digest"]),
        new_digest=str(row["new_digest"]),
        status=str(row["status"]),
        metadata=_metadata_from_row(row),
        digest_provenance=_digest_provenance_from_row(row),
    )


def _digest_provenance_from_row(row: sqlite3.Row) -> DigestTagProvenance | None:
    provenance = digest_provenance_from_row(row)
    if provenance is None:
        return None
    return DigestTagProvenance.model_validate(asdict(provenance))


def _sanitize_run_summary(settings: WebSettings, run: RunSummary) -> RunSummary:
    payload = run.model_dump(mode="json")
    payload["metadata"] = _sanitize_support_bundle_value(settings, payload["metadata"])
    payload["events"] = [
        _sanitize_run_event(settings, event).model_dump(mode="json")
        for event in run.events
    ]
    return RunSummary.model_validate(payload)


def _sanitize_run_detail(settings: WebSettings, run: RunDetail) -> RunDetail:
    payload = run.model_dump(mode="json")
    payload["metadata"] = _sanitize_support_bundle_value(settings, payload["metadata"])
    payload["events"] = [
        _sanitize_run_event(settings, event).model_dump(mode="json")
        for event in run.events
    ]
    for pending_update in payload["pending_updates"]:
        pending_update["metadata"] = _sanitize_support_bundle_value(
            settings,
            pending_update["metadata"],
        )
    return RunDetail.model_validate(payload)


def _sanitize_run_event(
    settings: WebSettings,
    event: RunEventRecord,
) -> RunEventRecord:
    payload = event.model_dump(mode="json")
    payload["metadata"] = _sanitize_support_bundle_value(settings, payload["metadata"])
    return RunEventRecord.model_validate(payload)


def _metadata_from_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = str(row["metadata_json"] or "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="invalid metadata JSON in database",
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=500,
            detail="metadata JSON must be an object",
        )
    return value


def _safe_log_path(settings: WebSettings, raw_log_file: str) -> Path | None:
    if not raw_log_file:
        return None
    log_dir = settings.config.log_dir
    candidate = Path(raw_log_file)
    if not candidate.is_absolute():
        candidate = log_dir / candidate
    try:
        resolved_log_dir = log_dir.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="log file not found") from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not resolve log file", exc),
        ) from exc
    if not resolved_candidate.is_relative_to(resolved_log_dir):
        raise HTTPException(status_code=403, detail="log file is outside WUD_LOG_DIR")
    return resolved_candidate


def _run_log_response(
    run_id: int,
    raw_log_file: str,
    log_path: Path,
    max_bytes: int,
) -> RunLogResponse:
    tail = _read_log_tail(log_path, max_bytes)
    return RunLogResponse(
        run_id=run_id,
        log_file=raw_log_file,
        exists=tail.exists,
        content=tail.content,
        truncated=tail.truncated,
        max_bytes=max_bytes,
    )


def _read_log_tail(log_path: Path, max_bytes: int) -> LogTail:
    try:
        if not log_path.is_file():
            return LogTail(
                exists=False,
                content="",
                truncated=False,
            )
        size = log_path.stat().st_size
        truncated = size > max_bytes
        with log_path.open("rb") as file:
            if truncated:
                file.seek(-max_bytes, os.SEEK_END)
            content = file.read(max_bytes).decode("utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="could not read log file",
        ) from exc
    return LogTail(
        exists=True,
        content=content,
        truncated=truncated,
    )
