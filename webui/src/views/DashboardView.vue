<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import {
  AlertTriangle,
  BellOff,
  CheckCircle2,
  Database,
  Settings2,
  Tags,
} from "@lucide/vue";
import { NAlert, NEmpty, NGi, NGrid } from "naive-ui";

import CoreUpdateTourPanel from "../components/CoreUpdateTourPanel.vue";
import RunResultSummary from "../components/RunResultSummary.vue";
import { useRouteRefresh } from "../components/app/routeRefresh";
import { useConnectionStore } from "../stores/connection";
import { useUpdatesStore } from "../stores/updates";
import { useRunsStore } from "../stores/runs";
import { useSettingsStore } from "../stores/settings";
import { runInBackground } from "../utils/promises";
import { isImageUpdate } from "../utils/updateSummary";
import { usePendingQueueState } from "./pending/usePendingQueueState";
import { pendingMetadataStatus } from "./pending/pendingDisplay";
import { pluralize } from "./pending/utils";

const connection = useConnectionStore();
const updates = useUpdatesStore();
const runs = useRunsStore();
const settings = useSettingsStore();

const loaded = ref(false);
const queue = usePendingQueueState();
const recentRuns = computed(() => runs.runs.filter(isImageUpdate).slice(0, 5));
const pendingCount = computed(() => updates.pending?.count ?? connection.status?.pending_count);
const reviewGroups = computed(() => queue.stackGroups.value.map(group => {
  const attention = group.items.filter(item => pendingMetadataStatus(item) !== "fresh" || item.diagnostic).length;
  const decisions = group.items.filter(item => item.tag_stream || (item.desired_tag && item.desired_tag !== item.current_tag)).length;
  return {
    ...group,
    attention,
    label: attention ? "Needs attention" : decisions ? "Version decision" : "Review updates",
    detail: attention
      ? `${pluralize(attention, "target")} ${attention === 1 ? "needs" : "need"} fresh update information or a configuration check.`
      : decisions
        ? `${pluralize(decisions, "version change")} to review in the plan.`
        : `${pluralize(group.items.length, "image update")} to review in the plan.`,
  };
}).sort((a, b) => Number(Boolean(b.attention)) - Number(Boolean(a.attention))));
const deferredCount = computed(() => queue.stoppedItems.value.length);
const snoozedCount = computed(() => queue.snoozedItems.value.length + queue.snoozedCandidates.value.length);
const error = computed(() => connection.error || updates.error || runs.error || settings.error);
const warnings = computed(() => [
  ...(connection.status?.warnings ?? []),
  ...(updates.pending?.warnings ?? []),
]);
const wudApiLabel = computed(() => {
  const api = connection.status?.wud_api;
  if (!api) {
    return "Unknown";
  }
  if (api.metadata_available) {
    return "Metadata ready";
  }
  if (api.available && api.state === "auth_required") {
    return "Auth required";
  }
  if (api.available) {
    return "Degraded";
  }
  return "Unavailable";
});

async function loadDashboard(): Promise<void> {
  try {
    await Promise.all([
      connection.loadStatus(),
      updates.loadPending(),
      runs.loadRuns(),
      settings.loadServicePolicies(),
      settings.loadSnoozes(),
      settings.loadTagExclusions(),
    ]);
  } finally {
    loaded.value = true;
  }
}

useRouteRefresh(loadDashboard);

onMounted(() => {
  runInBackground(loadDashboard());
});
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="error" type="error" :show-icon="false">
      {{ error }}
    </n-alert>

    <div v-if="warnings.length" class="warning-list">
      <n-alert
        v-for="warning in warnings"
        :key="warning"
        type="warning"
        :show-icon="false"
      >
        {{ warning }}
      </n-alert>
    </div>

    <CoreUpdateTourPanel
      step="dashboard"
      title="Start from current state"
      detail="Review pending work by stack, then check recent results for image verification, health checks, and follow-up. Review changes nothing; apply readiness is checked in the plan."
      next-label="Open pending updates"
      next-step="pending_select"
      next-to="/pending"
    >
      <div class="core-tour-facts">
        <span>Pending: {{ pendingCount ?? "unknown" }}</span>
        <span>Database: {{ !connection.status ? "unknown" : connection.status.db_ready ? "ready" : "missing" }}</span>
        <span>Mutations: {{ connection.status?.mutations_enabled ? "enabled" : "read-only" }}</span>
        <span>WUD API: {{ wudApiLabel }}</span>
      </div>
    </CoreUpdateTourPanel>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <h2>{{ pendingCount === undefined ? "Pending updates" : pluralize(pendingCount, "pending update") }}</h2>
          <p class="dashboard-help">Review changes nothing. The plan checks readiness before apply.</p>
        </div>
        <RouterLink to="/pending" class="text-link">Open pending updates</RouterLink>
      </div>
      <p v-if="!updates.pending" class="dashboard-help" role="status">
        {{ updates.error ? "Pending updates unavailable. Refresh to try again." : "Loading pending updates…" }}
      </p>
      <p v-else-if="updates.error" class="dashboard-help" role="status">Showing the last loaded queue. Refresh before reviewing updates.</p>
      <output
        v-if="updates.pending && !updates.error && pendingCount === 0 && !updates.pending.items.length"
        class="empty-state clear-queue-state clear-queue-state-compact"
      >
        <span class="clear-queue-mark" aria-hidden="true">
          <CheckCircle2 :size="24" />
        </span>
        <strong>Queue clear</strong>
        <span>No pending updates are waiting for review.</span>
      </output>
      <div v-if="queue.groupingReady.value && reviewGroups.length" class="dashboard-list">
        <RouterLink v-for="group in reviewGroups.slice(0, 5)" :key="group.name" to="/pending" class="dashboard-review-row">
          <div>
            <strong>{{ group.name }} · {{ group.services_label }}</strong>
            <p>{{ group.detail }}</p>
          </div>
          <span class="dashboard-review-state" :class="{ attention: group.attention }">{{ group.label }}</span>
        </RouterLink>
        <p v-if="reviewGroups.length > 5" class="dashboard-help">{{ reviewGroups.length - 5 }} more stacks in Pending.</p>
      </div>
      <div v-else-if="updates.pending && pendingCount && !queue.groupingReady.value">
        <p class="dashboard-help">Stack matching is unavailable. Open Pending to review image targets and diagnostics.</p>
        <ul class="dashboard-fallback-list">
          <li v-for="item in updates.pending.items.slice(0, 5)" :key="item.line_no">{{ item.image }}<span v-if="item.desired_tag"> → {{ item.desired_tag }}</span></li>
        </ul>
      </div>
      <p v-else-if="updates.pending && pendingCount && !updates.pending.items.length" class="dashboard-help">
        Update details are unavailable. Open Pending or refresh to check the queue.
      </p>
      <ul v-if="queue.unmatchedItems.value.length || deferredCount || snoozedCount" class="dashboard-queue-notes">
        <li v-if="queue.unmatchedItems.value.length">
          <RouterLink to="/pending">Check {{ pluralize(queue.unmatchedItems.value.length, "unmatched target") }}</RouterLink>
          <span>Check stack matching in Pending.</span>
        </li>
        <li v-if="deferredCount">
          <RouterLink to="/pending">{{ pluralize(deferredCount, "stopped or unverified target") }}</RouterLink>
          <span>Excluded from bulk review; inspect current service state first.</span>
        </li>
        <li v-if="snoozedCount">
          <RouterLink to="/pending">{{ pluralize(snoozedCount, "snoozed target") }}</RouterLink>
          <span>Deferred by active snoozes.</span>
        </li>
      </ul>
    </section>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <h2>Recent update results</h2>
        </div>
        <RouterLink to="/runs" class="text-link">View history</RouterLink>
      </div>
      <n-empty
        v-if="!recentRuns.length"
        class="empty-state"
        :description="runs.error ? 'Recent results unavailable. Refresh to try again.' : !loaded || runs.loading ? 'Loading recent results…' : runs.runs.length ? 'No update runs in recent history.' : 'No runs recorded.'"
        :show-icon="false"
      />
      <div v-else class="dashboard-list">
        <p v-if="runs.error" class="dashboard-help">Showing the last loaded results. Refresh to check for newer runs.</p>
        <RouterLink
          v-for="run in recentRuns"
          :key="run.id"
          :to="`/runs/${run.id}`"
          class="dashboard-run-row"
        >
          <RunResultSummary :run="run" compact />
          <span class="dashboard-run-status">{{ run.status }} · #{{ run.id }}</span>
        </RouterLink>
      </div>
    </section>

    <dl class="dashboard-status-strip" aria-label="System status">
      <div class="dashboard-status-item">
        <dt><Database :size="20" aria-hidden="true" />Database</dt>
        <dd>{{ !connection.status ? "Unknown" : connection.status.db_ready ? "Ready" : "Missing" }}</dd>
      </div>
      <div class="dashboard-status-item">
        <dt>
          <CheckCircle2 v-if="connection.status?.ok" :size="20" aria-hidden="true" />
          <AlertTriangle v-else :size="20" aria-hidden="true" />
          System status
        </dt>
        <dd>{{ !connection.status ? "Unknown" : connection.status.ok ? "OK" : "Needs attention" }}</dd>
      </div>
      <div class="dashboard-status-item">
        <dt><CheckCircle2 v-if="connection.status?.wud_api?.metadata_available" :size="20" aria-hidden="true" /><AlertTriangle v-else :size="20" aria-hidden="true" />WUD API</dt>
        <dd>{{ wudApiLabel }}</dd>
      </div>
    </dl>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <h2>Management</h2>
        </div>
      </div>
      <n-grid class="shortcut-grid" responsive="self" cols="1 920:3" :x-gap="10" :y-gap="10">
        <n-gi>
          <RouterLink to="/policies" class="shortcut-card">
            <Settings2 :size="20" />
            <span>Policies</span>
            <strong>{{ settings.servicePolicies.length }}</strong>
          </RouterLink>
        </n-gi>
        <n-gi>
          <RouterLink to="/snoozes" class="shortcut-card">
            <BellOff :size="20" />
            <span>Active snoozes</span>
            <strong>{{ settings.snoozes.length }}</strong>
          </RouterLink>
        </n-gi>
        <n-gi>
          <RouterLink to="/tag-exclusions" class="shortcut-card">
            <Tags :size="20" />
            <span>Active exclusions</span>
            <strong>{{ settings.tagExclusions.length }}</strong>
          </RouterLink>
        </n-gi>
      </n-grid>
    </section>
  </section>
</template>

<style scoped>
.dashboard-help {
  margin: 6px 0 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
  line-height: 1.5;
}

.dashboard-list { display: grid; }

.dashboard-review-row,
.dashboard-run-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 0;
  border-top: 1px solid var(--color-border-subtle);
  color: var(--color-ink);
  text-decoration: none;
  overflow-wrap: anywhere;
}

.dashboard-review-row:hover,
.dashboard-run-row:hover { background: var(--color-panel-tint); }

.dashboard-review-row > div,
.dashboard-run-row > section { min-width: 0; flex: 1; }

.dashboard-review-row p {
  margin: 4px 0 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.dashboard-review-state,
.dashboard-run-status {
  font-size: var(--text-metadata-size);
  flex-shrink: 0;
}

.dashboard-review-state { color: var(--color-action-blue); }
.dashboard-review-state.attention { color: var(--color-warning-fg); }
.dashboard-run-status { color: var(--color-muted-text); }

.dashboard-queue-notes,
.dashboard-fallback-list {
  margin: 12px 0 0;
  padding-left: 20px;
  font-size: var(--text-metadata-size);
  overflow-wrap: anywhere;
}
.dashboard-queue-notes li + li { margin-top: 8px; }
.dashboard-queue-notes span { display: block; margin-top: 2px; color: var(--color-muted-text); }
.dashboard-queue-notes a { color: var(--color-action-blue); }

.dashboard-status-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-border-subtle);
}

.dashboard-status-item {
  min-width: 0;
  padding: 14px 16px;
  background: var(--color-surface);
}

.dashboard-status-item dt {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
  font-weight: 600;
}

.dashboard-status-item dt svg {
  flex: 0 0 auto;
  color: var(--color-operational-teal);
}

.dashboard-status-item dd {
  min-width: 0;
  margin: 7px 0 0 27px;
  color: var(--color-ink);
  font-size: var(--text-body-size);
  font-weight: 700;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.shortcut-grid {
  margin-top: 16px;
}

.shortcut-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-height: 58px;
  padding: 12px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-panel-tint);
  transition:
    border-color var(--motion-base) var(--ease-out-quart),
    transform var(--motion-fast) var(--ease-out-quart);
}

.shortcut-card:hover {
  border-color: var(--color-border-hover);
  transform: translateY(-1px);
}

.shortcut-card:active {
  transform: translateY(0);
}

.shortcut-card svg {
  color: var(--color-operational-teal);
}

.shortcut-card span,
.shortcut-card strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

@media (--wud-app-shell) {
  .dashboard-status-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .dashboard-status-item:last-child:nth-child(odd) {
    grid-column: 1 / -1;
  }
}

@media (--wud-compact) {
  .dashboard-review-row,
  .dashboard-run-row { flex-direction: column; align-items: flex-start; gap: 8px; }

  .dashboard-status-strip {
    grid-template-columns: minmax(0, 1fr);
  }

  .dashboard-status-item:last-child:nth-child(odd) {
    grid-column: auto;
  }
}

</style>
