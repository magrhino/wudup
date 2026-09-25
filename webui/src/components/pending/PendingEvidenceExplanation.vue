<script setup lang="ts">
import type { SafetyCue } from "../../views/pending/safetyCues";

defineProps<{ cues: SafetyCue[] }>();
</script>

<template>
  <p
    v-if="cues.some((cue) => cue.key === 'verified-security' || cue.key.startsWith('security-'))"
    class="evidence-explanation"
  >
    Release advisories use published vulnerability evidence; candidate scans inspect
    the proposed image. Both are advisory checks, not a guarantee of a safe update.
  </p>
</template>

<style scoped>
.evidence-explanation {
  margin: 0;
  min-width: 0;
  max-width: 75ch;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
  line-height: 1.4;
}
</style>
