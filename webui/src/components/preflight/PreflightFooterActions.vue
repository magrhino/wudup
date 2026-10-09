<script setup lang="ts">
import { NButton, NFlex } from "naive-ui";

defineProps<{
  primaryLabel?: string;
  primaryDisabled?: boolean;
  primaryLoading?: boolean;
  // Vue renders slot fallback when the slot only yields a v-if comment, so hiding needs an explicit prop.
  hidePrimary?: boolean;
  // Shown beside the primary action, e.g. why it is disabled.
  primaryReason?: string;
  primaryReasonId?: string;
  secondaryLabel?: string;
}>();

const emit = defineEmits<{
  primary: [];
  secondary: [];
}>();
</script>

<template>
  <n-flex class="preflight-footer" justify="flex-end" align="center" :size="8">
    <p v-if="primaryReason" :id="primaryReasonId" class="preflight-footer-reason">
      {{ primaryReason }}
    </p>
    <slot name="secondary">
      <n-button size="small" quaternary @click="$emit('secondary')">
        {{ secondaryLabel ?? "Close" }}
      </n-button>
    </slot>
    <slot />
    <slot v-if="!hidePrimary" name="primary">
      <n-button
        type="primary"
        size="small"
        :disabled="primaryDisabled ?? false"
        :loading="primaryLoading ?? false"
        @click="$emit('primary')"
      >
        {{ primaryLabel ?? "Apply" }}
      </n-button>
    </slot>
  </n-flex>
</template>

<style scoped>
.preflight-footer-reason {
  flex: 1 1 12rem;
  min-width: 0;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 0.84rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
</style>
