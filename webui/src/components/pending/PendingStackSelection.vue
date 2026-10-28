<script setup lang="ts">
import { NTag } from "naive-ui";

import type {
  PendingGroupedItem,
  PendingItem,
  PendingSnoozedCandidate,
  PendingStackGroup,
  ReleaseNoteInfo,
  SecurityScanInfo,
} from "../../api/client";
import type {
  RuntimeDeferredPendingItem,
  SnoozedPendingItem,
} from "../../views/pending/snoozeSelection";
import {
  pendingMetadataStatus,
  type PendingTagInputProps,
} from "../../views/pending/pendingDisplay";
import type { SafetyCue } from "../../views/pending/safetyCues";
import {
  pendingSelectionForItem,
  pendingSelectionKey,
} from "../../views/pending/usePendingSelectionState";
import { pluralize } from "../../views/pending/utils";
import PendingEmptyQueueState from "./PendingEmptyQueueState.vue";
import PendingSnoozedEntriesPanel from "./PendingSnoozedEntriesPanel.vue";
import PendingStackCard from "./PendingStackCard.vue";
import PendingUpdateRow from "./PendingUpdateRow.vue";

defineProps<{
  latestRunId: number | null;
  loading: boolean;
  pendingSourceLabel: string;
  releaseNoteFor: (item: PendingGroupedItem) => ReleaseNoteInfo | null;
  releaseNoteReason: (note: ReleaseNoteInfo | null) => string;
  releaseNoteStatus: (note: ReleaseNoteInfo | null) => string;
  riskCues: (item: PendingGroupedItem) => SafetyCue[];
  securityScanFor: (item: PendingGroupedItem) => SecurityScanInfo | null;
  selectedSelectionKeySet: Set<string>;
  showSetupLink: boolean;
  skipReleaseDisabledReason: string;
  snoozeDisabled: boolean;
  snoozeDisabledMessage: string;
  snoozedCandidates: PendingSnoozedCandidate[];
  snoozedItems: SnoozedPendingItem[];
  stackGroups: PendingStackGroup[];
  stoppedItems: RuntimeDeferredPendingItem[];
  selfUpdateItems: SnoozedPendingItem[];
  stackHasSelection: (group: PendingStackGroup) => boolean;
  stackIndeterminate: (group: PendingStackGroup) => boolean;
  stackSelected: (group: PendingStackGroup) => boolean;
  staleDiagnosticDetail: (item: PendingGroupedItem) => string;
  staleDiagnosticLabel: (item: PendingGroupedItem) => string;
  tagInputProps: (item: Pick<PendingItem, "image">) => PendingTagInputProps;
  tagOverrideValue: (item: PendingItem) => string;
  unmatchedIssueSummary: string;
  unmatchedItems: PendingGroupedItem[];
  unmatchedReviewSummary: string;
  updateDisabled: (lineNumbers: number[]) => boolean;
}>();

const emit = defineEmits<{
  previewStack: [group: PendingStackGroup];
  skipRelease: [group: PendingStackGroup, item: PendingGroupedItem];
  snoozeItem: [group: PendingStackGroup, item: PendingGroupedItem];
  toggleItem: [item: PendingGroupedItem, checked: boolean];
  toggleStack: [group: PendingStackGroup, checked: boolean];
  updateTag: [item: PendingGroupedItem, value: string];
}>();

function verifiedLineNumbers(group: PendingStackGroup): number[] {
  return group.items
    .filter((item) => pendingMetadataStatus(item) === "fresh")
    .map((item) => item.line_no);
}
</script>

<template>
  <section class="stack-selection">
    <PendingStackCard
      v-for="group in stackGroups"
      :key="`${group.directory}/${group.compose_file}`"
      :group="group"
      :loading="loading"
      :release-note-for="releaseNoteFor"
      :release-note-reason="releaseNoteReason"
      :release-note-status="releaseNoteStatus"
      :risk-cues="riskCues"
      :security-scan-for="securityScanFor"
      :selected-selection-key-set="selectedSelectionKeySet"
      :skip-release-disabled-reason="skipReleaseDisabledReason"
      :stack-has-selection="stackHasSelection(group)"
      :stack-indeterminate="stackIndeterminate(group)"
      :stack-selected="stackSelected(group)"
      :snooze-disabled="snoozeDisabled"
      :snooze-disabled-message="snoozeDisabledMessage"
      :tag-input-props="tagInputProps"
      :tag-override-value="tagOverrideValue"
      :update-disabled="updateDisabled(verifiedLineNumbers(group))"
      @preview-stack="emit('previewStack', $event)"
      @skip-release="(stackGroup, item) => emit('skipRelease', stackGroup, item)"
      @snooze-item="(stackGroup, item) => emit('snoozeItem', stackGroup, item)"
      @toggle-item="(item, checked) => emit('toggleItem', item, checked)"
      @toggle-stack="(stackGroup, checked) => emit('toggleStack', stackGroup, checked)"
      @update-tag="(item, value) => emit('updateTag', item, value)"
    />

    <PendingSnoozedEntriesPanel
      :risk-cues="riskCues"
      :security-scan-for="securityScanFor"
      :selected-selection-key-set="selectedSelectionKeySet"
      :snoozed-candidates="snoozedCandidates"
      :snoozed-items="snoozedItems"
      :stopped-items="stoppedItems"
      :self-update-items="selfUpdateItems"
      :tag-input-props="tagInputProps"
      :tag-override-value="tagOverrideValue"
      @toggle-item="(item, checked) => emit('toggleItem', item, checked)"
      @update-tag="(item, value) => emit('updateTag', item, value)"
    />

    <article v-if="unmatchedItems.length" class="stack-card needs-review">
      <div class="stack-card-header">
        <div class="stack-title-block">
          <strong class="wrap-anywhere">Stale pending entries</strong>
          <span class="stack-path wrap-anywhere">{{ unmatchedReviewSummary }}</span>
        </div>
        <div class="stack-card-side">
          <div class="stack-card-tags">
            <n-tag size="small" type="warning">
              {{ pluralize(unmatchedItems.length, "item") }}
            </n-tag>
            <n-tag v-if="unmatchedIssueSummary" size="small" type="warning">
              {{ unmatchedIssueSummary }}
            </n-tag>
          </div>
        </div>
      </div>
      <details class="stack-details">
        <summary
          class="disclosure-summary disclosure-summary-triangle"
          aria-label="Details for unmatched updates"
        >
          Details
        </summary>
        <div class="stack-items">
          <PendingUpdateRow
            v-for="item in unmatchedItems"
            :key="`unmatched-${pendingSelectionKey(pendingSelectionForItem(item))}`"
            :item="item"
            :selected="selectedSelectionKeySet.has(pendingSelectionKey(pendingSelectionForItem(item)))"
            :service-label="item.repo"
            :status-label="staleDiagnosticLabel(item)"
            status-tag-type="warning"
            :risk-cues="[]"
            :security-scan="securityScanFor(item)"
            :meta-detail="staleDiagnosticDetail(item)"
            :show-diagnostic="Boolean(item.diagnostic)"
            :show-release-notes="false"
            :tag-override-value="tagOverrideValue(item)"
            :show-tag-input="Boolean(item.desired_tag)"
            :tag-input-props="tagInputProps(item)"
            @toggle="(_lineNo, checked) => emit('toggleItem', item, checked)"
            @update-tag="emit('updateTag', item, $event)"
          />
        </div>
      </details>
    </article>

    <PendingEmptyQueueState
      v-if="
        !stackGroups.length &&
        !snoozedCandidates.length &&
        !snoozedItems.length &&
        !stoppedItems.length &&
        !selfUpdateItems.length &&
        !unmatchedItems.length
      "
      :latest-run-id="latestRunId"
      :pending-source-label="pendingSourceLabel"
      :show-setup-link="showSetupLink"
    />
  </section>
</template>

<style scoped>
.stack-selection {
  display: grid;
  gap: 12px;
}

.stack-card {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  box-shadow: var(--shadow-panel-lift);
}

.stack-card.needs-review {
  background: var(--color-panel-tint);
}

.stack-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.stack-card-side {
  display: grid;
  justify-items: end;
  gap: 8px;
  min-width: 0;
}

.stack-title-block {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.stack-path {
  color: var(--color-muted-text);
  font-size: 0.82rem;
}

.stack-card-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  min-width: 0;
}

.stack-details {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.stack-details .disclosure-summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: fit-content;
  min-height: 32px;
  color: var(--color-action-blue);
  font-size: var(--text-metadata-size);
  font-weight: 700;
}

.stack-details[open] .disclosure-summary {
  margin-bottom: 8px;
}

.stack-items {
  display: grid;
  border-top: 1px solid var(--color-border-subtle);
}

@media (--wud-app-shell) {
  .stack-card-header {
    align-items: flex-start;
  }
}

@media (--wud-compact) {
  .stack-card-header {
    display: grid;
  }

  .stack-details .disclosure-summary {
    min-height: var(--size-touch-target);
  }

  .stack-card-tags {
    justify-content: flex-start;
  }

  .stack-card-side {
    justify-items: start;
    justify-content: flex-start;
  }
}
</style>
