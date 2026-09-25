<script setup lang="ts">
import { computed } from "vue";
import type { PlanResponse, ReleaseNoteInfo, SecurityScanInfo } from "../../api/client";
import { operationalImpact, reviewEvidence } from "../../views/pending/reviewSummary";

const props = defineProps<{
  plan: PlanResponse;
  releaseNotes: ReleaseNoteInfo[];
  releaseNotesLoading: boolean;
  releaseNotesError: string;
  securityScans: SecurityScanInfo[];
  securityScansLoading: boolean;
  securityScansError: string;
  reasons: string[];
}>();
const scanRequestReason = computed(() => props.securityScansLoading
  ? "Candidate security scan information is loading."
  : props.securityScansError ? `Candidate security scan metadata is unavailable: ${props.securityScansError}` : "");
const evidence = computed(() => reviewEvidence(
  props.plan, props.releaseNotes, scanRequestReason.value ? [] : props.securityScans,
  props.releaseNotesLoading, props.releaseNotesError,
));
const sections = computed(() => [
  { title: "Operational impact", items: props.plan.stacks.map(operationalImpact), empty: "No execution steps are recorded." },
  { title: "Supporting evidence", items: evidence.value.supporting, empty: "No supporting evidence is available for this selection." },
  { title: "Unresolved before apply", items: [...new Set([scanRequestReason.value, ...props.reasons, ...evidence.value.unresolved].filter(Boolean))], empty: "No additional unresolved reasons are reported. Review apply readiness below." },
]);
</script>

<template>
  <section class="review-decision-summary preflight-block" aria-label="Update review decision summary">
    <div v-for="section in sections" :key="section.title" class="review-decision-section">
      <h3>{{ section.title }}</h3>
      <ul v-if="section.items.length">
        <li v-for="item in section.items.slice(0, 3)" :key="item">{{ item }}</li>
      </ul>
      <p v-else>{{ section.empty }}</p>
      <details v-if="section.items.length > 3">
        <summary>Show {{ section.items.length - 3 }} more · {{ section.title.toLowerCase() }}</summary>
        <ul><li v-for="item in section.items.slice(3)" :key="item">{{ item }}</li></ul>
      </details>
      <p v-if="section.title === 'Supporting evidence'" class="review-evidence-limit">
        Release and scan evidence is advisory. Missing evidence does not mean an image is safe.
      </p>
    </div>
    <slot />
  </section>
</template>

<style scoped>
.review-decision-summary { display: grid; gap: 16px; min-width: 0; }
.review-decision-section { min-width: 0; overflow-wrap: anywhere; }
h3 { margin: 0 0 6px; font-size: 0.95rem; }
p, ul { margin: 0; color: var(--color-text-secondary); font-size: 0.84rem; line-height: 1.5; }
ul { padding-left: 20px; }
li + li { margin-top: 6px; }
details { margin-top: 6px; }
summary { cursor: pointer; color: var(--color-action-blue); font-size: 0.84rem; }
details ul { margin-top: 6px; }
.review-evidence-limit { margin-top: 8px; }
</style>
