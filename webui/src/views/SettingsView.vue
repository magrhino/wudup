<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import { useRoute } from "vue-router";
import { NAlert } from "naive-ui";

import { useRouteRefresh } from "../components/app/routeRefresh";
import { useCompactBreakpoint } from "../responsive";
import { useSettingsStore } from "../stores/settings";
import SettingsActionsSection from "./settings/SettingsActionsSection.vue";
import SettingsDiagnosticsSection from "./settings/SettingsDiagnosticsSection.vue";
import SettingsDocsSection from "./settings/SettingsDocsSection.vue";
import SettingsIntegrationsSection from "./settings/SettingsIntegrationsSection.vue";
import SettingsJumpNav from "./settings/SettingsJumpNav.vue";
import SettingsNotificationsSection from "./settings/SettingsNotificationsSection.vue";
import SettingsPreferencesSection from "./settings/SettingsPreferencesSection.vue";
import SettingsRuntimeSections from "./settings/SettingsRuntimeSections.vue";
import SettingsSafetyStrip from "./settings/SettingsSafetyStrip.vue";
import { focusOnboardingChecklist } from "./settings/settingsDom";
import { runInBackground } from "../utils/promises";

const settings = useSettingsStore();
const route = useRoute();
const settingsData = computed(() => settings.settings);
const compactSettingsLayout = useCompactBreakpoint();
const shouldFocusOnboardingChecklist = computed(
  () =>
    route?.query.onboarding === "1" &&
    settingsData.value !== null &&
    settings.onboarding?.visible === true,
);

watch(
  shouldFocusOnboardingChecklist,
  (shouldFocus) => {
    if (shouldFocus) {
      runInBackground(focusOnboardingChecklist());
    }
  },
  { immediate: true, flush: "post" },
);

onMounted(() => {
  runInBackground(loadInitialSettings());
});

useRouteRefresh(refreshSettings);

async function loadInitialSettings(): Promise<void> {
  await settings.loadSettings();
  await settings.ensureCoreUpdateTour();
}

async function refreshSettings(): Promise<void> {
  await Promise.all([
    settings.loadSettings(),
    settings.loadOnboarding(),
    settings.loadCoreUpdateTour(),
  ]);
}
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="settings.error" type="error" :show-icon="false">
      {{ settings.error }}
    </n-alert>

    <SettingsSafetyStrip v-if="settingsData" />
    <div class="settings-layout" :class="{ 'has-settings-map': settingsData }">
      <SettingsJumpNav v-if="settingsData" />
      <div class="settings-main content-stack">
        <SettingsActionsSection
          v-if="settingsData"
          :compact="compactSettingsLayout"
        />
        <SettingsPreferencesSection
          v-if="settingsData"
          :compact="compactSettingsLayout"
        />
        <SettingsNotificationsSection
          v-if="settingsData"
          :compact="compactSettingsLayout"
        />
        <SettingsIntegrationsSection v-if="settingsData" />
        <SettingsRuntimeSections :compact="compactSettingsLayout" />
        <SettingsDiagnosticsSection />
        <SettingsDocsSection />
      </div>
    </div>
  </section>
</template>

<style scoped>
.settings-layout {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.settings-layout.has-settings-map {
  grid-template-columns: minmax(190px, 230px) minmax(0, 1fr);
  align-items: start;
}

.settings-main {
  min-width: 0;
}

@media (--wud-app-shell) {
  .settings-layout.has-settings-map {
    grid-template-columns: 1fr;
  }
}
</style>
