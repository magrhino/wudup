import { createPinia, setActivePinia } from "pinia";
import { computed, ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";

import type {
  ApplyJobProgressEvent,
  PlanIssue,
  PlanSelectionRequest,
} from "../src/api/client";
import { deferred } from "./helpers/storeActions";
import { usePolledJob } from "../src/composables/usePolledJob";
import { useUpdateTargetOptions } from "../src/composables/useUpdateTargetOptions";
import { useAuthStore } from "../src/stores/auth";
import { useRunsStore } from "../src/stores/runs";
import { useSettingsStore } from "../src/stores/settings";
import { useUpdatesStore } from "../src/stores/updates";
import {
  applyJobLogResponse,
  applyJobResponse,
  applyPreflightResponse,
  authSession,
  pendingGroupedItem,
  pendingItem,
  pendingResponse,
  planResponse,
  snooze,
  updateTarget,
  updateTargetsResponse,
} from "./helpers/fixtures";
import {
  usePendingApplyJob,
  type PendingApplyJobPanelRef,
} from "../src/views/pending/usePendingApplyJob";
import {
  tagStreamLabelApprovalIssueKey,
  usePendingPlanReviewState,
} from "../src/views/pending/usePendingPlanReviewState";
import { usePendingQueueState } from "../src/views/pending/usePendingQueueState";
import {
  pendingSelectionKey,
  usePendingSelectionState,
} from "../src/views/pending/usePendingSelectionState";
import { mockApplyJobStream } from "./helpers/applyJobStream";

describe("useUpdateTargetOptions", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("returns empty options when update targets have not loaded", () => {
    const options = useUpdateTargetOptions();

    expect(options.targets.value).toEqual([]);
    expect(options.serviceKeyOptions.value).toEqual([]);
    expect(options.imageRepoOptions.value).toEqual([]);
    expect(options.tagOptionsForImageRepo("repo/app")).toEqual([]);
  });

  it("derives sorted, de-duplicated service and image options from the updates store", () => {
    const updates = useUpdatesStore();
    updates.updateTargets = updateTargetsResponse([
      updateTarget({
        service_key: "media/sonarr",
        image_repo: "repo/sonarr",
        current_tag: "4.0",
      }),
      updateTarget({
        service_key: "media/radarr",
        image_repo: "repo/radarr",
        current_tag: "5.0",
      }),
      updateTarget({
        service_key: "media/sonarr",
        image_repo: "repo/sonarr",
        current_tag: "4.1",
      }),
      updateTarget({
        service_key: "",
        image_repo: "",
        current_tag: "ignored",
      }),
    ]);
    const options = useUpdateTargetOptions();

    expect(options.serviceKeyOptions.value).toEqual([
      { label: "media/radarr", value: "media/radarr" },
      { label: "media/sonarr", value: "media/sonarr" },
    ]);
    expect(options.imageRepoOptions.value).toEqual([
      { label: "repo/radarr", value: "repo/radarr" },
      { label: "repo/sonarr", value: "repo/sonarr" },
    ]);
  });

  it("finds targets and tag options by service key and image repository", () => {
    const updates = useUpdatesStore();
    updates.updateTargets = updateTargetsResponse([
      updateTarget({
        service_key: "media/radarr",
        image_repo: "repo/radarr",
        current_tag: "5.0",
      }),
      updateTarget({
        service_key: "media/sonarr",
        image_repo: "repo/sonarr",
        current_tag: "4.0",
      }),
      updateTarget({
        service_key: "media/sonarr-beta",
        image_repo: "repo/sonarr",
        current_tag: "  ",
      }),
      updateTarget({
        service_key: "media/sonarr-nightly",
        image_repo: "repo/sonarr",
        current_tag: "4.0",
      }),
    ]);
    const options = useUpdateTargetOptions();

    expect(options.targetForServiceKey("media/radarr")?.image_repo).toBe(
      "repo/radarr",
    );
    expect(options.targetForImageRepo("repo/sonarr")?.service_key).toBe(
      "media/sonarr",
    );
    expect(options.tagOptionsForImageRepo("repo/sonarr")).toEqual([
      { label: "4.0", value: "4.0" },
    ]);
    expect(options.tagOptionsForImageRepo("repo/missing")).toEqual([]);
  });
});

type TestPreviewJob = {
  id: string;
  status: "queued" | "success" | "failure";
};

describe("usePolledJob", () => {
  it("polls until the job reaches a terminal state", async () => {
    vi.useFakeTimers();
    const queued: TestPreviewJob = { id: "preview", status: "queued" };
    const success: TestPreviewJob = { id: "preview", status: "success" };
    const start = vi.fn().mockResolvedValue(queued);
    const poll = vi.fn().mockResolvedValue(success);
    const job = usePolledJob<TestPreviewJob>(
      start,
      poll,
      (value) => value.status !== "queued",
      { intervalMs: 25 },
    );

    try {
      const run = job.run();
      await flushPromises();
      expect(job.polling.value).toBe(true);
      expect(job.job.value).toEqual(queued);

      await vi.advanceTimersByTimeAsync(25);

      await expect(run).resolves.toEqual(success);
      expect(poll).toHaveBeenCalledWith(queued);
      expect(job.job.value).toEqual(success);
      expect(job.polling.value).toBe(false);
      expect(job.error.value).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });

  it("captures start failures and clears polling", async () => {
    const start = vi.fn().mockRejectedValue(new Error("preview failed"));
    const poll = vi.fn();
    const job = usePolledJob<TestPreviewJob>(
      start,
      poll,
      (value) => value.status !== "queued",
      { intervalMs: 0 },
    );

    await expect(job.run()).rejects.toThrow("preview failed");

    expect(poll).not.toHaveBeenCalled();
    expect(job.error.value).toBe("preview failed");
    expect(job.polling.value).toBe(false);
  });

  it("does not restore cleared state when start resolves after reset", async () => {
    const success: TestPreviewJob = { id: "preview", status: "success" };
    let resolveStart: (value: TestPreviewJob) => void = () => {};
    const start = vi.fn().mockReturnValue(
      new Promise<TestPreviewJob>((resolve) => {
        resolveStart = resolve;
      }),
    );
    const poll = vi.fn();
    const job = usePolledJob<TestPreviewJob>(
      start,
      poll,
      (value) => value.status !== "queued",
      { intervalMs: 0 },
    );

    const run = job.run();
    job.reset();
    resolveStart(success);

    await expect(run).resolves.toBeNull();
    expect(poll).not.toHaveBeenCalled();
    expect(job.job.value).toBeNull();
    expect(job.polling.value).toBe(false);
  });

  it("does not restore cleared state when poll resolves after reset", async () => {
    vi.useFakeTimers();
    const queued: TestPreviewJob = { id: "preview", status: "queued" };
    const success: TestPreviewJob = { id: "preview", status: "success" };
    let resolvePoll: (value: TestPreviewJob) => void = () => {};
    const start = vi.fn().mockResolvedValue(queued);
    const poll = vi.fn().mockReturnValue(
      new Promise<TestPreviewJob>((resolve) => {
        resolvePoll = resolve;
      }),
    );
    const job = usePolledJob<TestPreviewJob>(
      start,
      poll,
      (value) => value.status !== "queued",
      { intervalMs: 25 },
    );

    try {
      const run = job.run();
      await flushPromises();
      expect(job.job.value).toEqual(queued);

      await vi.advanceTimersByTimeAsync(25);
      expect(poll).toHaveBeenCalledWith(queued);

      job.reset();
      resolvePoll(success);

      await expect(run).resolves.toBeNull();
      expect(job.job.value).toBeNull();
      expect(job.polling.value).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling when reset while a run is waiting", async () => {
    vi.useFakeTimers();
    const queued: TestPreviewJob = { id: "preview", status: "queued" };
    const start = vi.fn().mockResolvedValue(queued);
    const poll = vi.fn().mockResolvedValue({
      id: "preview",
      status: "success" satisfies TestPreviewJob["status"],
    });
    const job = usePolledJob<TestPreviewJob>(
      start,
      poll,
      (value) => value.status !== "queued",
      { intervalMs: 25 },
    );

    try {
      const run = job.run();
      await flushPromises();

      job.reset();
      await vi.advanceTimersByTimeAsync(25);

      await expect(run).resolves.toBeNull();
      expect(poll).not.toHaveBeenCalled();
      expect(job.job.value).toBeNull();
      expect(job.polling.value).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["start", "poll"] as const)(
    "ignores a stale %s failure after a newer run succeeds",
    async (phase) => {
      vi.useFakeTimers();
      const stale = deferred<TestPreviewJob>();
      const queued: TestPreviewJob = { id: "old", status: "queued" };
      const success: TestPreviewJob = { id: "new", status: "success" };
      const start = vi
        .fn()
        .mockImplementationOnce(() =>
          phase === "start" ? stale.promise : Promise.resolve(queued),
        )
        .mockResolvedValue(success);
      const job = usePolledJob(
        start,
        () => stale.promise,
        (value) => value.status === "success",
        { intervalMs: 25 },
      );
      try {
        const oldRun = job.run();
        await flushPromises();
        if (phase === "poll") {
          await vi.advanceTimersByTimeAsync(25);
        }
        await expect(job.run()).resolves.toEqual(success);
        stale.reject(new Error("obsolete request failed"));
        await expect(oldRun).resolves.toBeNull();
        expect(job.job.value).toEqual(success);
        expect(job.error.value).toBe("");
        expect(job.polling.value).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    },
  );
});

function failedApplyPreflight(code: string, detail: string) {
  const base = applyPreflightResponse();
  return applyPreflightResponse({
    ok: false,
    failures: 1,
    checks: base.checks.map((check) =>
      check.code === code
        ? {
            ...check,
            status: "FAIL" as const,
            detail,
          }
        : check,
    ),
  });
}

function warningApplyPreflight(code: string, detail: string) {
  const base = applyPreflightResponse();
  return applyPreflightResponse({
    warnings: 1,
    checks: base.checks.map((check) =>
      check.code === code
        ? {
            ...check,
            status: "WARN" as const,
            detail,
          }
        : check,
    ),
  });
}

function setupPendingPlanReview(mutationsEnabled = true) {
  const auth = useAuthStore();
  auth.session = authSession({ mutations_enabled: mutationsEnabled });
  const updates = useUpdatesStore();
  const selectedLineNumbers = ref<number[]>([]);
  const selectedSelections = ref<PlanSelectionRequest[]>([]);
  const selectedSelectionKeySet = computed(
    () => new Set(selectedSelections.value.map(pendingSelectionKey)),
  );
  const stackGroups = computed(() => updates.pending?.grouping.groups ?? []);
  const unmatchedItems = computed(() => updates.pending?.grouping.unmatched ?? []);
  const pendingSourceLabel = computed(() => "images.todo");
  const tagOverrideErrorForLines = vi.fn(() => "");
  const state = usePendingPlanReviewState({
    pendingSourceLabel,
    selectedLineNumbers,
    selectedSelections,
    selectedSelectionKeySet,
    stackGroups,
    tagOverrideErrorForLines,
    unmatchedItems,
  });

  return {
    state,
    updates,
    selectedLineNumbers,
    selectedSelections,
    tagOverrideErrorForLines,
  };
}

function setupPendingApplyJob() {
  const updates = useUpdatesStore();
  const runs = useRunsStore();
  const panelRef = ref<PendingApplyJobPanelRef | null>({
    focusPanel: vi.fn(),
    logElement: () => null,
  });
  const loadPendingAndReleaseNotes = vi.fn().mockResolvedValue(undefined);
  const state = usePendingApplyJob({
    applyJobPanelRef: panelRef,
    loadPendingAndReleaseNotes,
  });

  return {
    state,
    updates,
    runs,
    loadPendingAndReleaseNotes,
  };
}

function digestPinApprovalIssue(
  overrides: Partial<PlanIssue> = {},
): PlanIssue {
  return {
    severity: "error",
    code: "compose-digest-pin-label-rewrite-unapproved",
    message: "Digest-pin label rewrite must be approved.",
    line_no: 1,
    stack: "media",
    service: "app",
    hint: "",
    details: {
      stack: "media",
      service: "app",
      label_key: "org.opencontainers.image.version",
      current_label_value: "1.0",
      planned_tag: "1.1",
      proposed_label_value: "1.1",
    },
    ...overrides,
  };
}

describe("usePendingQueueState", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("filters snoozed entries out of selectable stack groups", () => {
    const updates = useUpdatesStore();
    const settings = useSettingsStore();
    const blocked = pendingGroupedItem({
      line_no: 1,
      image: "repo/app:1.0",
      services: ["app"],
    });
    const allowed = pendingGroupedItem({
      line_no: 2,
      image: "repo/worker:1.0",
      repo: "repo/worker",
      services: ["worker"],
    });
    updates.pending = pendingResponse([blocked, allowed]);
    settings.snoozes = [
      snooze({
        kind: "time",
        service_key: "media/app",
        active: true,
      }),
    ];

    const state = usePendingQueueState();

    expect(state.snoozedItems.value.map(({ item }) => item.line_no)).toEqual([1]);
    expect(state.stackGroups.value).toHaveLength(1);
    expect(state.stackGroups.value[0]?.items.map((item) => item.line_no)).toEqual([2]);
    expect(state.selectableLineNumbers.value).toEqual([2]);
    expect(state.pendingSourceLabel.value).toBe("images.todo");
  });

  it("treats whitespace-only pending source files as empty", () => {
    const updates = useUpdatesStore();
    updates.pending = { ...pendingResponse([]), source_file: "   " };

    const state = usePendingQueueState();

    expect(state.pendingSourceLabel.value).toBe("Pending file");
    expect(state.pendingSourceDisplay.value).toBe("Pending file");
  });
});

describe("usePendingSelectionState", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("syncs tag overrides with pending items and builds apply payloads", () => {
    const updates = useUpdatesStore();
    const onSelectionChanged = vi.fn();
    updates.pending = pendingResponse([
      pendingItem({
        line_no: 1,
        image: "repo/app:1.0",
        desired_tag: "1.1",
      }),
      pendingItem({
        line_no: 2,
        image: "repo/worker:1.0",
        repo: "repo/worker",
        desired_tag: "",
      }),
    ]);
    const state = usePendingSelectionState({
      pendingItems: computed(() => updates.pending?.items ?? []),
      selectableLineNumbers: computed(() => [1, 2]),
      onSelectionChanged,
    });

    const item = updates.pending.items[0];
    expect(state.tagOverridesForLines([1])).toEqual([]);

    state.updateTagOverride(item, "bad tag");
    expect(state.selectedLineNumbers.value).toEqual([1]);
    expect(state.tagOverrideErrorForLines([1])).toContain("invalid new tag");

    state.updateTagOverride(item, "1.2");
    expect(state.tagOverridesForLines([1])).toEqual([{ line_no: 1, tag: "1.2" }]);
    expect(state.lineNumbersHaveTagUpdates([1, 2])).toBe(true);

    state.selectAllVisible();
    expect(state.selectedLineNumbers.value).toEqual([1, 2]);
    state.updateCheckedRowKeys([2, 2, "bad-key"]);
    expect(state.selectedLineNumbers.value).toEqual([2]);
    expect(onSelectionChanged).toHaveBeenCalled();
  });

  it("clears stale tag overrides when pending items reload", async () => {
    const updates = useUpdatesStore();
    updates.pending = pendingResponse([
      pendingItem({
        line_no: 1,
        image: "repo/app:1.0",
        desired_tag: "1.1",
      }),
    ]);
    const state = usePendingSelectionState({
      pendingItems: computed(() => updates.pending?.items ?? []),
      selectableLineNumbers: computed(() => [1]),
    });

    state.updateTagOverride(updates.pending.items[0], "1.2");
    expect(state.tagOverridesForLines([1])).toEqual([
      { line_no: 1, tag: "1.2" },
    ]);

    updates.pending = pendingResponse([
      pendingItem({
        line_no: 1,
        image: "repo/app:1.0",
        desired_tag: "1.1",
      }),
    ]);
    await flushPromises();
    expect(state.tagOverridesForLines([1])).toEqual([
      { line_no: 1, tag: "1.2" },
    ]);

    updates.pending = pendingResponse([
      pendingItem({
        line_no: 1,
        image: "repo/app:2.0",
        desired_tag: "2.1",
      }),
    ]);
    await flushPromises();

    expect(state.tagOverrideValue(updates.pending.items[0])).toBe("2.1");
    expect(state.tagOverridesForLines([1])).toEqual([]);
  });

  it("drops stale stack-scoped selections while preserving line-only fallback", async () => {
    const updates = useUpdatesStore();
    updates.pending = pendingResponse([
      pendingItem({ line_no: 1, image: "repo/shared:latest" }),
    ]);
    const availableSelections = ref([
      { line_no: 1, selection_id: "selection-active" },
      { line_no: 1, selection_id: "selection-backup" },
    ]);
    const state = usePendingSelectionState({
      pendingItems: computed(() => updates.pending?.items ?? []),
      selectableLineNumbers: computed(() => [1]),
      selectableSelections: computed(() => availableSelections.value),
      availableSelections: computed(() => availableSelections.value),
    });

    state.toggleSelection(availableSelections.value[0], true);
    expect(state.selectedSelections.value).toEqual([
      { line_no: 1, selection_id: "selection-active" },
    ]);

    availableSelections.value = [
      { line_no: 1, selection_id: "selection-backup" },
    ];
    await flushPromises();
    expect(state.selectedSelections.value).toEqual([]);

    availableSelections.value = [{ line_no: 1, selection_id: "" }];
    await flushPromises();
    state.toggleLine(1, true);
    expect(state.selectedSelections.value).toEqual([
      { line_no: 1, selection_id: "" },
    ]);
  });
});

describe("usePendingPlanReviewState", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("owns update intent labels and apply payloads", () => {
    const { state } = setupPendingPlanReview();
    const tagOverrides = [{ line_no: 1, tag: "1.2" }];

    state.setUpdateIntent({
      title: "Review media plan",
      contextLabel: "media",
      lineNumbers: [1],
      selections: [{ line_no: 1, selection_id: "selection-media-app" }],
      allowTagUpdates: true,
      tagOverrides,
      digestPinLabelRewriteApprovals: [],
      tagStreamDecisions: [],
      tagStreamLabelRewriteApprovals: [],
    });

    expect(state.planContextLabel.value).toBe("media");
    expect(state.preflightTitle.value).toBe("Review media plan");
    expect(
      state.applyPlanPayload({
        allowTagUpdates: false,
        tagOverrides: [],
      }),
    ).toEqual({
      allowTagUpdates: true,
      tagOverrides,
      digestPinLabelRewriteApprovals: [],
      tagStreamDecisions: [],
      tagStreamLabelRewriteApprovals: [],
    });

    state.clearUpdateIntent();

    expect(state.planContextLabel.value).toBe("selected updates");
    expect(
      state.applyPlanPayload({
        allowTagUpdates: false,
        tagOverrides: [],
      }),
    ).toEqual({
      allowTagUpdates: false,
      tagOverrides: [],
      digestPinLabelRewriteApprovals: [],
      tagStreamDecisions: [],
      tagStreamLabelRewriteApprovals: [],
    });
  });

  it("disables selected updates when reactive tag validation changes", () => {
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const updates = useUpdatesStore();
    const selectedLineNumbers = ref<number[]>([1]);
    const selectedSelections = ref<PlanSelectionRequest[]>([
      { line_no: 1, selection_id: "" },
    ]);
    const selectedSelectionKeySet = computed(
      () => new Set(selectedSelections.value.map(pendingSelectionKey)),
    );
    const tagValidationError = ref("");
    const state = usePendingPlanReviewState({
      pendingSourceLabel: computed(() => "images.todo"),
      selectedLineNumbers,
      selectedSelections,
      selectedSelectionKeySet,
      stackGroups: computed(() => updates.pending?.grouping.groups ?? []),
      tagOverrideErrorForLines: () => tagValidationError.value,
      unmatchedItems: computed(() => updates.pending?.grouping.unmatched ?? []),
    });

    expect(state.updateSelectedDisabled.value).toBe(false);

    tagValidationError.value = "repo/app:1.0 has an invalid new tag.";
    expect(state.updateSelectedDisabled.value).toBe(true);

    tagValidationError.value = "";
    updates.loading = true;
    expect(state.updateSelectedDisabled.value).toBe(true);
  });

  it("derives apply readiness states from the current plan preflight", () => {
    const { state, updates } = setupPendingPlanReview();

    expect(state.applyReadinessStatusLabel.value).toBe("");
    expect(state.applyReadinessStatusType.value).toBe("error");
    expect(state.applyVisible.value).toBe(false);

    updates.plan = planResponse();

    expect(state.applyReadinessStatusLabel.value).toBe("Ready");
    expect(state.applyReadinessStatusType.value).toBe("success");
    expect(state.applyReadinessSummary.value).toBe("Required resources are reachable.");
    expect(state.applyButtonLabel.value).toBe("Apply 1 update");

    updates.plan = planResponse({
      apply_preflight: warningApplyPreflight(
        "bind-mounts-safe",
        "One bind mount should be reviewed.",
      ),
    });

    expect(state.applyReadinessStatusLabel.value).toBe("Warnings");
    expect(state.applyReadinessStatusType.value).toBe("warning");
    expect(state.applyPreflightAttentionChecks.value).toHaveLength(1);
    expect(state.applyReadinessSummary.value).toBe(
      "1 warning to review before applying.",
    );

    updates.plan = planResponse({
      can_apply: false,
      apply_preflight: failedApplyPreflight(
        "mutations-enabled",
        "Set WUD_WEB_MUTATIONS_ENABLED=true.",
      ),
    });

    expect(state.applyReadinessStatusLabel.value).toBe("Blocked");
    expect(state.applyReadinessStatusType.value).toBe("error");
    expect(state.applyDisabled.value).toBe(true);
    expect(state.applyReadinessSummary.value).toBe(
      "1 failed check must be fixed before applying.",
    );
  });

  it("derives cleanup and removal state without exposing stale cleanup issues", () => {
    const { state, updates, selectedLineNumbers } = setupPendingPlanReview(false);
    const item = pendingGroupedItem({
      line_no: 1,
      image: "repo/old:latest",
      repo: "repo/old",
      services: [],
      diagnostic: {
        code: "compose-label-active-file-missing",
        message:
          "Container old was created from stack media, but docker-compose.yml is missing.",
        hint: "Restore an active Compose file or remove the stale pending line.",
        stack: "media",
        service: "old",
        compose_file: "docker-compose.yml",
        found_files: ["docker-compose.archive.yml"],
        details: {
          preflight_findings: [
            "Compose file missing",
            "Archived file found",
            "Compose file missing",
          ],
          possible_reasons: ["Stack moved", "Stack moved"],
          recommended_actions: [
            "Restore Compose file",
            "Remove stale line",
            "Restore Compose file",
          ],
        },
      },
    });
    selectedLineNumbers.value = [1];
    updates.plan = planResponse({
      can_apply: false,
      issues: [
        {
          severity: "error",
          code: "compose-label-active-file-missing",
          message: "No Compose service matched repo/old:latest.",
          line_no: 1,
          stack: "",
          service: "",
          hint: "",
          details: {},
        },
      ],
      cleanup: {
        cleanup_id: "cleanup-test",
        can_remove_unmatched: false,
        items: [
          {
            line_no: item.line_no,
            raw: item.raw,
            image: item.image,
            desired_tag: item.desired_tag,
            digest: item.digest,
            reason: "compose-label-active-file-missing",
            diagnostic: item.diagnostic,
          },
        ],
      },
    });

    expect(state.cleanupButtonLabel.value).toBe("Remove 1 unmatched entry");
    expect(state.cleanupDisabled.value).toBe(true);
    expect(state.cleanupDisabledMessage.value).toContain("Read-only mode is active");
    expect(state.removeSelectedDisabled.value).toBe(true);
    expect(state.removeSelectedDisabledMessage.value).toContain(
      "Read-only mode is active",
    );
    expect(state.removalButtonLabel.value).toBe("Remove 1 selected entry");
    expect(state.cleanupReviewSummary.value).toContain(
      "1 entry needs review: Compose file missing.",
    );
    expect(state.cleanupAssistantFindings.value).toEqual([
      "Compose file missing",
      "Archived file found",
    ]);
    expect(state.cleanupAssistantReasons.value).toEqual(["Stack moved"]);
    expect(state.cleanupAssistantActions.value).toEqual([
      "Restore Compose file",
      "Remove stale line",
    ]);
    expect(state.visiblePlanIssues.value).toEqual([]);
  });

  it("keeps global plan issues visible when cleanup hides line-level stale issues", () => {
    const { state, updates } = setupPendingPlanReview();

    updates.plan = planResponse({
      issues: [
        {
          severity: "error",
          code: "compose-label-active-file-missing",
          message: "Global compose discovery issue.",
          line_no: null,
          stack: "",
          service: "",
          hint: "",
          details: {},
        },
        {
          severity: "error",
          code: "compose-label-active-file-missing",
          message: "Line-level stale entry.",
          line_no: 1,
          stack: "",
          service: "",
          hint: "",
          details: {},
        },
      ],
      cleanup: {
        cleanup_id: "cleanup-test",
        can_remove_unmatched: false,
        items: [
          {
            line_no: 1,
            raw: "repo/old:latest",
            image: "repo/old:latest",
            desired_tag: "",
            digest: "",
            reason: "compose-label-active-file-missing",
            diagnostic: null,
          },
        ],
      },
    });

    expect(state.visiblePlanIssues.value).toEqual([
      expect.objectContaining({
        line_no: null,
        message: "Global compose discovery issue.",
      }),
    ]);
  });

  it("keeps unparseable label approval blockers visible", () => {
    const { state, updates } = setupPendingPlanReview();
    const malformedIssues = [
      {
        severity: "error",
        code: "compose-tag-stream-label-rewrite-unapproved",
        message: "Malformed stream approval issue.",
        line_no: 1,
        stack: "media",
        service: "app",
        hint: "",
        details: {},
      },
      {
        severity: "error",
        code: "compose-digest-pin-label-rewrite-unapproved",
        message: "Malformed digest approval issue.",
        line_no: 1,
        stack: "media",
        service: "app",
        hint: "",
        details: {},
      },
    ];
    updates.plan = planResponse({ issues: malformedIssues });

    expect(state.tagStreamLabelApprovalIssues.value).toEqual([]);
    expect(state.digestPinLabelApprovalIssues.value).toEqual([]);
    expect(state.visiblePlanIssues.value).toEqual(malformedIssues);
  });

  it("replans with digest-pin label rewrite approval before marking it approved", async () => {
    const { state, updates } = setupPendingPlanReview();
    const issue = digestPinApprovalIssue();
    const createPlan = vi.spyOn(updates, "createPlan").mockResolvedValue(undefined);

    expect(state.digestPinLabelApprovalApproved(issue)).toBe(false);
    expect(await state.approveDigestPinLabelRewrite(issue)).toBe(false);

    state.setUpdateIntent({
      title: "Review media plan",
      contextLabel: "media",
      lineNumbers: [1],
      selections: [{ line_no: 1, selection_id: "selection-media-app" }],
      allowTagUpdates: true,
      tagOverrides: [{ line_no: 1, tag: "1.1" }],
      digestPinLabelRewriteApprovals: [],
    });

    await expect(state.approveDigestPinLabelRewrite(issue)).resolves.toBe(true);
    expect(createPlan).toHaveBeenCalledWith(
      [1],
      true,
      [{ line_no: 1, tag: "1.1" }],
      [
        {
          stack: "media",
          service: "app",
          label_key: "org.opencontainers.image.version",
          current_label_value: "1.0",
          planned_tag: "1.1",
          proposed_label_value: "1.1",
        },
      ],
      [{ line_no: 1, selection_id: "selection-media-app" }],
    );
    expect(state.digestPinLabelApprovalApproved(issue)).toBe(true);
  });

  it("replans stream decisions and exact label approvals", async () => {
    const { state, updates } = setupPendingPlanReview();
    const createPlan = vi.spyOn(updates, "createPlan").mockResolvedValue(undefined);
    const decisionIssue = {
      severity: "error",
      code: "tag-stream-change",
      message: "Choose a stream.",
      line_no: 1,
      stack: "media",
      service: "app",
      hint: "",
      details: {},
    };
    const labelIssue = {
      ...decisionIssue,
      code: "compose-tag-stream-label-rewrite-unapproved",
      details: {
        stack_directory: "/docker/media",
        compose_file: "docker-compose.yml",
        label_key: "wud.tag.include",
        current_label_value: "^stable-.+$",
        selected_tag: "1.2.0-distroless",
        proposed_label_value: String.raw`^\d+\.\d+\.\d+-distroless$$`,
      },
    };
    const siblingLabelIssue = {
      ...labelIssue,
      details: { ...labelIssue.details, compose_file: "compose.yml" },
    };
    expect(tagStreamLabelApprovalIssueKey(labelIssue)).not.toBe(
      tagStreamLabelApprovalIssueKey(siblingLabelIssue),
    );
    state.setUpdateIntent({
      title: "Review media plan",
      contextLabel: "media",
      lineNumbers: [1],
      selections: [{ line_no: 1, selection_id: "selection-media-app" }],
      allowTagUpdates: true,
      tagOverrides: [],
      digestPinLabelRewriteApprovals: [],
      tagStreamDecisions: [],
      tagStreamLabelRewriteApprovals: [],
    });

    await expect(state.chooseTagStream(decisionIssue, "preserve")).resolves.toBe(true);
    expect(createPlan).toHaveBeenNthCalledWith(
      1,
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-media-app" }],
      [{ line_no: 1, decision: "preserve" }],
      [],
    );
    expect(state.tagStreamDecisionSelected(decisionIssue, "preserve")).toBe(true);
    expect(state.tagStreamDecisionIssues.value).toEqual([decisionIssue]);
    const retainedDecisionIssue = state.tagStreamDecisionIssues.value[0];
    expect(retainedDecisionIssue).toBeDefined();

    await expect(
      state.chooseTagStream(retainedDecisionIssue!, "switch"),
    ).resolves.toBe(true);
    expect(createPlan).toHaveBeenNthCalledWith(
      2,
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-media-app" }],
      [{ line_no: 1, decision: "switch" }],
      [],
    );
    expect(state.tagStreamDecisionSelected(decisionIssue, "preserve")).toBe(false);
    expect(state.tagStreamDecisionSelected(decisionIssue, "switch")).toBe(true);

    await expect(
      state.chooseTagStream(retainedDecisionIssue!, "preserve"),
    ).resolves.toBe(true);
    expect(createPlan).toHaveBeenNthCalledWith(
      3,
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-media-app" }],
      [{ line_no: 1, decision: "preserve" }],
      [],
    );

    await expect(state.approveTagStreamLabelRewrite(labelIssue)).resolves.toBe(true);
    const approval = {
      line_no: 1,
      stack: "media",
      stack_directory: "/docker/media",
      compose_file: "docker-compose.yml",
      service: "app",
      label_key: "wud.tag.include",
      current_label_value: "^stable-.+$",
      selected_tag: "1.2.0-distroless",
      proposed_label_value: String.raw`^\d+\.\d+\.\d+-distroless$$`,
    };
    expect(createPlan).toHaveBeenNthCalledWith(
      4,
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-media-app" }],
      [{ line_no: 1, decision: "preserve" }],
      [approval],
    );
    expect(state.tagStreamLabelApprovalApproved(labelIssue)).toBe(true);
    expect(
      state.applyPlanPayload({ allowTagUpdates: false, tagOverrides: [] }),
    ).toMatchObject({
      tagStreamDecisions: [{ line_no: 1, decision: "preserve" }],
      tagStreamLabelRewriteApprovals: [approval],
    });

    await expect(
      state.chooseTagStream(retainedDecisionIssue!, "switch"),
    ).resolves.toBe(true);
    expect(createPlan).toHaveBeenNthCalledWith(
      5,
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-media-app" }],
      [{ line_no: 1, decision: "switch" }],
      [],
    );
    expect(state.tagStreamLabelApprovalApproved(labelIssue)).toBe(false);
  });

  it("surfaces digest-pin notice only when the plan contains digest-pin rewrites", () => {
    const { state, updates } = setupPendingPlanReview();

    expect(state.preflightDigestPinNotice.value).toBe("");

    updates.plan = planResponse({
      digest_pin_updates: true,
      stacks: [
        {
          ...planResponse().stacks[0],
          digest_pin_updates: [
            {
              source_image: "repo/app:1.0",
              resolved_tag: "1.1",
              planned_digest: "sha256:abc",
              final_image: "repo/app@sha256:abc",
              watch_tag: "1.1",
              marker: "sha256=abc",
              label_key: "org.opencontainers.image.version",
              label_value: "1.1",
              services: ["app"],
              label_rewrites: [],
            },
          ],
        },
      ],
    });

    expect(state.preflightDigestPinNotice.value).toBe(
      "1 digest-pin rewrite will pin approved tag updates after pull verification.",
    );
  });

  it("surfaces digest-unpin notice without digest-pin notice", () => {
    const { state, updates } = setupPendingPlanReview();

    updates.plan = planResponse({
      digest_pin_updates: false,
      stacks: [
        {
          ...planResponse().stacks[0],
          digest_unpin_updates: [
            {
              source_image: "repo/app@sha256:old",
              resolved_tag: "latest",
              tag_image: "repo/app:latest",
              current_digest: "sha256:old",
              target_digest: "sha256:new",
              watch_tag: "latest",
              marker: "wudup.resolved-tag=latest",
              label_key: "wud.tag.include",
              label_value: "^latest$",
              services: ["app"],
            },
          ],
          lines: [
            {
              ...planResponse().stacks[0].lines[0],
              action: "digest-unpin",
              compose_image: "repo/app@sha256:old",
              target_image: "repo/app:latest",
            },
          ],
        },
      ],
    });

    expect(state.preflightDigestPinNotice.value).toBe("");
    expect(state.preflightDigestUnpinNotice.value).toBe(
      "1 digest unpin migration will rewrite pinned Compose images back to their watched tag before pulling.",
    );
    expect(state.planDigestUnpinUpdates.value).toHaveLength(1);
  });
});

describe("usePendingApplyJob", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("creates apply snapshots from the current plan store state", () => {
    const { state, updates } = setupPendingApplyJob();
    updates.plan = planResponse();

    expect(state.createApplyJobSnapshot()).toMatchObject({
      contextLabel: "media",
      serviceCount: 1,
      stackCount: 1,
      sourceFile: "/out/images.todo",
      lines: [
        {
          lineNo: 1,
          serviceLabel: "app",
          tagRewriteLabel: "repo/app:1.0 -> repo/app:1.1",
          digestPinLabel: "",
          composeImage: "repo/app:1.0",
          targetImage: "repo/app:1.1",
        },
      ],
    });
  });

  it("derives apply job labels, logs, and failed progress precedence", () => {
    const { state, updates } = setupPendingApplyJob();
    const failedPull: ApplyJobProgressEvent = {
      job_id: "job-test",
      phase: "pull",
      status: "failure",
      message: "[media] Pull failed.",
      created_at: "2026-05-28T12:00:02+00:00",
      stack: "media",
      services: ["calibre"],
      line_numbers: [1],
    };
    const laterPullSuccess: ApplyJobProgressEvent = {
      job_id: "job-test",
      phase: "pull",
      status: "success",
      message: "[infra] Images pulled and verified.",
      created_at: "2026-05-28T12:00:03+00:00",
      stack: "infra",
      services: ["watchtower"],
      line_numbers: [2],
    };

    updates.setApplyJob(applyJobResponse({ status: "queued" }));

    expect(state.applyJobActive.value).toBe(true);
    expect(state.applyJobTitle.value).toBe("Applying 1 update");
    expect(state.applyJobNowTitle.value).toBe("Queued to start");
    expect(state.applyJobLatestLogMessage.value).toBe("Waiting for log output.");
    expect(state.applyJobLiveLogVisible.value).toBe(true);

    updates.setApplyJobLog(
      applyJobLogResponse({
        content: "first log line\nsecond log line\n",
      }),
    );

    expect(state.applyJobLatestLogMessage.value).toBe("second log line");

    updates.setApplyJob(
      applyJobResponse({
        status: "running",
        progress: [failedPull, laterPullSuccess],
      }),
    );

    expect(state.applyJobProgressSummary.value).toBe("Pull images failed");
    expect(state.applyJobProgressSteps.value.find((step) => step.key === "pull")).toMatchObject({
      status: "failure",
      message: "[media] Pull failed.",
      detail: "media / calibre / lines 1",
    });

    updates.setApplyJob(
      applyJobResponse({
        status: "failure",
        error: "updater exited with status 1",
        progress: [failedPull, laterPullSuccess],
      }),
    );

    expect(state.applyJobTitle.value).toBe("Apply failed");
    expect(state.applyJobNowTitle.value).toBe("Failed: Pull images");
    expect(state.applyJobNowMessage.value).toBe("updater exited with status 1");
  });

  it("shows multi-stack apply progress without marking future stacks complete", () => {
    const { state, updates } = setupPendingApplyJob();
    const basePlan = planResponse();
    const baseStack = basePlan.stacks[0];
    const stackFor = (name: string, service: string, lineNo: number) => ({
      ...baseStack,
      name,
      directory: `/docker/${name}`,
      project_directory: `/docker/${name}`,
      services_label: service,
      services: [service],
      pull_services: [service],
      stop_services: [service],
      actions: [],
      lines: [
        {
          ...baseStack.lines[0],
          line_no: lineNo,
          raw: `repo/${service}:1.0`,
          image: `repo/${service}:1.0`,
          resolved_image: `repo/${service}:1.0`,
          compose_image: `repo/${service}:1.0`,
          target_image: `repo/${service}:1.1`,
          service,
        },
      ],
    });
    updates.plan = planResponse({
      selected_line_numbers: [1, 2, 3],
      summary: {
        ...basePlan.summary,
        target_count: 3,
        matched_target_count: 3,
        stack_count: 3,
        service_count: 3,
      },
      stacks: [
        stackFor("media", "sonarr", 1),
        stackFor("infra", "redis", 2),
        stackFor("apps", "api", 3),
      ],
    });
    state.applyJobSnapshot.value = state.createApplyJobSnapshot();

    updates.setApplyJob(
      applyJobResponse({
        status: "running",
        selected_line_numbers: [1, 2, 3],
        progress: [
          {
            job_id: "job-test",
            phase: "health",
            status: "success",
            message: "[media] Health checks passed.",
            created_at: "2026-05-28T12:00:05+00:00",
            stack: "media",
            services: ["sonarr"],
            line_numbers: [1],
          },
          {
            job_id: "job-test",
            phase: "pull",
            status: "running",
            message: "[infra] Pulling selected image updates.",
            created_at: "2026-05-28T12:00:06+00:00",
            stack: "infra",
            services: ["redis"],
            line_numbers: [2],
          },
        ],
      }),
    );

    expect(
      state.applyJobProgressSteps.value.map((step) => ({
        label: step.label,
        status: step.status,
        statusLabel: step.statusLabel,
      })),
    ).toEqual([
      { label: "media", status: "success", statusLabel: "Complete" },
      { label: "infra", status: "running", statusLabel: "Running" },
      { label: "apps", status: "pending", statusLabel: "Queued" },
    ]);
    expect(state.applyJobProgressSummary.value).toBe("infra");
    expect(state.applyJobNowTitle.value).toBe("Running: infra");

    updates.setApplyJob(
      applyJobResponse({
        status: "running",
        selected_line_numbers: [1, 2, 3],
        progress: [
          {
            job_id: "job-test",
            phase: "health",
            status: "success",
            message: "[media] Health checks passed.",
            created_at: "2026-05-28T12:00:05+00:00",
            stack: "media",
            services: ["sonarr"],
            line_numbers: [1],
          },
          {
            job_id: "job-test",
            phase: "health",
            status: "success",
            message: "[infra] Health checks passed.",
            created_at: "2026-05-28T12:00:06+00:00",
            stack: "infra",
            services: ["redis"],
            line_numbers: [2],
          },
          {
            job_id: "job-test",
            phase: "health",
            status: "success",
            message: "[apps] Health checks passed.",
            created_at: "2026-05-28T12:00:07+00:00",
            stack: "apps",
            services: ["api"],
            line_numbers: [3],
          },
          {
            job_id: "job-test",
            phase: "completion",
            status: "success",
            message: "Updater completed successfully.",
            created_at: "2026-05-28T12:00:08+00:00",
            stack: "",
            services: [],
            line_numbers: [],
          },
        ],
      }),
    );

    expect(state.applyJobProgressSummary.value).toBe("3/3 stacks complete");
    expect(state.applyJobNowTitle.value).toBe("Completed: apps");

    updates.setApplyJob(
      applyJobResponse({
        status: "success",
        selected_line_numbers: [1, 2, 3],
        progress: updates.applyJob?.progress ?? [],
      }),
    );

    expect(
      state.applyJobProgressSteps.value.map((step) => ({
        label: step.label,
        status: step.status,
        statusLabel: step.statusLabel,
      })),
    ).toEqual([
      { label: "media", status: "success", statusLabel: "Complete" },
      { label: "infra", status: "success", statusLabel: "Complete" },
      { label: "apps", status: "success", statusLabel: "Complete" },
    ]);
    expect(
      state.applyJobProgressSteps.value.map((step) => ({
        message: step.message,
        detail: step.detail,
      })),
    ).toEqual([
      { message: "Stack update completed.", detail: "" },
      { message: "Stack update completed.", detail: "" },
      { message: "Stack update completed.", detail: "" },
    ]);

    updates.setApplyJob(
      applyJobResponse({
        status: "failure",
        selected_line_numbers: [1, 2, 3],
        progress: [
          {
            job_id: "job-test",
            phase: "pull",
            status: "failure",
            message: "[media] Pull failed.",
            created_at: "2026-05-28T12:00:05+00:00",
            stack: "media",
            services: ["sonarr"],
            line_numbers: [1],
          },
        ],
      }),
    );

    expect(
      state.applyJobProgressSteps.value.map((step) => ({
        label: step.label,
        status: step.status,
        statusLabel: step.statusLabel,
        message: step.message,
      })),
    ).toEqual([
      {
        label: "media",
        status: "failure",
        statusLabel: "Failed",
        message: "[media] Pull failed.",
      },
      {
        label: "infra",
        status: "skipped",
        statusLabel: "Not started",
        message: "Job failed before infra started.",
      },
      {
        label: "apps",
        status: "skipped",
        statusLabel: "Not started",
        message: "Job failed before apps started.",
      },
    ]);
  });

  it("keeps fallback verification stable when a job response omits progress events", () => {
    const { state, updates } = setupPendingApplyJob();
    updates.plan = planResponse();
    state.applyJobSnapshot.value = state.createApplyJobSnapshot();
    const job = applyJobResponse({ status: "success" });
    (job as unknown as { progress: unknown }).progress = {};

    updates.setApplyJob(job);

    expect(state.applyJobVerification.value).toMatchObject({
      status: "needs_review",
      total_count: 1,
      needs_review_count: 1,
      items: [
        {
          line_no: 1,
          image_status: "unknown",
          container_status: "unknown",
          health_status: "unknown",
          wud_status: "unknown",
          follow_up_needed: true,
        },
      ],
    });
  });

  it("handles stream errors and duplicate progress events", async () => {
    const { state, updates } = setupPendingApplyJob();
    const stream = mockApplyJobStream();
    const progress: ApplyJobProgressEvent = {
      job_id: "job-test",
      phase: "pull",
      status: "running",
      message: "[media] Pulling selected image updates.",
      created_at: "2026-05-28T12:00:01+00:00",
      stack: "media",
      services: ["calibre"],
      line_numbers: [1],
    };
    updates.setApplyJob(applyJobResponse({ status: "running" }));

    state.subscribeApplyJob("job-test");
    stream.emitLogData("{");
    await flushPromises();

    expect(updates.error).toBe("Job log stream returned invalid data.");
    expect(stream.close).not.toHaveBeenCalled();

    const progressData = JSON.stringify(progress);
    stream.emitProgressData(progressData);
    stream.emitProgressData(progressData);

    expect(updates.applyJob?.progress).toEqual([progress]);

    stream.emitJobData("{");
    await flushPromises();

    expect(updates.error).toBe("Job status stream returned invalid data.");
    expect(stream.close).toHaveBeenCalledTimes(1);
  });

  it("ignores invalid or mismatched progress stream events without closing the stream", () => {
    const { state, updates } = setupPendingApplyJob();
    const stream = mockApplyJobStream();
    const progress: ApplyJobProgressEvent = {
      job_id: "other-job",
      phase: "pull",
      status: "running",
      message: "[media] Pulling selected image updates.",
      created_at: "2026-05-28T12:00:01+00:00",
      stack: "media",
      services: ["calibre"],
      line_numbers: [1],
    };
    updates.setApplyJob(applyJobResponse({ job_id: "job-test", status: "running" }));

    state.subscribeApplyJob("job-test");
    stream.emitProgressData("{");

    expect(updates.error).toBe("Job progress stream returned invalid data.");
    expect(stream.close).not.toHaveBeenCalled();

    stream.emitProgressData(JSON.stringify(progress));
    expect(updates.applyJob?.progress).toEqual([]);
  });

  it("closes and refreshes after a terminal job stream event", async () => {
    const { state, updates, runs, loadPendingAndReleaseNotes } =
      setupPendingApplyJob();
    const stream = mockApplyJobStream();
    const loadApplyJobLogFromRun = vi
      .spyOn(updates, "loadApplyJobLogFromRun")
      .mockResolvedValue(applyJobLogResponse());
    const loadRuns = vi.spyOn(runs, "loadRuns").mockResolvedValue(undefined);

    state.subscribeApplyJob("job-test");
    stream.emitJobData(
      JSON.stringify(
        applyJobResponse({
          status: "success",
          run_id: 42,
        }),
      ),
    );
    await flushPromises();

    expect(stream.close).toHaveBeenCalledTimes(1);
    expect(loadApplyJobLogFromRun).toHaveBeenCalledWith(
      expect.objectContaining({ job_id: "job-test", run_id: 42 }),
    );
    expect(loadPendingAndReleaseNotes).toHaveBeenCalledTimes(1);
    expect(loadRuns).toHaveBeenCalledTimes(1);
  });

  it("derives the latest log message from apply job log content", () => {
    const { state, updates } = setupPendingApplyJob();

    // Setup an active job so fallback message is "Waiting for log output."
    updates.setApplyJob(applyJobResponse({ status: "running" }));

    updates.setApplyJobLog(applyJobLogResponse({ content: "" }));
    expect(state.applyJobLatestLogMessage.value).toBe("Waiting for log output.");

    updates.setApplyJobLog(applyJobLogResponse({ content: "   \n\t\n" }));
    expect(state.applyJobLatestLogMessage.value).toBe("Waiting for log output.");

    updates.setApplyJobLog(applyJobLogResponse({ content: "single line content" }));
    expect(state.applyJobLatestLogMessage.value).toBe("single line content");

    updates.setApplyJobLog(applyJobLogResponse({ content: "first\nsecond\n\n\n" }));
    expect(state.applyJobLatestLogMessage.value).toBe("second");
  });
});
