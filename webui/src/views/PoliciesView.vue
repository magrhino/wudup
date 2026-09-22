<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Edit3, Save, Trash2 } from "@lucide/vue";
import {
  NAlert,
  NButton,
  NEmpty,
  NFlex,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NSelect,
  NSwitch,
  NTag,
} from "naive-ui";

import { useRouteRefresh } from "../components/app/routeRefresh";
import type {
  AutoUpdateDay,
  ServicePolicyRecord,
  ServicePolicyUpdateMode,
} from "../api/client";
import { useUpdateTargetOptions } from "../composables/useUpdateTargetOptions";
import { usePolicyManagementCardsBreakpoint } from "../responsive";
import { useAuthStore } from "../stores/auth";
import { useSettingsStore } from "../stores/settings";
import { useConnectionStore } from "../stores/connection";
import { useUpdatesStore } from "../stores/updates";
import { runInBackground } from "../utils/promises";

const settings = useSettingsStore();
const route = useRoute();
const connection = useConnectionStore();
const updates = useUpdatesStore();
const auth = useAuthStore();
const { serviceKeyOptions } = useUpdateTargetOptions();
const useManagementCards = usePolicyManagementCardsBreakpoint();
const showSaveConfirm = ref(false);
const showDeleteConfirm = ref(false);
const deleteTarget = ref<ServicePolicyRecord | null>(null);

const policyForm = reactive({
  serviceKey: typeof route?.query?.service === "string" ? route.query.service : "",
  updateMode: "" as ServicePolicyUpdateMode,
  autoUpdate: false,
  autoUpdateTime: "",
  autoUpdateDays: [] as AutoUpdateDay[],
  snoozeDefaultSeconds: null as number | null,
});

const AUTO_UPDATE_TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;
const dayLabels: Record<AutoUpdateDay, string> = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

const updateModeOptions = [
  { label: "Default", value: "" },
  { label: "Pause", value: "pause" },
  { label: "Stop", value: "stop" },
  { label: "Live", value: "live" },
];
const weekdayOptions = Object.entries(dayLabels).map(([value, label]) => ({
  label,
  value,
}));

const mutationsEnabled = computed(
  () => auth.session?.mutations_enabled === true,
);
const timezoneKnown = computed(() => connection.status !== null);
const timezoneLabel = computed(() => connection.status?.timezone ?? "loading");
const scheduleFeedback = computed(() =>
  timezoneKnown.value
    ? `Server timezone: ${timezoneLabel.value}`
    : "Server timezone is loading.",
);
const autoUpdateScheduleValid = computed(
  () =>
    !policyForm.autoUpdate ||
    (AUTO_UPDATE_TIME_PATTERN.test(policyForm.autoUpdateTime) &&
      policyForm.autoUpdateDays.length > 0),
);
const formScheduleLabel = computed(() => {
  if (!policyForm.autoUpdate) {
    return "Disabled";
  }
  const time = normalizedAutoUpdateTime();
  if (time === null || policyForm.autoUpdateDays.length === 0) {
    return "Not scheduled";
  }
  const days = policyForm.autoUpdateDays.map((day) => dayLabels[day]).join(", ");
  return `${days} at ${time}`;
});
const saveDisabled = computed(
  () =>
    !mutationsEnabled.value ||
    !policyForm.serviceKey.trim() ||
    !autoUpdateScheduleValid.value ||
    (policyForm.autoUpdate && !timezoneKnown.value) ||
    settings.loading,
);
const viewError = computed(
  () => connection.error || updates.error || settings.error,
);

function editPolicy(policy: ServicePolicyRecord): void {
  policyForm.serviceKey = policy.service_key;
  policyForm.updateMode = policy.update_mode as ServicePolicyUpdateMode;
  policyForm.autoUpdate = policy.auto_update;
  policyForm.autoUpdateTime = policy.auto_update_time ?? "";
  policyForm.autoUpdateDays = [...policy.auto_update_days];
  policyForm.snoozeDefaultSeconds = policy.snooze_default_seconds;
}

function resetPolicyForm(): void {
  policyForm.serviceKey = "";
  policyForm.updateMode = "";
  policyForm.autoUpdate = false;
  policyForm.autoUpdateTime = "";
  policyForm.autoUpdateDays = [];
  policyForm.snoozeDefaultSeconds = null;
}

watch(() => route?.query?.service, (value) => {
  showSaveConfirm.value = false;
  resetPolicyForm();
  policyForm.serviceKey = typeof value === "string" ? value : "";
});

function setPolicyServiceKey(value: string | number | null): void {
  policyForm.serviceKey = value === null ? "" : String(value);
}

function modeLabel(mode: string): string {
  return mode || "default";
}

function snoozeLabel(seconds: number | null): string {
  return seconds === null ? "None" : `${seconds}s`;
}

function scheduleLabel(policy: ServicePolicyRecord): string {
  if (!policy.auto_update) {
    return "Disabled";
  }
  if (policy.auto_update_time === null || policy.auto_update_days.length === 0) {
    return "Not scheduled";
  }
  const days = policy.auto_update_days.map((day) => dayLabels[day]).join(", ");
  return `${days} at ${policy.auto_update_time}`;
}

function normalizedSnoozeSeconds(): number | null {
  if (policyForm.snoozeDefaultSeconds === null) {
    return null;
  }
  return Math.max(0, Math.trunc(policyForm.snoozeDefaultSeconds));
}

function normalizedAutoUpdateTime(): string | null {
  const value = policyForm.autoUpdateTime.trim();
  return AUTO_UPDATE_TIME_PATTERN.test(value) ? value : null;
}

function openSaveConfirm(): void {
  if (saveDisabled.value) {
    return;
  }
  showSaveConfirm.value = true;
}

async function confirmSave(): Promise<void> {
  if (saveDisabled.value) {
    return;
  }
  await settings.upsertServicePolicy(
    policyForm.serviceKey.trim(),
    policyForm.updateMode,
    policyForm.autoUpdate,
    normalizedSnoozeSeconds(),
    normalizedAutoUpdateTime(),
    [...policyForm.autoUpdateDays],
  );
}

function openDeleteConfirm(policy: ServicePolicyRecord): void {
  if (!mutationsEnabled.value) {
    return;
  }
  deleteTarget.value = policy;
  showDeleteConfirm.value = true;
}

async function confirmDelete(): Promise<void> {
  if (deleteTarget.value === null) {
    return;
  }
  await settings.deleteServicePolicy(deleteTarget.value.service_key);
  if (deleteTarget.value.service_key === policyForm.serviceKey) {
    resetPolicyForm();
  }
  deleteTarget.value = null;
}

onMounted(() => {
  if (connection.status === null) {
    runInBackground(connection.loadStatus());
  }
  runInBackground(updates.loadUpdateTargets());
  runInBackground(settings.loadServicePolicies());
});

useRouteRefresh(() => settings.loadServicePolicies());
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="viewError" type="error" :show-icon="false">
      {{ viewError }}
    </n-alert>
    <n-alert v-if="!mutationsEnabled" type="info" :show-icon="false">
      Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to edit policies.
    </n-alert>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Service policy</p>
          <h2>{{ policyForm.serviceKey ? "Edit policy" : "New policy" }}</h2>
        </div>
      </div>
      <n-form class="management-form" @submit.prevent="openSaveConfirm">
        <n-form-item
          label="Service key"
          required
          feedback="Required to save a policy. Use stack/service."
        >
          <n-select
            :value="policyForm.serviceKey"
            filterable
            tag
            clearable
            :options="serviceKeyOptions"
            placeholder="stack/service"
            :disabled="settings.loading"
            @update:value="setPolicyServiceKey"
          />
        </n-form-item>
        <n-form-item label="Update mode">
          <n-select
            v-model:value="policyForm.updateMode"
            :options="updateModeOptions"
            :disabled="settings.loading"
          />
        </n-form-item>
        <n-form-item label="Auto update">
          <n-switch v-model:value="policyForm.autoUpdate" :disabled="settings.loading" />
        </n-form-item>
        <n-form-item
          :label="`Update time (${timezoneLabel})`"
          :feedback="scheduleFeedback"
        >
          <n-input
            v-model:value="policyForm.autoUpdateTime"
            placeholder="09:30"
            :maxlength="5"
            :disabled="settings.loading"
          />
        </n-form-item>
        <n-form-item label="Update days">
          <n-select
            v-model:value="policyForm.autoUpdateDays"
            multiple
            :options="weekdayOptions"
            :disabled="settings.loading"
          />
        </n-form-item>
        <n-form-item
          label="Default snooze seconds"
          feedback="Optional. Leave empty for no default snooze."
        >
          <n-input-number
            v-model:value="policyForm.snoozeDefaultSeconds"
            clearable
            :min="0"
            :show-button="false"
            :disabled="settings.loading"
          />
        </n-form-item>
        <div class="form-actions">
          <n-button quaternary :disabled="settings.loading" @click="resetPolicyForm">
            Clear form
          </n-button>
          <n-button
            type="primary"
            attr-type="submit"
            :disabled="saveDisabled"
            :loading="settings.loading"
          >
            <template #icon>
              <Save :size="16" />
            </template>
            Save policy
          </n-button>
        </div>
      </n-form>
    </section>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SQLite state</p>
          <h2>{{ settings.servicePolicies.length }} service policies</h2>
        </div>
      </div>

      <div v-if="!useManagementCards" class="management-table policy-table">
        <div class="management-table-head">
          <span>Service</span>
          <span>Mode</span>
          <span>Auto</span>
          <span>Schedule</span>
          <span>Snooze</span>
          <span>Actions</span>
        </div>
        <div
          v-for="policy in settings.servicePolicies"
          :key="policy.service_key"
          class="management-row"
        >
          <strong>{{ policy.service_key }}</strong>
          <n-tag size="small">{{ modeLabel(policy.update_mode) }}</n-tag>
          <span>{{ policy.auto_update ? "Yes" : "No" }}</span>
          <span>{{ scheduleLabel(policy) }}</span>
          <span>{{ snoozeLabel(policy.snooze_default_seconds) }}</span>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button size="small" quaternary @click="editPolicy(policy)">
              <template #icon>
                <Edit3 :size="15" />
              </template>
              Edit
            </n-button>
            <n-button
              size="small"
              quaternary
              type="error"
              :disabled="!mutationsEnabled"
              @click="openDeleteConfirm(policy)"
            >
              <template #icon>
                <Trash2 :size="15" />
              </template>
              Delete
            </n-button>
          </n-flex>
        </div>
      </div>

      <div v-else class="mobile-list">
        <article
          v-for="policy in settings.servicePolicies"
          :key="policy.service_key"
          class="mobile-card"
        >
          <div class="mobile-card-title">
            <strong>{{ policy.service_key }}</strong>
            <n-tag size="small">{{ modeLabel(policy.update_mode) }}</n-tag>
          </div>
          <dl>
            <div>
              <dt>Auto update</dt>
              <dd>{{ policy.auto_update ? "Yes" : "No" }}</dd>
            </div>
            <div>
              <dt>Schedule</dt>
              <dd>{{ scheduleLabel(policy) }}</dd>
            </div>
            <div>
              <dt>Snooze</dt>
              <dd>{{ snoozeLabel(policy.snooze_default_seconds) }}</dd>
            </div>
          </dl>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button size="small" quaternary @click="editPolicy(policy)">
              <template #icon>
                <Edit3 :size="15" />
              </template>
              Edit
            </n-button>
            <n-button
              size="small"
              quaternary
              type="error"
              :disabled="!mutationsEnabled"
              @click="openDeleteConfirm(policy)"
            >
              <template #icon>
                <Trash2 :size="15" />
              </template>
              Delete
            </n-button>
          </n-flex>
        </article>
      </div>
      <n-empty
        v-if="!settings.servicePolicies.length"
        class="empty-state"
        description="No service policies."
        :show-icon="false"
      />
    </section>

    <n-modal
      v-model:show="showSaveConfirm"
      preset="dialog"
      title="Save service policy"
      positive-text="Save policy"
      negative-text="Cancel"
      :positive-button-props="{ type: 'primary', loading: settings.loading }"
      @positive-click="confirmSave"
    >
      <div class="confirmation-list">
        <div>
          <span>Service</span>
          <strong>{{ policyForm.serviceKey.trim() }}</strong>
        </div>
        <div>
          <span>Mode</span>
          <strong>{{ modeLabel(policyForm.updateMode) }}</strong>
        </div>
        <div>
          <span>Auto update</span>
          <strong>{{ policyForm.autoUpdate ? "Yes" : "No" }}</strong>
        </div>
        <div>
          <span>Schedule</span>
          <strong>{{ formScheduleLabel }}</strong>
        </div>
        <div>
          <span>Snooze</span>
          <strong>{{ snoozeLabel(normalizedSnoozeSeconds()) }}</strong>
        </div>
      </div>
    </n-modal>

    <n-modal
      v-model:show="showDeleteConfirm"
      preset="dialog"
      title="Delete service policy"
      positive-text="Delete"
      negative-text="Cancel"
      :positive-button-props="{ type: 'error', loading: settings.loading }"
      @positive-click="confirmDelete"
    >
      <div v-if="deleteTarget" class="confirmation-list">
        <div>
          <span>Service</span>
          <strong>{{ deleteTarget.service_key }}</strong>
        </div>
      </div>
    </n-modal>
  </section>
</template>

<style scoped>
.policy-table .management-table-head,
.policy-table .management-row {
  grid-template-columns:
    minmax(180px, 1.5fr) minmax(86px, 0.6fr) minmax(72px, 0.4fr) minmax(150px, 0.8fr) minmax(96px, 0.5fr) minmax(184px, auto);
}
</style>
