import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { createMemoryHistory, type Router } from "vue-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";

import type {
  PendingUpdateRecord,
  RunDetail,
  RunEventRecord,
  RollbackPlanResponse,
  RunSummary,
  RunVerificationSummary,
} from "../src/api/client";
import { createWudRouter } from "../src/router";
import { useAuthStore } from "../src/stores/auth";
import { useSettingsStore } from "../src/stores/settings";
import { useRunsStore } from "../src/stores/runs";
import { displayDigest } from "../src/utils/digestProvenance";
import RunDetailView from "../src/views/RunDetailView.vue";
import RunsView from "../src/views/RunsView.vue";
import {
  authSession,
  coreUpdateTourResponse,
  rollbackPlan,
  runVerification,
  runSummary,
} from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

function mockMediaQueries(matches: (query: string) => boolean): void {
  globalThis.window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: matches(query),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

function mockDesktopViewport(): void {
  mockMediaQueries((query) => query.includes("min-width"));
}

function mockMobileViewport(): void {
  mockMediaQueries((query) => query.includes("max-width"));
}

async function setupRoute(path: string): Promise<{
  pinia: ReturnType<typeof createPinia>;
  router: Router;
  settings: ReturnType<typeof useSettingsStore>;
  runs: ReturnType<typeof useRunsStore>;
}> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.session = authSession({ authenticated: true });
  const settings = useSettingsStore();
  const runs = useRunsStore();
  settings.coreUpdateTour = coreUpdateTourResponse();
  const router = createWudRouter(createMemoryHistory());
  await router.push(path);
  await router.isReady();
  return { pinia, router, settings, runs };
}

function runEvent(overrides?: Partial<RunEventRecord>): RunEventRecord {
  return {
    id: 1,
    run_id: 1,
    created_at: "2026-05-28T12:00:00+00:00",
    service_name: "app",
    stack_name: "media",
    image: "repo/app:1.0",
    target_image: "repo/app:1.1",
    old_image_id: "",
    new_image_id: "",
    old_digest: "",
    new_digest: "",
    status: "success",
    metadata: {},
    ...(overrides ?? {}),
  };
}

function pendingUpdate(
  overrides?: Partial<PendingUpdateRecord>,
): PendingUpdateRecord {
  return {
    id: 1,
    run_id: 1,
    line_no: 1,
    raw: "repo/app:1.0 sha256=abc",
    image: "repo/app:1.0",
    target_digest: "sha256:def",
    desired_tag: "1.1",
    service_key: "media/app",
    stack_name: "media",
    service_name: "app",
    status: "planned",
    status_reason: "selected",
    created_at: "2026-05-28T12:00:00+00:00",
    updated_at: "2026-05-28T12:00:00+00:00",
    metadata: {},
    ...(overrides ?? {}),
  };
}

function runDetail(overrides?: Partial<RunDetail>): RunDetail {
  const resolvedOverrides = overrides ?? {};
  const id = resolvedOverrides.id ?? 42;
  return {
    ...runSummary({
      id,
      dry_run: false,
      mode: "apply",
      log_file: `/out/logs/run-${id}.log`,
      events: [runEvent({ run_id: id })],
    }),
    pending_updates: [pendingUpdate({ run_id: id })],
    verification: runVerification(),
    ...resolvedOverrides,
  };
}

function emptyVerification(): RunVerificationSummary {
  return runVerification({
    total_count: 0,
    verified_count: 0,
    needs_review_count: 0,
    items: [],
  });
}

function multiServiceEvents(runId: number): RunEventRecord[] {
  return [
    runEvent({ id: 1, run_id: runId, service_name: "alpha" }),
    runEvent({ id: 2, run_id: runId, service_name: "alpha" }),
    runEvent({ id: 3, run_id: runId, service_name: "", stack_name: "beta" }),
    runEvent({ id: 4, run_id: runId, service_name: "", stack_name: "" }),
    runEvent({ id: 5, run_id: runId, service_name: "delta" }),
  ];
}

function actionRuns(): RunSummary[] {
  const cases: Array<{ mode: string; dryRun: boolean }> = [
    { mode: "cli", dryRun: true },
    { mode: "cli", dryRun: false },
    { mode: "apply", dryRun: false },
    { mode: "auto-update", dryRun: false },
    { mode: "cleanup", dryRun: false },
    { mode: "snooze-created", dryRun: false },
    { mode: "snooze-removed", dryRun: false },
    { mode: "service-policy-upserted", dryRun: false },
    { mode: "service-policy-deleted", dryRun: false },
    { mode: "tag-exclusion-upserted", dryRun: false },
    { mode: "tag-exclusion-status", dryRun: false },
    { mode: "web-auth", dryRun: false },
    { mode: "web-state", dryRun: false },
    { mode: "web-pending-cleanup", dryRun: false },
    { mode: "web-pending-removal", dryRun: false },
    { mode: "web-settings", dryRun: false },
    { mode: "container-restart", dryRun: false },
    { mode: "", dryRun: false },
    { mode: "custom-action", dryRun: false },
  ];

  return cases.map((item, index) => {
    const id = index + 1;
    let events: RunEventRecord[] = [];
    if (id === 1) {
      events = multiServiceEvents(id);
    } else if (id === 3) {
      events = [
        runEvent({ id: 10, run_id: id, service_name: "api" }),
        runEvent({ id: 11, run_id: id, service_name: "worker" }),
      ];
    }
    return runSummary({
      id,
      dry_run: item.dryRun,
      mode: item.mode,
      finished_at: id === 1 ? "2026-05-28T12:10:00+00:00" : null,
      events,
    });
  });
}

describe("RunsView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mockDesktopViewport();
  });

  it("loads and renders desktop history rows with action and service labels", async () => {
    const { pinia, router, settings, runs } = await setupRoute("/runs");
    runs.error = "history database is unavailable";
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "in_progress",
      step: "runs_history",
    });
    runs.runs = actionRuns();
    const loadRuns = vi.spyOn(runs, "loadRuns").mockResolvedValue();

    const wrapper = mountWithApp(RunsView, { pinia, router });
    await flushPromises();

    expect(loadRuns).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[role="alert"]').text()).toContain(
      "history database is unavailable",
    );
    expect(wrapper.find(".history-view-tab.active").text()).toBe("All runs");
    expect(wrapper.text()).toContain(`${runs.runs.length} recent runs`);
    expect(wrapper.text()).toContain("Verify the run afterward");
    expect(wrapper.text()).toContain("Details and logs stay linked from each run");
    expect(wrapper.find('[role="table"]').exists()).toBe(true);
    expect(wrapper.find('a[href="/runs/1"]').text()).toBe("#1");

    const rows = wrapper.findAll('[role="row"]');
    expect(rows[0].text()).toContain("CLI (dry run)");
    expect(rows[0].text()).toContain("4 services across 3 stacks");
    expect(rows[0].text()).toContain("Dry run · no changes applied");
    expect(rows[0].find("time").attributes("title")).toBe("2026-05-28T12:10:00+00:00");
    expect(rows[1].text()).toContain("CLI");
    expect(rows[1].text()).not.toContain("dry run");
    expect(rows[2].text()).toContain("Apply");
    expect(rows[2].text()).toContain("2");
    expect(rows[2].text()).toContain("media · 2 services");

    for (const label of [
      "Auto update",
      "Cleanup",
      "Snooze created",
      "Snooze removed",
      "Policy changed",
      "Policy removed",
      "Tag exclusion saved",
      "Tag exclusion status changed",
      "Web auth",
      "Web state",
      "Pending cleanup",
      "Pending removal",
      "Settings changed",
      "Container restarted",
      "Unknown",
      "custom-action",
    ]) {
      expect(wrapper.text()).toContain(label);
    }
  });

  it("renders mobile run cards with running and finished states", async () => {
    mockMobileViewport();
    const { pinia, router, runs } = await setupRoute("/runs");
    runs.runs = [
      runSummary({
        id: 21,
        status: "running",
        mode: "cli",
        dry_run: true,
        finished_at: null,
        events: [runEvent({ id: 21, run_id: 21, service_name: "api" })],
      }),
      runSummary({
        id: 22,
        status: "success",
        mode: "apply",
        dry_run: false,
        finished_at: "2026-05-28T12:05:00+00:00",
        events: [],
      }),
    ];
    vi.spyOn(runs, "loadRuns").mockResolvedValue();

    const wrapper = mountWithApp(RunsView, { pinia, router });
    await flushPromises();

    expect(wrapper.find('[role="table"]').exists()).toBe(false);
    expect(wrapper.findAll(".mobile-card")).toHaveLength(2);
    expect(wrapper.text()).toContain("#21 running");
    expect(wrapper.text()).toContain("CLI (dry run)");
    expect(wrapper.text()).toContain("api · 1.0 → 1.1");
    expect(wrapper.text()).toContain("Started");
    expect(wrapper.text()).toContain("#22 success");
    expect(wrapper.text()).toContain("Apply");
    expect(wrapper.findAll("time")[1].attributes("title")).toBe("2026-05-28T12:05:00+00:00");
  });

  it("renders the mobile empty state when there are no runs", async () => {
    mockMobileViewport();
    const { pinia, router, runs } = await setupRoute("/runs");
    runs.runs = [];
    vi.spyOn(runs, "loadRuns").mockResolvedValue();

    const wrapper = mountWithApp(RunsView, { pinia, router });
    await flushPromises();

    expect(wrapper.find(".empty-state").text()).toBe("No runs recorded.");
  });
});

describe("RunDetailView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("loads run detail, renders evidence, and reloads when the route id changes", async () => {
    const { pinia, router, runs } = await setupRoute("/runs/42");
    const oldDigest = "sha256:old-digest-value-12345";
    const newDigest = "sha256:new-digest-value-12345";
    const newOnlyDigest = "sha256:new-only-digest-value";
    const oldOnlyDigest = "sha256:old-only-digest-value";
    const provenanceDigest =
      "sha256:2222222222222222222222222222222222222222222222222222222222222222";
    const oldImageId = "sha256:old-image-id-12345";
    const newImageId = "sha256:new-image-id-12345";
    const firstDetail = runDetail({
      id: 42,
      mode: "apply",
      dry_run: false,
      status: "success",
      pending_updates: [
        pendingUpdate({
          id: 7,
          run_id: 42,
          line_no: 7,
          service_key: "media/api",
          status: "planned",
          status_reason: "selected",
        }),
        pendingUpdate({
          id: 8,
          run_id: 42,
          line_no: 8,
          service_key: "",
          image: "repo/fallback:1.0",
          status: "skipped",
          status_reason: "stale",
        }),
      ],
      events: [
        runEvent({
          id: 100,
          run_id: 42,
          service_name: "api",
          image: "repo/api:1.0",
          old_digest: oldDigest,
          new_digest: newDigest,
          old_image_id: oldImageId,
          new_image_id: newImageId,
        }),
        runEvent({
          id: 101,
          run_id: 42,
          service_name: "",
          stack_name: "stack-only",
          image: "repo/worker:1.0",
          new_digest: newOnlyDigest,
        }),
        runEvent({
          id: 102,
          run_id: 42,
          service_name: "",
          stack_name: "",
          image: "repo/fallback:1.0",
          old_digest: oldOnlyDigest,
        }),
        runEvent({
          id: 103,
          run_id: 42,
          service_name: "digest",
          stack_name: "media",
          image: "repo/digest@sha256:old",
          target_image: `repo/digest@${provenanceDigest}`,
          new_digest: provenanceDigest,
          digest_provenance: {
            source_image: "repo/digest:latest",
            resolved_tag: "latest",
            watch_tag: "latest",
            target_digest: provenanceDigest,
            final_image: `repo/digest@${provenanceDigest}`,
            provenance_source: "apply",
            provenance_confidence: "verified",
          },
        }),
      ],
      verification: runVerification({
        status: "needs_review",
        total_count: 2,
        verified_count: 1,
        needs_review_count: 1,
        items: [
          {
            line_no: 7,
            service_key: "media/api",
            stack_name: "media",
            service_name: "api",
            image: "repo/api:1.0",
            target_image: "repo/api:1.1",
            image_status: "new_image_running",
            container_status: "recreated",
            health_status: "passed",
            wud_status: "removed",
            follow_up_needed: false,
            summary: "new image running, container recreated, health passed, WUD line removed.",
          },
          {
            line_no: 8,
            service_key: "",
            stack_name: "",
            service_name: "",
            image: "repo/fallback:1.0",
            target_image: "repo/fallback:1.0",
            image_status: "unknown",
            container_status: "unknown",
            health_status: "unknown",
            wud_status: "restored",
            follow_up_needed: true,
            summary: "Manual review needed.",
          },
        ],
      }),
    });
    const secondDetail = runDetail({
      id: 43,
      mode: "cleanup",
      dry_run: true,
      status: "failure",
      log_file: "",
      pending_updates: [],
      events: [],
      verification: emptyVerification(),
    });
    const loadRunDetail = vi
      .spyOn(runs, "loadRunDetail")
      .mockImplementation(async (runId: number) => {
        runs.runDetails = {
          ...runs.runDetails,
          [runId]: runId === 42 ? firstDetail : secondDetail,
        };
      });

    const wrapper = mountWithApp(RunDetailView, { pinia, router });
    await flushPromises();
    await nextTick();

    expect(loadRunDetail).toHaveBeenCalledWith(42);
    expect(wrapper.text()).toContain("#42");
    expect(wrapper.find('a[href="/runs/42/log"]').text()).toContain("View log");
    expect(wrapper.text()).toContain("Status");
    expect(wrapper.text()).toContain("success");
    expect(wrapper.text()).toContain("Mode");
    expect(wrapper.text()).toContain("apply");
    expect(wrapper.text()).toContain("Dry run");
    expect(wrapper.text()).toContain("No");
    expect(wrapper.text()).toContain("Updates");
    expect(wrapper.text()).toContain("2");
    expect(wrapper.text()).toContain("#7");
    expect(wrapper.text()).toContain("media/api");
    expect(wrapper.text()).toContain("planned selected");
    expect(wrapper.text()).toContain("#8");
    expect(wrapper.text()).toContain("repo/fallback:1.0");
    expect(wrapper.text()).toContain("skipped stale");
    expect(wrapper.text()).toContain("Post-update verification");
    expect(wrapper.text()).toContain("Needs review");
    expect(wrapper.text()).toContain("New image running");
    expect(wrapper.text()).toContain("Health passed");
    expect(wrapper.text()).toContain("WUD line removed");
    expect(wrapper.text()).toContain("Follow-up needed");
    expect(wrapper.text()).toContain("api");
    expect(wrapper.text()).toContain("repo/api:1.0");
    expect(wrapper.text()).toContain("stack-only");
    expect(wrapper.text()).toContain("service");
    expect(wrapper.text()).toContain("repo/digest: latest -> latest");
    expect(wrapper.text()).toContain(
      `Digest: ${displayDigest(provenanceDigest)}`,
    );
    expect(wrapper.text().indexOf("repo/digest: latest -> latest")).toBeLessThan(
      wrapper.text().indexOf(`Digest: ${displayDigest(provenanceDigest)}`),
    );
    expect(wrapper.text()).toContain(
      `Digest: ${oldDigest.substring(0, 15)}... -> ${newDigest.substring(0, 15)}...`,
    );
    expect(wrapper.text()).toContain(
      `Digest: none -> ${newOnlyDigest.substring(0, 15)}...`,
    );
    expect(wrapper.text()).toContain(
      `Digest: ${oldOnlyDigest.substring(0, 15)}... -> none`,
    );
    expect(wrapper.text()).toContain(
      `Image ID: ${oldImageId.substring(0, 15)}... -> ${newImageId.substring(0, 15)}...`,
    );

    await router.push("/runs/43");
    await flushPromises();
    await nextTick();

    expect(loadRunDetail).toHaveBeenLastCalledWith(43);
    expect(wrapper.text()).toContain("#43");
    expect(wrapper.text()).toContain("failure");
    expect(wrapper.text()).toContain("No log path");
  });

  it("loads and renders verified rollback guidance only on demand", async () => {
    const { pinia, router, runs } = await setupRoute("/runs/51");
    const detail = runDetail({
      id: 51,
      mode: "stop",
      dry_run: false,
      finished_at: "2026-05-28T12:05:00+00:00",
      events: [runEvent({ id: 51, run_id: 51 })],
    });
    runs.runDetails = { 51: detail };
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();

    const ready = rollbackPlan({ run_id: 51 }).items[0]!;
    const plan = rollbackPlan({
      run_id: 51,
      status: "partial",
      detail: "Some services have verified rollback targets; others are blocked.",
      ready_count: 1,
      blocked_count: 1,
      items: [
        ready,
        {
          ...ready,
          event_id: 52,
          service_key: "media/worker",
          service_name: "worker",
          status: "blocked",
          reason: "A later successful updater run #52 changed this service.",
          rollback_image: "",
        },
      ],
    });
    const loadRollbackPlan = vi
      .spyOn(runs, "loadRollbackPlan")
      .mockImplementation(async () => {
        runs.rollbackPlans = { 51: plan };
      });

    const wrapper = mountWithApp(RunDetailView, { pinia, router });
    await flushPromises();

    expect(wrapper.text()).toContain("Rollback plan");
    expect(wrapper.text()).toContain("Read-only guidance");
    expect(wrapper.text()).not.toContain("Exact rollback target");

    await wrapper.get("button").trigger("click");
    await flushPromises();

    expect(loadRollbackPlan).toHaveBeenCalledWith(51);
    expect(wrapper.text()).toContain("partial");
    expect(wrapper.text()).toContain("1 ready · 1 blocked · 0 not needed");
    expect(wrapper.text()).toContain("media/app");
    expect(wrapper.text()).toContain(ready.rollback_image);
    expect(wrapper.text()).toContain("Replace this service image with the exact rollback target.");
    expect(wrapper.text()).toContain("media/worker");
    expect(wrapper.text()).toContain("later successful updater run #52");
    expect(wrapper.text()).not.toContain("docker compose -f");
  });

  it.each([
    {
      label: "ready",
      plan: rollbackPlan({ run_id: 61 }),
      expected: "Exact rollback target",
      guidance: true,
    },
    {
      label: "blocked",
      plan: rollbackPlan({
        run_id: 61,
        status: "blocked",
        detail: "No service has a verified rollback target; review each blocker.",
        ready_count: 0,
        blocked_count: 1,
        items: [
          {
            ...rollbackPlan().items[0]!,
            status: "blocked",
            reason: "The current Compose image no longer matches the recorded target image.",
          },
        ],
      }),
      expected: "current Compose image no longer matches",
      guidance: false,
    },
    {
      label: "unavailable",
      plan: rollbackPlan({
        run_id: 61,
        status: "unavailable",
        detail: "Could not verify current Compose state.",
        ready_count: 0,
        items: [],
      }),
      expected: "Could not verify current Compose state.",
      guidance: false,
    },
    {
      label: "empty",
      plan: rollbackPlan({
        run_id: 61,
        status: "not_applicable",
        detail: "This run has no recorded update events.",
        ready_count: 0,
        items: [],
      }),
      expected: "This run has no recorded update events.",
      guidance: false,
    },
  ] satisfies Array<{
    label: string;
    plan: RollbackPlanResponse;
    expected: string;
    guidance: boolean;
  }>)("renders the $label rollback-plan state", async ({ plan, expected, guidance }) => {
    const { pinia, router, runs } = await setupRoute("/runs/61");
    runs.runDetails = {
      61: runDetail({
        id: 61,
        mode: "stop",
        dry_run: false,
        finished_at: "2026-05-28T12:05:00+00:00",
        events: [runEvent({ id: 61, run_id: 61 })],
      }),
    };
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();
    vi.spyOn(runs, "loadRollbackPlan").mockImplementation(async () => {
      runs.rollbackPlans = { 61: plan };
    });

    const wrapper = mountWithApp(RunDetailView, { pinia, router });
    await flushPromises();
    await wrapper.get("button").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain(expected);
    expect(wrapper.text()).toContain(plan.status.replaceAll("_", " "));
    expect(wrapper.text()).toContain("Read-only guidance");
    expect(wrapper.text().includes("Replace this service image")).toBe(guidance);
  });

  it("renders fallback labels for unexpected verification statuses", async () => {
    const { pinia, router, runs } = await setupRoute("/runs/12");
    const unexpectedItem = {
      ...runVerification().items[0],
      image_status: "registry_pending",
      container_status: "container_pending",
      health_status: "probe_pending",
      wud_status: "file_pending",
    } as unknown as RunVerificationSummary["items"][number];
    runs.runDetails = {
      12: runDetail({
        id: 12,
        verification: runVerification({
          status: "needs_review",
          verified_count: 0,
          needs_review_count: 1,
          items: [unexpectedItem],
        }),
      }),
    };
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();

    const wrapper = mountWithApp(RunDetailView, { pinia, router });
    await flushPromises();

    expect(wrapper.text()).toContain("registry_pending");
    expect(wrapper.text()).toContain("container_pending");
    expect(wrapper.text()).toContain("probe_pending");
    expect(wrapper.text()).toContain("file_pending");
  });

  it("renders error and empty detail states", async () => {
    const { pinia, router, runs } = await setupRoute("/runs/9");
    runs.error = "run detail failed to load";
    runs.runDetails = {
      9: runDetail({
        id: 9,
        mode: "web-wud-rescan",
        status: "failure",
        log_file: "",
        metadata: {
          operation: "rescan_wud",
          status: "blocked",
          wud_api: {
            detail: "WUD API watch request requires authentication",
          },
        },
        pending_updates: [],
        events: [],
        verification: emptyVerification(),
      }),
    };
    vi.spyOn(runs, "loadRunDetail").mockResolvedValue();

    const wrapper = mountWithApp(RunDetailView, { pinia, router });
    await flushPromises();

    expect(wrapper.find('[role="alert"]').text()).toContain(
      "run detail failed to load",
    );
    expect(wrapper.find('a[href="/runs/9/log"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("Run metadata");
    expect(wrapper.text()).toContain("rescan_wud");
    expect(wrapper.text()).toContain("blocked");
    expect(wrapper.text()).toContain("WUD API watch request requires authentication");
    expect(wrapper.text()).toContain("No log path");
    expect(wrapper.text()).toContain("No pending records.");
    expect(wrapper.text()).toContain("No events recorded.");
  });
});
