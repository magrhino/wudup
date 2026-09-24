<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ShieldCheck } from "@lucide/vue";
import {
  NAlert,
  NButton,
  NFlex,
  NSkeleton,
  NTag,
} from "naive-ui";

import type { ReleaseNoteInfo, ReleaseNotificationSource } from "../api/client";
import CoreUpdateTourPanel from "../components/CoreUpdateTourPanel.vue";
import { useRouteRefresh } from "../components/app/routeRefresh";
import PendingApplyJobPanel from "../components/pending/PendingApplyJobPanel.vue";
import PendingApplyRecovery from "../components/pending/PendingApplyRecovery.vue";
import PendingCleanupModal from "../components/pending/PendingCleanupModal.vue";
import PendingFallbackQueue from "../components/pending/PendingFallbackQueue.vue";
import PendingPlanReviewModal from "../components/pending/PendingPlanReviewModal.vue";
import PendingRemovalModal from "../components/pending/PendingRemovalModal.vue";
import PendingReleaseNotificationModal from "../components/pending/PendingReleaseNotificationModal.vue";
import PendingSearchEmptyState from "../components/pending/PendingSearchEmptyState.vue";
import PendingSearchPanel from "../components/pending/PendingSearchPanel.vue";
import PendingSelectionToolbar from "../components/pending/PendingSelectionToolbar.vue";
import PendingStackSelection from "../components/pending/PendingStackSelection.vue";
import { useDataCardsBreakpoint } from "../responsive";
import { useAuthStore } from "../stores/auth";
import { useConnectionStore } from "../stores/connection";
import { useRunsStore } from "../stores/runs";
import { useSettingsStore } from "../stores/settings";
import { useUpdatesStore } from "../stores/updates";
import { displayDigest } from "../utils/digestProvenance";
import { runInBackground } from "../utils/promises";
import { securityScanSummaryDisplay } from "../utils/securityScans";
import {
  displayValue,
  releaseNoteReason,
  releaseNoteStatus as pendingReleaseNoteStatus,
  tagInputProps,
} from "./pending/pendingDisplay";
import { createPendingColumns } from "./pending/tableColumns";
import { pluralize } from "./pending/utils";
import {
  usePendingApplyJob,
  type PendingApplyJobPanelRef,
} from "./pending/usePendingApplyJob";
import { usePendingPlanActions } from "./pending/usePendingPlanActions";
import { usePendingPlanReviewState } from "./pending/usePendingPlanReviewState";
import { usePendingQueueState } from "./pending/usePendingQueueState";
import { usePendingSearchResultState } from "./pending/usePendingSearchResultState";
import { usePendingSearchState } from "./pending/usePendingSearchState";
import { usePendingSelectionState } from "./pending/usePendingSelectionState";

const updates = useUpdatesStore();
const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const connection = useConnectionStore();
const runs = useRunsStore();
const settings = useSettingsStore();
const isMobile = useDataCardsBreakpoint();
const applyJobPanelRef = ref<PendingApplyJobPanelRef | null>(null);
const pendingStatusMessage = ref("");
const pendingStatusError = ref("");

const pendingItems = computed(() => updates.pending?.items ?? []);
const {
  groupingReady,
  latestRun,
  pendingHeadingText,
  pendingLoaded,
  pendingLoadFailed,
  pendingLoading,
  pendingSourceDegraded,
  pendingSourceDisplay,
  pendingSourceFile,
  pendingSourceLabel,
  pendingSourceWarning,
  releaseChangelogFor,
  releaseNoteFor,
  riskCues,
  availableSelections,
  selectableLineNumbers,
  selectableSelections,
  selectAllLabel,
  snoozedCandidates,
  snoozedItems,
  stackGroups,
  stoppedItems,
  rawStackGroups,
  unmatchedItems,
} = usePendingQueueState();
const {
  clearPendingSearch,
  filteredPendingItems,
  filteredSnoozedCandidates,
  filteredSnoozedItems,
  filteredStackGroups,
  filteredStoppedItems,
  filteredUnmatchedItems,
  pendingSearchActive,
  pendingSearchEmpty,
  pendingSearchQuery,
  pendingSearchResultLabel,
  visibleSelections,
  visibleSelectableLineNumbers,
  visibleSelectableSelections,
  visibleSelectAllLabel,
} = usePendingSearchState({
  pendingItems,
  groupingReady,
  snoozedCandidates,
  snoozedItems,
  stoppedItems,
  selectableLineNumbers,
  selectableSelections,
  selectAllLabel,
  stackGroups,
  unmatchedItems,
  releaseChangelogFor,
  releaseNoteFor,
  releaseNoteReason,
  releaseNoteStatus,
  riskCues,
});
pendingSearchQuery.value = typeof route?.query?.search === "string" ? route.query.search : "";
watch(() => route?.query?.search, (value) => {
  pendingSearchQuery.value = typeof value === "string" ? value : "";
});

let clearPreflightHandler: () => void = () => undefined;
let loadPendingAndReleaseNotesHandler: (
  options?: {
    preserveCleanup?: boolean;
    freshAfterCurrent?: boolean;
  },
) => Promise<void> = async () => undefined;
const showReleaseNotificationModal = ref(false);
const releaseNotificationSource = ref<ReleaseNotificationSource | null>(null);
const securityScanRefreshReadOnlyMessage =
  "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server " +
  "to refresh candidate security scans.";
const securityScanRefreshMutationMessage =
  "Wait for the active WebUI mutation to finish before refreshing candidate " +
  "security scans.";
const PENDING_METADATA_REFRESH_INTERVAL_MS = 30_000;
const pendingMetadataRefreshInterval =
  ref<ReturnType<typeof globalThis.setInterval> | null>(null);
const pendingMetadataRefreshInFlight = ref(false);

const {
  clearSelection,
  lineNumbersHaveTagUpdates,
  selectAllVisible,
  selectedLineNumbers,
  selectedLineSet,
  selectedSelections,
  selectedSelectionKeySet,
  stackHasSelection,
  stackIndeterminate,
  stackSelected,
  tagOverrideErrorForLines,
  tagOverrideValue,
  tagOverridesForLines,
  toggleGroupedItem,
  toggleLine,
  toggleStack,
  updateCheckedRowKeys,
  updateDisabled,
  updateTagOverride,
} = usePendingSelectionState({
  pendingItems,
  selectableLineNumbers: visibleSelectableLineNumbers,
  selectableSelections: visibleSelectableSelections,
  availableSelections,
  onSelectionChanged: () => clearPreflightHandler(),
});

const columns = computed(() =>
  createPendingColumns({
    displayDigest,
    displayValue,
    releaseNoteFor,
    releaseNoteReason,
    releaseNoteStatus,
    riskCues,
    tagInputProps,
    tagOverrideValue,
    updateTagOverride,
  }),
);
const latestRunId = computed(() => latestRun.value?.id ?? null);
const showSetupLink = computed(
  () => settings.coreUpdateTour?.status === "in_progress",
);
const selectedHasTagUpdates = computed(() =>
  lineNumbersHaveTagUpdates(selectedLineNumbers.value),
);
const wudRescanUnavailableMessage = computed(() => {
  if (!updates.pending) {
    return "";
  }
  if (auth.session?.mutations_enabled === false) {
    return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to rescan WUD.";
  }
  const status = updates.pending.wud_api;
  if (!status) {
    return "WUD API status is unavailable.";
  }
  if (!status.available) {
    return status.detail || "WUD API is unavailable.";
  }
  if (status.state === "auth_required") {
    return status.detail || "WUD API requires authentication.";
  }
  if (status.state !== "ready") {
    return status.detail || "WUD API is not ready.";
  }
  return "";
});
const wudMetadataUnavailableMessage = computed(() => {
  const status = updates.pending?.wud_api;
  if (!status) {
    return "WUD API status is unavailable.";
  }
  if (!status.metadata_available) {
    return status.detail || "WUD API metadata is unavailable.";
  }
  return "";
});
const selectedWudRescanLineNumbers = computed(() => {
  const byLine = new Map(pendingItems.value.map((item) => [item.line_no, item]));
  return selectedLineNumbers.value.filter((lineNo) =>
    Boolean(byLine.get(lineNo)?.wud_metadata?.id),
  );
});
const globalRescanDisabled = computed(
  () => updates.loading || Boolean(wudRescanUnavailableMessage.value),
);
const selectedRescanVisible = computed(() => selectedLineNumbers.value.length > 0);
const selectedRescanDisabledMessage = computed(() => {
  if (!selectedLineNumbers.value.length) {
    return "";
  }
  if (wudRescanUnavailableMessage.value) {
    return wudRescanUnavailableMessage.value;
  }
  if (wudMetadataUnavailableMessage.value) {
    return wudMetadataUnavailableMessage.value;
  }
  if (!selectedWudRescanLineNumbers.value.length) {
    return "Selected entries do not have WUD container IDs.";
  }
  return "";
});
const selectedRescanDisabled = computed(
  () => updates.loading || Boolean(selectedRescanDisabledMessage.value),
);
const pendingRescanAlertType = computed(() =>
  updates.pendingRescan?.status !== "success" || updates.pendingRescan?.skipped.length
    ? "warning" : "info",
);
const pendingRescanMessage = computed(() => {
  const rescan = updates.pendingRescan;
  if (!rescan) {
    return "";
  }
  if (rescan.status === "blocked") {
    return "WUD scan request did not run. Review current WUD health and request details.";
  }
  const requested = rescan.scope === "all"
    ? "Full WUD scan requested."
    : `WUD rescan requested for ${pluralize(rescan.watched_count, "container")}.`;
  if (rescan.status === "partial") {
    const counts = rescan.scope === "selected"
      ? ` ${pluralize(rescan.requested_count, "selected entry", "selected entries")}; ${pluralize(rescan.watched_count, "container scan request")} sent.`
      : "";
    const skipped = rescan.skipped.length
      ? ` ${pluralize(rescan.skipped.length, "selected entry", "selected entries")} skipped.`
      : "";
    const prefix = rescan.scope === "all" ? `${requested} ` : "";
    return `${prefix}WUD reported a partial scan result.${counts}${skipped} Review request details.`;
  }
  if (rescan.skipped.length) {
    return `${requested} ${pluralize(rescan.skipped.length, "selected entry", "selected entries")} skipped. Review request details.`;
  }
  return `${requested} Waiting for fresh WUD results.`;
});
const releaseNotificationsDisabledReason = computed(() => {
  if (updates.releaseNotes?.notifications_enabled === false) {
    return (
      updates.releaseNotes.notifications_disabled_reason ||
      "Release-note notifications are disabled in Settings."
    );
  }
  return "";
});
const applyJobReleaseNotificationsVisible = computed(
  () => updates.applyJob?.status === "success" && Boolean(updates.applyJob.run_id),
);
const applyJobReleaseNotificationsDisabledMessage = computed(() => {
  if (!applyJobReleaseNotificationsVisible.value) {
    return "";
  }
  if (releaseNotificationsDisabledReason.value) {
    return releaseNotificationsDisabledReason.value;
  }
  if (updates.releaseNotificationLoading) {
    return "Release-note notification preview is loading.";
  }
  return "";
});
const applyJobReleaseNotificationsDisabled = computed(
  () =>
    !updates.applyJob?.run_id ||
    updates.releaseNotificationLoading ||
    Boolean(applyJobReleaseNotificationsDisabledMessage.value),
);
const releaseNotificationSendDisabledMessage = computed(() => {
  const response = updates.releaseNotification;
  if (!response) {
    return updates.releaseNotificationLoading
      ? ""
      : "Preview release-note notifications before sending.";
  }
  if (response.sent) {
    return "Release-note notifications were sent.";
  }
  if (!response.enabled) {
    return "Release-note notifications are disabled in Settings.";
  }
  if (!response.destination.configured) {
    return "Configure a Discord webhook in Settings or set DISCORD_WEBHOOK in the WebUI runtime.";
  }
  if (auth.session?.mutations_enabled === false) {
    return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to send notifications.";
  }
  if (!response.sendable_count) {
    if (response.skipped_count) {
      const skippedItems = response.items.filter((item) => item.skipped_reason);
      const duplicatesOnly = skippedItems.every(
        (item) => item.notification_status === "skipped_duplicate",
      );
      return duplicatesOnly
        ? "Duplicate notifications are skipped. Preview resend to send them again."
        : "Release-note notifications are skipped by the resend policy. Preview resend to review them.";
    }
    return "No release-note notifications are available to send.";
  }
  return "";
});
const releaseNotificationSendDisabled = computed(
  () =>
    updates.releaseNotificationLoading ||
    releaseNotificationSource.value === null ||
    Boolean(releaseNotificationSendDisabledMessage.value),
);
const securityScanRefreshVisible = computed(
  () => updates.securityScans?.scanning_enabled ?? false,
);
const securityScanSummary = computed(() => {
  if (!updates.securityScans && updates.securityScansError) {
    return { label: "Security scans unavailable", type: "warning" as const };
  }
  return securityScanSummaryDisplay({
    securityScans: updates.securityScans,
    securityScansCurrent: updates.securityScansCurrent,
    items: updates.currentSecurityScanItems,
  });
});
const securityScanSummaryLabel = computed(() => securityScanSummary.value.label);
const securityScanSummaryType = computed(() => securityScanSummary.value.type);

const {
  actionCommand,
  applyButtonLabel,
  applyDisabled,
  applyPreflight,
  applyPreflightAttentionChecks,
  applyPreflightCheckDetail,
  applyPreflightCheckLabel,
  applyPreflightCheckType,
  applyPreflightPassedChecks,
  applyPreflightPassedText,
  applyPlanPayload,
  applyReadinessStatusLabel,
  applyReadinessStatusType,
  applyReadinessSummary,
  applyVisible,
  batchSummaryLabel,
  cleanupAssistantActions,
  cleanupAssistantFindings,
  cleanupAssistantReasons,
  cleanupAvailable,
  cleanupButtonLabel,
  cleanupDisabled,
  cleanupDisabledMessage,
  cleanupItems,
  cleanupLineLabel,
  cleanupReviewSummary,
  approveDigestPinLabelRewrite,
  approveTagStreamLabelRewrite,
  clearUpdateIntent,
  digestPinLabelApprovalApproved,
  digestPinLabelApprovalIssues,
  digestPinLabelIssueProposedRegex,
  issueDetailString,
  issueHint,
  issueLabel,
  issueType,
  mutationDisabledMessage,
  mutationStateLabel,
  mutationStateType,
  pendingApplyTourDetail,
  pendingCleanupMessage,
  planActions,
  planAlertType,
  planDigestPinLabelRewrites,
  planDigestUnpinUpdates,
  planLines,
  planTagStreamUpdates,
  planMetadataWarning,
  planStatusLabel,
  preflightDigestPinNotice,
  preflightDigestUnpinNotice,
  preflightServiceImpactLabel,
  preflightSummary,
  preflightTagRewriteNotice,
  preflightTitle,
  removalButtonLabel,
  removalConfirmButtonLabel,
  removalDisabled,
  removalItems,
  removalLineLabel,
  removeSelectedDisabled,
  removeSelectedDisabledMessage,
  selectedTagOverrideError,
  selectedMetadataWarning,
  selectedUpdateContext,
  setUpdateIntent,
  chooseTagStream,
  staleDiagnosticDetail,
  staleDiagnosticLabel,
  unmatchedIssueSummary,
  unmatchedReviewCountLabel,
  unmatchedReviewSummary,
  updateSelectedDisabled,
  tagStreamDecisionIssues,
  tagStreamDecisionSelected,
  tagStreamLabelApprovalApproved,
  tagStreamLabelApprovalIssues,
  updateSelectedButtonLabel,
  visiblePlanIssues,
} = usePendingPlanReviewState({
  pendingSourceLabel,
  selectedLineNumbers,
  selectedSelections,
  selectedSelectionKeySet,
  stackGroups: rawStackGroups,
  tagOverrideErrorForLines,
  unmatchedItems,
});
const pendingHealthReadiness = computed(() => {
  if (selectedMetadataWarning.value) return selectedMetadataWarning.value;
  if (applyPreflight.value && (!applyPreflight.value.ok || !updates.plan?.can_apply)) {
    return `Selected plan blocked. ${applyReadinessSummary.value}`;
  }
  if (applyPreflight.value?.ok) {
    return "Advisory for this plan: its readiness checks passed. Review warnings before confirming.";
  }
  return "Update readiness is not yet checked. Review the selected plan to identify blockers.";
});
const pendingHealthCheckedAt = computed(() => {
  const value = updates.pendingWudMetadataCheckedAt || updates.pending?.wud_api.last_checked_at;
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString()
    : "Unavailable";
});
const {
  selectedHiddenCount,
  visibleUnmatchedIssueSummary,
  visibleUnmatchedReviewCountLabel,
  visibleUnmatchedReviewSummary,
} = usePendingSearchResultState({
  pendingSearchActive,
  visibleSelections,
  selectedSelections,
  filteredUnmatchedItems,
  unmatchedItems,
  unmatchedIssueSummary,
  unmatchedReviewCountLabel,
  unmatchedReviewSummary,
});

const {
  applyJobActive,
  applyJobAlertType,
  applyJobImpactLabel,
  applyJobLatestLogMessage,
  applyJobLiveLogExpanded,
  applyJobLiveLogToggleLabel,
  applyJobLiveLogVisible,
  applyJobLogEmptyMessage,
  applyJobLogText,
  applyJobLogTitle,
  applyJobLogWaiting,
  applyJobNowDescriptionIds,
  applyJobNowDetail,
  applyJobNowMessage,
  applyJobNowStatusLabel,
  applyJobNowTitle,
  applyJobPanelStatusLabel,
  applyJobProgressSteps,
  applyJobProgressSummary,
  applyJobSnapshot,
  applyJobStartedLabel,
  applyJobStatusMessage,
  applyJobSucceeded,
  applyJobTitle,
  applyJobUpdateLabel,
  applyJobVerification,
  createApplyJobSnapshot,
  focusApplyJobPanel,
  reconnectObservedApplyJob,
  subscribeApplyJob,
} = usePendingApplyJob({
  applyJobPanelRef,
  refreshAfterTerminalJob: () => refreshAfterTerminalApplyJob(),
});
const mutationInProgress = computed(
  () => updates.loading || applyJobActive.value,
);
const securityScanRefreshDisabled = computed(
  () =>
    updates.securityScansLoading ||
    auth.session?.mutations_enabled === false ||
    mutationInProgress.value,
);
const securityScanRefreshDisabledMessage = computed(() => {
  if (auth.session?.mutations_enabled === false) {
    return securityScanRefreshReadOnlyMessage;
  }
  if (mutationInProgress.value) {
    return securityScanRefreshMutationMessage;
  }
  return "";
});

const {
  clearPreflight,
  closeCleanupModal,
  closePreflightModal,
  closeRemovalModal,
  confirmApply,
  confirmCleanup,
  confirmSelectedRemoval,
  loadPendingAndReleaseNotes,
  openCleanupModal,
  retryPendingLoad,
  showCleanupModal,
  showPreflightModal,
  showRemovalModal,
  startSelectedRemoval,
  startSelectedUpdate,
  startStackUpdate,
} = usePendingPlanActions({
  applyDisabled,
  applyJobSnapshot,
  applyPlanPayload,
  cleanupAvailable,
  cleanupDisabled,
  clearUpdateIntent,
  createApplyJobSnapshot,
  focusApplyJobPanel,
  lineNumbersHaveTagUpdates,
  removalDisabled,
  removeSelectedDisabled,
  selectedLineNumbers,
  selectedSelections,
  selectedUpdateContext,
  stackGroups,
  setUpdateIntent,
  subscribeApplyJob,
  tagOverrideErrorForLines,
  tagOverridesForLines,
});

clearPreflightHandler = clearPreflight;
loadPendingAndReleaseNotesHandler = (options = {}) =>
  loadPendingAndReleaseNotes(options);
useRouteRefresh(() => updates.loadPending());

async function retryPendingStatus(): Promise<void> {
  pendingStatusMessage.value = "";
  pendingStatusError.value = "";
  await updates.loadPending().catch(() => undefined);
  if (updates.error) {
    pendingStatusError.value = `WUDup status refresh failed: ${updates.error}`;
    return;
  }
  pendingStatusMessage.value = "WUDup status refreshed.";
}

function viewAffectedContainers(): void {
  void router.push({ name: "issue-dump" });
}

async function refreshAfterTerminalApplyJob(): Promise<void> {
  await loadPendingAndReleaseNotesHandler({ freshAfterCurrent: true }).catch(
    () => undefined,
  );
}

function releaseNoteStatus(note: ReleaseNoteInfo | null): string {
  return pendingReleaseNoteStatus(note, updates.releaseNotesLoading);
}

async function rescanAllPending(): Promise<void> {
  if (globalRescanDisabled.value) {
    return;
  }
  clearPreflightHandler();
  await updates.rescanPending("all");
}

async function rescanSelectedPending(): Promise<void> {
  if (selectedRescanDisabled.value) {
    return;
  }
  clearPreflightHandler();
  await updates.rescanPending("selected", selectedLineNumbers.value);
}

async function previewApplyJobReleaseNotifications(): Promise<void> {
  const runId = updates.applyJob?.run_id;
  if (applyJobReleaseNotificationsDisabled.value || !runId) {
    return;
  }
  await previewReleaseNotifications({ run_id: runId });
}

async function previewReleaseNotifications(
  source: ReleaseNotificationSource,
): Promise<void> {
  releaseNotificationSource.value = source;
  showReleaseNotificationModal.value = true;
  await updates.previewReleaseNotifications(source).catch(() => undefined);
}

async function previewReleaseNotificationResend(): Promise<void> {
  if (releaseNotificationSource.value === null) {
    return;
  }
  const source = {
    ...releaseNotificationSource.value,
    resend: true,
  } as ReleaseNotificationSource;
  await previewReleaseNotifications(source);
}

function closeReleaseNotificationModal(): void {
  showReleaseNotificationModal.value = false;
  releaseNotificationSource.value = null;
  updates.clearReleaseNotification();
}

async function sendReleaseNotifications(): Promise<void> {
  if (releaseNotificationSendDisabled.value || releaseNotificationSource.value === null) {
    return;
  }
  await updates.sendReleaseNotifications(releaseNotificationSource.value).catch(() => undefined);
}

async function refreshSecurityScans(): Promise<void> {
  if (securityScanRefreshDisabled.value) {
    return;
  }
  await updates.refreshSecurityScans();
}

async function refreshPendingMetadataFromStatus(): Promise<void> {
  if (pendingMetadataRefreshInFlight.value) {
    return;
  }
  pendingMetadataRefreshInFlight.value = true;
  try {
    await connection.loadStatus({ silent: true });
    const sourceHash = connection.status?.source_hash ?? "";
    if (sourceHash && sourceHash !== (updates.pending?.source_hash ?? "")) {
      await updates.refreshPendingMetadata(selectedLineNumbers.value);
      await updates.loadReleaseNotes().catch(() => undefined);
      await updates.loadSecurityScans().catch(() => undefined);
      updates.refreshReleaseNotes().catch(() => undefined);
      return;
    }
    const checkedAt = connection.status?.wud_api.last_checked_at ?? "";
    if (checkedAt && checkedAt !== updates.pendingWudMetadataCheckedAt) {
      await updates.refreshPendingMetadata(selectedLineNumbers.value);
    }
  } finally {
    pendingMetadataRefreshInFlight.value = false;
  }
}

onMounted(() => {
  runInBackground(retryPendingLoad());
  runInBackground(settings.loadPendingSafetyCues());
  runInBackground(reconnectObservedApplyJob());
  pendingMetadataRefreshInterval.value = globalThis.setInterval(() => {
    runInBackground(refreshPendingMetadataFromStatus());
  }, PENDING_METADATA_REFRESH_INTERVAL_MS);
});

onBeforeUnmount(() => {
  if (pendingMetadataRefreshInterval.value !== null) {
    globalThis.clearInterval(pendingMetadataRefreshInterval.value);
    pendingMetadataRefreshInterval.value = null;
  }
});
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="(updates.error || runs.error)" type="error">
      {{ (updates.error || runs.error) }}
    </n-alert>
    <n-alert v-if="settings.pendingSafetyCueError" type="warning">
      Pending safety cues are unavailable: {{ settings.pendingSafetyCueError }}
    </n-alert>
    <n-alert v-if="updates.pending && !updates.pending.exists" type="warning">
      {{ updates.pending.source_file }} is missing.
    </n-alert>
    <n-alert v-if="updates.releaseNotesError" type="warning">
      Release-note metadata is unavailable: {{ updates.releaseNotesError }}
    </n-alert>
    <n-alert v-if="updates.releaseNotificationError" type="warning">
      Release-note notification is unavailable: {{ updates.releaseNotificationError }}
    </n-alert>
    <n-alert v-if="updates.securityScansError" type="warning">
      Candidate security scan metadata is unavailable: {{ updates.securityScansError }}
    </n-alert>
    <n-alert v-if="pendingRescanMessage" :type="pendingRescanAlertType">
      {{ pendingRescanMessage }}
      <n-flex
        v-if="updates.pendingRescan?.audit_run_id"
        inline
        class="inline-actions recovery-actions"
        align="center"
        :size="8"
      >
        <RouterLink
          class="text-link"
          :to="{ name: 'run-detail', params: { id: updates.pendingRescan.audit_run_id } }"
        >
          Request details
        </RouterLink>
      </n-flex>
    </n-alert>
    <n-alert v-if="pendingCleanupMessage" type="success">
      {{ pendingCleanupMessage }}
      <n-flex inline class="inline-actions recovery-actions" align="center" :size="8">
        <RouterLink
          class="text-link"
          :to="{ name: 'run-detail', params: { id: updates.pendingCleanup?.audit_run_id } }"
        >
          Details
        </RouterLink>
      </n-flex>
    </n-alert>
    <PendingApplyRecovery
      v-for="notice in updates.applyJobRecoveries"
      :key="notice.jobId"
      :job-id="notice.jobId"
      :run-id="notice.runId"
      :acknowledged="notice.acknowledged"
    />

    <PendingApplyJobPanel
      v-if="updates.applyJob"
      ref="applyJobPanelRef"
      v-model:live-log-expanded="applyJobLiveLogExpanded"
      :active="applyJobActive"
      :alert-type="applyJobAlertType"
      :impact-label="applyJobImpactLabel"
      :job="updates.applyJob"
      :latest-log-message="applyJobLatestLogMessage"
      :live-log-toggle-label="applyJobLiveLogToggleLabel"
      :live-log-visible="applyJobLiveLogVisible"
      :log="updates.applyJobLog"
      :log-empty-message="applyJobLogEmptyMessage"
      :log-text="applyJobLogText"
      :log-title="applyJobLogTitle"
      :log-waiting="applyJobLogWaiting"
      :now-description-ids="applyJobNowDescriptionIds"
      :now-detail="applyJobNowDetail"
      :now-message="applyJobNowMessage"
      :now-status-label="applyJobNowStatusLabel"
      :now-title="applyJobNowTitle"
      :panel-status-label="applyJobPanelStatusLabel"
      :progress-steps="applyJobProgressSteps"
      :progress-summary="applyJobProgressSummary"
      :release-notifications-disabled="applyJobReleaseNotificationsDisabled"
      :release-notifications-disabled-message="applyJobReleaseNotificationsDisabledMessage"
      :release-notifications-loading="updates.releaseNotificationLoading"
      :release-notifications-visible="applyJobReleaseNotificationsVisible"
      :snapshot="applyJobSnapshot"
      :started-label="applyJobStartedLabel"
      :status-message="applyJobStatusMessage"
      :succeeded="applyJobSucceeded"
      :title="applyJobTitle"
      :update-label="applyJobUpdateLabel"
      :verification="applyJobVerification"
      @preview-release-notes="previewApplyJobReleaseNotifications"
    />

    <div class="section-heading pending-heading">
      <div>
        <p class="eyebrow value-eyebrow pending-source" :title="pendingSourceFile">
          {{ pendingSourceDisplay }}
        </p>
        <h2>{{ pendingHeadingText }}</h2>
      </div>
      <n-flex align="center" :size="8">
        <n-tag size="small" :type="mutationStateType">{{ mutationStateLabel }}</n-tag>
      </n-flex>
    </div>

    <n-alert
      v-if="pendingSourceDegraded && pendingSourceWarning"
      type="warning"
      role="status"
    >
      <details open>
        <summary>
          <strong>Current WUD health: needs attention</strong>
          <span> · Last checked: {{ pendingHealthCheckedAt }}</span>
        </summary>
        <p>{{ pendingSourceWarning }}</p>
        <p>{{ pendingHealthReadiness }}</p>
        <n-flex
          inline
          class="inline-actions recovery-actions"
          align="center"
          :size="8"
        >
          <n-button size="small" type="primary" @click="viewAffectedContainers">
            View affected containers
          </n-button>
          <n-button
            size="small"
            secondary
            :loading="updates.loading"
            title="Reads current WUD status without triggering a rescan."
            @click="retryPendingStatus"
          >
            Refresh WUDup status
          </n-button>
        </n-flex>
      </details>
    </n-alert>
    <n-alert
      v-if="pendingStatusMessage"
      type="success"
      :show-icon="false"
      role="status"
    >
      {{ pendingStatusMessage }}
    </n-alert>
    <n-alert
      v-if="pendingStatusError"
      type="error"
      :show-icon="false"
      role="alert"
    >
      {{ pendingStatusError }}
    </n-alert>

    <CoreUpdateTourPanel
      step="pending_select"
      title="Select the update scope"
      detail="Choose one stack or selected lines before reviewing. Stack groups are the safest default because they keep related services together."
      next-label="Show preflight guidance"
      next-step="pending_preflight"
    >
      <div class="core-tour-facts">
        <span v-if="pendingLoaded">
          {{
            pendingSearchActive
              ? `${pluralize(filteredStackGroups.length, "stack")} matched`
              : pluralize(filteredStackGroups.length, "stack")
          }}
        </span>
        <span v-else>Loading stack matches</span>
        <span v-if="pendingLoaded">{{ visibleUnmatchedReviewCountLabel }}</span>
        <span v-else>Waiting for pending file</span>
        <span>{{ mutationStateLabel }}</span>
      </div>
    </CoreUpdateTourPanel>

    <CoreUpdateTourPanel
      step="pending_preflight"
      title="Review before anything changes"
      detail="Review the plan to see affected services, image targets, tag rewrites, skipped lines, and any blocking issues. Creating a plan does not pull, restart, or edit Docker state."
      next-label="Continue to apply guidance"
      next-step="pending_apply"
      :show="!showPreflightModal"
    />

    <CoreUpdateTourPanel
      step="pending_apply"
      title="Apply only after the plan is clear"
      :detail="pendingApplyTourDetail"
      next-label="Open History"
      next-step="runs_history"
      next-to="/runs"
    />

    <PendingSearchPanel
      v-if="pendingLoaded"
      v-model:query="pendingSearchQuery"
      :active="pendingSearchActive"
      :result-label="pendingSearchResultLabel"
      @clear="clearPendingSearch"
    />

    <PendingSelectionToolbar
      :batch-summary-label="batchSummaryLabel"
      :global-rescan-disabled="globalRescanDisabled"
      :global-rescan-disabled-message="wudRescanUnavailableMessage"
      :grouping-ready="groupingReady"
      :has-selected-tag-updates="selectedHasTagUpdates"
      :loading="updates.loading"
      :pending-loaded="pendingLoaded"
      :removal-button-label="removalButtonLabel"
      :remove-selected-disabled="removeSelectedDisabled"
      :remove-selected-disabled-message="removeSelectedDisabledMessage"
      :selectable-count="visibleSelectableSelections.length"
      :select-all-label="visibleSelectAllLabel"
      :selected-count="selectedSelections.length"
      :selected-hidden-count="selectedHiddenCount"
      :selected-metadata-warning="selectedMetadataWarning"
      :selected-rescan-disabled="selectedRescanDisabled"
      :selected-rescan-disabled-message="selectedRescanDisabledMessage"
      :selected-rescan-visible="selectedRescanVisible"
      :snoozed-count="
        filteredSnoozedItems.length + filteredSnoozedCandidates.length
      "
      :stack-count="filteredStackGroups.length"
      :stopped-count="filteredStoppedItems.length"
      :search-active="pendingSearchActive"
      :unmatched-review-count-label="visibleUnmatchedReviewCountLabel"
      :update-selected-disabled="updateSelectedDisabled"
      :update-selected-button-label="updateSelectedButtonLabel"
      @clear-selection="clearSelection"
      @rescan-all="rescanAllPending"
      @rescan-selected="rescanSelectedPending"
      @select-all="selectAllVisible"
      @start-removal="startSelectedRemoval"
      @start-update="startSelectedUpdate"
    >
      <template #tools>
        <n-flex align="center" :size="8">
          <n-tag size="small" :type="securityScanSummaryType">
            {{ securityScanSummaryLabel }}
          </n-tag>
          <n-button
            v-if="securityScanRefreshVisible"
            size="small"
            secondary
            :loading="updates.securityScansLoading"
            :disabled="securityScanRefreshDisabled"
            :title="securityScanRefreshDisabledMessage || undefined"
            @click="refreshSecurityScans"
          >
            <template #icon>
              <ShieldCheck :size="16" aria-hidden="true" />
            </template>
            Refresh security scans
          </n-button>
        </n-flex>
        <p>Release advisory evidence and candidate-image scans are separate checks. Neither guarantees an update is safe.</p>
      </template>
    </PendingSelectionToolbar>

    <n-alert
      v-if="selectedTagOverrideError"
      type="warning"
    >
      {{ selectedTagOverrideError }}
    </n-alert>

    <PendingSearchEmptyState
      v-if="pendingSearchEmpty"
      :query="pendingSearchQuery"
      @clear="clearPendingSearch"
    />

    <template v-if="groupingReady">
      <PendingStackSelection
        v-if="!pendingSearchEmpty"
        :latest-run-id="latestRunId"
        :loading="updates.loading"
        :pending-source-label="pendingSourceLabel"
        :release-note-for="releaseNoteFor"
        :release-note-reason="releaseNoteReason"
        :release-note-status="releaseNoteStatus"
        :risk-cues="riskCues"
        :security-scan-for="updates.securityScanFor"
        :selected-selection-key-set="selectedSelectionKeySet"
        :show-setup-link="showSetupLink"
        :snoozed-candidates="filteredSnoozedCandidates"
        :snoozed-items="filteredSnoozedItems"
        :stopped-items="filteredStoppedItems"
        :stack-groups="filteredStackGroups"
        :stack-has-selection="stackHasSelection"
        :stack-indeterminate="stackIndeterminate"
        :stack-selected="stackSelected"
        :stale-diagnostic-detail="staleDiagnosticDetail"
        :stale-diagnostic-label="staleDiagnosticLabel"
        :tag-input-props="tagInputProps"
        :tag-override-value="tagOverrideValue"
        :unmatched-issue-summary="visibleUnmatchedIssueSummary"
        :unmatched-items="filteredUnmatchedItems"
        :unmatched-review-summary="visibleUnmatchedReviewSummary"
        :update-disabled="updateDisabled"
        @preview-stack="startStackUpdate"
        @toggle-item="toggleGroupedItem"
        @toggle-stack="toggleStack"
        @update-tag="updateTagOverride"
      />
    </template>

    <template v-else-if="updates.pending">
      <PendingFallbackQueue
        v-if="!pendingSearchEmpty"
        :columns="columns"
        :is-mobile="isMobile"
        :items="filteredPendingItems"
        :latest-run-id="latestRunId"
        :loading="updates.loading"
        :pending-source-label="pendingSourceLabel"
        :release-note-for="releaseNoteFor"
        :release-note-reason="releaseNoteReason"
        :release-note-status="releaseNoteStatus"
        :risk-cues="riskCues"
        :selected-line-numbers="selectedLineNumbers"
        :selected-line-set="selectedLineSet"
        :show-setup-link="showSetupLink"
        :tag-input-props="tagInputProps"
        :tag-override-value="tagOverrideValue"
        @toggle-line="toggleLine"
        @update-checked-row-keys="updateCheckedRowKeys"
        @update-tag="updateTagOverride"
      />
    </template>

    <output
      v-else-if="pendingLoading"
      class="pending-loading-state"
      aria-live="polite"
      aria-label="Loading pending updates"
    >
      <n-skeleton aria-hidden="true" height="48px" />
      <n-skeleton aria-hidden="true" height="48px" />
      <n-skeleton aria-hidden="true" height="48px" />
    </output>

    <div
      v-else-if="pendingLoadFailed"
      class="empty-state pending-error-state"
      role="alert"
      aria-live="assertive"
    >
      <strong>Pending updates did not load</strong>
      <span>Check the WebUI API connection, then try again.</span>
      <n-button size="small" secondary :loading="updates.loading" @click="retryPendingLoad">
        Retry pending load
      </n-button>
    </div>

    <PendingPlanReviewModal
      v-if="updates.plan"
      :show="showPreflightModal"
      :plan="updates.plan"
      :action-command="actionCommand"
      :apply-button-label="applyButtonLabel"
      :apply-disabled="applyDisabled"
      :apply-preflight="applyPreflight"
      :apply-preflight-attention-checks="applyPreflightAttentionChecks"
      :apply-preflight-check-detail="applyPreflightCheckDetail"
      :apply-preflight-check-label="applyPreflightCheckLabel"
      :apply-preflight-check-type="applyPreflightCheckType"
      :apply-preflight-passed-checks="applyPreflightPassedChecks"
      :apply-preflight-passed-text="applyPreflightPassedText"
      :apply-readiness-status-label="applyReadinessStatusLabel"
      :apply-readiness-status-type="applyReadinessStatusType"
      :apply-readiness-summary="applyReadinessSummary"
      :apply-visible="applyVisible"
      :cleanup-available="cleanupAvailable"
      :cleanup-button-label="cleanupButtonLabel"
      :cleanup-disabled="cleanupDisabled"
      :cleanup-disabled-message="cleanupDisabledMessage"
      :cleanup-items="cleanupItems"
      :cleanup-review-summary="cleanupReviewSummary"
      :digest-pin-label-approval-approved="digestPinLabelApprovalApproved"
      :digest-pin-label-approval-issues="digestPinLabelApprovalIssues"
      :digest-pin-label-issue-proposed-regex="digestPinLabelIssueProposedRegex"
      :tag-stream-decision-issues="tagStreamDecisionIssues"
      :tag-stream-decision-selected="tagStreamDecisionSelected"
      :tag-stream-label-approval-approved="tagStreamLabelApprovalApproved"
      :tag-stream-label-approval-issues="tagStreamLabelApprovalIssues"
      :issue-detail-string="issueDetailString"
      :issue-hint="issueHint"
      :issue-label="issueLabel"
      :issue-type="issueType"
      :loading="updates.loading"
      :mutation-disabled-message="mutationDisabledMessage"
      :plan-actions="planActions"
      :plan-alert-type="planAlertType"
      :plan-digest-pin-label-rewrites="planDigestPinLabelRewrites"
      :plan-digest-unpin-updates="planDigestUnpinUpdates"
      :plan-lines="planLines"
      :release-notes="updates.releaseNotes?.items ?? []"
      :release-notes-loading="updates.releaseNotesLoading"
      :release-notes-error="updates.releaseNotesError"
      :security-scans="updates.currentSecurityScanItems"
      :plan-tag-stream-updates="planTagStreamUpdates"
      :plan-metadata-warning="planMetadataWarning"
      :plan-status-label="planStatusLabel"
      :preflight-digest-pin-notice="preflightDigestPinNotice"
      :preflight-digest-unpin-notice="preflightDigestUnpinNotice"
      :preflight-service-impact-label="preflightServiceImpactLabel"
      :preflight-summary="preflightSummary"
      :preflight-tag-rewrite-notice="preflightTagRewriteNotice"
      :preflight-title="preflightTitle"
      :stale-diagnostic-detail="staleDiagnosticDetail"
      :stale-diagnostic-label="staleDiagnosticLabel"
      :visible-plan-issues="visiblePlanIssues"
      @apply="confirmApply"
      @approve-digest-pin-label-rewrite="approveDigestPinLabelRewrite"
      @approve-tag-stream-label-rewrite="approveTagStreamLabelRewrite"
      @choose-tag-stream="chooseTagStream"
      @close="closePreflightModal"
      @open-cleanup="openCleanupModal"
    />

    <PendingCleanupModal
      v-if="updates.plan && cleanupAvailable"
      :show="showCleanupModal"
      :assistant-actions="cleanupAssistantActions"
      :assistant-findings="cleanupAssistantFindings"
      :assistant-reasons="cleanupAssistantReasons"
      :cleanup-button-label="cleanupButtonLabel"
      :cleanup-disabled="cleanupDisabled"
      :cleanup-items="cleanupItems"
      :cleanup-line-label="cleanupLineLabel"
      :loading="updates.loading"
      :pending-source-label="pendingSourceLabel"
      @close="closeCleanupModal"
      @confirm="confirmCleanup"
    />

    <PendingRemovalModal
      v-if="updates.pendingRemovalPlan"
      :show="showRemovalModal"
      :loading="updates.loading"
      :pending-source-label="pendingSourceLabel"
      :removal-confirm-button-label="removalConfirmButtonLabel"
      :removal-disabled="removalDisabled"
      :removal-items="removalItems"
      :removal-line-label="removalLineLabel"
      @close="closeRemovalModal"
      @confirm="confirmSelectedRemoval"
    />

    <PendingReleaseNotificationModal
      :show="showReleaseNotificationModal"
      :response="updates.releaseNotification"
      :error="updates.releaseNotificationError"
      :loading="updates.releaseNotificationLoading"
      :send-disabled="releaseNotificationSendDisabled"
      :send-disabled-message="releaseNotificationSendDisabledMessage"
      @close="closeReleaseNotificationModal"
      @resend-preview="previewReleaseNotificationResend"
      @send="sendReleaseNotifications"
    />
  </section>
</template>

<style scoped>
.pending-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
}

.pending-loading-state {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  box-shadow: var(--shadow-panel-lift);
}

.pending-error-state {
  gap: 8px;
  padding: 18px;
  text-align: center;
}

.recovery-actions {
  flex-wrap: wrap;
  margin-top: 8px;
}
</style>
