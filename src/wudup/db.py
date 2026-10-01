"""SQLite persistence helpers for WUDup."""

# Compatibility imports deliberately re-export these names unchanged.

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterable
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from .db_schema import (
    _EXPECTED_SCHEMAS_BY_VERSION as _EXPECTED_SCHEMAS_BY_VERSION,
)
from .db_schema import (
    _MIGRATIONS_BY_TARGET_VERSION as _MIGRATIONS_BY_TARGET_VERSION,
)
from .db_schema import (
    _SECURITY_SCAN_CACHE_SCHEMA_V9_SQL as _SECURITY_SCAN_CACHE_SCHEMA_V9_SQL,
)
from .db_schema import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from .db_schema import (
    DatabaseError as DatabaseError,
)
from .db_schema import (
    _user_version as _user_version,
)
from .db_schema import (
    _validate_schema as _validate_schema,
)
from .db_schema import (
    init_schema as init_db,  # noqa: F401 - compatibility re-export
)
from .db_schema import (
    utc_timestamp as utc_timestamp,
)
from .db_storage import _prepare_private_database
from .digest_provenance import (
    DIGEST_PROVENANCE_SQL_COLUMNS,
    DigestTagProvenance,
    digest_provenance_or_empty,
)


def connect_db(path: str | Path, *, owner_uid: int | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WUDup defaults applied."""

    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path = _prepare_private_database(db_path, owner_uid=owner_uid)

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def open_db(
    path: str | Path, *, owner_uid: int | None = None
) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection, yield it, and close it on exit.

    Use this for request-scoped or test-scoped connections that should be
    closed deterministically.  For long-lived connections that must survive
    across multiple call-sites, use :func:`connect_db` directly.
    """
    conn = connect_db(path, owner_uid=owner_uid)
    try:
        yield conn
    finally:
        conn.close()


def insert_update_run(
    conn: sqlite3.Connection,
    *,
    started_at: str | None = None,
    status: str = "started",
    dry_run: bool = False,
    mode: str = "",
    wud_file: str = "",
    log_file: str = "",
    metadata_json: str = "{}",
) -> int:
    """Insert one updater run and return its row id."""

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO update_runs (
                started_at,
                status,
                dry_run,
                mode,
                wud_file,
                log_file,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at or utc_timestamp(),
                status,
                int(dry_run),
                mode,
                wud_file,
                log_file,
                metadata_json,
            ),
        )
    return int(cursor.lastrowid)


def insert_update_event(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    service_name: str,
    image: str,
    status: str,
    created_at: str | None = None,
    stack_name: str = "",
    target_image: str = "",
    old_image_id: str = "",
    new_image_id: str = "",
    old_digest: str = "",
    new_digest: str = "",
    metadata_json: str = "{}",
    digest_provenance: DigestTagProvenance | None = None,
    commit: bool = True,
) -> int:
    """Insert one per-service event, optionally joining the caller's transaction."""

    provenance = digest_provenance_or_empty(digest_provenance)
    with (conn if commit else nullcontext()):
        cursor = conn.execute(
            """
            INSERT INTO update_events (
                run_id,
                created_at,
                service_name,
                stack_name,
                image,
                target_image,
                old_image_id,
                new_image_id,
                old_digest,
                new_digest,
                status,
                metadata_json,
                digest_source_image,
                digest_resolved_tag,
                digest_watch_tag,
                digest_target_digest,
                digest_final_image,
                digest_provenance_source,
                digest_provenance_confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                created_at or utc_timestamp(),
                service_name,
                stack_name,
                image,
                target_image,
                old_image_id,
                new_image_id,
                old_digest,
                new_digest,
                status,
                metadata_json,
                provenance["digest_source_image"],
                provenance["digest_resolved_tag"],
                provenance["digest_watch_tag"],
                provenance["digest_target_digest"],
                provenance["digest_final_image"],
                provenance["digest_provenance_source"],
                provenance["digest_provenance_confidence"],
            ),
        )
    return int(cursor.lastrowid)


def insert_snooze(
    conn: sqlite3.Connection,
    *,
    service_key: str,
    snoozed_until: str,
    reason: str = "",
    created_at: str | None = None,
    metadata_json: str = "{}",
) -> int:
    """Insert one service snooze and return its row id."""

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO snoozes (
                service_key,
                snoozed_until,
                reason,
                created_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                service_key,
                snoozed_until,
                reason,
                created_at or utc_timestamp(),
                metadata_json,
            ),
        )
    return int(cursor.lastrowid)


def insert_dependency_snooze(
    conn: sqlite3.Connection,
    *,
    service_key: str,
    wait_for_service_key: str,
    reason: str = "",
    created_at: str | None = None,
    metadata_json: str = "{}",
) -> int:
    """Insert one dependency snooze and return its row id."""

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO dependency_snoozes (
                service_key,
                wait_for_service_key,
                reason,
                created_at,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                service_key,
                wait_for_service_key,
                reason,
                created_at or utc_timestamp(),
                metadata_json,
            ),
        )
    return int(cursor.lastrowid)


def active_snooze(
    conn: sqlite3.Connection,
    *,
    service_key: str,
    now: str | None = None,
) -> sqlite3.Row | None:
    """Return the latest active snooze for a service, if one exists."""

    with closing(conn.execute(
        """
        SELECT *
        FROM snoozes
        WHERE service_key = ?
          AND snoozed_until > ?
        ORDER BY snoozed_until DESC, id DESC
        LIMIT 1
        """,
        (service_key, now or utc_timestamp()),
    )) as cursor:
        return cursor.fetchone()


def dependency_snooze_satisfied(
    conn: sqlite3.Connection,
    *,
    wait_for_service_key: str,
    created_at: str,
) -> bool:
    """Return true when the dependency service updated after snooze creation."""

    row = conn.execute(
        """
        SELECT 1
        FROM update_events
        WHERE stack_name || '/' || service_name = ?
          AND status = 'success'
          AND created_at >= ?
        LIMIT 1
        """,
        (wait_for_service_key, created_at),
    ).fetchone()
    return row is not None


def active_dependency_snooze_rows(
    conn: sqlite3.Connection,
    *,
    service_keys: Iterable[str] | None = None,
) -> tuple[sqlite3.Row, ...]:
    """Return unsatisfied dependency snoozes, optionally scoped by target service."""

    keys = tuple(dict.fromkeys(service_keys or ()))
    if keys:
        placeholders = ", ".join("?" for _ in keys)
        rows = conn.execute(
            f"""
            SELECT *
            FROM dependency_snoozes
            WHERE service_key IN ({placeholders})
            ORDER BY created_at DESC, id DESC
            """,
            keys,
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM dependency_snoozes
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return tuple(
        row
        for row in rows
        if not dependency_snooze_satisfied(
            conn,
            wait_for_service_key=str(row["wait_for_service_key"]),
            created_at=str(row["created_at"]),
        )
    )


def insert_pending_update(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    line_no: int,
    raw: str,
    image: str,
    target_digest: str = "",
    desired_tag: str = "",
    service_key: str = "",
    stack_name: str = "",
    service_name: str = "",
    status: str = "pending",
    status_reason: str = "",
    created_at: str | None = None,
    updated_at: str | None = None,
    metadata_json: str = "{}",
    digest_provenance: DigestTagProvenance | None = None,
) -> int:
    """Insert one parsed WUD target as explicit pending/update state."""

    now = utc_timestamp()
    created = created_at or now
    updated = updated_at or created
    provenance = digest_provenance_or_empty(digest_provenance)
    with conn:
        cursor = conn.execute(
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
                metadata_json,
                digest_source_image,
                digest_resolved_tag,
                digest_watch_tag,
                digest_target_digest,
                digest_final_image,
                digest_provenance_source,
                digest_provenance_confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                created,
                updated,
                metadata_json,
                provenance["digest_source_image"],
                provenance["digest_resolved_tag"],
                provenance["digest_watch_tag"],
                provenance["digest_target_digest"],
                provenance["digest_final_image"],
                provenance["digest_provenance_source"],
                provenance["digest_provenance_confidence"],
            ),
        )
    return int(cursor.lastrowid)


def update_pending_update(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    line_no: int,
    status: str,
    status_reason: str = "",
    service_key: str | None = None,
    stack_name: str | None = None,
    service_name: str | None = None,
    updated_at: str | None = None,
    digest_provenance: DigestTagProvenance | None = None,
) -> None:
    """Update explicit pending state for one parsed WUD target."""

    assignments = ["status = ?", "status_reason = ?", "updated_at = ?"]
    values: list[object] = [status, status_reason, updated_at or utc_timestamp()]
    if service_key is not None:
        assignments.append("service_key = ?")
        values.append(service_key)
    if stack_name is not None:
        assignments.append("stack_name = ?")
        values.append(stack_name)
    if service_name is not None:
        assignments.append("service_name = ?")
        values.append(service_name)
    if digest_provenance is not None:
        provenance = digest_provenance.sql_values()
        for column in DIGEST_PROVENANCE_SQL_COLUMNS:
            assignments.append(f"{column} = ?")
            values.append(provenance[column])
    values.extend([run_id, line_no])
    with conn:
        conn.execute(
            f"""
            UPDATE pending_updates
            SET {", ".join(assignments)}
            WHERE run_id = ?
              AND line_no = ?
            """,
            values,
        )


def upsert_known_image(
    conn: sqlite3.Connection,
    *,
    service_key: str,
    image: str,
    image_id: str = "",
    digest: str = "",
    updated_at: str | None = None,
    metadata_json: str = "{}",
    digest_provenance: DigestTagProvenance | None = None,
    commit: bool = True,
) -> None:
    """Record the latest known image state, optionally joining the caller's transaction."""

    updated = updated_at or utc_timestamp()
    provenance = digest_provenance_or_empty(digest_provenance)
    with (conn if commit else nullcontext()):
        conn.execute(
            """
            INSERT INTO known_images (
                service_key,
                image,
                image_id,
                digest,
                updated_at,
                metadata_json,
                digest_source_image,
                digest_resolved_tag,
                digest_watch_tag,
                digest_target_digest,
                digest_final_image,
                digest_provenance_source,
                digest_provenance_confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(service_key) DO UPDATE SET
                image = excluded.image,
                image_id = excluded.image_id,
                digest = excluded.digest,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json,
                digest_source_image = excluded.digest_source_image,
                digest_resolved_tag = excluded.digest_resolved_tag,
                digest_watch_tag = excluded.digest_watch_tag,
                digest_target_digest = excluded.digest_target_digest,
                digest_final_image = excluded.digest_final_image,
                digest_provenance_source = excluded.digest_provenance_source,
                digest_provenance_confidence = excluded.digest_provenance_confidence
            """,
            (
                service_key,
                image,
                image_id,
                digest,
                updated,
                metadata_json,
                provenance["digest_source_image"],
                provenance["digest_resolved_tag"],
                provenance["digest_watch_tag"],
                provenance["digest_target_digest"],
                provenance["digest_final_image"],
                provenance["digest_provenance_source"],
                provenance["digest_provenance_confidence"],
            ),
        )


def upsert_tag_exclusion_rule(
    conn: sqlite3.Connection,
    *,
    scope: str,
    image_repo: str,
    service_key: str = "",
    match_type: str = "exact",
    tag: str,
    regex_fragment: str,
    status: str = "active",
    created_at: str | None = None,
    updated_at: str | None = None,
    metadata_json: str = "{}",
) -> int:
    """Store or refresh one WUD tag exclusion rule."""

    now = utc_timestamp()
    created = created_at or now
    updated = updated_at or now
    with conn:
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, image_repo, service_key, match_type, tag)
            DO UPDATE SET
                regex_fragment = excluded.regex_fragment,
                status = excluded.status,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                scope,
                image_repo,
                service_key,
                match_type,
                tag,
                regex_fragment,
                status,
                created,
                updated,
                metadata_json,
            ),
        )
    with closing(conn.execute(
        """
        SELECT id
        FROM tag_exclusion_rules
        WHERE scope = ?
          AND image_repo = ?
          AND service_key = ?
          AND match_type = ?
          AND tag = ?
        LIMIT 1
        """,
        (scope, image_repo, service_key, match_type, tag),
    )) as cursor:
        row = cursor.fetchone()
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])


def active_tag_exclusion_rules(
    conn: sqlite3.Connection,
    *,
    image_repo: str,
    service_key: str = "",
    match_type: str = "exact",
) -> tuple[sqlite3.Row, ...]:
    """Return active exact exclusions for an image repo and optional service."""

    with closing(conn.execute(
        """
        SELECT *
        FROM tag_exclusion_rules
        WHERE status = 'active'
          AND image_repo = ?
          AND match_type = ?
          AND (
                (scope = 'image_repo' AND service_key = '')
             OR (scope = 'service' AND service_key = ?)
          )
        ORDER BY tag COLLATE BINARY
        """,
        (image_repo, match_type, service_key),
    )) as cursor:
        return tuple(cursor.fetchall())
