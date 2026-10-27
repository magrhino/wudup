<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  NAlert,
  NFormItem,
  NInput,
  NModal,
  NRadioButton,
  NRadioGroup,
} from "naive-ui";

import type { ReleaseSkipScope } from "../../api/client";
import { groupedItemServiceKeys } from "../../views/pending/pendingDisplay";
import {
  releaseSkipChoiceLabel,
  releaseSkipReturnText,
  type ReleaseSkipTarget,
} from "../../views/pending/releaseSkips";

const props = defineProps<{
  error: string;
  loading: boolean;
  show: boolean;
  target: ReleaseSkipTarget | null;
}>();

const emit = defineEmits<{
  close: [];
  confirm: [scope: ReleaseSkipScope, reason: string];
}>();

const scope = ref<ReleaseSkipScope>("release");
const reason = ref("");
// Keep the last target so the dialog does not go blank while it animates closed.
const shownTarget = ref<ReleaseSkipTarget | null>(props.target);

watch(
  () => props.target,
  (target) => {
    if (target) {
      shownTarget.value = target;
      scope.value = "release";
      reason.value = "";
    }
  },
);

const releaseKey = computed(() => shownTarget.value?.item.release_key ?? null);
const lineAvailable = computed(
  () => releaseKey.value?.basis === "version" && Boolean(releaseKey.value.line),
);
const serviceLabel = computed(() =>
  shownTarget.value
    ? groupedItemServiceKeys(shownTarget.value.group, shownTarget.value.item).join(", ")
    : "",
);
const offeredLabel = computed(() => {
  const item = shownTarget.value?.item;
  if (!item) {
    return "";
  }
  return item.target_image || item.resolved_image || item.image;
});
const choiceLabel = computed(() =>
  releaseKey.value ? releaseSkipChoiceLabel(releaseKey.value, scope.value) : "",
);
const returnText = computed(() =>
  releaseKey.value ? releaseSkipReturnText(releaseKey.value, scope.value) : "",
);
const releaseOptionLabel = computed(() => {
  const key = releaseKey.value;
  if (!key) {
    return "";
  }
  if (key.basis === "version") {
    return `Just ${key.label}`;
  }
  return key.basis === "digest" ? "This build" : `Tag ${key.label}`;
});
const title = computed(() =>
  shownTarget.value
    ? `Skip ${choiceLabel.value} for ${serviceLabel.value}?`
    : "Skip release",
);

function handleShowUpdate(value: boolean): void {
  if (!value) {
    emit("close");
  }
}

function confirm(): boolean {
  emit("confirm", lineAvailable.value ? scope.value : "release", reason.value.trim());
  return false;
}
</script>

<template>
  <n-modal
    :show="show"
    preset="dialog"
    :title="title"
    :positive-text="`Skip ${choiceLabel}`"
    negative-text="Cancel"
    :positive-button-props="{ type: 'primary', loading }"
    :mask-closable="!loading"
    class="release-skip-modal"
    @update:show="handleShowUpdate"
    @positive-click="confirm"
    @negative-click="emit('close')"
  >
    <div v-if="shownTarget && releaseKey" class="release-skip-body">
      <div class="confirmation-list">
        <div>
          <span>Service</span>
          <strong>{{ serviceLabel }}</strong>
        </div>
        <div>
          <span>Offered</span>
          <strong>
            <code>{{ offeredLabel }}</code>
          </strong>
        </div>
      </div>

      <n-form-item
        v-if="lineAvailable"
        label="What to skip"
        :show-feedback="false"
      >
        <n-radio-group
          v-model:value="scope"
          size="small"
          aria-label="What to skip"
          :disabled="loading"
        >
          <n-radio-button value="release">{{ releaseOptionLabel }}</n-radio-button>
          <n-radio-button value="line">Whole {{ releaseKey.line }}.x line</n-radio-button>
        </n-radio-group>
      </n-form-item>

      <p class="release-skip-return">
        {{ returnText }}
        <template v-if="releaseKey.basis === 'version'">
          Rebuilds of a skipped release stay skipped.
        </template>
      </p>

      <n-form-item label="What broke?" :show-feedback="false">
        <n-input
          v-model:value="reason"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 4 }"
          maxlength="1024"
          placeholder="Optional. Shown again when the next release arrives."
          :disabled="loading"
        />
      </n-form-item>

      <p class="release-skip-note">
        Saved in WUDup only. Compose files and WUD labels are not changed.
        Applying any update to this service clears the skip.
      </p>

      <n-alert v-if="error" type="error" :show-icon="false">
        {{ error }}
      </n-alert>
    </div>
  </n-modal>
</template>

<style scoped>
.release-skip-body {
  display: grid;
  gap: 14px;
  margin-top: 4px;
}

.release-skip-body code {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  font-weight: 400;
}

.release-skip-return {
  margin: 0;
  color: var(--color-heading);
}

.release-skip-note {
  margin: 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}
</style>
