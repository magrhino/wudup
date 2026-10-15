import { ref, type ComputedRef, type Ref } from "vue";

import {
  ApiError,
  type PendingGroupedItem,
  type PendingStackGroup,
  type PlanSelectionRequest,
  type TagOverrideRequest,
} from "../../api/client";
import { useUpdatesStore } from "../../stores/updates";
import { uniqueSorted } from "./pendingDisplay";
import type { ApplyJobPlanSnapshot } from "./usePendingApplyJob";
import type {
  PendingApplyPlanPayload,
  PendingUpdateIntent,
} from "./usePendingPlanReviewState";
import {
  pendingSelectionForItem,
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
  // Every reviewable group, including stopped and snoozed services that the
  // queue hides but an operator can still select and review.
  allStackGroups: ComputedRef<PendingStackGroup[]>;
  focusApplyJobPanel: () => Promise<void>;
  lineNumbersHaveTagUpdates: (lineNumbers: number[]) => boolean;
  markPlanStale: (detail: string) => void;
  selectedSelections: Ref<PlanSelectionRequest[]>;
  selectedUpdateContext: ComputedRef<string>;
  stackGroups: ComputedRef<PendingStackGroup[]>;
  setUpdateIntent: (intent: PendingUpdateIntent) => void;
  subscribeApplyJob: (jobId: string) => void;
  tagOverrideErrorForLines: (lineNumbers: number[]) => string;
  tagOverridesForLines: (lineNumbers: number[]) => TagOverrideRequest[];
  updateIntent: Ref<PendingUpdateIntent | null>;
};

export function usePendingPlanActions(options: UsePendingPlanActionsOptions) {
  const updates = useUpdatesStore();
  const showPreflightModal = ref(false);
  // Why a rebuilt plan covers fewer updates than the one first reviewed.
  const rebuildNotice = ref("");

  function clearPreflight(): void {
    showPreflightModal.value = false;
    rebuildNotice.value = "";
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
    rebuildNotice.value = "";
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

  async function rebuildStalePlan(): Promise<void> {
    const intent = options.updateIntent.value;
    const reviewedPlan = updates.plan;
    if (!intent || !reviewedPlan || updates.loading) {
      return;
    }
    // Selections carry line numbers that shift when WUD's list changes, so
    // find the services the operator reviewed again in a fresh pending list.
    const reviewed = new Map<string, string>();
    for (const stack of reviewedPlan.stacks) {
      for (const line of stack.lines) {
        reviewed.set(serviceKey(stack.directory, stack.compose_file, line.service), `${stack.name}/${line.service}`);
      }
    }
    try {
      await updates.loadPending({ freshAfterCurrent: true });
    } catch {
      clearPreflight();
      return;
    }
    const found = new Set<string>();
    const selections = groupedItems(options.allStackGroups.value)
      .filter(({ group, item }) => {
        const keys = item.services.map((service) =>
          serviceKey(group.directory, group.compose_file, service),
        );
        keys.filter((key) => reviewed.has(key)).forEach((key) => found.add(key));
        return keys.some((key) => reviewed.has(key));
      })
      .map(({ item }) => pendingSelectionForItem(item));
    const missing = [...reviewed].filter(([key]) => !found.has(key)).map(([, name]) => name);
    const notice = missing.length
      ? `WUD no longer reports a pending update for ${summarizeNames(missing)}, so the rebuilt plan leaves ${missing.length === 1 ? "it" : "them"} out.`
      : "";
    if (!selections.length) {
      clearPreflight();
      rebuildNotice.value = notice;
      return;
    }
    await startUpdateFlow({
      title: intent.title,
      contextLabel: intent.contextLabel,
      selections,
    });
    if (!updates.plan) {
      // Planning failed or nothing could be planned; the page shows why.
      clearPreflight();
      return;
    }
    rebuildNotice.value = notice;
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
    let job;
    try {
      job = await updates.applyPlan(
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
    } catch (exc) {
      // Keep the review open and say why, so the operator can rebuild it.
      if (isStalePlanError(exc)) {
        options.markPlanStale(exc.message);
        // The review shows the reason; the page banner would only repeat it.
        updates.clearError();
        return;
      }
      throw exc;
    }
    options.applyJobSnapshot.value = snapshot;
    options.subscribeApplyJob(job.job_id);
    showPreflightModal.value = false;
    rebuildNotice.value = "";
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
    rebuildNotice,
    rebuildStalePlan,
    retryPendingLoad,
    showPreflightModal,
    startSelectedUpdate,
    startStackUpdate,
  };
}

function isStalePlanError(exc: unknown): exc is ApiError {
  return (
    exc instanceof ApiError &&
    exc.status === 409 &&
    exc.message.toLowerCase().startsWith("plan is stale")
  );
}

function groupedItems(
  groups: PendingStackGroup[],
): { group: PendingStackGroup; item: PendingGroupedItem }[] {
  return groups.flatMap((group) => group.items.map((item) => ({ group, item })));
}

// Identifies a service by its Compose stack, not by its pending line number.
function serviceKey(directory: string, composeFile: string, service: string): string {
  return JSON.stringify([directory, composeFile, service]);
}

function summarizeNames(names: string[]): string {
  const shown = names.slice(0, 3).join(", ");
  return names.length > 3 ? `${shown} and ${names.length - 3} more` : shown;
}
