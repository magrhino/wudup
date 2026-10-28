<script setup lang="ts">
import { computed, ref } from "vue";
import { Check, CheckCircle2, ChevronDown, ChevronUp, Play, Send, X } from "@lucide/vue";
import { NAlert, NButton, NFlex, NTag } from "naive-ui";

import type {
  ApplyJobLogResponse,
  ApplyJobResponse,
  RunVerificationSummary,
} from "../../api/client";
import RunVerificationPanel from "../RunVerificationPanel.vue";
import type {
  ApplyJobPlanSnapshot,
  ApplyJobProgressStep,
} from "../../views/pending/usePendingApplyJob";
import { pluralize } from "../../views/pending/utils";

type TagType = "default" | "error" | "info" | "success" | "warning";

const props = withDefaults(defineProps<{
  active: boolean;
  alertType: TagType;
  impactLabel: string;
  latestLogMessage: string;
  liveLogExpanded: boolean;
  liveLogToggleLabel: string;
  liveLogVisible: boolean;
  log: ApplyJobLogResponse | null;
  logEmptyMessage: string;
  logText: string;
  logTitle: string;
  logWaiting: boolean;
  nowDescriptionIds: string;
  nowDetail: string;
  nowMessage: string;
  nowStatusLabel: string;
  nowTitle: string;
  panelStatusLabel: string;
  progressSteps: ApplyJobProgressStep[];
  progressSummary: string;
  releaseNotificationsDisabled?: boolean;
  releaseNotificationsDisabledMessage?: string;
  releaseNotificationsLoading?: boolean;
  releaseNotificationsVisible?: boolean;
  snapshot: ApplyJobPlanSnapshot | null;
  startedLabel: string;
  statusMessage: string;
  succeeded: boolean;
  title: string;
  updateLabel: string;
  verification: RunVerificationSummary;
  job: ApplyJobResponse;
}>(), {
  releaseNotificationsDisabled: true,
  releaseNotificationsDisabledMessage: "",
  releaseNotificationsLoading: false,
  releaseNotificationsVisible: false,
});

const emit = defineEmits<{
  (event: "update:liveLogExpanded", value: boolean): void;
  (event: "preview-release-notes"): void;
}>();

const applyJobPanelRef = ref<HTMLElement | null>(null);
const applyJobPanelLogRef = ref<HTMLElement | null>(null);
const currentProgressStepKey = computed(
  () => props.progressSteps.find((step) => step.status === "running")?.key ?? "",
);

defineExpose({
  focusPanel,
  logElement,
});

function focusPanel(behavior: ScrollBehavior): void {
  const panel = applyJobPanelRef.value;
  if (!panel) {
    return;
  }
  panel.scrollIntoView?.({
    block: "start",
    behavior,
  });
  panel.focus({ preventScroll: true });
}

function logElement(): HTMLElement | null {
  return applyJobPanelLogRef.value;
}

function progressTagType(
  status: ApplyJobProgressStep["status"],
): "default" | "info" | "success" | "error" {
  if (status === "success") {
    return "success";
  }
  if (status === "running") {
    return "info";
  }
  if (status === "failure") {
    return "error";
  }
  return "default";
}

function snapshotLineScope(
  line: ApplyJobPlanSnapshot["lines"][number],
): string {
  return line.scopeLabel || line.stackName || "Apply";
}
</script>

<template>
  <section
    ref="applyJobPanelRef"
    class="section-panel apply-job-panel"
    :class="{
      'apply-job-panel-active': active,
      'apply-job-panel-success': succeeded,
    }"
    tabindex="-1"
    aria-labelledby="apply-job-panel-title"
  >
    <div class="section-heading apply-job-heading">
      <div>
        <p class="eyebrow">Apply job</p>
        <div class="apply-job-heading-title">
          <span v-if="succeeded" class="apply-job-complete-mark" aria-hidden="true">
            <CheckCircle2 :size="18" />
          </span>
          <h2 id="apply-job-panel-title" class="wrap-anywhere">{{ title }}</h2>
        </div>
        <output class="apply-job-summary" aria-live="polite">
          {{ statusMessage }}
        </output>
      </div>
      <n-tag :type="alertType">{{ panelStatusLabel }}</n-tag>
    </div>

    <div v-if="active" class="apply-job-progress" aria-hidden="true">
      <span />
    </div>

    <output
      id="apply-job-panel-status"
      class="apply-job-now"
      :class="{
        'apply-job-now-success': job.status === 'success',
        'apply-job-now-failure': job.status === 'failure' && alertType === 'error',
        'apply-job-now-warning': job.status === 'failure' && alertType === 'warning',
      }"
      aria-live="polite"
      aria-atomic="true"
      tabindex="-1"
      aria-labelledby="apply-job-now-title"
      :aria-describedby="nowDescriptionIds"
    >
      <span class="apply-job-now-copy">
        <span>Current status</span>
        <strong id="apply-job-now-title" class="wrap-anywhere">{{ nowTitle }}</strong>
        <em id="apply-job-now-message" class="wrap-anywhere">{{ nowMessage }}</em>
        <small v-if="nowDetail" id="apply-job-now-detail" class="wrap-anywhere">
          {{ nowDetail }}
        </small>
      </span>
      <n-tag size="small" :type="alertType">{{ nowStatusLabel }}</n-tag>
    </output>

    <section class="apply-job-latest-log" aria-labelledby="apply-job-latest-log-title">
      <span id="apply-job-latest-log-title">Latest log line</span>
      <code class="wrap-anywhere">{{ latestLogMessage }}</code>
    </section>

    <section class="apply-job-progress-steps" aria-labelledby="apply-job-progress-title">
      <n-flex class="panel-subheading" align="center" justify="space-between" :size="8">
        <strong id="apply-job-progress-title">Update progress</strong>
        <n-tag size="small">{{ progressSummary }}</n-tag>
      </n-flex>
      <ol class="apply-progress-list">
        <li
          v-for="step in progressSteps"
          :key="step.key"
          class="apply-progress-step"
          :class="`apply-progress-step-${step.status}`"
          :aria-current="step.key === currentProgressStepKey ? 'step' : undefined"
        >
          <span class="apply-progress-icon" aria-hidden="true">
            <Check v-if="step.status === 'success'" :size="14" />
            <X v-else-if="step.status === 'failure'" :size="14" />
            <Play v-else-if="step.status === 'running'" :size="14" />
          </span>
          <span class="apply-progress-copy">
            <strong>{{ step.label }}</strong>
            <span class="wrap-anywhere">{{ step.message }}</span>
            <em v-if="step.detail" class="wrap-anywhere">{{ step.detail }}</em>
          </span>
          <n-tag size="small" :type="progressTagType(step.status)">
            {{ step.statusLabel }}
          </n-tag>
        </li>
      </ol>
    </section>

    <RunVerificationPanel
      v-if="!active"
      :verification="verification"
      title="Post-update verification"
    />

    <section
      v-if="releaseNotificationsVisible"
      class="apply-job-release-notes"
      aria-labelledby="apply-job-release-notes-title"
    >
      <span id="apply-job-release-notes-title">Release notes</span>
      <code class="wrap-anywhere">
        {{ releaseNotificationsDisabledMessage || "Preview and send Discord release-note notifications for this completed run." }}
      </code>
      <n-flex class="apply-job-run-links" align="center" :size="8">
        <n-button
          size="small"
          secondary
          :disabled="releaseNotificationsDisabled"
          :loading="releaseNotificationsLoading"
          :title="releaseNotificationsDisabledMessage"
          @click="emit('preview-release-notes')"
        >
          <template #icon>
            <Send :size="16" />
          </template>
          Preview release notes
        </n-button>
      </n-flex>
    </section>

    <details class="apply-job-details" :open="!active">
      <summary class="disclosure-summary disclosure-summary-triangle">
        <span>Applied scope</span>
        <n-tag size="small">{{ impactLabel || updateLabel }}</n-tag>
      </summary>
      <div class="apply-job-grid">
        <div class="compact-list">
          <div class="list-row">
            <span>Updates</span>
            <strong>{{ updateLabel }}</strong>
            <em>{{ startedLabel }}</em>
          </div>
          <div v-if="impactLabel" class="list-row">
            <span>Impact</span>
            <strong>{{ impactLabel }}</strong>
            <em>{{ snapshot?.sourceFile }}</em>
          </div>
          <div v-if="job.run_id" class="list-row">
            <span>Run</span>
            <strong>#{{ job.run_id }}</strong>
            <n-flex class="apply-job-run-links" align="center" :size="8">
              <RouterLink
                class="text-link"
                :to="{ name: 'run-detail', params: { id: job.run_id } }"
              >
                Details
              </RouterLink>
              <RouterLink
                class="text-link"
                :to="{ name: 'run-log', params: { id: job.run_id } }"
              >
                Log
              </RouterLink>
            </n-flex>
          </div>
        </div>

        <section
          class="apply-job-impact"
          aria-labelledby="apply-job-impact-title"
        >
          <n-flex class="panel-subheading" align="center" justify="space-between" :size="8">
            <strong id="apply-job-impact-title">Services and images</strong>
            <n-tag size="small">{{ pluralize(snapshot?.lines.length ?? 0, "service") }}</n-tag>
          </n-flex>
          <div v-if="snapshot?.lines.length" class="compact-list">
            <div
              v-for="line in snapshot.lines"
              :key="line.key"
              class="list-row plan-line-row"
            >
              <span>{{ snapshotLineScope(line) }}</span>
              <strong>{{ line.serviceLabel }}</strong>
              <em>
                <span v-if="line.tagRewriteLabel" class="tag-rewrite-detail">
                  <n-tag size="small" type="warning">Tag rewrite</n-tag>
                  {{ line.tagRewriteLabel }}
                </span>
                <span v-else-if="line.digestPinLabel" class="tag-rewrite-detail">
                  <n-tag size="small" type="info">Digest pin</n-tag>
                  {{ line.digestPinLabel }}
                </span>
                <template v-else>
                  <code>{{ line.composeImage }}</code>
                  <span aria-hidden="true"> -> </span>
                  <code>{{ line.targetImage }}</code>
                </template>
              </em>
            </div>
          </div>
          <div v-else class="empty-state">
            Plan details are unavailable after page reload.
          </div>
        </section>
      </div>
    </details>

    <section class="apply-job-live-log" aria-labelledby="apply-job-log-title">
      <n-flex
        class="panel-subheading apply-job-live-log-heading"
        align="flex-start"
        justify="space-between"
        :size="8"
      >
        <div class="apply-job-log-heading-copy">
          <strong id="apply-job-log-title">Live log</strong>
          <span class="apply-job-log-note">Raw command output</span>
          <span class="apply-job-log-path wrap-anywhere">{{ logTitle }}</span>
        </div>
        <n-button
          v-show="!active"
          class="apply-job-log-toggle"
          size="small"
          secondary
          :aria-expanded="liveLogExpanded"
          :title="liveLogToggleLabel"
          @click="emit('update:liveLogExpanded', !liveLogExpanded)"
        >
          <template #icon>
            <ChevronUp v-if="liveLogExpanded" :size="16" />
            <ChevronDown v-else :size="16" />
          </template>
          {{ liveLogExpanded ? "Hide output" : "Show output" }}
        </n-button>
      </n-flex>
      <div v-show="liveLogVisible" class="apply-job-live-log-body">
        <n-alert
          v-if="log?.truncated"
          class="preflight-block"
          type="warning"
          :show-icon="false"
        >
          Showing the last {{ log.max_bytes }} bytes.
        </n-alert>
        <n-alert
          v-if="log?.error"
          class="preflight-block"
          type="warning"
          :show-icon="false"
        >
          Live log unavailable: {{ log.error }}
        </n-alert>
        <div v-if="logWaiting" class="empty-state">
          {{ logEmptyMessage }}
        </div>
        <pre
          v-else-if="!log?.error"
          ref="applyJobPanelLogRef"
          class="log-viewer apply-job-log-viewer"
        >{{ logText }}</pre>
      </div>
    </section>

    <n-alert
      v-if="job.error"
      class="plan-section"
      :type="alertType === 'warning' ? 'warning' : 'error'"
    >
      {{ job.error }}
    </n-alert>
  </section>
</template>

<style scoped>
.apply-job-panel {
  display: grid;
  gap: 10px;
  scroll-margin-top: 88px;
  transition:
    border-color var(--motion-base) var(--ease-out-quart),
    background-color var(--motion-base) var(--ease-out-quart);
}

.apply-job-panel-active {
  position: sticky;
  top: 12px;
  z-index: 8;
  border-color: var(--color-border-hover);
}

.apply-job-panel-success {
  border-color: color-mix(in srgb,
      var(--color-border) 72%,
      var(--color-operational-teal) 28%);
  background: color-mix(in srgb,
      var(--color-surface) 97%,
      var(--color-operational-teal) 3%);
}

.apply-job-panel:focus-visible,
.apply-job-now:focus-visible {
  outline: 2px solid var(--color-border-hover);
  outline-offset: 3px;
}

.apply-job-heading {
  align-items: center;
}

.apply-job-heading-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.apply-job-complete-mark {
  display: inline-grid;
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  place-items: center;
  border-radius: 999px;
  color: var(--color-operational-teal);
  background: color-mix(in srgb,
      var(--color-surface) 82%,
      var(--color-operational-teal) 18%);
  animation: apply-complete-pop 240ms var(--ease-out-quint);
}

.apply-job-summary {
  display: block;
  max-width: 68ch;
  margin: 6px 0 0;
  color: var(--color-muted-text);
  font-size: var(--text-body-size);
}

.apply-job-progress {
  position: relative;
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--color-border-subtle);
}

.apply-job-progress span {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -42%;
  width: 42%;
  border-radius: inherit;
  background: var(--color-operational-teal);
  animation: apply-job-progress 1.35s ease-in-out infinite;
}

.apply-job-now {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 12px;
  border: 1px solid color-mix(in srgb,
      var(--color-border-hover) 62%,
      var(--color-operational-teal) 38%);
  border-radius: 7px;
  background: color-mix(in srgb,
      var(--color-surface) 92%,
      var(--color-operational-teal) 8%);
  transition:
    border-color var(--motion-base) var(--ease-out-quart),
    background-color var(--motion-base) var(--ease-out-quart);
}

.apply-job-now-success {
  border-color: color-mix(in srgb,
      var(--color-border-hover) 70%,
      var(--color-operational-teal) 30%);
  background: color-mix(in srgb,
      var(--color-surface) 96%,
      var(--color-operational-teal) 4%);
}

.apply-job-now-failure {
  border-color: color-mix(in srgb,
      var(--color-border) 54%,
      var(--color-error-fg) 46%);
  background: color-mix(in srgb,
      var(--color-surface) 90%,
      var(--color-error-fg) 10%);
}

.apply-job-now-warning {
  border-color: color-mix(in srgb,
      var(--color-border) 54%,
      var(--color-warning-fg) 46%);
  background: var(--color-warning-bg);
}

.apply-job-now-copy {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.apply-job-now-copy span {
  color: var(--color-muted-text);
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
}

.apply-job-now-copy strong {
  color: var(--color-ink);
  font-size: 1rem;
  line-height: 1.25;
}

.apply-job-now-copy em,
.apply-job-now-copy small {
  color: var(--color-text-secondary);
  font-size: var(--text-metadata-size);
  font-style: normal;
  line-height: 1.35;
}

.apply-job-latest-log,
.apply-job-release-notes {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-surface);
}

.apply-job-latest-log span,
.apply-job-release-notes span {
  color: var(--color-muted-text);
  font-size: 0.78rem;
  font-weight: 700;
}

.apply-job-latest-log code,
.apply-job-release-notes code {
  color: var(--color-code-text);
  font-size: 0.82rem;
  line-height: 1.45;
  white-space: pre-wrap;
}

.apply-job-details {
  min-width: 0;
  padding: 2px 0 0;
}

.apply-job-details .disclosure-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}

.apply-job-details .disclosure-summary span {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--color-ink);
  font-weight: 700;
}

.apply-job-details[open] .disclosure-summary {
  margin-bottom: 8px;
}

.apply-job-grid {
  display: grid;
  grid-template-columns: minmax(220px, 0.8fr) minmax(0, 1.2fr);
  gap: 12px;
  align-items: start;
}

.apply-job-impact {
  display: grid;
  gap: 8px;
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-panel-tint);
}

.apply-job-impact .compact-list {
  margin-top: 0;
}

.apply-job-progress-steps {
  display: grid;
  gap: 10px;
  min-width: 0;
  padding: 10px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-surface);
}

.apply-progress-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.apply-progress-step {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  min-height: 0;
  padding: 8px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-panel-tint);
  transition:
    border-color var(--motion-base) var(--ease-out-quart),
    background-color var(--motion-base) var(--ease-out-quart);
}

.apply-progress-step-running {
  border-color: color-mix(in srgb,
      var(--color-border-hover) 58%,
      var(--color-operational-teal) 42%);
  background: color-mix(in srgb,
      var(--color-surface) 92%,
      var(--color-operational-teal) 8%);
}

.apply-progress-step-skipped {
  background: var(--color-surface);
}

.apply-progress-step-success {
  border-color: color-mix(in srgb,
      var(--color-operational-teal) 42%,
      var(--color-border) 58%);
  background: color-mix(in srgb,
      var(--color-surface) 94%,
      var(--color-operational-teal) 6%);
}

.apply-progress-step-failure {
  border-color: color-mix(in srgb,
      var(--color-border) 52%,
      var(--color-error-fg) 48%);
  background: color-mix(in srgb,
      var(--color-surface) 90%,
      var(--color-error-fg) 10%);
}

.apply-progress-icon {
  position: relative;
  display: inline-grid;
  width: 22px;
  height: 22px;
  place-items: center;
  border: 1px solid var(--color-border-dashed);
  border-radius: 999px;
  color: var(--color-muted-text);
  background: var(--color-surface);
}

.apply-progress-step-running .apply-progress-icon {
  border-color: var(--color-operational-teal);
  color: var(--color-operational-teal);
}

.apply-progress-step-success .apply-progress-icon {
  border-color: color-mix(in srgb,
      var(--color-operational-teal) 64%,
      var(--color-border) 36%);
  color: var(--color-operational-teal);
  background: color-mix(in srgb,
      var(--color-surface) 82%,
      var(--color-operational-teal) 18%);
  animation: apply-stack-complete 220ms var(--ease-out-quint);
}

.apply-progress-step-success .apply-progress-icon::after {
  content: "";
  position: absolute;
  inset: -4px;
  border: 1px solid color-mix(in srgb,
      var(--color-operational-teal) 42%,
      transparent);
  border-radius: inherit;
  opacity: 0;
  animation: apply-stack-complete-ring 620ms var(--ease-out-quint) 1;
}

.apply-progress-step-failure .apply-progress-icon {
  border-color: var(--color-error-fg);
  color: var(--color-error-fg);
}

.apply-progress-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.apply-progress-copy strong {
  color: var(--color-ink);
  font-size: var(--text-metadata-size);
  line-height: 1.25;
}

.apply-progress-copy span,
.apply-progress-copy em {
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
  font-style: normal;
  line-height: 1.35;
}

.apply-job-live-log {
  display: grid;
  gap: 8px;
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-panel-tint);
}

.apply-job-live-log-heading {
  align-items: flex-start;
}

.apply-job-log-heading-copy {
  display: grid;
  flex: 1 1 220px;
  gap: 3px;
  min-width: 0;
}

.apply-job-log-path {
  max-width: 100%;
  color: var(--color-muted-text);
  font-family: var(--font-mono);
  font-size: 0.78rem;
}

.apply-job-log-note {
  color: var(--color-muted-text);
  font-size: 0.82rem;
}

.apply-job-log-toggle {
  flex: 0 0 auto;
}

.apply-job-live-log-body {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.apply-job-log-viewer {
  min-height: 160px;
  max-height: 260px;
  padding: 12px;
  border-color: transparent;
}

@media (--wud-app-shell) {
  .apply-job-panel-active {
    top: 76px;
  }

  .apply-job-grid {
    grid-template-columns: 1fr;
  }
}

@media (--wud-compact) {
  .apply-job-heading {
    display: grid;
  }

  .apply-progress-list {
    grid-template-columns: 1fr;
  }

  .apply-job-now {
    grid-template-columns: 1fr;
  }

  .apply-job-now > :deep(.n-tag) {
    width: fit-content;
  }

  .apply-job-details .disclosure-summary {
    min-height: var(--size-touch-target);
  }
}

@media (--wud-reduced-motion) {
  .apply-job-progress span {
    left: 0;
    width: 100%;
    transform: none !important;
  }

  .apply-job-complete-mark,
  .apply-progress-step-success .apply-progress-icon,
  .apply-progress-step-success .apply-progress-icon::after {
    animation: none;
  }
}

@keyframes apply-job-progress {
  0% {
    transform: translateX(0);
  }

  50% {
    transform: translateX(150%);
  }

  100% {
    transform: translateX(340%);
  }
}

@keyframes apply-complete-pop {
  0% {
    transform: scale(0.88);
  }

  100% {
    transform: scale(1);
  }
}

@keyframes apply-stack-complete {
  0% {
    transform: scale(0.9);
  }

  100% {
    transform: scale(1);
  }
}

@keyframes apply-stack-complete-ring {
  0% {
    opacity: 0.5;
    transform: scale(0.82);
  }

  100% {
    opacity: 0;
    transform: scale(1.2);
  }
}
</style>
