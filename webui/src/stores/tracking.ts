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
  const applyError = ref("");
  const applyErrorJobId = ref("");
  const rememberedJobId = ref(storedTrackingJobId());
  let loadVersion = 0;

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

  async function refreshRememberedJob(checkedJobId: string, version: number): Promise<void> {
    if (!checkedJobId || applying.value) return;
    try {
      const checkedJob = await webApi.applyJob(checkedJobId);
      if (version !== loadVersion || rememberedJobId.value !== checkedJobId || applying.value) return;
      job.value = checkedJob;
      rememberJob(checkedJob);
      if (applyErrorJobId.value === checkedJobId) {
        applyError.value = "";
        applyErrorJobId.value = "";
      }
    } catch (exc) {
      if (version !== loadVersion || rememberedJobId.value !== checkedJobId || applying.value) return;
      if (exc instanceof ApiError && exc.status === 404) {
        forgetJob();
        job.value = null;
        error.value = `Tracking repair job ${checkedJobId} is no longer available. Review run history before retrying.`;
      } else {
        error.value = `Could not check tracking repair job ${checkedJobId}: ${errorMessage(exc)}`;
      }
    }
  }

  async function load(): Promise<void> {
    const version = ++loadVersion;
    loading.value = true;
    error.value = "";
    await refreshRememberedJob(rememberedJobId.value, version);
    try {
      const currentInventory = await webApi.trackedContainers();
      if (version === loadVersion) inventory.value = currentInventory;
    } catch (exc) {
      if (version === loadVersion) {
        error.value = [error.value, errorMessage(exc)].filter(Boolean).join(" ");
      }
    } finally {
      if (version === loadVersion) loading.value = false;
    }
  }

  async function preview(targetId: string, regex: string): Promise<void> {
    planning.value = true;
    error.value = "";
    applyError.value = "";
    applyErrorJobId.value = "";
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
    applyError.value = "";
    applyErrorJobId.value = "";
    const selected = plan.value;
    plan.value = null;
    let current: ApplyJobResponse | null = null;
    try {
      current = await webApi.applyTrackingRepair(
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
        applyErrorJobId.value = current.job_id;
        applyError.value = current.error || "Tracking repair failed. Review the job before retrying.";
      }
    } catch (exc) {
      applyErrorJobId.value = current?.job_id ?? "";
      applyError.value = current
        ? `Could not check tracking repair job ${current.job_id}: ${errorMessage(exc)}`
        : `Could not start tracking repair: ${errorMessage(exc)}`;
    } finally {
      applying.value = false;
    }
  }

  return { inventory, plan, job, rememberedJobId, loading, planning, applying, error, applyError, load, preview, clearPlan, apply };
});
