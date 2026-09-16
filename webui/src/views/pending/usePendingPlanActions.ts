import { ref, type ComputedRef, type Ref } from "vue";

import type {
  PendingStackGroup,
  PlanSelectionRequest,
  TagOverrideRequest,
} from "../../api/client";
import { useRunsStore } from "../../stores/runs";
import { useUpdatesStore } from "../../stores/updates";
import { uniqueSorted } from "./pendingDisplay";
import type { ApplyJobPlanSnapshot } from "./usePendingApplyJob";
import type {
  PendingApplyPlanPayload,
  PendingUpdateIntent,
} from "./usePendingPlanReviewState";
import {
  pendingSelectionsForGroup,
  uniqueSelections,
} from "./usePendingSelectionState";

export type UsePendingPlanActionsOptions = {
  applyDisabled: ComputedRef<boolean>;
  applyJobSnapshot: Ref<ApplyJobPlanSnapshot | null>;
  applyPlanPayload: (fallback: {
    allowTagUpdates: boolean;
    tagOverrides: TagOverrideRequest[];
  }) => PendingApplyPlanPayload;
  cleanupAvailable: ComputedRef<boolean>;
  cleanupDisabled: ComputedRef<boolean>;
  clearUpdateIntent: () => void;
  createApplyJobSnapshot: () => ApplyJobPlanSnapshot | null;
  focusApplyJobPanel: () => Promise<void>;
  lineNumbersHaveTagUpdates: (lineNumbers: number[]) => boolean;
  removalDisabled: ComputedRef<boolean>;
  removeSelectedDisabled: ComputedRef<boolean>;
  selectedLineNumbers: Ref<number[]>;
  selectedSelections: Ref<PlanSelectionRequest[]>;
  selectedUpdateContext: ComputedRef<string>;
  stackGroups: ComputedRef<PendingStackGroup[]>;
  setUpdateIntent: (intent: PendingUpdateIntent) => void;
  subscribeApplyJob: (jobId: string) => void;
  tagOverrideErrorForLines: (lineNumbers: number[]) => string;
  tagOverridesForLines: (lineNumbers: number[]) => TagOverrideRequest[];
};

export function usePendingPlanActions(options: UsePendingPlanActionsOptions) {
  const updates = useUpdatesStore();
  const runs = useRunsStore();
  const showPreflightModal = ref(false);
  const showCleanupModal = ref(false);
  const showRemovalModal = ref(false);

  function clearPreflight(): void {
    showPreflightModal.value = false;
    showCleanupModal.value = false;
    showRemovalModal.value = false;
    options.clearUpdateIntent();
    updates.clearPlan();
  }

  async function startSelectedUpdate(): Promise<void> {
    await startUpdateFlow({
      title: "Review selected plan",
      contextLabel: options.selectedUpdateContext.value,
      selections: options.selectedSelections.value,
    });
  }

  async function startStackUpdate(group: PendingStackGroup): Promise<void> {
    const fullGroup =
      options.stackGroups.value.find(
        (candidate) =>
          candidate.directory === group.directory &&
          candidate.compose_file === group.compose_file &&
          candidate.project_directory === group.project_directory,
      ) ?? group;
    await startUpdateFlow({
      title: `Review ${group.name} plan`,
      contextLabel: group.name,
      selections: pendingSelectionsForGroup(fullGroup),
    });
  }

  async function startUpdateFlow(input: {
    title: string;
    contextLabel: string;
    selections: PlanSelectionRequest[];
  }): Promise<void> {
    const requestedSelections = uniqueSelections(input.selections);
    const metadataStatusByLine = new Map(
      (updates.pending?.items ?? []).map((item) => [
        item.line_no,
        item.metadata_status ?? "fresh",
      ]),
    );
    const selections = requestedSelections.filter(
      (selection) =>
        (metadataStatusByLine.get(selection.line_no) ?? "fresh") === "fresh",
    );
    const blockedMetadataCount = requestedSelections.length - selections.length;
    const lineNumbers = uniqueSorted(
      selections.map((selection) => selection.line_no),
    );
    if (lineNumbers.length === 0 || updates.loading) {
      return;
    }
    options.selectedSelections.value = requestedSelections;
    const validationError = options.tagOverrideErrorForLines(lineNumbers);
    if (validationError) {
      clearPreflight();
      return;
    }

    const intent: PendingUpdateIntent = {
      title: input.title,
      contextLabel: input.contextLabel,
      lineNumbers,
      selections,
      allowTagUpdates: options.lineNumbersHaveTagUpdates(lineNumbers),
      tagOverrides: options.tagOverridesForLines(lineNumbers),
      digestPinLabelRewriteApprovals: [],
      tagStreamDecisions: [],
      tagStreamLabelRewriteApprovals: [],
      blockedMetadataCount,
    };
    options.setUpdateIntent(intent);
    try {
      await updates.createPlan(
        intent.lineNumbers,
        intent.allowTagUpdates,
        intent.tagOverrides,
        intent.digestPinLabelRewriteApprovals,
        intent.selections,
      );
    } catch {
      showPreflightModal.value = false;
      options.clearUpdateIntent();
      return;
    }
    if (updates.plan) {
      showPreflightModal.value = true;
    }
  }

  function closePreflightModal(): void {
    clearPreflight();
  }

  function openCleanupModal(): void {
    if (!options.cleanupAvailable.value) {
      return;
    }
    showCleanupModal.value = true;
  }

  function closeCleanupModal(): void {
    showCleanupModal.value = false;
  }

  async function startSelectedRemoval(): Promise<void> {
    const lineNumbers = uniqueSorted(options.selectedLineNumbers.value);
    if (lineNumbers.length === 0 || options.removeSelectedDisabled.value) {
      return;
    }
    try {
      await updates.createRemovalPlan(lineNumbers);
    } catch {
      showRemovalModal.value = false;
      return;
    }
    if (updates.pendingRemovalPlan?.lines.length) {
      showRemovalModal.value = true;
    }
  }

  function closeRemovalModal(): void {
    showRemovalModal.value = false;
    updates.clearPlan();
  }

  async function confirmSelectedRemoval(): Promise<void> {
    const removal = updates.pendingRemovalPlan;
    if (
      !removal?.removal_id ||
      options.removalDisabled.value ||
      !removal.lines.length
    ) {
      return;
    }
    const result = await updates.removeSelectedPending(
      removal.removal_id,
      removal.lines.map((item) => ({ line_no: item.line_no, raw: item.raw })),
    );
    const removedLines = new Set(result.removed.map((item) => item.line_no));
    options.selectedSelections.value = options.selectedSelections.value.filter(
      (selection) => !removedLines.has(selection.line_no),
    );
    showRemovalModal.value = false;
    await Promise.all([
      loadPendingAndReleaseNotes({
        preserveCleanup: true,
        freshAfterCurrent: true,
      }),
      runs.loadRuns(),
    ]);
  }

  async function confirmCleanup(): Promise<void> {
    const cleanup = updates.plan?.cleanup;
    if (
      !cleanup?.cleanup_id ||
      options.cleanupDisabled.value ||
      !cleanup.items.length
    ) {
      return;
    }
    const result = await updates.cleanupPending(
      cleanup.cleanup_id,
      cleanup.items.map((item) => ({ line_no: item.line_no, raw: item.raw })),
    );
    const removedLines = new Set(result.removed.map((item) => item.line_no));
    options.selectedSelections.value = options.selectedSelections.value.filter(
      (selection) => !removedLines.has(selection.line_no),
    );
    showCleanupModal.value = false;
    showPreflightModal.value = false;
    options.clearUpdateIntent();
    await Promise.all([
      loadPendingAndReleaseNotes({
        preserveCleanup: true,
        freshAfterCurrent: true,
      }),
      runs.loadRuns(),
    ]);
  }

  async function confirmApply(): Promise<void> {
    if (!updates.plan || options.applyDisabled.value) {
      return;
    }
    const lineNumbers = updates.plan.selected_line_numbers;
    const snapshot = options.createApplyJobSnapshot();
    const payload = options.applyPlanPayload({
      allowTagUpdates: options.lineNumbersHaveTagUpdates(lineNumbers),
      tagOverrides: options.tagOverridesForLines(lineNumbers),
    });
    const job = await updates.applyPlan(
      updates.plan.plan_id,
      lineNumbers,
      payload.allowTagUpdates,
      payload.tagOverrides,
      payload.digestPinLabelRewriteApprovals,
      {
        selections: updates.plan.selected_selections ?? [],
        tagStreamDecisions: payload.tagStreamDecisions,
        tagStreamLabelRewriteApprovals:
          payload.tagStreamLabelRewriteApprovals,
      },
    );
    options.applyJobSnapshot.value = snapshot;
    options.subscribeApplyJob(job.job_id);
    showPreflightModal.value = false;
    options.clearUpdateIntent();
    await options.focusApplyJobPanel();
  }

  async function loadPendingAndReleaseNotes(
    requestOptions: {
      preserveCleanup?: boolean;
      freshAfterCurrent?: boolean;
    } = {},
  ): Promise<void> {
    await updates.loadPending(requestOptions);
    await updates.loadReleaseNotes().catch(() => undefined);
    await updates.loadSecurityScans().catch(() => undefined);
    refreshReleaseNotesInBackground();
  }

  function refreshReleaseNotesInBackground(): void {
    updates.refreshReleaseNotes().catch(() => undefined);
  }

  async function retryPendingLoad(): Promise<void> {
    await loadPendingAndReleaseNotes().catch(() => undefined);
  }

  return {
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
  };
}
