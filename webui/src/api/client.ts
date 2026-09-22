import { createDemoWebApi } from "./demo";

export type {
  DigestTagProvenance,
  WudApiState,
  WudApiStatus,
  WudContainerMetadata,
  TrackedContainerItem,
  TrackedContainersResponse,
  TrackingRepairPlan,
  WudApiDiagnosticEndpointStatus,
  WudApiAppDiagnostics,
  WudApiLogDiagnostics,
  WudApiStoreDiagnostics,
  WudApiWatcherDiagnostics,
  WudApiRegistryDiagnostics,
  WudApiConfigurationDiagnostics,
  PendingSourceMode,
  PendingSourceActive,
  PendingMetadataStatus,
  PendingSourceInfo,
  // Pending updates
  PendingItem,
  PendingDiagnostic,
  PendingGroupingStatus,
  PendingGroupedItem,
  PendingStackGroup,
  PendingGrouping,
  PendingSnoozedCandidate,
  PendingResponse,
  PendingMetadataRefreshStatus,
  PendingMetadataRefreshLine,
  PendingMetadataRefreshItem,
  PendingMetadataRefreshRequest,
  PendingMetadataRefreshResponse,
  PendingCleanupLine,
  PendingCleanupRemovedLine,
  PendingCleanupResponse,
  PendingRescanLine,
  PendingRescanScope,
  PendingRescanStatus,
  PendingRescanSkippedLine,
  PendingRescanResponse,
  SecurityScanState,
  SecurityScanVerdict,
  SecurityScanSeverityCounts,
  SecurityScanFinding,
  SecurityScanSubject,
  SecurityScanComparisonStatus,
  SecurityScanComparison,
  SecurityScanInfo,
  SecurityScansResponse,
  SecurityScanJobResponse,
  PendingRemovalPlanLine,
  PendingRemovalPlanResponse,
  // Update targets
  UpdateTargetsStatus,
  UpdateTargetItem,
  UpdateTargetsResponse,
  // Retag targets
  RetagTargetsStatus,
  RetagTargetChoice,
  RetagRuntimeState,
  RetagTargetItem,
  RetagTargetsResponse,
  RetagPlanStatus,
  RetagChoiceRequest,
  RetagPlanOptions,
  RetagPlanIssue,
  RetagPlanLabelRewrite,
  RetagPlanDigestPinUpdate,
  RetagPlanTagUpdate,
  RetagPlanStack,
  RetagPlanResponse,
  RetagPreviewJobResponse,
  // Release notes
  ReleaseNoteChangeType,
  ReleaseNoteClassification,
  ReleaseNoteClassificationTag,
  ReleaseSecurityAssessment,
  ReleaseSecurityOutcome,
  ReleaseSecuritySeverity,
  ReleaseNoteLink,
  ReleaseNoteInfo,
  ReleaseNotesResponse,
  ReleaseNotificationDestination,
  ReleaseNotificationTrigger,
  ReleaseNotificationItem,
  ReleaseNotificationSource,
  ReleaseNotificationResponse,
  ReleaseNotificationTestResponse,
  // Plans
  PlanStatus,
  PlanSummary,
  PlanIssue,
  PlanTarget,
  PlanLine,
  PlanTagUpdate,
  PlanTagStreamUpdate,
  TagStreamDecision,
  DigestPinLabelRewriteApprovalRequest,
  PlanDigestPinLabelRewrite,
  PlanDigestPinUpdate,
  PlanDigestUnpinUpdate,
  PlanAction,
  PlanStack,
  PlanSkipped,
  PlanCleanupItem,
  PlanCleanup,
  ApplyPreflightStatus,
  ApplyPreflightCheck,
  ApplyPreflightResponse,
  TagOverrideRequest,
  TagStreamDecisionRequest,
  TagStreamLabelRewriteApprovalRequest,
  PlanSelectionRequest,
  PlanMutationOptions,
  PlanResponse,
  // Jobs
  ApplyJobStatus,
  ApplyJobProgressStatus,
  ApplyJobProgressEvent,
  ApplyJobResponse,
  ApplyJobLogResponse,
  // Status and settings
  StatusResponse,
  SettingsEntrySource,
  SettingsEntry,
  SecretSettingStatus,
  ManagedSettingSource,
  ManagedSettingEntry,
  SettingsResponse,
  ManagedSettingsUpdateResponse,
  // Doctor and onboarding
  DoctorCheckStatus,
  DoctorSuggestion,
  DoctorCheck,
  DoctorResponse,
  OnboardingDocLink,
  OnboardingChecklistItem,
  OnboardingChecklistResponse,
  OnboardingDismissResponse,
  CoreUpdateTourStatus,
  CoreUpdateTourStep,
  CoreUpdateTourResponse,
  // Auth and setup
  AuthSessionResponse,
  CsrfResponse,
  SetupStatusResponse,
  // Run history
  RunEventRecord,
  RunSummary,
  PendingUpdateRecord,
  RunDetail,
  RunVerificationContainerStatus,
  RunVerificationHealthStatus,
  RunVerificationImageStatus,
  RunVerificationItem,
  RunVerificationStatus,
  RunVerificationSummary,
  RunVerificationWudStatus,
  RollbackPlanItem,
  RollbackPlanItemStatus,
  RollbackPlanResponse,
  RollbackPlanStatus,
  RunLogResponse,
  LogTail,
  // Diagnostics
  WudApiObservationOutcome,
  WudApiObservationReasonCode,
  WudApiObservationDiagnostic,
  WudApiObservationDiagnostics,
  DiagnosticsSupportBundleResponse,
  // Service policies, snoozes, and tag exclusions
  ServicePolicyUpdateMode,
  AutoUpdateDay,
  SnoozeKind,
  SnoozeState,
  TagExclusionScope,
  TagExclusionMatchType,
  TagExclusionStatus,
  TagExclusionStatusFilter,
  ServicePolicyRecord,
  SnoozeRecord,
  TagExclusionRuleRecord,
  StateOperation,
  StateOperationResponse,
  // Container restart
  ContainerRestartResponse,
  // Self-update
  SelfUpdateStatus,
  SelfUpdateStrategy,
  SelfUpdateReleaseNote,
  SelfUpdateResponse,
  SelfUpdateApplyResponse,
  SelfUpdatePlanResponse,
  SelfUpdatePrepareResponse,
  SelfUpdateRequest,
  SelfUpdatePrepareRequest,
} from "./types";

import type {
  AuthSessionResponse,
  CsrfResponse,
  SetupStatusResponse,
  StatusResponse,
  SettingsResponse,
  ManagedSettingsUpdateResponse,
  DoctorResponse,
  OnboardingChecklistResponse,
  OnboardingDismissResponse,
  CoreUpdateTourStatus,
  CoreUpdateTourStep,
  CoreUpdateTourResponse,
  PendingResponse,
  PendingMetadataRefreshRequest,
  PendingMetadataRefreshResponse,
  UpdateTargetsResponse,
  RetagTargetsResponse,
  TrackedContainersResponse,
  TrackingRepairPlan,
  RetagChoiceRequest,
  RetagPlanOptions,
  RetagPlanResponse,
  RetagPreviewJobResponse,
  DiagnosticsSupportBundleResponse,
  PendingCleanupLine,
  PendingCleanupResponse,
  PendingRescanLine,
  PendingRescanScope,
  PendingRescanResponse,
  SecurityScansResponse,
  SecurityScanJobResponse,
  PendingRemovalPlanResponse,
  ReleaseNotesResponse,
  ReleaseNotificationSource,
  ReleaseNotificationResponse,
  ReleaseNotificationTestResponse,
  ServicePolicyRecord,
  SnoozeState,
  SnoozeRecord,
  TagExclusionStatusFilter,
  TagExclusionRuleRecord,
  StateOperation,
  StateOperationResponse,
  SelfUpdateResponse,
  SelfUpdatePlanResponse,
  SelfUpdateApplyResponse,
  SelfUpdatePrepareResponse,
  SelfUpdateRequest,
  SelfUpdatePrepareRequest,
  ContainerRestartResponse,
  TagOverrideRequest,
  PlanMutationOptions,
  DigestPinLabelRewriteApprovalRequest,
  PlanResponse,
  ApplyJobResponse,
  RunSummary,
  RunDetail,
  RollbackPlanResponse,
  RunLogResponse,
} from "./types";

// ---------------------------------------------------------------------------
// Error class
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ---------------------------------------------------------------------------
// API prefix helpers
// ---------------------------------------------------------------------------

type WudApiGlobal = typeof globalThis & {
  WUD_API_PREFIX?: string;
};

const API_VERSION_PATH = "api/v1";

function trimTrailingSlashes(value: string): string {
  let end = value.length;
  while (end > 0 && value.charAt(end - 1) === "/") {
    end -= 1;
  }
  return value.slice(0, end);
}

function startsWithHttpUrl(value: string): boolean {
  const normalized = value.toLowerCase();
  return normalized.startsWith("http://") || normalized.startsWith("https://");
}

export function normalizeApiPrefix(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed) {
    return "/api/v1";
  }
  const withoutTrailingSlash = trimTrailingSlashes(trimmed);
  if (
    withoutTrailingSlash.startsWith("/") ||
    startsWithHttpUrl(withoutTrailingSlash)
  ) {
    return withoutTrailingSlash;
  }
  return `/${withoutTrailingSlash}`;
}

export function apiPrefixFromBasePath(basePath: string): string {
  const normalizedBasePath = trimTrailingSlashes(basePath.trim());
  if (!normalizedBasePath || normalizedBasePath === "/") {
    return "/api/v1";
  }
  const rootedBasePath = normalizedBasePath.startsWith("/")
    ? normalizedBasePath
    : `/${normalizedBasePath}`;
  return `${rootedBasePath}/${API_VERSION_PATH}`;
}

function currentDocumentBasePath(): string {
  if (typeof document === "undefined") {
    return "/";
  }
  const basePath = new URL(document.baseURI).pathname;
  if (basePath.endsWith("/")) {
    return basePath;
  }
  return basePath.slice(0, basePath.lastIndexOf("/") + 1) || "/";
}

function defaultApiPrefix(): string {
  return apiPrefixFromBasePath(currentDocumentBasePath());
}

const API_PREFIX = normalizeApiPrefix(
  (globalThis as WudApiGlobal).WUD_API_PREFIX ??
    import.meta.env.VITE_WUD_API_PREFIX ??
    defaultApiPrefix(),
);
export const LIVE_JOB_LOG_TAIL_BYTES = 65_536;

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_PREFIX}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : `Request failed with status ${response.status}`;
    throw new ApiError(response.status, detail);
  }

  return body as T;
}

// ---------------------------------------------------------------------------
// Auth and setup
// ---------------------------------------------------------------------------

const authApi = {
  csrf: () => apiRequest<CsrfResponse>("/auth/csrf"),
  setupStatus: () => apiRequest<SetupStatusResponse>("/setup/status"),
  setupClaim: (
    claim: string,
    username: string,
    password: string,
    csrfToken: string,
  ) =>
    apiRequest<AuthSessionResponse>("/setup/claim", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ claim, username, password }),
    }),
  resetAdminClaim: (
    claim: string,
    username: string,
    password: string,
    csrfToken: string,
  ) =>
    apiRequest<AuthSessionResponse>("/auth/reset-admin/claim", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ claim, username, password }),
    }),
  session: () => apiRequest<AuthSessionResponse>("/auth/session"),
  login: (username: string, password: string, csrfToken: string) =>
    apiRequest<AuthSessionResponse>("/auth/login", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ username, password }),
    }),
  logout: (csrfToken: string) =>
    apiRequest<AuthSessionResponse>("/auth/logout", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
};

// ---------------------------------------------------------------------------
// Status, settings, doctor, and onboarding
// ---------------------------------------------------------------------------

const systemApi = {
  status: () => apiRequest<StatusResponse>("/status"),
  settings: () => apiRequest<SettingsResponse>("/settings"),
  updateManagedSettings: (values: Record<string, string>, csrfToken: string) =>
    apiRequest<ManagedSettingsUpdateResponse>("/settings/managed", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ values }),
    }),
  doctor: (csrfToken: string) =>
    apiRequest<DoctorResponse>("/doctor", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  onboardingChecklist: (csrfToken: string) =>
    apiRequest<OnboardingChecklistResponse>("/onboarding/checklist", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  dismissOnboarding: (csrfToken: string) =>
    apiRequest<OnboardingDismissResponse>("/onboarding/dismiss", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  coreUpdateTour: () =>
    apiRequest<CoreUpdateTourResponse>("/onboarding/core-update-tour"),
  updateCoreUpdateTour: (
    status: CoreUpdateTourStatus,
    step: CoreUpdateTourStep,
    csrfToken: string,
  ) =>
    apiRequest<CoreUpdateTourResponse>("/onboarding/core-update-tour", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ status, step }),
    }),
  diagnosticsSupportBundle: () =>
    apiRequest<DiagnosticsSupportBundleResponse>("/diagnostics/support-bundle"),
};

// ---------------------------------------------------------------------------
// Pending updates
// ---------------------------------------------------------------------------

const pendingApi = {
  pending: () => apiRequest<PendingResponse>("/pending"),
  pendingMetadata: (request: PendingMetadataRefreshRequest, csrfToken: string) =>
    apiRequest<PendingMetadataRefreshResponse>("/pending/metadata", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify(request),
    }),
  cleanupPending: (
    cleanupId: string,
    lines: PendingCleanupLine[],
    csrfToken: string,
  ) =>
    apiRequest<PendingCleanupResponse>("/pending/cleanup", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        cleanup_id: cleanupId,
        lines,
        confirmation: "remove_unmatched",
      }),
    }),
  createRemovalPlan: (lineNumbers: number[], csrfToken: string) =>
    apiRequest<PendingRemovalPlanResponse>("/pending/removal-plan", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ line_numbers: lineNumbers }),
    }),
  removeSelectedPending: (
    removalId: string,
    lines: PendingCleanupLine[],
    csrfToken: string,
  ) =>
    apiRequest<PendingCleanupResponse>("/pending/removal", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        removal_id: removalId,
        lines,
        confirmation: "remove_selected",
      }),
    }),
  rescanPending: (
    scope: PendingRescanScope,
    lines: PendingRescanLine[],
    csrfToken: string,
  ) =>
    apiRequest<PendingRescanResponse>("/pending/rescan", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        confirmation: "rescan_wud",
        scope,
        line_numbers: lines.map((line) => line.line_no),
        lines,
      }),
    }),
};

// ---------------------------------------------------------------------------
// Update targets and release notes
// ---------------------------------------------------------------------------

const updatesApi = {
  updateTargets: () => apiRequest<UpdateTargetsResponse>("/update-targets"),
  trackedContainers: () => apiRequest<TrackedContainersResponse>("/tracked-containers"),
  createTrackingRepairPlan: (targetId: string, regex: string, csrfToken: string) =>
    apiRequest<TrackingRepairPlan>("/tracking-repairs", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ target_id: targetId, regex }),
    }),
  applyTrackingRepair: (plan: TrackingRepairPlan, csrfToken: string) =>
    apiRequest<ApplyJobResponse>("/tracking-repairs/apply", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        target_id: plan.target_id,
        regex: plan.proposed_regex,
        plan_id: plan.plan_id,
        confirmation: "apply-tracking-repair",
      }),
    }),
  retagTargets: (options: RetagPlanOptions = {}) =>
    apiRequest<RetagTargetsResponse>(
      options.github_latest_fallback
        ? "/retag-targets?github_latest_fallback=true"
        : "/retag-targets",
    ),
  refreshRetagGithubLatest: (csrfToken: string) =>
    apiRequest<RetagTargetsResponse>("/retag-targets/github-latest/refresh", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  createRetagPlan: (
    choices: RetagChoiceRequest[],
    csrfToken: string,
    options: RetagPlanOptions = {},
  ) =>
    apiRequest<RetagPlanResponse>("/retag-plans", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        choices,
        github_latest_fallback: options.github_latest_fallback ?? false,
      }),
    }),
  startRetagPreview: (
    choices: RetagChoiceRequest[],
    csrfToken: string,
    options: RetagPlanOptions = {},
  ) =>
    apiRequest<RetagPreviewJobResponse>("/retag-plans/preview", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        choices,
        github_latest_fallback: options.github_latest_fallback ?? false,
      }),
    }),
  retagPreviewJob: (previewJobId: string) =>
    apiRequest<RetagPreviewJobResponse>(
      `/retag-plans/preview/${encodeURIComponent(previewJobId)}`,
    ),
  applyRetagPlan: (
    planId: string,
    choices: RetagChoiceRequest[],
    csrfToken: string,
    options: RetagPlanOptions = {},
  ) =>
    apiRequest<ApplyJobResponse>("/retag-plans/apply", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        plan_id: planId,
        choices,
        github_latest_fallback: options.github_latest_fallback ?? false,
        confirmation: "apply-retags",
      }),
    }),
  releaseNotes: () => apiRequest<ReleaseNotesResponse>("/release-notes"),
  refreshReleaseNotes: (csrfToken: string) =>
    apiRequest<ReleaseNotesResponse>("/release-notes/refresh", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  previewReleaseNotifications: (
    source: ReleaseNotificationSource,
    csrfToken: string,
  ) =>
    apiRequest<ReleaseNotificationResponse>("/release-notifications/preview", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify(source),
    }),
  sendReleaseNotifications: (
    source: ReleaseNotificationSource,
    csrfToken: string,
  ) =>
    apiRequest<ReleaseNotificationResponse>("/release-notifications/send", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        ...source,
        confirmation: "send-release-notes",
      }),
    }),
  testReleaseNotificationWebhook: (csrfToken: string) =>
    apiRequest<ReleaseNotificationTestResponse>("/release-notifications/test", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ confirmation: "send-test-webhook" }),
    }),
  securityScans: () => apiRequest<SecurityScansResponse>("/security-scans"),
  refreshSecurityScans: (csrfToken: string) =>
    apiRequest<SecurityScanJobResponse>("/security-scans/refresh", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  securityScanJob: (jobId: string) =>
    apiRequest<SecurityScanJobResponse>(
      `/security-scans/jobs/${encodeURIComponent(jobId)}`,
    ),
};

// ---------------------------------------------------------------------------
// Service policies, snoozes, and tag exclusions
// ---------------------------------------------------------------------------

const policiesApi = {
  servicePolicies: () => apiRequest<ServicePolicyRecord[]>("/service-policies"),
  snoozes: (state: SnoozeState = "active") =>
    apiRequest<SnoozeRecord[]>(`/snoozes?state=${encodeURIComponent(state)}`),
  tagExclusions: (status: TagExclusionStatusFilter = "active") =>
    apiRequest<TagExclusionRuleRecord[]>(
      `/tag-exclusions?status=${encodeURIComponent(status)}`,
    ),
  stateOperation: (operation: StateOperation, csrfToken: string) =>
    apiRequest<StateOperationResponse>("/state/operations", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify(operation),
    }),
};

// ---------------------------------------------------------------------------
// Self-update and container restart
// ---------------------------------------------------------------------------

const selfUpdateApi = {
  selfUpdate: () => apiRequest<SelfUpdateResponse>("/self-update"),
  planSelfUpdate: (csrfToken: string) =>
    apiRequest<SelfUpdatePlanResponse>("/self-update/plan", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
    }),
  applySelfUpdate: (csrfToken: string, update: SelfUpdateResponse) =>
    apiRequest<SelfUpdateApplyResponse>("/self-update", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        confirmation: "pull_image",
        current_tag: update.current_tag,
        latest_tag: update.latest_tag,
        target_image: update.target_image,
        restart_container: update.restart_container,
      } satisfies SelfUpdateRequest),
    }),
  prepareSelfUpdate: (
    csrfToken: string,
    update: SelfUpdateResponse,
    plan: SelfUpdatePlanResponse,
  ) =>
    apiRequest<SelfUpdatePrepareResponse>("/self-update/prepare", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        confirmation: "prepare_tag_update",
        plan_id: plan.plan.plan_id,
        current_tag: update.current_tag,
        latest_tag: update.latest_tag,
        target_image: update.target_image,
        restart_container: update.restart_container,
      } satisfies SelfUpdatePrepareRequest),
    }),
  restartContainer: (csrfToken: string) =>
    apiRequest<ContainerRestartResponse>("/container/restart", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({ confirmation: "restart_container" }),
    }),
};

// ---------------------------------------------------------------------------
// Plans and jobs
// ---------------------------------------------------------------------------

const planMutationBody = (
  lineNumbers: number[],
  allowTagUpdates: boolean,
  tagOverrides: TagOverrideRequest[],
  digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
  options: PlanMutationOptions,
) => {
  const selections = options.selections ?? [];
  return {
    ...(selections.length ? { selections } : { line_numbers: lineNumbers }),
    allow_tag_updates: allowTagUpdates,
    tag_overrides: tagOverrides,
    tag_stream_decisions: options.tagStreamDecisions ?? [],
    tag_stream_label_rewrite_approvals:
      options.tagStreamLabelRewriteApprovals ?? [],
    digest_pin_label_rewrite_approvals: digestPinLabelRewriteApprovals,
  };
};

const plansApi = {
  createPlan: (
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
    csrfToken: string,
    options: PlanMutationOptions = {},
  ) =>
    apiRequest<PlanResponse>("/plans", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify(
        planMutationBody(
          lineNumbers,
          allowTagUpdates,
          tagOverrides,
          digestPinLabelRewriteApprovals,
          options,
        ),
      ),
    }),
  createJob: (
    planId: string,
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
    csrfToken: string,
    options: PlanMutationOptions = {},
  ) =>
    apiRequest<ApplyJobResponse>("/jobs", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        plan_id: planId,
        ...planMutationBody(
          lineNumbers,
          allowTagUpdates,
          tagOverrides,
          digestPinLabelRewriteApprovals,
          options,
        ),
        confirmation: "apply",
      }),
    }),
  applyPlan: (
    planId: string,
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
    csrfToken: string,
    options: PlanMutationOptions = {},
  ) =>
    apiRequest<ApplyJobResponse>("/plans/apply", {
      method: "POST",
      headers: { "x-wud-csrf-token": csrfToken },
      body: JSON.stringify({
        plan_id: planId,
        ...planMutationBody(
          lineNumbers,
          allowTagUpdates,
          tagOverrides,
          digestPinLabelRewriteApprovals,
          options,
        ),
        confirmation: "apply",
      }),
    }),
  job: (jobId: string) =>
    apiRequest<ApplyJobResponse>(`/jobs/${encodeURIComponent(jobId)}`),
  applyJob: (jobId: string) => apiRequest<ApplyJobResponse>(`/apply-jobs/${encodeURIComponent(jobId)}`),
  openJobStream: (jobId: string) =>
    new EventSource(
      `${API_PREFIX}/jobs/${encodeURIComponent(jobId)}/stream?log_tail_bytes=${LIVE_JOB_LOG_TAIL_BYTES}`,
      { withCredentials: true },
    ),
};

// ---------------------------------------------------------------------------
// Run history
// ---------------------------------------------------------------------------

const runsApi = {
  runs: () => apiRequest<RunSummary[]>("/runs"),
  runDetail: (runId: number) => apiRequest<RunDetail>(`/runs/${runId}`),
  rollbackPlan: (runId: number) =>
    apiRequest<RollbackPlanResponse>(`/runs/${runId}/rollback-plan`),
  runLog: (runId: number, tailBytes = 262_144) =>
    apiRequest<RunLogResponse>(`/runs/${runId}/log?tail_bytes=${tailBytes}`),
};

// ---------------------------------------------------------------------------
// Composed live API object
// ---------------------------------------------------------------------------

const liveWebApi = {
  ...authApi,
  ...systemApi,
  ...pendingApi,
  ...updatesApi,
  ...policiesApi,
  ...selfUpdateApi,
  ...plansApi,
  ...runsApi,
};

export type WebApi = typeof liveWebApi;

export const webApi: WebApi =
  import.meta.env.MODE === "demo" || import.meta.env.VITE_WUD_DEMO_MODE === "true"
    ? createDemoWebApi()
    : liveWebApi;
