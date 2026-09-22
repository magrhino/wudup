import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { webApi, type TrackingRepairPlan } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useTrackingStore } from "../src/stores/tracking";
import { applyJobResponse } from "./helpers/fixtures";

const plan: TrackingRepairPlan = {
  plan_id: "plan-radarr", source_hash: "before", rendered_hash: "after",
  target_id: "radarr", service_key: "media/radarr", stack: "media", service: "radarr",
  image: "repo/radarr:v1.36.2", current_regex: "^v1$", proposed_regex: "^v\\d+$",
  compose_diff: "+wud.tag.include=^v\\d+$", will_recreate: true, can_apply: true, issues: [],
};

describe("tracking store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-test");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("previews with CSRF and clears a stale plan after an error", async () => {
    const store = useTrackingStore();
    const preview = vi.spyOn(webApi, "createTrackingRepairPlan").mockResolvedValueOnce(plan)
      .mockRejectedValueOnce(new Error("Preview unavailable"));

    await store.preview("radarr", plan.proposed_regex);
    expect(preview).toHaveBeenCalledWith("radarr", plan.proposed_regex, "csrf-test");
    expect(store.plan).toEqual(plan);
    await store.preview("radarr", plan.proposed_regex);
    expect(store.plan).toBeNull();
    expect(store.error).toContain("Preview unavailable");
    expect(store.planning).toBe(false);
  });

  it("applies a selected plan, polls the job, and reloads inventory on success", async () => {
    vi.useFakeTimers();
    const store = useTrackingStore();
    store.plan = plan;
    const apply = vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValue(applyJobResponse({ status: "running" }));
    const poll = vi.spyOn(webApi, "applyJob")
      .mockResolvedValue(applyJobResponse({ status: "success" }));
    const inventory = { status: "ready" as const, count: 0, items: [], wud_status: null, warnings: [] };
    const load = vi.spyOn(webApi, "trackedContainers").mockResolvedValue(inventory);

    const pending = store.apply();
    await vi.advanceTimersByTimeAsync(750);
    await pending;

    expect(apply).toHaveBeenCalledWith(plan, "csrf-test");
    expect(poll).toHaveBeenCalledWith("job-test");
    expect(load).toHaveBeenCalledOnce();
    expect(store.inventory).toEqual(inventory);
    expect(store.job?.status).toBe("success");
    expect(store.plan).toBeNull();
    expect(store.applying).toBe(false);
  });

  it("keeps job failure and polling errors visible without leaving apply active", async () => {
    const store = useTrackingStore();
    store.plan = plan;
    const apply = vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValueOnce(applyJobResponse({ status: "failure", error: "Compose failed" }))
      .mockResolvedValueOnce(applyJobResponse({ status: "running" }));
    const load = vi.spyOn(webApi, "trackedContainers");

    await store.apply();
    expect(store.error).toBe("Compose failed");
    expect(store.applying).toBe(false);
    expect(load).not.toHaveBeenCalled();

    vi.useFakeTimers();
    store.plan = plan;
    vi.spyOn(webApi, "applyJob").mockRejectedValue(new Error("Job unavailable"));
    const pending = store.apply();
    await vi.advanceTimersByTimeAsync(750);
    await pending;
    expect(apply).toHaveBeenCalledTimes(2);
    expect(store.error).toContain("Job unavailable");
    expect(store.applying).toBe(false);
  });
});
