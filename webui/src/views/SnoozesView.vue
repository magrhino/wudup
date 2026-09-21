<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Plus, Trash2 } from "@lucide/vue";
import {
  NAlert,
  NButton,
  NEmpty,
  NFlex,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  NTag,
} from "naive-ui";

import type { SnoozeKind, SnoozeRecord, SnoozeState } from "../api/client";
import { useRouteRefresh } from "../components/app/routeRefresh";
import { useUpdateTargetOptions } from "../composables/useUpdateTargetOptions";
import { useManagementCardsBreakpoint } from "../responsive";
import { useAuthStore } from "../stores/auth";
import { useSettingsStore } from "../stores/settings";
import { useUpdatesStore } from "../stores/updates";
import { runInBackground } from "../utils/promises";

const settings = useSettingsStore();
const route = useRoute();
const updates = useUpdatesStore();
const auth = useAuthStore();
const { serviceKeyOptions } = useUpdateTargetOptions();
const useManagementCards = useManagementCardsBreakpoint();
const snoozeState = ref<SnoozeState>("active");
const snoozeKind = ref<SnoozeKind>("time");
const showCreateConfirm = ref(false);
const showDeleteConfirm = ref(false);
const deleteTarget = ref<SnoozeRecord | null>(null);

const snoozeForm = reactive({
  serviceKey: typeof route?.query?.service === "string" ? route.query.service : "",
  waitForServiceKey: "",
  snoozedUntil: futureIso(24),
  reason: "",
});

const kindOptions = [
  { label: "Until time", value: "time" },
  { label: "Until another service updates successfully", value: "dependency" },
];
const stateOptions = [
  { label: "Active", value: "active" },
  { label: "Expired", value: "expired" },
  { label: "All", value: "all" },
];

const mutationsEnabled = computed(
  () => auth.session?.mutations_enabled === true,
);
const createDisabled = computed(
  () =>
    !mutationsEnabled.value ||
    !snoozeForm.serviceKey.trim() ||
    (snoozeKind.value === "time" && !snoozeForm.snoozedUntil.trim()) ||
    (snoozeKind.value === "dependency" &&
      (!snoozeForm.waitForServiceKey.trim() ||
        snoozeForm.waitForServiceKey.trim() === snoozeForm.serviceKey.trim())) ||
    settings.loading,
);
const viewError = computed(() => updates.error || settings.error);

function futureIso(hours: number): string {
  return new Date(Date.now() + hours * 60 * 60 * 1000).toISOString();
}

function setFuture(hours: number): void {
  snoozeForm.snoozedUntil = futureIso(hours);
}

function resetSnoozeForm(): void {
  snoozeForm.serviceKey = "";
  snoozeForm.waitForServiceKey = "";
  snoozeForm.snoozedUntil = futureIso(24);
  snoozeForm.reason = "";
}

watch(() => route?.query?.service, (value) => {
  showCreateConfirm.value = false;
  resetSnoozeForm();
  snoozeForm.serviceKey = typeof value === "string" ? value : "";
});

function setSnoozeServiceKey(value: string | number | null): void {
  snoozeForm.serviceKey = value === null ? "" : String(value);
}

function setWaitForServiceKey(value: string | number | null): void {
  snoozeForm.waitForServiceKey = value === null ? "" : String(value);
}

function statusLabel(snooze: SnoozeRecord): string {
  if (snooze.kind === "dependency") {
    return snooze.active ? "waiting" : "satisfied";
  }
  return snooze.active ? "active" : "expired";
}

function conditionLabel(snooze: SnoozeRecord): string {
  if (snooze.kind === "dependency") {
    return snooze.wait_for_service_key
      ? `Until ${snooze.wait_for_service_key} updates successfully`
      : "Dependency not set";
  }
  return snooze.snoozed_until ?? "None";
}

function openCreateConfirm(): void {
  if (createDisabled.value) {
    return;
  }
  showCreateConfirm.value = true;
}

async function confirmCreate(): Promise<void> {
  if (createDisabled.value) {
    return;
  }
  if (snoozeKind.value === "dependency") {
    await settings.createDependencySnooze(
      snoozeForm.serviceKey.trim(),
      snoozeForm.waitForServiceKey.trim(),
      snoozeForm.reason.trim(),
      snoozeState.value,
    );
  } else {
    await settings.createSnooze(
      snoozeForm.serviceKey.trim(),
      snoozeForm.snoozedUntil.trim(),
      snoozeForm.reason.trim(),
      snoozeState.value,
    );
  }
  resetSnoozeForm();
}

function openDeleteConfirm(snooze: SnoozeRecord): void {
  if (!mutationsEnabled.value) {
    return;
  }
  deleteTarget.value = snooze;
  showDeleteConfirm.value = true;
}

async function confirmDelete(): Promise<void> {
  if (deleteTarget.value === null) {
    return;
  }
  await settings.deleteSnooze(
    deleteTarget.value.id,
    snoozeState.value,
    deleteTarget.value.kind,
  );
  deleteTarget.value = null;
}

onMounted(() => {
  runInBackground(updates.loadUpdateTargets());
  runInBackground(settings.loadSnoozes(snoozeState.value));
});

watch(snoozeState, (nextState) => {
  runInBackground(settings.loadSnoozes(nextState));
});

useRouteRefresh(() => settings.loadSnoozes(snoozeState.value));
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="viewError" type="error" :show-icon="false">
      {{ viewError }}
    </n-alert>
    <n-alert v-if="!mutationsEnabled" type="info" :show-icon="false">
      Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to manage snoozes.
    </n-alert>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Service snooze</p>
          <h2>New snooze</h2>
        </div>
      </div>
      <n-form class="management-form" @submit.prevent="openCreateConfirm">
        <n-form-item label="Snooze type" required>
          <n-select
            v-model:value="snoozeKind"
            :options="kindOptions"
            :disabled="settings.loading"
            aria-label="Snooze type"
          />
        </n-form-item>
        <n-form-item
          label="Service key"
          required
          feedback="Service to hold back. Use stack/service."
        >
          <n-select
            :value="snoozeForm.serviceKey"
            filterable
            tag
            clearable
            :options="serviceKeyOptions"
            placeholder="stack/service"
            :disabled="settings.loading"
            @update:value="setSnoozeServiceKey"
          />
        </n-form-item>
        <n-form-item
          v-if="snoozeKind === 'dependency'"
          label="Wait for service"
          required
          feedback="The held service is released once this service updates successfully."
        >
          <n-select
            :value="snoozeForm.waitForServiceKey"
            filterable
            tag
            clearable
            :options="serviceKeyOptions"
            placeholder="stack/service"
            :disabled="settings.loading"
            @update:value="setWaitForServiceKey"
          />
        </n-form-item>
        <n-form-item
          v-else
          label="Snoozed until"
          required
          feedback="Use an ISO timestamp, or choose 1h, 1d, or 7d."
        >
          <n-input
            v-model:value="snoozeForm.snoozedUntil"
            placeholder="YYYY-MM-DDTHH:MM:SSZ"
            :disabled="settings.loading"
          />
        </n-form-item>
        <n-form-item label="Reason">
          <n-input
            v-model:value="snoozeForm.reason"
            placeholder="maintenance"
            :disabled="settings.loading"
          />
        </n-form-item>
        <div class="form-actions">
          <n-button
            v-if="snoozeKind === 'time'"
            size="small"
            quaternary
            :disabled="settings.loading"
            @click="setFuture(1)"
          >
            1h
          </n-button>
          <n-button
            v-if="snoozeKind === 'time'"
            size="small"
            quaternary
            :disabled="settings.loading"
            @click="setFuture(24)"
          >
            1d
          </n-button>
          <n-button
            v-if="snoozeKind === 'time'"
            size="small"
            quaternary
            :disabled="settings.loading"
            @click="setFuture(168)"
          >
            7d
          </n-button>
          <n-button quaternary :disabled="settings.loading" @click="resetSnoozeForm">
            Clear form
          </n-button>
          <n-button
            type="primary"
            attr-type="submit"
            :disabled="createDisabled"
            :loading="settings.loading"
          >
            <template #icon>
              <Plus :size="16" />
            </template>
            Create snooze
          </n-button>
        </div>
      </n-form>
    </section>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SQLite state</p>
          <h2>{{ settings.snoozes.length }} snoozes</h2>
        </div>
        <n-select
          v-model:value="snoozeState"
          class="filter-control"
          :options="stateOptions"
          :disabled="settings.loading"
          aria-label="Filter snoozes by state"
        />
      </div>

      <div v-if="!useManagementCards" class="management-table snooze-table">
        <div class="management-table-head">
          <span>Service</span>
          <span>Condition</span>
          <span>Reason</span>
          <span>Status</span>
          <span>Actions</span>
        </div>
        <div
          v-for="snooze in settings.snoozes"
          :key="`${snooze.kind}-${snooze.id}`"
          class="management-row"
        >
          <strong>{{ snooze.service_key }}</strong>
          <span>{{ conditionLabel(snooze) }}</span>
          <span>{{ snooze.reason || "None" }}</span>
          <n-tag size="small" :type="snooze.active ? 'info' : 'default'">
            {{ statusLabel(snooze) }}
          </n-tag>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button
              size="small"
              quaternary
              type="error"
              :disabled="!mutationsEnabled"
              @click="openDeleteConfirm(snooze)"
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
          v-for="snooze in settings.snoozes"
          :key="`${snooze.kind}-${snooze.id}`"
          class="mobile-card"
        >
          <div class="mobile-card-title">
            <strong>{{ snooze.service_key }}</strong>
            <n-tag size="small" :type="snooze.active ? 'info' : 'default'">
              {{ statusLabel(snooze) }}
            </n-tag>
          </div>
          <dl>
            <div>
              <dt>Condition</dt>
              <dd>{{ conditionLabel(snooze) }}</dd>
            </div>
            <div>
              <dt>Reason</dt>
              <dd>{{ snooze.reason || "None" }}</dd>
            </div>
          </dl>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button
              size="small"
              quaternary
              type="error"
              :disabled="!mutationsEnabled"
              @click="openDeleteConfirm(snooze)"
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
        v-if="!settings.snoozes.length"
        class="empty-state"
        description="No snoozes."
        :show-icon="false"
      />
    </section>

    <n-modal
      v-model:show="showCreateConfirm"
      preset="dialog"
      title="Create snooze"
      positive-text="Create snooze"
      negative-text="Cancel"
      :positive-button-props="{ type: 'primary', loading: settings.loading }"
      @positive-click="confirmCreate"
    >
      <div class="confirmation-list">
        <div>
          <span>Service</span>
          <strong>{{ snoozeForm.serviceKey.trim() }}</strong>
        </div>
        <div>
          <span>Type</span>
          <strong>{{ snoozeKind === "dependency" ? "Dependency" : "Time" }}</strong>
        </div>
        <div>
          <span>Condition</span>
          <strong>
            <template v-if="snoozeKind === 'dependency'">
              Until {{ snoozeForm.waitForServiceKey.trim() }} updates successfully
            </template>
            <template v-else>
              {{ snoozeForm.snoozedUntil.trim() }}
            </template>
          </strong>
        </div>
        <div>
          <span>Reason</span>
          <strong>{{ snoozeForm.reason.trim() || "None" }}</strong>
        </div>
      </div>
    </n-modal>

    <n-modal
      v-model:show="showDeleteConfirm"
      preset="dialog"
      title="Delete snooze"
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
        <div>
          <span>Condition</span>
          <strong>{{ conditionLabel(deleteTarget) }}</strong>
        </div>
      </div>
    </n-modal>
  </section>
</template>

<style scoped>
.snooze-table .management-table-head,
.snooze-table .management-row {
  grid-template-columns:
    minmax(160px, 1fr) minmax(220px, 1fr) minmax(150px, 1fr) minmax(86px, 0.4fr) minmax(110px, auto);
}
</style>
