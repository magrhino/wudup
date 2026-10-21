<script setup lang="ts">
import { RotateCw } from "@lucide/vue";
import { NTag } from "naive-ui";
import { computed } from "vue";

import type { PlanStack, ReleaseNoteInfo } from "../../api/client";
import {
  pendingMetadataStatusLabel,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
  releaseNoteReason,
  releaseNoteStatus,
} from "../../views/pending/pendingDisplay";
import { stackImpact, stackImpactIsNotable, type StackImpact } from "../../views/pending/reviewSummary";
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
  // When set, a stack that interrupts more than its updated services gets an impact row after its last service.
  stacks?: PlanStack[];
}>();

type ListRow =
  | { kind: "line"; view: PlanLineView }
  | { kind: "impact"; key: string; stack: string; impact: StackImpact };

const rows = computed<ListRow[]>(() => {
  if (!props.stacks) return props.planLines.map((view): ListRow => ({ kind: "line", view }));
  // Group by stack position, not name: two Compose stacks can share a directory name.
  return props.stacks.flatMap((stack, index): ListRow[] => {
    const lines = stack.lines.map((line): ListRow => ({ kind: "line", view: { stack: stack.name, line } }));
    const impact = stackImpact(stack);
    return impact && lines.length && stackImpactIsNotable(impact)
      ? [...lines, { kind: "impact", key: `impact-${index}`, stack: stack.name, impact }]
      : lines;
  });
});

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
    <template
      v-for="row in rows"
      :key="row.kind === 'line'
        ? `${row.view.stack}-${row.view.line.line_no}-${row.view.line.service}`
        : row.key"
    >
      <div
        v-if="row.kind === 'impact'"
        class="list-row plan-line-row plan-impact-row"
        :aria-label="`${row.stack} stack impact`"
      >
        <span class="plan-impact-icon"><RotateCw :size="16" aria-hidden="true" /></span>
        <strong class="plan-line-heading">
          <span>{{ row.stack }}</span>
          <n-tag v-if="row.impact.wholeStack" size="small" type="warning">Whole stack</n-tag>
          <n-tag v-if="row.impact.mode === 'force-recreate'" size="small" type="warning">Force recreate</n-tag>
          <n-tag v-if="row.impact.mode === 'pause'" size="small" type="info">Paused</n-tag>
        </strong>
        <em class="plan-impact-detail">
          <template v-if="row.impact.alsoRestarted.length">
            <span>{{ row.impact.mode === "pause" ? "Also paused" : "Also restarts" }}</span>
            <n-tag v-for="service in row.impact.alsoRestarted" :key="service" size="small" type="warning">
              {{ service }}
            </n-tag>
          </template>
          <span v-else-if="row.impact.wholeStack">Every service in this stack restarts</span>
          <span v-else-if="row.impact.mode === 'pause'">Paused during the update instead of recreated</span>
          <span v-else-if="row.impact.mode === 'force-recreate'">Recreated even if unchanged</span>
          <span v-if="row.impact.dependenciesMayStart">Dependencies may also start</span>
        </em>
      </div>
      <div
        v-else
        class="list-row plan-line-row"
      >
        <span>#{{ row.view.line.line_no }}</span>
        <strong class="plan-line-heading">
          <span>{{ planLineServiceLabel(stackCount, row.view.stack, row.view.line) }}</span>
          <n-tag
            size="small"
            :type="pendingMetadataStatusTagType(row.view.line)"
            :title="pendingMetadataStatusTitle(row.view.line)"
          >
            {{ pendingMetadataStatusLabel(row.view.line) }} metadata
          </n-tag>
        </strong>
        <em>
          <span v-if="planLineTagRewriteLabel(row.view.line)" class="tag-rewrite-detail">
            <n-tag size="small" type="warning">Tag rewrite</n-tag>
            {{ planLineTagRewriteLabel(row.view.line) }}
          </span>
          <span
            v-else-if="planLineDigestPinLabel(row.view.line)"
            class="tag-rewrite-detail"
          >
            <n-tag size="small" type="info">Digest pin</n-tag>
            {{ planLineDigestPinLabel(row.view.line) }}
          </span>
          <span
            v-else-if="planLineDigestUnpinLabel(row.view.line)"
            class="tag-rewrite-detail"
          >
            <n-tag size="small" type="info">Digest unpin</n-tag>
            {{ planLineDigestUnpinLabel(row.view.line) }}
          </span>
          <template v-else>
            <code>{{ row.view.line.compose_image }}</code>
            <span aria-hidden="true"> -> </span>
            <code>{{ row.view.line.target_image }}</code>
          </template>
        </em>
        <PendingReleaseNotes class="plan-line-release"
          :candidate-label="`${row.view.stack} / ${row.view.line.service} · ${row.view.line.target_image}`"
          :candidate-tag="row.view.line.target_image.split('@')[0]?.split('/').pop()?.split(':')[1] || ''"
          v-bind="releaseNoteProps(row.view.line.line_no)"
        />
      </div>
    </template>
  </div>
  <div v-else class="empty-state">No matched services.</div>
</template>

<style scoped>
.plan-line-release {
  grid-column: 2 / -1;
}

.plan-impact-row {
  border-color: color-mix(in srgb, var(--color-warning-fg) 35%, var(--color-border-subtle));
  background: color-mix(in srgb, var(--color-warning-bg) 60%, transparent);
}

.plan-line-row .plan-impact-detail > span {
  color: var(--color-warning-fg);
}

.plan-impact-icon {
  display: inline-flex;
  color: var(--color-warning-fg);
}

.plan-line-row em.plan-impact-detail {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

@media (--wud-compact) {
  .plan-line-release {
    grid-column: 1 / -1;
  }
}
</style>
