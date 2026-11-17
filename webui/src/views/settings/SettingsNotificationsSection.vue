<script setup lang="ts">
import { Bell, RotateCcw, Save, Send, Trash2 } from "@lucide/vue";
import { NAlert, NButton, NFlex, NInput, NModal, NSelect, NSwitch } from "naive-ui";
import type { Directive } from "vue";

import { vSettingsSelectLoadingState } from "./settingsDom";
import { useManagedNotifications } from "./useManagedNotifications";

const vSelectTriggerDescription: Directive<HTMLElement, string> = {
  mounted(element, { value }) {
    element
      .querySelector<HTMLElement>(".n-base-selection-label")
      ?.setAttribute("aria-describedby", value);
  },
};

// Bound from script: vue-tsc 3.3.14 mis-parses `//` inside a static template
// attribute and breaks type checking for the rest of the template
// (vuejs/language-tools#6322).
const discordWebhookPlaceholder = "https://discord.com/api/webhooks/...";

defineProps<{
  compact: boolean;
}>();

const {
  settings,
  releaseNotesEnabledEntry,
  releaseNotificationDeliveryModeEntry,
  releaseNotificationModeEntry,
  releaseNotificationResendPolicyEntry,
  releaseNotificationCooldownEntry,
  discordWebhookEntry,
  releaseNotificationVerbosityEntry,
  releaseNotesEnabledValue,
  releaseNotificationDeliveryModeValue,
  releaseNotificationModeValue,
  releaseNotificationResendPolicyValue,
  releaseNotificationCooldownValue,
  discordWebhookValue,
  discordWebhookClearRequested,
  releaseNotificationVerbosityValue,
  notificationsMessage,
  notificationsError,
  testWebhookDialogVisible,
  notificationControlsDisabled,
  releaseNotesEnabledEditable,
  releaseNotificationDeliveryModeEditable,
  releaseNotificationModeEditable,
  releaseNotificationResendPolicyEditable,
  releaseNotificationCooldownEditable,
  discordWebhookEditable,
  discordWebhookConfigured,
  discordWebhookStatus,
  releaseNotificationVerbosityEditable,
  notificationsDirty,
  notificationSaveDisabled,
  testWebhookButtonDisabled,
  releaseNotificationDeliveryModeOptions,
  releaseNotificationModeOptions,
  releaseNotificationResendPolicyOptions,
  releaseNotificationVerbosityOptions,
  managedSourceLabel,
  resetNotificationForm,
  clearDiscordWebhook,
  openTestWebhookDialog,
  sendTestWebhook,
  saveManagedNotifications,
} = useManagedNotifications();
</script>

<template>
  <div id="settings-notifications" class="settings-zone">
    <div class="settings-zone-heading">
      <div>
        <h2>Notifications</h2>
      </div>
    </div>

    <section class="section-panel">
      <div class="section-heading">
        <div class="section-heading-main">
          <p class="eyebrow">Managed notifications</p>
          <h2>Release-note notifications</h2>
          <p class="section-copy">
            Send Discord updates on demand, or let WUD API polling send them on detection.
          </p>
        </div>
        <Bell :size="20" class="section-heading-icon" />
      </div>
      <n-alert
        v-if="notificationsMessage"
        type="success"
        :show-icon="false"
        class="settings-action-alert"
      >
        {{ notificationsMessage }}
      </n-alert>
      <n-alert
        v-if="notificationsError"
        type="error"
        :show-icon="false"
        class="settings-action-alert"
      >
        {{ notificationsError }}
      </n-alert>
      <div class="settings-preference-groups">
        <section class="settings-preference-group" aria-labelledby="settings-notification-delivery-heading">
          <div class="settings-preference-group-heading">
            <h3 id="settings-notification-delivery-heading">Delivery</h3>
            <p>Activation and Discord destination.</p>
          </div>
          <div class="settings-preference-list">
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Release-note notifications</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotesEnabledEntry) }}
                </span>
              </div>
              <div class="settings-preference-controls">
                <n-switch
                  v-model:value="releaseNotesEnabledValue"
                  :disabled="notificationControlsDisabled || !releaseNotesEnabledEditable"
                  aria-label="Release-note notifications"
                />
                <n-alert
                  v-if="releaseNotesEnabledEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotesEnabledEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Delivery mode</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotificationDeliveryModeEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="releaseNotificationDeliveryModeValue"
                  :options="releaseNotificationDeliveryModeOptions"
                  :disabled="
                    notificationControlsDisabled ||
                    !releaseNotificationDeliveryModeEditable
                  "
                  aria-label="Release notification delivery mode"
                />
                <n-alert
                  v-if="releaseNotificationDeliveryModeEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotificationDeliveryModeEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Discord webhook</strong>
                <span class="wrap-anywhere">
                  {{ discordWebhookStatus }}. Source:
                  {{ managedSourceLabel(discordWebhookEntry) }}
                </span>
              </div>
              <div class="settings-preference-controls settings-textarea-controls">
                <n-input
                  v-model:value="discordWebhookValue"
                  type="password"
                  show-password-on="click"
                  :disabled="notificationControlsDisabled || !discordWebhookEditable"
                  :placeholder="discordWebhookPlaceholder"
                  aria-label="Discord webhook URL"
                />
                <n-flex
                  class="settings-button-group"
                  :justify="compact ? 'flex-start' : 'flex-end'"
                  :size="8"
                >
                  <n-button
                    size="small"
                    :disabled="testWebhookButtonDisabled"
                    :loading="settings.loading"
                    @click="openTestWebhookDialog"
                  >
                    <template #icon>
                      <Send :size="16" />
                    </template>
                    Send test
                  </n-button>
                  <n-button
                    size="small"
                    :disabled="
                      notificationControlsDisabled ||
                      !discordWebhookEditable ||
                      !discordWebhookConfigured
                    "
                    @click="clearDiscordWebhook"
                  >
                    <template #icon>
                      <Trash2 :size="16" />
                    </template>
                    Clear webhook
                  </n-button>
                </n-flex>
                <n-alert
                  v-if="discordWebhookClearRequested"
                  type="warning"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  Stored webhook will be cleared when notifications are saved.
                </n-alert>
                <n-alert
                  v-if="discordWebhookEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ discordWebhookEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
          </div>
        </section>

        <section class="settings-preference-group" aria-labelledby="settings-notification-format-heading">
          <div class="settings-preference-group-heading">
            <h3 id="settings-notification-format-heading">Format and history</h3>
            <p>Message detail, batching, and duplicate handling.</p>
          </div>
          <div class="settings-preference-list">
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Verbosity</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotificationVerbosityEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="releaseNotificationVerbosityValue"
                  :options="releaseNotificationVerbosityOptions"
                  :disabled="
                    notificationControlsDisabled ||
                    !releaseNotificationVerbosityEditable
                  "
                  aria-label="Release notification verbosity"
                />
                <n-alert
                  v-if="releaseNotificationVerbosityEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotificationVerbosityEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Message grouping</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotificationModeEntry) }}
                </span>
                <span id="release-notification-summary-help" class="wrap-anywhere">
                  Summary sends one categorized batch and is unrelated to container image
                  digests.
                </span>
                <span id="release-notification-per-container-help" class="wrap-anywhere">
                  Per container sends one detailed notification for each update.
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-select-trigger-description="
                    'release-notification-summary-help release-notification-per-container-help'
                  "
                  v-model:value="releaseNotificationModeValue"
                  :options="releaseNotificationModeOptions"
                  :disabled="notificationControlsDisabled || !releaseNotificationModeEditable"
                  aria-label="Release notification message grouping"
                />
                <n-alert
                  v-if="releaseNotificationModeEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotificationModeEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Resend policy</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotificationResendPolicyEntry) }}
                </span>
              </div>
              <div
                v-settings-select-loading-state
                class="settings-preference-controls"
              >
                <n-select
                  v-model:value="releaseNotificationResendPolicyValue"
                  :options="releaseNotificationResendPolicyOptions"
                  :disabled="
                    notificationControlsDisabled ||
                    !releaseNotificationResendPolicyEditable
                  "
                  aria-label="Release notification resend policy"
                />
                <n-alert
                  v-if="releaseNotificationResendPolicyEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotificationResendPolicyEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
            <div class="settings-preference-row">
              <div>
                <strong class="wrap-anywhere">Notification cooldown</strong>
                <span class="wrap-anywhere">
                  Source:
                  {{ managedSourceLabel(releaseNotificationCooldownEntry) }}
                </span>
              </div>
              <div class="settings-preference-controls">
                <n-input
                  v-model:value="releaseNotificationCooldownValue"
                  :disabled="
                    notificationControlsDisabled ||
                    !releaseNotificationCooldownEditable
                  "
                  inputmode="numeric"
                  aria-label="Release notification cooldown seconds"
                />
                <n-alert
                  v-if="releaseNotificationCooldownEntry?.disabled_reason"
                  type="info"
                  :show-icon="false"
                  class="settings-action-alert"
                >
                  {{ releaseNotificationCooldownEntry.disabled_reason }}
                </n-alert>
              </div>
            </div>
          </div>
        </section>
      </div>
      <div class="settings-action-row settings-preference-actions">
        <div>
          <strong class="wrap-anywhere">No restart required</strong>
          <span class="wrap-anywhere">Notification settings apply to new WebUI requests immediately.</span>
        </div>
        <n-flex
          class="settings-button-group"
          :justify="compact ? 'flex-start' : 'flex-end'"
          :size="8"
        >
          <n-button :disabled="settings.loading || !notificationsDirty" @click="resetNotificationForm">
            <template #icon>
              <RotateCcw :size="16" />
            </template>
            Reset
          </n-button>
          <n-button
            type="primary"
            :disabled="notificationSaveDisabled"
            :loading="settings.loading"
            @click="saveManagedNotifications"
          >
            <template #icon>
              <Save :size="16" />
            </template>
            Save notifications
          </n-button>
        </n-flex>
      </div>
    </section>
    <n-modal
      v-model:show="testWebhookDialogVisible"
      preset="dialog"
      title="Send test webhook"
      positive-text="Send test"
      negative-text="Cancel"
      :positive-button-props="{
        type: 'primary',
        loading: settings.loading,
        disabled: testWebhookButtonDisabled,
      }"
      @positive-click="sendTestWebhook"
    >
      <n-alert type="warning" :show-icon="false" class="block-alert">
        This sends one Discord test message to the saved webhook destination.
      </n-alert>
      <p class="settings-dialog-copy">
        Destination source:
        <strong>{{ managedSourceLabel(discordWebhookEntry) }}</strong>
      </p>
    </n-modal>
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
