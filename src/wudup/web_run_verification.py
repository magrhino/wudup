"""Derived post-update verification summaries for WebUI run details."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .updater_models import STALE_PENDING_DIGEST_REASON
from .web_models import (
    PendingUpdateRecord,
    RunEventRecord,
    RunVerificationContainerStatus,
    RunVerificationHealthStatus,
    RunVerificationImageStatus,
    RunVerificationItem,
    RunVerificationSummary,
    RunVerificationWudStatus,
)

_UNKNOWN_STATUSES = {"unknown", "failed", "timed_out", "service_disappeared"}


def verification_from_run_records(
    pending_updates: Iterable[PendingUpdateRecord],
    events: Iterable[RunEventRecord],
) -> RunVerificationSummary:
    """Build per-line verification from persisted audit rows."""

    event_list = list(events)
    updates = list(pending_updates)
    matched_events = [_matching_event(update, event_list) for update in updates]
    identities_by_event: dict[int, set[tuple[str, str]]] = {}
    for update, event in zip(updates, matched_events):
        if event is not None:
            identities_by_event.setdefault(id(event), set()).add(
                (update.stack_name, update.service_name or update.image)
            )
    items = [
        _verification_item(
            update,
            event if event is not None and len(identities_by_event[id(event)]) == 1 else None,
        )
        for update, event in zip(updates, matched_events)
    ]
    needs_review_count = sum(1 for item in items if item.follow_up_needed)
    return RunVerificationSummary(
        status="needs_review" if needs_review_count else "verified",
        total_count=len(items),
        verified_count=len(items) - needs_review_count,
        needs_review_count=needs_review_count,
        items=items,
    )


def _verification_item(
    update: PendingUpdateRecord,
    event: RunEventRecord | None,
) -> RunVerificationItem:
    image_status = _image_status(update, event)
    container_status = _container_status(event)
    health_status = _health_status(event)
    wud_status = _wud_status(update)
    follow_up_needed = _follow_up_needed(
        image_status,
        container_status,
        health_status,
        wud_status,
    )
    target_image = event.target_image if event else ""
    return RunVerificationItem(
        line_no=update.line_no,
        event_id=event.id if event else None,
        service_key=update.service_key,
        stack_name=update.stack_name,
        service_name=update.service_name,
        image=update.image,
        target_image=target_image,
        image_status=image_status,
        container_status=container_status,
        health_status=health_status,
        wud_status=wud_status,
        follow_up_needed=follow_up_needed,
        summary=_summary(
            image_status,
            container_status,
            health_status,
            wud_status,
            follow_up_needed,
        ),
    )


def _matching_event(
    update: PendingUpdateRecord,
    events: list[RunEventRecord],
) -> RunEventRecord | None:
    # Legacy rows may omit identity fields, but known identities must never conflict.
    events = [
        event for event in events
        if not (update.stack_name and event.stack_name and update.stack_name != event.stack_name)
        and not (update.service_name and event.service_name and update.service_name != event.service_name)
    ]
    for event in events:
        if (
            event.stack_name
            and event.service_name
            and event.stack_name == update.stack_name
            and event.service_name == update.service_name
        ):
            return event
    matches = [event for event in events if event.service_name and event.service_name == update.service_name]
    if matches:
        return matches[0] if len(matches) == 1 else None
    matches = [event for event in events if event.image and event.image == update.image]
    return matches[0] if len(matches) == 1 else None


def _image_status(
    update: PendingUpdateRecord,
    event: RunEventRecord | None,
) -> RunVerificationImageStatus:
    if update.status == "failed" or (event is not None and event.status != "success"):
        return "failed"
    if event is None:
        return "unknown"
    reason = _event_reason(event)
    if reason == "already-current":
        return "already_current"
    if _image_changed(event):
        return "new_image_running"
    if reason == "updated" and event.target_image and event.target_image != event.image:
        return "new_image_running"
    return "unknown"


def _container_status(event: RunEventRecord | None) -> RunVerificationContainerStatus:
    if event is None:
        return "unknown"
    if event.status != "success":
        return "failed"
    reason = _event_reason(event)
    if reason == "already-current":
        return "skipped"
    if reason == "updated":
        return "recreated"
    return "unknown"


def _health_status(event: RunEventRecord | None) -> RunVerificationHealthStatus:
    if event is None:
        return "unknown"
    reason = _event_reason(event)
    if event.status == "success":
        if reason == "already-current":
            return "skipped"
        if reason == "updated":
            if _event_metadata_string(event, "runtime_state_after") == "not-running":
                return "skipped"
            return "passed"
        return "unknown"
    if reason == "health-failed":
        if _event_metadata_string(event, "health_evidence") == "service_disappeared":
            return "service_disappeared"
        return "timed_out"
    return "failed"


def _wud_status(update: PendingUpdateRecord) -> RunVerificationWudStatus:
    if update.status == "resolved":
        if update.status_reason == "removed-before-run":
            return "removed_before_run"
        return "removed"
    if update.status == "failed":
        if update.status_reason == STALE_PENDING_DIGEST_REASON:
            return "stale_removed"
        return "restored"
    return "unknown"


def _follow_up_needed(
    image_status: RunVerificationImageStatus,
    container_status: RunVerificationContainerStatus,
    health_status: RunVerificationHealthStatus,
    wud_status: RunVerificationWudStatus,
) -> bool:
    return (
        image_status in _UNKNOWN_STATUSES
        or container_status in _UNKNOWN_STATUSES
        or health_status in _UNKNOWN_STATUSES
        or wud_status in {"restored", "stale_removed", "unknown"}
    )


def _image_changed(event: RunEventRecord) -> bool:
    if event.old_image_id and event.new_image_id and event.old_image_id != event.new_image_id:
        return True
    return bool(
        event.old_digest
        and event.new_digest
        and event.old_digest != event.new_digest
    )


def _event_reason(event: RunEventRecord) -> str:
    return _event_metadata_string(event, "reason")


def _event_metadata_string(event: RunEventRecord, key: str) -> str:
    value: Any = event.metadata.get(key)
    return value if isinstance(value, str) else ""


def _summary(
    image_status: RunVerificationImageStatus,
    container_status: RunVerificationContainerStatus,
    health_status: RunVerificationHealthStatus,
    wud_status: RunVerificationWudStatus,
    follow_up_needed: bool,
) -> str:
    if follow_up_needed:
        return "Manual review needed."
    parts = [
        _IMAGE_LABELS[image_status],
        _CONTAINER_LABELS[container_status],
        _HEALTH_LABELS[health_status],
        _WUD_LABELS[wud_status],
    ]
    return ", ".join(parts) + "."


_IMAGE_LABELS = {
    "new_image_running": "new image running",
    "already_current": "image already current",
    "failed": "image update failed",
    "unknown": "image state unknown",
}
_CONTAINER_LABELS = {
    "recreated": "container recreated",
    "skipped": "recreate skipped",
    "failed": "container recreate failed",
    "unknown": "container state unknown",
}
_HEALTH_LABELS = {
    "passed": "health passed",
    "skipped": "health skipped",
    "timed_out": "health timed out",
    "service_disappeared": "service disappeared",
    "failed": "health failed",
    "unknown": "health unknown",
}
_WUD_LABELS = {
    "removed": "WUD line removed",
    "restored": "WUD line restored",
    "stale_removed": "stale WUD line removed",
    "removed_before_run": "WUD line removed before run",
    "unknown": "WUD line state unknown",
}
