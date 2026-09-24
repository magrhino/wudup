<script setup lang="ts">
import { computed, h, onMounted } from "vue";
import { RouterLink } from "vue-router";
import { NAlert, NDataTable, NEmpty, NTag, type DataTableColumns } from "naive-ui";

import CoreUpdateTourPanel from "../components/CoreUpdateTourPanel.vue";
import HistoryViewTabs from "../components/HistoryViewTabs.vue";
import { runAction } from "../utils/updateSummary";
import RunResultSummary from "../components/RunResultSummary.vue";
import { useRouteRefresh } from "../components/app/routeRefresh";
import type { RunSummary } from "../api/client";
import { useDataCardsBreakpoint } from "../responsive";
import { useRunsStore } from "../stores/runs";
import { runInBackground } from "../utils/promises";

const runs = useRunsStore();
const isMobile = useDataCardsBreakpoint();

const columns = computed<DataTableColumns<RunSummary>>(() => [
  {
    title: "Run",
    key: "id",
    width: 90,
    render: (row) =>
      h(RouterLink, { to: `/runs/${row.id}`, class: "text-link" }, () => `#${row.id}`),
  },
  { title: "Status", key: "status", minWidth: 100 },
  { title: "Action", key: "mode", minWidth: 140, render: (row) => runAction(row) },
  { title: "Result", key: "result", minWidth: 300, render: (row) => h(RunResultSummary, { run: row, compact: true }) },
]);

onMounted(() => {
  runInBackground(runs.loadRuns());
});

useRouteRefresh(() => runs.loadRuns());
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="runs.error" type="error" :show-icon="false">
      {{ runs.error }}
    </n-alert>

    <div class="section-heading">
      <div>
        <p class="eyebrow">SQLite history</p>
        <h2>{{ runs.runs.length }} recent runs</h2>
      </div>
    </div>

    <HistoryViewTabs />

    <CoreUpdateTourPanel
      step="runs_history"
      title="Verify the run afterward"
      detail="History records each preview, apply, cleanup, and settings action. Open a run to inspect metadata, then use the log link when command output matters."
      complete
      next-label="Finish tour"
    >
      <div class="core-tour-facts">
        <span>{{ runs.runs.length }} recent runs</span>
        <span>Details and logs stay linked from each run</span>
      </div>
    </CoreUpdateTourPanel>

    <n-data-table
      v-if="!isMobile"
      :columns="columns"
      :data="runs.runs"
      :loading="runs.loading"
      :pagination="{ pageSize: 15 }"
      size="small"
      class="data-surface"
    />

    <div v-else class="mobile-list">
      <RouterLink
        v-for="run in runs.runs"
        :key="run.id"
        :to="`/runs/${run.id}`"
        class="mobile-card linked"
      >
        <div class="mobile-card-title">
          <strong>#{{ run.id }} {{ run.status }}</strong>
          <n-tag size="small" :type="run.dry_run ? 'info' : 'warning'">
            {{ runAction(run) }}
          </n-tag>
        </div>
        <RunResultSummary :run="run" compact />
      </RouterLink>
      <n-empty
        v-if="!runs.runs.length"
        class="empty-state"
        description="No runs recorded."
        :show-icon="false"
      />
    </div>
  </section>
</template>
