<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { NAlert } from "naive-ui";

import { useSelfUpdateStore } from "../../stores/selfUpdate";
import { runInBackground } from "../../utils/promises";
import AppSelfUpdateBanner from "./AppSelfUpdateBanner.vue";
import AppSelfUpdateDialog from "./AppSelfUpdateDialog.vue";

const selfUpdate = useSelfUpdateStore();

const RELEASES_URL = "https://github.com/magrhino/wudup/releases";
const selfUpdateDialogVisible = ref(false);

const selfUpdateVisible = computed(
  () => selfUpdate.selfUpdate?.status === "available",
);
const selfUpdateButtonDisabled = computed(
  () => selfUpdate.loading || !(selfUpdate.selfUpdate?.can_update ?? false),
);
const selfUpdateStrategy = computed(
  () => selfUpdate.selfUpdate?.strategy ?? "pull_image",
);
const selfUpdateConfirmDisabled = computed(
  () =>
    selfUpdateButtonDisabled.value ||
    (selfUpdateStrategy.value === "prepare_tag_update" &&
      selfUpdate.selfUpdatePlan === null),
);
const selfUpdateActionLabel = computed(() =>
  selfUpdateStrategy.value === "prepare_tag_update"
    ? "Prepare tag update"
    : "Pull image",
);
const selfUpdateActionTitle = computed(() => {
  if (selfUpdateDisabledReason.value) {
    return selfUpdateDisabledReason.value;
  }
  return selfUpdateStrategy.value === "prepare_tag_update"
    ? "Review release notes and prepare tag update"
    : "Review release notes and pull image";
});
const selfUpdateDisabledReason = computed(
  () => selfUpdate.selfUpdate?.disabled_reason ?? "",
);
const selfUpdateFacts = computed(() => {
  const update = selfUpdate.selfUpdate;
  if (!update) {
    return "";
  }
  const image = update.target_image || "image unavailable";
  const container = update.restart_container || "restart target unavailable";
  return `${image} -> ${container}`;
});
const selfUpdateReleaseCapTitle = computed(() => {
  const cap = selfUpdate.selfUpdate?.release_notes_cap ?? 10;
  return `Showing the newest ${cap} matching releases between the running version and latest version. Open GitHub releases for older notes.`;
});
const selfUpdateReleasesUrl = computed(() => {
  const latest = selfUpdate.selfUpdate?.latest_tag;
  return latest
    ? `${RELEASES_URL}/tag/${latest}`
    : RELEASES_URL;
});
const selfUpdatePlanStack = computed(() => selfUpdate.selfUpdatePlan?.plan.stacks[0]);
const selfUpdatePlanTagUpdates = computed(
  () => selfUpdatePlanStack.value?.tag_updates ?? [],
);

async function openSelfUpdateDialog(): Promise<void> {
  selfUpdateDialogVisible.value = true;
  if (
    selfUpdate.selfUpdate?.strategy === "prepare_tag_update" &&
    selfUpdate.selfUpdatePlan === null
  ) {
    await selfUpdate.planSelfUpdate().catch(() => undefined);
  }
}

async function confirmSelfUpdate(): Promise<void> {
  await selfUpdate.applySelfUpdate();
  selfUpdateDialogVisible.value = false;
}

onMounted(() => {
  if (selfUpdate.selfUpdate === null) {
    runInBackground(selfUpdate.loadSelfUpdate());
  }
});
</script>

<template>
  <AppSelfUpdateBanner
    v-if="selfUpdateVisible"
    :current-tag="selfUpdate.selfUpdate?.current_tag"
    :latest-tag="selfUpdate.selfUpdate?.latest_tag"
    :facts="selfUpdateFacts"
    :disabled-reason="selfUpdateDisabledReason"
    :button-disabled="selfUpdateButtonDisabled"
    :action-title="selfUpdateActionTitle"
    :action-label="selfUpdateActionLabel"
    @open="openSelfUpdateDialog"
  />

  <n-alert
    v-if="selfUpdate.selfUpdateMessage"
    class="self-update-message"
    type="success"
  >
    {{ selfUpdate.selfUpdateMessage }}
  </n-alert>
  <n-alert
    v-if="selfUpdate.selfUpdateError"
    class="self-update-message"
    type="error"
  >
    {{ selfUpdate.selfUpdateError }}
  </n-alert>

  <AppSelfUpdateDialog
    v-model:show="selfUpdateDialogVisible"
    :strategy="selfUpdateStrategy"
    :action-label="selfUpdateActionLabel"
    :loading="selfUpdate.loading"
    :confirm-disabled="selfUpdateConfirmDisabled"
    :self-update="selfUpdate.selfUpdate"
    :plan-stack="selfUpdatePlanStack"
    :tag-updates="selfUpdatePlanTagUpdates"
    :release-cap-title="selfUpdateReleaseCapTitle"
    :releases-url="selfUpdateReleasesUrl"
    @confirm="confirmSelfUpdate"
  />
</template>

<style scoped>
.self-update-message {
  margin-bottom: 16px;
}
</style>
