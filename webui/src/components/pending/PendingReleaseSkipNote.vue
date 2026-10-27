<script setup lang="ts">
import { History } from "@lucide/vue";

import type { PendingReleaseSkip } from "../../api/client";
import { releaseSkipCueText } from "../../views/pending/releaseSkips";

defineProps<{
  skip: PendingReleaseSkip;
}>();
</script>

<template>
  <p class="release-skip-note wrap-anywhere">
    <History :size="14" aria-hidden="true" />
    <span>{{ releaseSkipCueText(skip) }}</span>
  </p>
</template>

<style scoped>
.release-skip-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  width: fit-content;
  max-width: 100%;
  margin: 0;
  padding: 5px 10px;
  border-radius: 7px;
  background: var(--color-warning-bg);
  color: var(--color-warning-fg);
  font-size: 0.82rem;
  line-height: 1.4;
}

.release-skip-note svg {
  flex: none;
  margin-top: 2px;
}
</style>
