<script setup lang="ts">
import type { Component } from "vue";
import { LogOut, RefreshCw } from "@lucide/vue";
import { NButton, NFlex } from "naive-ui";

defineProps<{
  title: string;
  themeButtonTitle: string;
  themeButtonAriaLabel: string;
  themePreferenceIcon: Component;
}>();

defineEmits<{
  "cycle-theme": [];
  refresh: [];
  logout: [];
}>();
</script>

<template>
  <header class="topbar">
    <div>
      <h1>{{ title }}</h1>
    </div>
    <n-flex class="topbar-actions" align="center" :size="8">
      <n-button
        quaternary
        circle
        :title="themeButtonTitle"
        :aria-label="themeButtonAriaLabel"
        @click="$emit('cycle-theme')"
      >
        <template #icon>
          <component :is="themePreferenceIcon" :size="18" />
        </template>
      </n-button>
      <n-button
        quaternary
        circle
        title="Refresh status for the current view; does not request a WUD rescan or security scan."
        aria-label="Refresh current view status"
        @click="$emit('refresh')"
      >
        <template #icon>
          <RefreshCw :size="18" />
        </template>
      </n-button>
      <n-button quaternary title="Sign out" @click="$emit('logout')">
        <template #icon>
          <LogOut :size="18" />
        </template>
        Sign out
      </n-button>
    </n-flex>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.topbar h1 {
  margin: 0;
  color: var(--color-ink);
  font-size: var(--text-title-size);
  font-weight: 700;
  line-height: 1.2;
}

@media (--wud-app-shell) {
  .topbar {
    align-items: flex-start;
  }
}

@media (--wud-compact) {
  .topbar {
    display: grid;
  }

  .topbar-actions :deep(.n-button) {
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }

  .topbar-actions :deep(.n-button--circle) {
    min-width: var(--size-touch-target);
  }
}
</style>
