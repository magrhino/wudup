<script setup lang="ts">
import { ArrowUpCircle } from "@lucide/vue";
import { NButton } from "naive-ui";

defineProps<{
  currentTag?: string;
  latestTag?: string;
  facts: string;
  disabledReason: string;
  buttonDisabled: boolean;
  actionTitle: string;
  actionLabel: string;
}>();

defineEmits<{
  open: [];
}>();
</script>

<template>
  <details class="self-update-banner" aria-label="WUDup self-update">
    <summary class="disclosure-summary disclosure-summary-triangle">
      <ArrowUpCircle :size="18" aria-hidden="true" />
      <span class="wrap-anywhere">
        WUDup update available: {{ currentTag }} &rarr; {{ latestTag }}
      </span>
    </summary>
    <div class="self-update-banner-content">
      <span class="self-update-facts">{{ facts }}</span>
      <div class="self-update-banner-actions">
        <span v-if="disabledReason" class="self-update-disabled">{{ disabledReason }}</span>
        <n-button
          secondary
          size="small"
          :disabled="buttonDisabled"
          :title="actionTitle"
          @click="$emit('open')"
        >
          {{ actionLabel }}
        </n-button>
      </div>
    </div>
  </details>
</template>

<style scoped>
.self-update-banner {
  margin-bottom: 12px;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.self-update-banner summary {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: var(--size-touch-target);
  padding-block: 4px;
  cursor: pointer;
}

.self-update-banner summary svg {
  flex: 0 0 auto;
}

.self-update-banner-content {
  display: grid;
  gap: 8px;
  padding-block: 4px 12px;
}

.self-update-banner-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.self-update-facts,
.self-update-disabled {
  overflow-wrap: anywhere;
}

@media (--wud-compact) {
  .self-update-banner-actions :deep(.n-button) {
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }
}
</style>
