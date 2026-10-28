import { computed, ref, type ComputedRef } from "vue";

import type {
  PendingGroupedItem,
  PendingItem,
  PendingSnoozedCandidate,
  PendingStackGroup,
  PlanSelectionRequest,
  ReleaseNoteInfo,
} from "../../api/client";
import type { ReleaseChangelogState } from "../../utils/releaseChangelog";
import type { SafetyCue } from "./safetyCues";
import type {
  RuntimeDeferredPendingItem,
  SelfUpdatePendingItem,
  SnoozedPendingItem,
} from "./snoozeSelection";
import {
  filterPendingItems,
  filterPendingStackGroups,
  filterSnoozedCandidates,
  filterSnoozedItems,
  normalizePendingSearch,
} from "./pendingFilter";
import { pluralize } from "./utils";

type UsePendingSearchStateOptions = {
  pendingItems: ComputedRef<PendingItem[]>;
  groupingReady: ComputedRef<boolean>;
  snoozedCandidates: ComputedRef<PendingSnoozedCandidate[]>;
  snoozedItems: ComputedRef<SnoozedPendingItem[]>;
  stoppedItems: ComputedRef<RuntimeDeferredPendingItem[]>;
  selfUpdateItems: ComputedRef<SelfUpdatePendingItem[]>;
  selectableLineNumbers: ComputedRef<number[]>;
  selectableSelections: ComputedRef<PlanSelectionRequest[]>;
  selectAllLabel: ComputedRef<string>;
  stackGroups: ComputedRef<PendingStackGroup[]>;
  unmatchedItems: ComputedRef<PendingGroupedItem[]>;
  releaseChangelogFor: (
    note: ReleaseNoteInfo | null,
  ) => ReleaseChangelogState | null;
  releaseNoteFor: (item: PendingItem) => ReleaseNoteInfo | null;
  releaseNoteReason: (note: ReleaseNoteInfo | null) => string;
  releaseNoteStatus: (note: ReleaseNoteInfo | null) => string;
  riskCues: (item: PendingItem) => SafetyCue[];
};

export function usePendingSearchState(options: UsePendingSearchStateOptions) {
  const pendingSearchQuery = ref("");
  const pendingSearchText = computed(() =>
    normalizePendingSearch(pendingSearchQuery.value),
  );
  const pendingSearchActive = computed(() => Boolean(pendingSearchText.value));
  const pendingSearchContext = {
    releaseChangelogFor: options.releaseChangelogFor,
    releaseNoteFor: options.releaseNoteFor,
    releaseNoteReason: options.releaseNoteReason,
    releaseNoteStatus: options.releaseNoteStatus,
    riskCues: options.riskCues,
  };

  const filteredStackGroups = computed(() =>
    filterPendingStackGroups(
      options.stackGroups.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const filteredUnmatchedItems = computed(() =>
    filterPendingItems(
      options.unmatchedItems.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const filteredSnoozedItems = computed(() =>
    filterSnoozedItems(
      options.snoozedItems.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const filteredStoppedItems = computed(() =>
    filterSnoozedItems(
      options.stoppedItems.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const filteredSelfUpdateItems = computed(() =>
    filterSnoozedItems(
      options.selfUpdateItems.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const filteredSnoozedCandidates = computed(() =>
    filterSnoozedCandidates(
      options.snoozedCandidates.value,
      pendingSearchText.value,
    ),
  );
  const filteredPendingItems = computed(() =>
    filterPendingItems(
      options.pendingItems.value,
      pendingSearchText.value,
      pendingSearchContext,
    ),
  );
  const visibleLineNumbers = computed(() => {
    if (options.groupingReady.value) {
      return [
        ...filteredStackGroups.value.flatMap((group) => group.visibleLineNumbers),
        ...filteredSnoozedItems.value.map(({ item }) => item.line_no),
        ...filteredStoppedItems.value.map(({ item }) => item.line_no),
        ...filteredSelfUpdateItems.value.map(({ item }) => item.line_no),
        ...filteredUnmatchedItems.value.map((item) => item.line_no),
      ];
    }
    return filteredPendingItems.value.map((item) => item.line_no);
  });
  const visibleSelections = computed<PlanSelectionRequest[]>(() => {
    if (options.groupingReady.value) {
      return [
        ...filteredStackGroups.value.flatMap((group) =>
          group.items.map((item) => ({
            line_no: item.line_no,
            selection_id: item.selection_id ?? "",
          })),
        ),
        ...filteredSnoozedItems.value.map(({ item }) => ({
          line_no: item.line_no,
          selection_id: item.selection_id ?? "",
        })),
        ...filteredStoppedItems.value.map(({ item }) => ({
          line_no: item.line_no,
          selection_id: item.selection_id ?? "",
        })),
        ...filteredUnmatchedItems.value.map((item) => ({
          line_no: item.line_no,
          selection_id: item.selection_id ?? "",
        })),
      ];
    }
    return filteredPendingItems.value.map((item) => ({
      line_no: item.line_no,
      selection_id: "",
    }));
  });
  const visibleSelectableLineNumbers = computed(() => {
    const visibleLines = new Set(visibleLineNumbers.value);
    return options.selectableLineNumbers.value.filter((lineNo) =>
      visibleLines.has(lineNo),
    );
  });
  const visibleSelectionKeys = computed(
    () =>
      new Set(
        visibleSelections.value.map(
          (selection) =>
            selection.selection_id || `line:${selection.line_no}`,
        ),
      ),
  );
  const visibleSelectableSelections = computed(() =>
    options.selectableSelections.value.filter((selection) =>
      visibleSelectionKeys.value.has(
        selection.selection_id || `line:${selection.line_no}`,
      ),
    ),
  );
  const visibleSelectAllLabel = computed(() =>
    pendingSearchActive.value ? "Select visible updates" : options.selectAllLabel.value,
  );
  const pendingSearchEmpty = computed(
    () =>
      pendingSearchActive.value &&
      (options.pendingItems.value.length > 0 ||
        options.snoozedCandidates.value.length > 0) &&
      visibleLineNumbers.value.length === 0 &&
      filteredSnoozedCandidates.value.length === 0,
  );
  const pendingSearchResultLabel = computed(() => {
    if (!pendingSearchActive.value) {
      return "";
    }
    const updateLabel = pluralize(
      visibleLineNumbers.value.length,
      "visible update",
    );
    const snoozeCount = filteredSnoozedCandidates.value.length;
    if (!snoozeCount) {
      return `${updateLabel} matched`;
    }
    const snoozeLabel = pluralize(snoozeCount, "visible snooze");
    if (visibleLineNumbers.value.length === 0) {
      return `${snoozeLabel} matched`;
    }
    return `${updateLabel} and ${snoozeLabel} matched`;
  });

  function clearPendingSearch(): void {
    pendingSearchQuery.value = "";
  }

  return {
    clearPendingSearch,
    filteredPendingItems,
    filteredSelfUpdateItems,
    filteredSnoozedCandidates,
    filteredSnoozedItems,
    filteredStackGroups,
    filteredStoppedItems,
    filteredUnmatchedItems,
    pendingSearchActive,
    pendingSearchEmpty,
    pendingSearchQuery,
    pendingSearchResultLabel,
    visibleLineNumbers,
    visibleSelections,
    visibleSelectableLineNumbers,
    visibleSelectableSelections,
    visibleSelectAllLabel,
  };
}
