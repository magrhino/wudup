// Retag choices, preview polling, and the remembered candidate preference.
// Shared apply jobs/recovery stay in updates; integration uses its public actions.
import { ref } from "vue";
import { defineStore } from "pinia";
import {
  webApi,
  type ApplyJobResponse,
  type RetagChoiceRequest,
  type RetagPlanResponse,
  type RetagPreviewJobResponse,
  type RetagTargetChoice,
  type RetagTargetItem,
  type RetagTargetsResponse,
} from "../api/client";
import { usePolledJob } from "../composables/usePolledJob";
import { useAuthStore } from "./auth";
import { useUpdatesStore } from "./updates";
import { runWithStoreState } from "./storeState";
import {
  canEnableRetagTargetChoice,
  normalizeRetagChoice,
  retagChoice as selectedRetagChoice,
  retagTargetIdentity,
  retagTargetTagValue,
} from "../utils/retagChoices";

const RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY = "retagGithubLatestFallback";
const TERMINAL_RETAG_PREVIEW_STATUSES = new Set<RetagPreviewJobResponse["status"]>([
  "success",
  "failure",
]);

export const useRetagsStore = defineStore("retags", () => {
  const retagTargets = ref<RetagTargetsResponse | null>(null);
  const retagChoices = ref<Record<string, RetagTargetChoice>>({});
  const retagTargetTags = ref<Record<string, string>>({});
  const retagPlan = ref<RetagPlanResponse | null>(null);
  const retagGithubLatestFallback = ref(readRememberedRetagGithubLatestFallback());
  let retagPreviewStart:
    | ((isCurrent: () => boolean) => Promise<RetagPreviewJobResponse | null>)
    | null = null;
  const retagPreviewPoller = usePolledJob<RetagPreviewJobResponse>(
    (isCurrent) => {
      if (retagPreviewStart === null) {
        throw new Error("Retag preview was not started");
      }
      return retagPreviewStart(isCurrent);
    },
    (job) => webApi.retagPreviewJob(job.preview_job_id),
    (job) => TERMINAL_RETAG_PREVIEW_STATUSES.has(job.status),
    { intervalMs: 400 },
  );
  const loading = ref(false);
  const error = ref("");
  let activeLoads = 0;

  async function loadWithState(work: () => Promise<void>): Promise<void> {
    activeLoads += 1;
    try {
      await runWithStoreState(loading, error, work);
    } finally {
      activeLoads -= 1;
      loading.value = activeLoads > 0;
    }
  }

  async function loadRetagTargets(
    options: { githubLatestFallback?: boolean } = {},
  ): Promise<void> {
    const githubLatestFallback =
      options.githubLatestFallback ?? retagGithubLatestFallback.value;
    retagGithubLatestFallback.value = githubLatestFallback;
    await loadWithState(async () => {
      retagTargets.value = await webApi.retagTargets({
        github_latest_fallback: githubLatestFallback,
      });
      resetRetagChoices();
      retagPlan.value = null;
      retagPreviewPoller.reset();
    });
  }

  async function setRetagGithubLatestFallback(enabled: boolean): Promise<void> {
    retagGithubLatestFallback.value = enabled;
    writeRememberedRetagGithubLatestFallback(enabled);
    await loadRetagTargets({ githubLatestFallback: enabled });
  }

  async function refreshRetagGithubLatest(): Promise<void> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      retagTargets.value = await webApi.refreshRetagGithubLatest(
        await auth.ensureCsrf(),
      );
      retagGithubLatestFallback.value = true;
      writeRememberedRetagGithubLatestFallback(true);
      resetRetagChoices();
      retagPlan.value = null;
      retagPreviewPoller.reset();
    });
  }

  function resetRetagChoices(): void {
    const items = retagTargets.value?.items ?? [];
    retagChoices.value = Object.fromEntries(
      items.map((item) => [
        retagTargetIdentity(item),
        "keep-current" satisfies RetagTargetChoice,
      ]),
    );
    retagTargetTags.value = Object.fromEntries(
      items.map((item) => [
        retagTargetIdentity(item),
        item.retag_available ? item.proposed_tag : "",
      ]),
    );
  }

  function findRetagTarget(targetKey: string): RetagTargetItem | undefined {
    const items = retagTargets.value?.items ?? [];
    const targetIdMatch = items.find((target) => target.target_id === targetKey);
    if (targetIdMatch) {
      return targetIdMatch;
    }
    const serviceKeyMatches = items.filter(
      (target) => target.service_key === targetKey,
    );
    return serviceKeyMatches.length === 1 ? serviceKeyMatches[0] : undefined;
  }

  function setRetagChoice(
    targetKey: string,
    choice: RetagTargetChoice,
  ): void {
    const item = findRetagTarget(targetKey);
    const choiceKey = item ? retagTargetIdentity(item) : targetKey;
    retagChoices.value = {
      ...retagChoices.value,
      [choiceKey]: item
        ? normalizeRetagChoice(item, choice, retagTargetTags.value)
        : choice,
    };
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function setRetagChoicesForItems(
    items: RetagTargetItem[],
    choice: RetagTargetChoice,
  ): void {
    const currentItems = new Map(
      (retagTargets.value?.items ?? []).map((item) => [
        retagTargetIdentity(item),
        item,
      ]),
    );
    const nextChoices = { ...retagChoices.value };
    for (const requestedItem of items) {
      const item = currentItems.get(retagTargetIdentity(requestedItem));
      if (!item) {
        continue;
      }
      nextChoices[retagTargetIdentity(item)] =
        choice === "switch-to-concrete" &&
        canEnableRetagTargetChoice(item, retagTargetTags.value)
          ? "switch-to-concrete"
          : "keep-current";
    }
    retagChoices.value = nextChoices;
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function setRetagTargetTag(targetKey: string, tag: string): void {
    const item = findRetagTarget(targetKey);
    const choiceKey = item ? retagTargetIdentity(item) : targetKey;
    retagTargetTags.value = {
      ...retagTargetTags.value,
      [choiceKey]: tag,
    };
    if (item && tag.trim()) {
      retagChoices.value = {
        ...retagChoices.value,
        [choiceKey]: "switch-to-concrete",
      };
    }
    retagPlan.value = null;
    retagPreviewPoller.reset();
  }

  function retagChoiceRequests(): RetagChoiceRequest[] {
    const items = retagTargets.value?.items ?? [];
    return items
      .map((item) => {
        const choice = selectedRetagChoice(
          item,
          retagChoices.value,
          retagTargetTags.value,
        );
        const request: RetagChoiceRequest = {
          service_key: item.service_key,
          choice,
        };
        const targetId = retagTargetIdentity(item);
        if (targetId !== item.service_key) {
          request.target_id = targetId;
        }
        if (choice === "switch-to-concrete") {
          if (item.runtime_state !== "running") {
            request.allow_start = true;
          }
          const tag = retagTargetTagValue(item, retagTargetTags.value).trim();
          if (tag) {
            request.target_tag = tag;
          }
        }
        return request;
      })
      .sort(
        (left, right) =>
          left.service_key.localeCompare(right.service_key) ||
          (left.target_id ?? "").localeCompare(right.target_id ?? ""),
      );
  }

  async function createRetagPlan(): Promise<RetagPlanResponse | null> {
    const auth = useAuthStore();
    const updates = useUpdatesStore();
    let response: RetagPlanResponse | null = null;
    await loadWithState(async () => {
      retagPlan.value = null;
      updates.clearApplyJob();
      const choices = retagChoiceRequests();
      const options = { github_latest_fallback: retagGithubLatestFallback.value };
      retagPreviewStart = async (isCurrent) => {
        const csrfToken = await auth.ensureCsrf();
        if (!isCurrent()) {
          return null;
        }
        return webApi.startRetagPreview(choices, csrfToken, options);
      };
      const job = await retagPreviewPoller.run();
      if (job === null) {
        return;
      }
      if (job.status === "failure") {
        throw new Error(job.error || "Retag preview failed");
      }
      if (job.plan === null) {
        throw new Error("Retag preview did not return a plan");
      }
      response = job.plan;
      retagPlan.value = job.plan;
    });
    return response;
  }

  async function applyRetagPlan(): Promise<ApplyJobResponse> {
    const auth = useAuthStore();
    const updates = useUpdatesStore();
    const planToApply = retagPlan.value;
    if (planToApply === null) {
      throw new Error("Retag preview must be loaded before applying");
    }
    const choicesToApply = retagChoiceRequests();
    const githubLatestFallback = retagGithubLatestFallback.value;
    await loadWithState(async () => {
      updates.clearApplyJobLog();
      const csrfToken = await auth.ensureCsrf();
      if (
        retagPlan.value !== planToApply ||
        retagGithubLatestFallback.value !== githubLatestFallback
      ) {
        throw new Error(
          "Retag preview changed. Preview the current selection again.",
        );
      }
      const job = await webApi.applyRetagPlan(
        planToApply.plan_id,
        choicesToApply,
        csrfToken,
        { github_latest_fallback: githubLatestFallback },
      );
      updates.setApplyJob(job);
    });
    const job = updates.applyJob;
    if (job === null) {
      throw new Error("Apply job was not created");
    }
    return job;
  }

  return {
    retagTargets,
    retagChoices,
    retagTargetTags,
    retagPlan,
    retagPreviewJob: retagPreviewPoller.job,
    retagPreviewPolling: retagPreviewPoller.polling,
    retagPreviewError: retagPreviewPoller.error,
    retagGithubLatestFallback,
    loading,
    error,
    loadRetagTargets,
    setRetagGithubLatestFallback,
    refreshRetagGithubLatest,
    resetRetagChoices,
    setRetagChoice,
    setRetagChoicesForItems,
    setRetagTargetTag,
    retagChoiceRequests,
    createRetagPlan,
    applyRetagPlan,
  };
});

function readRememberedRetagGithubLatestFallback(): boolean {
  const storage = localStorageAvailable();
  try {
    return storage?.getItem(RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function writeRememberedRetagGithubLatestFallback(enabled: boolean): void {
  const storage = localStorageAvailable();
  try {
    storage?.setItem(
      RETAG_GITHUB_LATEST_FALLBACK_STORAGE_KEY,
      enabled ? "true" : "false",
    );
  } catch {
    // Remembering this harmless UI preference is best-effort.
  }
}

function localStorageAvailable(): Storage | null {
  try {
    return "localStorage" in globalThis ? globalThis.localStorage : null;
  } catch {
    return null;
  }
}

