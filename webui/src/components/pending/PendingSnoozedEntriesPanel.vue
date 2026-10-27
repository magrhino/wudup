<script setup lang="ts">
import { NTag } from "naive-ui";

import type {
  PendingGroupedItem,
  PendingItem,
  PendingSnoozedCandidate,
  SecurityScanInfo,
} from "../../api/client";
import type {
  RuntimeDeferredPendingItem,
  SnoozedPendingItem,
} from "../../views/pending/snoozeSelection";
import {
  groupedItemServices,
  type PendingTagInputProps,
} from "../../views/pending/pendingDisplay";
import { releaseSkipWaitingText } from "../../views/pending/releaseSkips";
import type { SafetyCue } from "../../views/pending/safetyCues";
import {
  pendingSelectionForItem,
  pendingSelectionKey,
} from "../../views/pending/usePendingSelectionState";
import { pluralize } from "../../views/pending/utils";
import PendingUpdateRow from "./PendingUpdateRow.vue";

defineProps<{
  riskCues: (item: PendingGroupedItem) => SafetyCue[];
  securityScanFor: (item: PendingGroupedItem) => SecurityScanInfo | null;
  selectedSelectionKeySet: Set<string>;
  snoozedCandidates: PendingSnoozedCandidate[];
  snoozedItems: SnoozedPendingItem[];
  stoppedItems: RuntimeDeferredPendingItem[];
  tagInputProps: (item: Pick<PendingItem, "image">) => PendingTagInputProps;
  tagOverrideValue: (item: PendingItem) => string;
}>();

const emit = defineEmits<{
  toggleItem: [item: PendingGroupedItem, checked: boolean];
  updateTag: [item: PendingGroupedItem, value: string];
}>();

function candidateServiceLabel(candidate: PendingSnoozedCandidate): string {
  return candidate.service_key.replaceAll("/", " / ");
}

function candidateTargetLabel(candidate: PendingSnoozedCandidate): string {
  return (
    candidate.target_image ||
    candidate.desired_tag ||
    candidate.digest ||
    "Unknown target"
  );
}

function candidateMeta(candidate: PendingSnoozedCandidate): string {
  const parts = [candidate.reason || "Snoozed"];
  if (candidate.snooze_kind === "dependency") {
    parts.push(`waiting for ${candidate.wait_for_service_key}`);
  } else if (candidate.snoozed_until) {
    parts.push(`until ${candidate.snoozed_until}`);
  }
  if (candidate.source_id) {
    parts.push(candidate.source_id);
  }
  return parts.join(" | ");
}

function snoozedStatusLabel(item: PendingGroupedItem): string {
  return item.release_skip?.matches ? "Skipped release" : "Snoozed";
}

function snoozedMeta(item: PendingGroupedItem): string {
  const skip = item.release_skip;
  if (!skip?.matches) {
    return "";
  }
  const waiting = releaseSkipWaitingText(skip);
  return skip.reason ? `${waiting} Reason: ${skip.reason}` : waiting;
}

function runtimeStatusLabel(item: PendingGroupedItem): string {
  if (item.runtime_state === "not-running") {
    return "Stopped";
  }
  if (item.runtime_state === "mixed") {
    return pluralize(item.stopped_services.length, "stopped service");
  }
  return "Runtime unknown";
}

function runtimeMeta(item: PendingGroupedItem): string {
  if (item.runtime_state === "not-running") {
    return `Stopped before this update: ${item.stopped_services.join(", ")}. WUDup will recreate without starting them.`;
  }
  if (item.runtime_state === "mixed") {
    return `Stopped before this update: ${item.stopped_services.join(", ")}. Running services: ${item.running_services.join(", ")}.`;
  }
  return "Runtime state could not be verified. WUDup will check again before making changes.";
}
</script>

<template>
  <article v-if="stoppedItems.length" class="stack-card needs-review">
    <div class="stack-card-header">
      <div class="stack-title-block">
        <strong class="wrap-anywhere">Stopped or unverified containers</strong>
        <span class="stack-path wrap-anywhere">
          Excluded from bulk selection. Select individually to update without
          starting stopped containers.
        </span>
      </div>
      <div class="stack-card-side">
        <div class="stack-card-tags">
          <n-tag size="small" type="warning">
            {{ pluralize(stoppedItems.length, "item") }}
          </n-tag>
        </div>
      </div>
    </div>
    <details class="stack-details">
      <summary
        class="disclosure-summary disclosure-summary-triangle"
        aria-label="Details for stopped or unverified updates"
      >
        Details
      </summary>
      <div class="stack-items">
        <PendingUpdateRow
          v-for="{ group, item } in stoppedItems"
          :key="`stopped-${pendingSelectionKey(pendingSelectionForItem(item))}`"
          :item="item"
          :selected="selectedSelectionKeySet.has(pendingSelectionKey(pendingSelectionForItem(item)))"
          :group-name="group.name"
          :service-label="groupedItemServices(item)"
          :status-label="runtimeStatusLabel(item)"
          status-tag-type="warning"
          :risk-cues="riskCues(item)"
          :security-scan="securityScanFor(item)"
          :meta-detail="runtimeMeta(item)"
          :show-diagnostic="false"
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

  <article
    v-if="snoozedItems.length || snoozedCandidates.length"
    class="stack-card needs-review"
  >
    <div class="stack-card-header">
      <div class="stack-title-block">
        <strong class="wrap-anywhere">Snoozed pending entries</strong>
        <span class="stack-path wrap-anywhere">
          Excluded from bulk selection while snoozed.
        </span>
      </div>
      <div class="stack-card-side">
        <div class="stack-card-tags">
          <n-tag size="small" type="default">
            {{ pluralize(snoozedItems.length + snoozedCandidates.length, "item") }}
          </n-tag>
        </div>
      </div>
    </div>
    <details class="stack-details">
      <summary
        class="disclosure-summary disclosure-summary-triangle"
        aria-label="Details for snoozed updates"
      >
        Details
      </summary>
      <div class="stack-items">
        <PendingUpdateRow
          v-for="{ group, item } in snoozedItems"
          :key="`snoozed-${pendingSelectionKey(pendingSelectionForItem(item))}`"
          :item="item"
          :selected="selectedSelectionKeySet.has(pendingSelectionKey(pendingSelectionForItem(item)))"
          :group-name="group.name"
          :service-label="groupedItemServices(item)"
          :status-label="snoozedStatusLabel(item)"
          status-tag-type="default"
          :risk-cues="riskCues(item)"
          :security-scan="securityScanFor(item)"
          :meta-detail="snoozedMeta(item)"
          :show-diagnostic="false"
          :show-release-notes="false"
          :tag-override-value="tagOverrideValue(item)"
          :show-tag-input="Boolean(item.desired_tag)"
          :tag-input-props="tagInputProps(item)"
          @toggle="(_lineNo, checked) => emit('toggleItem', item, checked)"
          @update-tag="emit('updateTag', item, $event)"
        />
        <div
          v-for="candidate in snoozedCandidates"
          :key="`snoozed-candidate-${candidate.key}`"
          class="pending-snooze-record"
        >
          <div class="pending-snooze-record-main">
            <strong class="wrap-anywhere">
              {{ candidateServiceLabel(candidate) }}
            </strong>
            <n-tag size="small" type="default">Snoozed</n-tag>
          </div>
          <span class="pending-snooze-record-target wrap-anywhere">
            {{ candidate.image }} -> {{ candidateTargetLabel(candidate) }}
          </span>
          <span class="pending-snooze-record-meta wrap-anywhere">
            {{ candidateMeta(candidate) }}
          </span>
          <span class="pending-snooze-record-meta">
            Display only. No matching pending update row.
          </span>
        </div>
      </div>
    </details>
  </article>
</template>

<style scoped>
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
  margin-bottom: 4px;
}

.stack-items {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.pending-snooze-record {
  display: grid;
  gap: 6px;
  min-width: 0;
  padding: 10px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.pending-snooze-record:first-child {
  border-top: 0;
  padding-top: 0;
}

.pending-snooze-record-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.pending-snooze-record-target {
  color: var(--color-heading);
  font-size: var(--text-body-size);
}

.pending-snooze-record-meta {
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

@media (--wud-compact) {
  .stack-card-header {
    display: grid;
  }
}
</style>
