// Self-update status, strategies, submissions, and follow-up messages.
import { ref } from "vue";
import { defineStore } from "pinia";
import {
  webApi,
  type SelfUpdateApplyResponse,
  type SelfUpdatePlanResponse,
  type SelfUpdatePrepareResponse,
  type SelfUpdateResponse,
} from "../api/client";
import { useAuthStore } from "./auth";
import { errorMessage, runWithStoreState } from "./storeState";

export const useSelfUpdateStore = defineStore("selfUpdate", () => {
  const selfUpdate = ref<SelfUpdateResponse | null>(null);
  const selfUpdatePlan = ref<SelfUpdatePlanResponse | null>(null);
  const selfUpdateMessage = ref("");
  const selfUpdateError = ref("");
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

  async function loadSelfUpdate(): Promise<void> {
    selfUpdateError.value = "";
    try {
      selfUpdate.value = await webApi.selfUpdate();
      selfUpdatePlan.value = null;
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
  }

  async function planSelfUpdate(): Promise<SelfUpdatePlanResponse> {
    const auth = useAuthStore();
    selfUpdateError.value = "";
    let response: SelfUpdatePlanResponse | null = null;
    try {
      await loadWithState(async () => {
        response = await webApi.planSelfUpdate(await auth.ensureCsrf());
        selfUpdatePlan.value = response;
      });
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
    if (response === null) {
      throw new Error("Self-update plan did not return a response");
    }
    return response;
  }

  async function applySelfUpdate(): Promise<
    SelfUpdateApplyResponse | SelfUpdatePrepareResponse
  > {
    const auth = useAuthStore();
    selfUpdateMessage.value = "";
    selfUpdateError.value = "";
    let response: SelfUpdateApplyResponse | SelfUpdatePrepareResponse | null = null;
    try {
      await loadWithState(async () => {
        if (selfUpdate.value === null) {
          throw new Error("Self-update status has not been loaded");
        }
        if (selfUpdate.value.strategy === "prepare_tag_update") {
          const planLocal = selfUpdatePlan.value;
          if (planLocal === null) {
            throw new Error(
              "Self-update tag update preview must be loaded before applying",
            );
          }
          const csrfToken = await auth.ensureCsrf();
          response = await webApi.prepareSelfUpdate(
            csrfToken,
            selfUpdate.value,
            planLocal,
          );
          selfUpdateMessage.value =
            "Tag updated and image pulled. Recreate the WUDup container from outside the WebUI to run the new version. Tagged deployments are recommended for predictable updates.";
        } else {
          const csrfToken = await auth.ensureCsrf();
          response = await webApi.applySelfUpdate(csrfToken, selfUpdate.value);
          selfUpdateMessage.value = response.external_recreate_required
            ? "Image prepared, but the running container still uses the previous image. Recreate the WUDup container to run the new version."
            : "Running container image identity matches the prepared update.";
        }
        try {
          selfUpdate.value = await webApi.selfUpdate();
          selfUpdatePlan.value = null;
        } catch {
          // Keep the success visible even if the follow-up status check fails.
        }
      });
    } catch (caughtError) {
      selfUpdateError.value = errorMessage(caughtError);
      throw caughtError;
    }
    if (response === null) {
      throw new Error("Self-update did not return a response");
    }
    return response;
  }

  return {
    selfUpdate,
    selfUpdatePlan,
    selfUpdateMessage,
    selfUpdateError,
    loading,
    error,
    loadSelfUpdate,
    planSelfUpdate,
    applySelfUpdate,
  };
});
