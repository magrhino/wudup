import { ref } from "vue";
import { defineStore } from "pinia";

import {
  ApiError,
  type ApplyJobResponse,
  type TrackedContainersResponse,
  type TrackingRepairPlan,
  webApi,
} from "../api/client";
import { useAuthStore } from "./auth";
import { errorMessage } from "./storeState";

const MAX_APPLY_POLLS = 400; // About five minutes; the server job continues after polling stops.
const TRACKING_JOB_STORAGE_KEY = "trackingRepairJobId";

function trackingJobStorage(): Storage | null {
  try {
    return "sessionStorage" in globalThis ? globalThis.sessionStorage : null;
  } catch {
    return null;
  }
}

function storedTrackingJobId(): string {
  try {
    return trackingJobStorage()?.getItem(TRACKING_JOB_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export const useTrackingStore = defineStore("tracking", () => {
  const inventory = ref<TrackedContainersResponse | null>(null);
  const plan = ref<TrackingRepairPlan | null>(null);
  const job = ref<ApplyJobResponse | null>(null);
  const loading = ref(false);
  const planning = ref(false);
  const applying = ref(false);
  const error = ref("");
  const rememberedJobId = ref(storedTrackingJobId());

  function forgetJob(): void {
    rememberedJobId.value = "";
    try {
      trackingJobStorage()?.removeItem(TRACKING_JOB_STORAGE_KEY);
    } catch {
      // Session storage may be unavailable; the in-memory ID is still cleared.
    }
  }

  function rememberJob(current: ApplyJobResponse): void {
    if (current.status !== "queued" && current.status !== "running") {
      forgetJob();
      return;
    }
    rememberedJobId.value = current.job_id;
    try {
      trackingJobStorage()?.setItem(TRACKING_JOB_STORAGE_KEY, current.job_id);
    } catch {
      // The job remains visible in memory when session storage is unavailable.
    }
  }

  async function load(): Promise<void> {
    loading.value = true;
    error.value = "";
    if (rememberedJobId.value && !applying.value) {
      try {
        job.value = await webApi.applyJob(rememberedJobId.value);
        rememberJob(job.value);
        if (job.value.status === "failure") {
          error.value = job.value.error || `Tracking repair job ${job.value.job_id} failed.`;
        }
      } catch (exc) {
        const missingJobId = rememberedJobId.value;
        if (exc instanceof ApiError && exc.status === 404) {
          forgetJob();
          job.value = null;
          error.value = `Tracking repair job ${missingJobId} is no longer available. Review run history before retrying.`;
        } else {
          error.value = `Could not check tracking repair job ${missingJobId}: ${errorMessage(exc)}`;
        }
      }
    }
    try {
      inventory.value = await webApi.trackedContainers();
    } catch (exc) {
      error.value = [error.value, errorMessage(exc)].filter(Boolean).join(" ");
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
      rememberJob(current);
      for (
        let attempts = 0;
        current.status !== "success" && current.status !== "failure" && attempts < MAX_APPLY_POLLS;
        attempts += 1
      ) {
        await new Promise((resolve) => globalThis.setTimeout(resolve, 750));
        current = await webApi.applyJob(current.job_id);
        job.value = current;
        rememberJob(current);
      }
      if (current.status === "success") {
        await load();
      } else if (current.status === "failure") {
        error.value = current.error || "Tracking repair failed. Review the job before retrying.";
      }
    } catch (exc) {
      error.value = rememberedJobId.value
        ? `Could not check tracking repair job ${rememberedJobId.value}: ${errorMessage(exc)}`
        : errorMessage(exc);
    } finally {
      applying.value = false;
    }
  }

  return { inventory, plan, job, rememberedJobId, loading, planning, applying, error, load, preview, clearPlan, apply };
});
