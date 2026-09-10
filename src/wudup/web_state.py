"""WebUI SQLite state route handlers and audit helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from datetime import time as datetime_time
from typing import Any

from fastapi import HTTPException, Query, Request

from . import web_scheduler
from .compose_rewrite import js_regex_escape
from .db import (
    DatabaseError,
    dependency_snooze_satisfied,
    init_db,
    open_db,
    utc_timestamp,
)
from .images import repo_key, tag_value_valid
from .web_auth import (
    _immediate_transaction,
    _request_actor_type,
    _safe_exception_detail,
    _settings,
)
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_metadata import json_list as _json_list
from .web_metadata import json_object as _json_object
from .web_models import (
    CreateDependencySnoozeOperation,
    CreateSnoozeOperation,
    DeleteDependencySnoozeOperation,
    DeleteServicePolicyOperation,
    DeleteSnoozeOperation,
    ServicePolicyRecord,
    SetTagExclusionStatusOperation,
    SnoozeRecord,
    SnoozeState,
    StateOperation,
    StateOperationResponse,
    TagExclusionRuleRecord,
    TagExclusionStatusFilter,
    UpsertServicePolicyOperation,
    UpsertTagExclusionOperation,
    WebSettings,
)
from .web_runs import _metadata_from_row

AUTO_UPDATE_DAYS = web_scheduler.AUTO_UPDATE_DAYS
_state_actor_type = _request_actor_type


def api_service_policies(request: Request) -> list[ServicePolicyRecord]:
    settings = _settings(request)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM service_policy
                ORDER BY service_key COLLATE BINARY
                """
            ).fetchall()
    except ReadOnlyDatabaseMissing:
        return []
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc
    return [_service_policy_from_row(row) for row in rows]


def api_snoozes(
    request: Request,
    state: SnoozeState = Query(default="active"),
) -> list[SnoozeRecord]:
    settings = _settings(request)
    now = utc_timestamp()
    time_where = ""
    time_params: tuple[object, ...] = ()
    if state == "active":
        time_where = "WHERE snoozed_until > ?"
        time_params = (now,)
    elif state == "expired":
        time_where = "WHERE snoozed_until <= ?"
        time_params = (now,)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            time_rows = conn.execute(
                f"""
                SELECT *
                FROM snoozes
                {time_where}
                ORDER BY snoozed_until DESC, id DESC
                """,
                time_params,
            ).fetchall()
            dependency_rows = conn.execute(
                """
                SELECT *
                FROM dependency_snoozes
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
            dependency_records = [
                _dependency_snooze_from_row(conn, row)
                for row in dependency_rows
            ]
    except ReadOnlyDatabaseMissing:
        return []
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc
    records = [_snooze_from_row(row, now=now) for row in time_rows]
    records.extend(
        record
        for record in dependency_records
        if state == "all"
        or (state == "active" and record.active)
        or (state == "expired" and not record.active)
    )
    return records


def api_tag_exclusions(
    request: Request,
    status: TagExclusionStatusFilter = Query(default="active"),
) -> list[TagExclusionRuleRecord]:
    settings = _settings(request)
    where = ""
    params: tuple[object, ...] = ()
    if status != "all":
        where = "WHERE status = ?"
        params = (status,)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tag_exclusion_rules
                {where}
                ORDER BY image_repo COLLATE BINARY,
                         scope COLLATE BINARY,
                         service_key COLLATE BINARY,
                         match_type COLLATE BINARY,
                         tag COLLATE BINARY,
                         id
                """,
                params,
            ).fetchall()
    except ReadOnlyDatabaseMissing:
        return []
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc
    return [_tag_exclusion_from_row(row) for row in rows]


def api_state_operation(
    payload: StateOperation,
    request: Request,
) -> StateOperationResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                return _apply_state_operation(conn, settings, request, payload)
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not update database", exc),
        ) from exc


def _auto_update_days_from_row(row: sqlite3.Row) -> tuple[str, ...]:
    return web_scheduler._auto_update_days_from_row(row)


def _service_policy_from_row(row: sqlite3.Row) -> ServicePolicyRecord:
    return ServicePolicyRecord(
        service_key=str(row["service_key"]),
        update_mode=str(row["update_mode"]),
        auto_update=bool(row["auto_update"]),
        snooze_default_seconds=(
            None
            if row["snooze_default_seconds"] is None
            else int(row["snooze_default_seconds"])
        ),
        auto_update_time=(
            None if row["auto_update_time"] is None else str(row["auto_update_time"])
        ),
        auto_update_days=list(_auto_update_days_from_row(row)),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metadata=_metadata_from_row(row),
    )


def _snooze_from_row(row: sqlite3.Row, *, now: str) -> SnoozeRecord:
    return SnoozeRecord(
        id=int(row["id"]),
        service_key=str(row["service_key"]),
        snoozed_until=str(row["snoozed_until"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
        active=str(row["snoozed_until"]) > now,
        kind="time",
        wait_for_service_key="",
        metadata=_metadata_from_row(row),
    )


def _dependency_snooze_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> SnoozeRecord:
    wait_for_service_key = str(row["wait_for_service_key"])
    active = not dependency_snooze_satisfied(
        conn,
        wait_for_service_key=wait_for_service_key,
        created_at=str(row["created_at"]),
    )
    return SnoozeRecord(
        id=int(row["id"]),
        service_key=str(row["service_key"]),
        snoozed_until=None,
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
        active=active,
        kind="dependency",
        wait_for_service_key=wait_for_service_key,
        metadata=_metadata_from_row(row),
    )


def _tag_exclusion_from_row(row: sqlite3.Row) -> TagExclusionRuleRecord:
    return TagExclusionRuleRecord(
        id=int(row["id"]),
        scope=str(row["scope"]),
        image_repo=str(row["image_repo"]),
        service_key=str(row["service_key"]),
        match_type=str(row["match_type"]),
        tag=str(row["tag"]),
        regex_fragment=str(row["regex_fragment"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metadata=_metadata_from_row(row),
    )


def _apply_state_operation(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: StateOperation,
) -> StateOperationResponse:
    if isinstance(payload, UpsertServicePolicyOperation):
        return _upsert_service_policy(conn, settings, request, payload)
    if isinstance(payload, DeleteServicePolicyOperation):
        return _delete_service_policy(conn, settings, request, payload)
    if isinstance(payload, CreateSnoozeOperation):
        return _create_snooze(conn, settings, request, payload)
    if isinstance(payload, DeleteSnoozeOperation):
        return _delete_snooze(conn, settings, request, payload)
    if isinstance(payload, CreateDependencySnoozeOperation):
        return _create_dependency_snooze(conn, settings, request, payload)
    if isinstance(payload, DeleteDependencySnoozeOperation):
        return _delete_dependency_snooze(conn, settings, request, payload)
    if isinstance(payload, UpsertTagExclusionOperation):
        return _upsert_tag_exclusion(conn, settings, request, payload)
    if isinstance(payload, SetTagExclusionStatusOperation):
        return _set_tag_exclusion_status(conn, settings, request, payload)
    raise HTTPException(status_code=422, detail="unsupported operation")


def _upsert_service_policy(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: UpsertServicePolicyOperation,
) -> StateOperationResponse:
    service_key = _required_state_text(payload.service_key, "service_key")
    before_row = _service_policy_row(conn, service_key)
    (
        update_mode,
        auto_update,
        snooze_default_seconds,
        auto_update_time,
        auto_update_days,
    ) = _service_policy_upsert_values(payload, before_row)
    now = utc_timestamp()
    conn.execute(
        """
        INSERT INTO service_policy (
            service_key,
            update_mode,
            auto_update,
            snooze_default_seconds,
            auto_update_time,
            auto_update_days_json,
            created_at,
            updated_at,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')
        ON CONFLICT(service_key) DO UPDATE SET
            update_mode = excluded.update_mode,
            auto_update = excluded.auto_update,
            snooze_default_seconds = excluded.snooze_default_seconds,
            auto_update_time = excluded.auto_update_time,
            auto_update_days_json = excluded.auto_update_days_json,
            updated_at = excluded.updated_at
        """,
        (
            service_key,
            update_mode,
            int(auto_update),
            snooze_default_seconds,
            auto_update_time,
            _json_list(auto_update_days),
            now,
            now,
        ),
    )
    after_row = _service_policy_row(conn, service_key)
    if after_row is None:
        raise HTTPException(status_code=500, detail="service policy was not saved")
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="service_policy",
        resource_id=service_key,
        target={"service_key": service_key},
        before=_service_policy_summary(before_row),
        after=_service_policy_summary(after_row),
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="service_policy",
        resource_id=service_key,
        resource=_service_policy_from_row(after_row),
    )


def _service_policy_upsert_values(
    payload: UpsertServicePolicyOperation,
    before_row: sqlite3.Row | None,
) -> tuple[str, bool, int | None, str | None, tuple[str, ...]]:
    if before_row is None:
        return (
            payload.update_mode,
            payload.auto_update,
            payload.snooze_default_seconds,
            _normalized_auto_update_time(payload.auto_update_time),
            _normalized_auto_update_days(payload.auto_update_days),
        )

    fields_set = payload.model_fields_set
    update_mode = (
        payload.update_mode
        if "update_mode" in fields_set
        else str(before_row["update_mode"])
    )
    auto_update = (
        payload.auto_update
        if "auto_update" in fields_set
        else bool(before_row["auto_update"])
    )
    snooze_default_seconds = (
        payload.snooze_default_seconds
        if "snooze_default_seconds" in fields_set
        else (
            None
            if before_row["snooze_default_seconds"] is None
            else int(before_row["snooze_default_seconds"])
        )
    )
    auto_update_time = (
        _normalized_auto_update_time(payload.auto_update_time)
        if "auto_update_time" in fields_set
        else (
            None
            if before_row["auto_update_time"] is None
            else str(before_row["auto_update_time"])
        )
    )
    auto_update_days = (
        _normalized_auto_update_days(payload.auto_update_days)
        if "auto_update_days" in fields_set
        else _auto_update_days_from_row(before_row)
    )
    return (
        update_mode,
        auto_update,
        snooze_default_seconds,
        auto_update_time,
        auto_update_days,
    )


def _normalized_auto_update_time(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return None
    try:
        datetime_time.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="auto_update_time must use HH:MM 24-hour format",
        ) from exc
    if len(text) != 5 or text[2] != ":":
        raise HTTPException(
            status_code=422,
            detail="auto_update_time must use HH:MM 24-hour format",
        )
    return text


def _normalized_auto_update_days(values: Sequence[str]) -> tuple[str, ...]:
    days: list[str] = []
    for value in values:
        if value not in AUTO_UPDATE_DAYS:
            raise HTTPException(status_code=422, detail="auto_update_days is invalid")
        if value not in days:
            days.append(value)
    return tuple(days)


def _delete_service_policy(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: DeleteServicePolicyOperation,
) -> StateOperationResponse:
    service_key = _required_state_text(payload.service_key, "service_key")
    before_row = _service_policy_row(conn, service_key)
    if before_row is None:
        raise HTTPException(status_code=404, detail="service policy not found")
    conn.execute("DELETE FROM service_policy WHERE service_key = ?", (service_key,))
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="service_policy",
        resource_id=service_key,
        target={"service_key": service_key},
        before=_service_policy_summary(before_row),
        after=None,
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="service_policy",
        resource_id=service_key,
    )


def _create_snooze(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: CreateSnoozeOperation,
) -> StateOperationResponse:
    service_key = _required_state_text(payload.service_key, "service_key")
    snoozed_until = _future_iso_timestamp(payload.snoozed_until, "snoozed_until")
    reason = payload.reason.strip()
    cursor = conn.execute(
        """
        INSERT INTO snoozes (
            service_key,
            snoozed_until,
            reason,
            created_at,
            metadata_json
        )
        VALUES (?, ?, ?, ?, '{}')
        """,
        (service_key, snoozed_until, reason, utc_timestamp()),
    )
    snooze_id = int(cursor.lastrowid)
    after_row = _snooze_row(conn, snooze_id)
    if after_row is None:
        raise HTTPException(status_code=500, detail="snooze was not saved")
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="snooze",
        resource_id=str(snooze_id),
        target={"id": snooze_id, "service_key": service_key},
        before=None,
        after=_snooze_summary(after_row),
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="snooze",
        resource_id=str(snooze_id),
        resource=_snooze_from_row(after_row, now=utc_timestamp()),
    )


def _delete_snooze(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: DeleteSnoozeOperation,
) -> StateOperationResponse:
    before_row = _snooze_row(conn, payload.snooze_id)
    if before_row is None:
        raise HTTPException(status_code=404, detail="snooze not found")
    conn.execute("DELETE FROM snoozes WHERE id = ?", (payload.snooze_id,))
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="snooze",
        resource_id=str(payload.snooze_id),
        target={
            "id": payload.snooze_id,
            "service_key": str(before_row["service_key"]),
        },
        before=_snooze_summary(before_row),
        after=None,
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="snooze",
        resource_id=str(payload.snooze_id),
    )


def _create_dependency_snooze(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: CreateDependencySnoozeOperation,
) -> StateOperationResponse:
    service_key = _required_state_text(payload.service_key, "service_key")
    wait_for_service_key = _required_state_text(
        payload.wait_for_service_key,
        "wait_for_service_key",
    )
    if service_key == wait_for_service_key:
        raise HTTPException(
            status_code=422,
            detail="wait_for_service_key must be different from service_key",
        )
    reason = payload.reason.strip()
    cursor = conn.execute(
        """
        INSERT INTO dependency_snoozes (
            service_key,
            wait_for_service_key,
            reason,
            created_at,
            metadata_json
        )
        VALUES (?, ?, ?, ?, '{}')
        """,
        (service_key, wait_for_service_key, reason, utc_timestamp()),
    )
    snooze_id = int(cursor.lastrowid)
    after_row = _dependency_snooze_row(conn, snooze_id)
    if after_row is None:
        raise HTTPException(status_code=500, detail="dependency snooze was not saved")
    target = {
        "id": snooze_id,
        "service_key": service_key,
        "wait_for_service_key": wait_for_service_key,
    }
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="dependency_snooze",
        resource_id=str(snooze_id),
        target=target,
        before=None,
        after=_dependency_snooze_summary(after_row),
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="dependency_snooze",
        resource_id=str(snooze_id),
        resource=_dependency_snooze_from_row(conn, after_row),
    )


def _delete_dependency_snooze(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: DeleteDependencySnoozeOperation,
) -> StateOperationResponse:
    before_row = _dependency_snooze_row(conn, payload.snooze_id)
    if before_row is None:
        raise HTTPException(status_code=404, detail="dependency snooze not found")
    conn.execute(
        "DELETE FROM dependency_snoozes WHERE id = ?",
        (payload.snooze_id,),
    )
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="dependency_snooze",
        resource_id=str(payload.snooze_id),
        target={
            "id": payload.snooze_id,
            "service_key": str(before_row["service_key"]),
            "wait_for_service_key": str(before_row["wait_for_service_key"]),
        },
        before=_dependency_snooze_summary(before_row),
        after=None,
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="dependency_snooze",
        resource_id=str(payload.snooze_id),
    )


def _upsert_tag_exclusion(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: UpsertTagExclusionOperation,
) -> StateOperationResponse:
    image_repo = _normalized_image_repo(payload.image_repo)
    service_key = _tag_exclusion_service_key(payload.scope, payload.service_key)
    tag = _valid_tag(payload.tag)
    before_row = _tag_exclusion_unique_row(
        conn,
        scope=payload.scope,
        image_repo=image_repo,
        service_key=service_key,
        match_type=payload.match_type,
        tag=tag,
    )
    now = utc_timestamp()
    conn.execute(
        """
        INSERT INTO tag_exclusion_rules (
            scope,
            image_repo,
            service_key,
            match_type,
            tag,
            regex_fragment,
            status,
            created_at,
            updated_at,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        ON CONFLICT(scope, image_repo, service_key, match_type, tag)
        DO UPDATE SET
            regex_fragment = excluded.regex_fragment,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            payload.scope,
            image_repo,
            service_key,
            payload.match_type,
            tag,
            js_regex_escape(tag),
            payload.status,
            now,
            now,
        ),
    )
    after_row = _tag_exclusion_unique_row(
        conn,
        scope=payload.scope,
        image_repo=image_repo,
        service_key=service_key,
        match_type=payload.match_type,
        tag=tag,
    )
    if after_row is None:
        raise HTTPException(status_code=500, detail="tag exclusion was not saved")
    resource_id = str(after_row["id"])
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="tag_exclusion",
        resource_id=resource_id,
        target={
            "id": int(after_row["id"]),
            "scope": payload.scope,
            "image_repo": image_repo,
            "service_key": service_key,
            "match_type": payload.match_type,
            "tag": tag,
        },
        before=_tag_exclusion_summary(before_row),
        after=_tag_exclusion_summary(after_row),
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="tag_exclusion",
        resource_id=resource_id,
        resource=_tag_exclusion_from_row(after_row),
    )


def _set_tag_exclusion_status(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    payload: SetTagExclusionStatusOperation,
) -> StateOperationResponse:
    before_row = _tag_exclusion_row(conn, payload.rule_id)
    if before_row is None:
        raise HTTPException(status_code=404, detail="tag exclusion not found")
    conn.execute(
        """
        UPDATE tag_exclusion_rules
        SET status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (payload.status, utc_timestamp(), payload.rule_id),
    )
    after_row = _tag_exclusion_row(conn, payload.rule_id)
    if after_row is None:
        raise HTTPException(status_code=500, detail="tag exclusion was not saved")
    audit_run_id = _insert_state_audit(
        conn,
        settings,
        request,
        operation=payload.kind,
        resource_type="tag_exclusion",
        resource_id=str(payload.rule_id),
        target={
            "id": payload.rule_id,
            "scope": str(before_row["scope"]),
            "image_repo": str(before_row["image_repo"]),
            "service_key": str(before_row["service_key"]),
            "match_type": str(before_row["match_type"]),
            "tag": str(before_row["tag"]),
        },
        before=_tag_exclusion_summary(before_row),
        after=_tag_exclusion_summary(after_row),
    )
    return StateOperationResponse(
        operation=payload.kind,
        status="success",
        audit_run_id=audit_run_id,
        resource_type="tag_exclusion",
        resource_id=str(payload.rule_id),
        resource=_tag_exclusion_from_row(after_row),
    )


def _service_policy_row(
    conn: sqlite3.Connection,
    service_key: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM service_policy
        WHERE service_key = ?
        LIMIT 1
        """,
        (service_key,),
    ).fetchone()


def _snooze_row(conn: sqlite3.Connection, snooze_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM snoozes
        WHERE id = ?
        LIMIT 1
        """,
        (snooze_id,),
    ).fetchone()


def _dependency_snooze_row(
    conn: sqlite3.Connection,
    snooze_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM dependency_snoozes
        WHERE id = ?
        LIMIT 1
        """,
        (snooze_id,),
    ).fetchone()


def _tag_exclusion_row(
    conn: sqlite3.Connection,
    rule_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM tag_exclusion_rules
        WHERE id = ?
        LIMIT 1
        """,
        (rule_id,),
    ).fetchone()


def _tag_exclusion_unique_row(
    conn: sqlite3.Connection,
    *,
    scope: str,
    image_repo: str,
    service_key: str,
    match_type: str,
    tag: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM tag_exclusion_rules
        WHERE scope = ?
          AND image_repo = ?
          AND service_key = ?
          AND match_type = ?
          AND tag = ?
        LIMIT 1
        """,
        (scope, image_repo, service_key, match_type, tag),
    ).fetchone()


def _required_state_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    return cleaned


def _future_iso_timestamp(value: str, field_name: str) -> str:
    raw = _required_state_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid ISO timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must include a timezone",
        )
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if normalized <= now:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be in the future",
        )
    return normalized.isoformat()


def _normalized_image_repo(value: str) -> str:
    cleaned = _required_state_text(value, "image_repo")
    if any(character.isspace() for character in cleaned):
        raise HTTPException(status_code=422, detail="image_repo must not contain spaces")
    normalized = repo_key(cleaned)
    if not normalized:
        raise HTTPException(status_code=422, detail="image_repo is required")
    return normalized


def _tag_exclusion_service_key(scope: str, service_key: str) -> str:
    cleaned = service_key.strip()
    if scope == "service":
        if not cleaned:
            raise HTTPException(
                status_code=422,
                detail="service_key is required for service tag exclusions",
            )
        return cleaned
    if cleaned:
        raise HTTPException(
            status_code=422,
            detail="service_key is only valid for service tag exclusions",
        )
    return ""


def _valid_tag(value: str) -> str:
    tag = _required_state_text(value, "tag")
    if not tag_value_valid(tag):
        raise HTTPException(status_code=422, detail=f"tag is invalid: {tag}")
    return tag


def _insert_managed_settings_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    *,
    updated_keys: Sequence[str],
    before: dict[str, str],
    after: dict[str, str],
) -> int:
    now = utc_timestamp()
    target = {"keys": sorted(updated_keys)}
    metadata = {
        "source": "webui",
        "operation": "update_managed_settings",
        "actor_type": _state_actor_type(settings, request),
        "resource_type": "managed_settings",
        "resource_id": "webui_preferences",
        "target": target,
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
        VALUES (?, ?, 'success', 0, 'web-settings', ?, '', ?)
        """,
        (
            now,
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    event_metadata = {
        **metadata,
        "before": before,
        "after": after,
    }
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
        VALUES (?, ?, 'settings', 'webui', 'managed-settings', 'webui-preferences', 'success', ?)
        """,
        (
            run_id,
            now,
            _json_object(event_metadata),
        ),
    )
    return run_id


def _insert_state_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    *,
    operation: str,
    resource_type: str,
    resource_id: str,
    target: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": "webui",
        "operation": operation,
        "actor_type": _state_actor_type(settings, request),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "target": target,
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
        VALUES (?, ?, 'success', 0, 'web-state', ?, '', ?)
        """,
        (
            now,
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    event_metadata = {
        **metadata,
        "before": before,
        "after": after,
    }
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
        VALUES (?, ?, ?, ?, ?, ?, 'success', ?)
        """,
        (
            run_id,
            now,
            _state_audit_service_name(target, resource_type),
            _state_audit_stack_name(target),
            _state_audit_image(target, resource_type, resource_id),
            resource_id,
            _json_object(event_metadata),
        ),
    )
    return run_id


def _state_audit_stack_name(target: Mapping[str, Any]) -> str:
    service_key = str(target.get("service_key") or "")
    if "/" not in service_key:
        return ""
    return service_key.split("/", 1)[0]


def _state_audit_service_name(
    target: Mapping[str, Any],
    resource_type: str,
) -> str:
    service_key = str(target.get("service_key") or "")
    if "/" in service_key:
        return service_key.split("/", 1)[1]
    if service_key:
        return service_key
    return str(target.get("image_repo") or resource_type)


def _state_audit_image(
    target: Mapping[str, Any],
    resource_type: str,
    resource_id: str,
) -> str:
    return str(
        target.get("image_repo")
        or target.get("service_key")
        or resource_id
        or resource_type
    )


def _service_policy_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "service_key": str(row["service_key"]),
        "update_mode": str(row["update_mode"]),
        "auto_update": bool(row["auto_update"]),
        "snooze_default_seconds": (
            None
            if row["snooze_default_seconds"] is None
            else int(row["snooze_default_seconds"])
        ),
        "auto_update_time": (
            None if row["auto_update_time"] is None else str(row["auto_update_time"])
        ),
        "auto_update_days": list(_auto_update_days_from_row(row)),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _snooze_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "service_key": str(row["service_key"]),
        "snoozed_until": str(row["snoozed_until"]),
        "reason": str(row["reason"]),
        "created_at": str(row["created_at"]),
        "kind": "time",
        "wait_for_service_key": "",
    }


def _dependency_snooze_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "service_key": str(row["service_key"]),
        "snoozed_until": None,
        "reason": str(row["reason"]),
        "created_at": str(row["created_at"]),
        "kind": "dependency",
        "wait_for_service_key": str(row["wait_for_service_key"]),
    }


def _tag_exclusion_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "scope": str(row["scope"]),
        "image_repo": str(row["image_repo"]),
        "service_key": str(row["service_key"]),
        "match_type": str(row["match_type"]),
        "tag": str(row["tag"]),
        "regex_fragment": str(row["regex_fragment"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
