"""WebUI Discord release-note notification route handlers."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from threading import Event, Thread
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request

from . import (
    web_discord,
    web_pending_sources,
    web_release_notification_state,
    web_wud_api,
    web_wud_refresh,
)
from .config import VALID_UPDATE_MODES
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .release_notes import refresh_release_notes
from .web_auth import request_actor_type as _request_actor_type
from .web_database import ReadOnlyDatabaseMissing, connect_readonly_db
from .web_discord import (
    DISCORD_COLOR,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_CATEGORIES,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_FOOTER,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_REASON_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_ROW_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_SUBJECT_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_DIGEST_VERSION_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_EMBED_DESCRIPTION_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_MESSAGE_CONTENT_LIMIT,  # noqa: F401 - compatibility re-export
    DISCORD_SECURITY_COLOR,  # noqa: F401 - compatibility re-export
    DISCORD_SUPPRESS_EMBEDS_FLAG,  # noqa: F401 - compatibility re-export
    DISCORD_WEBHOOK_TIMEOUT_SECONDS,  # noqa: F401 - compatibility re-export
    DISCORD_WEBHOOK_USER_AGENT,  # noqa: F401 - compatibility re-export
    DISCORD_WEBHOOK_USERNAME,  # noqa: F401 - compatibility re-export
    SEMVER_PARTS_RE,  # noqa: F401 - compatibility re-export
    _NotificationTarget,
)
from .web_metadata import json_object
from .web_models import (
    PendingSourceInfo,
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseNotificationDestination,
    ReleaseNotificationItem,
    ReleaseNotificationPreviewRequest,
    ReleaseNotificationResponse,
    ReleaseNotificationSendRequest,
    ReleaseNotificationTestRequest,
    ReleaseNotificationTestResponse,
    ReleaseNotificationTrigger,
    WebSettings,
    WudApiStatus,
)
from .web_redaction import redact_sensitive_text as _redact_sensitive_text
from .web_redaction import (
    redact_unknown_absolute_paths as _redact_unknown_absolute_paths,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_release_notes import (
    release_note_source_resolver,
    release_notes_disabled_state,
)
from .web_release_notification_state import (
    RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND,
)
from .web_request_context import request_settings as _settings
from .web_settings import (
    effective_release_notes_enabled,
    effective_release_notification_config,
)
from .wud_file import WudTarget, parse_wud_text

RUN_NOTIFICATION_STATUS_REASON = "updated"
NO_RELEASE_NOTIFICATIONS_AVAILABLE_DETAIL = (
    "no release-note notifications are available to send"
)
MUTATIONS_DISABLED_DETAIL = "mutations are disabled"
RELEASE_NOTIFICATIONS_DISABLED_DETAIL = "release-note notifications are disabled"
DISCORD_WEBHOOK_NOT_CONFIGURED_DETAIL = (
    "Discord release-note webhook is not configured"
)
RELEASE_NOTIFICATION_POLL_SECONDS = 60.0
SCHEDULER_ACTOR_TYPE = "release-notification-scheduler"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _NotificationSource:
    targets: tuple[_NotificationTarget, ...]
    source_file: str
    source: PendingSourceInfo
    wud_api: WudApiStatus
    metadata_by_line: Mapping[int, web_wud_api.WudApiContainer]
    warnings: tuple[str, ...] = ()


def api_preview_release_notifications(
    payload: ReleaseNotificationPreviewRequest,
    request: Request,
) -> ReleaseNotificationResponse:
    settings = _settings(request)
    return preview_release_notifications(settings, payload)


def api_send_release_notifications(
    payload: ReleaseNotificationSendRequest,
    request: Request,
) -> ReleaseNotificationResponse:
    settings = _settings(request)
    return send_release_notifications(settings, payload, request=request)


def initialize_release_notification_scheduler_state(state: Any) -> None:
    state.web_release_notification_stop = Event()
    state.web_release_notification_thread = None


def shutdown_release_notification_scheduler_state(state: Any) -> None:
    stop_event: Event = state.web_release_notification_stop
    stop_event.set()
    thread = state.web_release_notification_thread
    if thread is not None:
        thread.join(timeout=1.0)


def start_release_notification_scheduler(
    app: FastAPI,
    settings: WebSettings,
) -> Thread | None:
    if not settings.mutations_enabled:
        return None
    existing_thread = app.state.web_release_notification_thread
    if existing_thread is not None and existing_thread.is_alive():
        return existing_thread
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
    stop_event = Event()
    app.state.web_release_notification_stop = stop_event
    thread = Thread(
        target=_release_notification_scheduler_loop,
        args=(settings, stop_event),
        name="wud-release-notification-scheduler",
        daemon=True,
    )
    app.state.web_release_notification_thread = thread
    thread.start()
    return thread


def _release_notification_scheduler_loop(
    settings: WebSettings,
    stop_event: Event,
) -> None:
    while not stop_event.wait(RELEASE_NOTIFICATION_POLL_SECONDS):
        try:
            poll_wud_api_release_notifications(settings)
        except Exception:
            LOGGER.exception("release notification scheduler tick failed")


def poll_wud_api_release_notifications(
    settings: WebSettings,
) -> ReleaseNotificationResponse | None:
    api_settings = cast(WebSettings, replace(settings, pending_source="api"))
    delivery_mode = effective_release_notification_config(api_settings).delivery_mode
    verified_only = delivery_mode == RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND
    try:
        require_release_notification_sendable(api_settings)
    except HTTPException as exc:
        if (
            exc.status_code in {403, 422}
            and exc.detail
            in {
                MUTATIONS_DISABLED_DETAIL,
                RELEASE_NOTIFICATIONS_DISABLED_DETAIL,
                DISCORD_WEBHOOK_NOT_CONFIGURED_DETAIL,
            }
        ):
            return None
        raise

    source = web_wud_refresh.refresh_wud_pending_source(
        settings,
        include_wud_metadata=True,
        force=True,
        api_source=True,
    ).source
    if (
        source.degraded
        or source.wud_snapshot is None
        or not source.wud_snapshot.status.metadata_available
    ):
        return None

    line_numbers = [target.line_no for target in source.parsed.targets]
    if not line_numbers:
        return None

    payload = ReleaseNotificationSendRequest(
        line_numbers=line_numbers,
        confirmation="send-release-notes",
    )
    try:
        return send_release_notifications(
            api_settings,
            payload,
            request=None,
            actor_type=SCHEDULER_ACTOR_TYPE,
            pending_source=source,
            verified_only=verified_only,
        )
    except HTTPException as exc:
        if exc.status_code == 422 and exc.detail == NO_RELEASE_NOTIFICATIONS_AVAILABLE_DETAIL:
            return None
        raise


def preview_release_notifications(
    settings: WebSettings,
    payload: ReleaseNotificationPreviewRequest,
    *,
    sent: bool = False,
) -> ReleaseNotificationResponse:
    return _notification_response(settings, payload, sent=sent)


def require_release_notification_sendable(settings: WebSettings) -> str:
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    if not effective_release_notes_enabled(settings):
        raise HTTPException(
            status_code=403,
            detail=RELEASE_NOTIFICATIONS_DISABLED_DETAIL,
        )
    webhook = web_discord._discord_webhook(settings)
    if not webhook.value:
        raise HTTPException(
            status_code=422,
            detail=DISCORD_WEBHOOK_NOT_CONFIGURED_DETAIL,
        )
    return webhook.value


def send_release_notifications(
    settings: WebSettings,
    payload: ReleaseNotificationSendRequest,
    *,
    request: Request | None,
    actor_type: str | None = None,
    pending_source: web_pending_sources.PendingSourceResult | None = None,
    verified_only: bool = False,
) -> ReleaseNotificationResponse:
    webhook = require_release_notification_sendable(settings)

    response = _notification_response(
        settings,
        payload,
        sent=False,
        pending_source=pending_source,
        verified_only=verified_only,
    )
    if response.sendable_count <= 0:
        raise HTTPException(
            status_code=422,
            detail=NO_RELEASE_NOTIFICATIONS_AVAILABLE_DETAIL,
        )

    audit_run_id = 0
    sent_batch_count = 0
    sent_count = 0
    try:
        sent_items: list[ReleaseNotificationItem] = []
        audit_run_id = _insert_release_notification_audit_start(
            settings,
            request,
            payload,
            response,
            actor_type=actor_type,
        )
        response = _reserve_release_notification_history(
            settings,
            response,
            audit_run_id,
        )
        if response.sendable_count <= 0:
            _finish_release_notification_audit(
                settings,
                audit_run_id,
                response,
                request=request,
                payload=payload,
                status="failure",
                sent_count=0,
                sent_batch_count=0,
                error=NO_RELEASE_NOTIFICATIONS_AVAILABLE_DETAIL,
                actor_type=actor_type,
            )
            raise HTTPException(
                status_code=422,
                detail=NO_RELEASE_NOTIFICATIONS_AVAILABLE_DETAIL,
            )
        for batch in web_discord._payload_batches(response.items, response.mode):
            web_discord._post_discord_payload(webhook, batch["payload"])
            sent_batch_count += 1
            sent_count += int(batch.get("count") or 0)
            batch_items = list(batch.get("items") or [])
            sent_items.extend(batch_items)
            _record_release_notification_history(
                settings,
                batch_items,
                response,
                audit_run_id,
                status="sent",
            )
        _finish_release_notification_audit(
            settings,
            audit_run_id,
            response,
            request=request,
            payload=payload,
            status="success",
            sent_count=sent_count,
            sent_batch_count=sent_batch_count,
            actor_type=actor_type,
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        detail = _safe_release_notification_exception_detail(settings, exc, webhook)
        if audit_run_id:
            sent_keys = {item.notification_key for item in sent_items}
            remaining_items = [
                item
                for item in response.items
                if not item.skipped_reason and item.notification_key not in sent_keys
            ]
            _safe_record_release_notification_history(
                settings,
                remaining_items,
                response,
                audit_run_id,
                status="failure",
            )
            _safe_finish_release_notification_audit_failure(
                settings,
                audit_run_id,
                response,
                request=request,
                payload=payload,
                error=detail,
                sent_count=sent_count,
                sent_batch_count=sent_batch_count,
                actor_type=actor_type,
            )
        raise HTTPException(
            status_code=500,
            detail=detail,
        ) from exc
    return response.model_copy(update={"sent": True, "audit_run_id": audit_run_id})


def api_test_release_notification_webhook(
    _payload: ReleaseNotificationTestRequest,
    request: Request,
) -> ReleaseNotificationTestResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail=MUTATIONS_DISABLED_DETAIL)
    webhook = web_discord._discord_webhook(settings)
    if not webhook.value:
        raise HTTPException(
            status_code=422,
            detail=DISCORD_WEBHOOK_NOT_CONFIGURED_DETAIL,
        )

    destination = ReleaseNotificationDestination(
        configured=True,
        source=webhook.source,
    )
    audit_run_id = 0
    try:
        audit_run_id = _insert_release_notification_test_audit_start(
            settings,
            request,
            destination,
        )
        web_discord._post_discord_payload(webhook.value, web_discord._test_discord_payload())
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        detail = _safe_release_notification_test_exception_detail(
            settings,
            exc,
            webhook.value,
        )
        if audit_run_id:
            _safe_finish_release_notification_test_audit_failure(
                settings,
                audit_run_id,
                request=request,
                destination=destination,
                error=detail,
            )
        raise HTTPException(status_code=500, detail=detail) from exc
    try:
        _finish_release_notification_test_audit(
            settings,
            audit_run_id,
            request=request,
            destination=destination,
            status="success",
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        detail = _safe_exception_detail(
            settings,
            "could not finalize Discord test webhook audit",
            exc,
        )
        _safe_finish_release_notification_test_audit_failure(
            settings,
            audit_run_id,
            request=request,
            destination=destination,
            error=detail,
        )
    return ReleaseNotificationTestResponse(
        sent=True,
        destination=destination,
        audit_run_id=audit_run_id,
    )


def _notification_response(
    settings: WebSettings,
    payload: ReleaseNotificationPreviewRequest,
    *,
    sent: bool,
    pending_source: web_pending_sources.PendingSourceResult | None = None,
    verified_only: bool = False,
) -> ReleaseNotificationResponse:
    enabled = effective_release_notes_enabled(settings)
    notification_config = effective_release_notification_config(settings)
    destination = _release_notification_destination(settings)
    if not enabled:
        disabled = release_notes_disabled_state(settings)
        return ReleaseNotificationResponse(
            enabled=False,
            mode=notification_config.mode,
            resend_policy=notification_config.resend_policy,
            destination=destination,
            source_file=str(settings.config.wud_out_file),
            source=disabled.source,
            wud_api=disabled.wud_api,
            warnings=[disabled.reason],
            sent=sent,
        )

    source = _notification_source(settings, payload, pending_source=pending_source)
    if not source.targets:
        return ReleaseNotificationResponse(
            enabled=True,
            mode=notification_config.mode,
            resend_policy=notification_config.resend_policy,
            destination=destination,
            source=source.source,
            source_file=source.source_file,
            wud_api=source.wud_api,
            warnings=list(source.warnings),
            sent=sent,
        )

    notes = _release_note_infos(settings, source)
    items, warnings = _notification_items(
        settings,
        source,
        notes,
        config=notification_config,
        resend=payload.resend,
        verified_only=verified_only,
    )
    sendable_count = sum(1 for item in items if not item.skipped_reason)
    batches = web_discord._payload_batches(items, notification_config.mode)
    return ReleaseNotificationResponse(
        enabled=True,
        mode=notification_config.mode,
        resend_policy=notification_config.resend_policy,
        destination=destination,
        source=source.source,
        source_file=source.source_file,
        count=len(items),
        sendable_count=sendable_count,
        skipped_count=len(items) - sendable_count,
        batch_count=len(batches),
        messages=web_discord._payload_messages(batches),
        items=items,
        wud_api=source.wud_api,
        warnings=[*source.warnings, *warnings],
        sent=sent,
    )


def _notification_source(
    settings: WebSettings,
    payload: ReleaseNotificationPreviewRequest,
    *,
    pending_source: web_pending_sources.PendingSourceResult | None = None,
) -> _NotificationSource:
    if payload.run_id is not None:
        return _run_notification_source(settings, payload.run_id)
    return _pending_notification_source(
        settings,
        payload.line_numbers,
        pending_source=pending_source,
    )


def _pending_notification_source(
    settings: WebSettings,
    line_numbers: Sequence[int],
    *,
    pending_source: web_pending_sources.PendingSourceResult | None = None,
) -> _NotificationSource:
    selected_line_numbers = _unique_line_numbers(line_numbers)
    try:
        source = pending_source or web_wud_refresh.refresh_wud_pending_source(
            settings,
            include_wud_metadata=True,
        ).source
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read pending source", exc),
        ) from exc
    requested = set(selected_line_numbers)
    metadata_by_line = dict(source.metadata_by_line or {})
    targets_by_line = {target.line_no: target for target in source.parsed.targets}
    targets: list[_NotificationTarget] = []
    warnings = list(source.warnings)
    for line_no in selected_line_numbers:
        target = targets_by_line.get(line_no)
        if target is None:
            warnings.append(f"Line {line_no} is not an actionable pending update.")
            continue
        metadata = metadata_by_line.get(line_no)
        targets.append(
            _NotificationTarget(
                target=target,
                service_key=target.key,
                wud_container_id="" if metadata is None else metadata.id,
            )
        )
    if not requested:
        warnings.append("No pending updates were selected.")
    return _NotificationSource(
        targets=tuple(targets),
        source_file=source.source_file,
        source=source.response_source(),
        wud_api=_wud_api_status(source.wud_snapshot.status if source.wud_snapshot else None),
        metadata_by_line=metadata_by_line,
        warnings=tuple(warnings),
    )


def _run_notification_source(settings: WebSettings, run_id: int) -> _NotificationSource:
    try:
        with closing(connect_readonly_db(settings)) as conn:
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
            if str(run["status"]) != "success":
                raise HTTPException(
                    status_code=422,
                    detail="release notifications require a successful run",
                )
            if int(run["dry_run"]) or str(run["mode"]) not in VALID_UPDATE_MODES:
                raise HTTPException(
                    status_code=422,
                    detail="release notifications require a successful update run",
                )
            rows = conn.execute(
                """
                SELECT *
                FROM pending_updates
                WHERE run_id = ?
                  AND status = 'resolved'
                  AND status_reason = ?
                ORDER BY line_no, id
                """,
                (run_id, RUN_NOTIFICATION_STATUS_REASON),
            ).fetchall()
    except ReadOnlyDatabaseMissing as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read run", exc),
        ) from exc

    targets: list[_NotificationTarget] = []
    warnings: list[str] = []
    for row in rows:
        parsed = parse_wud_text(str(row["raw"]))
        if not parsed.targets:
            warnings.append(f"Run #{run_id} line {row['line_no']} is not actionable.")
            continue
        target = parsed.targets[0]
        target = WudTarget(
            line_no=int(row["line_no"]),
            raw=target.raw,
            first=target.first,
            key=target.key,
            repo=target.repo,
            has_tag=target.has_tag,
            allow_repo=target.allow_repo,
            digest=target.digest,
            desired_tag=target.desired_tag,
            tag_token=target.tag_token,
        )
        targets.append(
            _NotificationTarget(
                target=target,
                service_key=str(row["service_key"]),
            )
        )
    return _NotificationSource(
        targets=tuple(targets),
        source_file=f"Run #{run_id}",
        source=PendingSourceInfo(
            configured=settings.pending_source,
            active="file",
            label=f"Run #{run_id}",
            fresh=True,
            degraded=False,
            detail="Release notifications built from persisted run records.",
        ),
        wud_api=_disabled_wud_api_status(),
        metadata_by_line={},
        warnings=tuple(warnings),
    )


def _unique_line_numbers(line_numbers: Sequence[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    selected: list[int] = []
    for line_no in line_numbers:
        if line_no in seen:
            raise HTTPException(
                status_code=422,
                detail=f"line_numbers line {line_no} was provided more than once",
            )
        seen.add(line_no)
        selected.append(line_no)
    return tuple(selected)


def _release_note_infos(
    settings: WebSettings,
    source: _NotificationSource,
) -> dict[int, ReleaseNoteInfo]:
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            infos = refresh_release_notes(
                conn,
                (item.target for item in source.targets),
                settings.command_env or {},
                source_resolver=release_note_source_resolver(
                    settings,
                    wud_metadata=dict(source.metadata_by_line),
                ),
                target_tag_resolver=web_wud_api.target_tag_resolver_from_metadata(
                    source.metadata_by_line,
                ),
                redact_error=lambda value: _redact_sensitive_text(settings, value),
            )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not refresh release-note metadata",
                exc,
            ),
        ) from exc
    return {item.line_no: item for item in infos}


def _notification_items(
    settings: WebSettings,
    source: _NotificationSource,
    notes: Mapping[int, ReleaseNoteInfo],
    *,
    config: web_release_notification_state.ReleaseNotificationConfig,
    resend: bool,
    verified_only: bool = False,
) -> tuple[list[ReleaseNotificationItem], list[str]]:
    candidates: list[ReleaseNotificationItem] = []
    identities: dict[int, web_release_notification_state.NotificationIdentity] = {}
    warnings: list[str] = []
    trigger_cache: dict[str, tuple[list[ReleaseNotificationTrigger], str]] = {}
    for target in source.targets:
        note = notes.get(target.target.line_no)
        if note is None:
            warnings.append(f"Line {target.target.line_no} has no release-note metadata.")
            continue
        if verified_only and note.security.outcome != "verified_critical_high":
            continue
        metadata = source.metadata_by_line.get(target.target.line_no)
        identity = web_release_notification_state.notification_identity(
            target.target,
            note,
            metadata,
        )
        identities[target.target.line_no] = identity
        triggers: list[ReleaseNotificationTrigger] = []
        if target.wud_container_id:
            if target.wud_container_id not in trigger_cache:
                trigger_cache[target.wud_container_id] = web_wud_api.container_triggers(
                    settings,
                    target.wud_container_id,
                )
            triggers, trigger_warning = trigger_cache[target.wud_container_id]
            if trigger_warning:
                warnings.append(
                    f"Line {target.target.line_no} WUD triggers unavailable: "
                    f"{trigger_warning}"
                )
        image_repo = note.image_repo or target.target.repo
        upstream_repo = note.upstream_repo
        current_version, target_version = web_discord._notification_versions(
            target,
            note,
            metadata,
        )
        category, reason_code, reason_label = web_discord._notification_digest_reason(
            target,
            note,
            metadata,
            current_version=current_version,
            target_version=target_version,
        )
        title, description = web_discord._notification_copy(
            settings,
            target,
            note,
            triggers,
            image_repo=image_repo,
            upstream_repo=upstream_repo,
            update_kind=str(getattr(metadata, "update_kind", "") or ""),
            verbosity=config.verbosity,
        )
        candidates.append(
            ReleaseNotificationItem(
                line_no=target.target.line_no,
                image=target.target.first,
                service_key=target.service_key,
                title=title,
                description=description,
                status=note.status,
                release_tag=note.release_tag,
                image_repo=image_repo,
                upstream_repo=upstream_repo,
                current_version=current_version,
                target_version=target_version,
                category=category,
                reason_code=reason_code,
                reason_label=reason_label,
                security={
                    "outcome": note.security.outcome,
                    "severity": note.security.severity,
                    "reason_code": note.security.reason_code,
                    "reason": note.security.reason,
                    "advisory_ids": note.security.advisory_ids,
                    "lookup_truncated": note.security.lookup_truncated,
                },
                links=_release_note_links(note),
                triggers=triggers,
                notification_key=identity.notification_key,
            )
        )
    urgent_lines = {
        item.line_no for item in candidates if item.category == "security_urgent"
    }
    regular_identities = {
        line_no: identity
        for line_no, identity in identities.items()
        if line_no not in urgent_lines
    }
    urgent_identities = {
        line_no: identity
        for line_no, identity in identities.items()
        if line_no in urgent_lines
    }
    annotations = _notification_annotations(
        settings,
        config,
        regular_identities,
        resend=resend,
    )
    annotations.update(
        _notification_annotations(
            settings,
            replace(config, resend_policy="remote_change"),
            urgent_identities,
            resend=resend,
        )
    )
    items: list[ReleaseNotificationItem] = []
    for item in candidates:
        annotation = annotations[item.line_no]
        items.append(
            item.model_copy(
                update={
                    "notification_status": annotation.status,
                    "notification_last_sent_at": annotation.last_sent_at,
                    "notification_send_count": annotation.send_count,
                    "skipped_reason": annotation.skipped_reason,
                }
            )
        )
    items.sort(key=lambda item: 0 if item.category == "security_urgent" else 1)
    return items, warnings


def _notification_annotations(
    settings: WebSettings,
    config: web_release_notification_state.ReleaseNotificationConfig,
    identities: Mapping[int, web_release_notification_state.NotificationIdentity],
    *,
    resend: bool,
) -> dict[int, web_release_notification_state.NotificationAnnotation]:
    if not identities:
        return {}
    try:
        with closing(connect_readonly_db(settings)) as conn:
            return web_release_notification_state.notification_annotations(
                conn,
                config,
                identities,
                resend=resend,
            )
    except (AttributeError, ReadOnlyDatabaseMissing):
        return web_release_notification_state.notification_annotations_from_history(
            config,
            identities,
            {},
            resend=resend,
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release notification history",
                exc,
            ),
        ) from exc


def _release_note_links(note: ReleaseNoteInfo) -> list[ReleaseNoteLink]:
    return [
        ReleaseNoteLink(
            label=str(link.label),
            url=str(link.url),
            kind=str(link.kind),
        )
        for link in note.links
    ]


def _release_notification_destination(
    settings: WebSettings,
) -> ReleaseNotificationDestination:
    webhook = web_discord._discord_webhook(settings)
    return ReleaseNotificationDestination(
        configured=bool(webhook.value),
        source=webhook.source,
    )


def _safe_release_notification_exception_detail(
    settings: WebSettings,
    exc: BaseException,
    webhook_url: str,
) -> str:
    detail = _redact_sensitive_text(settings, str(exc), extra_secrets=(webhook_url,))
    detail = _redact_unknown_absolute_paths(detail)
    return f"could not send Discord release-note notifications: {detail}"


def _safe_release_notification_test_exception_detail(
    settings: WebSettings,
    exc: BaseException,
    webhook_url: str,
) -> str:
    detail = _redact_sensitive_text(settings, str(exc), extra_secrets=(webhook_url,))
    detail = _redact_unknown_absolute_paths(detail)
    return f"could not send Discord test webhook: {detail}"


def _insert_release_notification_test_audit_start(
    settings: WebSettings,
    request: Request,
    destination: ReleaseNotificationDestination,
) -> int:
    now = utc_timestamp()
    metadata = _release_notification_test_audit_metadata(
        settings,
        request,
        destination,
        status="running",
    )
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
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
                VALUES (?, NULL, 'running', 0, 'web-release-notifications', ?, '', ?)
                """,
                (
                    now,
                    str(settings.config.wud_out_file),
                    json_object(metadata),
                ),
            )
            return int(cursor.lastrowid)


def _finish_release_notification_test_audit(
    settings: WebSettings,
    run_id: int,
    *,
    request: Request,
    destination: ReleaseNotificationDestination,
    status: str,
    error: str = "",
) -> None:
    now = utc_timestamp()
    metadata = _release_notification_test_audit_metadata(
        settings,
        request,
        destination,
        status=status,
        error=error,
    )
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
                  AND mode = 'web-release-notifications'
                """,
                (now, status, json_object(metadata), run_id),
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
                VALUES (?, ?, 'release-notes', 'webui', 'discord', 'discord-test', ?, ?)
                """,
                (
                    run_id,
                    now,
                    status,
                    json_object(metadata),
                ),
            )


def _safe_finish_release_notification_test_audit_failure(
    settings: WebSettings,
    run_id: int,
    *,
    request: Request,
    destination: ReleaseNotificationDestination,
    error: str,
) -> None:
    try:
        _finish_release_notification_test_audit(
            settings,
            run_id,
            request=request,
            destination=destination,
            status="failure",
            error=error,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to finalize Discord test webhook audit")


def _release_notification_test_audit_metadata(
    settings: WebSettings,
    request: Request,
    destination: ReleaseNotificationDestination,
    *,
    status: str,
    error: str = "",
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source": "webui",
        "operation": "test_release_notification_webhook",
        "actor_type": _request_actor_type(settings, request),
        "resource_type": "release_notifications",
        "resource_id": "discord",
        "status": status,
        "destination": {
            "type": destination.type,
            "source": destination.source,
        },
    }
    if error:
        metadata["error"] = error
    return metadata


def _insert_release_notification_audit_start(
    settings: WebSettings,
    request: Request | None,
    payload: ReleaseNotificationPreviewRequest,
    response: ReleaseNotificationResponse,
    *,
    actor_type: str | None = None,
) -> int:
    now = utc_timestamp()
    metadata = _release_notification_audit_metadata(
        settings,
        request,
        payload,
        response,
        status="running",
        sent_count=0,
        sent_batch_count=0,
        actor_type=actor_type,
    )
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
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
                VALUES (?, NULL, 'running', 0, 'web-release-notifications', ?, '', ?)
                """,
                (
                    now,
                    response.source_file or str(settings.config.wud_out_file),
                    json_object(metadata),
                ),
            )
            return int(cursor.lastrowid)


def _finish_release_notification_audit(
    settings: WebSettings,
    run_id: int,
    response: ReleaseNotificationResponse,
    *,
    request: Request | None,
    payload: ReleaseNotificationPreviewRequest,
    status: str,
    sent_count: int,
    sent_batch_count: int,
    error: str = "",
    actor_type: str | None = None,
) -> None:
    now = utc_timestamp()
    metadata = _release_notification_audit_metadata(
        settings,
        request,
        payload,
        response,
        status=status,
        sent_count=sent_count,
        sent_batch_count=sent_batch_count,
        error=error,
        actor_type=actor_type,
    )
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
                  AND mode = 'web-release-notifications'
                """,
                (now, status, json_object(metadata), run_id),
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
                VALUES (?, ?, 'release-notes', 'webui', 'discord', 'discord', ?, ?)
                """,
                (
                    run_id,
                    now,
                    status,
                    json_object({**metadata, "items": _audit_items(response.items)}),
                ),
            )


def _safe_finish_release_notification_audit_failure(
    settings: WebSettings,
    run_id: int,
    response: ReleaseNotificationResponse,
    *,
    request: Request | None,
    payload: ReleaseNotificationPreviewRequest,
    error: str,
    sent_count: int,
    sent_batch_count: int,
    actor_type: str | None = None,
) -> None:
    try:
        _finish_release_notification_audit(
            settings,
            run_id,
            response,
            request=request,
            payload=payload,
            status="failure",
            sent_count=sent_count,
            sent_batch_count=sent_batch_count,
            error=error,
            actor_type=actor_type,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to finalize Discord release-note notification audit")


def _record_release_notification_history(
    settings: WebSettings,
    items: Sequence[ReleaseNotificationItem],
    response: ReleaseNotificationResponse,
    audit_run_id: int,
    *,
    status: str,
) -> None:
    config = _release_notification_config(response)
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            for item in items:
                if not item.notification_key:
                    continue
                web_release_notification_state.upsert_notification_history(
                    conn,
                    identity=_release_notification_identity(item),
                    config=config,
                    status=status,
                    audit_run_id=audit_run_id,
                )


def _safe_record_release_notification_history(
    settings: WebSettings,
    items: Sequence[ReleaseNotificationItem],
    response: ReleaseNotificationResponse,
    audit_run_id: int,
    *,
    status: str,
) -> None:
    try:
        _record_release_notification_history(
            settings,
            items,
            response,
            audit_run_id,
            status=status,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to record Discord release-note notification history")


def _reserve_release_notification_history(
    settings: WebSettings,
    response: ReleaseNotificationResponse,
    audit_run_id: int,
) -> ReleaseNotificationResponse:
    config = _release_notification_config(response)
    items: list[ReleaseNotificationItem] = []
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            for item in response.items:
                if item.skipped_reason or not item.notification_key:
                    items.append(item)
                    continue
                allow_existing = item.notification_status in {
                    "manual_resend",
                    "cooldown_ready",
                }
                reserved = web_release_notification_state.reserve_notification_history(
                    conn,
                    identity=_release_notification_identity(item),
                    config=config,
                    audit_run_id=audit_run_id,
                    allow_existing=allow_existing,
                )
                items.append(item if reserved else _release_notification_reserved_item(item))
    return _release_notification_response_with_items(response, items)


def _release_notification_config(
    response: ReleaseNotificationResponse,
) -> web_release_notification_state.ReleaseNotificationConfig:
    return web_release_notification_state.ReleaseNotificationConfig(
        mode=response.mode,
        resend_policy=response.resend_policy,
    )


def _release_notification_identity(
    item: ReleaseNotificationItem,
) -> web_release_notification_state.NotificationIdentity:
    return web_release_notification_state.NotificationIdentity(
        notification_key=item.notification_key,
        metadata={
            "line_no": item.line_no,
            "image": item.image,
            "service_key": item.service_key,
            "status": item.status,
            "release_tag": item.release_tag,
            "image_repo": item.image_repo,
            "upstream_repo": item.upstream_repo,
            "security": {
                "outcome": item.security.outcome,
                "severity": item.security.severity,
                "advisory_ids": item.security.advisory_ids,
            },
        },
    )


def _release_notification_reserved_item(
    item: ReleaseNotificationItem,
) -> ReleaseNotificationItem:
    return item.model_copy(
        update={
            "notification_status": "sending",
            "skipped_reason": "Notification is already being sent.",
        }
    )


def _release_notification_response_with_items(
    response: ReleaseNotificationResponse,
    items: Sequence[ReleaseNotificationItem],
) -> ReleaseNotificationResponse:
    sendable_count = sum(1 for item in items if not item.skipped_reason)
    batches = web_discord._payload_batches(items, response.mode)
    return response.model_copy(
        update={
            "count": len(items),
            "sendable_count": sendable_count,
            "skipped_count": len(items) - sendable_count,
            "batch_count": len(batches),
            "messages": web_discord._payload_messages(batches),
            "items": list(items),
        }
    )


def _release_notification_audit_metadata(
    settings: WebSettings,
    request: Request | None,
    payload: ReleaseNotificationPreviewRequest,
    response: ReleaseNotificationResponse,
    *,
    status: str,
    sent_count: int,
    sent_batch_count: int,
    error: str = "",
    actor_type: str | None = None,
) -> dict[str, object]:
    target: dict[str, object] = {}
    if payload.run_id is not None:
        target["run_id"] = payload.run_id
    else:
        target["line_numbers"] = list(payload.line_numbers)
    actor_context = actor_type or (
        "system" if request is None else _request_actor_type(settings, request)
    )
    metadata: dict[str, object] = {
        "source": "webui" if request is not None else actor_context,
        "operation": "send_release_notifications",
        "actor_type": actor_context,
        "resource_type": "release_notifications",
        "resource_id": "discord",
        "status": status,
        "target": target,
        "mode": response.mode,
        "resend_policy": response.resend_policy,
        "resend": payload.resend,
        "destination": {
            "type": response.destination.type,
            "source": response.destination.source,
        },
        "sent_count": sent_count,
        "skipped_count": response.skipped_count,
        "batch_count": response.batch_count,
        "sent_batch_count": sent_batch_count,
    }
    if error:
        metadata["error"] = error
    return metadata


def _audit_items(items: Sequence[ReleaseNotificationItem]) -> list[dict[str, object]]:
    return [
        {
            "line_no": item.line_no,
            "image": item.image,
            "service_key": item.service_key,
            "status": item.status,
            "release_tag": item.release_tag,
            "image_repo": item.image_repo,
            "upstream_repo": item.upstream_repo,
            "trigger_count": len(item.triggers),
            "notification_key": item.notification_key,
            "notification_status": item.notification_status,
            "notification_last_sent_at": item.notification_last_sent_at,
            "notification_send_count": item.notification_send_count,
            "skipped_reason": item.skipped_reason,
            "security_outcome": item.security.outcome,
            "security_severity": item.security.severity,
            "security_advisory_ids": item.security.advisory_ids,
        }
        for item in items
    ]


def _wud_api_status(status: WudApiStatus | None) -> WudApiStatus:
    if status is not None:
        return status
    return _disabled_wud_api_status()


def _disabled_wud_api_status() -> WudApiStatus:
    return WudApiStatus(
        state="unavailable",
        available=False,
        metadata_available=False,
        last_checked_at="",
    )
