import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { webApi } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useRunsStore } from "../src/stores/runs";
import { useSelfUpdateStore } from "../src/stores/selfUpdate";
import { jsonRequestBody, jsonResponse, mockFetch } from "./helpers/storeActions";
import {
  selfUpdateApplyResponse,
  selfUpdatePlanResponse,
  selfUpdatePrepareResponse,
  selfUpdateResponse,
} from "./helpers/fixtures";

describe("self-update store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("loads self-update status for the shell banner", async () => {
    const fetchMock = mockFetch(selfUpdateResponse());
    useConnectionStore();
    useSettingsStore();
    const selfUpdate = useSelfUpdateStore();
    useRunsStore();

    await selfUpdate.loadSelfUpdate();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update");
    expect(selfUpdate.selfUpdate?.latest_tag).toBe("v0.25.0");
  });

  it("loads self-update tag prepare plan", async () => {
    const fetchMock = mockFetch(selfUpdatePlanResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-plan");
    useConnectionStore();
    useSettingsStore();
    const selfUpdate = useSelfUpdateStore();
    useRunsStore();

    const response = await selfUpdate.planSelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update/plan");
    expect(selfUpdate.selfUpdatePlan?.plan.plan_id).toBe("self-update-plan-test");
    expect(response.external_recreate_required).toBe(true);
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-plan");
  });

  it("passes csrf from auth store to self-update apply", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(selfUpdateApplyResponse()))
      .mockResolvedValueOnce(jsonResponse(selfUpdateResponse()));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const selfUpdate = useSelfUpdateStore();
    useRunsStore();
    selfUpdate.selfUpdate = selfUpdateResponse();

    const response = await selfUpdate.applySelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(response.container).toBe("wudup");
    expect(selfUpdate.selfUpdateMessage).toBe(
      "Image prepared, but the running container still uses the previous image. Recreate the WUDup container to run the new version.",
    );
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "pull_image",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:latest",
      restart_container: "wudup",
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-self-update");
  });

  it("prepares pinned self-update tags from cached plan", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(selfUpdatePrepareResponse()))
      .mockResolvedValueOnce(jsonResponse(selfUpdateResponse({ strategy: "prepare_tag_update" })));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const selfUpdate = useSelfUpdateStore();
    useRunsStore();
    selfUpdate.selfUpdate = selfUpdateResponse({
      strategy: "prepare_tag_update",
      current_image: "ghcr.io/magrhino/wudup:v0.24.2",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      external_recreate_required: true,
    });
    selfUpdate.selfUpdatePlan = selfUpdatePlanResponse();

    const response = await selfUpdate.applySelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(response.status).toBe("tag_prepared");
    expect(selfUpdate.selfUpdateMessage).toBe(
      "Tag updated and image pulled. Recreate the WUDup container from outside the WebUI to run the new version. Tagged deployments are recommended for predictable updates.",
    );
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update/prepare");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "prepare_tag_update",
      plan_id: "self-update-plan-test",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      restart_container: "wudup",
    });
  });

  it("requires a loaded self-update tag prepare plan before applying", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const selfUpdate = useSelfUpdateStore();
    useRunsStore();
    selfUpdate.selfUpdate = selfUpdateResponse({
      strategy: "prepare_tag_update",
      current_image: "ghcr.io/magrhino/wudup:v0.24.2",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      external_recreate_required: true,
    });

    await expect(selfUpdate.applySelfUpdate()).rejects.toThrow(
      "Self-update tag update preview must be loaded before applying",
    );

    expect(ensureCsrf).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(selfUpdate.selfUpdateError).toBe(
      "Self-update tag update preview must be loaded before applying",
    );
  });

  it.each(["pull_image", "prepare_tag_update"] as const)(
    "keeps successful %s results when the follow-up status refresh fails",
    async (strategy) => {
      vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-self-update");
      const selfUpdate = useSelfUpdateStore();
      const status = selfUpdateResponse({ strategy });
      const plan = selfUpdatePlanResponse();
      selfUpdate.selfUpdate = status;
      selfUpdate.selfUpdatePlan = plan;
      const response = strategy === "pull_image"
        ? selfUpdateApplyResponse()
        : selfUpdatePrepareResponse();
      const submit = strategy === "pull_image"
        ? vi.spyOn(webApi, "applySelfUpdate").mockResolvedValue(response as ReturnType<typeof selfUpdateApplyResponse>)
        : vi.spyOn(webApi, "prepareSelfUpdate").mockResolvedValue(response as ReturnType<typeof selfUpdatePrepareResponse>);
      vi.spyOn(webApi, "selfUpdate").mockRejectedValue(new Error("status unavailable"));

      await expect(selfUpdate.applySelfUpdate()).resolves.toBe(response);

      expect(submit).toHaveBeenCalledTimes(1);
      expect(selfUpdate.selfUpdate).toEqual(status);
      expect(selfUpdate.selfUpdatePlan).toEqual(plan);
      expect(selfUpdate.selfUpdateMessage).toBe(strategy === "pull_image"
        ? "Image prepared, but the running container still uses the previous image. Recreate the WUDup container to run the new version."
        : "Tag updated and image pulled. Recreate the WUDup container from outside the WebUI to run the new version. Tagged deployments are recommended for predictable updates.");
      expect(selfUpdate.selfUpdateError).toBe("");
      expect(selfUpdate.error).toBe("");
      expect(selfUpdate.loading).toBe(false);
    },
  );

  it("preserves a self-update submission error and clears loading", async () => {
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-self-update");
    const selfUpdate = useSelfUpdateStore();
    selfUpdate.selfUpdate = selfUpdateResponse();
    const failure = new Error("mutations are disabled");
    vi.spyOn(webApi, "applySelfUpdate").mockRejectedValue(failure);
    const refresh = vi.spyOn(webApi, "selfUpdate");

    await expect(selfUpdate.applySelfUpdate()).rejects.toBe(failure);

    expect(refresh).not.toHaveBeenCalled();
    expect(selfUpdate.selfUpdateMessage).toBe("");
    expect(selfUpdate.selfUpdateError).toBe("mutations are disabled");
    expect(selfUpdate.error).toBe("mutations are disabled");
    expect(selfUpdate.loading).toBe(false);
  });

});
