<script setup lang="ts">
import { computed } from "vue";
import { Play, Repeat2, ShieldAlert } from "@lucide/vue";
import { NButton, NCheckbox, NTag } from "naive-ui";

import type {
  PendingGroupedItem,
  PendingItem,
  PendingStackGroup,
  ReleaseNoteInfo,
  SecurityScanInfo,
} from "../../api/client";
import { displayDigest } from "../../utils/digestProvenance";
import {
  groupedItemActionLabel,
  groupedItemActionTagType,
  groupedItemServices,
  groupedItemTagRewriteLabel,
  groupedItemTarget,
  groupTagChangeCount,
  itemsBreakingCount,
  itemsVerifiedSecurityCount,
  pendingMetadataStatusLabel,
  pendingMetadataStatus,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
  type PendingTagInputProps,
  previewImageLabel,
} from "../../views/pending/pendingDisplay";
import type { SafetyCue } from "../../views/pending/safetyCues";
import {
  pendingSelectionForItem,
  pendingSelectionKey,
} from "../../views/pending/usePendingSelectionState";
import { pluralize } from "../../views/pending/utils";
import PendingReleaseNotes from "./PendingReleaseNotes.vue";
import PendingUpdateRow from "./PendingUpdateRow.vue";

const props = defineProps<{
  group: PendingStackGroup;
  loading: boolean;
  releaseNoteFor: (item: PendingGroupedItem) => ReleaseNoteInfo | null;
  releaseNoteReason: (note: ReleaseNoteInfo | null) => string;
  releaseNoteStatus: (note: ReleaseNoteInfo | null) => string;
  riskCues: (item: PendingGroupedItem) => SafetyCue[];
  securityScanFor: (item: PendingGroupedItem) => SecurityScanInfo | null;
  selectedSelectionKeySet: Set<string>;
  stackHasSelection: boolean;
  stackIndeterminate: boolean;
  stackSelected: boolean;
  tagInputProps: (item: Pick<PendingItem, "image">) => PendingTagInputProps;
  tagOverrideValue: (item: PendingItem) => string;
  updateDisabled: boolean;
}>();

const emit = defineEmits<{
  previewStack: [group: PendingStackGroup];
  toggleItem: [item: PendingGroupedItem, checked: boolean];
  toggleStack: [group: PendingStackGroup, checked: boolean];
  updateTag: [item: PendingGroupedItem, value: string];
}>();

function actionableRiskCues(item: PendingGroupedItem): SafetyCue[] {
  return props.riskCues(item).filter((cue) => cue.type === "warning" || cue.type === "error");
}

const verifiedUpdateCount = computed(
  () =>
    props.group.items.filter((item) => pendingMetadataStatus(item) === "fresh")
      .length,
);
const blockedUpdateCount = computed(
  () => props.group.items.length - verifiedUpdateCount.value,
);
const previewDisabled = computed(
  () => props.updateDisabled || verifiedUpdateCount.value === 0,
);
const previewLabel = computed(() => {
  if (!blockedUpdateCount.value) {
    return `Review ${props.group.name} plan`;
  }
  if (!verifiedUpdateCount.value) {
    return "No verified updates";
  }
  return `Review ${pluralize(verifiedUpdateCount.value, "verified update")}`;
});
const previewDisabledMessage = computed(() =>
  verifiedUpdateCount.value
    ? ""
    : "No updates in this stack have fresh WUD metadata. Check your WUD configuration.",
);
</script>

<template>
  <article
    class="stack-card"
    :class="{ selected: stackHasSelection }"
  >
    <div class="stack-card-header">
      <div class="stack-title-block">
        <n-checkbox
          :checked="stackSelected"
          :indeterminate="stackIndeterminate"
          :aria-label="`Select stack ${group.name}`"
          @update:checked="emit('toggleStack', group, Boolean($event))"
        >
          <span class="stack-checkbox-label">
            <span class="sr-only">Select stack </span>
            <span class="stack-checkbox-kicker" aria-hidden="true">Stack</span>
            <strong class="wrap-anywhere" :title="group.directory">{{ group.name }}</strong>
          </span>
        </n-checkbox>
        <div class="stack-identity" aria-label="Stack impact">
          <span class="wrap-anywhere">
            <span class="identity-label">Services</span>
            {{ group.services_label }}
          </span>
        </div>
      </div>
      <div class="stack-card-side">
        <div class="stack-card-tags">
          <n-tag size="small">{{ pluralize(group.items.length, "update") }}</n-tag>
          <n-tag v-if="groupTagChangeCount(group)" size="small" type="warning">
            {{ pluralize(groupTagChangeCount(group), "tag rewrite") }}
          </n-tag>
          <n-tag
            v-if="itemsVerifiedSecurityCount(group.items, releaseNoteFor)"
            class="stack-advisory-tag"
            size="small"
            type="error"
          >
            <template #icon>
              <ShieldAlert :size="14" aria-hidden="true" />
            </template>
            {{ pluralize(itemsVerifiedSecurityCount(group.items, releaseNoteFor), "verified high/critical release update") }}
          </n-tag>
          <n-tag
            v-if="itemsBreakingCount(group.items, releaseNoteFor)"
            size="small"
            type="warning"
          >
            {{ pluralize(itemsBreakingCount(group.items, releaseNoteFor), "breaking cue") }}
          </n-tag>
        </div>
        <div class="stack-card-actions">
          <n-button
            size="small"
            secondary
            type="primary"
            :disabled="previewDisabled"
            :loading="loading"
            :title="previewDisabledMessage || undefined"
            @click="emit('previewStack', group)"
          >
            <template #icon>
              <Play :size="16" />
            </template>
            {{ previewLabel }}
          </n-button>
        </div>
      </div>
    </div>

    <div class="stack-change-preview" aria-label="Change preview">
      <div
        v-for="item in group.items"
        :key="`${group.name}-${item.line_no}-preview`"
        class="stack-change-row"
      >
        <strong class="stack-change-service wrap-anywhere">{{ groupedItemServices(item) }}</strong>
        <span class="stack-change-target wrap-anywhere">
          <code
            class="stack-change-value wrap-anywhere"
            data-label="Current"
            :title="item.image"
          >
            {{ previewImageLabel(item.image, displayDigest) }}
          </code>
          <span aria-hidden="true">-&gt;</span>
          <code
            class="stack-change-value wrap-anywhere"
            data-label="Target"
            :title="groupedItemTarget(item)"
          >
            {{ previewImageLabel(groupedItemTarget(item), displayDigest) }}
          </code>
          <n-tag
            size="small"
            :type="groupedItemActionTagType(item)"
          >
            {{ groupedItemActionLabel(item) }}
          </n-tag>
          <n-tag
            v-if="pendingMetadataStatus(item) !== 'fresh'"
            size="small"
            :type="pendingMetadataStatusTagType(item)"
            :title="pendingMetadataStatusTitle(item)"
          >
            {{ pendingMetadataStatusLabel(item) }} metadata
          </n-tag>
          <span
            v-if="actionableRiskCues(item).length"
            class="risk-badges-container stack-change-risk-cues"
            aria-label="Safety cues"
          >
            <n-tag
              v-for="cue in actionableRiskCues(item)"
              :key="`${item.line_no}-${cue.key}`"
              size="small"
              :type="cue.type"
              class="safety-badge"
            >
              {{ cue.label }}
            </n-tag>
          </span>
          <n-button
            v-if="item.tag_stream"
            size="small"
            secondary
            type="info"
            class="stream-choice-trigger"
            aria-haspopup="dialog"
            :aria-label="`Choose update stream for ${item.image}`"
            @click="emit('previewStack', group)"
          >
            <template #icon>
              <Repeat2 :size="15" aria-hidden="true" />
            </template>
            Choose stream
          </n-button>
        </span>
        <PendingReleaseNotes
          class="stack-change-release"
          :candidate-label="`${groupedItemServices(item)} · ${groupedItemTarget(item)}`"
          :candidate-tag="groupedItemTarget(item).split('@')[0]?.split('/').pop()?.split(':')[1] || ''"
          :release-note="releaseNoteFor(item)"
          :release-note-status="releaseNoteStatus(releaseNoteFor(item))"
          :release-note-reason="releaseNoteReason(releaseNoteFor(item))"
        />
      </div>
    </div>

    <details class="stack-details">
      <summary
        class="disclosure-summary disclosure-summary-triangle"
        :aria-label="`Details for ${group.name}`"
      >
        Details
      </summary>
      <div class="stack-items">
      <PendingUpdateRow
        v-for="item in group.items"
        :key="`${group.name}-${pendingSelectionKey(pendingSelectionForItem(item))}`"
          :item="item"
          :selected="selectedSelectionKeySet.has(pendingSelectionKey(pendingSelectionForItem(item)))"
          :service-label="groupedItemServices(item)"
          :status-label="groupedItemActionLabel(item)"
          :status-tag-type="groupedItemActionTagType(item)"
          :risk-cues="riskCues(item)"
          :tag-rewrite-label="groupedItemTagRewriteLabel(item)"
          :security-scan="securityScanFor(item)"
          :show-diagnostic="Boolean(item.diagnostic)"
          :tag-override-value="tagOverrideValue(item)"
          :show-tag-input="Boolean(item.desired_tag)"
          :tag-input-props="tagInputProps(item)"
          @toggle="(_lineNo, checked) => emit('toggleItem', item, checked)"
          @review-stream="emit('previewStack', group)"
          @update-tag="emit('updateTag', item, $event)"
        />
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

.stack-card.selected {
  border-color: var(--color-border-hover);
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

.stack-title-block :deep(.n-checkbox__label) {
  padding-inline: 6px 0;
}

.stack-checkbox-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  box-sizing: border-box;
  min-height: 30px;
  padding: 5px 10px;
  border: 1px solid color-mix(in srgb,
      var(--color-border-hover) 42%,
      var(--color-border-subtle));
  border-radius: 7px;
  background: color-mix(in srgb,
      var(--color-surface) 94%,
      var(--color-action-blue) 6%);
  color: var(--color-text-secondary);
  font-size: var(--text-metadata-size);
  line-height: 1.35;
  transition:
    border-color var(--motion-base) var(--ease-out-quart),
    background-color var(--motion-base) var(--ease-out-quart);
}

.stack-title-block :deep(.n-checkbox:hover) .stack-checkbox-label {
  border-color: var(--color-border-hover);
  background: color-mix(in srgb,
      var(--color-surface) 90%,
      var(--color-action-blue) 10%);
}

.stack-checkbox-label strong {
  color: var(--color-ink);
}

.stack-checkbox-kicker {
  color: var(--color-muted-text);
  font-size: var(--text-label-size);
  font-weight: 700;
  line-height: 1.2;
}

.stack-identity {
  display: grid;
  gap: 3px;
  color: var(--color-text-secondary);
  font-size: 0.84rem;
  line-height: 1.35;
}

.identity-label {
  margin-right: 6px;
  color: var(--color-muted-text);
  font-weight: 700;
}

.stack-change-preview {
  display: grid;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--color-border-subtle);
}

.stack-change-row {
  display: grid;
  grid-template-columns: minmax(120px, 0.32fr) minmax(0, 1fr);
  gap: 10px;
  min-width: 0;
  color: var(--color-text-secondary);
  font-size: 0.84rem;
  line-height: 1.4;
}

.stack-change-release {
  grid-column: 2;
}

.stack-change-service {
  color: var(--color-ink);
}

.stack-change-target {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px 7px;
}

.stack-change-target code {
  color: var(--color-code-text);
  font-family: var(--font-mono);
  font-size: 0.82rem;
}

.stream-choice-trigger {
  margin-inline-start: 2px;
}

.stack-advisory-tag {
  max-width: 100%;
  height: auto;
  min-height: 24px;
}

.stack-advisory-tag :deep(.n-tag__content) {
  white-space: normal;
}

.stack-card-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  min-width: 0;
}

.stack-card-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
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

.stack-change-risk-cues {
  display: inline-flex;
}

@media (--wud-app-shell) {
  .stack-card-header {
    align-items: flex-start;
  }
}

@media (--wud-compact) {
  .stack-change-release {
    grid-column: 1 / -1;
  }

  .stack-card-header {
    display: grid;
  }

  .stack-card-actions :deep(.n-button) {
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }

  .stack-details .disclosure-summary {
    min-height: var(--size-touch-target);
  }

  .stack-title-block :deep(.n-checkbox) {
    min-height: var(--size-touch-target);
    display: inline-flex;
    align-items: center;
  }

  .stack-card-tags {
    justify-content: flex-start;
  }

  .stack-change-row {
    grid-template-columns: 1fr;
    gap: 4px;
  }

  .stack-change-target {
    display: grid;
    align-items: start;
    gap: 5px;
  }

  .stack-change-target > :deep(.n-tag) {
    width: fit-content;
  }

  .stack-change-target > span[aria-hidden="true"] {
    display: none;
  }

  .stack-change-target code {
    display: block;
  }

  .stack-change-value::before {
    content: attr(data-label);
    display: block;
    margin-bottom: 1px;
    color: var(--color-muted-text);
    font-family: var(--font-sans);
    font-size: var(--text-label-size);
    font-weight: 700;
    line-height: 1.2;
  }

  .stack-card-side,
  .stack-card-actions {
    justify-items: start;
    justify-content: flex-start;
  }
}
</style>
