import type { WebApi } from "../client";
import type {
  CsrfResponse,
  DigestPinLabelRewriteApprovalRequest,
  PendingMetadataRefreshRequest,
  PendingRescanLine,
  PendingRescanScope,
  PlanMutationOptions,
  RetagChoiceRequest,
  SnoozeState,
  TagExclusionStatusFilter,
  TagOverrideRequest,
} from "../types";
import {
  DEMO_CSRF_TOKEN,
  STATIC_DEMO_READ_ONLY_MESSAGE,
} from "./constants";
import { DemoApiState } from "./state";

function rejectStaticDemoMutation(): never {
  throw new Error(STATIC_DEMO_READ_ONLY_MESSAGE);
}

async function rejectStaticDemoMutationAsync(): Promise<never> {
  return rejectStaticDemoMutation();
}

function demoSuggestedTrackingRegex(tag: string): string {
  if (!/^v?\d+(?:\.\d+)+(?:[-_.][A-Za-z0-9][A-Za-z0-9._-]*)?$/.test(tag)) return "";
  if (/^v?\d+(?:\.\d+)+$/.test(tag)) {
    return `^${tag.startsWith("v") ? "v" : ""}\\d+(?:\\.\\d+)+$`;
  }
  return `^${tag.replace(/\d+/g, "\\d+").replaceAll(".", "\\.")}$`;
}

function demoTrackingHealth(regex: string, exact: string, suggested: string) {
  if (!regex) return "no-filter";
  if (regex === exact) return suggested ? "frozen" : "exact-tag";
  return regex === suggested ? "version-pattern" : "custom";
}

function demoTrackingDetail(health: string, regex: string): string {
  if (health === "exact-tag") return "The filter matches this tag only. Same-tag image changes require WUD digest watching.";
  if (health === "frozen") return "The filter matches only the installed version tag.";
  return regex ? "Demo tracking filter; inspect which tags it matches." : "No WUD tag filter is set.";
}

export function createDemoWebApi(): WebApi {
  const state = new DemoApiState();

  return {
    csrf: async (): Promise<CsrfResponse> => ({ csrf_token: DEMO_CSRF_TOKEN }),
    setupStatus: async () => state.setupStatus(),
    setupClaim: async (
      _claim: string,
      _username: string,
      _password: string,
      _csrfToken: string,
    ) => state.session(),
    resetAdminClaim: async (
      _claim: string,
      _username: string,
      _password: string,
      _csrfToken: string,
    ) => state.session(),
    session: async () => state.session(),
    login: async (_username: string, _password: string, _csrfToken: string) =>
      state.session(),
    logout: async (_csrfToken: string) => state.session(),
    status: async () => state.status(),
    settings: async () => state.settings(),
    updateManagedSettings: rejectStaticDemoMutationAsync,
    doctor: async (_csrfToken: string) => state.doctor(),
    onboardingChecklist: async (_csrfToken: string) => state.onboardingChecklist(),
    dismissOnboarding: rejectStaticDemoMutationAsync,
    coreUpdateTour: async () => state.coreUpdateTour,
    updateCoreUpdateTour: rejectStaticDemoMutationAsync,
    pending: async () => state.pendingResponse(),
    pendingMetadata: async (
      request: PendingMetadataRefreshRequest,
      _csrfToken: string,
    ) =>
      state.pendingMetadata(request),
    updateTargets: async () => state.updateTargets(),
    retagTargets: async () => state.retagTargets(),
    trackedContainers: async () => {
      const retags = state.retagTargets();
      return {
        status: retags.status,
        count: retags.count,
        wud_status: null,
        warnings: retags.warnings,
        items: retags.items.map((item) => {
          const trackingRegex = item.label_value.replaceAll("$$", "$");
          const suggestedRegex = demoSuggestedTrackingRegex(item.current_tag);
          const exactTagRegex = `^${item.current_tag.replaceAll(".", "\\.")}$`;
          const trackingHealth = demoTrackingHealth(trackingRegex, exactTagRegex, suggestedRegex);
          return {
            target_id: item.target_id || item.service_key,
            service_key: item.service_key,
            stack: item.stack,
            service: item.service,
            image: item.image,
            current_tag: item.current_tag,
            runtime_state: item.runtime_state,
            tracking_regex: trackingRegex,
            tracking_health: trackingHealth,
            tracking_detail: demoTrackingDetail(trackingHealth, trackingRegex),
            suggested_regex: suggestedRegex === trackingRegex ? "" : suggestedRegex,
            wud: null,
            wud_match_state: "unknown" as const,
            wud_update_available: null,
            last_image_recorded_at: "",
            last_action_at: "",
            last_action_status: "",
            last_action_run_id: null,
            retag_available: item.retag_available,
          };
        }),
      };
    },
    createTrackingRepairPlan: rejectStaticDemoMutationAsync,
    applyTrackingRepair: rejectStaticDemoMutationAsync,
    refreshRetagGithubLatest: rejectStaticDemoMutationAsync,
    startRetagPreview: async (
      choices: RetagChoiceRequest[],
      _csrfToken: string,
      _options = {},
    ) => state.createRetagPreviewJob(choices),
    retagPreviewJob: async (previewJobId: string) =>
      state.retagPreviewJob(previewJobId),
    createRetagPlan: async (
      choices: RetagChoiceRequest[],
      _csrfToken: string,
      _options = {},
    ) => state.createRetagPlan(choices),
    applyRetagPlan: rejectStaticDemoMutationAsync,
    diagnosticsSupportBundle: async () => state.diagnosticsSupportBundle(),
    cleanupPending: rejectStaticDemoMutationAsync,
    createRemovalPlan: rejectStaticDemoMutationAsync,
    removeSelectedPending: rejectStaticDemoMutationAsync,
    rescanPending: async (
      scope: PendingRescanScope,
      lines: PendingRescanLine[],
      _csrfToken: string,
    ) => state.rescanPending(scope, lines),
    releaseNotes: async () => state.releaseNotes(),
    refreshReleaseNotes: async (_csrfToken: string) => state.releaseNotes(),
    previewReleaseNotifications: rejectStaticDemoMutationAsync,
    sendReleaseNotifications: rejectStaticDemoMutationAsync,
    testReleaseNotificationWebhook: rejectStaticDemoMutationAsync,
    securityScans: async () => state.securityScans(),
    refreshSecurityScans: rejectStaticDemoMutationAsync,
    securityScanJob: async (jobId: string) => state.securityScanJob(jobId),
    selfUpdate: async () => state.selfUpdate(),
    planSelfUpdate: async (_csrfToken: string) => state.selfUpdatePlan(),
    applySelfUpdate: rejectStaticDemoMutationAsync,
    prepareSelfUpdate: rejectStaticDemoMutationAsync,
    servicePolicies: async () => state.servicePolicies(),
    snoozes: async (snoozeState: SnoozeState = "active") =>
      state.snoozeRecords(snoozeState),
    tagExclusions: async (status: TagExclusionStatusFilter = "active") =>
      state.tagExclusionRecords(status),
    stateOperation: rejectStaticDemoMutationAsync,
    restartContainer: rejectStaticDemoMutationAsync,
    createPlan: async (
      lineNumbers: number[],
      allowTagUpdates: boolean,
      tagOverrides: TagOverrideRequest[],
      digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
      _csrfToken: string,
      options: PlanMutationOptions = {},
    ) =>
      state.createPlan(
        lineNumbers,
        allowTagUpdates,
        tagOverrides,
        digestPinLabelRewriteApprovals,
        options,
      ),
    createJob: rejectStaticDemoMutationAsync,
    applyPlan: rejectStaticDemoMutationAsync,
    job: rejectStaticDemoMutationAsync,
    applyJob: rejectStaticDemoMutationAsync,
    openJobStream: (_jobId: string) => rejectStaticDemoMutation(),
    runs: async () => state.runSummaries(),
    runDetail: async (runId: number) => state.runDetail(runId),
    rollbackPlan: async (runId: number) => state.rollbackPlan(runId),
    runLog: async (runId: number, _tailBytes = 262_144) => state.runLog(runId),
  };
}
