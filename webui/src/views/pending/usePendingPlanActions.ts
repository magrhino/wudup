import { ref, type ComputedRef, type Ref } from "vue";

import type {
  PendingStackGroup,
  PlanSelectionRequest,
  TagOverrideRequest,
} from "../../api/client";
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
  clearUpdateIntent: () => void;
  createApplyJobSnapshot: () => ApplyJobPlanSnapshot | null;
  focusApplyJobPanel: () => Promise<void>;
  lineNumbersHaveTagUpdates: (lineNumbers: number[]) => boolean;
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
  const showPreflightModal = ref(false);

  function clearPreflight(): void {
    showPreflightModal.value = false;
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
    closePreflightModal,
    confirmApply,
    loadPendingAndReleaseNotes,
    retryPendingLoad,
    showPreflightModal,
    startSelectedUpdate,
    startStackUpdate,
  };
}
