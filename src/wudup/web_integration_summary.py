"""Compact read-only summaries for machine integrations.

Framework-free builders behind `/api/integrations/v1`. They only call existing
read paths (cached WUD snapshot, SQLite caches, Compose discovery) and never
refresh, rescan, plan, or write. Output is capped and stripped of free text
that consumers should not trust, such as release-note bodies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import (
    web_pending,
    web_release_notes,
    web_runs,
    web_security,
    web_state,
    web_tracking,
)
from .db import SERVICE_UPDATE_RUN_MODES, utc_timestamp
from .web_integration_models import (
    IntegrationIssue,
    IntegrationPolicy,
    IntegrationRelease,
    IntegrationRun,
    IntegrationScan,
    IntegrationSecurity,
    IntegrationServiceResponse,
    IntegrationSeverityCounts,
    IntegrationSnooze,
    IntegrationSummaryResponse,
    IntegrationUpdate,
    IntegrationUpdates,
    IntegrationWudStatus,
)
from .web_models import (
    PendingGroupedItem,
    PendingItem,
    PendingResponse,
    PendingSnoozedCandidate,
    ReleaseNoteInfo,
    RunHistorySummary,
    SecurityScanFinding,
    SecurityScanInfo,
    ServicePolicyRecord,
    WebSettings,
)
from .web_redaction import sanitize_support_bundle_text
from .web_scheduler import AUTO_UPDATE_MISSED_MODE

MAX_UPDATES = 20
MAX_ISSUES = 10
MAX_BREAKING_REASONS = 3
MAX_ADVISORIES = 5
MAX_RUN_FAILED_SERVICES = 5
TITLE_LIMIT = 80
REASON_LIMIT = 120
RECENT_RUN_WINDOW = timedelta(hours=24)
SERVICE_RUN_LOOKBACK = 20
UPDATE_RUN_LOOKBACK = 20
# Runs that changed (or tried to change) running services. Self-update runs
# are written only by web_self_update.py, which has no shared constant.
UPDATE_RUN_MODES = (*SERVICE_UPDATE_RUN_MODES, "web-self-update")
# Self-update runs finish with their own success statuses (SelfUpdateAuditStatus).
SUCCESS_RUN_STATUSES = frozenset(
    {"success", "running_image_verified", "image_prepared", "tag_prepared"}
)
RUNNING_RUN_STATUSES = frozenset({"started", "running", "scheduled", "in_progress"})
FAILED_RUN_STATUSES = frozenset({"failure", "failed"})
# Only these decide whether a failed run is still the latest outcome; an
# in-flight or unrecognized run must not clear run-failed.
CONCLUSIVE_RUN_STATUSES = frozenset({"success", "needs_review", "failure"})
UNHEALTHY_HEALTH_STATUSES = frozenset({"failed", "timed_out", "service_disappeared"})
BROKEN_SCAN_STATES = frozenset({"error", "auth_required", "unavailable_offline"})
STALE_METADATA_STATUSES = frozenset({"retained", "recovered"})
_SECURITY_BY_OUTCOME: dict[str, IntegrationSecurity] = {
    "verified_critical_high": "critical_high",
    "needs_review": "review",
    "ordinary": "none",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


class IntegrationServiceNotFound(LookupError):
    """Raised when no tracked Compose service matches the requested key."""


@dataclass(frozen=True)
class _PendingEntry:
    service: str
    item: PendingItem
    grouped: PendingGroupedItem | None


@dataclass(frozen=True)
class _SummaryInputs:
    pending: PendingResponse
    notes: dict[int, ReleaseNoteInfo]
    scans: dict[int, SecurityScanInfo]
    scanning_enabled: bool
    policies: dict[str, ServicePolicyRecord]


def integration_summary(settings: WebSettings) -> IntegrationSummaryResponse:
    now = datetime.now(UTC)
    inputs = _summary_inputs(settings)
    entries = _pending_entries(inputs.pending)
    updates = [_update(entry, inputs) for entry in entries]
    updates.sort(
        key=lambda update: (
            update.security != "critical_high",
            not update.breaking,
            update.service,
            update.image,
        )
    )
    runs = _recent_update_runs(settings)
    last_run = runs[0] if runs else None
    concluded = next(
        (run for run in runs if _run_status(run) in CONCLUSIVE_RUN_STATUSES),
        None,
    )
    missed = web_runs.run_history(settings, limit=1, modes=(AUTO_UPDATE_MISSED_MODE,))
    system_issues = [
        *_wud_issues(inputs.pending),
        *_run_issues(concluded, now),
        *_missed_run_issues(settings, missed, now),
        *_unplaced_issues(settings, inputs.pending),
        *_scan_issues(settings, inputs),
    ]
    # System and run issues come first so per-update issues can never push
    # them past the cap; each group is ordered urgent-first, then by id, so
    # the visible window only moves when the set of issues changes.
    issues = [
        *sorted(system_issues, key=_issue_order),
        *sorted(_update_issues(updates), key=_issue_order),
    ]
    return IntegrationSummaryResponse(
        checked_at=utc_timestamp(),
        wud=_wud_status(settings, inputs.pending),
        updates=IntegrationUpdates(
            total=len(updates),
            items=updates[:MAX_UPDATES],
            more=max(0, len(updates) - MAX_UPDATES),
            unplaced=_unplaced_count(inputs.pending),
            snoozed=len(inputs.pending.snoozed_candidates),
        ),
        last_run=None if last_run is None else _run(settings, last_run),
        issues=issues[:MAX_ISSUES],
        issues_more=max(0, len(issues) - MAX_ISSUES),
    )


def integration_service(
    settings: WebSettings,
    stack: str,
    service: str,
) -> IntegrationServiceResponse:
    service_key = f"{stack}/{service}"
    inputs = _summary_inputs(settings)
    policy = _policy(inputs.policies.get(service_key))
    last_run = _last_service_run(settings, stack, service)
    entry = next(
        (
            entry
            for entry in _pending_entries(inputs.pending)
            if entry.service and entry.service == service_key
        ),
        None,
    )
    if entry is not None:
        return _pending_service(settings, entry, inputs, policy, last_run)
    candidate = next(
        (
            candidate
            for candidate in inputs.pending.snoozed_candidates
            if candidate.service_key == service_key
        ),
        None,
    )
    if candidate is not None:
        return _snoozed_service(candidate, policy, last_run)
    tracked = next(
        (
            item
            for item in web_tracking.tracked_containers(settings).items
            if item.service_key == service_key
        ),
        None,
    )
    if tracked is None:
        raise IntegrationServiceNotFound(
            f"WUDup does not track a Compose service named {_text(service_key, REASON_LIMIT)}."
        )
    return IntegrationServiceResponse(
        service=service_key,
        image=tracked.image,
        current=tracked.current_tag,
        runtime=tracked.runtime_state,
        policy=policy,
        last_run=last_run,
    )


def _summary_inputs(settings: WebSettings) -> _SummaryInputs:
    pending = web_pending.pending_response(settings, force_api=False)
    notes = web_release_notes.cached_release_notes_response(settings)
    # Integrations are polled on a timer, so never ask a registry for a
    # missing digest here; the WebUI Security page still resolves them.
    scans = web_security.security_scans_response(settings, resolve_missing_digests=False)
    return _SummaryInputs(
        pending=pending,
        notes={item.line_no: item for item in notes.items},
        scans={item.line_no: item for item in scans.items},
        scanning_enabled=scans.scanning_enabled,
        policies={
            record.service_key: record
            for record in web_state.service_policy_records(settings)
        },
    )


def _pending_entries(pending: PendingResponse) -> list[_PendingEntry]:
    grouping = pending.grouping
    if grouping.status != "ready":
        # Without Compose matches no update has a service; `image` names it.
        return [_PendingEntry("", item, None) for item in pending.items]
    entries = [
        _PendingEntry(service_key, item, item)
        for group in grouping.groups
        for item in group.items
        # A stack-level match has no service names; key it by stack so the
        # update is still counted and can still raise issues.
        for service_key in (
            [f"{group.name}/{service}" for service in item.services] or [group.name]
        )
    ]
    entries.extend(_PendingEntry("", item, item) for item in grouping.unmatched)
    return entries


def _update(entry: _PendingEntry, inputs: _SummaryInputs) -> IntegrationUpdate:
    item = entry.item
    note = _matching_note(inputs.notes.get(item.line_no), item)
    scan = inputs.scans.get(item.line_no)
    policy = inputs.policies.get(entry.service)
    return IntegrationUpdate(
        service=entry.service,
        image=item.repo,
        current=item.current_tag,
        target=_target(item),
        kind=_kind(item),
        breaking=bool(note and note.breaking),
        security=_security(note),
        fixes=_fixes(scan),
        scan=scan.state if scan is not None else "unknown",
        auto=bool(policy and policy.auto_update),
        skipped=bool(
            entry.grouped is not None
            and entry.grouped.release_skip is not None
            and entry.grouped.release_skip.matches
        ),
    )


def _pending_service(
    settings: WebSettings,
    entry: _PendingEntry,
    inputs: _SummaryInputs,
    policy: IntegrationPolicy | None,
    last_run: IntegrationRun | None,
) -> IntegrationServiceResponse:
    item = entry.item
    note = _matching_note(inputs.notes.get(item.line_no), item)
    scan = inputs.scans.get(item.line_no)
    skip = entry.grouped.release_skip if entry.grouped is not None else None
    return IntegrationServiceResponse(
        service=entry.service,
        image=item.repo,
        current=item.current_tag,
        target=_target(item),
        kind=_kind(item),
        runtime=entry.grouped.runtime_state if entry.grouped is not None else "unknown",
        update_available=True,
        release=None if note is None else _release(note),
        scan=None if scan is None else _scan(scan),
        policy=policy,
        snooze=(
            IntegrationSnooze(
                kind="release",
                label=_text(skip.label, TITLE_LIMIT),
                reason=_text(sanitize_support_bundle_text(settings, skip.reason), REASON_LIMIT),
            )
            if skip is not None and skip.matches
            else None
        ),
        last_run=last_run,
    )


def _snoozed_service(
    candidate: PendingSnoozedCandidate,
    policy: IntegrationPolicy | None,
    last_run: IntegrationRun | None,
) -> IntegrationServiceResponse:
    return IntegrationServiceResponse(
        service=candidate.service_key,
        image=candidate.image,
        current=candidate.current_tag,
        target=candidate.desired_tag or candidate.wud_metadata.remote_tag,
        kind=candidate.wud_metadata.semver_diff or candidate.wud_metadata.update_kind,
        update_available=True,
        policy=policy,
        snooze=IntegrationSnooze(
            kind=candidate.snooze_kind,
            until=candidate.snoozed_until or "",
            label=_text(candidate.wait_for_service_key, TITLE_LIMIT),
            reason=_text(candidate.reason, REASON_LIMIT),
        ),
        last_run=last_run,
    )


def _matching_note(
    note: ReleaseNoteInfo | None,
    item: PendingItem,
) -> ReleaseNoteInfo | None:
    # Release notes and pending items are both keyed by line; skip a note that
    # was resolved for a different update if WUD changed between the reads.
    # Compare tags, not repos: a note's image_repo is its release source (a
    # GitHub or LinuxServer repo), not the image reference.
    if note is None or note.status != "ready":
        return None
    current = note.classification.current.raw
    if current and current != item.current_tag:
        return None
    return note


def _target(item: PendingItem) -> str:
    if item.desired_tag:
        return item.desired_tag
    if item.wud_metadata is not None and item.wud_metadata.remote_tag:
        return item.wud_metadata.remote_tag
    return item.digest[:19] if item.digest else ""


def _kind(item: PendingItem) -> str:
    metadata = item.wud_metadata
    if metadata is None:
        return ""
    return metadata.semver_diff or metadata.update_kind


def _security(note: ReleaseNoteInfo | None) -> IntegrationSecurity:
    if note is None:
        return "unknown"
    return _SECURITY_BY_OUTCOME.get(note.security.outcome, "unknown")


def _fixes(scan: SecurityScanInfo | None) -> IntegrationSeverityCounts | None:
    if scan is None or scan.comparison.status == "unknown":
        return None
    return _severity_counts(scan.comparison.fixed_findings)


def _severity_counts(findings: Iterable[SecurityScanFinding]) -> IntegrationSeverityCounts:
    counts = IntegrationSeverityCounts()
    for finding in findings:
        if finding.severity == "critical":
            counts.critical += 1
        elif finding.severity == "high":
            counts.high += 1
    return counts


def _release(note: ReleaseNoteInfo) -> IntegrationRelease:
    return IntegrationRelease(
        tag=_text(note.release_tag, TITLE_LIMIT),
        title=_text(note.title, TITLE_LIMIT),
        published_at=note.published_at,
        breaking=note.breaking,
        breaking_reasons=[
            _text(reason, REASON_LIMIT)
            for reason in note.breaking_reasons[:MAX_BREAKING_REASONS]
        ],
        security=_security(note),
        advisories=[
            _text(advisory, TITLE_LIMIT)
            for advisory in note.security.advisory_ids[:MAX_ADVISORIES]
        ],
        link=next((link.url for link in note.links if link.url.startswith("https://")), ""),
    )


def _scan(scan: SecurityScanInfo) -> IntegrationScan:
    return IntegrationScan(
        state=scan.state,
        scanned_at=scan.scanned_at,
        comparison=scan.comparison.status,
        target=IntegrationSeverityCounts(
            critical=scan.severity_counts.critical,
            high=scan.severity_counts.high,
        ),
        fixes=_fixes(scan),
    )


def _policy(record: ServicePolicyRecord | None) -> IntegrationPolicy | None:
    if record is None:
        return None
    return IntegrationPolicy(
        mode=record.update_mode,
        auto=record.auto_update,
        time=record.auto_update_time or "",
        days=list(record.auto_update_days),
    )


def _wud_status(settings: WebSettings, pending: PendingResponse) -> IntegrationWudStatus:
    detail = ""
    if pending.wud_api.state != "ready":
        detail = _text(
            sanitize_support_bundle_text(settings, pending.wud_api.detail),
            REASON_LIMIT,
        )
    return IntegrationWudStatus(
        state=pending.wud_api.state,
        fresh=pending.source.fresh,
        detail=detail,
    )


def _unplaced_count(pending: PendingResponse) -> int:
    if pending.grouping.status != "ready":
        return pending.count
    return len(pending.grouping.unmatched)


def _recent_update_runs(settings: WebSettings) -> list[RunHistorySummary]:
    return web_runs.run_history(
        settings,
        limit=UPDATE_RUN_LOOKBACK,
        modes=UPDATE_RUN_MODES,
        include_dry_runs=False,
    )


def _last_service_run(
    settings: WebSettings,
    stack: str,
    service: str,
) -> IntegrationRun | None:
    runs = web_runs.run_history(
        settings,
        limit=SERVICE_RUN_LOOKBACK,
        modes=UPDATE_RUN_MODES,
        include_dry_runs=False,
    )
    for run in runs:
        if any(
            event.stack_name == stack and event.service_name == service
            for event in run.events
        ):
            return _run(settings, run)
    return None


def _run(settings: WebSettings, run: RunHistorySummary) -> IntegrationRun:
    failed = _failed_services(run)
    return IntegrationRun(
        id=run.id,
        at=run.finished_at or run.started_at,
        mode=run.mode,
        status=_run_status(run),
        services=len({(event.stack_name, event.service_name) for event in run.events}),
        failed=failed[:MAX_RUN_FAILED_SERVICES],
        reason=_text(sanitize_support_bundle_text(settings, _run_reason(run)), REASON_LIMIT),
    )


def _run_status(run: RunHistorySummary) -> str:
    if run.status in FAILED_RUN_STATUSES or _unhealthy_items(run):
        return "failure"
    if run.status in SUCCESS_RUN_STATUSES:
        return "needs_review" if run.verification.status == "needs_review" else "success"
    if run.status in RUNNING_RUN_STATUSES:
        return "running"
    if run.status == "missed":
        return "missed"
    return "unknown"


def _unhealthy_items(run: RunHistorySummary) -> list[str]:
    return [
        item.service_key or f"{item.stack_name}/{item.service_name}"
        for item in run.verification.items
        if item.health_status in UNHEALTHY_HEALTH_STATUSES
    ]


def _failed_services(run: RunHistorySummary) -> list[str]:
    failed = [
        f"{event.stack_name}/{event.service_name}"
        for event in run.events
        if event.status == "failure"
    ]
    failed.extend(_unhealthy_items(run))
    return list(dict.fromkeys(failed))


def _run_reason(run: RunHistorySummary) -> str:
    for item in run.verification.items:
        if item.health_status in UNHEALTHY_HEALTH_STATUSES and item.summary:
            return item.summary
    for event in run.events:
        if event.status == "failure":
            return str(event.metadata.get("failure_phase") or event.metadata.get("reason") or "")
    summary = run.metadata.get("summary")
    return summary if isinstance(summary, str) else ""


def _wud_issues(pending: PendingResponse) -> list[IntegrationIssue]:
    if pending.wud_api.state != "ready":
        return [
            IntegrationIssue(
                id="wudup:wud-unreachable",
                severity="urgent",
                code="wud-unreachable",
                name="WUD API unreachable",
                summary="WUDup cannot read update checks from WUD, so new updates are not detected.",
            )
        ]
    stale = pending.source.degraded or any(
        item.metadata_status in STALE_METADATA_STATUSES for item in pending.items
    )
    if not stale:
        return []
    return [
        IntegrationIssue(
            id="wudup:wud-stale",
            severity="digest",
            code="wud-stale",
            name="Some WUD update data is stale",
            summary="WUD could not recheck some containers; their update info may be out of date.",
        )
    ]


def _run_issues(run: RunHistorySummary | None, now: datetime) -> list[IntegrationIssue]:
    if run is None or _run_status(run) != "failure":
        return []
    at = run.finished_at or run.started_at
    failed = _failed_services(run)
    services = ", ".join(failed[:MAX_RUN_FAILED_SERVICES]) or "one or more services"
    reason = _run_reason(run)
    summary = f"{services}: {reason}" if reason else services
    return [
        IntegrationIssue(
            id=f"wudup:run-failed:{run.id}",
            severity="urgent" if _is_recent(at, now) else "digest",
            code="run-failed",
            name="Update run failed",
            summary=_text(summary, REASON_LIMIT),
            since=at,
        )
    ]


def _update_issues(updates: Sequence[IntegrationUpdate]) -> list[IntegrationIssue]:
    issues: list[IntegrationIssue] = []
    for update in updates:
        if update.skipped:
            continue
        if update.security == "critical_high":
            issues.append(
                IntegrationIssue(
                    id=f"wudup:security-release:{_update_subject(update)}:{update.target}",
                    severity="urgent",
                    code="security-release",
                    name="Security update available",
                    summary=_text(
                        f"{_update_subject(update)} {update.current} -> {update.target} fixes a "
                        "critical or high security advisory.",
                        REASON_LIMIT,
                    ),
                )
            )
        if update.breaking:
            issues.append(
                IntegrationIssue(
                    id=f"wudup:breaking-update:{_update_subject(update)}:{update.target}",
                    severity="digest",
                    code="breaking-update",
                    name="Breaking update pending",
                    summary=_text(
                        f"{_update_subject(update)} {update.current} -> {update.target} has "
                        "breaking changes; review before updating.",
                        REASON_LIMIT,
                    ),
                )
            )
    return issues


def _missed_run_issues(
    settings: WebSettings,
    runs: Sequence[RunHistorySummary],
    now: datetime,
) -> list[IntegrationIssue]:
    if not runs or not _is_recent(runs[0].started_at, now):
        return []
    run = runs[0]
    slots = run.metadata.get("slots")
    reasons = [
        str(slot.get("reason") or "")
        for slot in slots
        if isinstance(slot, dict)
    ] if isinstance(slots, list) else []
    services = run.metadata.get("service_keys")
    names = ", ".join(str(name) for name in services) if isinstance(services, list) else ""
    reason = next((reason for reason in reasons if reason), "")
    summary = ": ".join(part for part in (names, reason) if part) or "A scheduled update was skipped."
    return [
        IntegrationIssue(
            id=f"wudup:auto-update-missed:{run.id}",
            severity="digest",
            code="auto-update-missed",
            name="Scheduled auto-update skipped",
            summary=_text(sanitize_support_bundle_text(settings, summary), REASON_LIMIT),
            since=run.started_at,
        )
    ]


def _unplaced_issues(
    settings: WebSettings,
    pending: PendingResponse,
) -> list[IntegrationIssue]:
    count = _unplaced_count(pending)
    if not count:
        return []
    diagnostic = next(
        (
            item.diagnostic.message
            for item in pending.grouping.unmatched
            if item.diagnostic is not None and item.diagnostic.message
        ),
        "",
    )
    summary = f"{count} update(s) could not be matched to a Compose service"
    if diagnostic:
        summary = f"{summary}: {sanitize_support_bundle_text(settings, diagnostic)}"
    return [
        IntegrationIssue(
            id="wudup:update-unplaced",
            severity="digest",
            code="update-unplaced",
            name="Updates without a Compose service",
            summary=_text(summary, REASON_LIMIT),
        )
    ]


def _scan_issues(settings: WebSettings, inputs: _SummaryInputs) -> list[IntegrationIssue]:
    if not inputs.scanning_enabled:
        return []
    broken = [scan for scan in inputs.scans.values() if scan.state in BROKEN_SCAN_STATES]
    if not broken:
        return []
    message = next((scan.error_message for scan in broken if scan.error_message), "")
    summary = f"{len(broken)} security scan(s) failed"
    if message:
        summary = f"{summary}: {sanitize_support_bundle_text(settings, message)}"
    return [
        IntegrationIssue(
            id="wudup:scan-broken",
            severity="digest",
            code="scan-broken",
            name="Security scans failing",
            summary=_text(summary, REASON_LIMIT),
        )
    ]


def _update_subject(update: IntegrationUpdate) -> str:
    # Unplaced updates have no service; name them by image so their issue ids
    # stay distinct from any service's.
    return update.service or f"image:{update.image}"


def _issue_order(issue: IntegrationIssue) -> tuple[bool, str]:
    return issue.severity != "urgent", issue.id


def _is_recent(timestamp: str, now: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now - parsed < RECENT_RUN_WINDOW


def _text(value: str, limit: int) -> str:
    cleaned = " ".join(_CONTROL_CHARACTERS.sub(" ", value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}…"
