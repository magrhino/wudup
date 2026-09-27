<script setup lang="ts">
import {
  NAlert,
  NCheckbox,
  NDataTable,
  NInput,
  NTag,
  type DataTableColumns,
  type DataTableRowKey,
} from "naive-ui";

import type { PendingItem, ReleaseNoteInfo } from "../../api/client";
import {
  digestProvenanceDisplay,
  displayDigest,
} from "../../utils/digestProvenance";
import {
  pendingMetadataStatusLabel,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
  rowKey,
  type PendingTagInputProps,
} from "../../views/pending/pendingDisplay";
import type { SafetyCue } from "../../views/pending/safetyCues";
import PendingEmptyQueueState from "./PendingEmptyQueueState.vue";
import PendingReleaseNotes from "./PendingReleaseNotes.vue";

defineProps<{
  columns: DataTableColumns<PendingItem>;
  isMobile: boolean;
  items: PendingItem[];
  latestRunId: number | null;
  loading: boolean;
  pendingSourceLabel: string;
  releaseNoteFor: (item: PendingItem) => ReleaseNoteInfo | null;
  releaseNoteReason: (note: ReleaseNoteInfo | null) => string;
  releaseNoteStatus: (note: ReleaseNoteInfo | null) => string;
  riskCues: (item: PendingItem) => SafetyCue[];
  selectedLineNumbers: number[];
  selectedLineSet: Set<number>;
  showSetupLink: boolean;
  tagInputProps: (item: Pick<PendingItem, "image">) => PendingTagInputProps;
  tagOverrideValue: (item: PendingItem) => string;
}>();

const emit = defineEmits<{
  toggleLine: [lineNo: number, checked: boolean];
  updateCheckedRowKeys: [keys: DataTableRowKey[]];
  updateTag: [item: PendingItem, value: string];
}>();
</script>

<template>
  <n-alert type="info">
    Stack grouping is unavailable. Showing pending file order.
  </n-alert>

  <PendingEmptyQueueState
    v-if="!items.length"
    :latest-run-id="latestRunId"
    :pending-source-label="pendingSourceLabel"
    :show-setup-link="showSetupLink"
  />

  <n-data-table
    v-else-if="!isMobile"
    :columns="columns"
    :data="items"
    :loading="loading"
    :pagination="{ pageSize: 15 }"
    :row-key="rowKey"
    :checked-row-keys="selectedLineNumbers"
    size="small"
    class="data-surface"
    @update:checked-row-keys="emit('updateCheckedRowKeys', $event)"
  />

  <div v-else class="mobile-list">
    <article v-for="item in items" :key="item.line_no" class="mobile-card">
      <div class="mobile-card-title">
        <n-checkbox
          :checked="selectedLineSet.has(item.line_no)"
          @update:checked="emit('toggleLine', item.line_no, Boolean($event))"
        >
          <span class="sr-only">Select update </span>
          <strong>{{ item.image }}</strong>
        </n-checkbox>
        <n-tag size="small">#{{ item.line_no }}</n-tag>
      </div>
      <dl>
        <div>
          <dt>Repository</dt>
          <dd>{{ item.repo }}</dd>
        </div>
        <div>
          <dt>Current tag</dt>
          <dd>{{ item.current_tag || "None" }}</dd>
        </div>
        <div>
          <dt>New tag</dt>
          <dd>
            <n-input
              v-if="item.desired_tag"
              :value="tagOverrideValue(item)"
              size="small"
              class="tag-override-input"
              :placeholder="item.desired_tag"
              :input-props="tagInputProps(item)"
              @update:value="emit('updateTag', item, $event)"
            />
            <span v-else>None</span>
          </dd>
        </div>
        <div>
          <dt>New digest</dt>
          <dd>
            <span
              v-if="digestProvenanceDisplay(item.digest_provenance)"
              class="digest-provenance"
              :title="digestProvenanceDisplay(item.digest_provenance)?.title"
            >
              <span class="digest-provenance-primary">
                {{ digestProvenanceDisplay(item.digest_provenance)?.primary }}
              </span>
              <code
                v-if="digestProvenanceDisplay(item.digest_provenance)?.digest"
                class="digest-value"
              >
                {{ digestProvenanceDisplay(item.digest_provenance)?.digest }}
              </code>
            </span>
            <code v-else-if="item.digest" class="digest-value" :title="item.digest">
              {{ displayDigest(item.digest) }}
            </code>
            <span v-else>None</span>
          </dd>
        </div>
        <div>
          <dt>Metadata</dt>
          <dd>
            <n-tag
              size="small"
              :type="pendingMetadataStatusTagType(item)"
              :title="pendingMetadataStatusTitle(item)"
            >
              {{ pendingMetadataStatusLabel(item) }}
            </n-tag>
          </dd>
        </div>
        <div>
          <dt>Safety cues</dt>
          <dd>
            <div v-if="riskCues(item).length" class="risk-badges-container">
              <n-tag
                v-for="cue in riskCues(item)"
                :key="`${item.line_no}-${cue.key}`"
                size="small"
                :type="cue.type"
                class="safety-badge"
              >
                {{ cue.label }}
              </n-tag>
            </div>
            <span v-else class="risk-badges-muted">None</span>
          </dd>
        </div>
        <div>
          <dt>Release notes</dt>
          <dd>
            <PendingReleaseNotes
              :release-note="releaseNoteFor(item)"
              :release-note-status="releaseNoteStatus(releaseNoteFor(item))"
              :release-note-reason="releaseNoteReason(releaseNoteFor(item))"
            />
          </dd>
        </div>
      </dl>
    </article>
  </div>
</template>
