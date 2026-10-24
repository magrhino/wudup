<script setup lang="ts">
import { RotateCcw, Save } from "@lucide/vue";
import { NAlert, NButton, NFlex, NInput, NSelect } from "naive-ui";

import { vSettingsSelectLoadingState } from "./settingsDom";
import { useManagedPreferences } from "./useManagedPreferences";

defineProps<{
  compact: boolean;
}>();

const {
  settings,
  themePreferenceEntry,
  onboardingChecklistEntry,
  sidebarBadgesEntry,
  composeIgnorePathsEntry,
  digestPinUpdatesEntry,
  retagDigestPinsEntry,
  themePreferenceValue,
  onboardingChecklistValue,
  sidebarBadgesValue,
  composeIgnorePathsValue,
  digestPinUpdatesValue,
  retagDigestPinsValue,
  preferencesMessage,
  preferencesError,
  preferencesDisabledReason,
  preferenceControlsDisabled,
  composeIgnorePathsEditable,
  digestPinUpdatesEditable,
  retagDigestPinsEditable,
  preferencesDirty,
  preferenceSaveDisabled,
  themePreferenceOptions,
  onboardingChecklistOptions,
  sidebarBadgesOptions,
  digestPinUpdatesOptions,
  retagDigestPinsOptions,
  coreUpdateTourStatus,
  coreUpdateTourStep,
  managedSourceLabel,
  resetPreferenceForm,
  saveManagedPreferences,
  relaunchOnboardingChecklist,
  replayCoreUpdateTour,
  dismissCoreUpdateTour,
} = useManagedPreferences();
</script>

<template>
  <div id="settings-preferences" class="settings-zone">
    <div class="settings-zone-heading">
      <div>
        <h2>Preferences</h2>
      </div>
    </div>

    <section class="section-panel">
      <div class="section-heading">
        <div class="section-heading-main">
          <p class="eyebrow">Managed preferences</p>
          <h2>WebUI preferences</h2>
          <p class="section-copy">
            WebUI choices stored in SQLite. Server config still owns runtime
            paths and secrets.
          </p>
        </div>
        <Save :size="20" class="section-heading-icon" />
      </div>
      <n-alert
        v-if="preferencesMessage"
        type="success"
        :show-icon="false"
        class="settings-action-alert"
      >
        {{ preferencesMessage }}
      </n-alert>
      <n-alert
        v-if="preferencesError"
        type="error"
        :show-icon="false"
        class="settings-action-alert"
      >
        {{ preferencesError }}
      </n-alert>
      <div class="settings-preference-groups">
        <section class="settings-preference-group" aria-labelledby="settings-interface-heading">
          <div class="settings-preference-group-heading">
            <h3 id="settings-interface-heading">Interface</h3>
            <p>Display defaults and first-run interface state.</p>
          </div>
          <div class="settings-preference-list">
            <div
              v-settings-select-loading-state
              class="settings-preference-row"
            >
              <div>
                <strong class="wrap-anywhere">Theme preference</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(themePreferenceEntry) }}
                </span>
              </div>
              <n-select
                v-model:value="themePreferenceValue"
                :options="themePreferenceOptions"
                :disabled="preferenceControlsDisabled"
                aria-label="Theme preference"
              />
            </div>
            <div
              v-settings-select-loading-state
              class="settings-preference-row"
            >
              <div>
                <strong class="wrap-anywhere">Sidebar counts</strong>
                <span class="wrap-anywhere">
                  Shows pending updates and containers that need a tracking
                  fix next to their navigation links.
                </span>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(sidebarBadgesEntry) }}
                </span>
              </div>
              <n-select
                v-model:value="sidebarBadgesValue"
                :options="sidebarBadgesOptions"
                :disabled="preferenceControlsDisabled"
                aria-label="Sidebar counts"
              />
            </div>
          </div>
        </section>

        <section class="settings-preference-group" aria-labelledby="settings-update-heading">
          <div class="settings-preference-group-heading">
            <h3 id="settings-update-heading">Update behavior</h3>
            <p>Managed update defaults that do not require a container restart.</p>
          </div>
          <div class="settings-preference-list">
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Compose ignore paths</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(composeIgnorePathsEntry) }}
                </span>
              </div>
              <div class="settings-preference-controls settings-textarea-controls">
                <n-input
                  v-model:value="composeIgnorePathsValue"
                  type="textarea"
                  :autosize="{ minRows: 2, maxRows: 4 }"
                  :disabled="preferenceControlsDisabled || !composeIgnorePathsEditable"
                  placeholder="old, archive/disabled"
                  aria-label="Compose ignore paths"
                />
                <n-alert
                  v-if="composeIgnorePathsEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ composeIgnorePathsEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Digest-pin updates</strong>
                <span class="wrap-anywhere">
                  Pins resolved digests during standard update plans. Retags
                  use the separate preference below.
                </span>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(digestPinUpdatesEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="digestPinUpdatesValue"
                  :options="digestPinUpdatesOptions"
                  :disabled="preferenceControlsDisabled || !digestPinUpdatesEditable"
                  aria-label="Digest-pin updates"
                />
                <n-alert
                  v-if="digestPinUpdatesEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ digestPinUpdatesEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Retag digest pins</strong>
                <span class="wrap-anywhere">
                  Disabled writes the selected <code>image:tag</code>. Enabled
                  resolves that tag and writes <code>image@sha256</code>.
                </span>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(retagDigestPinsEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="retagDigestPinsValue"
                  :options="retagDigestPinsOptions"
                  :disabled="preferenceControlsDisabled || !retagDigestPinsEditable"
                  aria-label="Retag digest pins"
                />
              </div>
            </div>
          </div>
        </section>

        <section class="settings-preference-group" aria-labelledby="settings-guidance-heading">
          <div class="settings-preference-group-heading">
            <h3 id="settings-guidance-heading">Guidance</h3>
            <p>First-run checklist and core update tour state.</p>
          </div>
          <div class="settings-preference-list">
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Onboarding checklist</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(onboardingChecklistEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="onboardingChecklistValue"
                  :options="onboardingChecklistOptions"
                  :disabled="preferenceControlsDisabled"
                  aria-label="Onboarding checklist"
                />
                <n-button
                  size="small"
                  :disabled="preferenceControlsDisabled"
                  :loading="settings.loading"
                  @click="relaunchOnboardingChecklist"
                >
                  <template #icon>
                    <RotateCcw :size="16" />
                  </template>
                  Relaunch onboarding
                </n-button>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Core update tour</strong>
                <span class="wrap-anywhere">
                  State: {{ coreUpdateTourStatus }}. Step:
                  {{ coreUpdateTourStep }}.
                </span>
              </div>
              <n-flex
                class="settings-button-group"
                :justify="compact ? 'flex-start' : 'flex-end'"
                :size="8"
              >
                <n-button
                  size="small"
                  :disabled="preferenceControlsDisabled"
                  :loading="settings.loading"
                  @click="dismissCoreUpdateTour"
                >
                  Dismiss tour
                </n-button>
                <n-button
                  size="small"
                  type="primary"
                  :disabled="preferenceControlsDisabled"
                  :loading="settings.loading"
                  @click="replayCoreUpdateTour"
                >
                  <template #icon>
                    <RotateCcw :size="16" />
                  </template>
                  Replay tour
                </n-button>
              </n-flex>
            </div>
          </div>
        </section>
      </div>
      <div class="settings-action-row settings-preference-actions">
        <div>
          <strong class="wrap-anywhere">No restart required</strong>
          <span class="wrap-anywhere">Managed values apply to new WebUI requests immediately.</span>
        </div>
        <n-flex
          class="settings-button-group"
          :justify="compact ? 'flex-start' : 'flex-end'"
          :size="8"
        >
          <n-button :disabled="settings.loading || !preferencesDirty" @click="resetPreferenceForm">
            <template #icon>
              <RotateCcw :size="16" />
            </template>
            Reset
          </n-button>
          <n-button
            type="primary"
            :disabled="preferenceSaveDisabled"
            :loading="settings.loading"
            @click="saveManagedPreferences"
          >
            <template #icon>
              <Save :size="16" />
            </template>
            Save preferences
          </n-button>
        </n-flex>
      </div>
      <n-alert
        v-if="preferencesDisabledReason"
        type="info"
        :show-icon="false"
        class="settings-action-alert"
      >
        {{ preferencesDisabledReason }}
      </n-alert>
    </section>
  </div>
</template>

<style scoped>
.settings-zone {
  display: grid;
  gap: 12px;
  scroll-margin-top: 18px;
}

.settings-zone-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
}

.settings-zone-heading>div {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.settings-preference-groups {
  display: grid;
  gap: 18px;
  margin-top: 14px;
}

.settings-preference-group {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.settings-preference-group-heading {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.settings-preference-list {
  display: grid;
}

.settings-preference-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(220px, 300px);
  align-items: center;
  gap: 14px;
  padding: 10px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.settings-preference-row:first-child {
  border-top: 0;
}

.settings-preference-row>div {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.settings-preference-row span {
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.settings-preference-row>.settings-preference-controls {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.settings-preference-controls :deep(.n-select) {
  flex: 1 1 180px;
  min-width: 180px;
}

.settings-preference-row>.settings-textarea-controls {
  align-items: stretch;
  flex-direction: column;
}

.settings-textarea-controls :deep(.n-input) {
  width: 100%;
}

.settings-action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border-subtle);
}

.settings-action-row>div {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.settings-action-row span {
  color: var(--color-muted-text);
}

.settings-action-alert {
  margin-top: 12px;
}

.settings-preference-actions {
  align-items: end;
}

@media (--wud-compact) {
  .settings-preference-row {
    grid-template-columns: 1fr;
  }

  .settings-zone-heading {
    display: grid;
  }

  .settings-action-row {
    display: grid;
    align-items: start;
  }

  .settings-action-row :deep(.n-button),
  .settings-button-group :deep(.n-button) {
    justify-self: start;
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }

  .settings-preference-row>.settings-preference-controls {
    justify-content: flex-start;
  }
}
</style>
