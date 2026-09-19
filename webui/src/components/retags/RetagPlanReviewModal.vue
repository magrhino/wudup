<script setup lang="ts">
import { computed } from "vue";
import { NAlert, NTag } from "naive-ui";

import type { RetagPlanResponse, RetagPreviewJobResponse } from "../../api/client";
import PreflightFooterActions from "../preflight/PreflightFooterActions.vue";
import PreflightMetricsGrid, {
  type PreflightMetric,
} from "../preflight/PreflightMetricsGrid.vue";
import PreflightModalShell from "../preflight/PreflightModalShell.vue";
import PreflightNoticeList from "../preflight/PreflightNoticeList.vue";
import PreflightProgressDisplay from "../preflight/PreflightProgressDisplay.vue";
import {
  labelRewriteSummary,
  planStatusType,
  pluralize,
  retagPlanSourceFile,
  retagPlanStackUpdates,
  retagUpdateModeLabel,
  retagUpdateSummary,
} from "../../views/retags/display";

type RetagDuplicateServiceTarget = {
  key: string;
  label: string;
  location: string;
  image: string;
};
type RetagDuplicateServiceConflict = {
  serviceKey: string;
  targets: RetagDuplicateServiceTarget[];
};

const props = defineProps<{
  show: boolean;
  plan: RetagPlanResponse | null;
  previewJob: RetagPreviewJobResponse | null;
  impactLabel: string;
  mutationNotice: string;
  runtimeWarning: string;
  previewError: string;
  duplicateServiceConflicts: RetagDuplicateServiceConflict[];
  applyDisabled: boolean;
  loading: boolean;
  applyJobActive: boolean;
}>();

const emit = defineEmits<{
  close: [];
  apply: [];
}>();

const previewActive = computed(
  () =>
    props.loading ||
    props.previewJob?.status === "queued" ||
    props.previewJob?.status === "running",
);

const statusLabel = computed(
  () => props.plan?.status ?? props.previewJob?.status ?? "preview",
);

const statusType = computed(() => {
  if (props.plan) {
    return planStatusType(props.plan);
  }
  return previewActive.value ? "info" : "default";
});

const duplicateServiceHeadline = computed(() => {
  const keys = props.duplicateServiceConflicts.map((item) => item.serviceKey);
  if (!keys.length) {
    return "";
  }
  const label =
    keys.length === 1 ? "Duplicate service key" : "Duplicate service keys";
  return `${label}: ${keys.join(", ")}.`;
});

const duplicateServiceRecovery = computed(() => {
  const conflicts = props.duplicateServiceConflicts;
  if (!conflicts.length) {
    return "";
  }
  if (conflicts.length === 1) {
    const targetCount = conflicts[0].targets.length || 2;
    const targetLabel = pluralize(targetCount, "discovered target");
    return `${duplicateServiceHeadline.value} Retag preview stopped because ${targetLabel} share this Compose project/service identity. Keep only one target for this key, or update Compose so each project/service pair is unique, then reload retag targets and preview again.`;
  }
  return `${duplicateServiceHeadline.value} Retag preview stopped because discovered targets share these Compose project/service identities. Keep only one target for each key, or update Compose so each project/service pair is unique, then reload retag targets and preview again.`;
});

const previewErrorSummary = computed(
  () => duplicateServiceHeadline.value || props.previewError,
);

const previewErrorDetail = computed(
  () => duplicateServiceRecovery.value || props.previewError,
);

const summary = computed(() => {
  if (previewErrorSummary.value) {
    return previewErrorSummary.value;
  }
  if (props.previewJob?.status === "failure") {
    return props.previewJob.error || "Retag preview failed.";
  }
  if (!props.plan) {
    if (!previewActive.value && !props.previewJob) {
      return "Retag targets or selections changed. Close this preview and preview your current selection again.";
    }
    return "Building a preview from the selected candidates.";
  }
  if (props.plan.status === "blocked") {
    return `${pluralize(props.plan.issues.length, "issue")} must be resolved before applying.`;
  }
  if (props.plan.status === "empty") {
    return "No selected services need retag changes.";
  }
  return `${pluralize(props.plan.selected_count, "service")} ready to retag.`;
});

const metrics = computed<PreflightMetric[]>(() => {
  const plan = props.plan;
  if (!plan) {
    return [
      { label: "Status", value: statusLabel.value },
      { label: "Progress", value: props.previewJob?.progress.length ?? 0 },
    ];
  }
  return [
    { label: "Services", value: plan.selected_count },
    { label: "Stacks", value: plan.stacks.length },
    { label: "Keep current", value: plan.keep_current_count },
    { label: "Source", value: retagPlanSourceFile(plan) },
  ];
});

const retagPlanUpdates = computed(() =>
  (props.plan?.stacks ?? []).flatMap((stack) =>
    retagPlanStackUpdates(stack).map((update) => ({
      stack,
      update,
    })),
  ),
);

const warnings = computed(() => [
  ...(props.previewJob?.warnings ?? []),
  ...(props.plan?.warnings ?? []),
]);

const uniqueWarnings = computed(() => [...new Set(warnings.value)]);
</script>

<template>
  <PreflightModalShell
    :show="show"
    eyebrow="Preflight"
    title="Review retag preview"
    :summary="summary"
    :impact-label="impactLabel"
    :status-label="statusLabel"
    :status-type="statusType"
    @close="$emit('close')"
  >
    <n-alert
      v-if="mutationNotice"
      type="warning"
      :show-icon="false"
    >
      {{ mutationNotice }}
    </n-alert>

    <n-alert
      v-if="runtimeWarning"
      type="warning"
      :show-icon="false"
    >
      {{ runtimeWarning }}
    </n-alert>

    <n-alert
      v-if="previewErrorDetail"
      type="error"
      :show-icon="false"
    >
      <div
        v-if="duplicateServiceConflicts.length"
        class="duplicate-service-alert"
      >
        <p>{{ previewErrorDetail }}</p>
        <div class="duplicate-service-list">
          <section
            v-for="conflict in duplicateServiceConflicts"
            :key="conflict.serviceKey"
            class="duplicate-service-group"
          >
            <strong>{{ conflict.serviceKey }}</strong>
            <ul
              v-if="conflict.targets.length"
              class="duplicate-service-targets"
            >
              <li
                v-for="target in conflict.targets"
                :key="target.key"
              >
                <span>{{ target.label }}</span>
                <code v-if="target.location">{{ target.location }}</code>
                <code>{{ target.image }}</code>
              </li>
            </ul>
            <p v-else>
              Reload retag targets to see the affected Compose rows.
            </p>
          </section>
        </div>
      </div>
      <template v-else>
        {{ previewErrorDetail }}
      </template>
    </n-alert>

    <PreflightMetricsGrid :items="metrics" />

    <PreflightProgressDisplay
      :active="previewActive"
      :progress="previewJob?.progress ?? []"
      empty-message="Waiting for the retag preview job to start."
    />

    <PreflightNoticeList
      :warnings="uniqueWarnings"
      :issues="plan?.issues ?? []"
    />

    <section
      v-if="plan"
      class="preflight-impact preflight-block"
      aria-labelledby="retag-preview-services-title"
    >
      <div class="preflight-impact-heading">
        <strong id="retag-preview-services-title">Services and images</strong>
        <n-tag size="small">{{ pluralize(retagPlanUpdates.length, "service") }}</n-tag>
      </div>
      <div v-if="retagPlanUpdates.length" class="compact-list">
        <div
          v-for="({ stack, update }, index) in retagPlanUpdates"
          :key="`preview-${stack.directory}-${stack.compose_file}-${stack.project_directory}-${update.service_key}-${index}`"
          class="list-row plan-line-row"
        >
          <span>{{ stack.stack }}</span>
          <strong>{{ update.service_key }}</strong>
          <em>
            <n-tag size="small" type="info">
              {{ retagUpdateModeLabel(update) }}
            </n-tag>
            <code>{{ retagUpdateSummary(update) }}</code>
            <span>{{ labelRewriteSummary(update) }}</span>
          </em>
        </div>
      </div>
      <div v-else class="empty-state">No retag changes selected.</div>
    </section>

    <PreflightFooterActions
      primary-label="Apply selected retags"
      :primary-disabled="Boolean(previewErrorDetail) || !plan || applyDisabled"
      :primary-loading="loading || applyJobActive"
      secondary-label="Close"
      @primary="$emit('apply')"
      @secondary="$emit('close')"
    />
  </PreflightModalShell>
</template>

<style scoped>
.plan-line-row em {
  display: grid;
  gap: 3px;
}

.duplicate-service-alert {
  display: grid;
  gap: 10px;
}

.duplicate-service-alert p {
  margin: 0;
}

.duplicate-service-list {
  display: grid;
  gap: 8px;
}

.duplicate-service-group {
  display: grid;
  gap: 6px;
}

.duplicate-service-targets {
  display: grid;
  gap: 6px;
  margin: 0;
  padding-left: 18px;
}

.duplicate-service-targets li {
  display: grid;
  gap: 2px;
}

.duplicate-service-targets code {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  overflow-wrap: anywhere;
}
</style>
