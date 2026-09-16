import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, webApi } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useRunsStore } from "../src/stores/runs";
import { useUpdatesStore } from "../src/stores/updates";
import PendingApplyRecovery from "../src/components/pending/PendingApplyRecovery.vue";
import { mountWithApp } from "./helpers/mount";
import { applyJobResponse, pendingResponse, pendingRescanResponse, pendingSourceInfo,
  planResponse, runSummary, runVerification, statusResponse, wudApiStatus } from "./helpers/fixtures";
import { mockPendingLifecycle, mountPendingView, setupStores } from "./helpers/viewSecurity";

const before = "2026-09-16T10:00:00Z";
const after = "2026-09-16T10:01:00Z";
const missing = () => new ApiError(404, "Job not found");

beforeEach(() => setActivePinia(createPinia()));

describe("scan feedback and current health", () => {
  it.each(["load", "metadata"])("settles feedback only on a newer successful %s read", async (method) => {
    const updates = useUpdatesStore();
    updates.pending = pendingResponse();
    const notice = pendingRescanResponse({ wud_api: wudApiStatus({ last_checked_at: before }) });
    updates.pendingRescan = notice;
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf");
    const response = {
      ...pendingResponse(), status: "ready" as const, requires_pending_reload: false,
      wud_api: wudApiStatus({ last_checked_at: before }),
    };
    const fetch = method === "load"
      ? vi.spyOn(webApi, "pending").mockResolvedValue(response)
      : vi.spyOn(webApi, "pendingMetadata").mockResolvedValue(response);
    const refresh = () => method === "load" ? updates.loadPending() : updates.refreshPendingMetadata();
    await refresh();
    expect(updates.pendingRescan).toEqual(notice);
    fetch.mockRejectedValueOnce(new Error("offline"));
    await expect(refresh()).rejects.toThrow("offline");
    expect(updates.pendingRescan).toEqual(notice);
    response.wud_api.last_checked_at = after;
    await refresh();
    expect(updates.pendingRescan).toBeNull();
  });

  it("does not restore scan feedback after the immediate reload finds fresh results", async () => {
    const updates = useUpdatesStore();
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf");
    vi.spyOn(webApi, "rescanPending").mockResolvedValue(pendingRescanResponse({
      status: "partial", wud_api: wudApiStatus({ last_checked_at: before }),
    }));
    vi.spyOn(webApi, "pending").mockResolvedValue({
      ...pendingResponse(), wud_api: wudApiStatus({ last_checked_at: after }),
    });
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "loadSecurityScans").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(useRunsStore(), "loadRuns").mockResolvedValue();
    await updates.rescanPending("all");
    expect(updates.pendingRescan).toBeNull();
  });

  it.each([true, false])("polling settles the request while current degradation is %s", async (degraded) => {
    vi.useFakeTimers();
    const { pinia, settings, updates, connection, auth } = setupStores(true);
    updates.pending = pendingResponse();
    updates.pendingWudMetadataCheckedAt = before;
    updates.pendingRescan = pendingRescanResponse({ wud_api: wudApiStatus({ last_checked_at: before }) });
    mockPendingLifecycle(settings, updates);
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf");
    vi.spyOn(connection, "loadStatus").mockImplementation(async () => {
      connection.status = statusResponse({ source_hash: updates.pending!.source_hash,
        wud_api: wudApiStatus({ last_checked_at: after }) });
    });
    vi.spyOn(webApi, "pendingMetadata").mockResolvedValue({
      status: "ready", requires_pending_reload: false, source_hash: updates.pending.source_hash!,
      source: pendingSourceInfo({ degraded, detail: degraded ? "One unresolved container." : "" }),
      wud_api: wudApiStatus({ last_checked_at: after }), items: [],
    });
    const wrapper = mountPendingView(pinia);
    try {
      await flushPromises();
      expect(wrapper.text()).toContain("Full WUD scan requested");
      await vi.advanceTimersByTimeAsync(30_000);
      await flushPromises();
      expect(wrapper.text()).not.toContain("Full WUD scan requested");
      expect(wrapper.text().includes("Current WUD health")).toBe(degraded);
    } finally {
      wrapper.unmount();
      vi.useRealTimers();
    }
  });

  it.each([
    { scope: "selected" as const, requested_count: 2, watched_count: 1 },
    { scope: "all" as const, requested_count: 1, watched_count: 1 },
  ])("preserves partial $scope request warnings with no skipped entries", async (counts) => {
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    updates.pendingRescan = pendingRescanResponse({
      ...counts, status: "partial", skipped: [],
      wud_api: wudApiStatus({ detail: "Historical WUD watch error." }),
    });
    const wrapper = mountPendingView(pinia);
    await flushPromises();
    const notice = wrapper.findAll('[data-alert-type="warning"]')
      .find((alert) => alert.text().includes("WUD reported a partial scan result."));
    expect(notice).toBeDefined();
    expect(notice!.text()).toContain("Review request details.");
    expect(notice!.text()).not.toContain("Waiting for fresh WUD results");
    expect(wrapper.text()).not.toContain("Historical WUD watch error");
    if (counts.scope === "selected") {
      expect(notice!.text()).toContain("2 selected entries; 1 container scan request sent.");
    }
    wrapper.unmount();
  });

  it("keeps unresolved health and last-check context available when collapsed", async () => {
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = { ...pendingResponse(), source: pendingSourceInfo({
      degraded: true, detail: "One container still needs review.",
    }) };
    updates.pendingWudMetadataCheckedAt = before;
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    await flushPromises();
    const disclosure = wrapper.findAll<HTMLDetailsElement>("details")
      .find((details) => details.find("summary").text().includes("Current WUD health"))!;
    expect(disclosure.element.open).toBe(true);
    disclosure.element.open = false;
    updates.pendingWudMetadataCheckedAt = after;
    await flushPromises();
    expect(disclosure.element.open).toBe(false);
    expect(disclosure.find("summary").text()).toContain("needs attention");
    expect(disclosure.find("summary").text()).toContain(new Date(after).toLocaleString());
    expect(updates.pending.source.degraded).toBe(true);
    disclosure.element.open = true;
    expect(disclosure.text()).toContain("One container still needs review.");
    expect(disclosure.text()).toContain("View affected containers");
    updates.pending.source.degraded = false;
    await flushPromises();
    expect(wrapper.text()).not.toContain("Current WUD health");
    wrapper.unmount();
  });

  it("shows one current-health summary and uses readiness evidence", async () => {
    const { pinia, settings, updates } = setupStores(true);
    const warning = "Update status is unknown for 1 container.";
    updates.pending = { ...pendingResponse(), source: pendingSourceInfo({ degraded: true, detail: warning }) };
    updates.pendingRescan = pendingRescanResponse({ status: "partial", wud_api: wudApiStatus({ detail: warning }) });
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    await flushPromises();
    expect(wrapper.text().split(warning)).toHaveLength(2);
    expect(wrapper.text()).toContain("Last checked:");
    expect(wrapper.text()).toContain("Update readiness is not yet checked");
    updates.plan = planResponse();
    await flushPromises();
    expect(wrapper.text()).toContain("Advisory for this plan");
    updates.plan.apply_preflight!.ok = false;
    await flushPromises();
    expect(wrapper.text()).toContain("Selected plan blocked");
    wrapper.unmount();
  });
});

describe("missing apply job recovery", () => {
  it("retains the correlated run across reload, acknowledgement, and another update", async () => {
    let updates = useUpdatesStore();
    updates.setApplyJob(applyJobResponse({ job_id: "lost", run_id: 10, status: "running" }));
    setActivePinia(createPinia());
    updates = useUpdatesStore();
    vi.spyOn(webApi, "job").mockRejectedValue(missing());
    await updates.loadApplyJob("lost", { recoverMissing: true });
    const runs = useRunsStore();
    runs.runs = [runSummary({ id: 99 })];
    expect(updates.applyJobRecoveries).toEqual([{ jobId: "lost", runId: 10, acknowledged: false }]);
    updates.acknowledgeApplyJobRecovery("lost");
    updates.dismissResolvedApplyJobRecovery("lost");
    updates.setApplyJob(applyJobResponse({ job_id: "new", status: "success" }));
    setActivePinia(createPinia());
    updates = useUpdatesStore();
    expect(updates.applyJobRecoveries).toEqual([{ jobId: "lost", runId: 10, acknowledged: true }]);
  });

  it("does not correlate a legacy job ID with another job's persisted run", async () => {
    sessionStorage.setItem("applyJobId", "legacy-job");
    sessionStorage.setItem("applyJobRun", JSON.stringify({ jobId: "other-job", runId: 99 }));
    const updates = useUpdatesStore();
    vi.spyOn(webApi, "job").mockRejectedValue(missing());
    await updates.loadApplyJob("legacy-job", { recoverMissing: true });
    expect(updates.applyJobRecoveries[0].runId).toBeNull();
  });

  it.each([
    ["success", "verified", true],
    ["success", "needs_review", false],
    ["failure", "needs_review", false],
    ["running", "verified", false],
  ] as const)("reconciles %s/%s from the related run only", async (status, verification, resolved) => {
    const updates = useUpdatesStore();
    updates.setApplyJob(applyJobResponse({ job_id: "lost", run_id: 10 }));
    vi.spyOn(webApi, "job").mockRejectedValue(missing());
    await updates.loadApplyJob("lost", { recoverMissing: true });
    const runDetail = vi.spyOn(webApi, "runDetail").mockResolvedValue({
      ...runSummary({ id: 10, status, dry_run: false, finished_at: status === "running" ? null : after }),
      pending_updates: [], verification: runVerification({ status: verification }),
    });
    useRunsStore().runs = [runSummary({ id: 99, status: "success" })];
    await updates.reviewApplyJobRecovery("lost");
    expect(runDetail).toHaveBeenCalledWith(10);
    expect(updates.applyJobRecoveryResolved(10)).toBe(resolved);
    updates.dismissResolvedApplyJobRecovery("lost");
    expect(updates.applyJobRecoveries).toHaveLength(resolved ? 0 : 1);
  });

  it("keeps unknown outcomes visible when acknowledged without linking to an unrelated run", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const updates = useUpdatesStore();
    updates.setApplyJob(applyJobResponse({ job_id: "lost" }));
    vi.spyOn(webApi, "job").mockRejectedValue(missing());
    await updates.loadApplyJob("lost", { recoverMissing: true });
    useRunsStore().runs = [runSummary({ id: 99 })];
    const wrapper = mountWithApp(PendingApplyRecovery, { pinia });
    await wrapper.setProps({ jobId: "lost", runId: null, acknowledged: true });
    await flushPromises();
    expect(wrapper.text()).toContain("Update outcome needs review");
    expect(wrapper.text()).toContain("Notice acknowledged");
    expect(wrapper.text()).toContain("Review History");
    expect(wrapper.text()).not.toContain("#99");
    expect(wrapper.text()).not.toContain("Dismiss verified notice");
    await wrapper.findAll("button").find((button) => button.text() === "Check outcome")!.trigger("click");
    await flushPromises();
    expect(updates.applyJobRecoveries).toHaveLength(1);
    expect(wrapper.text()).toContain("Update outcome needs review");
    wrapper.unmount();
  });
});
