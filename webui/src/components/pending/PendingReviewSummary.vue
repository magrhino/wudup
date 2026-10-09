<script setup lang="ts">
import { AlertTriangle } from "@lucide/vue";
import { computed } from "vue";
import type { PlanResponse, ReleaseNoteInfo, SecurityScanInfo } from "../../api/client";
import { impactSummary, operationalImpact, reviewEvidence } from "../../views/pending/reviewSummary";

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
const scanRequestGap = computed(() => props.securityScansError
  ? `Candidate security scan metadata is unavailable: ${props.securityScansError}` : "");
// While the scan request is loading or failed, report it once instead of a per-line "no scan" gap.
const evidence = computed(() => reviewEvidence(
  props.plan, props.releaseNotes, props.securityScansLoading || scanRequestGap.value ? null : props.securityScans,
  props.releaseNotesLoading, props.releaseNotesError,
));
// Only items that could change the apply decision are shown by default.
const attention = computed(() => [...new Set([
  props.securityScansLoading ? "Candidate security scan information is loading." : "",
  ...props.reasons,
  ...evidence.value.unresolved,
].filter(Boolean))]);
const impact = computed(() => props.plan.stacks.map(stack => impactSummary(stack, props.plan.stacks.length > 1)));
const missingEvidence = computed(() => [scanRequestGap.value, ...evidence.value.gaps].filter(Boolean));
const evidenceSections = computed(() => [
  { title: "Found", items: evidence.value.supporting },
  { title: "Not available", items: missingEvidence.value },
  { title: "Planned steps", items: props.plan.stacks.map(operationalImpact) },
].filter(section => section.items.length));
</script>

<template>
  <section class="review-decision-summary preflight-block" aria-label="Update review decision summary">
    <div v-if="attention.length" class="review-attention">
      <h3>Check before applying</h3>
      <ul>
        <li v-for="item in attention" :key="item">
          <AlertTriangle :size="16" aria-hidden="true" />
          <span>{{ item }}</span>
        </li>
      </ul>
    </div>
    <p v-for="line in impact" :key="line" class="review-impact">{{ line }}</p>
    <slot />
    <details class="review-evidence">
      <summary>
        Evidence details<template v-if="missingEvidence.length"> · {{ missingEvidence.length }} not available</template>
      </summary>
      <div v-for="section in evidenceSections" :key="section.title" class="review-evidence-section">
        <h4>{{ section.title }}</h4>
        <ul><li v-for="item in section.items" :key="item">{{ item }}</li></ul>
      </div>
      <p class="review-evidence-limit">
        Release and scan evidence is advisory. Missing evidence does not mean an image is safe.
      </p>
    </details>
  </section>
</template>

<style scoped>
.review-decision-summary { display: grid; gap: 12px; min-width: 0; }
.review-attention, .review-evidence { min-width: 0; overflow-wrap: anywhere; }
h3 { margin: 0 0 6px; font-size: 0.95rem; }
.review-attention ul { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; line-height: 1.45; }
.review-attention li { display: flex; align-items: flex-start; gap: 8px; }
.review-attention svg { flex: 0 0 auto; margin-top: 2px; color: var(--color-warning-fg); }
.review-impact { margin: 0; color: var(--color-text-secondary); line-height: 1.45; }
summary { cursor: pointer; color: var(--color-action-blue); font-size: 0.84rem; }
.review-evidence-section { margin-top: 10px; }
h4 { margin: 0 0 4px; font-size: 0.84rem; }
.review-evidence ul, .review-evidence-limit { margin: 0; color: var(--color-text-secondary); font-size: 0.84rem; line-height: 1.5; }
.review-evidence ul { padding-left: 20px; }
.review-evidence li + li { margin-top: 4px; }
.review-evidence-limit { margin-top: 10px; }
</style>
