// webui/src/stores/updates.ts
import { computed, ref } from "vue";
import { defineStore } from "pinia";
import {
  ApiError,
  LIVE_JOB_LOG_TAIL_BYTES,
  type ApplyJobLogResponse,
  type ApplyJobResponse,
  type DigestPinLabelRewriteApprovalRequest,
  type PendingCleanupLine,
  type PendingCleanupResponse,
  type PendingGroupedItem,
  type PendingMetadataRefreshItem,
  type PendingRemovalPlanResponse,
  type PendingRescanLine,
  type PendingRescanResponse,
  type PendingRescanScope,
  type PendingItem,
  type PlanResponse,
  type PlanMutationOptions,
  type PlanSelectionRequest,
  type PendingResponse,
  type ReleaseNoteInfo,
  type ReleaseNotesResponse,
  type ReleaseNotificationResponse,
  type ReleaseNotificationSource,
  type RetagChoiceRequest,
  type RetagPlanResponse,
  type RetagPreviewJobResponse,
  type RetagTargetChoice,
  type RetagTargetItem,
  type RetagTargetsResponse,
  type SelfUpdateApplyResponse,
  type SelfUpdatePlanResponse,
  type SelfUpdatePrepareResponse,
  type SelfUpdateResponse,
  type SecurityScanInfo,
  type SecurityScanJobResponse,
  type SecurityScansResponse,
  type TagOverrideRequest,
  type TagStreamDecisionRequest,
  type TagStreamLabelRewriteApprovalRequest,
  type WudContainerMetadata,
  type UpdateTargetsResponse,
  webApi,
} from "../api/client";
import { usePolledJob } from "../composables/usePolledJob";
import { useAuthStore } from "./auth";
import { errorMessage, runWithStoreState } from "./storeState";
import {
  canEnableRetagTargetChoice,
  normalizeRetagChoice,
  retagChoice as selectedRetagChoice,
  retagTargetIdentity,
  retagTargetTagValue,
} from "../utils/retagChoices";
import {
  fetchReleaseChangelog,
  IDLE_RELEASE_CHANGELOG,
  releaseChangelogKey,
  type ReleaseChangelogState,
} from "../utils/releaseChangelog";
import { useRunsStore } from "./runs";

export const APPLY_JOB_RECOVERY_MESSAGE =
  "The update job is no longer available. The WebUI may have restarted. Review its run and log before applying more updates; the outcome is still unknown.";
type ApplyJobRecovery = {
  jobId: string;
  runId: number | null;
  acknowledged: boolean;
};
const PENDING_RESCAN_SELECTION_REQUIRED_MESSAGE =
  "Select at least one pending update to rescan.";
const PENDING_PLAN_METADATA_CHANGED_MESSAGE =
  "Selected update metadata changed. Review the warnings and preview the plan again.";

type PendingLoadOptions = {
  preserveCleanup?: boolean;
  freshAfterCurrent?: boolean;
};

const APPLY_JOB_STORAGE_KEY = "applyJobId";
const RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY = "retagGithubLatestFallback";
const TERMINAL_APPLY_JOB_STATUSES = new Set<ApplyJobResponse["status"]>([
  "success",
  "failure",
]);
const WUD_METADATA_FIELDS: readonly (keyof WudContainerMetadata)[] = [
  "id",
  "name",
  "display_name",
  "status",
  "watcher",
  "local_tag",
  "local_digest",
  "remote_tag",
  "remote_digest",
  "update_kind",
  "semver_diff",
  "link",
  "error",
  "platform",
  "platform_os",
  "platform_architecture",
  "platform_variant",
];

function wudMetadataChanged(
  current: WudContainerMetadata | null | undefined,
  next: WudContainerMetadata | null | undefined,
): boolean {
  const currentMetadata = current ?? null;
  const nextMetadata = next ?? null;
  if (currentMetadata === null || nextMetadata === null) {
    return currentMetadata !== nextMetadata;
  }
  return WUD_METADATA_FIELDS.some(
    (field) => currentMetadata[field] !== nextMetadata[field],
  );
}

function pendingMetadataChanged(
  item: PendingItem,
  metadata: PendingMetadataRefreshItem,
): boolean {
  return (
    item.raw !== metadata.raw ||
    item.source_id !== metadata.source_id ||
    (item.metadata_status ?? "fresh") !==
      (metadata.metadata_status ?? item.metadata_status ?? "fresh") ||
    wudMetadataChanged(item.wud_metadata, metadata.wud_metadata)
  );
}

const TERMINAL_RETAG_PREVIEW_STATUSES = new Set<RetagPreviewJobResponse["status"]>([
  "success",
  "failure",
]);
const TERMINAL_SECURITY_SCAN_STATUSES = new Set<SecurityScanJobResponse["status"]>([
  "success",
  "failure",
]);
export const SECURITY_SCAN_POLL_MAX_ATTEMPTS = 720;
export const SECURITY_SCAN_POLL_INTERVAL_MS = 500;
const SECURITY_SCAN_POLL_ATTEMPTS_PER_CANDIDATE = 720;

export const useUpdatesStore = defineStore("updates", () => {
  const pending = ref<PendingResponse | null>(null);
  const pendingWudMetadataCheckedAt = ref("");
  const updateTargets = ref<UpdateTargetsResponse | null>(null);
  const retagTargets = ref<RetagTargetsResponse | null>(null);
  const retagChoices = ref<Record<string, RetagTargetChoice>>({});
  const retagTargetTags = ref<Record<string, string>>({});
  const retagPlan = ref<RetagPlanResponse | null>(null);
  const retagGithubLatestFallback = ref(readRememberedRetagGithubLatestFallback());
  let retagPreviewStart: (() => Promise<RetagPreviewJobResponse>) | null = null;
  const retagPreviewPoller = usePolledJob<RetagPreviewJobResponse>(
    () => {
      if (retagPreviewStart === null) {
        throw new Error("Retag preview was not started");
      }
      return retagPreviewStart();
    },
    (job) => webApi.retagPreviewJob(job.preview_job_id),
    (job) => TERMINAL_RETAG_PREVIEW_STATUSES.has(job.status),
    { intervalMs: 400 },
  );
  const releaseNotes = ref<ReleaseNotesResponse | null>(null);
  const releaseNotification = ref<ReleaseNotificationResponse | null>(null);
  const securityScans = ref<SecurityScansResponse | null>(null);
  const currentSecurityScans = computed<SecurityScansResponse | null>(() => {
    const scans = securityScans.value;
    const pendingSourceHash = pending.value?.source_hash ?? "";
    if (!scans || !pendingSourceHash || scans.source_hash !== pendingSourceHash) {
      return null;
    }
    return scans;
  });
  const securityScansCurrent = computed(() => currentSecurityScans.value !== null);
  const currentSecurityScanItems = computed<SecurityScanInfo[]>(() => {
    const scans = currentSecurityScans.value;
    const pendingItems = pending.value?.items ?? [];
    if (!scans || pendingItems.length === 0) {
      return [];
    }
    const pendingLineNumbers = new Set(pendingItems.map((item) => item.line_no));
    return scans.items.filter((scan) => pendingLineNumbers.has(scan.line_no));
  });
  const securityScanJob = ref<SecurityScanJobResponse | null>(null);
  const releaseChangelogs = ref<Record<string, ReleaseChangelogState>>({});
  const releaseChangelogRequests = new Map<string, Promise<void>>();
  const selfUpdate = ref<SelfUpdateResponse | null>(null);
  const selfUpdatePlan = ref<SelfUpdatePlanResponse | null>(null);
  const selfUpdateMessage = ref("");
  const selfUpdateError = ref("");
  const plan = ref<PlanResponse | null>(null);
  const pendingCleanup = ref<PendingCleanupResponse | null>(null);
  const pendingRemovalPlan = ref<PendingRemovalPlanResponse | null>(null);
  const pendingRescan = ref<PendingRescanResponse | null>(null);
  const applyJob = ref<ApplyJobResponse | null>(null);
  const applyJobLog = ref<ApplyJobLogResponse | null>(null);
  const rememberedApplyJobId = ref(readRememberedApplyJobId());
  const rememberedApplyRunId = ref<number | null>(readApplyRecoveryStorage("applyJobRun", null));
  const applyJobRecoveries = ref<ApplyJobRecovery[]>(readApplyRecoveryStorage("applyJobRecoveries", []));
  const loading = ref(false);
  const releaseNotesLoading = ref(false);
  const releaseNotesError = ref("");
  const releaseNotificationLoading = ref(false);
  const releaseNotificationError = ref("");
  const securityScansLoading = ref(false);
  const securityScansError = ref("");
  const error = ref("");
  let pendingLoadInFlight: Promise<void> | null = null;
  let pendingLoadTrailing: Promise<void> | null = null;
  let pendingLoadTrailingPreservesCleanup = true;

  async function loadWithState(work: () => Promise<void>): Promise<void> {
    await runWithStoreState(loading, error, work);
  }

  function startPendingLoad(options: PendingLoadOptions): Promise<void> {
    const load = loadWithState(() => reloadPending(options)).finally(() => {
      if (pendingLoadInFlight === load) {
        pendingLoadInFlight = pendingLoadTrailing;
      }
    });
    pendingLoadInFlight = load;
    return load;
  }

  function loadPending(options: PendingLoadOptions = {}): Promise<void> {
    const active = pendingLoadInFlight;
    if (active === null) {
      return startPendingLoad(options);
    }
    if (!options.freshAfterCurrent) {
      return pendingLoadTrailing ?? active;
    }

    pendingLoadTrailingPreservesCleanup &&= options.preserveCleanup === true;
    if (pendingLoadTrailing !== null) {
      return pendingLoadTrailing;
    }

    const trailing = active
      .catch(() => undefined)
      .then(() => {
        const preserveCleanup = pendingLoadTrailingPreservesCleanup;
        pendingLoadTrailing = null;
        pendingLoadTrailingPreservesCleanup = true;
        return startPendingLoad({ preserveCleanup });
      })
      .finally(() => {
        if (pendingLoadTrailing === trailing) {
          pendingLoadTrailing = null;
          pendingLoadTrailingPreservesCleanup = true;
        }
      });
    pendingLoadTrailing = trailing;
    return trailing;
  }

  async function refreshPendingMetadata(
    selectedLineNumbers?: readonly number[],
  ): Promise<void> {
    const current = pending.value;
    if (current === null) {
      return;
    }
    const selectedScope = [
      ...new Set(
        selectedLineNumbers ?? plan.value?.selected_line_numbers ?? [],
      ),
    ];
    const auth = useAuthStore();
    const lines = current.items.map((item) => ({
      line_no: item.line_no,
      raw: item.raw,
      source_id: item.source_id,
    }));
    const response = await webApi.pendingMetadata(
      {
        source_hash: current.source_hash ?? "",
        lines,
      },
      await auth.ensureCsrf(),
    );
    if (response.requires_pending_reload) {
      const openPlan = plan.value;
      const sourceHashChanged =
        response.source_hash !== (current.source_hash ?? "");
      clearReleaseNoteDisplay();
      await loadPending({
        preserveCleanup: true,
        freshAfterCurrent: true,
      });
      const selectedMetadataChanged =
        sourceHashChanged ||
        pendingLinesChanged(
          current.items,
          pending.value?.items ?? [],
          selectedScope,
        );
      if (openPlan !== null && selectedMetadataChanged) {
        error.value = PENDING_PLAN_METADATA_CHANGED_MESSAGE;
      } else if (openPlan !== null) {
        plan.value = openPlan;
      }
      return;
    }
    if (pending.value !== current) {
      return;
    }
    const refreshedByLine = new Map(
      response.items.map((item) => [item.line_no, item]),
    );
    const currentByLine = new Map(
      current.items.map((item) => [item.line_no, item]),
    );
    const selectedPlanMetadataChanged = selectedScope.some((lineNo) => {
      const currentItem = currentByLine.get(lineNo);
      const refreshedItem = refreshedByLine.get(lineNo);
      return (
        currentItem !== undefined &&
        refreshedItem !== undefined &&
        (currentItem.metadata_status ?? "fresh") !==
          (refreshedItem.metadata_status ?? currentItem.metadata_status ?? "fresh")
      );
    });
    const metadataChanged = patchPendingMetadata(response.items);
    if (pending.value) {
      pending.value = {
        ...pending.value,
        source_hash: response.source_hash,
        source: response.source,
        wud_api: response.wud_api,
      };
    }
    pendingWudMetadataCheckedAt.value = response.wud_api.last_checked_at;
    settlePendingRescan(response.wud_api.last_checked_at);
    if (metadataChanged) {
      clearReleaseNoteDisplay();
    }
    if (selectedPlanMetadataChanged) {
      plan.value = null;
      error.value = PENDING_PLAN_METADATA_CHANGED_MESSAGE;
    }
  }

  async function loadUpdateTargets(): Promise<void> {
    await loadWithState(async () => {
      updateTargets.value = await webApi.updateTargets();
    });
  }

  async function loadRetagTargets(
    options: { githubLatestFallback?: boolean } = {},
  ): Promise<void> {
    const githubLatestFallback =
      options.githubLatestFallback ?? retagGithubLatestFallback.value;
    retagGithubLatestFallback.value = githubLatestFallback;
    await loadWithState(async () => {
      retagTargets.value = await webApi.retagTargets({
        github_latest_fallback: githubLatestFallback,
      });
      resetRetagChoices();
      retagPlan.value = null;
      retagPreviewPoller.reset();
    });
  }

  async function setRetagGithubLatestFallback(enabled: boolean): Promise<void> {
    retagGithubLatestFallback.value = enabled;
    writeRememberedRetagGithubLatestFallback(enabled);
    await loadRetagTargets({ githubLatestFallback: enabled });
  }

  async function refreshRetagGithubLatest(): Promise<void> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      retagTargets.value = await webApi.refreshRetagGithubLatest(
        await auth.ensureCsrf(),
      );
      retagGithubLatestFallback.value = true;
      writeRememberedRetagGithubLatestFallback(true);
      resetRetagChoices();
      retagPlan.value = null;
      retagPreviewPoller.reset();
    });
  }

  function resetRetagChoices(): void {
    const items = retagTargets.value?.items ?? [];
    retagChoices.value = Object.fromEntries(
      items.map((item) => [
        retagTargetIdentity(item),
        "keep-current" satisfies RetagTargetChoice,
      ]),
    );
    retagTargetTags.value = Object.fromEntries(
      items.map((item) => [
        retagTargetIdentity(item),
        item.retag_available ? item.proposed_tag : "",
      ]),
    );
  }

  function findRetagTarget(targetKey: string): RetagTargetItem | undefined {
    const items = retagTargets.value?.items ?? [];
    const targetIdMatch = items.find((target) => target.target_id === targetKey);
    if (targetIdMatch) {
      return targetIdMatch;
    }
    const serviceKeyMatches = items.filter(
      (target) => target.service_key === targetKey,
    );
    return serviceKeyMatches.length === 1 ? serviceKeyMatches[0] : undefined;
  }

  function setRetagChoice(
    targetKey: string,
    choice: RetagTargetChoice,
  ): void {
    const item = findRetagTarget(targetKey);
    const choiceKey = item ? retagTargetIdentity(item) : targetKey;
    retagChoices.value = {
      ...retagChoices.value,
      [choiceKey]: item
        ? normalizeRetagChoice(item, choice, retagTargetTags.value)
        : choice,
    };
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function setRetagChoicesForItems(
    items: RetagTargetItem[],
    choice: RetagTargetChoice,
  ): void {
    const currentItems = new Map(
      (retagTargets.value?.items ?? []).map((item) => [
        retagTargetIdentity(item),
        item,
      ]),
    );
    const nextChoices = { ...retagChoices.value };
    for (const requestedItem of items) {
      const item = currentItems.get(retagTargetIdentity(requestedItem));
      if (!item) {
        continue;
      }
      nextChoices[retagTargetIdentity(item)] =
        choice === "switch-to-concrete" &&
        canEnableRetagTargetChoice(item, retagTargetTags.value)
          ? "switch-to-concrete"
          : "keep-current";
    }
    retagChoices.value = nextChoices;
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function setRetagTargetTag(targetKey: string, tag: string): void {
    const item = findRetagTarget(targetKey);
    const choiceKey = item ? retagTargetIdentity(item) : targetKey;
    retagTargetTags.value = {
      ...retagTargetTags.value,
      [choiceKey]: tag,
    };
    if (item && tag.trim()) {
      retagChoices.value = {
        ...retagChoices.value,
        [choiceKey]: "switch-to-concrete",
      };
    }
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function retagChoiceRequests(): RetagChoiceRequest[] {
    const items = retagTargets.value?.items ?? [];
    return items
      .map((item) => {
        const choice = selectedRetagChoice(
          item,
          retagChoices.value,
          retagTargetTags.value,
        );
        const request: RetagChoiceRequest = {
          service_key: item.service_key,
          choice,
        };
        const targetId = retagTargetIdentity(item);
        if (targetId !== item.service_key) {
          request.target_id = targetId;
        }
        if (choice === "switch-to-concrete") {
          if (item.runtime_state !== "running") {
            request.allow_start = true;
          }
          const tag = retagTargetTagValue(item, retagTargetTags.value).trim();
          if (tag) {
            request.target_tag = tag;
          }
        }
        return request;
      })
      .sort(
        (left, right) =>
          left.service_key.localeCompare(right.service_key) ||
          (left.target_id ?? "").localeCompare(right.target_id ?? ""),
      );
  }

  async function createRetagPlan(): Promise<RetagPlanResponse> {
    const auth = useAuthStore();
    let response: RetagPlanResponse | null = null;
    await loadWithState(async () => {
      retagPlan.value = null;
      applyJob.value = null;
      applyJobLog.value = null;
      const choices = retagChoiceRequests();
      const csrfToken = await auth.ensureCsrf();
      const options = { github_latest_fallback: retagGithubLatestFallback.value };
      retagPreviewStart = () =>
        webApi.startRetagPreview(choices, csrfToken, options);
      const job = await retagPreviewPoller.run();
      if (job.status === "failure") {
        throw new Error(job.error || "Retag preview failed");
      }
      if (job.plan === null) {
        throw new Error("Retag preview did not return a plan");
      }
      response = job.plan;
      retagPlan.value = job.plan;
    });
    if (response === null) {
      throw new Error("Retag plan did not return a response");
    }
    return response;
  }

  async function applyRetagPlan(): Promise<ApplyJobResponse> {
    const auth = useAuthStore();
    const planToApply = retagPlan.value;
    if (planToApply === null) {
      throw new Error("Retag preview must be loaded before applying");
    }
    await loadWithState(async () => {
      applyJobLog.value = null;
      const job = await webApi.applyRetagPlan(
        planToApply.plan_id,
        retagChoiceRequests(),
        await auth.ensureCsrf(),
        { github_latest_fallback: retagGithubLatestFallback.value },
      );
      setApplyJob(job);
    });
    if (applyJob.value === null) {
      throw new Error("Apply job was not created");
    }
    return applyJob.value;
  }

  async function loadReleaseNotes(): Promise<void> {
    releaseNotesLoading.value = true;
    releaseNotesError.value = "";
    try {
      releaseNotes.value = await webApi.releaseNotes();
    } catch (caughtError) {
      releaseNotesError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      releaseNotesLoading.value = false;
    }
  }

  async function refreshReleaseNotes(): Promise<void> {
    const auth = useAuthStore();
    releaseNotesLoading.value = true;
    releaseNotesError.value = "";
    try {
      releaseNotes.value = await webApi.refreshReleaseNotes(await auth.ensureCsrf());
    } catch (caughtError) {
      releaseNotesError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      releaseNotesLoading.value = false;
    }
  }

  async function previewReleaseNotifications(
    source: ReleaseNotificationSource,
  ): Promise<ReleaseNotificationResponse> {
    const auth = useAuthStore();
    releaseNotificationLoading.value = true;
    releaseNotificationError.value = "";
    releaseNotification.value = null;
    try {
      releaseNotification.value = await webApi.previewReleaseNotifications(
        source,
        await auth.ensureCsrf(),
      );
      return releaseNotification.value;
    } catch (caughtError) {
      releaseNotificationError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      releaseNotificationLoading.value = false;
    }
  }

  async function sendReleaseNotifications(
    source: ReleaseNotificationSource,
  ): Promise<ReleaseNotificationResponse> {
    const auth = useAuthStore();
    releaseNotificationLoading.value = true;
    releaseNotificationError.value = "";
    try {
      releaseNotification.value = await webApi.sendReleaseNotifications(
        source,
        await auth.ensureCsrf(),
      );
      return releaseNotification.value;
    } catch (caughtError) {
      releaseNotificationError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      releaseNotificationLoading.value = false;
    }
  }

  function clearReleaseNotification(): void {
    releaseNotification.value = null;
    releaseNotificationError.value = "";
  }

  async function loadSecurityScans(): Promise<void> {
    securityScansLoading.value = true;
    securityScansError.value = "";
    try {
      securityScans.value = await webApi.securityScans();
    } catch (caughtError) {
      securityScansError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      securityScansLoading.value = false;
    }
  }

  async function refreshSecurityScans(): Promise<void> {
    const auth = useAuthStore();
    securityScansLoading.value = true;
    securityScansError.value = "";
    try {
      let job = await webApi.refreshSecurityScans(await auth.ensureCsrf());
      securityScanJob.value = job;
      let pollAttempts = 0;
      while (!TERMINAL_SECURITY_SCAN_STATUSES.has(job.status)) {
        if (pollAttempts >= securityScanPollMaxAttempts(job)) {
          throw new Error("Security scan refresh timed out");
        }
        pollAttempts += 1;
        await delay(SECURITY_SCAN_POLL_INTERVAL_MS);
        job = await webApi.securityScanJob(job.job_id);
        securityScanJob.value = job;
      }
      if (job.status === "failure") {
        throw new Error(job.error || "Security scan refresh failed");
      }
      if (job.result) {
        securityScans.value = job.result;
      } else {
        await loadSecurityScans();
      }
    } catch (caughtError) {
      securityScansError.value = errorMessage(caughtError);
      throw caughtError;
    } finally {
      securityScansLoading.value = false;
    }
  }

  function securityScanFor(item: PendingItem): SecurityScanInfo | null {
    const scans = currentSecurityScans.value;
    if (!scans) {
      return null;
    }
    return currentSecurityScanItems.value.find(
      (candidate) => candidate.line_no === item.line_no,
    ) ?? null;
  }

  function securityScanPollMaxAttempts(job: SecurityScanJobResponse): number {
    const totalCount = Math.max(1, job.total_count || 0);
    return Math.max(
      SECURITY_SCAN_POLL_MAX_ATTEMPTS,
      totalCount * SECURITY_SCAN_POLL_ATTEMPTS_PER_CANDIDATE,
    );
  }

  function releaseChangelogStateFor(
    note: ReleaseNoteInfo | null,
  ): ReleaseChangelogState {
    const key = releaseChangelogKeyFor(note);
    return key
      ? releaseChangelogs.value[key] ?? IDLE_RELEASE_CHANGELOG
      : IDLE_RELEASE_CHANGELOG;
  }

  async function loadReleaseChangelog(note: ReleaseNoteInfo | null): Promise<void> {
    const link = releaseChangelogLinkFor(note);
    if (link === "") {
      return;
    }
    const key = releaseChangelogKey(link);
    if (key === "") {
      return;
    }
    const currentState = releaseChangelogs.value[key];
    if (currentState?.status === "ready") {
      return;
    }
    const pendingRequest = releaseChangelogRequests.get(key);
    if (pendingRequest) {
      await pendingRequest;
      return;
    }
    setReleaseChangelogState(key, {
      status: "loading",
      body: "",
      sourceUrl: "",
      error: "",
    });
    const request = fetchReleaseChangelog(link, note?.release_tag ?? "")
      .then((result) => {
        if (result.status === "ready") {
          setReleaseChangelogState(key, {
            status: "ready",
            body: result.body,
            sourceUrl: result.sourceUrl,
            error: "",
          });
          return;
        }
        setReleaseChangelogState(key, {
          status: "unavailable",
          body: "",
          sourceUrl: "",
          error: result.error,
        });
      })
      .catch(() => {
        setReleaseChangelogState(key, {
          status: "error",
          body: "",
          sourceUrl: "",
          error: "Could not load notes. Try again or open the GitHub release.",
        });
      })
      .finally(() => {
        releaseChangelogRequests.delete(key);
      });
    releaseChangelogRequests.set(key, request);
    await request;
  }

  function setReleaseChangelogState(
    key: string,
    state: ReleaseChangelogState,
  ): void {
    releaseChangelogs.value = {
      ...releaseChangelogs.value,
      [key]: state,
    };
  }

  async function loadSelfUpdate(): Promise<void> {
    selfUpdateError.value = "";
    try {
      selfUpdate.value = await webApi.selfUpdate();
      selfUpdatePlan.value = null;
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
  }

  async function planSelfUpdate(): Promise<SelfUpdatePlanResponse> {
    const auth = useAuthStore();
    selfUpdateError.value = "";
    let response: SelfUpdatePlanResponse | null = null;
    try {
      await loadWithState(async () => {
        response = await webApi.planSelfUpdate(await auth.ensureCsrf());
        selfUpdatePlan.value = response;
      });
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
    if (response === null) {
      throw new Error("Self-update plan did not return a response");
    }
    return response;
  }

  async function applySelfUpdate(): Promise<
    SelfUpdateApplyResponse | SelfUpdatePrepareResponse
  > {
    const auth = useAuthStore();
    selfUpdateMessage.value = "";
    selfUpdateError.value = "";
    let response: SelfUpdateApplyResponse | SelfUpdatePrepareResponse | null = null;
    try {
      await loadWithState(async () => {
        if (selfUpdate.value === null) {
          throw new Error("Self-update status has not been loaded");
        }
        if (selfUpdate.value.strategy === "prepare_tag_update") {
          const planLocal = selfUpdatePlan.value;
          if (planLocal === null) {
            throw new Error(
              "Self-update tag update preview must be loaded before applying",
            );
          }
          const csrfToken = await auth.ensureCsrf();
          response = await webApi.prepareSelfUpdate(
            csrfToken,
            selfUpdate.value,
            planLocal,
          );
          selfUpdateMessage.value =
            "Tag updated and image pulled. Recreate the WUDup container from outside the WebUI to run the new version. Tagged deployments are recommended for predictable updates.";
        } else {
          const csrfToken = await auth.ensureCsrf();
          response = await webApi.applySelfUpdate(csrfToken, selfUpdate.value);
          selfUpdateMessage.value = response.external_recreate_required
            ? "Image prepared, but the running container still uses the previous image. Recreate the WUDup container to run the new version."
            : "Running container image identity matches the prepared update.";
        }
        try {
          selfUpdate.value = await webApi.selfUpdate();
          selfUpdatePlan.value = null;
        } catch {
          // Keep the success visible even if the follow-up status check fails.
        }
      });
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
    if (response === null) {
      throw new Error("Self-update did not return a response");
    }
    return response;
  }

  async function createPlan(
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[] = [],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[] = [],
    selections: PlanSelectionRequest[] = [],
    tagStreamDecisions: TagStreamDecisionRequest[] = [],
    tagStreamLabelRewriteApprovals: TagStreamLabelRewriteApprovalRequest[] = [],
  ): Promise<void> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      plan.value = null;
      pendingCleanup.value = null;
      pendingRemovalPlan.value = null;
      applyJob.value = null;
      applyJobLog.value = null;
      plan.value = await webApi.createPlan(
        lineNumbers,
        allowTagUpdates,
        tagOverrides,
        digestPinLabelRewriteApprovals,
        await auth.ensureCsrf(),
        {
          selections,
          tagStreamDecisions,
          tagStreamLabelRewriteApprovals,
        },
      );
    });
  }

  async function cleanupPending(
    cleanupId: string,
    lines: PendingCleanupLine[],
  ): Promise<PendingCleanupResponse> {
    const auth = useAuthStore();
    let response: PendingCleanupResponse | null = null;
    await loadWithState(async () => {
      response = await webApi.cleanupPending(
        cleanupId,
        lines,
        await auth.ensureCsrf(),
      );
      pendingCleanup.value = response;
      plan.value = null;
      pendingRemovalPlan.value = null;
    });
    if (response === null) {
      throw new Error("Pending cleanup did not return a response");
    }
    return response;
  }

  async function createRemovalPlan(
    lineNumbers: number[],
  ): Promise<PendingRemovalPlanResponse> {
    const auth = useAuthStore();
    let response: PendingRemovalPlanResponse | null = null;
    await loadWithState(async () => {
      plan.value = null;
      pendingCleanup.value = null;
      pendingRemovalPlan.value = await webApi.createRemovalPlan(
        lineNumbers,
        await auth.ensureCsrf(),
      );
      response = pendingRemovalPlan.value;
    });
    if (response === null) {
      throw new Error("Pending removal plan did not return a response");
    }
    return response;
  }

  async function removeSelectedPending(
    removalId: string,
    lines: PendingCleanupLine[],
  ): Promise<PendingCleanupResponse> {
    const auth = useAuthStore();
    let response: PendingCleanupResponse | null = null;
    await loadWithState(async () => {
      response = await webApi.removeSelectedPending(
        removalId,
        lines,
        await auth.ensureCsrf(),
      );
      pendingCleanup.value = response;
      pendingRemovalPlan.value = null;
      plan.value = null;
    });
    if (response === null) {
      throw new Error("Pending removal did not return a response");
    }
    return response;
  }

  async function rescanPending(
    scope: PendingRescanScope,
    lineNumbers: number[] = [],
  ): Promise<PendingRescanResponse> {
    if (scope === "selected" && lineNumbers.length === 0) {
      pendingRescan.value = null;
      error.value = PENDING_RESCAN_SELECTION_REQUIRED_MESSAGE;
      throw new Error(PENDING_RESCAN_SELECTION_REQUIRED_MESSAGE);
    }
    const auth = useAuthStore();
    const runs = useRunsStore();
    let response: PendingRescanResponse | null = null;
    await loadWithState(async () => {
      plan.value = null;
      pendingCleanup.value = null;
      pendingRemovalPlan.value = null;
      pendingRescan.value = null;
      response = await webApi.rescanPending(
        scope,
        rescanLinesFor(scope, lineNumbers),
        await auth.ensureCsrf(),
      );
    });
    const rescanResponse = response;
    if (rescanResponse === null) {
      throw new Error("Pending rescan did not return a response");
    }
    pendingRescan.value = rescanResponse;
    await loadPending({ freshAfterCurrent: true });
    await loadReleaseNotes().catch(() => undefined);
    await loadSecurityScans().catch(() => undefined);
    refreshReleaseNotes().catch(() => undefined);
    await runs.loadRuns().catch(() => undefined);
    return rescanResponse;
  }

  function clearPlan(): void {
    plan.value = null;
    pendingRemovalPlan.value = null;
  }

  async function reloadPending(
    options: Pick<PendingLoadOptions, "preserveCleanup"> = {},
  ): Promise<void> {
    plan.value = null;
    pendingRemovalPlan.value = null;
    if (!options.preserveCleanup) {
      pendingCleanup.value = null;
    }
    setPending(await webApi.pending());
  }

  function setPending(response: PendingResponse): void {
    pending.value = response;
    pendingWudMetadataCheckedAt.value = response.wud_api.last_checked_at;
    settlePendingRescan(response.wud_api.last_checked_at);
  }

  function settlePendingRescan(checkedAt: string): void {
    const previous = pendingRescan.value?.wud_api.last_checked_at;
    if (pendingRescan.value && Date.parse(checkedAt) > (Date.parse(previous ?? "") || 0)) {
      pendingRescan.value = null;
    }
  }

  function pendingLinesChanged(
    before: PendingItem[],
    after: PendingItem[],
    lineNumbers: readonly number[],
  ): boolean {
    const beforeByLine = new Map(before.map((item) => [item.line_no, item]));
    const afterByLine = new Map(after.map((item) => [item.line_no, item]));
    return lineNumbers.some((lineNo) => {
      const previous = beforeByLine.get(lineNo);
      const current = afterByLine.get(lineNo);
      if (previous === undefined) {
        return true;
      }
      return (
        previous.raw !== current?.raw ||
        previous.source_id !== current?.source_id ||
        (previous.metadata_status ?? "fresh") !==
          (current?.metadata_status ?? "fresh")
      );
    });
  }

  function patchPendingMetadata(items: PendingMetadataRefreshItem[]): boolean {
    const current = pending.value;
    if (current === null) {
      return false;
    }
    const byLine = new Map(items.map((item) => [item.line_no, item]));
    let changed = false;
    const patchItem = <T extends PendingItem>(item: T): T => {
      const metadata = byLine.get(item.line_no);
      if (!metadata) {
        return item;
      }
      if (pendingMetadataChanged(item, metadata)) {
        changed = true;
      }
      return {
        ...item,
        raw: metadata.raw,
        source_id: metadata.source_id,
        metadata_status: metadata.metadata_status ?? item.metadata_status ?? "fresh",
        wud_metadata: metadata.wud_metadata,
      };
    };
    pending.value = {
      ...current,
      items: current.items.map(patchItem),
      grouping: {
        ...current.grouping,
        groups: current.grouping.groups.map((group) => ({
          ...group,
          items: group.items.map((item: PendingGroupedItem) => patchItem(item)),
        })),
        unmatched: current.grouping.unmatched.map((item) => patchItem(item)),
      },
    };
    return changed;
  }

  function clearReleaseNoteDisplay(): void {
    releaseNotes.value = null;
    releaseNotesError.value = "";
    releaseNotification.value = null;
    releaseNotificationError.value = "";
  }

  async function createJob(
    planId: string,
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[] = [],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[] = [],
    options: PlanMutationOptions = {},
  ): Promise<ApplyJobResponse> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      applyJobLog.value = null;
      const job = await webApi.createJob(
        planId,
        lineNumbers,
        allowTagUpdates,
        tagOverrides,
        digestPinLabelRewriteApprovals,
        await auth.ensureCsrf(),
        options,
      );
      setApplyJob(job);
    });
    if (applyJob.value === null) {
      throw new Error("Apply job was not created");
    }
    return applyJob.value;
  }

  async function applyPlan(
    planId: string,
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[] = [],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[] = [],
    options: PlanMutationOptions = {},
  ): Promise<ApplyJobResponse> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      applyJobLog.value = null;
      const job = await webApi.applyPlan(
        planId,
        lineNumbers,
        allowTagUpdates,
        tagOverrides,
        digestPinLabelRewriteApprovals,
        await auth.ensureCsrf(),
        options,
      );
      setApplyJob(job);
    });
    if (applyJob.value === null) {
      throw new Error("Apply job was not created");
    }
    return applyJob.value;
  }

  function setApplyJob(job: ApplyJobResponse): void {
    applyJob.value = job;
    rememberApplyJob(job);
  }

  function setApplyJobLog(log: ApplyJobLogResponse): void {
    applyJobLog.value = log;
  }

  function setError(message: string): void {
    error.value = message;
  }

  async function loadApplyJob(
    jobId: string,
    options: { recoverMissing?: boolean } = {},
  ): Promise<ApplyJobResponse | null> {
    try {
      const job = await webApi.job(jobId);
      setApplyJob(job);
      return job;
    } catch (caughtError) {
      if (
        options.recoverMissing &&
        caughtError instanceof ApiError &&
        caughtError.status === 404
      ) {
        markApplyJobRecovery(jobId);
        return null;
      }
      error.value = errorMessage(caughtError);
      throw caughtError;
    }
  }

  async function loadApplyJobLogFromRun(
    job: ApplyJobResponse | null = applyJob.value,
  ): Promise<ApplyJobLogResponse | null> {
    if (!job?.run_id) {
      return null;
    }
    try {
      const runLog = await webApi.runLog(job.run_id, LIVE_JOB_LOG_TAIL_BYTES);
      const log: ApplyJobLogResponse = {
        job_id: job.job_id,
        log_file: runLog.log_file || job.log_file,
        exists: runLog.exists,
        content: runLog.content,
        truncated: runLog.truncated,
        max_bytes: runLog.max_bytes,
        error: "",
      };
      setApplyJobLog(log);
      return log;
    } catch (caughtError) {
      error.value = errorMessage(caughtError);
      return null;
    }
  }

  function markApplyJobRecovery(jobId: string): void {
    let runId: number | null = null;
    if (applyJob.value?.job_id === jobId) {
      runId = applyJob.value.run_id;
    } else if (rememberedApplyJobId.value === jobId) {
      runId = rememberedApplyRunId.value;
    }
    if (!applyJobRecoveries.value.some((notice) => notice.jobId === jobId)) {
      applyJobRecoveries.value.push({ jobId, runId, acknowledged: false });
      persistApplyJobRecoveries();
    }
    if (applyJob.value?.job_id === jobId) {
      applyJob.value = null;
      applyJobLog.value = null;
    }
    if (rememberedApplyJobId.value === jobId) clearRememberedApplyJobId();
  }

  function persistApplyJobRecoveries(): void {
    writeApplyRecoveryStorage("applyJobRecoveries", applyJobRecoveries.value);
  }

  function acknowledgeApplyJobRecovery(jobId: string, acknowledged = true): void {
    const notice = applyJobRecoveries.value.find((entry) => entry.jobId === jobId);
    if (notice) notice.acknowledged = acknowledged;
    persistApplyJobRecoveries();
  }

  function applyJobRecoveryResolved(runId: number | null): boolean {
    const run = runId === null ? null : useRunsStore().runDetails[runId];
    return Boolean(run?.finished_at && !run.dry_run && run.status === "success" &&
      run.verification.status === "verified" && run.verification.total_count > 0);
  }

  function dismissResolvedApplyJobRecovery(jobId: string): void {
    applyJobRecoveries.value = applyJobRecoveries.value.filter((notice) =>
      notice.jobId !== jobId || !applyJobRecoveryResolved(notice.runId));
    persistApplyJobRecoveries();
  }

  async function reviewApplyJobRecovery(jobId: string): Promise<void> {
    const notice = applyJobRecoveries.value.find((entry) => entry.jobId === jobId);
    if (!notice) return;
    // Only a job response may establish correlation; run order is not evidence.
    if (notice.runId === null) {
      try {
        const job = await webApi.job(jobId);
        notice.runId = job.run_id;
        persistApplyJobRecoveries();
      } catch (caughtError) {
        if (!(caughtError instanceof ApiError && caughtError.status === 404)) throw caughtError;
      }
    }
    if (notice.runId !== null) await useRunsStore().loadRunDetail(notice.runId);
  }

  function rememberApplyJob(job: ApplyJobResponse): void {
    if (TERMINAL_APPLY_JOB_STATUSES.has(job.status)) {
      clearRememberedApplyJobId();
      return;
    }
    rememberedApplyJobId.value = job.job_id;
    rememberedApplyRunId.value = job.run_id;
    writeRememberedApplyJobId(job.job_id);
    writeApplyRecoveryStorage("applyJobRun", { jobId: job.job_id, runId: job.run_id });
  }

  function clearRememberedApplyJobId(): void {
    rememberedApplyJobId.value = "";
    rememberedApplyRunId.value = null;
    removeRememberedApplyJobId();
    writeApplyRecoveryStorage("applyJobRun", null);
  }

  function rescanLinesFor(
    scope: PendingRescanScope,
    lineNumbers: number[],
  ): PendingRescanLine[] {
    if (scope !== "selected") {
      return [];
    }
    const byLine = new Map(
      (pending.value?.items ?? []).map((item) => [item.line_no, item]),
    );
    const sourceHash = pending.value?.source_hash ?? "";
    return lineNumbers.map((lineNo) => {
      const item = byLine.get(lineNo);
      return {
        line_no: lineNo,
        raw: item?.raw ?? "",
        source_id: item?.source_id ?? "",
        source_hash: sourceHash,
        container_id: item?.wud_metadata?.id ?? "",
      };
    });
  }

  return {
    pending,
    pendingWudMetadataCheckedAt,
    updateTargets,
    retagTargets,
    retagChoices,
    retagTargetTags,
    retagPlan,
    retagPreviewJob: retagPreviewPoller.job,
    retagPreviewPolling: retagPreviewPoller.polling,
    retagPreviewError: retagPreviewPoller.error,
    retagGithubLatestFallback,
    releaseNotes,
    releaseNotification,
    securityScans,
    currentSecurityScans,
    securityScansCurrent,
    currentSecurityScanItems,
    securityScanJob,
    releaseChangelogs,
    selfUpdate,
    selfUpdatePlan,
    selfUpdateMessage,
    selfUpdateError,
    plan,
    pendingCleanup,
    pendingRemovalPlan,
    pendingRescan,
    applyJob,
    applyJobLog,
    rememberedApplyJobId,
    applyJobRecoveries,
    acknowledgeApplyJobRecovery,
    applyJobRecoveryResolved,
    dismissResolvedApplyJobRecovery,
    reviewApplyJobRecovery,
    loading,
    releaseNotesLoading,
    releaseNotesError,
    releaseNotificationLoading,
    releaseNotificationError,
    securityScansLoading,
    securityScansError,
    error,
    loadPending,
    refreshPendingMetadata,
    loadUpdateTargets,
    loadRetagTargets,
    setRetagGithubLatestFallback,
    refreshRetagGithubLatest,
    resetRetagChoices,
    setRetagChoice,
    setRetagChoicesForItems,
    setRetagTargetTag,
    retagChoiceRequests,
    createRetagPlan,
    applyRetagPlan,
    loadReleaseNotes,
    refreshReleaseNotes,
    previewReleaseNotifications,
    sendReleaseNotifications,
    clearReleaseNotification,
    loadSecurityScans,
    refreshSecurityScans,
    securityScanFor,
    releaseChangelogStateFor,
    releaseChangelogCanLoad,
    loadReleaseChangelog,
    loadSelfUpdate,
    planSelfUpdate,
    applySelfUpdate,
    createPlan,
    cleanupPending,
    createRemovalPlan,
    removeSelectedPending,
    rescanPending,
    clearPlan,
    createJob,
    applyPlan,
    setApplyJob,
    setApplyJobLog,
    setError,
    loadApplyJob,
    loadApplyJobLogFromRun,
    markApplyJobRecovery,
    rememberApplyJob,
    clearRememberedApplyJobId,
  };
});

function releaseChangelogKeyFor(note: ReleaseNoteInfo | null): string {
  const link = releaseChangelogLinkFor(note);
  return link ? releaseChangelogKey(link) : "";
}

function releaseChangelogCanLoad(note: ReleaseNoteInfo | null): boolean {
  const link = releaseChangelogLinkFor(note);
  return Boolean(link && releaseChangelogKey(link));
}

function releaseChangelogLinkFor(note: ReleaseNoteInfo | null): string {
  return note?.links.find((link) => link.kind === "github_release")?.url ?? "";
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function readRememberedApplyJobId(): string {
  const storage = sessionStorageAvailable();
  try {
    return storage?.getItem(APPLY_JOB_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function readApplyRecoveryStorage<T>(key: string, fallback: T): T {
  try {
    const value = JSON.parse(sessionStorageAvailable()?.getItem(key) ?? "null");
    if (key === "applyJobRun") {
      return (value?.jobId === readRememberedApplyJobId() &&
        Number.isSafeInteger(value?.runId) && value.runId > 0 ? value.runId : fallback) as T;
    }
    return (Array.isArray(value) ? value.filter((entry) =>
      entry && typeof entry.jobId === "string" && entry.jobId &&
      (entry.runId === null || (Number.isSafeInteger(entry.runId) && entry.runId > 0)) &&
      typeof entry.acknowledged === "boolean") : fallback) as T;
  } catch {
    return fallback;
  }
}

function writeApplyRecoveryStorage(key: string, value: unknown): void {
  try {
    sessionStorageAvailable()?.setItem(key, JSON.stringify(value));
  } catch {
    // Recovery remains available in memory if session storage is unavailable.
  }
}

function writeRememberedApplyJobId(jobId: string): void {
  const storage = sessionStorageAvailable();
  try {
    storage?.setItem(APPLY_JOB_STORAGE_KEY, jobId);
  } catch {
    // Remembering a transient job id is best-effort.
  }
}

function removeRememberedApplyJobId(): void {
  const storage = sessionStorageAvailable();
  try {
    storage?.removeItem(APPLY_JOB_STORAGE_KEY);
  } catch {
    // Remembering a transient job id is best-effort.
  }
}

function readRememberedRetagGithubLatestFallback(): boolean {
  const storage = localStorageAvailable();
  try {
    return storage?.getItem(RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function writeRememberedRetagGithubLatestFallback(enabled: boolean): void {
  const storage = localStorageAvailable();
  try {
    storage?.setItem(
      RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY,
      enabled ? "true" : "false",
    );
  } catch {
    // Remembering this harmless UI preference is best-effort.
  }
}

function localStorageAvailable(): Storage | null {
  try {
    return "localStorage" in globalThis ? globalThis.localStorage : null;
  } catch {
    return null;
  }
}

function sessionStorageAvailable(): Storage | null {
  try {
    return "sessionStorage" in globalThis ? globalThis.sessionStorage : null;
  } catch {
    return null;
  }
}
