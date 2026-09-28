<script setup lang="ts">
import { ShieldCheck } from "@lucide/vue";
import { NButton, NTag } from "naive-ui";

import type { SafetyCue } from "../../views/pending/safetyCues";

defineProps<{
  canScan: boolean;
  disabled: boolean;
  disabledMessage: string;
  error: string;
  loading: boolean;
  progressLabel: string;
  summaryLabel: string;
  summaryType: SafetyCue["type"];
}>();

const emit = defineEmits<{
  scan: [];
}>();
</script>

<template>
  <div class="candidate-scan-controls">
    <div class="candidate-scan-actions">
      <span aria-live="polite">
        <n-tag size="small" :type="summaryType">{{ summaryLabel }}</n-tag>
      </span>
      <n-button
        v-if="canScan"
        size="small"
        secondary
        :loading="loading"
        :disabled="disabled"
        :title="disabledMessage || 'Scan every candidate image in the queue for known vulnerabilities.'"
        @click="emit('scan')"
      >
        <template #icon>
          <ShieldCheck :size="16" aria-hidden="true" />
        </template>
        Scan candidate images
      </n-button>
    </div>
    <p v-if="progressLabel" class="candidate-scan-status" aria-live="polite">{{ progressLabel }}</p>
    <p v-if="error" class="candidate-scan-status candidate-scan-error" role="alert">
      Candidate security scan metadata is unavailable: {{ error }}
    </p>
  </div>
</template>

<style scoped>
.candidate-scan-controls {
  display: grid;
  justify-items: end;
  gap: 4px;
  min-width: 0;
}

.candidate-scan-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.candidate-scan-status {
  margin: 0;
  max-width: 60ch;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
  text-align: end;
}

.candidate-scan-error {
  color: var(--color-warning-fg);
}

@media (--wud-compact) {
  .candidate-scan-controls {
    justify-items: start;
  }

  .candidate-scan-actions {
    justify-content: flex-start;
  }

  .candidate-scan-actions :deep(.n-button) {
    min-height: var(--size-touch-target);
  }

  .candidate-scan-status {
    text-align: start;
  }
}
</style>
