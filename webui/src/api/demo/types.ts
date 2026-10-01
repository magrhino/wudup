import type {
  AuthSessionResponse,
  DiagnosticsSupportBundleResponse,
  DoctorResponse,
  OnboardingChecklistResponse,
  PendingGroupedItem,
  PendingResponse,
  ReleaseNoteInfo,
  ReleaseNotesResponse,
  RetagTargetsResponse,
  RunDetail,
  RunLogResponse,
  RollbackPlanResponse,
  RunSummary,
  SelfUpdatePlanResponse,
  SelfUpdateResponse,
  ServicePolicyRecord,
  SettingsResponse,
  SetupStatusResponse,
  SnoozeRecord,
  StatusResponse,
  TagExclusionRuleRecord,
  UpdateTargetsResponse,
} from "../types";

export type DemoStackName = "data" | "home" | "media";

export type DemoStack = {
  name: DemoStackName;
  servicesLabel: string;
  services: string[];
};

export type DemoPendingItem = PendingGroupedItem & {
  stack: DemoStackName | "";
  service: string;
};

type DemoDiagnosticsSupportBundleResponse = Omit<
  DiagnosticsSupportBundleResponse,
  "wudup_version" | "wud_updater_version" | "pending_summary"
> & {
  pending_summary: PendingResponse;
};

type ReleaseNoteNotificationFields =
  | "notification_key"
  | "notification_status"
  | "notification_last_sent_at"
  | "notification_send_count"
  | "notification_skipped_reason";

export type DemoReleaseNoteInfo =
  Omit<ReleaseNoteInfo, ReleaseNoteNotificationFields> &
  Partial<Pick<ReleaseNoteInfo, ReleaseNoteNotificationFields>>;

export type DemoReleaseNotesResponse = Omit<ReleaseNotesResponse, "items"> & {
  items: DemoReleaseNoteInfo[];
};

export type DemoGeneratedFixtures = {
  auth: {
    session: AuthSessionResponse;
    setupStatus: SetupStatusResponse;
  };
  status: Omit<StatusResponse, "version">;
  settings: SettingsResponse;
  doctor: DoctorResponse;
  onboarding: OnboardingChecklistResponse;
  pending: PendingResponse;
  updateTargets: UpdateTargetsResponse;
  planCases: never[];
  retagTargets: RetagTargetsResponse;
  retagCases: never[];
  releaseNotes: DemoReleaseNotesResponse;
  selfUpdate: SelfUpdateResponse;
  selfUpdatePlan: SelfUpdatePlanResponse;
  diagnostics: DemoDiagnosticsSupportBundleResponse;
  servicePolicies: ServicePolicyRecord[];
  snoozes: {
    active: SnoozeRecord[];
    expired: SnoozeRecord[];
    all: SnoozeRecord[];
  };
  tagExclusions: {
    active: TagExclusionRuleRecord[];
    disabled: TagExclusionRuleRecord[];
    all: TagExclusionRuleRecord[];
  };
  runs: {
    summaries: RunSummary[];
    details: Record<string, RunDetail>;
    logs: Record<string, RunLogResponse>;
    rollbackPlans: Record<string, RollbackPlanResponse>;
  };
};
