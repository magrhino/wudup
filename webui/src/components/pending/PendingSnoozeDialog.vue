<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { NAlert, NFormItem, NInput, NModal, NSelect } from "naive-ui";

import { useSettingsStore } from "../../stores/settings";
import { errorMessage } from "../../stores/storeState";
import { displayDigest } from "../../utils/digestProvenance";
import {
  groupedItemServiceKeys,
  groupedItemTarget,
  previewChangeLabels,
} from "../../views/pending/pendingDisplay";
import {
  activeSnoozedServiceKeys,
  type SnoozedPendingItem,
} from "../../views/pending/snoozeSelection";

const props = defineProps<{
  target: SnoozedPendingItem | null;
}>();

const emit = defineEmits<{
  close: [];
  snoozed: [target: SnoozedPendingItem, message: string];
}>();

// Matches the backend CreateSnoozeOperation.reason limit.
const reasonMaxLength = 1024;

const settings = useSettingsStore();
const durationHours = ref(24);
const reason = ref("");
const error = ref("");
const saving = ref(false);

const durationOptions = [
  { label: "1 day", value: 24 },
  { label: "3 days", value: 72 },
  { label: "1 week", value: 168 },
  { label: "30 days", value: 720 },
];

const serviceKeys = computed(() =>
  props.target ? groupedItemServiceKeys(props.target.group, props.target.item) : [],
);
const changeLabels = computed(() =>
  props.target
    ? previewChangeLabels(
        props.target.item.image,
        groupedItemTarget(props.target.item),
        displayDigest,
      )
    : null,
);
const untilLabel = computed(() =>
  new Date(futureMs(durationHours.value)).toLocaleString(),
);

watch(
  () => props.target,
  () => {
    durationHours.value = 24;
    reason.value = "";
    error.value = "";
  },
);

function futureMs(hours: number): number {
  return Date.now() + Number(hours) * 60 * 60 * 1000;
}

function updateShow(show: boolean): void {
  if (!show && !saving.value) {
    emit("close");
  }
}

async function confirmSnooze(): Promise<boolean> {
  const target = props.target;
  const keys = serviceKeys.value;
  if (!target || !keys.length || saving.value) {
    return false;
  }
  const snoozedUntil = new Date(futureMs(durationHours.value));
  saving.value = true;
  error.value = "";
  let failure: unknown = null;
  let confirmed = true;
  try {
    // Saved one at a time: each save reloads the active snoozes, so the next
    // duplicate check and any partial-failure report see every earlier save.
    for (const serviceKey of keys) {
      // A retry after a partial failure must not duplicate snoozes already saved.
      if (activeSnoozedServiceKeys(settings.snoozes).has(serviceKey)) {
        continue;
      }
      await settings.createSnooze(
        serviceKey,
        snoozedUntil.toISOString(),
        reason.value.trim(),
        "active",
      );
    }
  } catch (error_) {
    failure = error_;
    // The save may have committed before the refresh failed; re-read to know.
    try {
      await settings.loadSnoozes("active");
    } catch {
      confirmed = false;
    }
  } finally {
    saving.value = false;
  }

  const active = activeSnoozedServiceKeys(settings.snoozes);
  const missing = keys.filter((key) => !active.has(key));
  if (failure !== null && (missing.length || !confirmed)) {
    const detail = errorMessage(failure);
    const saved = keys.filter((key) => active.has(key));
    if (!confirmed) {
      error.value = `WUDup could not confirm whether the snooze was saved: ${detail}. Check Snoozes before trying again.`;
    } else if (saved.length) {
      error.value = `Snoozed ${saved.join(", ")}, but ${missing.join(", ")} was not saved: ${detail}. Try again to snooze the rest.`;
    } else {
      error.value = `Snooze was not saved: ${detail}`;
    }
    return false;
  }
  emit(
    "snoozed",
    target,
    `Snoozed ${keys.join(", ")} until ${snoozedUntil.toLocaleString()}.`,
  );
  emit("close");
  return true;
}
</script>

<template>
  <n-modal
    :show="target !== null"
    preset="dialog"
    title="Snooze update"
    positive-text="Snooze"
    negative-text="Cancel"
    :closable="!saving"
    :close-on-esc="!saving"
    :mask-closable="!saving"
    :positive-button-props="{
      type: 'primary',
      loading: saving,
      disabled: !serviceKeys.length,
    }"
    :negative-button-props="{ disabled: saving }"
    @update:show="updateShow"
    @positive-click="confirmSnooze"
  >
    <div v-if="target" class="pending-snooze-dialog">
      <div class="confirmation-list">
        <div>
          <span>{{ serviceKeys.length === 1 ? "Service" : "Services" }}</span>
          <strong>{{ serviceKeys.join(", ") || "None matched" }}</strong>
        </div>
      </div>
      <p v-if="changeLabels" class="pending-snooze-change" aria-label="Update to snooze">
        <code :title="target.item.image">{{ changeLabels.current }}</code>
        <span aria-hidden="true">-&gt;</span>
        <code :title="groupedItemTarget(target.item)">{{ changeLabels.target }}</code>
      </p>
      <n-form-item label="Snooze for" :show-feedback="false">
        <n-select
          v-model:value="durationHours"
          :options="durationOptions"
          :disabled="saving"
          aria-label="Snooze duration"
          aria-describedby="pending-snooze-until"
        />
      </n-form-item>
      <p id="pending-snooze-until" class="pending-snooze-until">
        Holds every update for
        {{ serviceKeys.length === 1 ? "this service" : "these services" }}
        until {{ untilLabel }}, then returns it to Pending.
      </p>
      <n-form-item label="Reason" :show-feedback="false">
        <n-input
          v-model:value="reason"
          :disabled="saving"
          :maxlength="reasonMaxLength"
          placeholder="Optional"
          aria-label="Snooze reason"
        />
      </n-form-item>
      <p class="pending-snooze-note">
        Saved in WUDup only. Compose files and WUD labels are not changed.
        Delete it from Snoozes to end it early.
      </p>
      <n-alert v-if="error" type="error" :show-icon="false" role="alert">
        {{ error }}
      </n-alert>
    </div>
  </n-modal>
</template>

<style scoped>
.pending-snooze-dialog {
  display: grid;
  gap: 12px;
}

.pending-snooze-change {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 7px;
  min-width: 0;
  margin: 0;
  color: var(--color-muted-text);
}

.pending-snooze-change code {
  color: var(--color-code-text);
  font-family: var(--font-mono);
  font-size: var(--text-data-size, 0.84rem);
  overflow-wrap: anywhere;
}

.pending-snooze-until {
  margin: -6px 0 0;
  color: var(--color-heading);
}

.pending-snooze-note {
  margin: 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}
</style>
