import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createMemoryHistory } from "vue-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, webApi } from "../src/api/client";
import { createWudRouter } from "../src/router";
import DashboardView from "../src/views/DashboardView.vue";
import DoctorView from "../src/views/DoctorView.vue";
import PendingView from "../src/views/PendingView.vue";
import PoliciesView from "../src/views/PoliciesView.vue";
import SettingsView from "../src/views/SettingsView.vue";
import SnoozesView from "../src/views/SnoozesView.vue";
import TagExclusionsView from "../src/views/TagExclusionsView.vue";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useUpdatesStore, APPLY_JOB_RECOVERY_MESSAGE } from "../src/stores/updates";
import { useRunsStore } from "../src/stores/runs";
import {
  applyPreflightResponse,
  applyJobLogResponse,
  applyJobResponse,
  authSession,
  coreUpdateTourResponse,
  doctorResponse,
  onboardingChecklistResponse,
  pendingGroupedItem,
  pendingGrouping,
  pendingItem,
  pendingResponse,
  pendingSourceInfo,
  planResponse,
  releaseNoteInfo,
  releaseNotificationResponse,
  releaseNotesResponse,
  runVerification,
  runSummary,
  servicePolicy,
  settingsResponse,
  snooze,
  statusResponse,
  tagExclusion,
  updateTarget,
  updateTargetsResponse,
} from "./helpers/fixtures";
import { mountWithApp, naiveStubs } from "./helpers/mount";


import {
  buttonByText,
  emitSelectValue,
  failedApplyPreflight,
  mockApplyJobStream,
  mockMobileViewport,
  mockPendingLifecycle,
  mountPendingView,
  pendingWithUnmatched,
  setupStores,
  stalePendingPreflightFindings,
  stalePendingPossibleReasons,
  stalePendingRecommendedActions,
  unmatchedPendingItem,
} from "./helpers/viewSecurity";

function apiBackedPendingResponse() {
  return {
    ...pendingResponse([
      pendingItem({ source: "api", source_id: "docker.local.app" }),
    ]),
    source_file: "WUD API",
    source: pendingSourceInfo({
      configured: "api",
      active: "api",
      label: "WUD API",
    }),
  };
}

function mockApplyPlan(updates: ReturnType<typeof useUpdatesStore>): void {
  vi.spyOn(updates, "createPlan").mockImplementation(async () => {
    updates.plan = planResponse();
  });
  vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
    const job = applyJobResponse();
    updates.setApplyJob(job);
    return job;
  });
}

async function previewAndApplyFirstUpdate(
  wrapper: ReturnType<typeof mountPendingView>,
): Promise<void> {
  await wrapper
    .findAll("button")
    .find((button) => button.text().includes("Review media plan"))
    ?.trigger("click");
  await flushPromises();
  await wrapper
    .find('[role="dialog"]')
    .findAll("button")
    .find((button) => button.text().includes("Apply 1 update"))
    ?.trigger("click");
  await flushPromises();
}

describe("pending view apply jobs", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("creates an apply job only after explicit confirmation", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    let pendingRefreshRemovesSelection = false;
    const loadPending = vi.spyOn(updates, "loadPending").mockImplementation(async () => {
      updates.plan = null;
      if (pendingRefreshRemovesSelection) {
        updates.pending = pendingResponse([]);
      }
    });
    const loadReleaseNotes = vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    const refreshReleaseNotes = vi
      .spyOn(updates, "refreshReleaseNotes")
      .mockResolvedValue();
    const loadRuns = vi.spyOn(runs, "loadRuns").mockResolvedValue();
    const loadRunDetail = vi
      .spyOn(runs, "loadRunDetail")
      .mockImplementation(async (runId: number) => {
        runs.runDetails = {
          ...runs.runDetails,
          [runId]: {
            ...runSummary({
              id: runId,
              dry_run: false,
              mode: "apply",
              log_file: "/out/logs/job-test.log",
            }),
            pending_updates: [],
            verification: runVerification(),
          },
        };
      });
    const previewReleaseNotifications = vi
      .spyOn(updates, "previewReleaseNotifications")
      .mockImplementation(async () => {
        updates.releaseNotification = releaseNotificationResponse();
      });
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        selected_selections: [
          { line_no: 1, selection_id: "selection-reviewed" },
        ],
      });
    });
    const applyPlan = vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
      const job = applyJobResponse();
      updates.setApplyJob(job);
      return job;
    });
    const focus = vi
      .spyOn(HTMLElement.prototype, "focus")
      .mockImplementation(() => undefined);
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    expect(applyPlan).not.toHaveBeenCalled();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);

    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"))
      ?.trigger("click");

    expect(applyPlan).toHaveBeenCalledWith(
      "plan-test",
      [1],
      true,
      [],
      [],
      {
        selections: [{ line_no: 1, selection_id: "selection-reviewed" }],
        tagStreamDecisions: [],
        tagStreamLabelRewriteApprovals: [],
      },
    );
    expect(jobStream.observed).toBe(true);
    await flushPromises();

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    const applyPanel = wrapper.find(".apply-job-panel");
    expect(applyPanel.text()).toContain("Applying 1 update");
    expect(applyPanel.text()).toContain("Current status");
    expect(applyPanel.text()).toContain("Queued to start");
    expect(applyPanel.text()).toContain("Latest log line");
    expect(applyPanel.text()).toContain("Applied scope");
    const panel = applyPanel.element;
    const panelStatus = wrapper.find("#apply-job-panel-status").element;
    const panelLatestLog = wrapper.find(
      '[aria-labelledby="apply-job-latest-log-title"]',
    ).element;
    const panelProgress = wrapper.find(
      '[aria-labelledby="apply-job-progress-title"]',
    ).element;
    const panelDetails = wrapper.find(".apply-job-details").element;
    expect(wrapper.find(".run-verification-panel").exists()).toBe(false);
    expect(focus.mock.contexts).toContain(panel);
    expect(
      Boolean(panelStatus.compareDocumentPosition(panelLatestLog) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(
      Boolean(panelLatestLog.compareDocumentPosition(panelProgress) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(
      Boolean(panelProgress.compareDocumentPosition(panelDetails) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(wrapper.find(".apply-job-details").attributes("open")).toBeUndefined();
    expect(applyPanel.text()).toContain("repo/app:1.0");

    jobStream.emitProgress({
      job_id: "job-test",
      phase: "pull",
      status: "running",
      message: "[media] Pulling selected image updates.",
      created_at: "2026-05-28T12:00:01+00:00",
      stack: "media",
      services: ["calibre"],
      line_numbers: [1],
    });
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("Update progress");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Pull images");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Running");
    expect(wrapper.find(".apply-job-panel").text()).toContain("media");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Running: Pull images");
    expect(wrapper.find(".apply-job-panel").text()).toContain("media / calibre / lines 1");
    expect(wrapper.find(".apply-progress-step-running").attributes("aria-current")).toBe(
      "step",
    );

    jobStream.emitLog(
      applyJobLogResponse({
        content: "[2026-05-28T12:00:00+00:00] [INFO] docker-update-from-wud-v2\n",
      }),
    );
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("docker-update-from-wud-v2");

    pendingRefreshRemovesSelection = true;
    jobStream.emitJob(applyJobResponse({ status: "success", run_id: 10 }));
    await flushPromises();

    expect(loadPending).toHaveBeenCalled();
    expect(loadReleaseNotes).toHaveBeenCalled();
    expect(refreshReleaseNotes).toHaveBeenCalled();
    expect(loadRuns).toHaveBeenCalled();
    expect(loadRunDetail).toHaveBeenCalledWith(10);
    expect(wrapper.find(".apply-job-panel").text()).toContain("Apply complete");
    expect(wrapper.find(".apply-job-panel").text()).toContain("repo/app:1.0");
    expect(wrapper.find(".apply-job-panel").text()).toContain("#10");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Update complete");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Post-update verification");
    expect(wrapper.find(".apply-job-panel").text()).toContain("Verified");
    expect(wrapper.find(".apply-job-panel").text()).toContain("New image running");
    expect(wrapper.find(".apply-job-panel").text()).toContain("WUD line removed");
    await wrapper
      .find(".apply-job-panel")
      .findAll("button")
      .find((button) => button.text().includes("Preview release notes"))
      ?.trigger("click");
    await flushPromises();
    expect(previewReleaseNotifications).toHaveBeenCalledWith({ run_id: 10 });
    expect(wrapper.find("dialog").text()).toContain(
      "Send Discord notifications",
    );
    expect(wrapper.find(".apply-job-details").attributes("open")).toBe("");
    expect(wrapper.find(".apply-job-live-log-body").attributes("style")).toContain(
      "display: none",
    );
    const showOutputButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Show output"));
    if (!showOutputButton) {
      throw new Error("Missing completed live log toggle");
    }
    expect(showOutputButton.attributes("aria-expanded")).toBe("false");
    await showOutputButton.trigger("click");
    await nextTick();
    expect(
      wrapper.find(".apply-job-live-log-body").attributes("style") ?? "",
    ).not.toContain("display: none");
    expect(wrapper.find(".apply-job-log-viewer").text()).toContain(
      "docker-update-from-wud-v2",
    );
    expect(showOutputButton.attributes("aria-expanded")).toBe("true");
    expect(wrapper.find(".batch-action-bar").text()).toContain("Select updates to review");
  });

  it("refreshes api-backed pending reads after successful apply without a second rescan", async () => {
    const { pinia, settings, updates, runs } = setupStores(true);
    updates.pending = apiBackedPendingResponse();
    const { loadPending, loadReleaseNotes } = mockPendingLifecycle(settings, updates);
    vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();
    mockApplyPlan(updates);
    const rescanPending = vi.spyOn(updates, "rescanPending");
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await previewAndApplyFirstUpdate(wrapper);

    loadPending.mockClear();
    loadReleaseNotes.mockClear();
    jobStream.emitJob(applyJobResponse({ status: "success", run_id: 10 }));
    await flushPromises();

    expect(rescanPending).not.toHaveBeenCalled();
    expect(loadPending).toHaveBeenCalledTimes(1);
    expect(loadPending).toHaveBeenCalledWith({ freshAfterCurrent: true });
    expect(loadReleaseNotes).toHaveBeenCalledTimes(1);
  });

  it("reloads pending state after terminal remembered apply jobs", async () => {
    window.sessionStorage.setItem("applyJobId", "job-terminal");
    const { pinia, settings, updates, runs } = setupStores(true);
    const apiPending = apiBackedPendingResponse();
    const loadPending = vi.spyOn(updates, "loadPending").mockImplementation(async () => {
      updates.pending = apiPending;
    });
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "loadSecurityScans").mockResolvedValue();
    vi.spyOn(updates, "loadApplyJobLogFromRun").mockResolvedValue(
      applyJobLogResponse({ job_id: "job-terminal" }),
    );
    vi.spyOn(settings, "loadPendingSafetyCues").mockResolvedValue();
    vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();
    vi.spyOn(webApi, "job").mockResolvedValue(
      applyJobResponse({
        job_id: "job-terminal",
        status: "success",
        run_id: 10,
      }),
    );
    const rescanPending = vi.spyOn(updates, "rescanPending");

    mountPendingView(pinia);
    await flushPromises();

    expect(loadPending).toHaveBeenCalledTimes(2);
    expect(loadPending).toHaveBeenLastCalledWith({ freshAfterCurrent: true });
    expect(rescanPending).not.toHaveBeenCalled();
  });

  it("keeps an earlier phase failure visible after a later same-phase success", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse();
    });
    vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
      const job = applyJobResponse();
      updates.setApplyJob(job);
      return job;
    });
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"))
      ?.trigger("click");
    await flushPromises();

    const failedPull = {
      job_id: "job-test",
      phase: "pull",
      status: "failure" as const,
      message: "[media] Pull failed.",
      created_at: "2026-05-28T12:00:02+00:00",
      stack: "media",
      services: ["calibre"],
      line_numbers: [1],
    };
    const laterPullSuccess = {
      job_id: "job-test",
      phase: "pull",
      status: "success" as const,
      message: "[infra] Images pulled and verified.",
      created_at: "2026-05-28T12:00:03+00:00",
      stack: "infra",
      services: ["watchtower"],
      line_numbers: [2],
    };

    jobStream.emitProgress(failedPull);
    jobStream.emitProgress(laterPullSuccess);
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("Pull images failed");
    expect(wrapper.find(".apply-job-panel").text()).toContain("[media] Pull failed.");
    expect(wrapper.find(".apply-job-panel").text()).toContain(
      "media / calibre / lines 1",
    );
    expect(wrapper.find(".apply-job-panel").text()).toContain("Pull images failed");

    jobStream.emitJob(
      applyJobResponse({
        status: "failure",
        error: "updater exited with status 1",
        progress: [failedPull, laterPullSuccess],
      }),
    );
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("Failed: Pull images");
    expect(wrapper.find(".apply-job-panel").text()).toContain("[media] Pull failed.");
    expect(wrapper.find(".apply-job-panel").text()).toContain(
      "media / calibre / lines 1",
    );
  });

  it("loads the persisted run log when the job stream ends without live log content", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    const loadRuns = vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse();
    });
    vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
      const job = applyJobResponse({ status: "running" });
      updates.setApplyJob(job);
      return job;
    });
    const runLog = vi.spyOn(webApi, "runLog").mockResolvedValue({
      run_id: 10,
      log_file: "/out/logs/run-10.log",
      exists: true,
      content: "fallback run log\n",
      truncated: false,
      max_bytes: 65_536,
    });
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"))
      ?.trigger("click");
    await flushPromises();

    jobStream.emitJob(
      applyJobResponse({
        status: "success",
        run_id: 10,
        log_file: "/out/logs/job-terminal.log",
      }),
    );
    await flushPromises();

    expect(runLog).toHaveBeenCalledWith(10, 65_536);
    expect(loadRuns).toHaveBeenCalled();
    expect(jobStream.close).toHaveBeenCalled();
    expect(wrapper.find(".apply-job-panel").text()).toContain("fallback run log");
    expect(wrapper.find(".apply-job-live-log-body").attributes("style")).toContain(
      "display: none",
    );

    const showOutputButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Show output"));
    if (!showOutputButton) {
      throw new Error("Missing completed live log toggle");
    }
    await showOutputButton.trigger("click");
    await nextTick();
    expect(
      wrapper.find(".apply-job-live-log-body").attributes("style") ?? "",
    ).not.toContain("display: none");
    expect(wrapper.find(".apply-job-log-viewer").text()).toContain("fallback run log");
  });

  it("loads the persisted run log for already-terminal apply job state", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    updates.setApplyJob(
      applyJobResponse({
        status: "success",
        run_id: 10,
        log_file: "/out/logs/job-terminal.log",
      }),
    );
    const runLog = vi.spyOn(webApi, "runLog").mockResolvedValue({
      run_id: 10,
      log_file: "/out/logs/run-10.log",
      exists: true,
      content: "existing terminal run log\n",
      truncated: false,
      max_bytes: 65_536,
    });

    const wrapper = mountPendingView(pinia);
    await flushPromises();

    expect(runLog).toHaveBeenCalledWith(10, 65_536);
    expect(wrapper.find(".apply-job-panel").text()).toContain(
      "existing terminal run log",
    );
    expect(wrapper.find(".apply-job-live-log-body").attributes("style")).toContain(
      "display: none",
    );

    const showOutputButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Show output"));
    if (!showOutputButton) {
      throw new Error("Missing completed live log toggle");
    }
    await showOutputButton.trigger("click");
    await nextTick();
    expect(
      wrapper.find(".apply-job-live-log-body").attributes("style") ?? "",
    ).not.toContain("display: none");
    expect(wrapper.find(".apply-job-log-viewer").text()).toContain(
      "existing terminal run log",
    );
  });

  it("keeps failed apply jobs visible with the confirmed plan impact", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse();
    });
    vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
      const job = applyJobResponse({
        status: "running",
        started_at: "2026-05-28T12:00:00+00:00",
      });
      updates.setApplyJob(job);
      return job;
    });
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("running");
    expect(wrapper.find(".apply-job-panel").text()).toContain("repo/app:1.0");

    jobStream.emitJob(
      applyJobResponse({
        status: "failure",
        error: "updater exited with status 1",
      }),
    );
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("Apply failed");
    expect(wrapper.find(".apply-job-panel").text()).toContain(
      "updater exited with status 1",
    );
    expect(wrapper.find(".apply-job-panel").text()).toContain("repo/app:1.0");
    expect(jobStream.close).toHaveBeenCalled();
  });

  it("reports invalid log stream payloads without closing the job stream", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(runs, "loadRuns").mockResolvedValue();
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse();
    });
    vi.spyOn(updates, "applyPlan").mockImplementation(async () => {
      const job = applyJobResponse({ status: "running" });
      updates.setApplyJob(job);
      return job;
    });
    const jobStream = mockApplyJobStream();
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"))
      ?.trigger("click");
    await flushPromises();

    jobStream.emitInvalidLog();
    await flushPromises();

    expect(wrapper.text()).toContain("Job log stream returned invalid data.");
    expect(jobStream.close).not.toHaveBeenCalled();

    jobStream.emitLog(applyJobLogResponse({ content: "next log line\n" }));
    await flushPromises();

    expect(wrapper.find(".apply-job-panel").text()).toContain("next log line");
  });

  it("shows recovery guidance when a remembered apply job is missing", async () => {
    window.sessionStorage.setItem("applyJobId", "job-lost");
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    vi.spyOn(updates, "loadPending").mockResolvedValue();
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(webApi, "job").mockRejectedValue(
      new ApiError(404, "apply job not found"),
    );
    const loadRuns = vi.spyOn(runs, "loadRuns").mockImplementation(async () => {
      runs.runs = [runSummary({ id: 42 })];
    });
    const wrapper = mountPendingView(pinia);

    await flushPromises();

    expect(webApi.job).toHaveBeenCalledWith("job-lost");
    expect(loadRuns).toHaveBeenCalled();
    expect(updates.applyJob).toBeNull();
    expect(updates.rememberedApplyJobId).toBe("");
    expect(window.sessionStorage.getItem("applyJobId")).toBeNull();
    expect(wrapper.text()).toContain(APPLY_JOB_RECOVERY_MESSAGE);
    expect(wrapper.text()).toContain("Review History");
    expect(wrapper.text()).toContain("No related run is known");
    expect(wrapper.find(".apply-recovery").text()).not.toContain("Latest run");
    expect(wrapper.find(".apply-recovery").text()).not.toContain("Review run #42");
  });
});
