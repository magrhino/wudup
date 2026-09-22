"""Release-note SQLite persistence, legacy row decoding, and freshness policy.

Owns cache reads, digest pruning, upserts, and success/error retry TTLs. Provider
classification and security decoding are pure collaborators; cache reads never
fetch remote data."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone

from .lsio_updates import (
    LSIOUpdateClassification,
    classification_from_mapping,
    classify_lsio_update,
)
from .release_note_models import (
    ReleaseNoteContext,
    ReleaseNoteInfo,
    ReleaseNoteLink,
)
from .release_note_providers import (
    _classify_context,
    _github_release_link_tag,
)
from .release_note_security import (
    SECURITY_RETRYABLE_REASON_CODES,
    security_assessment_from_mapping,
)

SUCCESS_CACHE_TTL_SECONDS = 21_600
ERROR_CACHE_TTL_SECONDS = 900


def _cached_info(conn: sqlite3.Connection, context: ReleaseNoteContext) -> ReleaseNoteInfo:
    if context.provider == "unsupported":
        return _placeholder_info(context)
    row = conn.execute(
        """
        SELECT *
        FROM release_note_cache
        WHERE cache_key = ?
        """,
        (context.cache_key,),
    ).fetchone()
    if row is None:
        return _placeholder_info(context)
    return _row_to_info(row, line_no=context.line_no)


def _placeholder_info(context: ReleaseNoteContext) -> ReleaseNoteInfo:
    if context.provider == "unsupported":
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="unsupported",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            error=context.error,
            classification=_classify_context(context),
        )
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="missing",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        classification=_classify_context(context),
    )


def _row_to_info(row: sqlite3.Row, *, line_no: int) -> ReleaseNoteInfo:
    metadata = _json_object(str(row["metadata_json"]))
    classification = classification_from_mapping(metadata.get("classification"))
    if "classification" not in metadata:
        classification = _classify_cache_row(row)
    security = security_assessment_from_mapping(metadata.get("security"))
    return ReleaseNoteInfo(
        line_no=line_no,
        status=str(row["status"]),  # type: ignore[arg-type]
        provider=str(row["provider"]),
        image_repo=str(row["image_repo"]),
        upstream_repo=str(row["upstream_repo"]),
        release_tag=str(row["release_tag"]),
        title=str(row["title"]),
        published_at=str(row["published_at"]),
        breaking=bool(row["breaking"]),
        breaking_reasons=_json_list(str(row["breaking_reasons_json"])),
        links=[
            ReleaseNoteLink(
                label=str(item.get("label", "")),
                url=str(item.get("url", "")),
                kind=str(item.get("kind", "")),
            )
            for item in _json_object_list(str(row["links_json"]))
        ],
        refreshed_at=str(row["updated_at"]),
        error=str(row["error"]),
        body=str(row["body"]),
        classification=classification,
        security=security,
    )


def _cache_metadata_incomplete(
    conn: sqlite3.Connection,
    context: ReleaseNoteContext,
) -> bool:
    row = conn.execute(
        "SELECT metadata_json FROM release_note_cache WHERE cache_key = ?",
        (context.cache_key,),
    ).fetchone()
    if row is None:
        return False
    metadata = _json_object(str(row["metadata_json"]))
    return "classification" not in metadata or "security" not in metadata


def _classify_cache_row(row: sqlite3.Row) -> LSIOUpdateClassification:
    return classify_lsio_update(
        image_repo=str(row["image_repo"]),
        current_tag=str(row["current_tag"]),
        target_tag=str(row["target_tag"]),
        lsio_tag=_lsio_release_tag_from_links(str(row["links_json"])),
        upstream_version=str(row["release_tag"]),
    )


def _lsio_release_tag_from_links(raw: str) -> str:
    for item in _json_object_list(raw):
        if str(item.get("kind") or "") == "lsio_release":
            tag = _github_release_link_tag(str(item.get("url") or ""))
            if tag:
                return tag
    return ""


def _prune_digest_cache(
    conn: sqlite3.Connection,
    contexts: Iterable[ReleaseNoteContext],
) -> None:
    active_digests: dict[tuple[str, str, str, str, str], set[str]] = {}
    for context in contexts:
        if context.target_digest:
            identity = (
                context.provider,
                context.image_repo,
                context.upstream_repo,
                context.current_tag,
                context.target_tag,
            )
            active_digests.setdefault(identity, set()).add(context.target_digest)

    with conn:
        for identity, digests in active_digests.items():
            rows = conn.execute(
                """
                SELECT cache_key, target_digest
                FROM release_note_cache
                WHERE provider = ?
                  AND image_repo = ?
                  AND upstream_repo = ?
                  AND current_tag = ?
                  AND target_tag = ?
                  AND target_digest != ''
                """,
                identity,
            )
            conn.executemany(
                "DELETE FROM release_note_cache WHERE cache_key = ?",
                ((row["cache_key"],) for row in rows if row["target_digest"] not in digests),
            )


def _upsert_cache(
    conn: sqlite3.Connection,
    context: ReleaseNoteContext,
    info: ReleaseNoteInfo,
    timestamp: str,
) -> None:
    links_json = json.dumps([asdict(link) for link in info.links], sort_keys=True)
    reasons_json = json.dumps(info.breaking_reasons, sort_keys=True)
    metadata_json = json.dumps(
        {
            "line_no": context.line_no,
            "classification": asdict(info.classification),
            "security": asdict(info.security),
        },
        sort_keys=True,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO release_note_cache (
                cache_key,
                provider,
                image_repo,
                upstream_repo,
                current_tag,
                target_tag,
                status,
                release_tag,
                title,
                published_at,
                breaking,
                breaking_reasons_json,
                links_json,
                error,
                body,
                created_at,
                updated_at,
                metadata_json,
                target_digest
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                provider = excluded.provider,
                image_repo = excluded.image_repo,
                upstream_repo = excluded.upstream_repo,
                current_tag = excluded.current_tag,
                target_tag = excluded.target_tag,
                status = excluded.status,
                release_tag = excluded.release_tag,
                title = excluded.title,
                published_at = excluded.published_at,
                breaking = excluded.breaking,
                breaking_reasons_json = excluded.breaking_reasons_json,
                links_json = excluded.links_json,
                error = excluded.error,
                body = excluded.body,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json,
                target_digest = excluded.target_digest
            """,
            (
                context.cache_key,
                context.provider,
                context.image_repo,
                context.upstream_repo,
                context.current_tag,
                context.target_tag,
                info.status,
                info.release_tag,
                info.title,
                info.published_at,
                1 if info.breaking else 0,
                reasons_json,
                links_json,
                info.error,
                info.body,
                timestamp,
                timestamp,
                metadata_json,
                context.target_digest,
            ),
        )


def _cache_stale(info: ReleaseNoteInfo, now: str) -> bool:
    if not info.refreshed_at:
        return True
    try:
        updated = datetime.fromisoformat(info.refreshed_at)
        current = datetime.fromisoformat(now)
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    retryable_security = info.security.reason_code in SECURITY_RETRYABLE_REASON_CODES
    ttl = (
        ERROR_CACHE_TTL_SECONDS
        if info.status == "error" or retryable_security
        else SUCCESS_CACHE_TTL_SECONDS
    )
    return (current - updated).total_seconds() >= ttl


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_object_list(raw: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
