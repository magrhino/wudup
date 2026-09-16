import { computed, type ComputedRef, type Ref } from "vue";

import type {
  PendingGroupedItem,
  PlanSelectionRequest,
} from "../../api/client";
import { pendingSelectionKey } from "./usePendingSelectionState";
import { pluralize } from "./utils";

type UsePendingSearchResultStateOptions = {
  pendingSearchActive: ComputedRef<boolean>;
  visibleSelections: ComputedRef<PlanSelectionRequest[]>;
  selectedSelections: Ref<PlanSelectionRequest[]>;
  filteredUnmatchedItems: ComputedRef<PendingGroupedItem[]>;
  unmatchedItems: ComputedRef<PendingGroupedItem[]>;
  unmatchedIssueSummary: ComputedRef<string>;
  unmatchedReviewCountLabel: ComputedRef<string>;
  unmatchedReviewSummary: ComputedRef<string>;
};

export function usePendingSearchResultState(
  options: UsePendingSearchResultStateOptions,
) {
  const selectedHiddenCount = computed(() => {
    if (!options.pendingSearchActive.value) {
      return 0;
    }
    const visibleKeys = new Set(
      options.visibleSelections.value.map(pendingSelectionKey),
    );
    return options.selectedSelections.value.filter(
      (selection) => !visibleKeys.has(pendingSelectionKey(selection)),
    ).length;
  });
  const visibleUnmatchedReviewSummary = computed(() => {
    if (!options.pendingSearchActive.value) {
      return options.unmatchedReviewSummary.value;
    }
    const count = options.filteredUnmatchedItems.value.length;
    const verb = count === 1 ? "needs" : "need";
    return `${pluralize(count, "pending line", "pending lines")} matched search and ${verb} review.`;
  });
  const visibleUnmatchedIssueSummary = computed(() =>
    options.pendingSearchActive.value &&
    options.filteredUnmatchedItems.value.length !== options.unmatchedItems.value.length
      ? ""
      : options.unmatchedIssueSummary.value,
  );
  const visibleUnmatchedReviewCountLabel = computed(() => {
    if (!options.pendingSearchActive.value) {
      return options.unmatchedItems.value.length
        ? options.unmatchedReviewCountLabel.value
        : "";
    }
    return options.filteredUnmatchedItems.value.length
      ? `${pluralize(options.filteredUnmatchedItems.value.length, "item")} needing review`
      : "";
  });

  return {
    selectedHiddenCount,
    visibleUnmatchedIssueSummary,
    visibleUnmatchedReviewCountLabel,
    visibleUnmatchedReviewSummary,
  };
}
