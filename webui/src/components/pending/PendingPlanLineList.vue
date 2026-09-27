<script setup lang="ts">
import { NTag } from "naive-ui";

import type { ReleaseNoteInfo } from "../../api/client";
import {
  pendingMetadataStatusLabel,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
  releaseNoteReason,
  releaseNoteStatus,
} from "../../views/pending/pendingDisplay";
import {
  planLineDigestPinLabel,
  planLineDigestUnpinLabel,
  planLineServiceLabel,
  planLineTagRewriteLabel,
  type PlanLineView,
} from "../../views/pending/utils";
import PendingReleaseNotes from "./PendingReleaseNotes.vue";

const props = defineProps<{
  planLines: PlanLineView[];
  releaseNotes: ReleaseNoteInfo[];
  releaseNotesError: string;
  releaseNotesLoading: boolean;
  stackCount: number;
}>();

function releaseNoteProps(lineNo: number) {
  const note = props.releaseNotes.find((item) => item.line_no === lineNo) ?? null;
  const lookupError = note ? "" : props.releaseNotesError;
  return {
    releaseNote: note,
    releaseNoteStatus: lookupError ? "Check failed" : releaseNoteStatus(note, props.releaseNotesLoading),
    releaseNoteReason: releaseNoteReason(note) || lookupError,
  };
}
</script>

<template>
  <div v-if="planLines.length" class="compact-list">
    <div
      v-for="{ stack, line } in planLines"
      :key="`${stack}-${line.line_no}-${line.service}`"
      class="list-row plan-line-row"
    >
      <span>#{{ line.line_no }}</span>
      <strong class="plan-line-heading">
        <span>{{ planLineServiceLabel(stackCount, stack, line) }}</span>
        <n-tag
          size="small"
          :type="pendingMetadataStatusTagType(line)"
          :title="pendingMetadataStatusTitle(line)"
        >
          {{ pendingMetadataStatusLabel(line) }} metadata
        </n-tag>
      </strong>
      <em>
        <span v-if="planLineTagRewriteLabel(line)" class="tag-rewrite-detail">
          <n-tag size="small" type="warning">Tag rewrite</n-tag>
          {{ planLineTagRewriteLabel(line) }}
        </span>
        <span
          v-else-if="planLineDigestPinLabel(line)"
          class="tag-rewrite-detail"
        >
          <n-tag size="small" type="info">Digest pin</n-tag>
          {{ planLineDigestPinLabel(line) }}
        </span>
        <span
          v-else-if="planLineDigestUnpinLabel(line)"
          class="tag-rewrite-detail"
        >
          <n-tag size="small" type="info">Digest unpin</n-tag>
          {{ planLineDigestUnpinLabel(line) }}
        </span>
        <template v-else>
          <code>{{ line.compose_image }}</code>
          <span aria-hidden="true"> -> </span>
          <code>{{ line.target_image }}</code>
        </template>
      </em>
      <PendingReleaseNotes class="plan-line-release"
        :candidate-label="`${stack} / ${line.service} · ${line.target_image}`"
        :candidate-tag="line.target_image.split('@')[0]?.split('/').pop()?.split(':')[1] || ''"
        v-bind="releaseNoteProps(line.line_no)"
      />
    </div>
  </div>
  <div v-else class="empty-state">No matched services.</div>
</template>

<style scoped>
.plan-line-release {
  grid-column: 2 / -1;
}

@media (--wud-compact) {
  .plan-line-release {
    grid-column: 1 / -1;
  }
}
</style>
