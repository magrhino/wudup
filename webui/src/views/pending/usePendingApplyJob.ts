import {
  computed,
  getCurrentInstance,
  nextTick,
  onUnmounted,
  ref,
  watch,
  type Ref,
} from "vue";

import {
  webApi,
  type ApplyJobLogResponse,
  type ApplyJobProgressEvent,
  type ApplyJobResponse,
  type RunVerificationContainerStatus,
  type RunVerificationHealthStatus,
  type RunVerificationImageStatus,
  type RunVerificationItem,
  type RunVerificationSummary,
  type RunVerificationWudStatus,
} from "../../api/client";
import { prefersReducedMotion } from "../../responsive";
import { useRunsStore } from "../../stores/runs";
import { useUpdatesStore } from "../../stores/updates";
import { runInBackground } from "../../utils/promises";
import {
  pendingPlanContextLabel,
  planLineDigestPinLabel,
  planLinesFromPlan,
  planLineServiceLabel,
  planLineTagRewriteLabel,
  pluralize,
} from "./utils";

export type ApplyJobSnapshotLine = {
  key: string;
  lineNo: number | null;
  stackName: string;
  scopeLabel?: string;
  serviceLabel: string;
  tagRewriteLabel: string;
  digestPinLabel: string;
  composeImage: string;
  targetImage: string;
};

export type ApplyJobPlanSnapshot = {
  contextLabel: string;
  serviceCount: number;
  stackCount: number;
  sourceFile: string;
  lines: ApplyJobSnapshotLine[];
};

type VerificationSnapshotLine = ApplyJobSnapshotLine & { lineNo: number };

export type ApplyJobProgressPhase = {
  key: string;
  label: string;
  waitingMessage: string;
};

export type ApplyJobProgressStep = ApplyJobProgressPhase & {
  status: "pending" | ApplyJobProgressEvent["status"];
  statusLabel: string;
  message: string;
  detail: string;
  event: ApplyJobProgressEvent | null;
};

export type PendingApplyJobPanelRef = {
  focusPanel: (behavior: ScrollBehavior) => void;
  logElement: () => HTMLElement | null;
};

export type UsePendingApplyJobOptions = {
  applyJobPanelRef: Ref<PendingApplyJobPanelRef | null>;
  loadPendingAndReleaseNotes?: () => Promise<void>;
  refreshAfterTerminalJob?: () => Promise<void>;
  progressPhases?: ApplyJobProgressPhase[];
  updateNoun?: string;
  completeNowTitle?: string;
  successStatusMessage?: (updateLabel: string) => string;
};

export const terminalJobStatuses = new Set<ApplyJobResponse["status"]>([
  "success",
  "failure",
]);

const applyJobProgressPhases: ApplyJobProgressPhase[] = [
  {
    key: "preflight",
    label: "Preflight",
    waitingMessage: "Waiting to validate the pending entries and Compose state.",
  },
  {
    key: "pull",
    label: "Pull images",
    waitingMessage: "Waiting for image pulls to begin.",
  },
  {
    key: "recreate",
    label: "Recreate",
    waitingMessage: "Waiting to recreate selected services.",
  },
  {
    key: "health",
    label: "Health wait",
    waitingMessage: "Waiting for container health checks.",
  },
  {
    key: "cleanup",
    label: "Cleanup",
    waitingMessage: "Waiting to reconcile the pending entries.",
  },
  {
    key: "completion",
    label: "Complete",
    waitingMessage: "Waiting for the updater result.",
  },
];

export function usePendingApplyJob(options: UsePendingApplyJobOptions) {
  const updates = useUpdatesStore();
  const runs = useRunsStore();
  const jobEventSource = ref<EventSource | null>(null);
  const applyJobLiveLogExpanded = ref(true);
  const applyJobRunLogFallbackRunId = ref<number | null>(null);
  const applyJobSnapshot = ref<ApplyJobPlanSnapshot | null>(null);
  const planContextLabel = computed(() => pendingPlanContextLabel(updates.plan));
  const planLines = computed(() => planLinesFromPlan(updates.plan));
  const progressPhases = options.progressPhases ?? applyJobProgressPhases;
  const updateNoun = options.updateNoun ?? "update";

  const applyJobAlertType = computed(() => {
    if (updates.applyJob?.status === "failure") {
      return "error";
    }
    if (updates.applyJob?.status === "success") {
      return "success";
    }
    return "info";
  });
  const applyJobActive = computed(() =>
    Boolean(updates.applyJob && !terminalJobStatuses.has(updates.applyJob.status)),
  );
  const applyJobSucceeded = computed(() => updates.applyJob?.status === "success");
  const applyJobUpdateCount = computed(
    () =>
      updates.applyJob?.selected_line_numbers.length ||
      applyJobSnapshot.value?.lines.length ||
      0,
  );
  const applyJobUpdateLabel = computed(() =>
    pluralize(applyJobUpdateCount.value, updateNoun),
  );
  const applyJobTitle = computed(() => {
    if (!updates.applyJob) {
      return "";
    }
    if (updates.applyJob.status === "queued" || updates.applyJob.status === "running") {
      return `Applying ${applyJobUpdateLabel.value}`;
    }
    if (updates.applyJob.status === "success") {
      return "Apply complete";
    }
    if (updates.applyJob.status === "failure") {
      return "Apply failed";
    }
    return "Apply job";
  });
  const applyJobStatusMessage = computed(() => {
    if (!updates.applyJob) {
      return "";
    }
    if (updates.applyJob.status === "queued") {
      return "Waiting for the updater job to start.";
    }
    if (updates.applyJob.status === "running") {
      return "Updater command is running.";
    }
    if (updates.applyJob.status === "success") {
      return options.successStatusMessage
        ? options.successStatusMessage(applyJobUpdateLabel.value)
        : `${applyJobUpdateLabel.value} finished. Pending updates and run history were refreshed.`;
    }
    if (updates.applyJob.error) {
      return updates.applyJob.error;
    }
    return "Updater stopped before completing the selected updates.";
  });
  const applyJobStartedLabel = computed(() => {
    if (!updates.applyJob) {
      return "";
    }
    return updates.applyJob.started_at || "Queued";
  });
  const applyJobSnapshotLines = computed(() => applyJobSnapshot.value?.lines ?? []);
  const applyJobImpactLabel = computed(() => {
    if (!applyJobSnapshot.value) {
      return "";
    }
    const serviceCount =
      applyJobSnapshot.value.serviceCount || applyJobSnapshotLines.value.length;
    const stackCount = applyJobSnapshot.value.stackCount;
    if (stackCount > 1) {
      return `${pluralize(serviceCount, "service")} across ${pluralize(stackCount, "stack")}`;
    }
    return `${pluralize(serviceCount, "service")} in ${applyJobSnapshot.value.contextLabel}`;
  });
  const applyJobLogText = computed(() => updates.applyJobLog?.content ?? "");
  const applyJobLogTitle = computed(
    () => updates.applyJobLog?.log_file || updates.applyJob?.log_file || "Live log",
  );
  const applyJobLiveLogVisible = computed(
    () => applyJobActive.value || applyJobLiveLogExpanded.value,
  );
  const applyJobLiveLogToggleLabel = computed(() =>
    applyJobLiveLogExpanded.value ? "Hide live log output" : "Show live log output",
  );
  const applyJobLatestLogLine = computed(() =>
    latestNonEmptyLogLine(applyJobLogText.value),
  );
  const applyJobLatestLogMessage = computed(() => {
    if (applyJobLatestLogLine.value) {
      return applyJobLatestLogLine.value;
    }
    return applyJobActive.value ? "Waiting for log output." : "No log output captured.";
  });
  const applyJobLogEmptyMessage = computed(() =>
    applyJobActive.value ? "Waiting for log output." : "No live log was captured.",
  );
  const applyJobLogWaiting = computed(() => {
    const log = updates.applyJobLog;
    if (!log || log.error) {
      return !log;
    }
    return !log.exists && !log.content;
  });
  const applyJobRunDetail = computed(() => {
    const runId = updates.applyJob?.run_id;
    return runId ? runs.runDetails[runId] ?? null : null;
  });
  const displayApplyJobProgressByPhase = computed(() => {
    const displayEvents = new Map<string, ApplyJobProgressEvent>();
    for (const event of applyJobProgressEvents(updates.applyJob)) {
      displayEvents.set(
        event.phase,
        displayProgressEvent(displayEvents.get(event.phase) ?? null, event),
      );
    }
    return displayEvents;
  });
  const applyJobStackNames = computed(() =>
    stackNamesFromSnapshot(applyJobSnapshot.value),
  );
  const applyJobStackProgressEvents = computed(() =>
    applyJobProgressEvents(updates.applyJob).filter((event) => event.stack),
  );
  const applyJobProgressMode = computed<"phase" | "stack">(() =>
    applyJobStackNames.value.length > 1 && applyJobStackProgressEvents.value.length
      ? "stack"
      : "phase",
  );
  const applyJobProgressSteps = computed<ApplyJobProgressStep[]>(() =>
    applyJobProgressMode.value === "stack"
      ? stackProgressSteps(
          applyJobStackNames.value,
          applyJobStackProgressEvents.value,
          updates.applyJob?.status ?? null,
          progressPhases,
        )
      : progressPhases.map((phase) => {
          const event = displayApplyJobProgressByPhase.value.get(phase.key) ?? null;
          const status = event?.status ?? "pending";
          return {
            ...phase,
            status,
            statusLabel: progressStatusLabel(status),
            message: event?.message || phase.waitingMessage,
            detail: progressEventDetail(event),
            event,
          };
        }),
  );
  const applyJobProgressSummary = computed(() => {
    const progress = applyJobProgressEvents(updates.applyJob);
    if (!progress.length) {
      return applyJobActive.value ? "Starting" : "No progress events";
    }
    const failed = applyJobProgressSteps.value.find(
      (step) => step.status === "failure",
    );
    if (failed) {
      return `${failed.label} failed`;
    }
    const running = applyJobProgressSteps.value.find(
      (step) => step.status === "running",
    );
    if (running) {
      return running.label;
    }
    if (applyJobProgressMode.value === "stack") {
      const completeCount = applyJobProgressSteps.value.filter(
        (step) => step.status === "success",
      ).length;
      if (completeCount) {
        return `${completeCount}/${applyJobStackNames.value.length} stacks complete`;
      }
    }
    const complete = displayApplyJobProgressByPhase.value.get("completion");
    if (complete?.status === "success") {
      return "Complete";
    }
    const lastProgress = progress.at(-1)!;
    const lastPhase = applyJobProgressSteps.value.find(
      (step) => step.key === lastProgress.phase,
    );
    return lastPhase ? lastPhase.label : applyJobUpdateLabel.value;
  });
  const applyJobCurrentStep = computed<ApplyJobProgressStep | null>(() => {
    const failed = applyJobProgressSteps.value.find(
      (step) => step.status === "failure",
    );
    if (failed) {
      return failed;
    }
    const running = applyJobProgressSteps.value.find(
      (step) => step.status === "running",
    );
    if (running) {
      return running;
    }
    const lastStackStep =
      applyJobProgressMode.value === "stack"
        ? [...applyJobProgressSteps.value]
            .reverse()
            .find((step) => step.status !== "pending") ?? null
        : null;
    if (lastStackStep) {
      const completePhase = stackCompletePhase(progressPhases);
      const completeStacks = new Set(
        applyJobStackProgressEvents.value
          .filter(
            (event) =>
              event.phase === completePhase &&
              (event.status === "success" || event.status === "skipped"),
          )
          .map((event) => event.stack),
      );
      if (
        applyJobStackNames.value.every((stackName) => completeStacks.has(stackName))
      ) {
        return lastStackStep;
      }
    }
    const completion = displayApplyJobProgressByPhase.value.get("completion");
    if (completion?.status === "success") {
      return (
        applyJobProgressSteps.value.find((step) => step.key === "completion") ??
        lastStackStep
      );
    }
    return [...applyJobProgressSteps.value].reverse().find((step) => step.event) ?? null;
  });
  const applyJobVerification = computed<RunVerificationSummary>(() =>
    applyJobRunDetail.value?.verification ??
    fallbackVerification(updates.applyJob, applyJobSnapshot.value),
  );
  const applyJobNowTitle = computed(() => {
    if (!updates.applyJob) {
      return "";
    }
    const step = applyJobCurrentStep.value;
    if (updates.applyJob.status === "failure") {
      return step ? `Failed: ${step.label}` : "Apply failed";
    }
    if (updates.applyJob.status === "success") {
      return options.completeNowTitle ?? "Update complete";
    }
    if (step?.status === "running") {
      return `Running: ${step.label}`;
    }
    if (step?.status === "success") {
      return `Completed: ${step.label}`;
    }
    if (updates.applyJob.status === "queued") {
      return "Queued to start";
    }
    return "Starting updater";
  });
  const applyJobNowMessage = computed(() => {
    if (!updates.applyJob) {
      return "";
    }
    if (updates.applyJob.status === "failure" && updates.applyJob.error) {
      return updates.applyJob.error;
    }
    return applyJobCurrentStep.value?.message || applyJobStatusMessage.value;
  });
  const applyJobNowDetail = computed(
    () => applyJobCurrentStep.value?.detail || applyJobImpactLabel.value,
  );
  const applyJobNowDescriptionIds = computed(() =>
    applyJobNowDetail.value
      ? "apply-job-now-message apply-job-now-detail"
      : "apply-job-now-message",
  );
  const applyJobNowStatusLabel = computed(() => {
    if (updates.applyJob?.status === "success") {
      return "Complete";
    }
    if (updates.applyJob?.status === "failure") {
      return "Failed";
    }
    return applyJobProgressSummary.value;
  });
  const applyJobPanelStatusLabel = computed(() => {
    if (updates.applyJob?.status === "queued") {
      return "Queued";
    }
    if (updates.applyJob?.status === "running") {
      return "Running";
    }
    if (updates.applyJob?.status === "success") {
      return "Complete";
    }
    if (updates.applyJob?.status === "failure") {
      return "Failed";
    }
    return "Job";
  });

  function subscribeApplyJob(jobId: string): void {
    closeJobStream();
    const source = webApi.openJobStream(jobId);
    jobEventSource.value = source;
    source.addEventListener("job", (event) => {
      runInBackground(handleJobEvent(event as MessageEvent<string>));
    });
    source.addEventListener("progress", (event) => {
      handleJobProgressEvent(event as MessageEvent<string>);
    });
    source.addEventListener("log", (event) => {
      runInBackground(handleJobLogEvent(event as MessageEvent<string>));
    });
    source.onerror = () => {
      if (updates.applyJob && terminalJobStatuses.has(updates.applyJob.status)) {
        closeJobStream();
        return;
      }
      runInBackground(recoverOrRefreshApplyJob(jobId));
    };
  }

  async function handleJobEvent(event: MessageEvent<string>): Promise<void> {
    let job: ApplyJobResponse;
    try {
      job = JSON.parse(event.data) as ApplyJobResponse;
    } catch {
      updates.setError("Job status stream returned invalid data.");
      closeJobStream();
      return;
    }
    updates.setApplyJob(job);
    if (!terminalJobStatuses.has(job.status)) {
      return;
    }
    closeJobStream();
    await loadTerminalApplyJobLogIfMissing(job);
    await refreshAfterTerminalJob();
  }

  function handleJobProgressEvent(event: MessageEvent<string>): void {
    let progress: ApplyJobProgressEvent;
    try {
      progress = JSON.parse(event.data) as ApplyJobProgressEvent;
    } catch {
      updates.setError("Job progress stream returned invalid data.");
      return;
    }
    const job = updates.applyJob;
    if (!job || job.job_id !== progress.job_id) {
      return;
    }
    const progressEvents = applyJobProgressEvents(job);
    const progressKey = progressEventKey(progress);
    if (progressEvents.some((item) => progressEventKey(item) === progressKey)) {
      return;
    }
    updates.setApplyJob({
      ...job,
      progress: [...progressEvents, progress],
    });
  }

  async function loadTerminalApplyJobLogIfMissing(
    job: ApplyJobResponse | null = updates.applyJob,
  ): Promise<void> {
    if (
      !job?.run_id ||
      !terminalJobStatuses.has(job.status) ||
      updates.applyJobLog?.content ||
      applyJobRunLogFallbackRunId.value === job.run_id
    ) {
      return;
    }
    applyJobRunLogFallbackRunId.value = job.run_id;
    try {
      await updates.loadApplyJobLogFromRun(job);
    } catch {
      applyJobRunLogFallbackRunId.value = null;
    }
  }

  async function handleJobLogEvent(event: MessageEvent<string>): Promise<void> {
    let log: ApplyJobLogResponse;
    try {
      log = JSON.parse(event.data) as ApplyJobLogResponse;
    } catch {
      updates.setError("Job log stream returned invalid data.");
      return;
    }
    const logElement = options.applyJobPanelRef.value?.logElement() ?? null;
    const panelShouldScroll = shouldAutoScrollLog(logElement);
    updates.setApplyJobLog(log);
    await nextTick();
    if (panelShouldScroll) {
      scrollLogToBottom(options.applyJobPanelRef.value?.logElement() ?? null);
    }
  }

  function closeJobStream(): void {
    jobEventSource.value?.close();
    jobEventSource.value = null;
  }

  async function recoverOrRefreshApplyJob(jobId: string): Promise<void> {
    const job = await updates
      .loadApplyJob(jobId, { recoverMissing: true })
      .catch(() => undefined);
    if (job === undefined) {
      return;
    }
    if (job === null) {
      closeJobStream();
      await runs.loadRuns().catch(() => undefined);
      return;
    }
    if (terminalJobStatuses.has(job.status)) {
      closeJobStream();
      await refreshAfterTerminalJob();
    }
  }

  async function reconnectObservedApplyJob(): Promise<void> {
    if (!updates.rememberedApplyJobId) {
      return;
    }
    const job = await updates
      .loadApplyJob(updates.rememberedApplyJobId, { recoverMissing: true })
      .catch(() => undefined);
    if (job === undefined) {
      return;
    }
    if (job === null) {
      await runs.loadRuns().catch(() => undefined);
      return;
    }
    if (terminalJobStatuses.has(job.status)) {
      await refreshAfterTerminalJob();
      return;
    }
    subscribeApplyJob(job.job_id);
  }

  async function refreshAfterTerminalJob(): Promise<void> {
    const refresh = options.refreshAfterTerminalJob ?? options.loadPendingAndReleaseNotes;
    const runId = updates.applyJob?.run_id ?? null;
    await Promise.all([
      refresh?.() ?? Promise.resolve(),
      runs.loadRuns(),
      runId ? runs.loadRunDetail(runId).catch(() => undefined) : Promise.resolve(),
    ]);
  }

  function createApplyJobSnapshot(): ApplyJobPlanSnapshot | null {
    const plan = updates.plan;
    if (!plan) {
      return null;
    }
    return {
      contextLabel: planContextLabel.value,
      serviceCount:
        plan.summary.service_count || plan.summary.target_count || planLines.value.length,
      stackCount: plan.summary.stack_count,
      sourceFile: plan.source_file,
      lines: planLines.value.map(({ stack, line }) => ({
        key: `${stack}-${line.line_no}-${line.service}`,
        lineNo: line.line_no,
        stackName: stack,
        serviceLabel: planLineServiceLabel(plan.summary.stack_count, stack, line),
        tagRewriteLabel: planLineTagRewriteLabel(line),
        digestPinLabel: planLineDigestPinLabel(line),
        composeImage: line.compose_image,
        targetImage: line.target_image,
      })),
    };
  }

  async function focusApplyJobPanel(): Promise<void> {
    await nextTick();
    options.applyJobPanelRef.value?.focusPanel(
      prefersReducedMotion() ? "auto" : "smooth",
    );
  }

  if (getCurrentInstance()) {
    onUnmounted(() => {
      closeJobStream();
    });
  }

  watch(
    () => [updates.applyJob?.status, updates.applyJob?.run_id] as const,
    () => {
      runInBackground(loadTerminalApplyJobLogIfMissing());
    },
    { immediate: true },
  );

  watch(
    () => updates.applyJob?.status,
    (status) => {
      applyJobLiveLogExpanded.value = status
        ? !terminalJobStatuses.has(status)
        : true;
    },
    { immediate: true },
  );

  return {
    applyJobActive,
    applyJobAlertType,
    applyJobImpactLabel,
    applyJobLatestLogMessage,
    applyJobLiveLogExpanded,
    applyJobLiveLogToggleLabel,
    applyJobLiveLogVisible,
    applyJobLogEmptyMessage,
    applyJobLogText,
    applyJobLogTitle,
    applyJobLogWaiting,
    applyJobNowDescriptionIds,
    applyJobNowDetail,
    applyJobNowMessage,
    applyJobNowStatusLabel,
    applyJobNowTitle,
    applyJobPanelStatusLabel,
    applyJobProgressSteps,
    applyJobProgressSummary,
    applyJobSnapshot,
    applyJobStartedLabel,
    applyJobStatusMessage,
    applyJobSucceeded,
    applyJobTitle,
    applyJobUpdateLabel,
    applyJobVerification,
    closeJobStream,
    createApplyJobSnapshot,
    focusApplyJobPanel,
    loadTerminalApplyJobLogIfMissing,
    reconnectObservedApplyJob,
    subscribeApplyJob,
    terminalJobStatuses,
  };
}

function progressStatusLabel(status: ApplyJobProgressStep["status"]): string {
  if (status === "running") {
    return "Running";
  }
  if (status === "success") {
    return "Complete";
  }
  if (status === "failure") {
    return "Failed";
  }
  if (status === "skipped") {
    return "Skipped";
  }
  return "Waiting";
}

function displayProgressEvent(
  current: ApplyJobProgressEvent | null,
  next: ApplyJobProgressEvent,
): ApplyJobProgressEvent {
  if (!current || current.status !== "failure") {
    return next;
  }
  if (next.status === "failure") {
    return next;
  }
  return current;
}

function stackNamesFromSnapshot(snapshot: ApplyJobPlanSnapshot | null): string[] {
  const names = new Set<string>();
  for (const line of snapshot?.lines ?? []) {
    const name = line.stackName.trim();
    if (name) {
      names.add(name);
    }
  }
  return [...names];
}

function stackProgressSteps(
  stackNames: string[],
  progress: ApplyJobProgressEvent[],
  jobStatus: ApplyJobResponse["status"] | null,
  phases: ApplyJobProgressPhase[],
): ApplyJobProgressStep[] {
  const completePhase = stackCompletePhase(phases);
  return stackNames.map((stackName) =>
    stackProgressStep(
      stackName,
      progress.filter((event) => event.stack === stackName),
      jobStatus,
      completePhase,
    ),
  );
}

function stackProgressStep(
  stackName: string,
  events: ApplyJobProgressEvent[],
  jobStatus: ApplyJobResponse["status"] | null,
  completePhase: string,
): ApplyJobProgressStep {
  const failed = [...events].reverse().find((event) => event.status === "failure");
  if (failed) {
    return stackStep(stackName, "failure", "Failed", failed.message, failed);
  }
  const latest = events.at(-1) ?? null;
  if (jobStatus === "success") {
    return stackStep(
      stackName,
      "success",
      "Complete",
      "Stack update completed.",
      null,
    );
  }
  const running = [...events].reverse().find((event) => event.status === "running");
  if (running) {
    return stackStep(stackName, "running", "Running", running.message, running);
  }
  if (
    latest?.phase === completePhase &&
    (latest.status === "success" || latest.status === "skipped")
  ) {
    return stackStep(stackName, "success", "Complete", latest.message, latest);
  }
  if (latest) {
    return stackStep(stackName, "running", "In progress", latest.message, latest);
  }
  if (jobStatus === "failure") {
    return stackStep(
      stackName,
      "skipped",
      "Not started",
      `Job failed before ${stackName} started.`,
      null,
    );
  }
  return stackStep(
    stackName,
    "pending",
    "Queued",
    `Waiting for ${stackName} to start.`,
    null,
  );
}

function stackStep(
  stackName: string,
  status: ApplyJobProgressStep["status"],
  statusLabel: string,
  message: string,
  event: ApplyJobProgressEvent | null,
): ApplyJobProgressStep {
  return {
    key: `stack:${stackName}`,
    label: stackName,
    waitingMessage: `Waiting for ${stackName} to start.`,
    status,
    statusLabel,
    message,
    detail: progressEventDetail(event),
    event,
  };
}

function stackCompletePhase(phases: ApplyJobProgressPhase[]): string {
  return phases.find((phase) => phase.key === "health")?.key ?? "completion";
}

function progressEventDetail(event: ApplyJobProgressEvent | null): string {
  if (!event) {
    return "";
  }
  const parts = [];
  if (event.stack) {
    parts.push(event.stack);
  }
  if (event.services.length) {
    parts.push(event.services.join(", "));
  }
  if (event.line_numbers.length) {
    parts.push(`lines ${event.line_numbers.join(", ")}`);
  }
  return parts.join(" / ");
}

function progressEventKey(event: ApplyJobProgressEvent): string {
  return [
    event.created_at,
    event.phase,
    event.status,
    event.stack,
    event.message,
  ].join("\u0000");
}

function fallbackVerification(
  job: ApplyJobResponse | null,
  snapshot: ApplyJobPlanSnapshot | null,
): RunVerificationSummary {
  if (!job || !snapshot?.lines.length) {
    return emptyVerification();
  }
  const lines = snapshot.lines.filter(
    (line): line is VerificationSnapshotLine => line.lineNo !== null,
  );
  if (!lines.length) {
    return emptyVerification();
  }
  const items = lines.map((line) => fallbackVerificationItem(job, line));
  const needsReviewCount = items.filter((item) => item.follow_up_needed).length;
  return {
    status: needsReviewCount ? "needs_review" : "verified",
    total_count: items.length,
    verified_count: items.length - needsReviewCount,
    needs_review_count: needsReviewCount,
    items,
  };
}

function emptyVerification(): RunVerificationSummary {
  return {
    status: "verified",
    total_count: 0,
    verified_count: 0,
    needs_review_count: 0,
    items: [],
  };
}

function fallbackVerificationItem(
  job: ApplyJobResponse,
  line: VerificationSnapshotLine,
): RunVerificationItem {
  const progress = applyJobProgressEvents(job);
  const pull = progressForLine(progress, "pull", line.lineNo);
  const recreate = progressForLine(progress, "recreate", line.lineNo);
  const health = progressForLine(progress, "health", line.lineNo);
  const cleanup = progressForLine(progress, "cleanup", line.lineNo);
  const imageStatus = fallbackImageStatus(job, pull);
  const containerStatus = fallbackContainerStatus(recreate);
  const healthStatus = fallbackHealthStatus(health);
  const wudStatus = fallbackWudStatus(job, cleanup);
  const followUpNeeded = verificationFollowUpNeeded(
    imageStatus,
    containerStatus,
    healthStatus,
    wudStatus,
  );
  return {
    line_no: line.lineNo,
    service_key: line.serviceLabel,
    stack_name: "",
    service_name: line.serviceLabel,
    image: line.composeImage,
    target_image: line.targetImage,
    image_status: imageStatus,
    container_status: containerStatus,
    health_status: healthStatus,
    wud_status: wudStatus,
    follow_up_needed: followUpNeeded,
    summary: followUpNeeded ? "Manual review needed." : "Update verified.",
  };
}

function applyJobProgressEvents(
  job: ApplyJobResponse | null | undefined,
): ApplyJobProgressEvent[] {
  return Array.isArray(job?.progress) ? job.progress : [];
}

function progressForLine(
  progress: ApplyJobProgressEvent[],
  phase: string,
  lineNo: number,
): ApplyJobProgressEvent | null {
  return [...progress]
    .reverse()
    .find((event) => event.phase === phase && event.line_numbers.includes(lineNo)) ?? null;
}

function fallbackImageStatus(
  job: ApplyJobResponse,
  event: ApplyJobProgressEvent | null,
): RunVerificationImageStatus {
  if (event?.status === "failure" || job.status === "failure") {
    return "failed";
  }
  if (event?.status === "success") {
    return "new_image_running";
  }
  if (event?.status === "skipped") {
    return "already_current";
  }
  return "unknown";
}

function fallbackContainerStatus(
  event: ApplyJobProgressEvent | null,
): RunVerificationContainerStatus {
  if (event?.status === "success") {
    return "recreated";
  }
  if (event?.status === "skipped") {
    return "skipped";
  }
  if (event?.status === "failure") {
    return "failed";
  }
  return "unknown";
}

function fallbackHealthStatus(
  event: ApplyJobProgressEvent | null,
): RunVerificationHealthStatus {
  if (event?.status === "success") {
    return "passed";
  }
  if (event?.status === "skipped") {
    return "skipped";
  }
  if (event?.status === "failure") {
    return event.message.toLowerCase().includes("no containers")
      ? "service_disappeared"
      : "timed_out";
  }
  return "unknown";
}

function fallbackWudStatus(
  job: ApplyJobResponse,
  event: ApplyJobProgressEvent | null,
): RunVerificationWudStatus {
  if (event?.status === "success" && job.status === "success") {
    return "removed";
  }
  if (event?.status === "failure") {
    return "restored";
  }
  return "unknown";
}

function verificationFollowUpNeeded(
  imageStatus: RunVerificationImageStatus,
  containerStatus: RunVerificationContainerStatus,
  healthStatus: RunVerificationHealthStatus,
  wudStatus: RunVerificationWudStatus,
): boolean {
  return (
    imageStatus === "failed" ||
    imageStatus === "unknown" ||
    containerStatus === "failed" ||
    containerStatus === "unknown" ||
    healthStatus === "failed" ||
    healthStatus === "timed_out" ||
    healthStatus === "service_disappeared" ||
    healthStatus === "unknown" ||
    wudStatus === "restored" ||
    wudStatus === "stale_removed" ||
    wudStatus === "unknown"
  );
}

function latestNonEmptyLogLine(log: string): string {
  let lineEnd = log.length;
  while (lineEnd > 0) {
    let lineStart = log.lastIndexOf("\n", lineEnd - 1);
    if (lineStart === -1) {
      lineStart = 0;
    } else {
      lineStart += 1;
    }
    const line = log.slice(lineStart, lineEnd).trim();
    if (line) {
      return line;
    }
    lineEnd = lineStart > 0 ? lineStart - 1 : 0;
  }
  return "";
}

function shouldAutoScrollLog(element: HTMLElement | null): boolean {
  if (!element) {
    return true;
  }
  const distanceFromBottom =
    element.scrollHeight - element.scrollTop - element.clientHeight;
  return distanceFromBottom <= 48;
}

function scrollLogToBottom(element: HTMLElement | null): void {
  if (!element) {
    return;
  }
  element.scrollTop = element.scrollHeight;
}
