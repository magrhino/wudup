<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  NAlert,
  NButton,
  NFlex,
  NSkeleton,
  NTag,
} from "naive-ui";

import type { ReleaseNoteInfo } from "../api/client";
import CoreUpdateTourPanel from "../components/CoreUpdateTourPanel.vue";
import { useRouteRefresh } from "../components/app/routeRefresh";
import PendingApplyJobPanel from "../components/pending/PendingApplyJobPanel.vue";
import PendingApplyRecovery from "../components/pending/PendingApplyRecovery.vue";
import PendingFallbackQueue from "../components/pending/PendingFallbackQueue.vue";
import PendingPlanReviewModal from "../components/pending/PendingPlanReviewModal.vue";
import PendingReleaseNotificationModal from "../components/pending/PendingReleaseNotificationModal.vue";
import PendingSearchEmptyState from "../components/pending/PendingSearchEmptyState.vue";
import PendingSearchPanel from "../components/pending/PendingSearchPanel.vue";
import PendingCandidateScanControls from "../components/pending/PendingCandidateScanControls.vue";
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
import {
  staleDiagnosticDetail,
  staleDiagnosticLabel,
} from "./pending/planReviewFormatters";
import { createPendingColumns } from "./pending/tableColumns";
import { pluralize } from "./pending/utils";
import {
  usePendingApplyJob,
  type PendingApplyJobPanelRef,
} from "./pending/usePendingApplyJob";
import { usePendingPlanActions } from "./pending/usePendingPlanActions";
import { usePendingPlanReviewState } from "./pending/usePendingPlanReviewState";
import { usePendingQueueState } from "./pending/usePendingQueueState";
import { usePendingReleaseNotifications } from "./pending/usePendingReleaseNotifications";
import { usePendingRescan } from "./pending/usePendingRescan";
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
    freshAfterCurrent?: boolean;
  },
) => Promise<void> = async () => undefined;
const securityScanRefreshReadOnlyMessage =
  "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server " +
  "to scan candidate images.";
const securityScanRefreshMutationMessage =
  "Wait for the active WebUI mutation to finish before scanning candidate " +
  "images.";
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
const {
  globalRescanDisabled,
  pendingRescanAlertType,
  pendingRescanMessage,
  rescanAllPending,
  rescanSelectedPending,
  selectedRescanDisabled,
  selectedRescanDisabledMessage,
  selectedRescanVisible,
  wudRescanUnavailableMessage,
} = usePendingRescan({
  pendingItems,
  selectedLineNumbers,
  clearPreflight: () => clearPreflightHandler(),
});
const {
  applyJobReleaseNotificationsDisabled,
  applyJobReleaseNotificationsDisabledMessage,
  applyJobReleaseNotificationsVisible,
  closeReleaseNotificationModal,
  previewApplyJobReleaseNotifications,
  previewReleaseNotificationResend,
  releaseNotificationSendDisabled,
  releaseNotificationSendDisabledMessage,
  sendReleaseNotifications,
  showReleaseNotificationModal,
} = usePendingReleaseNotifications();
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
const securityScanProgressLabel = computed(() => {
  const job = updates.securityScanJob;
  if (!updates.securityScansLoading || !job || (job.status !== "queued" && job.status !== "running")) {
    return "";
  }
  return job.total_count
    ? `Scanning ${job.completed_count} of ${job.total_count} candidate images…`
    : "Starting candidate image scans…";
});
const securityScanSummaryType = computed(() => securityScanSummary.value.type);

const {
  applyButtonLabel,
  applyDisabled,
  applyPreflight,
  applyPreflightAttentionChecks,
  applyPreflightCheckDetail,
  applyPreflightPassedChecks,
  applyPreflightPassedText,
  applyPlanPayload,
  applyReadinessStatusLabel,
  applyReadinessStatusType,
  applyReadinessSummary,
  applyVisible,
  batchSummaryLabel,
  cleanupAvailable,
  cleanupItems,
  cleanupReviewSummary,
  approveDigestPinLabelRewrite,
  approveTagStreamLabelRewrite,
  clearUpdateIntent,
  digestPinLabelApprovalApproved,
  digestPinLabelApprovalIssues,
  mutationDisabledMessage,
  applyBlockedReason,
  mutationStateLabel,
  mutationStateType,
  pendingApplyTourDetail,
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
  preflightSummary,
  preflightTagRewriteNotice,
  preflightTitle,
  selectedTagOverrideError,
  selectedMetadataWarning,
  selectedUpdateContext,
  setUpdateIntent,
  chooseTagStream,
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
  closePreflightModal,
  confirmApply,
  loadPendingAndReleaseNotes,
  retryPendingLoad,
  showPreflightModal,
  startSelectedUpdate,
  startStackUpdate,
} = usePendingPlanActions({
  applyDisabled,
  applyJobSnapshot,
  applyPlanPayload,
  clearUpdateIntent,
  createApplyJobSnapshot,
  focusApplyJobPanel,
  lineNumbersHaveTagUpdates,
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
        <p class="eyebrow value-eyebrow pending-source">
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
        <span v-else>Waiting for pending updates</span>
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
      @start-update="startSelectedUpdate"
    >
      <template #scan>
        <PendingCandidateScanControls
          :can-scan="securityScanRefreshVisible"
          :disabled="securityScanRefreshDisabled"
          :disabled-message="securityScanRefreshDisabledMessage"
          :error="updates.securityScansError"
          :loading="updates.securityScansLoading"
          :progress-label="securityScanProgressLabel"
          :summary-label="securityScanSummaryLabel"
          :summary-type="securityScanSummaryType"
          @scan="refreshSecurityScans"
        />
      </template>
      <template #tools>
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
      :apply-button-label="applyButtonLabel"
      :apply-disabled="applyDisabled"
      :apply-preflight="applyPreflight"
      :apply-preflight-attention-checks="applyPreflightAttentionChecks"
      :apply-preflight-check-detail="applyPreflightCheckDetail"
      :apply-preflight-passed-checks="applyPreflightPassedChecks"
      :apply-preflight-passed-text="applyPreflightPassedText"
      :apply-readiness-status-label="applyReadinessStatusLabel"
      :apply-readiness-status-type="applyReadinessStatusType"
      :apply-readiness-summary="applyReadinessSummary"
      :apply-visible="applyVisible"
      :cleanup-available="cleanupAvailable"
      :cleanup-items="cleanupItems"
      :cleanup-review-summary="cleanupReviewSummary"
      :digest-pin-label-approval-approved="digestPinLabelApprovalApproved"
      :digest-pin-label-approval-issues="digestPinLabelApprovalIssues"
      :tag-stream-decision-issues="tagStreamDecisionIssues"
      :tag-stream-decision-selected="tagStreamDecisionSelected"
      :tag-stream-label-approval-approved="tagStreamLabelApprovalApproved"
      :tag-stream-label-approval-issues="tagStreamLabelApprovalIssues"
      :loading="updates.loading"
      :mutation-disabled-message="mutationDisabledMessage"
      :apply-blocked-reason="applyBlockedReason"
      :plan-actions="planActions"
      :plan-alert-type="planAlertType"
      :plan-digest-pin-label-rewrites="planDigestPinLabelRewrites"
      :plan-digest-unpin-updates="planDigestUnpinUpdates"
      :plan-lines="planLines"
      :release-notes="updates.releaseNotes?.items ?? []"
      :release-notes-loading="updates.releaseNotesLoading"
      :release-notes-error="updates.releaseNotesError"
      :security-scans="updates.currentSecurityScanItems"
      :security-scans-loading="updates.securityScansLoading"
      :security-scans-error="updates.securityScansError"
      :security-scans-disabled="updates.securityScans?.scanning_enabled === false && !updates.securityScansError"
      :plan-tag-stream-updates="planTagStreamUpdates"
      :plan-metadata-warning="planMetadataWarning"
      :plan-status-label="planStatusLabel"
      :preflight-digest-pin-notice="preflightDigestPinNotice"
      :preflight-digest-unpin-notice="preflightDigestUnpinNotice"
      :preflight-summary="preflightSummary"
      :preflight-tag-rewrite-notice="preflightTagRewriteNotice"
      :preflight-title="preflightTitle"
      :visible-plan-issues="visiblePlanIssues"
      @apply="confirmApply"
      @approve-digest-pin-label-rewrite="approveDigestPinLabelRewrite"
      @approve-tag-stream-label-rewrite="approveTagStreamLabelRewrite"
      @choose-tag-stream="chooseTagStream"
      @close="closePreflightModal"
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
