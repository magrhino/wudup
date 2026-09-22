// ---------------------------------------------------------------------------
// Pending updates
// ---------------------------------------------------------------------------

export interface DigestTagProvenance {
  source_image: string;
  resolved_tag: string;
  watch_tag: string;
  target_digest: string;
  final_image: string;
  provenance_source: string;
  provenance_confidence: string;
}

export type WudApiState = "ready" | "unavailable" | "auth_required" | "error";

export interface WudApiStatus {
  state: WudApiState;
  available: boolean;
  metadata_available: boolean;
  last_checked_at: string;
  detail: string;
}

export interface WudContainerMetadata {
  id: string;
  name: string;
  display_name: string;
  status: string;
  watcher: string;
  local_tag: string;
  local_digest: string;
  remote_tag: string;
  remote_digest: string;
  update_kind: string;
  semver_diff: string;
  link: string;
  error: string;
  platform: string;
  platform_os: string;
  platform_architecture: string;
  platform_variant: string;
}

export interface TrackedContainerItem {
  target_id: string;
  service_key: string;
  stack: string;
  service: string;
  image: string;
  current_tag: string;
  runtime_state: "running" | "not-running" | "unknown";
  tracking_regex: string;
  tracking_health: string;
  tracking_detail: string;
  suggested_regex: string;
  wud: WudContainerMetadata | null;
  wud_match_state: "watching" | "untracked" | "unknown" | "ambiguous";
  wud_update_available: boolean | null;
  last_image_recorded_at: string;
  last_action_at: string;
  last_action_status: string;
  last_action_run_id: number | null;
  retag_available: boolean;
}

export interface TrackedContainersResponse {
  status: "ready" | "unavailable";
  count: number;
  items: TrackedContainerItem[];
  wud_status: WudApiStatus | null;
  warnings: string[];
}

export interface TrackingRepairPlan {
  plan_id: string;
  source_hash: string;
  rendered_hash: string;
  target_id: string;
  service_key: string;
  stack: string;
  service: string;
  image: string;
  current_regex: string;
  proposed_regex: string;
  compose_diff: string;
  will_recreate: boolean;
  can_apply: boolean;
  issues: string[];
}

export interface WudApiDiagnosticEndpointStatus {
  state: WudApiState;
  available: boolean;
  last_checked_at: string;
  detail: string;
}

export interface WudApiAppDiagnostics {
  status: WudApiDiagnosticEndpointStatus;
  name: string;
  version: string;
}

export interface WudApiLogDiagnostics {
  status: WudApiDiagnosticEndpointStatus;
  level: string;
}

export interface WudApiStoreDiagnostics {
  status: WudApiDiagnosticEndpointStatus;
  path: string;
  file: string;
  configuration: Record<string, unknown>;
}

export interface WudApiWatcherDiagnostics {
  id: string;
  type: string;
  name: string;
  cron: string;
  watch_by_default: boolean | null;
  configuration: Record<string, unknown>;
}

export interface WudApiRegistryDiagnostics {
  id: string;
  type: string;
  name: string;
  configuration: Record<string, unknown>;
}

export interface WudApiConfigurationDiagnostics {
  health: WudApiDiagnosticEndpointStatus;
  app: WudApiAppDiagnostics;
  log: WudApiLogDiagnostics;
  store: WudApiStoreDiagnostics;
  watchers_status: WudApiDiagnosticEndpointStatus;
  watchers: WudApiWatcherDiagnostics[];
  registries_status: WudApiDiagnosticEndpointStatus;
  registries: WudApiRegistryDiagnostics[];
}

export type PendingSourceMode = "file" | "api" | "auto";
export type PendingSourceActive = "file" | "api";
export type PendingMetadataStatus = "fresh" | "retained" | "recovered";

export interface PendingSourceInfo {
  configured: PendingSourceMode;
  active: PendingSourceActive;
  label: string;
  fresh: boolean;
  degraded: boolean;
  fallback_reason: string;
  detail: string;
}

export interface PendingTagStream {
  current_stream: string;
  reported_stream: string;
}

export interface PendingItem {
  line_no: number;
  raw: string;
  image: string;
  key: string;
  repo: string;
  current_tag: string;
  has_tag: boolean;
  allow_repo: boolean;
  digest: string;
  desired_tag: string;
  platform: string;
  platform_os: string;
  platform_architecture: string;
  platform_variant: string;
  digest_provenance?: DigestTagProvenance | null;
  wud_metadata?: WudContainerMetadata | null;
  source: PendingSourceActive;
  source_id: string;
  tag_stream?: PendingTagStream | null;
  metadata_status?: PendingMetadataStatus;
}

export interface PendingDiagnostic {
  code: string;
  message: string;
  hint: string;
  stack: string;
  service: string;
  compose_file: string;
  found_files: string[];
  details: Record<string, unknown>;
}

export type PendingGroupingStatus = "ready" | "unavailable";
export type PendingRuntimeState =
  | "running"
  | "not-running"
  | "mixed"
  | "unknown";

export interface PendingGroupedItem extends PendingItem {
  selection_id?: string;
  resolved_image: string;
  target_image: string;
  compose_images: string[];
  services: string[];
  action: string;
  diagnostic: PendingDiagnostic | null;
  runtime_state: PendingRuntimeState;
  running_services: string[];
  stopped_services: string[];
}

export interface PendingStackGroup {
  name: string;
  directory: string;
  compose_file: string;
  project_directory: string;
  project_name: string;
  services_label: string;
  services: string[];
  line_numbers: number[];
  items: PendingGroupedItem[];
}

export interface PendingGrouping {
  status: PendingGroupingStatus;
  groups: PendingStackGroup[];
  unmatched: PendingGroupedItem[];
  warnings: string[];
}

export interface PendingSnoozedCandidate {
  key: string;
  service_key: string;
  stack: string;
  service: string;
  image: string;
  target_image: string;
  current_tag: string;
  desired_tag: string;
  digest: string;
  source_id: string;
  wud_metadata: WudContainerMetadata;
  snooze_kind: SnoozeKind;
  reason: string;
  snoozed_until: string | null;
  wait_for_service_key: string;
}

export interface PendingResponse {
  source_file: string;
  source: PendingSourceInfo;
  source_hash?: string;
  exists: boolean;
  count: number;
  items: PendingItem[];
  grouping: PendingGrouping;
  snoozed_candidates: PendingSnoozedCandidate[];
  wud_api: WudApiStatus;
  warnings: string[];
}

export interface PendingCleanupLine {
  line_no: number;
  raw: string;
}

export interface PendingCleanupRemovedLine extends PendingCleanupLine {
  image: string;
  reason: string;
}

export interface PendingCleanupResponse {
  status: "success";
  audit_run_id: number;
  removed_count: number;
  removed: PendingCleanupRemovedLine[];
}

export type PendingMetadataRefreshStatus = "ready" | "stale";

export interface PendingMetadataRefreshLine {
  line_no: number;
  raw: string;
  source_id: string;
}

export interface PendingMetadataRefreshRequest {
  source_hash: string;
  lines: PendingMetadataRefreshLine[];
}

export interface PendingMetadataRefreshItem {
  line_no: number;
  raw: string;
  source_id: string;
  wud_metadata: WudContainerMetadata | null;
  metadata_status?: PendingMetadataStatus;
}

export interface PendingMetadataRefreshResponse {
  status: PendingMetadataRefreshStatus;
  requires_pending_reload: boolean;
  source_hash: string;
  source: PendingSourceInfo;
  wud_api: WudApiStatus;
  items: PendingMetadataRefreshItem[];
}

export type PendingRescanScope = "all" | "selected";
export type PendingRescanStatus = "success" | "partial" | "blocked";

export interface PendingRescanSkippedLine {
  line_no: number;
  raw: string;
  reason: string;
}

export interface PendingRescanLine {
  line_no: number;
  raw: string;
  source_id: string;
  source_hash: string;
  container_id: string;
}

export interface PendingRescanResponse {
  status: PendingRescanStatus;
  audit_run_id: number;
  scope: PendingRescanScope;
  requested_count: number;
  watched_count: number;
  skipped: PendingRescanSkippedLine[];
  wud_api: WudApiStatus;
}

export type SecurityScanState =
  | "disabled"
  | "not_scanned"
  | "queued"
  | "running"
  | "complete"
  | "stale"
  | "partial"
  | "unsupported"
  | "unavailable_offline"
  | "auth_required"
  | "error";

export type SecurityScanVerdict = "findings" | "none_reported" | "unknown";

export interface SecurityScanSeverityCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
  unknown: number;
}

export interface SecurityScanFinding {
  target: string;
  target_class: string;
  target_type: string;
  vulnerability_id: string;
  package_name: string;
  installed_version: string;
  fixed_version: string;
  severity: "critical" | "high" | "medium" | "low" | "unknown";
  title: string;
  primary_url: string;
}

export interface SecurityScanSubject {
  requested_ref: string;
  reported_digest: string;
  index_digest: string;
  manifest_digest: string;
  immutable_ref: string;
  platform: string;
}

export type SecurityScanComparisonStatus =
  | "unknown"
  | "improved"
  | "unchanged"
  | "mixed"
  | "worse";

export interface SecurityScanComparison {
  status: SecurityScanComparisonStatus;
  current_subject: SecurityScanSubject;
  fixed_findings: SecurityScanFinding[];
  remaining_findings: SecurityScanFinding[];
  introduced_findings: SecurityScanFinding[];
  message: string;
}

export interface SecurityScanInfo {
  line_no: number;
  state: SecurityScanState;
  verdict: SecurityScanVerdict;
  scanner: string;
  scanner_version: string;
  scanner_schema: string;
  scanned_at: string;
  db_revision: string;
  db_updated_at: string;
  severity_counts: SecurityScanSeverityCounts;
  advisory_counts: SecurityScanSeverityCounts;
  advisory_counts_known: boolean;
  fixable_counts: SecurityScanSeverityCounts;
  unfixed_count: number;
  findings: SecurityScanFinding[];
  subject: SecurityScanSubject;
  comparison: SecurityScanComparison;
  warnings: string[];
  error_code: string;
  error_message: string;
}

export interface SecurityScansResponse {
  source_file: string;
  source: PendingSourceInfo;
  source_hash: string;
  scanning_enabled: boolean;
  scanner: string;
  scan_mode: string;
  count: number;
  items: SecurityScanInfo[];
  warnings: string[];
}

export interface SecurityScanJobResponse {
  job_id: string;
  status: "queued" | "running" | "success" | "failure";
  total_count: number;
  completed_count: number;
  result: SecurityScansResponse | null;
  error: string;
}

export interface PendingRemovalPlanLine {
  line_no: number;
  raw: string;
  image: string;
  desired_tag: string;
  digest: string;
}

export interface PendingRemovalPlanResponse {
  removal_id: string;
  source_file: string;
  can_remove: boolean;
  selected_line_numbers: number[];
  lines: PendingRemovalPlanLine[];
}

// ---------------------------------------------------------------------------
// Update targets
// ---------------------------------------------------------------------------

export type UpdateTargetsStatus = "ready" | "unavailable";

export interface UpdateTargetItem {
  service_key: string;
  stack: string;
  service: string;
  image: string;
  image_repo: string;
  current_tag: string;
  directory: string;
  compose_file: string;
  project_directory: string;
}

export interface UpdateTargetsResponse {
  status: UpdateTargetsStatus;
  count: number;
  items: UpdateTargetItem[];
  warnings: string[];
}

// ---------------------------------------------------------------------------
// Retag targets
// ---------------------------------------------------------------------------

export type RetagTargetsStatus = "ready" | "unavailable";
export type RetagTargetChoice = "keep-current" | "switch-to-concrete";
export type RetagRuntimeState = "running" | "not-running" | "unknown";

export interface RetagTargetItem {
  target_id?: string;
  service_key: string;
  stack: string;
  service: string;
  image: string;
  image_repo: string;
  current_tag: string;
  tracking_tag: string;
  tracking_tag_source: string;
  proposed_tag: string;
  final_image: string;
  candidate_source: string;
  candidate_warning: string;
  candidate_link_label: string;
  candidate_link_url: string;
  runtime_state: RetagRuntimeState;
  retag_available: boolean;
  retag_reason: string;
  choices: RetagTargetChoice[];
  label_key: string;
  label_value: string;
  directory: string;
  compose_file: string;
  project_directory: string;
  digest_provenance?: DigestTagProvenance | null;
}

export interface RetagTargetsResponse {
  status: RetagTargetsStatus;
  count: number;
  items: RetagTargetItem[];
  warnings: string[];
}

export type RetagPlanStatus = "ready" | "empty" | "blocked" | "unavailable";

export interface RetagChoiceRequest {
  service_key: string;
  target_id?: string;
  choice: RetagTargetChoice;
  target_tag?: string;
  allow_start?: boolean;
}

export interface RetagPlanOptions {
  github_latest_fallback?: boolean;
}

export interface RetagPlanIssue {
  severity: string;
  code: string;
  message: string;
  service_key: string;
  stack: string;
  service: string;
  hint: string;
  details: Record<string, unknown>;
}

export interface RetagPlanLabelRewrite {
  service: string;
  label_key: string;
  current_label_value: string;
  planned_tag: string;
  proposed_label_value: string;
  proposed_label_regex: string;
  approved: boolean;
  reason: string;
}

export interface RetagPlanDigestPinUpdate {
  target_id: string;
  service_key: string;
  stack: string;
  service: string;
  source_image: string;
  resolved_tag: string;
  planned_digest: string;
  final_image: string;
  watch_tag: string;
  marker: string;
  label_key: string;
  label_value: string;
  label_rewrites: RetagPlanLabelRewrite[];
  digest_provenance?: DigestTagProvenance | null;
}

export interface RetagPlanTagUpdate {
  target_id: string;
  service_key: string;
  stack: string;
  service: string;
  source_image: string;
  target_tag: string;
  final_image: string;
  label_key: string;
  label_value: string;
  label_rewrites: RetagPlanLabelRewrite[];
}

export interface RetagPlanStack {
  stack: string;
  directory: string;
  compose_file: string;
  project_directory: string;
  services: string[];
  tag_updates: RetagPlanTagUpdate[];
  digest_pin_updates: RetagPlanDigestPinUpdate[];
}

export interface RetagPlanResponse {
  plan_id: string;
  status: RetagPlanStatus;
  can_apply: boolean;
  external_recreate_required: boolean;
  selected_count: number;
  keep_current_count: number;
  stacks: RetagPlanStack[];
  issues: RetagPlanIssue[];
  warnings: string[];
}

export interface RetagPreviewJobResponse {
  preview_job_id: string;
  status: ApplyJobStatus;
  plan: RetagPlanResponse | null;
  warnings: string[];
  error: string;
  progress: ApplyJobProgressEvent[];
}

// ---------------------------------------------------------------------------
// Release notes
// ---------------------------------------------------------------------------

export interface ReleaseNoteLink {
  label: string;
  url: string;
  kind: string;
}

export type ReleaseNoteChangeType = "upstream_update" | "image_rebuild" | "unknown";

export interface ReleaseNoteClassificationTag {
  raw: string;
  kind: string;
  arch: string;
  branch: string;
  upstream_version: string;
  build_suffix: string;
}

export interface ReleaseNoteClassification {
  change_type: ReleaseNoteChangeType;
  reason: string;
  current: ReleaseNoteClassificationTag;
  target: ReleaseNoteClassificationTag;
}

export type ReleaseSecurityOutcome =
  | "verified_critical_high"
  | "needs_review"
  | "ordinary";

export type ReleaseSecuritySeverity =
  | "critical"
  | "high"
  | "moderate"
  | "low"
  | "unknown"
  | "none";

export interface ReleaseSecurityAssessment {
  outcome: ReleaseSecurityOutcome;
  severity: ReleaseSecuritySeverity;
  reason_code: string;
  reason: string;
  advisory_ids: string[];
  lookup_truncated: boolean;
}

export interface ReleaseNoteInfo {
  line_no: number;
  status: string;
  provider: string;
  image_repo: string;
  upstream_repo: string;
  release_tag: string;
  title: string;
  published_at: string;
  breaking: boolean;
  breaking_reasons: string[];
  links: ReleaseNoteLink[];
  refreshed_at: string;
  error: string;
  body?: string;
  classification?: ReleaseNoteClassification;
  security: ReleaseSecurityAssessment;
  notification_key: string;
  notification_status: string;
  notification_last_sent_at: string;
  notification_send_count: number;
  notification_skipped_reason: string;
}

export interface ReleaseNotesResponse {
  source_file: string;
  source: PendingSourceInfo;
  count: number;
  items: ReleaseNoteInfo[];
  enabled: boolean;
  disabled_reason: string;
  notifications_enabled: boolean;
  notifications_disabled_reason: string;
  wud_api: WudApiStatus;
  warnings: string[];
}

export interface ReleaseNotificationDestination {
  type: "discord";
  configured: boolean;
  source: string;
}

export interface ReleaseNotificationTrigger {
  id: string;
  type: string;
  name: string;
}

export interface ReleaseNotificationItem {
  line_no: number;
  image: string;
  service_key: string;
  title: string;
  description: string;
  status: string;
  release_tag: string;
  image_repo: string;
  upstream_repo: string;
  current_version: string;
  target_version: string;
  category: "security_urgent" | "needs_review" | "worth_noting" | "routine";
  reason_code: string;
  reason_label: string;
  security: ReleaseSecurityAssessment;
  links: ReleaseNoteLink[];
  triggers: ReleaseNotificationTrigger[];
  notification_key: string;
  notification_status: string;
  notification_last_sent_at: string;
  notification_send_count: number;
  skipped_reason: string;
}

export type ReleaseNotificationSource =
  | { line_numbers: number[]; resend?: boolean }
  | { run_id: number; resend?: boolean };

export interface ReleaseNotificationResponse {
  enabled: boolean;
  mode: "digest" | "per_container";
  resend_policy: "remote_change" | "cooldown";
  destination: ReleaseNotificationDestination;
  source: PendingSourceInfo;
  source_file: string;
  count: number;
  sendable_count: number;
  skipped_count: number;
  batch_count: number;
  messages: string[];
  items: ReleaseNotificationItem[];
  wud_api: WudApiStatus;
  warnings: string[];
  sent: boolean;
  audit_run_id: number;
  error: string;
}

export interface ReleaseNotificationTestResponse {
  sent: boolean;
  destination: ReleaseNotificationDestination;
  audit_run_id: number;
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export type PlanStatus = "ready" | "empty" | "blocked";

export interface PlanSummary {
  target_count: number;
  matched_target_count: number;
  stack_count: number;
  service_count: number;
  skipped_count: number;
  issue_count: number;
}

export interface PlanIssue {
  severity: string;
  code: string;
  message: string;
  line_no: number | null;
  stack: string;
  service: string;
  hint: string;
  details: Record<string, unknown>;
}

export interface PlanTarget {
  line_no: number;
  raw: string;
  image: string;
  resolved_image: string;
  digest: string;
  desired_tag: string;
  matched: boolean;
  action: string;
  metadata_status?: PendingMetadataStatus;
}

export interface PlanLine {
  line_no: number;
  raw: string;
  image: string;
  resolved_image: string;
  compose_image: string;
  target_image: string;
  service: string;
  digest: string;
  desired_tag: string;
  action: string;
  metadata_status?: PendingMetadataStatus;
  digest_provenance?: DigestTagProvenance | null;
}

export interface PlanTagUpdate {
  old_image: string;
  desired_tag: string;
  new_image: string;
  services: string[];
}

export type TagStreamDecision = "preserve" | "switch";

export interface TagStreamDecisionRequest {
  line_no: number;
  decision: TagStreamDecision;
}

export interface TagStreamLabelRewriteApprovalRequest {
  line_no: number;
  stack: string;
  stack_directory: string;
  compose_file: string;
  service: string;
  label_key: string;
  current_label_value: string;
  selected_tag: string;
  proposed_label_value: string;
}

export interface PlanTagStreamUpdate {
  line_no: number;
  service: string;
  current_tag: string;
  reported_tag: string;
  selected_tag: string;
  decision: TagStreamDecision;
  label_key: string;
  current_label_value: string;
  proposed_label_value: string;
  proposed_label_regex: string;
  approved: boolean;
  reason: string;
}

export interface DigestPinLabelRewriteApprovalRequest {
  stack: string;
  service: string;
  label_key: string;
  current_label_value: string;
  planned_tag: string;
  proposed_label_value: string;
}

export interface PlanDigestPinLabelRewrite {
  service: string;
  label_key: string;
  current_label_value: string;
  planned_tag: string;
  proposed_label_value: string;
  proposed_label_regex: string;
  approved: boolean;
  reason: string;
}

export interface PlanDigestPinUpdate {
  source_image: string;
  resolved_tag: string;
  planned_digest: string;
  final_image: string;
  watch_tag: string;
  marker: string;
  label_key: string;
  label_value: string;
  services: string[];
  label_rewrites: PlanDigestPinLabelRewrite[];
  digest_provenance?: DigestTagProvenance | null;
}

export interface PlanDigestUnpinUpdate {
  source_image: string;
  resolved_tag: string;
  tag_image: string;
  current_digest: string;
  target_digest: string;
  watch_tag: string;
  marker: string;
  label_key: string;
  label_value: string;
  services: string[];
  digest_provenance?: DigestTagProvenance | null;
}

export interface PlanAction {
  kind: string;
  description: string;
  cwd: string;
  args: string[];
}

export interface PlanStack {
  name: string;
  directory: string;
  compose_file: string;
  project_directory: string;
  services_label: string;
  services: string[];
  pull_services: string[];
  stop_services: string[];
  force_recreate: boolean;
  up_no_deps: boolean;
  tag_updates: PlanTagUpdate[];
  tag_stream_updates: PlanTagStreamUpdate[];
  digest_pin_updates: PlanDigestPinUpdate[];
  digest_unpin_updates: PlanDigestUnpinUpdate[];
  actions: PlanAction[];
  lines: PlanLine[];
}

export interface PlanSkipped {
  line_no: number;
  raw: string;
  image: string;
  desired_tag: string;
  reason: string;
}

export interface PlanCleanupItem {
  line_no: number;
  raw: string;
  image: string;
  desired_tag: string;
  digest: string;
  reason: string;
  diagnostic: PendingDiagnostic | null;
}

export interface PlanCleanup {
  cleanup_id: string;
  can_remove_unmatched: boolean;
  items: PlanCleanupItem[];
}

export type ApplyPreflightStatus = "PASS" | "WARN" | "FAIL";

export interface ApplyPreflightCheck {
  status: ApplyPreflightStatus;
  code: string;
  label: string;
  detail: string;
  source_check_codes: string[];
}

export interface ApplyPreflightResponse {
  ok: boolean;
  failures: number;
  warnings: number;
  checks: ApplyPreflightCheck[];
}

export interface TagOverrideRequest {
  line_no: number;
  tag: string;
}

export interface PlanSelectionRequest {
  line_no: number;
  selection_id: string;
}

export interface PlanMutationOptions {
  selections?: PlanSelectionRequest[];
  tagStreamDecisions?: TagStreamDecisionRequest[];
  tagStreamLabelRewriteApprovals?: TagStreamLabelRewriteApprovalRequest[];
}

export interface PlanResponse {
  plan_id: string;
  dry_run: boolean;
  can_apply: boolean;
  status: PlanStatus;
  source_file: string;
  source: PendingSourceInfo;
  mode: string;
  max_wait: number;
  digest_pin_updates: boolean;
  selected_line_numbers: number[];
  selected_selections?: PlanSelectionRequest[];
  summary: PlanSummary;
  targets: PlanTarget[];
  stacks: PlanStack[];
  skipped: PlanSkipped[];
  issues: PlanIssue[];
  cleanup: PlanCleanup;
  apply_preflight: ApplyPreflightResponse;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export type ApplyJobStatus = "queued" | "running" | "success" | "failure";
export type ApplyJobProgressStatus = "running" | "success" | "failure" | "skipped";

export interface ApplyJobProgressEvent {
  job_id: string;
  phase: string;
  status: ApplyJobProgressStatus;
  message: string;
  created_at: string;
  stack: string;
  services: string[];
  line_numbers: number[];
}

export interface ApplyJobResponse {
  job_id: string;
  status: ApplyJobStatus;
  run_id: number | null;
  log_file: string;
  started_at: string | null;
  finished_at: string | null;
  error: string;
  selected_line_numbers: number[];
  progress: ApplyJobProgressEvent[];
}

export interface ApplyJobLogResponse {
  job_id: string;
  log_file: string;
  exists: boolean;
  content: string;
  truncated: boolean;
  max_bytes: number;
  error: string;
}

// ---------------------------------------------------------------------------
// Status and settings
// ---------------------------------------------------------------------------

export interface StatusResponse {
  ok: boolean;
  version: string;
  wud_file: string;
  wud_file_exists: boolean;
  pending_count: number;
  pending_source: PendingSourceInfo;
  source_hash: string;
  db_path: string;
  db_ready: boolean;
  auth_required: boolean;
  dev_auth_bypass: boolean;
  setup_required: boolean;
  mutations_enabled: boolean;
  timezone: string;
  auto_update_scheduler_enabled: boolean;
  static_spa_available: boolean;
  wud_api: WudApiStatus;
  warnings: string[];
}

export type SettingsEntrySource = "configured" | "default" | "derived" | "request";

export interface SettingsEntry {
  name: string;
  value: string;
  default_value: string;
  configured: boolean;
  source: SettingsEntrySource;
}

export interface SecretSettingStatus {
  name: string;
  configured: boolean;
}

export type ManagedSettingSource = "configured" | "default";

export interface ManagedSettingEntry {
  key: string;
  value: string;
  default_value: string;
  source: ManagedSettingSource;
  editable: boolean;
  allowed_values: string[];
  restart_required: boolean;
  disabled_reason: string;
  configured?: boolean;
  sensitive?: boolean;
}

export interface SettingsResponse {
  updater: SettingsEntry[];
  webui: SettingsEntry[];
  secrets: SecretSettingStatus[];
  managed: ManagedSettingEntry[];
}

export interface ManagedSettingsUpdateResponse {
  managed: ManagedSettingEntry[];
  audit_run_id: number;
}

// ---------------------------------------------------------------------------
// Doctor and onboarding
// ---------------------------------------------------------------------------

export type DoctorCheckStatus = "PASS" | "WARN" | "FAIL";

export interface DoctorSuggestion {
  label: string;
  description: string;
  snippet: string;
}

export interface DoctorCheck {
  status: DoctorCheckStatus;
  code: string;
  category: string;
  name: string;
  detail: string;
  target: string;
  suggestions: DoctorSuggestion[];
}

export interface DoctorResponse {
  ok: boolean;
  failures: number;
  warnings: number;
  checks: DoctorCheck[];
}

export interface OnboardingDocLink {
  label: string;
  url: string;
}

export interface OnboardingChecklistItem {
  key: string;
  title: string;
  status: DoctorCheckStatus;
  detail: string;
  check_codes: string[];
  suggestions: DoctorSuggestion[];
  docs: OnboardingDocLink[];
}

export interface OnboardingChecklistResponse {
  dismissed: boolean;
  dismissed_at: string;
  all_passed: boolean;
  visible: boolean;
  items: OnboardingChecklistItem[];
}

export interface OnboardingDismissResponse {
  dismissed: boolean;
  dismissed_at: string;
}

export type CoreUpdateTourStatus =
  | "not_started"
  | "in_progress"
  | "completed"
  | "dismissed";

export type CoreUpdateTourStep =
  | "dashboard"
  | "pending_select"
  | "pending_preflight"
  | "pending_apply"
  | "runs_history";

export interface CoreUpdateTourResponse {
  status: CoreUpdateTourStatus;
  step: CoreUpdateTourStep;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Auth and setup
// ---------------------------------------------------------------------------

export interface AuthSessionResponse {
  authenticated: boolean;
  setup_required: boolean;
  auth_required: boolean;
  dev_auth_bypass: boolean;
  mutations_enabled: boolean;
  username: string | null;
}

export interface CsrfResponse {
  csrf_token: string;
}

export interface SetupStatusResponse {
  setup_required: boolean;
  claim_required: boolean;
  authenticated: boolean;
  auth_required: boolean;
  dev_auth_bypass: boolean;
  mutations_enabled: boolean;
  password_min_length: number;
}

// ---------------------------------------------------------------------------
// Run history
// ---------------------------------------------------------------------------

export interface RunEventRecord {
  id: number;
  run_id: number;
  created_at: string;
  service_name: string;
  stack_name: string;
  image: string;
  target_image: string;
  old_image_id: string;
  new_image_id: string;
  old_digest: string;
  new_digest: string;
  status: string;
  metadata: Record<string, unknown>;
  digest_provenance?: DigestTagProvenance | null;
}

export interface RunSummary {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  dry_run: boolean;
  mode: string;
  wud_file: string;
  log_file: string;
  metadata: Record<string, unknown>;
  events: RunEventRecord[];
}

export interface PendingUpdateRecord {
  id: number;
  run_id: number;
  line_no: number;
  raw: string;
  image: string;
  target_digest: string;
  desired_tag: string;
  service_key: string;
  stack_name: string;
  service_name: string;
  status: string;
  status_reason: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  digest_provenance?: DigestTagProvenance | null;
}

export type RunVerificationStatus = "verified" | "needs_review";
export type RunVerificationImageStatus =
  | "new_image_running"
  | "already_current"
  | "failed"
  | "unknown";
export type RunVerificationContainerStatus =
  | "recreated"
  | "skipped"
  | "failed"
  | "unknown";
export type RunVerificationHealthStatus =
  | "passed"
  | "skipped"
  | "timed_out"
  | "service_disappeared"
  | "failed"
  | "unknown";
export type RunVerificationWudStatus =
  | "removed"
  | "restored"
  | "stale_removed"
  | "removed_before_run"
  | "unknown";

export interface RunVerificationItem {
  line_no: number;
  service_key: string;
  stack_name: string;
  service_name: string;
  image: string;
  target_image: string;
  image_status: RunVerificationImageStatus;
  container_status: RunVerificationContainerStatus;
  health_status: RunVerificationHealthStatus;
  wud_status: RunVerificationWudStatus;
  follow_up_needed: boolean;
  summary: string;
}

export interface RunVerificationSummary {
  status: RunVerificationStatus;
  total_count: number;
  verified_count: number;
  needs_review_count: number;
  items: RunVerificationItem[];
}

export interface RunDetail extends RunSummary {
  pending_updates: PendingUpdateRecord[];
  verification: RunVerificationSummary;
}

export type RollbackPlanStatus =
  | "ready"
  | "partial"
  | "blocked"
  | "not_needed"
  | "not_applicable"
  | "unavailable";

export type RollbackPlanItemStatus = "ready" | "blocked" | "not_needed";

export interface RollbackPlanItem {
  event_id: number;
  service_key: string;
  stack_name: string;
  service_name: string;
  status: RollbackPlanItemStatus;
  reason: string;
  recorded_previous_image: string;
  recorded_target_image: string;
  rollback_image: string;
  previous_image_id: string;
  previous_digest: string;
  current_compose_image: string;
  current_container_image_ids: string[];
}

export interface RollbackPlanResponse {
  run_id: number;
  status: RollbackPlanStatus;
  detail: string;
  ready_count: number;
  blocked_count: number;
  not_needed_count: number;
  items: RollbackPlanItem[];
}

export interface RunLogResponse {
  run_id: number;
  log_file: string;
  exists: boolean;
  content: string;
  truncated: boolean;
  max_bytes: number;
}

export interface LogTail {
  exists: boolean;
  content: string;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

export type WudApiObservationOutcome =
  | "retained"
  | "recovered"
  | "unresolved"
  | "unsupported_ignored";

export type WudApiObservationReasonCode =
  | "malformed_observation"
  | "missing_image"
  | "invalid_update_flag"
  | "reported_error"
  | "missing_scan_result"
  | "unsupported_registry";

export interface WudApiObservationDiagnostic {
  outcome: WudApiObservationOutcome;
  reason_code: WudApiObservationReasonCode;
  container_id: string;
  name: string;
  image: string;
  watcher: string;
  registry: string;
  update_available: boolean | null;
  usable_result: boolean;
  retryable: boolean;
  error: string;
}

export interface WudApiObservationDiagnostics {
  counts: {
    available: number;
    degraded: number;
    retained: number;
    recovered: number;
    unresolved: number;
    unsupported_ignored: number;
  };
  items: WudApiObservationDiagnostic[];
}

export interface DiagnosticsSupportBundleResponse {
  wudup_version: string;
  wud_updater_version?: string;
  settings: SettingsResponse;
  doctor_result: DoctorResponse;
  wud_api_diagnostics: WudApiConfigurationDiagnostics;
  wud_api_observations: WudApiObservationDiagnostics;
  pending_summary: PendingResponse;
  last_run_status: RunSummary | null;
  diagnostics_warnings: string[];
  discovery_warnings: string[];
  log_tail: LogTail | null;
}

// ---------------------------------------------------------------------------
// Service policies, snoozes, and tag exclusions
// ---------------------------------------------------------------------------

export type ServicePolicyUpdateMode = "" | "pause" | "stop" | "live";
export type AutoUpdateDay = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";
export type SnoozeKind = "time" | "dependency";
export type SnoozeState = "active" | "expired" | "all";
export type TagExclusionScope = "image_repo" | "service";
export type TagExclusionMatchType = "exact";
export type TagExclusionStatus = "active" | "disabled";
export type TagExclusionStatusFilter = TagExclusionStatus | "all";

export interface ServicePolicyRecord {
  service_key: string;
  update_mode: ServicePolicyUpdateMode;
  auto_update: boolean;
  snooze_default_seconds: number | null;
  auto_update_time: string | null;
  auto_update_days: AutoUpdateDay[];
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface SnoozeRecord {
  id: number;
  service_key: string;
  snoozed_until: string | null;
  reason: string;
  created_at: string;
  active: boolean;
  kind: SnoozeKind;
  wait_for_service_key: string;
  metadata: Record<string, unknown>;
}

export interface TagExclusionRuleRecord {
  id: number;
  scope: TagExclusionScope;
  image_repo: string;
  service_key: string;
  match_type: TagExclusionMatchType;
  tag: string;
  regex_fragment: string;
  status: TagExclusionStatus;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export type StateOperation =
  | {
      kind: "upsert_service_policy";
      service_key: string;
      update_mode?: ServicePolicyUpdateMode;
      auto_update?: boolean;
      snooze_default_seconds?: number | null;
      auto_update_time?: string | null;
      auto_update_days?: AutoUpdateDay[];
    }
  | {
      kind: "delete_service_policy";
      service_key: string;
    }
  | {
      kind: "create_snooze";
      service_key: string;
      snoozed_until: string;
      reason?: string;
    }
  | {
      kind: "delete_snooze";
      snooze_id: number;
    }
  | {
      kind: "create_dependency_snooze";
      service_key: string;
      wait_for_service_key: string;
      reason?: string;
    }
  | {
      kind: "delete_dependency_snooze";
      snooze_id: number;
    }
  | {
      kind: "upsert_tag_exclusion";
      scope: TagExclusionScope;
      image_repo: string;
      service_key?: string;
      match_type?: TagExclusionMatchType;
      tag: string;
      status?: TagExclusionStatus;
    }
  | {
      kind: "set_tag_exclusion_status";
      rule_id: number;
      status: TagExclusionStatus;
    };

export interface StateOperationResponse {
  operation: StateOperation["kind"];
  status: "success";
  audit_run_id: number;
  resource_type: string;
  resource_id: string;
  resource: ServicePolicyRecord | SnoozeRecord | TagExclusionRuleRecord | null;
}

// ---------------------------------------------------------------------------
// Container restart
// ---------------------------------------------------------------------------

export interface ContainerRestartResponse {
  status: "scheduled";
  audit_run_id: number;
  container: string;
}

// ---------------------------------------------------------------------------
// Self-update
// ---------------------------------------------------------------------------

export type SelfUpdateStatus =
  | "available"
  | "up_to_date"
  | "disabled"
  | "unavailable";
export type SelfUpdateStrategy = "pull_image" | "prepare_tag_update";

export interface SelfUpdateReleaseNote {
  tag: string;
  title: string;
  published_at: string;
  url: string;
  body: string;
  body_truncated: boolean;
  breaking: boolean;
  breaking_reasons: string[];
}

export interface SelfUpdateResponse {
  status: SelfUpdateStatus;
  strategy: SelfUpdateStrategy;
  current_tag: string;
  latest_tag: string;
  current_image: string;
  target_image: string;
  restart_container: string;
  release_notes: SelfUpdateReleaseNote[];
  release_notes_truncated: boolean;
  release_notes_cap: number;
  can_update: boolean;
  disabled_reason: string;
  external_recreate_required: boolean;
  warnings: string[];
}

export interface SelfUpdateApplyResponse {
  status: "prepared_only" | "running_image_verified";
  audit_run_id: number;
  current_tag: string;
  latest_tag: string;
  target_image: string;
  container: string;
  running_image_id: string;
  prepared_image_id: string;
  external_recreate_required: boolean;
}

export interface SelfUpdatePlanResponse {
  strategy: "prepare_tag_update";
  plan: PlanResponse;
  current_tag: string;
  latest_tag: string;
  current_image: string;
  target_image: string;
  restart_container: string;
  external_recreate_required: boolean;
  warning: string;
}

export interface SelfUpdatePrepareResponse {
  status: "tag_prepared";
  audit_run_id: number;
  current_tag: string;
  latest_tag: string;
  target_image: string;
  container: string;
  external_recreate_required: boolean;
}

export interface SelfUpdateRequest {
  confirmation: "pull_image";
  current_tag: string;
  latest_tag: string;
  target_image: string;
  restart_container: string;
}

export interface SelfUpdatePrepareRequest {
  confirmation: "prepare_tag_update";
  plan_id: string;
  current_tag: string;
  latest_tag: string;
  target_image: string;
  restart_container: string;
}
