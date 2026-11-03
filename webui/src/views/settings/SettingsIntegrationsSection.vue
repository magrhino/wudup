<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { useClipboard } from "@vueuse/core";
import { Copy, KeyRound } from "@lucide/vue";
import { NAlert, NButton, NModal } from "naive-ui";

import type { ReadonlyTokenAction, ReadonlyTokenStatus } from "../../api/client";
import { useSettingsStore } from "../../stores/settings";

const settings = useSettingsStore();
const { copy, isSupported: isClipboardSupported } = useClipboard();

// The new token lives only here, never in the store or browser storage, and is
// cleared when the operator dismisses it or leaves the page.
const newToken = ref("");
const copyMessage = ref("");
const actionError = ref("");
const confirmAction = ref<ReadonlyTokenAction | null>(null);
const working = ref(false);

const status = computed<ReadonlyTokenStatus | null>(
  () => settings.settings?.readonly_token ?? null,
);
const hasToken = computed(() => (status.value?.source ?? "none") !== "none");
const sourceLabel = computed(() => {
  switch (status.value?.source) {
    case "env":
      return "Set by container config";
    case "webui":
      return "Generated in the WebUI";
    default:
      return "Not set";
  }
});
const createdLabel = computed(() => {
  const value = status.value?.created_at ?? "";
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
});
const confirmVisible = computed({
  get: () => confirmAction.value !== null,
  set: (visible: boolean) => {
    if (!visible) {
      confirmAction.value = null;
    }
  },
});
const confirmTitle = computed(() => {
  if (confirmAction.value === "revoke") {
    return "Revoke read-only API token";
  }
  return hasToken.value ? "Replace read-only API token" : "Generate read-only API token";
});
const confirmCopy = computed(() => {
  if (confirmAction.value === "revoke") {
    return revokeCopy();
  }
  if (hasToken.value) {
    return "The current token stops working immediately. Update every integration with the new one.";
  }
  return "The token is shown once. Store it in the integration's secret file right away.";
});

function revokeCopy(): string {
  if (status.value?.source === "env") {
    return "This removes the stored WebUI token. The container-config token keeps working.";
  }
  return "Integrations using this token get 401 responses until you generate a new one.";
}

function openConfirm(action: ReadonlyTokenAction): void {
  actionError.value = "";
  confirmAction.value = action;
}

function clearToken(): void {
  newToken.value = "";
  copyMessage.value = "";
}

async function runConfirmedAction(): Promise<void> {
  const action = confirmAction.value;
  if (action === null) {
    return;
  }
  working.value = true;
  clearToken();
  try {
    const response = await settings.changeReadonlyToken(action);
    newToken.value = response.token;
  } catch (error_) {
    actionError.value =
      error_ instanceof Error ? error_.message : "Could not change the token";
    // The token may have changed elsewhere, such as a CLI revoke; show the
    // current status instead of a stale one.
    await settings.loadSettings().catch(() => undefined);
  } finally {
    working.value = false;
    confirmAction.value = null;
  }
}

async function copyToken(): Promise<void> {
  await copy(newToken.value);
  copyMessage.value = "Copied.";
}

onBeforeUnmount(clearToken);
</script>

<template>
  <section
    v-if="status"
    id="settings-integrations"
    class="section-panel"
    data-test="settings-integrations"
  >
    <div class="section-heading">
      <div class="section-heading-main">
        <p class="eyebrow">Integrations</p>
        <h2>Read-only API token</h2>
        <p class="section-copy">
          Lets dashboards, bots, and scripts read a compact update summary from
          <code>/api/integrations/v1</code>. The token can only read: it cannot
          apply updates, change settings, or use any other WUDup route.
        </p>
      </div>
      <KeyRound :size="20" class="section-heading-icon" />
    </div>

    <dl class="integration-facts">
      <div>
        <dt>Status</dt>
        <dd data-test="readonly-token-source">{{ sourceLabel }}</dd>
      </div>
      <div v-if="status.fingerprint">
        <dt>Fingerprint</dt>
        <dd><code>{{ status.fingerprint }}</code></dd>
      </div>
      <div v-if="createdLabel">
        <dt>Created</dt>
        <dd>{{ createdLabel }}</dd>
      </div>
      <div v-if="status.stored_fingerprint">
        <dt>Stored WebUI token</dt>
        <dd data-test="readonly-token-stored"><code>{{ status.stored_fingerprint }}</code></dd>
      </div>
    </dl>

    <n-alert
      v-if="newToken"
      type="warning"
      :show-icon="false"
      class="settings-action-alert"
      data-test="readonly-token-once"
    >
      <p class="integration-once-copy">
        Copy this token now. WUDup stores only a hash, so it cannot show it again.
      </p>
      <code class="integration-token wrap-anywhere">{{ newToken }}</code>
      <div class="integration-once-actions">
        <n-button v-if="isClipboardSupported" size="small" secondary @click="copyToken">
          <template #icon>
            <Copy :size="14" />
          </template>
          Copy
        </n-button>
        <n-button size="small" @click="clearToken">Done</n-button>
        <span v-if="copyMessage">{{ copyMessage }}</span>
      </div>
    </n-alert>

    <n-alert
      v-if="actionError"
      type="error"
      :show-icon="false"
      class="settings-action-alert"
    >
      {{ actionError }}
    </n-alert>

    <div class="settings-action-row">
      <n-button
        type="primary"
        secondary
        :disabled="!status.editable"
        data-test="readonly-token-rotate"
        @click="openConfirm('rotate')"
      >
        {{ hasToken && status.source === "webui" ? "Replace token" : "Generate token" }}
      </n-button>
      <n-button
        v-if="status.source === 'webui' || status.stored_fingerprint"
        type="error"
        secondary
        :disabled="!status.revocable"
        data-test="readonly-token-revoke"
        @click="openConfirm('revoke')"
      >
        Revoke
      </n-button>
    </div>
    <n-alert
      v-if="status.disabled_reason"
      type="info"
      :show-icon="false"
      class="settings-action-alert"
      data-test="readonly-token-disabled"
    >
      {{ status.disabled_reason }}
    </n-alert>

    <n-modal
      v-model:show="confirmVisible"
      preset="dialog"
      :title="confirmTitle"
      :positive-text="confirmAction === 'revoke' ? 'Revoke token' : 'Generate token'"
      negative-text="Cancel"
      :positive-button-props="{
        type: confirmAction === 'revoke' ? 'error' : 'primary',
        loading: working,
      }"
      @positive-click="runConfirmedAction"
    >
      <p class="settings-dialog-copy">{{ confirmCopy }}</p>
    </n-modal>
  </section>
</template>

<style scoped>
#settings-integrations {
  scroll-margin-top: 18px;
}

.integration-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 24px;
  margin: 12px 0 0;
}

.integration-facts dt {
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.integration-facts dd {
  margin: 0;
  color: var(--color-ink);
}

.integration-once-copy {
  margin: 0 0 8px;
}

.integration-token {
  display: block;
  padding: 8px;
  border-radius: 6px;
  background: var(--color-surface);
  user-select: all;
}

.integration-once-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.settings-action-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border-subtle);
}

.settings-action-alert {
  margin-top: 12px;
}

.settings-dialog-copy {
  margin: 0;
  line-height: 1.45;
}

@media (--wud-compact) {
  .settings-action-row :deep(.n-button) {
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }
}
</style>
