"""Data models and type aliases for WebUI."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from .config import UpdaterConfig

__all__ = (
    "APPLY_JOB_PROGRESS_STATUSES",
    "DEFAULT_CORE_UPDATE_TOUR_STEP",
    "DEFAULT_SECURITY_SCAN_CACHE_DIR",
    "PASSWORD_MIN_LENGTH",
    "SELF_UPDATE_RELEASE_NOTES_CAP",
    "TERMINAL_APPLY_JOB_STATUSES",
    "AdminRecoveryClaim",
    "ApplyJobLogResponse",
    "ApplyJobProgressEvent",
    "ApplyJobProgressStatus",
    "ApplyJobResponse",
    "ApplyJobStatus",
    "ApplyPlanRequest",
    "ApplyPreflightCheck",
    "ApplyPreflightResponse",
    "AuthSessionResponse",
    "AutoUpdateDay",
    "AutoUpdatePolicy",
    "AutoUpdateSelection",
    "ContainerRestartRequest",
    "ContainerRestartResponse",
    "CoreUpdateTourResponse",
    "CoreUpdateTourStatus",
    "CoreUpdateTourStep",
    "CoreUpdateTourUpdateRequest",
    "CreateDependencySnoozeOperation",
    "CreateSnoozeOperation",
    "CsrfResponse",
    "DeleteDependencySnoozeOperation",
    "DeleteServicePolicyOperation",
    "DeleteSnoozeOperation",
    "DiagnosticsSupportBundleResponse",
    "DigestPinLabelRewriteApprovalRequest",
    "DigestTagProvenance",
    "DoctorCheckResponse",
    "DoctorCheckStatus",
    "DoctorResponse",
    "DoctorSuggestionResponse",
    "HealthResponse",
    "LineNumber",
    "LogTail",
    "LoginRequest",
    "LoginThrottleEntry",
    "ManagedSettingEntry",
    "ManagedSettingSource",
    "ManagedSettingsUpdateRequest",
    "ManagedSettingsUpdateResponse",
    "OnboardingChecklistItem",
    "OnboardingChecklistResponse",
    "OnboardingDismissResponse",
    "OnboardingDocLink",
    "PendingCleanupLine",
    "PendingCleanupRemovedLine",
    "PendingCleanupRequest",
    "PendingCleanupResponse",
    "PendingDiagnostic",
    "PendingGroupedItem",
    "PendingGrouping",
    "PendingGroupingStatus",
    "PendingItem",
    "PendingMetadataRefreshItem",
    "PendingMetadataRefreshLine",
    "PendingMetadataRefreshRequest",
    "PendingMetadataRefreshResponse",
    "PendingMetadataRefreshStatus",
    "PendingMetadataStatus",
    "PendingRemovalPlanLine",
    "PendingRemovalPlanRequest",
    "PendingRemovalPlanResponse",
    "PendingRemovalRequest",
    "PendingRescanLine",
    "PendingRescanRequest",
    "PendingRescanResponse",
    "PendingRescanScope",
    "PendingRescanSkippedLine",
    "PendingRescanStatus",
    "PendingResponse",
    "PendingRuntimeState",
    "PendingSnoozedCandidate",
    "PendingSourceActive",
    "PendingSourceInfo",
    "PendingSourceMode",
    "PendingStackGroup",
    "PendingTagStream",
    "PendingUpdateRecord",
    "PlanAction",
    "PlanCleanup",
    "PlanCleanupItem",
    "PlanDigestPinLabelRewrite",
    "PlanDigestPinUpdate",
    "PlanDigestUnpinUpdate",
    "PlanIssue",
    "PlanLine",
    "PlanRequest",
    "PlanResponse",
    "PlanSelectionRequest",
    "PlanSkipped",
    "PlanStack",
    "PlanStatus",
    "PlanSummary",
    "PlanTagStreamUpdate",
    "PlanTagUpdate",
    "PlanTarget",
    "ReadyResponse",
    "ReleaseNoteChangeType",
    "ReleaseNoteClassification",
    "ReleaseNoteClassificationTag",
    "ReleaseNoteInfo",
    "ReleaseNoteLink",
    "ReleaseNotesResponse",
    "ReleaseNotificationDestination",
    "ReleaseNotificationItem",
    "ReleaseNotificationPreviewRequest",
    "ReleaseNotificationResponse",
    "ReleaseNotificationSendRequest",
    "ReleaseNotificationTestRequest",
    "ReleaseNotificationTestResponse",
    "ReleaseNotificationTrigger",
    "ReleaseSecurityAssessment",
    "ReleaseSecurityOutcome",
    "ReleaseSecuritySeverity",
    "ResetAdminClaimRequest",
    "RetagApplyRequest",
    "RetagChoiceRequest",
    "RetagPlanDigestPinUpdate",
    "RetagPlanIssue",
    "RetagPlanLabelRewrite",
    "RetagPlanRequest",
    "RetagPlanResponse",
    "RetagPlanStack",
    "RetagPlanStatus",
    "RetagPlanTagUpdate",
    "RetagPreviewJobResponse",
    "RetagRuntimeState",
    "RetagTargetItem",
    "RetagTargetsResponse",
    "RollbackPlanItem",
    "RollbackPlanItemStatus",
    "RollbackPlanResponse",
    "RollbackPlanStatus",
    "RunDetail",
    "RunEventRecord",
    "RunLogResponse",
    "RunSummary",
    "RunVerificationContainerStatus",
    "RunVerificationHealthStatus",
    "RunVerificationImageStatus",
    "RunVerificationItem",
    "RunVerificationStatus",
    "RunVerificationSummary",
    "RunVerificationWudStatus",
    "SecretSettingStatus",
    "SecurityScanComparison",
    "SecurityScanConfig",
    "SecurityScanFinding",
    "SecurityScanInfo",
    "SecurityScanJobResponse",
    "SecurityScanSeverityCounts",
    "SecurityScanState",
    "SecurityScanSubject",
    "SecurityScanVerdict",
    "SecurityScansResponse",
    "SelfUpdateApplyResponse",
    "SelfUpdateAuditStatus",
    "SelfUpdatePlanResponse",
    "SelfUpdatePrepareRequest",
    "SelfUpdatePrepareResponse",
    "SelfUpdateReleaseNote",
    "SelfUpdateRequest",
    "SelfUpdateResponse",
    "SelfUpdateStatus",
    "SelfUpdateStrategy",
    "ServicePolicyRecord",
    "ServicePolicyUpdateMode",
    "SetTagExclusionStatusOperation",
    "SettingsEntry",
    "SettingsEntrySource",
    "SettingsResponse",
    "SetupClaimRequest",
    "SetupStatusResponse",
    "SnoozeKind",
    "SnoozeRecord",
    "SnoozeState",
    "StateOperation",
    "StateOperationResponse",
    "StatusResponse",
    "TagExclusionMatchType",
    "TagExclusionRuleRecord",
    "TagExclusionScope",
    "TagExclusionStatus",
    "TagExclusionStatusFilter",
    "TagOverrideRequest",
    "TagStreamDecisionRequest",
    "TagStreamLabelRewriteApprovalRequest",
    "UpdateTargetItem",
    "UpdateTargetsResponse",
    "UpdateTargetsStatus",
    "UpsertServicePolicyOperation",
    "UpsertTagExclusionOperation",
    "WebApplyJob",
    "WebApplyJobProgressEvent",
    "WebSelfUpdatePlan",
    "WebSettings",
    "WudApiAppDiagnostics",
    "WudApiClientConfig",
    "WudApiConfigurationDiagnostics",
    "WudApiDiagnosticEndpointStatus",
    "WudApiLogDiagnostics",
    "WudApiObservationCounts",
    "WudApiObservationDiagnostic",
    "WudApiObservationDiagnostics",
    "WudApiObservationOutcome",
    "WudApiObservationReason",
    "WudApiRegistryDiagnostics",
    "WudApiState",
    "WudApiStatus",
    "WudApiStoreDiagnostics",
    "WudApiWatcherDiagnostics",
    "WudContainerMetadata",
)

SELF_UPDATE_RELEASE_NOTES_CAP = 10

PASSWORD_MIN_LENGTH = 12

DEFAULT_CORE_UPDATE_TOUR_STEP = "dashboard"

LineNumber = Annotated[int, Field(ge=1)]

PlanStatus = Literal["ready", "empty", "blocked"]

PendingGroupingStatus = Literal["ready", "unavailable"]

PendingRuntimeState = Literal["running", "not-running", "mixed", "unknown"]

PendingRescanScope = Literal["all", "selected"]

PendingRescanStatus = Literal["success", "partial", "blocked"]
PendingMetadataRefreshStatus = Literal["ready", "stale"]
PendingMetadataStatus = Literal["fresh", "retained", "recovered"]

DoctorCheckStatus = Literal["PASS", "WARN", "FAIL"]

WudApiState = Literal["ready", "unavailable", "auth_required", "error"]
WudApiObservationOutcome = Literal[
    "retained",
    "recovered",
    "unresolved",
    "unsupported_ignored",
]
WudApiObservationReason = Literal[
    "malformed_observation",
    "missing_image",
    "invalid_update_flag",
    "reported_error",
    "missing_scan_result",
    "unsupported_registry",
]

ApplyJobStatus = Literal["queued", "running", "success", "failure"]

ApplyJobProgressStatus = Literal["running", "success", "failure", "skipped"]

SelfUpdateStatus = Literal["available", "up_to_date", "disabled", "unavailable"]

SelfUpdateStrategy = Literal["pull_image", "prepare_tag_update"]

ReleaseNoteChangeType = Literal["upstream_update", "image_rebuild", "unknown"]
ReleaseSecurityOutcome = Literal[
    "verified_critical_high", "needs_review", "ordinary"
]
ReleaseSecuritySeverity = Literal[
    "critical", "high", "moderate", "low", "unknown", "none"
]

SelfUpdateAuditStatus = Literal[
    "image_prepared",
    "running_image_verified",
    "tag_prepared",
    "failure",
]

SettingsEntrySource = Literal["configured", "default", "derived", "request"]

ManagedSettingSource = Literal["configured", "default"]

ServicePolicyUpdateMode = Literal["", "pause", "stop", "live"]

AutoUpdateDay = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CoreUpdateTourStatus = Literal[
    "not_started",
    "in_progress",
    "completed",
    "dismissed",
]

CoreUpdateTourStep = Literal[
    "dashboard",
    "pending_select",
    "pending_preflight",
    "pending_apply",
    "runs_history",
]

SnoozeKind = Literal["time", "dependency"]

SnoozeState = Literal["active", "expired", "all"]

TagExclusionScope = Literal["image_repo", "service"]

TagExclusionMatchType = Literal["exact"]

TagExclusionStatus = Literal["active", "disabled"]

TagExclusionStatusFilter = Literal["active", "disabled", "all"]

TERMINAL_APPLY_JOB_STATUSES = frozenset({"success", "failure"})

APPLY_JOB_PROGRESS_STATUSES = frozenset({"running", "success", "failure", "skipped"})

@dataclass(frozen=True)
class WudApiClientConfig:
    header_items: tuple[tuple[str, str], ...] = ()
    secret_values: tuple[str, ...] = ()
    fingerprint: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.header_items)


DEFAULT_SECURITY_SCAN_CACHE_DIR = "/logs/trivy-cache"


@dataclass(frozen=True)
class SecurityScanConfig:
    enabled: bool = False
    executable: str = "trivy"
    cache_dir: str = DEFAULT_SECURITY_SCAN_CACHE_DIR
    timeout_seconds: int = 300


@dataclass(frozen=True)
class WebSettings:
    config: UpdaterConfig
    auth_token: str
    dev_no_auth: bool = False
    allowed_origins: frozenset[str] = frozenset()
    public_origin: str = ""
    allowed_hosts: frozenset[str] = frozenset()
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    secure_cookies: str = "auto"
    mutations_enabled: bool = False
    static_dir: Path | None = None
    host_docker_base: Path | None = None
    restart_container: str = ""
    wud_api_base_url: str = ""
    wud_api_startup_wait_seconds: float = 0.0
    wud_api_client: WudApiClientConfig = dataclass_field(
        default_factory=WudApiClientConfig
    )
    pending_source: PendingSourceMode = "api"
    legacy_scripts_enabled: bool = True
    release_notes_enabled_env: bool | None = None
    security_scan: SecurityScanConfig = dataclass_field(
        default_factory=SecurityScanConfig
    )
    command_env: Mapping[str, str] | None = None

    @property
    def auth_required(self) -> bool:
        return not self.dev_no_auth

@dataclass
class WebApplyJobProgressEvent:
    phase: str
    status: ApplyJobProgressStatus
    message: str
    created_at: str
    stack: str = ""
    services: tuple[str, ...] = ()
    line_numbers: tuple[int, ...] = ()

@dataclass
class WebApplyJob:
    id: str
    status: ApplyJobStatus
    selected_line_numbers: tuple[int, ...]
    version: int = 0
    run_id: int | None = None
    log_file: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    progress: tuple[WebApplyJobProgressEvent, ...] = ()

@dataclass(frozen=True)
class WebSelfUpdatePlan:
    plan_id: str
    created_at: float
    wud_file: Path
    current_tag: str
    latest_tag: str
    current_image: str
    target_spec: str
    target_image: str
    restart_container: str

@dataclass
class LoginThrottleEntry:
    failures: int
    first_failed_at: float
    last_failed_at: float
    locked_until: float = 0.0

@dataclass(frozen=True)
class AutoUpdatePolicy:
    service_key: str
    update_mode: str
    auto_update_time: str
    auto_update_days: tuple[str, ...]
    schedule_key: str
    scheduled_for: datetime

@dataclass(frozen=True)
class AutoUpdateSelection:
    line_numbers: tuple[int, ...]
    service_keys: tuple[str, ...]
    schedule_keys: tuple[str, ...]
    scheduled_for: datetime
    update_mode: str

@dataclass(frozen=True)
class LogTail:
    exists: bool
    content: str
    truncated: bool

@dataclass(frozen=True)
class AdminRecoveryClaim:
    username: str
    claim: str
    expires_at: str
    revoked_sessions: int
    audit_run_id: int

class DigestTagProvenance(BaseModel):
    source_image: str
    resolved_tag: str
    watch_tag: str
    target_digest: str
    final_image: str
    provenance_source: str
    provenance_confidence: str


class WudApiStatus(BaseModel):
    state: WudApiState
    available: bool
    metadata_available: bool
    last_checked_at: str
    detail: str = ""


class WudContainerMetadata(BaseModel):
    id: str
    name: str
    display_name: str
    status: str
    watcher: str
    local_tag: str
    local_digest: str
    remote_tag: str
    remote_digest: str
    update_kind: str
    semver_diff: str
    link: str
    error: str
    platform: str = ""
    platform_os: str = ""
    platform_architecture: str = ""
    platform_variant: str = ""

PendingSourceMode = Literal["file", "api", "auto"]
PendingSourceActive = Literal["file", "api"]


class PendingSourceInfo(BaseModel):
    configured: PendingSourceMode = "file"
    active: PendingSourceActive = "file"
    label: str = "Pending file"
    fresh: bool = True
    degraded: bool = False
    fallback_reason: str = ""
    detail: str = ""


class WudApiDiagnosticEndpointStatus(BaseModel):
    state: WudApiState = "unavailable"
    available: bool = False
    last_checked_at: str = ""
    detail: str = ""


class WudApiAppDiagnostics(BaseModel):
    status: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    name: str = ""
    version: str = ""


class WudApiLogDiagnostics(BaseModel):
    status: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    level: str = ""


class WudApiStoreDiagnostics(BaseModel):
    status: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    path: str = ""
    file: str = ""
    configuration: dict[str, Any] = Field(default_factory=dict)


class WudApiWatcherDiagnostics(BaseModel):
    id: str = ""
    type: str = ""
    name: str = ""
    cron: str = ""
    watch_by_default: bool | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class WudApiRegistryDiagnostics(BaseModel):
    id: str = ""
    type: str = ""
    name: str = ""
    configuration: dict[str, Any] = Field(default_factory=dict)


class WudApiConfigurationDiagnostics(BaseModel):
    health: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    app: WudApiAppDiagnostics = Field(default_factory=WudApiAppDiagnostics)
    log: WudApiLogDiagnostics = Field(default_factory=WudApiLogDiagnostics)
    store: WudApiStoreDiagnostics = Field(default_factory=WudApiStoreDiagnostics)
    watchers_status: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    watchers: list[WudApiWatcherDiagnostics] = Field(default_factory=list)
    registries_status: WudApiDiagnosticEndpointStatus = Field(
        default_factory=WudApiDiagnosticEndpointStatus
    )
    registries: list[WudApiRegistryDiagnostics] = Field(default_factory=list)


class WudApiObservationDiagnostic(BaseModel):
    outcome: WudApiObservationOutcome
    reason_code: WudApiObservationReason
    container_id: str = ""
    name: str = ""
    image: str = ""
    registry: str = ""
    watcher: str = ""
    update_available: bool | None = None
    usable_result: bool = False
    retryable: bool = False
    error: str = ""


class WudApiObservationCounts(BaseModel):
    available: int = 0
    degraded: int = 0
    retained: int = 0
    recovered: int = 0
    unresolved: int = 0
    unsupported_ignored: int = 0


class WudApiObservationDiagnostics(BaseModel):
    counts: WudApiObservationCounts = Field(default_factory=WudApiObservationCounts)
    items: list[WudApiObservationDiagnostic] = Field(default_factory=list)


class PendingTagStream(BaseModel):
    current_stream: str
    reported_stream: str


class PendingItem(BaseModel):
    line_no: int
    raw: str
    image: str
    key: str
    repo: str
    current_tag: str
    has_tag: bool
    allow_repo: bool
    digest: str
    desired_tag: str
    platform: str = ""
    platform_os: str = ""
    platform_architecture: str = ""
    platform_variant: str = ""
    digest_provenance: DigestTagProvenance | None = None
    wud_metadata: WudContainerMetadata | None = None
    source: PendingSourceActive = "file"
    source_id: str = ""
    tag_stream: PendingTagStream | None = None
    metadata_status: PendingMetadataStatus = "fresh"


class PendingDiagnostic(BaseModel):
    code: str
    message: str
    hint: str = ""
    stack: str = ""
    service: str = ""
    compose_file: str = ""
    found_files: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

class PendingGroupedItem(PendingItem):
    resolved_image: str
    target_image: str
    compose_images: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    action: str
    selection_id: str = ""
    diagnostic: PendingDiagnostic | None = None
    runtime_state: PendingRuntimeState = "unknown"
    running_services: list[str] = Field(default_factory=list)
    stopped_services: list[str] = Field(default_factory=list)

class PendingStackGroup(BaseModel):
    name: str
    directory: str
    compose_file: str
    project_directory: str
    project_name: str
    services_label: str
    services: list[str] = Field(default_factory=list)
    line_numbers: list[int] = Field(default_factory=list)
    items: list[PendingGroupedItem] = Field(default_factory=list)


class PendingGrouping(BaseModel):
    status: PendingGroupingStatus
    groups: list[PendingStackGroup] = Field(default_factory=list)
    unmatched: list[PendingGroupedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PendingSnoozedCandidate(BaseModel):
    key: str
    service_key: str
    stack: str
    service: str
    image: str
    target_image: str
    current_tag: str
    desired_tag: str
    digest: str
    source_id: str
    wud_metadata: WudContainerMetadata
    snooze_kind: SnoozeKind
    reason: str
    snoozed_until: str | None = None
    wait_for_service_key: str = ""

class PendingResponse(BaseModel):
    source_file: str
    source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    source_hash: str = ""
    exists: bool
    count: int
    items: list[PendingItem] = Field(default_factory=list)
    grouping: PendingGrouping = Field(
        default_factory=lambda: PendingGrouping(status="unavailable")
    )
    snoozed_candidates: list[PendingSnoozedCandidate] = Field(default_factory=list)
    wud_api: WudApiStatus = Field(
        default_factory=lambda: WudApiStatus(
            state="unavailable",
            available=False,
            metadata_available=False,
            last_checked_at="",
        )
    )
    warnings: list[str] = Field(default_factory=list)

UpdateTargetsStatus = Literal["ready", "unavailable"]

class UpdateTargetItem(BaseModel):
    service_key: str
    stack: str
    service: str
    image: str
    image_repo: str
    current_tag: str
    directory: str
    compose_file: str
    project_directory: str

class UpdateTargetsResponse(BaseModel):
    status: UpdateTargetsStatus
    count: int
    items: list[UpdateTargetItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

RetagTargetsStatus = Literal["ready", "unavailable"]

RetagPlanStatus = Literal["ready", "empty", "blocked", "unavailable"]

RetagRuntimeState = Literal["running", "not-running", "unknown"]

class RetagTargetItem(BaseModel):
    target_id: str
    service_key: str
    stack: str
    service: str
    image: str
    image_repo: str
    current_tag: str
    tracking_tag: str
    tracking_tag_source: str
    proposed_tag: str
    final_image: str
    candidate_source: str = ""
    candidate_warning: str = ""
    candidate_link_label: str = ""
    candidate_link_url: str = ""
    runtime_state: RetagRuntimeState = "unknown"
    retag_available: bool
    retag_reason: str
    choices: list[str] = Field(default_factory=list)
    label_key: str
    label_value: str
    directory: str
    compose_file: str
    project_directory: str
    digest_provenance: DigestTagProvenance | None = None

class RetagTargetsResponse(BaseModel):
    status: RetagTargetsStatus
    count: int
    items: list[RetagTargetItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TrackedContainerItem(BaseModel):
    target_id: str
    service_key: str
    stack: str
    service: str
    compose_path: str
    image: str
    current_tag: str
    runtime_state: RetagRuntimeState
    tracking_regex: str = ""
    tracking_health: str
    tracking_detail: str
    suggested_regex: str = ""
    wud: WudContainerMetadata | None = None
    wud_match_state: Literal["watching", "untracked", "unknown", "ambiguous"] = "unknown"
    wud_update_available: bool | None = None
    last_image_recorded_at: str = ""
    last_action_at: str = ""
    last_action_status: str = ""
    last_action_run_id: int | None = None
    retag_available: bool = False


class TrackedContainersResponse(BaseModel):
    status: RetagTargetsStatus
    count: int
    items: list[TrackedContainerItem] = Field(default_factory=list)
    wud_status: WudApiStatus | None = None
    warnings: list[str] = Field(default_factory=list)


class TrackingRepairRequest(BaseModel):
    target_id: str = Field(min_length=1, max_length=128)
    regex: str = Field(min_length=3, max_length=256)


class TrackingRepairPlan(BaseModel):
    plan_id: str
    source_hash: str
    rendered_hash: str
    target_id: str
    service_key: str
    stack: str
    service: str
    image: str
    current_regex: str
    proposed_regex: str
    compose_diff: str
    will_recreate: bool
    can_apply: bool
    issues: list[str] = Field(default_factory=list)


class TrackingRepairApplyRequest(TrackingRepairRequest):
    plan_id: str = Field(min_length=1, max_length=128)
    confirmation: Literal["apply-tracking-repair"]

class RetagChoiceRequest(BaseModel):
    service_key: str = Field(min_length=1, max_length=512)
    target_id: str | None = Field(default=None, min_length=1, max_length=128)
    choice: Literal["keep-current", "switch-to-concrete"]
    target_tag: str | None = Field(default=None, max_length=128)
    allow_start: bool = False

    @model_validator(mode="after")
    def target_tag_requires_switch_choice(self) -> RetagChoiceRequest:
        if self.choice == "keep-current" and self.target_tag is not None:
            raise ValueError(
                "target_tag is only allowed when choice is switch-to-concrete"
            )
        if self.choice == "keep-current" and self.allow_start:
            raise ValueError(
                "allow_start is only allowed when choice is switch-to-concrete"
            )
        return self

class RetagPlanRequest(BaseModel):
    choices: list[RetagChoiceRequest] = Field(min_length=1)
    github_latest_fallback: bool = False

class RetagApplyRequest(BaseModel):
    plan_id: str = Field(min_length=1)
    choices: list[RetagChoiceRequest] = Field(min_length=1)
    github_latest_fallback: bool = False
    confirmation: Literal["apply-retags"]

class RetagPlanIssue(BaseModel):
    severity: str
    code: str
    message: str
    service_key: str = ""
    stack: str = ""
    service: str = ""
    hint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

class RetagPlanLabelRewrite(BaseModel):
    service: str
    label_key: str
    current_label_value: str
    planned_tag: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str

class RetagPlanDigestPinUpdate(BaseModel):
    target_id: str = ""
    service_key: str
    stack: str
    service: str
    source_image: str
    resolved_tag: str
    planned_digest: str
    final_image: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    label_rewrites: list[RetagPlanLabelRewrite] = Field(default_factory=list)
    digest_provenance: DigestTagProvenance | None = None


class RetagPlanTagUpdate(BaseModel):
    target_id: str = ""
    service_key: str
    stack: str
    service: str
    source_image: str
    target_tag: str
    final_image: str
    label_key: str
    label_value: str
    label_rewrites: list[RetagPlanLabelRewrite] = Field(default_factory=list)


class RetagPlanStack(BaseModel):
    stack: str
    directory: str
    compose_file: str
    project_directory: str
    services: list[str] = Field(default_factory=list)
    tag_updates: list[RetagPlanTagUpdate] = Field(default_factory=list)
    digest_pin_updates: list[RetagPlanDigestPinUpdate] = Field(default_factory=list)

class RetagPlanResponse(BaseModel):
    plan_id: str
    status: RetagPlanStatus
    can_apply: bool
    external_recreate_required: bool = False
    selected_count: int = 0
    keep_current_count: int = 0
    stacks: list[RetagPlanStack] = Field(default_factory=list)
    issues: list[RetagPlanIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

class RetagPreviewJobResponse(BaseModel):
    preview_job_id: str
    status: ApplyJobStatus
    plan: RetagPlanResponse | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str = ""
    progress: list[ApplyJobProgressEvent] = Field(default_factory=list)

class ReleaseNoteLink(BaseModel):
    label: str
    url: str
    kind: str

class ReleaseNoteClassificationTag(BaseModel):
    raw: str = ""
    kind: str = "unknown"
    arch: str = ""
    branch: str = ""
    upstream_version: str = ""
    build_suffix: str = ""

class ReleaseNoteClassification(BaseModel):
    change_type: ReleaseNoteChangeType = "unknown"
    reason: str = "ambiguous-tags"
    current: ReleaseNoteClassificationTag = Field(
        default_factory=ReleaseNoteClassificationTag
    )
    target: ReleaseNoteClassificationTag = Field(
        default_factory=ReleaseNoteClassificationTag
    )

class ReleaseSecurityAssessment(BaseModel):
    outcome: ReleaseSecurityOutcome = "ordinary"
    severity: ReleaseSecuritySeverity = "none"
    reason_code: str = "no_security_signal"
    reason: str = "No security urgency signal was found in the release notes."
    advisory_ids: list[str] = Field(default_factory=list)
    lookup_truncated: bool = False

class ReleaseNoteInfo(BaseModel):
    line_no: int
    status: str
    provider: str
    image_repo: str
    upstream_repo: str
    release_tag: str = ""
    title: str = ""
    published_at: str = ""
    breaking: bool = False
    breaking_reasons: list[str] = Field(default_factory=list)
    links: list[ReleaseNoteLink] = Field(default_factory=list)
    refreshed_at: str = ""
    error: str = ""
    body: str = ""
    classification: ReleaseNoteClassification = Field(
        default_factory=ReleaseNoteClassification
    )
    security: ReleaseSecurityAssessment = Field(
        default_factory=ReleaseSecurityAssessment
    )
    notification_key: str = ""
    notification_status: str = "new"
    notification_last_sent_at: str = ""
    notification_send_count: int = 0
    notification_skipped_reason: str = ""

class ReleaseNotesResponse(BaseModel):
    source_file: str
    source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    count: int
    items: list[ReleaseNoteInfo] = Field(default_factory=list)
    enabled: bool = True
    disabled_reason: str = ""
    notifications_enabled: bool = True
    notifications_disabled_reason: str = ""
    wud_api: WudApiStatus = Field(
        default_factory=lambda: WudApiStatus(
            state="unavailable",
            available=False,
            metadata_available=False,
            last_checked_at="",
        )
    )
    warnings: list[str] = Field(default_factory=list)

class ReleaseNotificationPreviewRequest(BaseModel):
    line_numbers: list[LineNumber] = Field(default_factory=list)
    run_id: int | None = Field(default=None, ge=1)
    resend: bool = False

    @model_validator(mode="after")
    def exactly_one_source(self) -> ReleaseNotificationPreviewRequest:
        if bool(self.line_numbers) == bool(self.run_id):
            raise ValueError("provide exactly one of line_numbers or run_id")
        return self

class ReleaseNotificationSendRequest(ReleaseNotificationPreviewRequest):
    confirmation: Literal["send-release-notes"]

class ReleaseNotificationTestRequest(BaseModel):
    confirmation: Literal["send-test-webhook"]

class ReleaseNotificationDestination(BaseModel):
    type: Literal["discord"] = "discord"
    configured: bool = False
    source: str = ""

class ReleaseNotificationTestResponse(BaseModel):
    sent: bool = False
    destination: ReleaseNotificationDestination = Field(
        default_factory=ReleaseNotificationDestination
    )
    audit_run_id: int = 0

class ReleaseNotificationTrigger(BaseModel):
    id: str = ""
    type: str = ""
    name: str = ""

class ReleaseNotificationItem(BaseModel):
    line_no: int
    image: str
    service_key: str = ""
    title: str
    description: str
    status: str
    release_tag: str = ""
    image_repo: str = ""
    upstream_repo: str = ""
    current_version: str = ""
    target_version: str = ""
    category: Literal[
        "security_urgent", "needs_review", "worth_noting", "routine"
    ] = "needs_review"
    reason_code: str = ""
    reason_label: str = ""
    security: ReleaseSecurityAssessment = Field(
        default_factory=ReleaseSecurityAssessment
    )
    links: list[ReleaseNoteLink] = Field(default_factory=list)
    triggers: list[ReleaseNotificationTrigger] = Field(default_factory=list)
    notification_key: str = ""
    notification_status: str = "new"
    notification_last_sent_at: str = ""
    notification_send_count: int = 0
    skipped_reason: str = ""

class ReleaseNotificationResponse(BaseModel):
    enabled: bool
    mode: Literal["digest", "per_container"] = "digest"
    resend_policy: Literal["remote_change", "cooldown"] = "remote_change"
    destination: ReleaseNotificationDestination = Field(
        default_factory=ReleaseNotificationDestination
    )
    source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    source_file: str = ""
    count: int = 0
    sendable_count: int = 0
    skipped_count: int = 0
    batch_count: int = 0
    messages: list[str] = Field(default_factory=list)
    items: list[ReleaseNotificationItem] = Field(default_factory=list)
    wud_api: WudApiStatus = Field(
        default_factory=lambda: WudApiStatus(
            state="unavailable",
            available=False,
            metadata_available=False,
            last_checked_at="",
        )
    )
    warnings: list[str] = Field(default_factory=list)
    sent: bool = False
    audit_run_id: int = 0
    error: str = ""

class HealthResponse(BaseModel):
    ok: bool
    version: str

class StatusResponse(BaseModel):
    ok: bool
    version: str
    wud_file: str
    wud_file_exists: bool
    pending_count: int
    pending_source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    source_hash: str = ""
    db_path: str
    db_ready: bool
    auth_required: bool
    dev_auth_bypass: bool
    setup_required: bool
    mutations_enabled: bool
    timezone: str
    auto_update_scheduler_enabled: bool
    static_spa_available: bool
    wud_api: WudApiStatus = Field(
        default_factory=lambda: WudApiStatus(
            state="unavailable",
            available=False,
            metadata_available=False,
            last_checked_at="",
        )
    )
    warnings: list[str] = Field(default_factory=list)

class SettingsEntry(BaseModel):
    name: str
    value: str
    default_value: str
    configured: bool
    source: SettingsEntrySource

class SecretSettingStatus(BaseModel):
    name: str
    configured: bool

class ManagedSettingEntry(BaseModel):
    key: str
    value: str
    default_value: str
    source: ManagedSettingSource
    editable: bool
    allowed_values: list[str] = Field(default_factory=list)
    restart_required: bool
    disabled_reason: str = ""
    configured: bool = False
    sensitive: bool = False

class ManagedSettingsUpdateRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)

class ManagedSettingsUpdateResponse(BaseModel):
    managed: list[ManagedSettingEntry] = Field(default_factory=list)
    audit_run_id: int

class SettingsResponse(BaseModel):
    updater: list[SettingsEntry] = Field(default_factory=list)
    webui: list[SettingsEntry] = Field(default_factory=list)
    secrets: list[SecretSettingStatus] = Field(default_factory=list)
    managed: list[ManagedSettingEntry] = Field(default_factory=list)

class DoctorSuggestionResponse(BaseModel):
    label: str
    description: str = ""
    snippet: str = ""

class DoctorCheckResponse(BaseModel):
    status: DoctorCheckStatus
    code: str
    category: str
    name: str
    detail: str = ""
    target: str = ""
    suggestions: list[DoctorSuggestionResponse] = Field(default_factory=list)

class DoctorResponse(BaseModel):
    ok: bool
    failures: int
    warnings: int
    checks: list[DoctorCheckResponse] = Field(default_factory=list)

class ReadyResponse(BaseModel):
    ok: bool
    version: str
    checks: list[DoctorCheckResponse] = Field(default_factory=list)

class ApplyPreflightCheck(BaseModel):
    status: DoctorCheckStatus
    code: str
    label: str
    detail: str = ""
    source_check_codes: list[str] = Field(default_factory=list)

class ApplyPreflightResponse(BaseModel):
    ok: bool
    failures: int
    warnings: int
    checks: list[ApplyPreflightCheck] = Field(default_factory=list)

class OnboardingDocLink(BaseModel):
    label: str
    url: str

class OnboardingChecklistItem(BaseModel):
    key: str
    title: str
    status: DoctorCheckStatus
    detail: str = ""
    check_codes: list[str] = Field(default_factory=list)
    suggestions: list[DoctorSuggestionResponse] = Field(default_factory=list)
    docs: list[OnboardingDocLink] = Field(default_factory=list)

class OnboardingChecklistResponse(BaseModel):
    dismissed: bool
    dismissed_at: str
    all_passed: bool
    visible: bool
    items: list[OnboardingChecklistItem] = Field(default_factory=list)

class OnboardingDismissResponse(BaseModel):
    dismissed: bool
    dismissed_at: str

class CoreUpdateTourResponse(BaseModel):
    status: CoreUpdateTourStatus
    step: CoreUpdateTourStep
    updated_at: str = ""

class CoreUpdateTourUpdateRequest(BaseModel):
    status: CoreUpdateTourStatus
    step: CoreUpdateTourStep = DEFAULT_CORE_UPDATE_TOUR_STEP

class CsrfResponse(BaseModel):
    csrf_token: str

class SetupStatusResponse(BaseModel):
    setup_required: bool
    claim_required: bool
    authenticated: bool
    auth_required: bool
    dev_auth_bypass: bool
    mutations_enabled: bool
    password_min_length: int

class SetupClaimRequest(BaseModel):
    claim: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)

class ResetAdminClaimRequest(BaseModel):
    claim: str = Field(min_length=1)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)

class AuthSessionResponse(BaseModel):
    authenticated: bool
    setup_required: bool
    auth_required: bool
    dev_auth_bypass: bool
    mutations_enabled: bool
    username: str | None = None

class RunEventRecord(BaseModel):
    id: int
    run_id: int
    created_at: str
    service_name: str
    stack_name: str
    image: str
    target_image: str
    old_image_id: str
    new_image_id: str
    old_digest: str
    new_digest: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    digest_provenance: DigestTagProvenance | None = None

class RunSummary(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    status: str
    dry_run: bool
    mode: str
    wud_file: str
    log_file: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[RunEventRecord] = Field(default_factory=list)

class PendingUpdateRecord(BaseModel):
    id: int
    run_id: int
    line_no: int
    raw: str
    image: str
    target_digest: str
    desired_tag: str
    service_key: str
    stack_name: str
    service_name: str
    status: str
    status_reason: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    digest_provenance: DigestTagProvenance | None = None

RunVerificationStatus = Literal["verified", "needs_review"]
RunVerificationImageStatus = Literal[
    "new_image_running",
    "already_current",
    "failed",
    "unknown",
]
RunVerificationContainerStatus = Literal["recreated", "skipped", "failed", "unknown"]
RunVerificationHealthStatus = Literal[
    "passed",
    "skipped",
    "timed_out",
    "service_disappeared",
    "failed",
    "unknown",
]
RunVerificationWudStatus = Literal[
    "removed",
    "restored",
    "stale_removed",
    "removed_before_run",
    "unknown",
]

SecurityScanState = Literal[
    "disabled",
    "not_scanned",
    "queued",
    "running",
    "complete",
    "stale",
    "partial",
    "unsupported",
    "unavailable_offline",
    "auth_required",
    "error",
]

SecurityScanVerdict = Literal["findings", "none_reported", "unknown"]


class SecurityScanSeverityCounts(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    unknown: int = 0


class SecurityScanFinding(BaseModel):
    target: str = ""
    target_class: str = ""
    target_type: str = ""
    vulnerability_id: str = ""
    package_name: str = ""
    installed_version: str = ""
    fixed_version: str = ""
    severity: Literal["critical", "high", "medium", "low", "unknown"] = "unknown"
    title: str = ""
    primary_url: str = ""


class SecurityScanSubject(BaseModel):
    requested_ref: str = ""
    reported_digest: str = ""
    index_digest: str = ""
    manifest_digest: str = ""
    immutable_ref: str = ""
    platform: str = ""


class SecurityScanComparison(BaseModel):
    status: Literal["unknown", "improved", "unchanged", "mixed", "worse"] = "unknown"
    current_subject: SecurityScanSubject = Field(default_factory=SecurityScanSubject)
    fixed_findings: list[SecurityScanFinding] = Field(default_factory=list)
    remaining_findings: list[SecurityScanFinding] = Field(default_factory=list)
    introduced_findings: list[SecurityScanFinding] = Field(default_factory=list)
    message: str = ""


class SecurityScanInfo(BaseModel):
    line_no: int
    state: SecurityScanState
    verdict: SecurityScanVerdict = "unknown"
    scanner: str = ""
    scanner_version: str = ""
    scanner_schema: str = ""
    scanned_at: str = ""
    db_revision: str = ""
    db_updated_at: str = ""
    severity_counts: SecurityScanSeverityCounts = Field(
        default_factory=SecurityScanSeverityCounts
    )
    advisory_counts: SecurityScanSeverityCounts = Field(
        default_factory=SecurityScanSeverityCounts
    )
    advisory_counts_known: bool = False
    fixable_counts: SecurityScanSeverityCounts = Field(
        default_factory=SecurityScanSeverityCounts
    )
    unfixed_count: int = 0
    findings: list[SecurityScanFinding] = Field(default_factory=list)
    subject: SecurityScanSubject = Field(default_factory=SecurityScanSubject)
    comparison: SecurityScanComparison = Field(default_factory=SecurityScanComparison)
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    error_message: str = ""


class SecurityScansResponse(BaseModel):
    source_file: str
    source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    source_hash: str = ""
    scanning_enabled: bool = False
    scanner: Literal["trivy"] = "trivy"
    scan_mode: Literal["registry"] = "registry"
    count: int = 0
    items: list[SecurityScanInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SecurityScanJobResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "success", "failure"]
    total_count: int = 0
    completed_count: int = 0
    result: SecurityScansResponse | None = None
    error: str = ""


class RunVerificationItem(BaseModel):
    line_no: int
    event_id: int | None = None
    service_key: str = ""
    stack_name: str = ""
    service_name: str = ""
    image: str = ""
    target_image: str = ""
    image_status: RunVerificationImageStatus = "unknown"
    container_status: RunVerificationContainerStatus = "unknown"
    health_status: RunVerificationHealthStatus = "unknown"
    wud_status: RunVerificationWudStatus = "unknown"
    follow_up_needed: bool = True
    summary: str = ""


class RunVerificationSummary(BaseModel):
    status: RunVerificationStatus = "verified"
    total_count: int = 0
    verified_count: int = 0
    needs_review_count: int = 0
    items: list[RunVerificationItem] = Field(default_factory=list)


class RunHistorySummary(RunSummary):
    verification: RunVerificationSummary = Field(default_factory=RunVerificationSummary)
    verification_omitted_count: int = 0


class RunDetail(RunSummary):
    pending_updates: list[PendingUpdateRecord] = Field(default_factory=list)
    verification: RunVerificationSummary = Field(default_factory=RunVerificationSummary)


RollbackPlanStatus = Literal[
    "ready",
    "partial",
    "blocked",
    "not_needed",
    "not_applicable",
    "unavailable",
]

RollbackPlanItemStatus = Literal["ready", "blocked", "not_needed"]


class RollbackPlanItem(BaseModel):
    event_id: int
    service_key: str
    stack_name: str
    service_name: str
    status: RollbackPlanItemStatus
    reason: str
    recorded_previous_image: str
    recorded_target_image: str
    rollback_image: str = ""
    previous_image_id: str
    previous_digest: str
    current_compose_image: str = ""
    current_container_image_ids: list[str] = Field(default_factory=list)


class RollbackPlanResponse(BaseModel):
    run_id: int
    status: RollbackPlanStatus
    detail: str
    ready_count: int = 0
    blocked_count: int = 0
    not_needed_count: int = 0
    items: list[RollbackPlanItem] = Field(default_factory=list)


class RunLogResponse(BaseModel):
    run_id: int
    log_file: str
    exists: bool
    content: str
    truncated: bool
    max_bytes: int

class DiagnosticsSupportBundleResponse(BaseModel):
    wudup_version: str
    wud_updater_version: str = ""
    settings: SettingsResponse
    doctor_result: DoctorResponse
    wud_api_diagnostics: WudApiConfigurationDiagnostics = Field(
        default_factory=WudApiConfigurationDiagnostics
    )
    wud_api_observations: WudApiObservationDiagnostics = Field(
        default_factory=WudApiObservationDiagnostics
    )
    pending_summary: PendingResponse
    last_run_status: RunSummary | None
    diagnostics_warnings: list[str] = Field(default_factory=list)
    discovery_warnings: list[str] = Field(default_factory=list)
    log_tail: LogTail | None

class TagOverrideRequest(BaseModel):
    line_no: LineNumber
    tag: str = Field(min_length=1, max_length=128)


class TagStreamDecisionRequest(BaseModel):
    line_no: LineNumber
    decision: Literal["preserve", "switch"]


class TagStreamLabelRewriteApprovalRequest(BaseModel):
    line_no: LineNumber
    stack: str = Field(min_length=1, max_length=256)
    stack_directory: str = Field(min_length=1, max_length=4096)
    compose_file: str = Field(min_length=1, max_length=256)
    service: str = Field(min_length=1, max_length=256)
    label_key: str = Field(min_length=1, max_length=256)
    current_label_value: str = Field(min_length=1, max_length=512)
    selected_tag: str = Field(min_length=1, max_length=128)
    proposed_label_value: str = Field(min_length=1, max_length=512)

class DigestPinLabelRewriteApprovalRequest(BaseModel):
    stack: str = Field(min_length=1, max_length=256)
    service: str = Field(min_length=1, max_length=256)
    label_key: str = Field(min_length=1, max_length=256)
    current_label_value: str = Field(min_length=1, max_length=512)
    planned_tag: str = Field(min_length=1, max_length=128)
    proposed_label_value: str = Field(min_length=1, max_length=512)

class PlanSelectionRequest(BaseModel):
    line_no: LineNumber
    selection_id: str = Field(default="", max_length=128)

class PlanRequest(BaseModel):
    line_numbers: list[LineNumber] = Field(default_factory=list)
    selections: list[PlanSelectionRequest] = Field(default_factory=list)
    allow_tag_updates: bool = False
    tag_overrides: list[TagOverrideRequest] = Field(default_factory=list)
    tag_stream_decisions: list[TagStreamDecisionRequest] = Field(default_factory=list)
    tag_stream_label_rewrite_approvals: list[
        TagStreamLabelRewriteApprovalRequest
    ] = Field(default_factory=list)
    digest_pin_label_rewrite_approvals: list[
        DigestPinLabelRewriteApprovalRequest
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection_mode(self) -> PlanRequest:
        if bool(self.line_numbers) == bool(self.selections):
            raise ValueError("provide exactly one of line_numbers or selections")
        return self

class PlanSummary(BaseModel):
    target_count: int
    matched_target_count: int
    stack_count: int
    service_count: int
    skipped_count: int
    issue_count: int

class PlanIssue(BaseModel):
    severity: str
    code: str
    message: str
    line_no: int | None = None
    stack: str = ""
    service: str = ""
    hint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

class PlanTarget(BaseModel):
    line_no: int
    raw: str
    image: str
    resolved_image: str
    digest: str
    desired_tag: str
    matched: bool
    action: str
    metadata_status: PendingMetadataStatus = "fresh"

class PlanLine(BaseModel):
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
    metadata_status: PendingMetadataStatus = "fresh"
    digest_provenance: DigestTagProvenance | None = None

class PlanTagUpdate(BaseModel):
    old_image: str
    desired_tag: str
    new_image: str
    services: list[str] = Field(default_factory=list)


class PlanTagStreamUpdate(BaseModel):
    line_no: int
    service: str
    current_tag: str
    reported_tag: str
    selected_tag: str
    decision: Literal["preserve", "switch"]
    label_key: str
    current_label_value: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str

class PlanDigestPinLabelRewrite(BaseModel):
    service: str
    label_key: str
    current_label_value: str
    planned_tag: str
    proposed_label_value: str
    proposed_label_regex: str
    approved: bool
    reason: str

class PlanDigestPinUpdate(BaseModel):
    source_image: str
    resolved_tag: str
    planned_digest: str
    final_image: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: list[str] = Field(default_factory=list)
    label_rewrites: list[PlanDigestPinLabelRewrite] = Field(default_factory=list)
    digest_provenance: DigestTagProvenance | None = None

class PlanDigestUnpinUpdate(BaseModel):
    source_image: str
    resolved_tag: str
    tag_image: str
    current_digest: str
    target_digest: str
    watch_tag: str
    marker: str
    label_key: str
    label_value: str
    services: list[str] = Field(default_factory=list)
    digest_provenance: DigestTagProvenance | None = None

class PlanAction(BaseModel):
    kind: str
    description: str
    cwd: str
    args: list[str] = Field(default_factory=list)

class PlanStack(BaseModel):
    name: str
    directory: str
    compose_file: str
    project_directory: str
    services_label: str
    services: list[str] = Field(default_factory=list)
    pull_services: list[str] = Field(default_factory=list)
    stop_services: list[str] = Field(default_factory=list)
    force_recreate: bool
    up_no_deps: bool
    tag_updates: list[PlanTagUpdate] = Field(default_factory=list)
    tag_stream_updates: list[PlanTagStreamUpdate] = Field(default_factory=list)
    digest_pin_updates: list[PlanDigestPinUpdate] = Field(default_factory=list)
    digest_unpin_updates: list[PlanDigestUnpinUpdate] = Field(default_factory=list)
    actions: list[PlanAction] = Field(default_factory=list)
    lines: list[PlanLine] = Field(default_factory=list)

class PlanSkipped(BaseModel):
    line_no: int
    raw: str
    image: str
    desired_tag: str
    reason: str

class PlanCleanupItem(BaseModel):
    line_no: int
    raw: str
    image: str
    desired_tag: str
    digest: str
    reason: str
    diagnostic: PendingDiagnostic | None = None

class PlanCleanup(BaseModel):
    cleanup_id: str = ""
    can_remove_unmatched: bool = False
    items: list[PlanCleanupItem] = Field(default_factory=list)

class PlanResponse(BaseModel):
    plan_id: str
    dry_run: bool
    can_apply: bool
    status: PlanStatus
    source_file: str
    source: PendingSourceInfo = Field(default_factory=PendingSourceInfo)
    mode: str
    max_wait: int
    digest_pin_updates: bool
    selected_line_numbers: list[int] = Field(default_factory=list)
    selected_selections: list[PlanSelectionRequest] = Field(default_factory=list)
    summary: PlanSummary
    targets: list[PlanTarget] = Field(default_factory=list)
    stacks: list[PlanStack] = Field(default_factory=list)
    skipped: list[PlanSkipped] = Field(default_factory=list)
    issues: list[PlanIssue] = Field(default_factory=list)
    cleanup: PlanCleanup = Field(default_factory=PlanCleanup)
    apply_preflight: ApplyPreflightResponse

class PendingCleanupLine(BaseModel):
    line_no: LineNumber
    raw: str

class PendingCleanupRequest(BaseModel):
    cleanup_id: str = Field(min_length=1)
    lines: list[PendingCleanupLine] = Field(min_length=1)
    confirmation: Literal["remove_unmatched"]

class PendingCleanupRemovedLine(BaseModel):
    line_no: int
    raw: str
    image: str
    reason: str

class PendingCleanupResponse(BaseModel):
    status: Literal["success"]
    audit_run_id: int
    removed_count: int
    removed: list[PendingCleanupRemovedLine] = Field(default_factory=list)

class PendingMetadataRefreshLine(BaseModel):
    line_no: LineNumber
    raw: str
    source_id: str = ""

class PendingMetadataRefreshRequest(BaseModel):
    source_hash: str
    lines: list[PendingMetadataRefreshLine] = Field(default_factory=list)

class PendingMetadataRefreshItem(BaseModel):
    line_no: LineNumber
    raw: str
    source_id: str = ""
    wud_metadata: WudContainerMetadata | None = None
    metadata_status: PendingMetadataStatus = "fresh"

class PendingMetadataRefreshResponse(BaseModel):
    status: PendingMetadataRefreshStatus
    requires_pending_reload: bool
    source_hash: str
    source: PendingSourceInfo
    wud_api: WudApiStatus
    items: list[PendingMetadataRefreshItem] = Field(default_factory=list)

class PendingRescanSkippedLine(BaseModel):
    line_no: int
    raw: str
    reason: str

class PendingRescanLine(BaseModel):
    line_no: LineNumber
    raw: str
    source_id: str = ""
    source_hash: str = ""
    container_id: str = ""

class PendingRescanRequest(BaseModel):
    confirmation: Literal["rescan_wud"]
    scope: PendingRescanScope
    line_numbers: list[LineNumber] = Field(default_factory=list)
    lines: list[PendingRescanLine] = Field(default_factory=list)

class PendingRescanResponse(BaseModel):
    status: PendingRescanStatus
    audit_run_id: int
    scope: PendingRescanScope
    requested_count: int
    watched_count: int
    skipped: list[PendingRescanSkippedLine] = Field(default_factory=list)
    wud_api: WudApiStatus

class PendingRemovalPlanRequest(BaseModel):
    line_numbers: list[LineNumber] = Field(min_length=1)

class PendingRemovalPlanLine(BaseModel):
    line_no: int
    raw: str
    image: str
    desired_tag: str = ""
    digest: str = ""

class PendingRemovalPlanResponse(BaseModel):
    removal_id: str
    source_file: str
    can_remove: bool
    selected_line_numbers: list[int] = Field(default_factory=list)
    lines: list[PendingRemovalPlanLine] = Field(default_factory=list)

class PendingRemovalRequest(BaseModel):
    removal_id: str = Field(min_length=1)
    lines: list[PendingCleanupLine] = Field(min_length=1)
    confirmation: Literal["remove_selected"]

class ApplyPlanRequest(BaseModel):
    plan_id: str = Field(min_length=1)
    line_numbers: list[LineNumber] = Field(default_factory=list)
    selections: list[PlanSelectionRequest] = Field(default_factory=list)
    allow_tag_updates: bool = False
    tag_overrides: list[TagOverrideRequest] = Field(default_factory=list)
    tag_stream_decisions: list[TagStreamDecisionRequest] = Field(default_factory=list)
    tag_stream_label_rewrite_approvals: list[
        TagStreamLabelRewriteApprovalRequest
    ] = Field(default_factory=list)
    digest_pin_label_rewrite_approvals: list[
        DigestPinLabelRewriteApprovalRequest
    ] = Field(default_factory=list)
    confirmation: Literal["apply"]

    @model_validator(mode="after")
    def validate_selection_mode(self) -> ApplyPlanRequest:
        if bool(self.line_numbers) == bool(self.selections):
            raise ValueError("provide exactly one of line_numbers or selections")
        return self

class ApplyJobResponse(BaseModel):
    job_id: str
    status: ApplyJobStatus
    run_id: int | None = None
    log_file: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    selected_line_numbers: list[int] = Field(default_factory=list)
    progress: list[ApplyJobProgressEvent] = Field(default_factory=list)

class ApplyJobProgressEvent(BaseModel):
    job_id: str
    phase: str
    status: ApplyJobProgressStatus
    message: str
    created_at: str
    stack: str = ""
    services: list[str] = Field(default_factory=list)
    line_numbers: list[int] = Field(default_factory=list)

class ApplyJobLogResponse(BaseModel):
    job_id: str
    log_file: str = ""
    exists: bool = False
    content: str = ""
    truncated: bool = False
    max_bytes: int
    error: str = ""

class ServicePolicyRecord(BaseModel):
    service_key: str
    update_mode: str
    auto_update: bool
    snooze_default_seconds: int | None
    auto_update_time: str | None
    auto_update_days: list[AutoUpdateDay] = Field(default_factory=list)
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

class SnoozeRecord(BaseModel):
    id: int
    service_key: str
    snoozed_until: str | None
    reason: str
    created_at: str
    active: bool
    kind: SnoozeKind = "time"
    wait_for_service_key: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

class TagExclusionRuleRecord(BaseModel):
    id: int
    scope: str
    image_repo: str
    service_key: str
    match_type: str
    tag: str
    regex_fragment: str
    status: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)

class UpsertServicePolicyOperation(BaseModel):
    kind: Literal["upsert_service_policy"]
    service_key: str = Field(min_length=1, max_length=512)
    update_mode: ServicePolicyUpdateMode = ""
    auto_update: bool = True
    snooze_default_seconds: int | None = Field(default=None, ge=0)
    auto_update_time: str | None = Field(default=None, max_length=5)
    auto_update_days: list[AutoUpdateDay] = Field(default_factory=list)

class DeleteServicePolicyOperation(BaseModel):
    kind: Literal["delete_service_policy"]
    service_key: str = Field(min_length=1, max_length=512)

class CreateSnoozeOperation(BaseModel):
    kind: Literal["create_snooze"]
    service_key: str = Field(min_length=1, max_length=512)
    snoozed_until: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=1024)

class DeleteSnoozeOperation(BaseModel):
    kind: Literal["delete_snooze"]
    snooze_id: int = Field(ge=1)

class CreateDependencySnoozeOperation(BaseModel):
    kind: Literal["create_dependency_snooze"]
    service_key: str = Field(min_length=1, max_length=512)
    wait_for_service_key: str = Field(min_length=1, max_length=512)
    reason: str = Field(default="", max_length=1024)

class DeleteDependencySnoozeOperation(BaseModel):
    kind: Literal["delete_dependency_snooze"]
    snooze_id: int = Field(ge=1)

class UpsertTagExclusionOperation(BaseModel):
    kind: Literal["upsert_tag_exclusion"]
    scope: TagExclusionScope
    image_repo: str = Field(min_length=1, max_length=512)
    service_key: str = Field(default="", max_length=512)
    match_type: TagExclusionMatchType = "exact"
    tag: str = Field(min_length=1, max_length=128)
    status: TagExclusionStatus = "active"

class SetTagExclusionStatusOperation(BaseModel):
    kind: Literal["set_tag_exclusion_status"]
    rule_id: int = Field(ge=1)
    status: TagExclusionStatus

StateOperation = Annotated[
    UpsertServicePolicyOperation
    | DeleteServicePolicyOperation
    | CreateSnoozeOperation
    | DeleteSnoozeOperation
    | CreateDependencySnoozeOperation
    | DeleteDependencySnoozeOperation
    | UpsertTagExclusionOperation
    | SetTagExclusionStatusOperation,
    Field(discriminator="kind"),
]

class StateOperationResponse(BaseModel):
    operation: str
    status: Literal["success"]
    audit_run_id: int
    resource_type: str
    resource_id: str
    resource: ServicePolicyRecord | SnoozeRecord | TagExclusionRuleRecord | None = None

class ContainerRestartRequest(BaseModel):
    confirmation: Literal["restart_container"]

class ContainerRestartResponse(BaseModel):
    status: Literal["scheduled"]
    audit_run_id: int
    container: str

class SelfUpdateReleaseNote(BaseModel):
    tag: str
    title: str
    published_at: str
    url: str
    body: str = ""
    body_truncated: bool = False
    breaking: bool = False
    breaking_reasons: list[str] = Field(default_factory=list)

class SelfUpdateResponse(BaseModel):
    status: SelfUpdateStatus
    strategy: SelfUpdateStrategy = "pull_image"
    current_tag: str
    latest_tag: str
    current_image: str
    target_image: str
    restart_container: str
    release_notes: list[SelfUpdateReleaseNote] = Field(default_factory=list)
    release_notes_truncated: bool = False
    release_notes_cap: int = SELF_UPDATE_RELEASE_NOTES_CAP
    can_update: bool = False
    disabled_reason: str = ""
    external_recreate_required: bool = False
    warnings: list[str] = Field(default_factory=list)

class SelfUpdateRequest(BaseModel):
    confirmation: Literal["pull_image"]
    current_tag: str
    latest_tag: str
    target_image: str
    restart_container: str

class SelfUpdatePlanResponse(BaseModel):
    strategy: Literal["prepare_tag_update"]
    plan: PlanResponse
    current_tag: str
    latest_tag: str
    current_image: str
    target_image: str
    restart_container: str
    external_recreate_required: bool = True
    warning: str = (
        "This updates the Compose image tag and pulls the image. Recreate the "
        "WUDup container from outside the WebUI to run it."
    )

class SelfUpdatePrepareRequest(BaseModel):
    confirmation: Literal["prepare_tag_update"]
    plan_id: str = Field(min_length=1)
    current_tag: str
    latest_tag: str
    target_image: str
    restart_container: str

class SelfUpdateApplyResponse(BaseModel):
    status: Literal["prepared_only", "running_image_verified"]
    audit_run_id: int
    current_tag: str
    latest_tag: str
    target_image: str
    container: str
    running_image_id: str
    prepared_image_id: str
    external_recreate_required: bool

class SelfUpdatePrepareResponse(BaseModel):
    status: Literal["tag_prepared"]
    audit_run_id: int
    current_tag: str
    latest_tag: str
    target_image: str
    container: str
    external_recreate_required: bool = True
