<script setup lang="ts">
import { computed } from "vue";
import { useTimeAgo } from "@vueuse/core";
import { CircleCheck, AlertTriangle } from "@lucide/vue";
import type { RunSummary } from "../api/client";
import { resultSignals, runChanges, runAction } from "../utils/updateSummary";
import UpdateScopeSummary from "./UpdateScopeSummary.vue";

const props = defineProps<{ run: RunSummary; compact?: boolean }>();
const changes = computed(() => runChanges(props.run));
const signals = computed(() => resultSignals(props.run));
const timestamp = computed(() => props.run.finished_at || props.run.started_at);
const validTime = computed(() => Number.isFinite(Date.parse(timestamp.value)));
const timeAgo = useTimeAgo(timestamp);
</script>

<template>
  <section class="run-result-summary" :class="{ compact }" aria-label="Update result summary">
    <UpdateScopeSummary :changes="changes" :fallback="`${runAction(run)} · Run #${run.id}`" :compact="compact" />
    <div class="result-evidence">
      <span v-for="signal in signals" :key="signal.text" class="result-signal" :class="signal.tone">
        <CircleCheck v-if="signal.tone === 'success'" :size="15" aria-hidden="true" />
        <AlertTriangle v-else-if="signal.tone === 'warning'" :size="15" aria-hidden="true" />
        {{ signal.text }}
      </span>
      <time v-if="validTime" :datetime="timestamp" :title="timestamp">{{ run.finished_at ? '' : 'Started ' }}{{ timeAgo }}</time>
      <span v-else>Time not recorded</span>
    </div>
    <p v-if="!compact" class="result-context">
      {{ run.dry_run ? 'Planned image changes' : 'Recorded image targets' }} · Run #{{ run.id }}
    </p>
    <details v-if="!compact" class="result-times">
      <summary>Exact timestamps</summary>
      <dl>
        <div><dt>Started</dt><dd><time :datetime="run.started_at">{{ run.started_at || 'Not recorded' }}</time></dd></div>
        <div><dt>Finished</dt><dd><time v-if="run.finished_at" :datetime="run.finished_at">{{ run.finished_at }}</time><span v-else>Not recorded yet</span></dd></div>
      </dl>
    </details>
  </section>
</template>

<style scoped>
.run-result-summary { min-width: 0; padding: 18px 0; border-block: 1px solid var(--color-border); }
.run-result-summary.compact { padding: 0; border: 0; }
.result-evidence { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 8px; font-size: var(--text-metadata-size); }
.result-signal { display: inline-flex; align-items: center; gap: 5px; }
.result-signal svg { flex-shrink: 0; }
.success { color: var(--color-operational-teal); }
.warning { color: var(--color-warning-fg); }
time, .result-context { color: var(--color-muted-text); }
.result-context { margin: 8px 0 0; font-size: var(--text-metadata-size); }
.result-times { margin-top: 8px; font-size: var(--text-metadata-size); }
.result-times summary { cursor: pointer; color: var(--color-action-blue); }
.result-times dl { display: flex; flex-wrap: wrap; gap: 8px 24px; margin: 8px 0 0; }
.result-times dt { color: var(--color-muted-text); }
.result-times dd { margin: 0; overflow-wrap: anywhere; }
</style>
