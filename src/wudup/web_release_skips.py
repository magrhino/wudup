"""WebUI release-skip storage, listing, and pending-item annotation."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from typing import Any

from fastapi import HTTPException

from . import web_database
from .db import (
    DatabaseError,
    active_release_skip_rows,
    release_skip_rows_with_status,
    service_updated_since,
    utc_timestamp,
)
from .images import repo_key
from .release_skips import (
    ReleaseKey,
    ReleaseSkip,
    release_key,
    skip_for_release,
    skip_from_row,
    skip_label,
    skips_by_service,
)
from .web_metadata import json_object as _json_object
from .web_models import (
    CreateReleaseSkipOperation,
    PendingReleaseKey,
    PendingReleaseSkip,
    SnoozeRecord,
    WebSettings,
)
from .web_runs import _metadata_from_row

LOGGER = logging.getLogger(__name__)


def release_skip_records(conn: sqlite3.Connection) -> list[SnoozeRecord]:
    """Return every release skip as a snooze record, newest first."""

    return [
        release_skip_record(conn, row, active=not row["cleared"])
        for row in release_skip_rows_with_status(conn)
    ]


def release_skip_record(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    active: bool | None = None,
) -> SnoozeRecord:
    if active is None:
        active = not service_updated_since(
            conn,
            service_key=str(row["service_key"]),
            created_at=str(row["created_at"]),
        )
    return SnoozeRecord(
        id=int(row["id"]),
        service_key=str(row["service_key"]),
        snoozed_until=None,
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
        active=active,
        kind="release",
        release_label=skip_label(
            str(row["basis"]),
            str(row["value"]),
            str(row["scope"]),
        ),
        skipped_tag=str(row["skipped_tag"]),
        metadata=_metadata_from_row(row),
    )


def release_skip_row(conn: sqlite3.Connection, skip_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM release_skips
        WHERE id = ?
        LIMIT 1
        """,
        (skip_id,),
    ).fetchone()


def release_skip_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "service_key": str(row["service_key"]),
        "image_repo": str(row["image_repo"]),
        "basis": str(row["basis"]),
        "value": str(row["value"]),
        "scope": str(row["scope"]),
        "skipped_tag": str(row["skipped_tag"]),
        "reason": str(row["reason"]),
        "created_at": str(row["created_at"]),
        "kind": "release",
    }


def insert_release_skips(
    conn: sqlite3.Connection,
    payload: CreateReleaseSkipOperation,
) -> tuple[sqlite3.Row, ...]:
    """Store one release skip per service and return the stored rows.

    The release is derived here from the offered image, tag, and digest
    rather than trusted from the browser.
    """

    service_keys = tuple(
        dict.fromkeys(key.strip() for key in payload.service_keys if key.strip())
    )
    if not service_keys:
        raise HTTPException(status_code=422, detail="service_keys is required")
    image_repo = repo_key(payload.image.strip())
    key = release_key(image_repo, payload.target_tag, payload.digest)
    if key is None:
        raise HTTPException(
            status_code=422,
            detail="target_tag or digest is required to name the release to skip",
        )
    if payload.scope == "line" and not key.line:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{key.label} has no major.minor version, "
                "so only this release can be skipped"
            ),
        )
    skipped_tag = payload.target_tag.strip()
    reason = payload.reason.strip()
    created_at = utc_timestamp()
    existing = skips_by_service(
        skip_from_row(row)
        for row in active_release_skip_rows(conn, service_keys=service_keys)
    )
    rows: list[sqlite3.Row] = []
    for service_key in service_keys:
        if _already_skipped(existing.get(service_key, ()), key, payload.scope):
            raise HTTPException(
                status_code=409,
                detail=f"{service_key} already skips {skip_label(key.basis, key.value, payload.scope)}",
            )
        cursor = conn.execute(
            """
            INSERT INTO release_skips (
                service_key,
                image_repo,
                basis,
                value,
                scope,
                skipped_tag,
                reason,
                created_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service_key,
                image_repo,
                key.basis,
                key.value,
                payload.scope,
                skipped_tag,
                reason,
                created_at,
                _json_object({"digest": payload.digest.strip()}),
            ),
        )
        row = release_skip_row(conn, int(cursor.lastrowid))
        if row is None:
            raise HTTPException(status_code=500, detail="release skip was not saved")
        rows.append(row)
    return tuple(rows)


def pending_release_skips(
    settings: WebSettings,
    service_keys: Iterable[str],
) -> dict[str, tuple[ReleaseSkip, ...]]:
    """Read active release skips for pending services; empty when unreadable."""

    keys = tuple(dict.fromkeys(service_keys))
    if not keys:
        return {}
    try:
        with closing(web_database.connect_readonly_db(settings)) as conn:
            rows = active_release_skip_rows(conn, service_keys=keys)
    except web_database.ReadOnlyDatabaseMissing:
        return {}
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        LOGGER.warning(
            "Could not read release skips from the WUDup database, so skipped "
            "releases are shown in Pending until it can be read again: %s",
            exc,
        )
        return {}
    return skips_by_service(skip_from_row(row) for row in rows)


def pending_release_fields(
    skips: Mapping[str, tuple[ReleaseSkip, ...]],
    *,
    service_keys: Iterable[str],
    image_repo: str,
    desired_tag: str,
    digest: str,
) -> tuple[PendingReleaseKey | None, PendingReleaseSkip | None]:
    """Return the offered release and the skip that hides or preceded it."""

    key = release_key(image_repo, desired_tag, digest)
    skip, matches = skip_for_release(skips, service_keys, key)
    return _pending_release_key(key), _pending_release_skip(skip, matches)


def _already_skipped(
    skips: Iterable[ReleaseSkip],
    key: ReleaseKey,
    scope: str,
) -> bool:
    return any(
        skip.basis == key.basis and skip.value == key.value and skip.scope == scope
        for skip in skips
    )


def _pending_release_key(key: ReleaseKey | None) -> PendingReleaseKey | None:
    if key is None:
        return None
    return PendingReleaseKey(
        basis=key.basis,
        value=key.value,
        line=key.line,
        label=key.label,
    )


def _pending_release_skip(
    skip: ReleaseSkip | None,
    matches: bool,
) -> PendingReleaseSkip | None:
    if skip is None:
        return None
    return PendingReleaseSkip(
        id=skip.id,
        service_key=skip.service_key,
        basis=skip.basis,
        value=skip.value,
        scope=skip.scope,
        label=skip.label,
        skipped_tag=skip.skipped_tag,
        reason=skip.reason,
        created_at=skip.created_at,
        matches=matches,
    )
