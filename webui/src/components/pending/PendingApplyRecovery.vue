<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { NAlert, NButton, NFlex } from "naive-ui";
import { useRunsStore } from "../../stores/runs";
import { APPLY_JOB_RECOVERY_MESSAGE, useUpdatesStore } from "../../stores/updates";

const props = defineProps<{
  jobId: string;
  runId: number | null;
  acknowledged: boolean;
}>();
const updates = useUpdatesStore();
const runs = useRunsStore();
const checking = ref(false);
const error = ref("");
const checked = ref(false);
const run = computed(() => props.runId === null ? null : runs.runDetails[props.runId]);
const resolved = computed(() => updates.applyJobRecoveryResolved(props.runId));
const title = computed(() => {
  if (resolved.value) return "Update verified";
  if (run.value?.status === "failure") return "Update failed — review required";
  return "Update outcome needs review";
});
const detail = computed(() => {
  if (resolved.value) return "The related run completed successfully and its updates were verified.";
  if (run.value?.finished_at) return "The related run has finished, but the update still needs review. Inspect its verification results and log before retrying.";
  return APPLY_JOB_RECOVERY_MESSAGE;
});

async function checkOutcome(): Promise<void> {
  checking.value = true;
  error.value = "";
  checked.value = false;
  try {
    await updates.reviewApplyJobRecovery(props.jobId);
    checked.value = true;
  } catch (caught) {
    error.value = `Could not check this update. ${caught instanceof Error ? caught.message : "Try again."}`;
  } finally {
    checking.value = false;
  }
}

onMounted(() => {
  if (props.runId !== null) void checkOutcome();
});
</script>

<template>
  <n-alert :type="resolved ? 'success' : 'warning'" class="apply-recovery" role="status">
    <strong>{{ title }}</strong>
    <span v-if="acknowledged"> · Notice acknowledged</span>
    <template v-if="!acknowledged">
      <p>{{ detail }}</p>
      <p class="muted wrap-anywhere">Job {{ jobId }}</p>
      <p v-if="runId === null">
        No related run is known. Review History to identify this update; the latest run may belong to another action.
      </p>
      <p v-if="checked && !resolved">Evidence checked. This outcome still needs review.</p>
    </template>
    <p v-if="error" role="alert">{{ error }}</p>
    <n-flex class="recovery-actions" align="center" :size="8">
      <template v-if="runId !== null">
        <RouterLink class="text-link" :to="{ name: 'run-detail', params: { id: runId } }">Review run #{{ runId }}</RouterLink>
        <RouterLink class="text-link" :to="{ name: 'run-log', params: { id: runId } }">Updater log</RouterLink>
      </template>
      <RouterLink v-else class="text-link" to="/runs">Review History</RouterLink>
      <n-button size="small" secondary :loading="checking" :disabled="checking" @click="checkOutcome">Check outcome</n-button>
      <n-button v-if="resolved" size="small" secondary @click="updates.dismissResolvedApplyJobRecovery(jobId)">Dismiss verified notice</n-button>
      <n-button v-else size="small" secondary @click="updates.acknowledgeApplyJobRecovery(jobId, !acknowledged)">
        {{ acknowledged ? 'Expand notice' : 'Acknowledge and collapse' }}
      </n-button>
    </n-flex>
  </n-alert>
</template>
