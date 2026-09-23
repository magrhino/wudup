import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, webApi, type TrackingRepairPlan } from "../src/api/client";
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
    globalThis.sessionStorage.clear();
    setActivePinia(createPinia());
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-test");
  });
  afterEach(() => {
    globalThis.sessionStorage.clear();
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
    expect(store.applyError).toBe("");
    expect(store.plan).toBeNull();
    expect(store.applying).toBe(false);
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBeNull();
  });

  it("keeps an inventory refresh error separate from a successful repair", async () => {
    const store = useTrackingStore();
    store.plan = plan;
    vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValue(applyJobResponse({ status: "success" }));
    vi.spyOn(webApi, "trackedContainers").mockRejectedValue(new Error("Inventory unavailable"));

    await store.apply();

    expect(store.job?.status).toBe("success");
    expect(store.error).toContain("Inventory unavailable");
    expect(store.applyError).toBe("");
  });

  it("keeps job failure and polling errors visible without leaving apply active", async () => {
    const store = useTrackingStore();
    store.plan = plan;
    const apply = vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValueOnce(applyJobResponse({ status: "failure", error: "Compose failed" }))
      .mockResolvedValueOnce(applyJobResponse({ status: "running" }));
    const load = vi.spyOn(webApi, "trackedContainers");

    await store.apply();
    expect(store.applyError).toBe("Compose failed");
    expect(store.applying).toBe(false);
    expect(load).not.toHaveBeenCalled();

    vi.useFakeTimers();
    store.plan = plan;
    vi.spyOn(webApi, "applyJob").mockRejectedValue(new Error("Job unavailable"));
    const pending = store.apply();
    await vi.advanceTimersByTimeAsync(750);
    await pending;
    expect(apply).toHaveBeenCalledTimes(2);
    expect(store.applyError).toContain("Job unavailable");
    expect(store.applying).toBe(false);
  });

  it("does not blame an older job when submitting a new repair fails", async () => {
    globalThis.sessionStorage.setItem("trackingRepairJobId", "older-job");
    const store = useTrackingStore();
    store.plan = plan;
    vi.spyOn(webApi, "applyTrackingRepair").mockRejectedValue(new Error("New repair rejected"));

    await store.apply();

    expect(store.applyError).toBe("Could not start tracking repair: New repair rejected");
    expect(store.error).toBe("");
    expect(store.rememberedJobId).toBe("older-job");
  });

  it("keeps an older job check error separate from a rejected new repair", async () => {
    globalThis.sessionStorage.setItem("trackingRepairJobId", "older-job");
    const store = useTrackingStore();
    let rejectOlderJob!: (reason: Error) => void;
    vi.spyOn(webApi, "applyJob").mockReturnValue(new Promise((_resolve, reject) => {
      rejectOlderJob = reject;
    }));
    vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });
    vi.spyOn(webApi, "applyTrackingRepair").mockRejectedValue(new Error("New repair rejected"));

    const pendingLoad = store.load();
    store.plan = plan;
    await store.apply();
    rejectOlderJob(new Error("Old job unavailable"));
    await pendingLoad;

    expect(store.applyError).toBe("Could not start tracking repair: New repair rejected");
    expect(store.error).toContain("Could not check tracking repair job older-job: Old job unavailable");
  });

  it("keeps a late inventory error separate from a failed new repair", async () => {
    const store = useTrackingStore();
    let rejectInventory!: (reason: Error) => void;
    vi.spyOn(webApi, "trackedContainers").mockReturnValue(new Promise((_resolve, reject) => {
      rejectInventory = reject;
    }));
    vi.spyOn(webApi, "applyTrackingRepair").mockRejectedValue(new Error("New repair rejected"));

    const pendingLoad = store.load();
    store.plan = plan;
    await store.apply();
    rejectInventory(new Error("Inventory unavailable"));
    await pendingLoad;

    expect(store.applyError).toBe("Could not start tracking repair: New repair rejected");
    expect(store.error).toContain("Inventory unavailable");
  });

  it.each(["success", "failure"] as const)("replaces a polling error with a recovered %s job", async (status) => {
    vi.useFakeTimers();
    const store = useTrackingStore();
    store.plan = plan;
    vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValue(applyJobResponse({ status: "running" }));
    vi.spyOn(webApi, "applyJob")
      .mockRejectedValueOnce(new Error("Poll unavailable"))
      .mockResolvedValueOnce(applyJobResponse({ status, error: status === "failure" ? "Compose failed" : "" }));
    vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });

    const pendingApply = store.apply();
    await vi.advanceTimersByTimeAsync(750);
    await pendingApply;
    expect(store.applyError).toContain("Poll unavailable");

    await store.load();
    expect(store.job?.status).toBe(status);
    expect(store.applyError).toBe("");
    expect(store.error).toBe("");
    if (status === "failure") expect(store.job?.error).toBe("Compose failed");
  });

  it("ignores an older failed check when another load recovers the same job", async () => {
    globalThis.sessionStorage.setItem("trackingRepairJobId", "job-test");
    const store = useTrackingStore();
    let rejectOlderCheck!: (reason: Error) => void;
    let resolveNewerCheck!: (job: ReturnType<typeof applyJobResponse>) => void;
    vi.spyOn(webApi, "applyJob")
      .mockReturnValueOnce(new Promise((_resolve, reject) => { rejectOlderCheck = reject; }))
      .mockReturnValueOnce(new Promise((resolve) => { resolveNewerCheck = resolve; }));
    vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });

    const olderLoad = store.load();
    const newerLoad = store.load();
    rejectOlderCheck(new Error("Check unavailable"));
    await olderLoad;
    resolveNewerCheck(applyJobResponse({ status: "success" }));
    await newerLoad;

    expect(store.job?.status).toBe("success");
    expect(store.error).toBe("");
    expect(store.rememberedJobId).toBe("");
  });

  it("ignores a delayed response for an older job after a new job starts", async () => {
    vi.useFakeTimers();
    globalThis.sessionStorage.setItem("trackingRepairJobId", "older-job");
    const store = useTrackingStore();
    let resolveOlderJob!: (job: ReturnType<typeof applyJobResponse>) => void;
    const olderJob = new Promise<ReturnType<typeof applyJobResponse>>((resolve) => {
      resolveOlderJob = resolve;
    });
    vi.spyOn(webApi, "applyJob").mockImplementation((jobId) => jobId === "older-job"
      ? olderJob
      : Promise.resolve(applyJobResponse({ job_id: "new-job", status: "success" })));
    vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValue(applyJobResponse({ job_id: "new-job", status: "running" }));
    vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });

    const pendingLoad = store.load();
    store.plan = plan;
    const pendingApply = store.apply();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.rememberedJobId).toBe("new-job");

    resolveOlderJob(applyJobResponse({ job_id: "older-job", status: "success" }));
    await pendingLoad;
    expect(store.job?.job_id).toBe("new-job");
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBe("new-job");

    await vi.advanceTimersByTimeAsync(750);
    await pendingApply;
    expect(store.job?.status).toBe("success");
  });

  it("stops polling a long-running job and keeps its ID visible", async () => {
    vi.useFakeTimers();
    const store = useTrackingStore();
    store.plan = plan;
    vi.spyOn(webApi, "applyTrackingRepair")
      .mockResolvedValue(applyJobResponse({ status: "running" }));
    const poll = vi.spyOn(webApi, "applyJob")
      .mockResolvedValue(applyJobResponse({ status: "running" }));

    const pending = store.apply();
    await vi.advanceTimersByTimeAsync(300_000);
    await pending;

    expect(poll).toHaveBeenCalledTimes(400);
    expect(store.job?.job_id).toBe("job-test");
    expect(store.error).toBe("");
    expect(store.applying).toBe(false);
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBe("job-test");
  });

  it("recovers a still-running repair job after a fresh store loads", async () => {
    globalThis.sessionStorage.setItem("trackingRepairJobId", "job-test");
    const store = useTrackingStore();
    const inventory = vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });
    const check = vi.spyOn(webApi, "applyJob")
      .mockResolvedValueOnce(applyJobResponse({ status: "running" }))
      .mockResolvedValueOnce(applyJobResponse({ status: "success" }));

    await store.load();
    expect(check).toHaveBeenCalledWith("job-test");
    expect(store.job?.job_id).toBe("job-test");
    expect(store.error).toBe("");
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBe("job-test");

    await store.load();
    expect(store.job?.status).toBe("success");
    expect(store.error).toBe("");
    expect(check.mock.invocationCallOrder[1]).toBeLessThan(inventory.mock.invocationCallOrder[1]);
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBeNull();
  });

  it("retires a missing repair job instead of retrying its 404 on every load", async () => {
    globalThis.sessionStorage.setItem("trackingRepairJobId", "job-lost");
    const store = useTrackingStore();
    vi.spyOn(webApi, "trackedContainers").mockResolvedValue({
      status: "ready", count: 0, items: [], wud_status: null, warnings: [],
    });
    const check = vi.spyOn(webApi, "applyJob")
      .mockRejectedValue(new ApiError(404, "apply job not found"));

    await store.load();
    expect(store.error).toContain("job-lost");
    expect(store.rememberedJobId).toBe("");
    expect(globalThis.sessionStorage.getItem("trackingRepairJobId")).toBeNull();
    await store.load();
    expect(check).toHaveBeenCalledOnce();
  });
});
