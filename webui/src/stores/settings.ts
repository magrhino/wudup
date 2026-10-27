// webui/src/stores/settings.ts
import { ref } from "vue";
import { defineStore } from "pinia";
import {
  type AutoUpdateDay,
  type CoreUpdateTourResponse,
  type CoreUpdateTourStatus,
  type CoreUpdateTourStep,
  type ManagedSettingsUpdateResponse,
  type OnboardingChecklistResponse,
  type OnboardingDismissResponse,
  type ReleaseNotificationTestResponse,
  type ReleaseSkipScope,
  type ServicePolicyRecord,
  type ServicePolicyUpdateMode,
  type SettingsResponse,
  type SnoozeKind,
  type SnoozeRecord,
  type SnoozeState,
  type StateOperation,
  type TagExclusionRuleRecord,
  type TagExclusionScope,
  type TagExclusionStatus,
  type TagExclusionStatusFilter,
  webApi,
} from "../api/client";
import { useAuthStore } from "./auth";
import { errorMessage, runWithStoreState } from "./storeState";

function deleteSnoozeOperation(snoozeId: number, kind: SnoozeKind): StateOperation {
  switch (kind) {
    case "dependency":
      return { kind: "delete_dependency_snooze", snooze_id: snoozeId };
    case "release":
      return { kind: "delete_release_skip", snooze_id: snoozeId };
    default:
      return { kind: "delete_snooze", snooze_id: snoozeId };
  }
}

export const useSettingsStore = defineStore("settings", () => {
  const settings = ref<SettingsResponse | null>(null);
  const onboarding = ref<OnboardingChecklistResponse | null>(null);
  const coreUpdateTour = ref<CoreUpdateTourResponse | null>(null);
  const servicePolicies = ref<ServicePolicyRecord[]>([]);
  const snoozes = ref<SnoozeRecord[]>([]);
  const tagExclusions = ref<TagExclusionRuleRecord[]>([]);
  const snoozeStateFilter = ref<SnoozeState>("active");
  const tagExclusionStatusFilter = ref<TagExclusionStatusFilter>("active");
  const loading = ref(false);
  const error = ref("");
  const pendingSafetyCueError = ref("");
  let coreUpdateTourLoadPromise: Promise<void> | null = null;

  async function loadWithState(work: () => Promise<void>): Promise<void> {
    await runWithStoreState(loading, error, work);
  }

  async function loadSettings(): Promise<void> {
    await loadWithState(async () => {
      settings.value = await webApi.settings();
    });
  }

  async function mutateAndReload(
    operation: StateOperation,
    reload: () => Promise<void>,
  ): Promise<void> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      await webApi.stateOperation(operation, await auth.ensureCsrf());
      await reload();
    });
  }

  async function updateManagedSettings(
    values: Record<string, string>,
  ): Promise<ManagedSettingsUpdateResponse> {
    const auth = useAuthStore();
    let response: ManagedSettingsUpdateResponse | null = null;
    const reloadOnboarding = Object.prototype.hasOwnProperty.call(
      values,
      "onboarding_checklist",
    );
    await loadWithState(async () => {
      response = await webApi.updateManagedSettings(values, await auth.ensureCsrf());
      if (settings.value) {
        settings.value = {
          ...settings.value,
          managed: response.managed,
        };
      }
    });
    if (response === null) {
      throw new Error("Managed settings update did not return a response");
    }
    if (reloadOnboarding) {
      try {
        onboarding.value = await webApi.onboardingChecklist(await auth.ensureCsrf());
      } catch {
        // The preference save succeeded; the checklist can refresh on the next view load.
      }
    }
    return response;
  }

  async function testReleaseNotificationWebhook(): Promise<ReleaseNotificationTestResponse> {
    const auth = useAuthStore();
    let response: ReleaseNotificationTestResponse | null = null;
    await loadWithState(async () => {
      response = await webApi.testReleaseNotificationWebhook(await auth.ensureCsrf());
    });
    if (response === null) {
      throw new Error("Test webhook request did not return a response");
    }
    return response;
  }

  async function loadOnboarding(): Promise<void> {
    const auth = useAuthStore();
    await loadWithState(async () => {
      onboarding.value = await webApi.onboardingChecklist(await auth.ensureCsrf());
    });
  }

  async function dismissOnboarding(): Promise<OnboardingDismissResponse> {
    const auth = useAuthStore();
    let response: OnboardingDismissResponse | null = null;
    await loadWithState(async () => {
      response = await webApi.dismissOnboarding(await auth.ensureCsrf());
      if (onboarding.value) {
        onboarding.value = {
          ...onboarding.value,
          dismissed: response.dismissed,
          dismissed_at: response.dismissed_at,
          visible: false,
        };
      }
    });
    if (response === null) {
      throw new Error("Onboarding dismiss did not return a response");
    }
    return response;
  }

  async function loadCoreUpdateTour(): Promise<void> {
    await loadWithState(async () => {
      coreUpdateTour.value = await webApi.coreUpdateTour();
    });
  }

  async function ensureCoreUpdateTour(): Promise<void> {
    if (coreUpdateTour.value !== null) {
      return;
    }
    coreUpdateTourLoadPromise ??= loadCoreUpdateTour().finally(() => {
      coreUpdateTourLoadPromise = null;
    });
    await coreUpdateTourLoadPromise;
  }

  async function updateCoreUpdateTour(
    status: CoreUpdateTourStatus,
    step: CoreUpdateTourStep,
  ): Promise<CoreUpdateTourResponse> {
    const auth = useAuthStore();
    let response: CoreUpdateTourResponse | null = null;
    await loadWithState(async () => {
      response = await webApi.updateCoreUpdateTour(
        status,
        step,
        await auth.ensureCsrf(),
      );
      coreUpdateTour.value = response;
    });
    if (response === null) {
      throw new Error("Core update tour update did not return a response");
    }
    return response;
  }

  async function loadPendingSafetyCues(): Promise<void> {
    const [nextServicePolicies, nextSnoozes] = await Promise.allSettled([
      webApi.servicePolicies(),
      webApi.snoozes("active"),
    ]);
    const errors: string[] = [];
    if (nextServicePolicies.status === "fulfilled") {
      servicePolicies.value = nextServicePolicies.value;
    } else {
      errors.push(
        `webApi.servicePolicies() failed: ${errorMessage(nextServicePolicies.reason)}`,
      );
    }
    if (nextSnoozes.status === "fulfilled") {
      snoozes.value = nextSnoozes.value;
    } else {
      errors.push(`webApi.snoozes("active") failed: ${errorMessage(nextSnoozes.reason)}`);
    }
    pendingSafetyCueError.value = errors.join(" ");
  }

  async function loadServicePolicies(): Promise<void> {
    await loadWithState(async () => {
      servicePolicies.value = await webApi.servicePolicies();
    });
  }

  async function loadSnoozes(state: SnoozeState = "active"): Promise<void> {
    await loadWithState(async () => {
      snoozeStateFilter.value = state;
      snoozes.value = await webApi.snoozes(state);
    });
  }

  async function loadTagExclusions(
    status: TagExclusionStatusFilter = "active",
  ): Promise<void> {
    await loadWithState(async () => {
      tagExclusionStatusFilter.value = status;
      tagExclusions.value = await webApi.tagExclusions(status);
    });
  }

  async function upsertServicePolicy(
    serviceKey: string,
    updateMode: ServicePolicyUpdateMode,
    autoUpdate: boolean,
    snoozeDefaultSeconds: number | null,
    autoUpdateTime: string | null,
    autoUpdateDays: AutoUpdateDay[],
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "upsert_service_policy",
        service_key: serviceKey,
        update_mode: updateMode,
        auto_update: autoUpdate,
        snooze_default_seconds: snoozeDefaultSeconds,
        auto_update_time: autoUpdateTime,
        auto_update_days: autoUpdateDays,
      },
      async () => {
        servicePolicies.value = await webApi.servicePolicies();
      },
    );
  }

  async function deleteServicePolicy(serviceKey: string): Promise<void> {
    await mutateAndReload(
      {
        kind: "delete_service_policy",
        service_key: serviceKey,
      },
      async () => {
        servicePolicies.value = await webApi.servicePolicies();
      },
    );
  }

  async function createSnooze(
    serviceKey: string,
    snoozedUntil: string,
    reason: string,
    state: SnoozeState,
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "create_snooze",
        service_key: serviceKey,
        snoozed_until: snoozedUntil,
        reason,
      },
      async () => {
        snoozeStateFilter.value = state;
        snoozes.value = await webApi.snoozes(state);
      },
    );
  }

  async function deleteSnooze(
    snoozeId: number,
    state: SnoozeState,
    kind: SnoozeKind = "time",
  ): Promise<void> {
    await mutateAndReload(
      deleteSnoozeOperation(snoozeId, kind),
      async () => {
        snoozeStateFilter.value = state;
        snoozes.value = await webApi.snoozes(state);
      },
    );
  }

  async function createDependencySnooze(
    serviceKey: string,
    waitForServiceKey: string,
    reason: string,
    state: SnoozeState,
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "create_dependency_snooze",
        service_key: serviceKey,
        wait_for_service_key: waitForServiceKey,
        reason,
      },
      async () => {
        snoozeStateFilter.value = state;
        snoozes.value = await webApi.snoozes(state);
      },
    );
  }

  async function createReleaseSkip(
    target: {
      serviceKeys: string[];
      image: string;
      targetTag: string;
      digest: string;
    },
    scope: ReleaseSkipScope,
    reason: string,
    state: SnoozeState,
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "create_release_skip",
        service_keys: target.serviceKeys,
        image: target.image,
        target_tag: target.targetTag,
        digest: target.digest,
        scope,
        reason,
      },
      async () => {
        snoozeStateFilter.value = state;
        snoozes.value = await webApi.snoozes(state);
      },
    );
  }

  async function upsertTagExclusion(
    scope: TagExclusionScope,
    imageRepo: string,
    serviceKey: string,
    tag: string,
    status: TagExclusionStatus,
    statusFilter: TagExclusionStatusFilter,
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "upsert_tag_exclusion",
        scope,
        image_repo: imageRepo,
        service_key: serviceKey,
        match_type: "exact",
        tag,
        status,
      },
      async () => {
        tagExclusionStatusFilter.value = statusFilter;
        tagExclusions.value = await webApi.tagExclusions(statusFilter);
      },
    );
  }

  async function setTagExclusionStatus(
    ruleId: number,
    status: TagExclusionStatus,
    statusFilter: TagExclusionStatusFilter,
  ): Promise<void> {
    await mutateAndReload(
      {
        kind: "set_tag_exclusion_status",
        rule_id: ruleId,
        status,
      },
      async () => {
        tagExclusionStatusFilter.value = statusFilter;
        tagExclusions.value = await webApi.tagExclusions(statusFilter);
      },
    );
  }

  return {
    settings,
    onboarding,
    coreUpdateTour,
    servicePolicies,
    snoozes,
    tagExclusions,
    snoozeStateFilter,
    tagExclusionStatusFilter,
    loading,
    error,
    pendingSafetyCueError,
    loadSettings,
    updateManagedSettings,
    testReleaseNotificationWebhook,
    loadOnboarding,
    dismissOnboarding,
    loadCoreUpdateTour,
    ensureCoreUpdateTour,
    updateCoreUpdateTour,
    loadPendingSafetyCues,
    loadServicePolicies,
    loadSnoozes,
    loadTagExclusions,
    upsertServicePolicy,
    deleteServicePolicy,
    createSnooze,
    createDependencySnooze,
    createReleaseSkip,
    deleteSnooze,
    upsertTagExclusion,
    setTagExclusionStatus,
  };
});
