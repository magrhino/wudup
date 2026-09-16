<script setup lang="ts">
import { Check, Play, RefreshCw, Trash2, X } from "@lucide/vue";
import { NButton, NFlex } from "naive-ui";

defineProps<{
  batchSummaryLabel: string;
  globalRescanDisabled: boolean;
  globalRescanDisabledMessage: string;
  groupingReady: boolean;
  hasSelectedTagUpdates: boolean;
  loading: boolean;
  pendingLoaded: boolean;
  removalButtonLabel: string;
  removeSelectedDisabled: boolean;
  removeSelectedDisabledMessage: string;
  selectableCount: number;
  selectAllLabel: string;
  selectedCount: number;
  selectedHiddenCount: number;
  selectedMetadataWarning: string;
  selectedRescanDisabled: boolean;
  selectedRescanDisabledMessage: string;
  selectedRescanVisible: boolean;
  snoozedCount: number;
  stackCount: number;
  stoppedCount: number;
  searchActive: boolean;
  unmatchedReviewCountLabel: string;
  updateSelectedDisabled: boolean;
  updateSelectedButtonLabel: string;
}>();

const emit = defineEmits<{
  clearSelection: [];
  rescanAll: [];
  rescanSelected: [];
  selectAll: [];
  startRemoval: [];
  startUpdate: [];
}>();
</script>

<template>
  <div v-if="pendingLoaded" class="selection-tools">
    <details class="queue-tools">
      <summary class="disclosure-summary disclosure-summary-triangle">
        Queue details and actions
      </summary>
      <div class="queue-tools-content">
        <p class="wrap-anywhere">
          Pending counts entries from the active source before search. WUD detected
          updates count container signals, which can map to the same pending entry
          or multiple Compose services. These counts do not form a simple sum.
        </p>
        <p v-if="groupingReady" class="wrap-anywhere">
          {{ stackCount }} stacks · {{ selectableCount }} selectable updates ·
          {{ stoppedCount }} stopped/unverified · {{ snoozedCount }} snoozed
          <template v-if="unmatchedReviewCountLabel"> · {{ unmatchedReviewCountLabel }}</template>.
          {{ searchActive ? 'These counts reflect the current search.' : 'These counts cover the whole queue.' }}
          Selectable means included by Select all; review still requires fresh WUD metadata.
          Stopped/unverified services are excluded from bulk selection.
          Needs review means no actionable Compose match; inspect those entries below.
        </p>
        <p v-else>Pending file order. Compose grouping is unavailable; review checks the selected entries.</p>
        <p>Select all replaces the selection with the visible selectable updates. Review includes hidden selections; Clear selection clears all selections.</p>
        <n-flex class="pending-actions" align="center" :size="8">
          <n-button
            size="small"
            secondary
            :disabled="globalRescanDisabled"
            :loading="loading"
            :title="globalRescanDisabledMessage || 'Ask WUD to check registries for updates.'"
            @click="emit('rescanAll')"
          >
            <template #icon><RefreshCw :size="16" /></template>
            Rescan WUD
          </n-button>
          <n-button
            v-if="selectedCount && selectedRescanVisible"
            size="small"
            secondary
            :disabled="selectedRescanDisabled"
            :loading="loading"
            :title="selectedRescanDisabledMessage"
            @click="emit('rescanSelected')"
          >
            <template #icon><RefreshCw :size="16" /></template>
            Rescan selected in WUD
          </n-button>
          <n-button
            v-if="selectedCount"
            type="warning"
            size="small"
            secondary
            :disabled="removeSelectedDisabled"
            :loading="loading"
            @click="emit('startRemoval')"
          >
            <template #icon><Trash2 :size="16" /></template>
            {{ removalButtonLabel }}
          </n-button>
        </n-flex>
        <slot name="tools" />
        <p v-if="globalRescanDisabledMessage">{{ globalRescanDisabledMessage }}</p>
        <p v-if="selectedCount && selectedRescanDisabledMessage">{{ selectedRescanDisabledMessage }}</p>
        <p v-if="selectedCount && removeSelectedDisabledMessage">{{ removeSelectedDisabledMessage }}</p>
      </div>
    </details>
    <n-button size="small" quaternary :disabled="!selectableCount" @click="emit('selectAll')">
      <template #icon><Check :size="16" /></template>
      {{ selectAllLabel }}
    </n-button>
  </div>

  <section v-if="pendingLoaded" class="batch-action-bar" aria-label="Review updates">
    <div class="selection-summary" aria-live="polite">
      <strong class="wrap-anywhere">{{ selectedCount ? batchSummaryLabel : 'Select updates to review' }}</strong>
      <span v-if="selectedHiddenCount" class="wrap-anywhere">
        {{ selectedHiddenCount }} selected {{ selectedHiddenCount === 1 ? 'update hidden' : 'updates hidden' }} by search; included in review.
      </span>
      <span v-else class="wrap-anywhere">Review changes nothing. Apply follows in the plan.</span>
    </div>
    <n-flex class="pending-actions" align="center" :size="8">
      <n-button v-if="selectedCount" size="small" quaternary @click="emit('clearSelection')">
        <template #icon><X :size="16" /></template>
        Clear selection
      </n-button>
      <n-button
        type="primary"
        size="small"
        :disabled="!selectedCount || updateSelectedDisabled"
        :loading="loading"
        @click="emit('startUpdate')"
      >
        <template #icon><Play :size="16" /></template>
        {{ updateSelectedButtonLabel }}
      </n-button>
    </n-flex>
  </section>
  <p v-if="pendingLoaded && selectedMetadataWarning" class="selection-warning">{{ selectedMetadataWarning }}</p>
  <p v-if="pendingLoaded && hasSelectedTagUpdates" class="selection-warning">Tag rewrites are confirmed in review before Apply.</p>
</template>

<style scoped>
.pending-actions {
  flex-wrap: wrap;
}

.selection-tools {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.queue-tools {
  min-width: 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.queue-tools summary {
  width: fit-content;
  min-height: 32px;
  align-content: center;
  cursor: pointer;
}

.queue-tools-content {
  display: grid;
  gap: 8px;
  padding-block: 8px;
  max-width: 75ch;
}

.queue-tools p,
.selection-warning {
  margin: 0;
}

.batch-action-bar {
  position: sticky;
  top: 8px;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  box-shadow: var(--shadow-panel-lift);
}

.selection-summary {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.selection-summary strong {
  font-size: var(--text-metadata-size);
}

.selection-summary span,
.selection-warning {
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

@media (--wud-app-shell) {
  .batch-action-bar {
    top: 88px;
    flex-wrap: wrap;
  }
}

@media (--wud-compact) {
  .pending-actions :deep(.n-button),
  .selection-tools :deep(.n-button),
  .queue-tools summary {
    min-height: var(--size-touch-target);
  }
}
</style>
