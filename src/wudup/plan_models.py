"""Shared dry-run plan models for WebUI planning helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .digest_provenance import DigestTagProvenance
from .updater_models import CompletedUpdateSelection, UpdateSelection

if TYPE_CHECKING:
    from .web_models import (
        PendingMetadataStatus,
        PendingSourceActive,
        PendingSourceMode,
    )


class PlanInputError(ValueError):
    """Raised when a requested plan cannot be built from the submitted lines."""


class PlanFileMissing(FileNotFoundError):
    """Raised when the WUD file is missing for a selected-line plan."""


@dataclass(frozen=True)
class DryRunPlanIssue:
    severity: str
    code: str
    message: str
    line_no: int | None = None
    stack: str = ""
    service: str = ""
    hint: str = ""
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class UnmatchedDiagnostic:
    code: str
    message: str
    hint: str = ""
    stack: str = ""
    service: str = ""
    compose_file: str = ""
    found_files: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DryRunPlanTarget:
    line_no: int
    raw: str
    image: str
    resolved_image: str
    digest: str
    desired_tag: str
    matched: bool
    action: str


@dataclass(frozen=True)
class DryRunPlanLine:
    line_no: int
    raw: str
    image: str
    resolved_image: str
    compose_image: str
    target_image: str
    service: str
    digest: str
    desired_tag: str
    action: str
    digest_provenance: DigestTagProvenance | None = None


@dataclass(frozen=True)
class DryRunPlanTagUpdate:
    old_image: str
    desired_tag: str
    new_image: str
    services: tuple[str, ...]


@dataclass(frozen=True)
class DryRunPlanTagStreamUpdate:
    line_no: int
    service: str
    current_tag: str
    reported_tag: str
    selected_tag: str
    decision: str
    label_key: str
    current_label_value: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str


@dataclass(frozen=True)
class DryRunPlanDigestPinUpdate:
    source_image: str
    resolved_tag: str
    planned_digest: str
    final_image: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: tuple[str, ...]
    label_rewrites: tuple[DryRunPlanDigestPinLabelRewrite, ...] = ()
    digest_provenance: DigestTagProvenance | None = None


@dataclass(frozen=True)
class DryRunPlanDigestUnpinUpdate:
    source_image: str
    resolved_tag: str
    tag_image: str
    current_digest: str
    target_digest: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: tuple[str, ...]
    digest_provenance: DigestTagProvenance | None = None
    transform_label_value: str = ""
    transform_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class DryRunPlanDigestPinLabelRewrite:
    service: str
    label_key: str
    current_label_value: str
    planned_tag: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str


@dataclass(frozen=True)
class DryRunPlanAction:
    kind: str
    description: str
    cwd: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class DryRunPlanStack:
    name: str
    directory: str
    compose_file: str
    project_directory: str
    services_label: str
    services: tuple[str, ...]
    pull_services: tuple[str, ...]
    stop_services: tuple[str, ...]
    force_recreate: bool
    up_no_deps: bool
    tag_updates: tuple[DryRunPlanTagUpdate, ...] = ()
    tag_stream_updates: tuple[DryRunPlanTagStreamUpdate, ...] = ()
    digest_pin_updates: tuple[DryRunPlanDigestPinUpdate, ...] = ()
    digest_unpin_updates: tuple[DryRunPlanDigestUnpinUpdate, ...] = ()
    actions: tuple[DryRunPlanAction, ...] = ()
    lines: tuple[DryRunPlanLine, ...] = ()


@dataclass(frozen=True)
class DryRunPlanSkipped:
    line_no: int
    raw: str
    image: str
    desired_tag: str
    reason: str


@dataclass(frozen=True)
class DryRunPlanSummary:
    target_count: int
    matched_target_count: int
    stack_count: int
    service_count: int
    skipped_count: int
    issue_count: int


@dataclass(frozen=True)
class DryRunPlanCleanupItem:
    line_no: int
    raw: str
    image: str
    desired_tag: str
    digest: str
    reason: str
    diagnostic: UnmatchedDiagnostic | None = None


@dataclass(frozen=True)
class DryRunPlanCleanup:
    cleanup_id: str = ""
    can_remove_unmatched: bool = False
    items: tuple[DryRunPlanCleanupItem, ...] = ()


@dataclass(frozen=True)
class DryRunPlanSource:
    configured: PendingSourceMode = "file"
    active: PendingSourceActive = "file"
    label: str = "Pending file"
    fresh: bool = True
    degraded: bool = False
    fallback_reason: str = ""
    detail: str = ""
    source_hash: str = ""
    source_ids_by_line: Mapping[int, str] = field(default_factory=dict)
    metadata_status_by_line: Mapping[int, PendingMetadataStatus] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class DryRunPlan:
    plan_id: str
    dry_run: bool
    can_apply: bool
    status: str
    source_file: str
    mode: str
    max_wait: int
    digest_pin_updates: bool
    selected_line_numbers: tuple[int, ...]
    summary: DryRunPlanSummary
    selected_selections: tuple[UpdateSelection, ...] = ()
    completed_update_selections: tuple[CompletedUpdateSelection, ...] = ()
    source: DryRunPlanSource = field(default_factory=DryRunPlanSource)
    targets: tuple[DryRunPlanTarget, ...] = ()
    stacks: tuple[DryRunPlanStack, ...] = ()
    skipped: tuple[DryRunPlanSkipped, ...] = ()
    issues: tuple[DryRunPlanIssue, ...] = ()
    cleanup: DryRunPlanCleanup = field(default_factory=DryRunPlanCleanup)

    def metadata_status_for_line(self, line_no: int) -> PendingMetadataStatus:
        fallback: PendingMetadataStatus = (
            "fresh"
            if self.source.active == "file" and not self.source.degraded
            else "recovered"
        )
        return self.source.metadata_status_by_line.get(line_no, fallback)

    def selected_metadata_statuses(self) -> tuple[PendingMetadataStatus, ...]:
        return tuple(
            self.metadata_status_for_line(line_no)
            for line_no in self.selected_line_numbers
        )


@dataclass(frozen=True)
class PendingGroupingItem:
    line_no: int
    raw: str
    image: str
    key: str
    repo: str
    has_tag: bool
    allow_repo: bool
    digest: str
    desired_tag: str
    resolved_image: str
    target_image: str
    compose_images: tuple[str, ...]
    services: tuple[str, ...]
    runtime_services: tuple[str, ...]
    action: str
    selection_id: str = ""
    platform: str = ""
    platform_os: str = ""
    platform_architecture: str = ""
    platform_variant: str = ""
    diagnostic: UnmatchedDiagnostic | None = None
    digest_provenance: DigestTagProvenance | None = None
    tag_stream: PendingTagStream | None = None


@dataclass(frozen=True)
class PendingTagStream:
    current_stream: str
    reported_stream: str


@dataclass(frozen=True)
class PendingStackGroup:
    name: str
    directory: str
    compose_file: str
    project_directory: str
    project_name: str
    services_label: str
    services: tuple[str, ...]
    line_numbers: tuple[int, ...]
    items: tuple[PendingGroupingItem, ...]


@dataclass(frozen=True)
class PendingGroupingResult:
    status: str
    groups: tuple[PendingStackGroup, ...] = ()
    unmatched: tuple[PendingGroupingItem, ...] = ()
    warnings: tuple[str, ...] = ()
