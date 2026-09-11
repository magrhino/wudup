"""WebUI Discord release-note notification route handlers."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from threading import Event, Thread
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request

from . import (
    web_pending_sources,
    web_release_notification_state,
    web_wud_api,
    web_wud_refresh,
)
from .config import VALID_UPDATE_MODES
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .images import image_repo_ref, image_tag
from .release_notes import refresh_release_notes
from .web_auth import (
    _redact_sensitive_text,
    _redact_unknown_absolute_paths,
    _request_actor_type,
    _safe_exception_detail,
    _settings,
)
from .web_database import ReadOnlyDatabaseMissing, connect_readonly_db
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
from .web_release_notes import (
    release_note_source_resolver,
    release_notes_disabled_state,
)
from .web_release_notification_state import (
    RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND,
)
from .web_settings import (
    effective_release_notes_enabled,
    effective_release_notification_config,
    effective_release_notification_webhook,
)
from .wud_file import WudTarget, is_digest_target_line, parse_wud_text

DISCORD_MESSAGE_CONTENT_LIMIT = 2000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_DIGEST_ROW_LIMIT = 1500
DISCORD_DIGEST_SUBJECT_LIMIT = 160
DISCORD_DIGEST_VERSION_LIMIT = 128
DISCORD_DIGEST_REASON_LIMIT = 160
DISCORD_WEBHOOK_TIMEOUT_SECONDS = 10.0
DISCORD_WEBHOOK_USER_AGENT = "wudup-webui-release-notifications/1.0"
DISCORD_WEBHOOK_USERNAME = "WUDup Release Notes"
DISCORD_SUPPRESS_EMBEDS_FLAG = 1 << 2
DISCORD_COLOR = 0x57F287
DISCORD_SECURITY_COLOR = 0xED4245
DISCORD_DIGEST_FOOTER = "Open WUDup for full notes, digests, and apply plan."
DISCORD_DIGEST_CATEGORIES = (
    ("security_urgent", "🛡️ Critical/High security"),
    ("needs_review", "⚠️ Needs review"),
    ("worth_noting", "🟡 Worth noting"),
    ("routine", "🟢 Routine"),
)
SEMVER_PARTS_RE = re.compile(
    r"(?<![\dA-Za-z.])v?(\d{1,10})[.](\d{1,10})(?:[.](\d{1,10}))?",
    re.ASCII,
)
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
class _NotificationTarget:
    target: WudTarget
    service_key: str = ""
    wud_container_id: str = ""


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
    webhook = _discord_webhook(settings)
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
        for batch in _payload_batches(response.items, response.mode):
            _post_discord_payload(webhook, batch["payload"])
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
    webhook = _discord_webhook(settings)
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
        _post_discord_payload(webhook.value, _test_discord_payload())
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
    batches = _payload_batches(items, notification_config.mode)
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
        messages=_payload_messages(batches),
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
        current_version, target_version = _notification_versions(
            target,
            note,
            metadata,
        )
        category, reason_code, reason_label = _notification_digest_reason(
            target,
            note,
            metadata,
            current_version=current_version,
            target_version=target_version,
        )
        title, description = _notification_copy(
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


def _notification_versions(
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    metadata: web_wud_api.WudApiContainer | None,
) -> tuple[str, str]:
    current_version = str(getattr(metadata, "local_tag", "") or "")
    target_version = str(getattr(metadata, "remote_tag", "") or "")
    digest_update = _is_digest_update(target, metadata)
    if not current_version:
        current_version = image_tag(target.target.first) or target.target.tag_token
    if not target_version:
        target_version = (
            "new digest"
            if digest_update
            else target.target.desired_tag or note.release_tag
        )
    if (
        _should_annotate_latest_release(current_version, target_version)
        and note.release_tag
        and note.release_tag.lower() != "latest"
    ):
        target_version = f"latest (release {note.release_tag})"
    if not current_version:
        current_version = "current image"
    if not target_version:
        target_version = "updated image"
    return current_version, target_version


def _is_digest_update(
    target: _NotificationTarget,
    metadata: web_wud_api.WudApiContainer | None,
) -> bool:
    return (
        str(getattr(metadata, "update_kind", "") or "") == "digest"
        or is_digest_target_line(target.target)
    )


def _should_annotate_latest_release(
    current_version: str,
    target_version: str,
) -> bool:
    return target_version.lower() == "latest" or (
        current_version.lower() == "latest" and target_version == "new digest"
    )


def _notification_digest_reason(
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    metadata: web_wud_api.WudApiContainer | None,
    *,
    current_version: str,
    target_version: str,
) -> tuple[str, str, str]:
    security = note.security
    if security.outcome == "verified_critical_high":
        advisory_label = ", ".join(security.advisory_ids[:3])
        label = f"{security.severity.title()} security update"
        if advisory_label:
            label = f"{label} ({advisory_label})"
        return "security_urgent", "verified_security", label
    if security.outcome == "needs_review":
        return "needs_review", "security_needs_review", "security update needs review"
    semver_diff = str(getattr(metadata, "semver_diff", "") or "").lower()
    if not semver_diff:
        semver_diff = _semver_diff(current_version, target_version)
    classification = getattr(note, "classification", None)
    change_type = str(getattr(classification, "change_type", "") or "")
    provider_prefix = "LSIO image update: " if note.provider == "lsio" else ""

    if note.breaking:
        return (
            "needs_review",
            "breaking_change",
            f"{provider_prefix}possible breaking change",
        )
    if semver_diff == "major":
        return "needs_review", "major_bump", f"{provider_prefix}major version bump"
    status_reasons = {
        "error": ("release_notes_error", "release-note lookup failed"),
        "unsupported": ("release_notes_unsupported", "release notes unsupported"),
        "missing": ("release_notes_missing", "release notes unavailable"),
        "not_found": ("release_notes_not_found", "matching release not found"),
    }
    if note.status in status_reasons:
        code, label = status_reasons[note.status]
        return "needs_review", code, f"{provider_prefix}{label}"
    lsio_reason = _lsio_digest_reason(note.provider, change_type)
    mutable_latest_reason = _mutable_latest_digest_reason(
        current_version,
        target_version,
        note.provider,
        lsio_reason,
    )
    if mutable_latest_reason is not None:
        return mutable_latest_reason
    if not _has_release_or_changelog_link(note.links):
        return (
            "needs_review",
            "release_link_missing",
            f"{provider_prefix}release or changelog link unavailable",
        )
    if lsio_reason is not None:
        return "worth_noting", *lsio_reason
    if note.provider == "lsio":
        return "worth_noting", "lsio_update", "LSIO image update"
    if semver_diff == "minor":
        return "worth_noting", "minor_bump", "minor update with release notes"
    if semver_diff == "patch":
        return "routine", "patch_bump", "patch update with release notes"
    if _is_digest_update(target, metadata):
        return "routine", "routine_digest", "image digest update"
    return "routine", "routine_update", "update metadata available"


def _mutable_latest_digest_reason(
    current_version: str,
    target_version: str,
    provider: str,
    lsio_reason: tuple[str, str] | None,
) -> tuple[str, str, str] | None:
    normalized_target = target_version.lower()
    if (
        current_version.lower() != "latest"
        and normalized_target != "latest"
        and not normalized_target.startswith("latest (")
    ):
        return None
    if provider != "lsio":
        return "needs_review", "mutable_latest", "mutable latest tag"
    if lsio_reason is None:
        return (
            "needs_review",
            "lsio_latest",
            "LSIO image update via mutable latest",
        )
    code, label = lsio_reason
    return "needs_review", code, f"{label} via mutable latest"


def _lsio_digest_reason(provider: str, change_type: str) -> tuple[str, str] | None:
    if provider != "lsio":
        return None
    return {
        "upstream_update": ("lsio_upstream", "LSIO/upstream release"),
        "image_rebuild": ("lsio_rebuild", "LSIO image rebuild"),
    }.get(change_type)


def _semver_diff(current_version: str, target_version: str) -> str:
    current_match = SEMVER_PARTS_RE.search(current_version)
    target_match = SEMVER_PARTS_RE.search(target_version)
    if current_match is None or target_match is None:
        return ""
    current = tuple(int(part or 0) for part in current_match.groups())
    target = tuple(int(part or 0) for part in target_match.groups())
    if target[0] != current[0]:
        return "major"
    if target[1] != current[1]:
        return "minor"
    if target[2] != current[2]:
        return "patch"
    return ""


def _has_release_or_changelog_link(links: Sequence[ReleaseNoteLink]) -> bool:
    return any(
        link.url
        and (
            "release" in link.kind.lower()
            or "changelog" in link.kind.lower()
            or "release" in link.label.lower()
            or "changelog" in link.label.lower()
        )
        for link in links
    )


def _notification_copy(
    settings: WebSettings,
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    triggers: Sequence[ReleaseNotificationTrigger],
    *,
    image_repo: str,
    upstream_repo: str,
    update_kind: str = "",
    verbosity: str = "summary",
) -> tuple[str, str]:
    classification = getattr(note, "classification", None)
    change_type = str(getattr(classification, "change_type", "") or "")
    build_suffix = str(
        getattr(getattr(classification, "target", None), "build_suffix", "") or ""
    )
    image_rebuild = change_type == "image_rebuild"
    upstream_update = change_type == "upstream_update"
    repo = image_repo
    tag = note.release_tag or target.target.desired_tag or target.target.tag_token
    title = _notification_title(
        target,
        repo=repo,
        upstream_repo=upstream_repo,
        tag=tag,
        build_suffix=build_suffix,
        image_rebuild=image_rebuild,
        upstream_update=upstream_update,
        update_kind=update_kind,
    )
    lines = _notification_description_lines(
        settings,
        target,
        note,
        triggers,
        repo=repo,
        upstream_repo=upstream_repo,
        tag=tag,
        build_suffix=build_suffix,
        image_rebuild=image_rebuild,
        upstream_update=upstream_update,
    )
    body = str(getattr(note, "body", "") or "").strip()
    if verbosity == "full" and body:
        lines.extend(("", body))
    return title, "\n".join(lines)[:DISCORD_EMBED_DESCRIPTION_LIMIT]


def _notification_title(
    target: _NotificationTarget,
    *,
    repo: str,
    upstream_repo: str,
    tag: str,
    build_suffix: str,
    image_rebuild: bool,
    upstream_update: bool,
    update_kind: str,
) -> str:
    if image_rebuild:
        return _lsio_image_rebuild_title(repo, tag, build_suffix)
    if upstream_update:
        title = _upstream_application_update_title(repo, upstream_repo, tag)
        if title:
            return title
    return (
        f"{_notification_title_subject(repo, target)} "
        f"{_notification_title_kind(target, update_kind)}"
    )


def _notification_title_subject(repo: str, target: _NotificationTarget) -> str:
    subject = image_repo_ref(repo or target.target.repo or target.target.first)
    return subject.rsplit("/", 1)[-1] or subject or target.target.first


def _notification_title_kind(target: _NotificationTarget, update_kind: str) -> str:
    if update_kind == "digest" or is_digest_target_line(target.target):
        return "Digest Update"
    return "Tag Update"


def _lsio_image_rebuild_title(repo: str, tag: str, build_suffix: str) -> str:
    title = "LSIO image rebuild"
    if repo:
        title = f"{title}: {repo}"
    if tag:
        title = f"{title} {tag}"
    elif build_suffix:
        title = f"{title} {build_suffix}"
    return title


def _upstream_application_update_title(
    repo: str,
    upstream_repo: str,
    tag: str,
) -> str:
    title_repo = upstream_repo or repo
    if not title_repo:
        return ""
    title = f"Upstream application update: {title_repo}"
    if tag:
        title = f"{title} {tag}"
    return title


def _notification_description_lines(
    settings: WebSettings,
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    triggers: Sequence[ReleaseNotificationTrigger],
    *,
    repo: str,
    upstream_repo: str,
    tag: str,
    build_suffix: str,
    image_rebuild: bool,
    upstream_update: bool,
) -> list[str]:
    lines = [f"`{target.target.first}`"]
    if target.service_key:
        lines.append(f"Service: `{target.service_key}`")
    if repo:
        lines.append(f"Repository: `{repo}`")
    if upstream_repo and upstream_repo != repo:
        lines.append(f"Upstream: `{upstream_repo}`")
    if tag:
        lines.append(f"Release: `{tag}`")
    if image_rebuild:
        lines.append("Update type: LinuxServer.io rebuild")
        if build_suffix:
            lines.append(f"LSIO build: `{build_suffix}`")
    elif upstream_update:
        lines.append("Update type: upstream application update")
    if note.security.outcome == "verified_critical_high":
        identifiers = ", ".join(note.security.advisory_ids[:4])
        security_line = f"Security: verified {note.security.severity.title()} advisory"
        if identifiers:
            security_line = f"{security_line} ({identifiers})"
        lines.append(security_line)
        lines.append(note.security.reason)
    elif note.security.outcome == "needs_review":
        lines.append("Security: release needs review")
        lines.append(note.security.reason)
    lines.append(_status_line(settings, note))
    if note.breaking:
        lines.append("Breaking-risk indicators were detected.")
        lines.extend(note.breaking_reasons[:3])
    if triggers:
        label = ", ".join(_trigger_label(trigger) for trigger in triggers[:5])
        lines.append(f"WUD triggers: {label}")
    return lines


def _status_line(settings: WebSettings, note: ReleaseNoteInfo) -> str:
    if note.status == "ready":
        return "Release metadata is ready in WUDup."
    if note.status == "not_found":
        return "A matching GitHub release was not found; project links are included."
    if note.status == "missing":
        return "Release metadata was not cached before this notification."
    if note.error:
        error = _redact_sensitive_text(settings, note.error)
        return f"Release-note status: {error or note.status}"
    return f"Release-note status: {note.status}"


def _trigger_label(trigger: ReleaseNotificationTrigger) -> str:
    if trigger.type and trigger.name:
        return f"{trigger.type}/{trigger.name}"
    return trigger.name or trigger.type or trigger.id


def _payload_batches(
    items: Sequence[ReleaseNotificationItem],
    mode: str = "digest",
) -> list[dict[str, object]]:
    sendable = [item for item in items if not item.skipped_reason]
    if mode == "digest":
        return _digest_payload_batches(sendable)
    batches: list[dict[str, object]] = []
    for item in sendable:
        payload = {
            "username": DISCORD_WEBHOOK_USERNAME,
            "allowed_mentions": {"parse": []},
            "embeds": [_discord_embed(item)],
        }
        batches.append(
            {
                "index": len(batches) + 1,
                "count": 1,
                "items": [item],
                "payload": payload,
            }
        )
    return batches


def _digest_payload_batches(
    items: Sequence[ReleaseNotificationItem],
) -> list[dict[str, object]]:
    if not items:
        return []
    total_header = f"🧾 WUDup batch — {len(items)} updates found"
    batches: list[dict[str, object]] = []
    lines: list[str] = []
    batch_items: list[ReleaseNotificationItem] = []
    current_category = ""

    def finish_batch() -> None:
        header = f"🧾 WUDup batch — {len(batch_items)} updates found"
        content = "\n\n".join((header, "\n".join(lines), DISCORD_DIGEST_FOOTER))
        batches.append(
            {
                "index": len(batches) + 1,
                "count": len(batch_items),
                "items": list(batch_items),
                "payload": {
                    "username": DISCORD_WEBHOOK_USERNAME,
                    "allowed_mentions": {"parse": []},
                    "flags": DISCORD_SUPPRESS_EMBEDS_FLAG,
                    "content": content,
                },
            }
        )

    for category, category_label in DISCORD_DIGEST_CATEGORIES:
        for item in (item for item in items if item.category == category):
            row = _digest_row(item)
            prefix = _digest_category_prefix(
                lines,
                current_category=current_category,
                category=category,
                category_label=category_label,
            )
            candidate_lines = [*lines, *prefix, row]
            candidate = "\n\n".join(
                (total_header, "\n".join(candidate_lines), DISCORD_DIGEST_FOOTER)
            )
            if batch_items and len(candidate) > DISCORD_MESSAGE_CONTENT_LIMIT:
                finish_batch()
                lines = [category_label, row]
                batch_items = [item]
            else:
                lines = candidate_lines
                batch_items.append(item)
            current_category = category
    finish_batch()
    return batches


def _digest_category_prefix(
    lines: Sequence[str],
    *,
    current_category: str,
    category: str,
    category_label: str,
) -> list[str]:
    if current_category == category:
        return []
    return ([""] if lines else []) + [category_label]


def _payload_messages(batches: Sequence[Mapping[str, object]]) -> list[str]:
    messages: list[str] = []
    for batch in batches:
        payload = batch.get("payload")
        if not isinstance(payload, Mapping):
            continue
        content = str(payload.get("content") or "")
        if content:
            messages.append(content)
    return messages


def _digest_row(item: ReleaseNotificationItem) -> str:
    subject = item.service_key or image_repo_ref(
        item.image_repo or item.image
    ).rsplit("/", 1)[-1]
    row = (
        f"• {_discord_inline(subject)[:DISCORD_DIGEST_SUBJECT_LIMIT]} "
        f"{_discord_code(item.current_version[:DISCORD_DIGEST_VERSION_LIMIT])} → "
        f"{_discord_code(item.target_version[:DISCORD_DIGEST_VERSION_LIMIT])} — "
        f"{item.reason_label[:DISCORD_DIGEST_REASON_LIMIT]}"
    )
    selected_links: list[str] = []
    for link in _digest_links(item.links):
        candidate = f"{row} — {' | '.join([*selected_links, link])}"
        if len(candidate) > DISCORD_DIGEST_ROW_LIMIT:
            break
        selected_links.append(link)
    return f"{row} — {' | '.join(selected_links)}" if selected_links else row


def _compact_digest_link_label(link: ReleaseNoteLink) -> str:
    kind = link.kind.lower()
    label = link.label.lower()
    if "advisory" in kind or "ghsa" in label or "cve" in label:
        return link.label
    if "changelog" in kind or "changelog" in label:
        return "changelog"
    if kind == "lsio_release" or "lsio release" in label:
        return "LSIO release"
    if "upstream" in kind or "upstream" in label:
        return "upstream"
    if "release" in kind or "release" in label:
        return "release"
    if "project" in kind or "project" in label:
        return "project"
    return ""


def _digest_links(links: Sequence[ReleaseNoteLink]) -> list[str]:
    selected: list[str] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        compact_label = _compact_digest_link_label(link)
        if not compact_label:
            continue
        key = (compact_label, link.url)
        if not link.url or key in seen:
            continue
        seen.add(key)
        selected.append(_discord_link(compact_label, link.url))
    return selected


def _discord_inline(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ").strip()


def _discord_code(value: str) -> str:
    return f"`{_discord_inline(value)}`"


def _discord_embed(item: ReleaseNotificationItem) -> dict[str, object]:
    links = [_discord_link(link.label, link.url) for link in item.links if link.url]
    fields: list[dict[str, object]] = [
        {"name": "Image", "value": f"`{item.image}`", "inline": False},
    ]
    if item.service_key:
        fields.append({"name": "Service", "value": f"`{item.service_key}`", "inline": True})
    if item.image_repo:
        fields.append(
            {"name": "Repository", "value": f"`{item.image_repo}`", "inline": True}
        )
    if item.upstream_repo and item.upstream_repo != item.image_repo:
        fields.append(
            {"name": "Upstream", "value": f"`{item.upstream_repo}`", "inline": True}
        )
    if links:
        fields.append({"name": "Links", "value": " - ".join(links)[:1024], "inline": False})
    if item.security.outcome != "ordinary":
        fields.append(
            {
                "name": "Security",
                "value": item.security.reason[:1024],
                "inline": False,
            }
        )
    if item.triggers:
        fields.append(
            {
                "name": "WUD triggers",
                "value": ", ".join(_trigger_label(trigger) for trigger in item.triggers)[:1024],
                "inline": False,
            }
        )
    embed: dict[str, object] = {
        "title": item.title[:256],
        "description": item.description[:DISCORD_EMBED_DESCRIPTION_LIMIT],
        "color": (
            DISCORD_SECURITY_COLOR
            if item.category == "security_urgent"
            else DISCORD_COLOR
        ),
        "fields": fields[:25],
    }
    first_url = next((link.url for link in item.links if link.url), "")
    if first_url:
        embed["url"] = first_url
    return embed


def _discord_link(label: str, url: str) -> str:
    safe_label = (label or "Release link").replace("[", "").replace("]", "")
    safe_url = url.replace(")", "%29")
    return f"[{safe_label}]({safe_url})"


@dataclass(frozen=True)
class _WebhookConfig:
    value: str
    source: str


def _discord_webhook(settings: WebSettings) -> _WebhookConfig:
    value, source = effective_release_notification_webhook(settings)
    return _WebhookConfig(value=value, source=source)


def _release_notification_destination(
    settings: WebSettings,
) -> ReleaseNotificationDestination:
    webhook = _discord_webhook(settings)
    return ReleaseNotificationDestination(
        configured=bool(webhook.value),
        source=webhook.source,
    )


def _post_discord_payload(webhook_url: str, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        method="POST",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_WEBHOOK_USER_AGENT,
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=DISCORD_WEBHOOK_TIMEOUT_SECONDS,
    ) as response:
        if response.status < 200 or response.status >= 300:
            raise urllib.error.HTTPError(
                webhook_url,
                response.status,
                "Discord webhook request failed",
                response.headers,
                None,
            )


def _test_discord_payload() -> dict[str, object]:
    items = [
        ReleaseNotificationItem(
            line_no=1,
            image="ghcr.io/magrhino/wudup:latest",
            service_key="system/wudup",
            title="WUDup test notification",
            description="Representative mutable-tag digest row.",
            status="ready",
            image_repo="magrhino/wudup",
            upstream_repo="magrhino/wudup",
            current_version="latest",
            target_version="latest (release v1.2.3)",
            category="needs_review",
            reason_code="mutable_latest",
            reason_label="mutable latest tag",
            links=[
                ReleaseNoteLink(
                    label="GitHub release",
                    url="https://github.com/magrhino/wudup/releases",
                    kind="github_release",
                )
            ],
        ),
        ReleaseNotificationItem(
            line_no=2,
            image="lscr.io/linuxserver/jellyfin:latest",
            service_key="media/jellyfin",
            title="Jellyfin test notification",
            description="Representative LSIO digest row.",
            status="ready",
            image_repo="linuxserver/docker-jellyfin",
            upstream_repo="jellyfin/jellyfin",
            current_version="latest",
            target_version="latest (release v10.11.0)",
            category="needs_review",
            reason_code="lsio_latest",
            reason_label="LSIO image update via mutable latest",
            links=[
                ReleaseNoteLink(
                    label="LSIO release",
                    url="https://github.com/linuxserver/docker-jellyfin/releases",
                    kind="lsio_release",
                ),
                ReleaseNoteLink(
                    label="Upstream release",
                    url="https://github.com/jellyfin/jellyfin/releases",
                    kind="github_release",
                ),
            ],
        ),
    ]
    payload = _digest_payload_batches(items)[0]["payload"]
    assert isinstance(payload, dict)
    return payload


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
    batches = _payload_batches(items, response.mode)
    return response.model_copy(
        update={
            "count": len(items),
            "sendable_count": sendable_count,
            "skipped_count": len(items) - sendable_count,
            "batch_count": len(batches),
            "messages": _payload_messages(batches),
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
