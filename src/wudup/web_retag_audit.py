"""Retag audit records and verified known-image persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from .db import (
    init_db,
    insert_update_event,
    insert_update_run,
    open_db,
    upsert_known_image,
    utc_timestamp,
)
from .db_schema import EXPECTED_SCHEMA
from .digest_provenance import DigestTagProvenance
from .web_database import immediate_transaction as _immediate_transaction
from .web_metadata import json_object as _json_object
from .web_models import WebSettings
from .web_retag_plans import (
    RetagPlanBuild as _RetagPlanBuild,
)
from .web_retag_plans import (
    RetagPlanUpdate as _RetagPlanUpdate,
)
from .web_retag_plans import (
    retag_plan_digest_update as _retag_plan_digest_update,
)
from .web_retag_plans import (
    retag_plan_tag_update as _retag_plan_tag_update,
)
from .web_retag_plans import (
    retag_update_identity as _retag_update_identity,
)

# Every column, so a rollback restores whole rows as the schema grows.
_KNOWN_IMAGE_COLUMNS = tuple(column[0] for column in EXPECTED_SCHEMA["known_images"])


@dataclass(frozen=True)
class RetagKnownImageChange:
    """One known-image row written by a retag, with the row it replaced."""

    service_key: str
    previous: tuple[object, ...] | None
    recorded: tuple[object, ...]


def _record_successful_retag_known_images(
    settings: WebSettings,
    updates: Sequence[_RetagPlanUpdate],
) -> tuple[RetagKnownImageChange, ...]:
    """Record one stack's known images atomically and return what changed."""
    if not updates:
        return ()
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        previous: dict[str, tuple[object, ...] | None] = {}
        with _immediate_transaction(conn):
            for item in updates:
                if item.known_image_service_key_ambiguous:
                    continue
                if item.service_key not in previous:
                    previous[item.service_key] = _known_image_row(
                        conn, item.service_key
                    )
                upsert_known_image(
                    conn,
                    service_key=item.service_key,
                    image=item.update.final_image,
                    digest=item.update.planned_digest,
                    metadata_json=_json_object(
                        {
                            "source": "webui",
                            "operation": "retag",
                        }
                    ),
                    digest_provenance=_retag_digest_provenance(
                        item,
                        confidence="verified",
                    ),
                    commit=False,
                )
            changes: list[RetagKnownImageChange] = []
            for service_key, previous_row in previous.items():
                recorded = _known_image_row(conn, service_key)
                if recorded is None:
                    raise RuntimeError(
                        f"known image record for {service_key} was not saved"
                    )
                changes.append(
                    RetagKnownImageChange(
                        service_key=service_key,
                        previous=previous_row,
                        recorded=recorded,
                    )
                )
        return tuple(changes)


def _restore_retag_known_images(
    settings: WebSettings,
    changes: Sequence[RetagKnownImageChange],
) -> None:
    """Put back the rows replaced by a rolled-back retag.

    A row is only restored while it still holds the value this retag wrote, so
    a newer record from another writer is never overwritten.
    """
    if not changes:
        return
    placeholders = ", ".join("?" for _ in _KNOWN_IMAGE_COLUMNS)
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with _immediate_transaction(conn):
            for change in changes:
                if _known_image_row(conn, change.service_key) != change.recorded:
                    continue
                if change.previous is None:
                    conn.execute(
                        "DELETE FROM known_images WHERE service_key = ?",
                        (change.service_key,),
                    )
                    continue
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO known_images ({", ".join(_KNOWN_IMAGE_COLUMNS)})
                    VALUES ({placeholders})
                    """,
                    change.previous,
                )


def _known_image_row(
    conn: sqlite3.Connection,
    service_key: str,
) -> tuple[object, ...] | None:
    row = conn.execute(
        f"""
        SELECT {", ".join(_KNOWN_IMAGE_COLUMNS)}
        FROM known_images
        WHERE service_key = ?
        """,
        (service_key,),
    ).fetchone()
    return None if row is None else tuple(row)


def _insert_retag_audit_run(
    settings: WebSettings,
    build: _RetagPlanBuild,
    *,
    status: str,
) -> int:
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        return insert_update_run(
            conn,
            status=status,
            dry_run=False,
            mode="web-retag",
            wud_file=str(settings.config.wud_out_file),
            metadata_json=_json_object(_retag_audit_metadata(build, status=status)),
        )


def _finish_retag_audit_run(
    settings: WebSettings,
    run_id: int,
    build: _RetagPlanBuild,
    *,
    status: str,
    error: str = "",
    successful_updates: Sequence[_RetagPlanUpdate] = (),
    retained_known_image_updates: Sequence[_RetagPlanUpdate] = (),
) -> None:
    metadata = _retag_audit_metadata(build, status=status, error=error)
    successful_update_ids = {
        _retag_update_identity(item) for item in successful_updates
    }
    retained_known_image_ids = {
        _retag_update_identity(item) for item in retained_known_image_updates
    }
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        now = utc_timestamp()
        for item in build.updates:
            item_status = (
                "success"
                if status == "success"
                or _retag_update_identity(item) in successful_update_ids
                else "failure"
            )
            event_metadata: dict[str, object] = {
                "source": "webui",
                "operation": "retag",
                "service_key": item.service_key,
                "target_id": item.target_id,
                "resolved_tag": item.update.resolved_tag,
                "watch_tag": item.update.watch_tag,
                "known_image_recorded": (
                    (
                        item_status == "success"
                        or _retag_update_identity(item) in retained_known_image_ids
                    )
                    and not item.known_image_service_key_ambiguous
                ),
            }
            if item_status == "success" and item.known_image_service_key_ambiguous:
                event_metadata["known_image_skip_reason"] = (
                    "duplicate service_key"
                )
            insert_update_event(
                conn,
                run_id=run_id,
                created_at=now,
                service_name=item.update.services[0] if item.update.services else "",
                stack_name=item.stack.name,
                image=item.update.old_image,
                target_image=item.update.final_image,
                status=item_status,
                metadata_json=_json_object(event_metadata),
                digest_provenance=_retag_digest_provenance(
                    item,
                    confidence=(
                        "verified" if item_status == "success" else "planned"
                    ),
                ),
            )
        conn.execute(
            """
            UPDATE update_runs
            SET finished_at = ?, status = ?, metadata_json = ?
            WHERE id = ?
            """,
            (now, status, _json_object(metadata), run_id),
        )
        conn.commit()


def _retag_audit_metadata(
    build: _RetagPlanBuild,
    *,
    status: str,
    error: str = "",
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source": "webui",
        "operation": "retag",
        "status": status,
        "plan_id": build.response.plan_id,
        "services": [item.service_key for item in build.updates],
        "targets": [item.target_id for item in build.updates],
        "external_recreate_required": False,
        "retag_digest_pins": any(item.digest_pin for item in build.updates),
        "tag_updates": [
            _retag_plan_tag_update(item).model_dump(mode="json")
            for item in build.updates
            if not item.digest_pin
        ],
        "digest_pin_updates": [
            _retag_plan_digest_update(item).model_dump(mode="json")
            for item in build.updates
            if item.digest_pin
        ],
    }
    if error:
        metadata["error"] = error
    return metadata


def _retag_digest_provenance(
    item: _RetagPlanUpdate,
    *,
    confidence: str,
) -> DigestTagProvenance | None:
    if not item.digest_pin:
        return None
    return DigestTagProvenance(
        source_image=item.update.old_image,
        resolved_tag=item.update.resolved_tag,
        watch_tag=item.update.watch_tag,
        target_digest=item.update.planned_digest,
        final_image=item.update.final_image,
        provenance_source="retag",
        provenance_confidence=confidence,
    )
