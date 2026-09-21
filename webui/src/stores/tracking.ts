import { ref } from "vue";
import { defineStore } from "pinia";

import {
  type ApplyJobResponse,
  type TrackedContainersResponse,
  type TrackingRepairPlan,
  webApi,
} from "../api/client";
import { useAuthStore } from "./auth";
import { errorMessage } from "./storeState";

export const useTrackingStore = defineStore("tracking", () => {
  const inventory = ref<TrackedContainersResponse | null>(null);
  const plan = ref<TrackingRepairPlan | null>(null);
  const job = ref<ApplyJobResponse | null>(null);
  const loading = ref(false);
  const planning = ref(false);
  const applying = ref(false);
  const error = ref("");

  async function load(): Promise<void> {
    loading.value = true;
    error.value = "";
    try {
      inventory.value = await webApi.trackedContainers();
    } catch (exc) {
      error.value = errorMessage(exc);
    } finally {
      loading.value = false;
    }
  }

  async function preview(targetId: string, regex: string): Promise<void> {
    planning.value = true;
    error.value = "";
    plan.value = null;
    try {
      plan.value = await webApi.createTrackingRepairPlan(
        targetId,
        regex,
        await useAuthStore().ensureCsrf(),
      );
    } catch (exc) {
      error.value = errorMessage(exc);
    } finally {
      planning.value = false;
    }
  }

  function clearPlan(): void {
    plan.value = null;
  }

  async function apply(): Promise<void> {
    if (!plan.value?.can_apply || applying.value) {
      return;
    }
    applying.value = true;
    error.value = "";
    const selected = plan.value;
    plan.value = null;
    try {
      let current = await webApi.applyTrackingRepair(
        selected,
        await useAuthStore().ensureCsrf(),
      );
      job.value = current;
      while (current.status !== "success" && current.status !== "failure") {
        await new Promise((resolve) => globalThis.setTimeout(resolve, 750));
        current = await webApi.applyJob(current.job_id);
        job.value = current;
      }
      if (current.status === "success") {
        await load();
      } else {
        error.value = current.error || "Tracking repair failed. Review the job before retrying.";
      }
    } catch (exc) {
      error.value = errorMessage(exc);
    } finally {
      applying.value = false;
    }
  }

  return { inventory, plan, job, loading, planning, applying, error, load, preview, clearPlan, apply };
});
