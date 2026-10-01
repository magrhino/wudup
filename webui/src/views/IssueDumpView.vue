<script setup lang="ts">
import { onMounted } from "vue";
import { Copy, Download, RefreshCw } from "@lucide/vue";
import { NAlert, NButton, NEmpty, NFlex } from "naive-ui";

import { useRouteRefresh } from "../components/app/routeRefresh";
import type { DiagnosticsSupportBundleResponse } from "../api/client";
import { runInBackground } from "../utils/promises";
import { useSettingsDiagnostics } from "./settings/useSettingsDiagnostics";

function affectedContainerDetails(bundle: DiagnosticsSupportBundleResponse) {
  const observations = bundle.wud_api_observations;
  return {
    summary: {
      containers_affected: observations.counts.degraded,
      using_previous_results: observations.counts.retained,
      update_status_unknown: observations.counts.unresolved,
    },
    containers: observations.items.filter(
      (item) => item.outcome !== "unsupported_ignored",
    ),
  };
}

const {
  diagnosticsDownloading,
  diagnosticsText,
  diagnosticsMessage,
  diagnosticsError,
  isClipboardSupported,
  refreshSupportBundle,
  copySupportBundle,
  downloadSupportBundle,
} = useSettingsDiagnostics({
  reuseLoadedText: true,
  artifactLabel: "Affected container details",
  downloadFilename: "wudup-affected-containers.json",
  selectBundle: affectedContainerDetails,
});

useRouteRefresh(refreshSupportBundle);

onMounted(() => {
  runInBackground(refreshSupportBundle());
});
</script>

<template>
  <section class="content-stack">
    <div class="section-heading">
      <div>
        <h2>Affected WUD containers</h2>
        <p class="section-copy">
          The last WUD update check failed for the containers listed below.
          These errors concern update information and do not indicate that a
          container stopped. WUDup shows WUD's saved results until a successful
          scan replaces them. Refreshing this page only reloads those results;
          wait for the next scheduled WUD scan or run a WUD rescan to retry.
        </p>
      </div>
      <n-flex class="inline-actions" align="center" :size="8">
        <n-button
          quaternary
          circle
          title="Reload saved check results"
          aria-label="Reload saved check results"
          :loading="diagnosticsDownloading"
          @click="refreshSupportBundle"
        >
          <template #icon>
            <RefreshCw :size="17" aria-hidden="true" />
          </template>
        </n-button>
        <n-button
          v-if="isClipboardSupported"
          secondary
          :loading="diagnosticsDownloading"
          @click="copySupportBundle"
        >
          <template #icon>
            <Copy :size="17" aria-hidden="true" />
          </template>
          Copy container details
        </n-button>
        <n-button
          secondary
          :loading="diagnosticsDownloading"
          @click="downloadSupportBundle"
        >
          <template #icon>
            <Download :size="17" aria-hidden="true" />
          </template>
          Download container details
        </n-button>
      </n-flex>
    </div>

    <n-alert
      v-if="diagnosticsMessage"
      type="success"
      :show-icon="false"
      role="status"
    >
      {{ diagnosticsMessage }}
    </n-alert>
    <n-alert
      v-if="diagnosticsError"
      type="error"
      :show-icon="false"
      role="alert"
    >
      {{ diagnosticsError }}
    </n-alert>

    <n-empty
      v-if="!diagnosticsText && !diagnosticsDownloading"
      class="empty-state"
      description="Affected container details are unavailable. Refresh to try again."
      :show-icon="false"
    />
    <pre v-else class="log-viewer issue-dump-viewer">{{ diagnosticsText || "Loading affected containers…" }}</pre>
  </section>
</template>
