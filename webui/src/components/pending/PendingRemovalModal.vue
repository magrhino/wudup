<script setup lang="ts">
import { Trash2 } from "@lucide/vue";
import { NAlert, NButton, NFlex, NModal, NTag } from "naive-ui";

import type { PendingRemovalPlanLine } from "../../api/client";
import { pluralize } from "../../views/pending/utils";

defineProps<{
  loading: boolean;
  pendingSourceLabel: string;
  removalConfirmButtonLabel: string;
  removalDisabled: boolean;
  removalItems: PendingRemovalPlanLine[];
  removalLineLabel: (item: PendingRemovalPlanLine) => string;
  show: boolean;
}>();

const emit = defineEmits<{
  (event: "close"): void;
  (event: "confirm"): void;
}>();

function handleModalShowUpdate(value: boolean): void {
  if (!value) {
    emit("close");
  }
}
</script>

<template>
  <n-modal
    :show="show"
    :mask-closable="false"
    @update:show="handleModalShowUpdate"
  >
    <!-- Naive UI requires a div root for keyboard focus trapping. -->
    <div
      class="preflight-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="removal-modal-title"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">Pending removal</p>
          <h2 id="removal-modal-title">Remove selected entries</h2>
          <p class="preflight-summary-text">
            These lines will be removed from {{ pendingSourceLabel }} without running Docker updates.
          </p>
        </div>
        <n-tag type="warning">{{ pluralize(removalItems.length, "entry", "entries") }}</n-tag>
      </div>

      <n-alert class="preflight-block" type="warning">
        This only edits {{ pendingSourceLabel }}. Containers, images, and Compose services are not deleted or updated, and WUD may add these entries again if the updates still exist.
      </n-alert>

      <section class="preflight-impact preflight-block" aria-labelledby="removal-lines-title">
        <div class="preflight-impact-heading">
          <strong id="removal-lines-title">Source lines</strong>
          <n-tag size="small">{{ pluralize(removalItems.length, "line") }}</n-tag>
        </div>
        <div class="compact-list">
          <div
            v-for="item in removalItems"
            :key="`removal-confirm-${item.line_no}`"
            class="list-row plan-line-row"
          >
            <span>Line</span>
            <strong>{{ removalLineLabel(item) }}</strong>
            <em><code>{{ item.raw }}</code></em>
          </div>
        </div>
      </section>

      <n-flex class="preflight-footer" justify="flex-end" :size="8">
        <n-button size="small" quaternary @click="emit('close')">
          Cancel
        </n-button>
        <n-button
          type="warning"
          size="small"
          :disabled="removalDisabled"
          :loading="loading"
          @click="emit('confirm')"
        >
          <template #icon>
            <Trash2 :size="16" />
          </template>
          {{ removalConfirmButtonLabel }}
        </n-button>
      </n-flex>
    </div>
  </n-modal>
</template>
