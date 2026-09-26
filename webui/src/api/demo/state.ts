import type {
  AuthSessionResponse,
  CoreUpdateTourResponse,
  DiagnosticsSupportBundleResponse,
  DigestPinLabelRewriteApprovalRequest,
  DoctorResponse,
  OnboardingChecklistResponse,
  PendingItem,
  PendingMetadataRefreshRequest,
  PendingMetadataRefreshResponse,
  PendingRescanLine,
  PendingResponse,
  PendingRescanResponse,
  PendingRescanScope,
  PlanMutationOptions,
  PlanResponse,
  ReleaseNotesResponse,
  RetagChoiceRequest,
  RetagPreviewJobResponse,
  RetagPlanResponse,
  RetagTargetsResponse,
  RunDetail,
  RunLogResponse,
  RollbackPlanResponse,
  RunSummary,
  SecurityScanInfo,
  SecurityScanJobResponse,
  SecurityScansResponse,
  ServicePolicyRecord,
  SelfUpdatePlanResponse,
  SelfUpdateResponse,
  SettingsResponse,
  SetupStatusResponse,
  SnoozeRecord,
  SnoozeState,
  StatusResponse,
  TagExclusionRuleRecord,
  TagExclusionStatusFilter,
  TagOverrideRequest,
  UpdateTargetsResponse,
} from "../types";
import {
  DEMO_VERSION,
  STATIC_DEMO_READ_ONLY_MESSAGE,
} from "./constants";
import { generatedFixtures } from "./generatedFixtures";
import {
  cleanupLineKey,
  clone,
} from "./helpers";
import {
  normalizeDemoSelections,
  readOnlyPlanFromPending,
  uniqueSortedNumbers,
} from "./plan";
import { retagPlanFromChoices } from "./retag";
import { securityScanInfo as demoSecurityScanInfo } from "./security";
import type { DemoGeneratedFixtures } from "./types";
import {
  normalizeSecurityDigest,
  pendingItemPlatform,
} from "../../utils/securityScans";

const fixtures: DemoGeneratedFixtures = generatedFixtures;

function activeLineNumbers(activeKeys: Set<string>): Set<number> {
  return new Set(
    fixtures.pending.items
      .filter((item) => activeKeys.has(cleanupLineKey(item)))
      .map((item) => item.line_no),
  );
}

function demoNotificationKey(lineNo: number): string {
  return `demo-release-notification-${lineNo}`;
}

function filterPendingResponse(activeKeys: Set<string>): PendingResponse {
  const response: PendingResponse = clone(fixtures.pending);
  response.items = response.items.filter((item) => activeKeys.has(cleanupLineKey(item)));
  response.count = response.items.length;
  response.grouping.groups = response.grouping.groups
    .map((group) => {
      const items = group.items
        .filter((item) => activeKeys.has(cleanupLineKey(item)))
        .map((item) => ({
          ...item,
          selection_id:
            item.selection_id ||
            [
              "demo-selection",
              group.directory,
              group.compose_file,
              group.name,
              item.line_no,
              item.services.join(","),
            ].join(":"),
        }));
      return {
        ...group,
        line_numbers: items.map((item) => item.line_no),
        items,
      };
    })
    .filter((group) => group.items.length > 0);
  response.grouping.unmatched = response.grouping.unmatched
    .filter((item) => activeKeys.has(cleanupLineKey(item)))
    .map((item) => ({ ...item, selection_id: "" }));
  return response;
}

export class DemoApiState {
  private readonly activePendingLineKeys = new Set(
    fixtures.pending.items.map((item) => cleanupLineKey(item)),
  );
  policies = clone(fixtures.servicePolicies);
  snoozes = clone(fixtures.snoozes.all);
  tagExclusions = clone(fixtures.tagExclusions.all);
  runs = clone(fixtures.runs.summaries);
  private readonly runDetails = new Map(
    Object.entries(fixtures.runs.details).map(([id, detail]) => [
      Number(id),
      clone(detail),
    ]),
  );
  private readonly runLogs = new Map(
    Object.entries(fixtures.runs.logs).map(([id, log]) => [
      Number(id),
      clone(log),
    ]),
  );
  private readonly retagPreviewJobs = new Map<string, RetagPreviewJobResponse>();
  coreUpdateTour: CoreUpdateTourResponse = {
    status: "not_started",
    step: "dashboard",
    updated_at: "",
  };
  private nextRetagPreview = 1;

  session(): AuthSessionResponse {
    return {
      ...clone(fixtures.auth.session),
      dev_auth_bypass: false,
      mutations_enabled: false,
    };
  }

  setupStatus(): SetupStatusResponse {
    return {
      ...clone(fixtures.auth.setupStatus),
      dev_auth_bypass: false,
      mutations_enabled: false,
    };
  }

  status(): StatusResponse {
    return {
      ...clone(fixtures.status),
      version: DEMO_VERSION,
      dev_auth_bypass: false,
      mutations_enabled: false,
      auto_update_scheduler_enabled: false,
      pending_count: this.pendingResponse().count,
      source_hash: this.pendingResponse().source_hash ?? "",
    };
  }

  settings(): SettingsResponse {
    const settings = clone(fixtures.settings);
    settings.managed = settings.managed.map((entry) => ({
      ...entry,
      editable: false,
      disabled_reason:
        entry.disabled_reason || STATIC_DEMO_READ_ONLY_MESSAGE,
    }));
    this.updateSettingsEntry(settings.webui, "WUD_WEB_DEV_NO_AUTH", "false", false, "default");
    this.updateSettingsEntry(
      settings.webui,
      "WUD_WEB_MUTATIONS_ENABLED",
      "false",
      false,
      "default",
    );
    this.updateSettingsEntry(
      settings.webui,
      "WUD_WEB_AUTO_UPDATE_SCHEDULER_ENABLED",
      "false",
      false,
      "derived",
    );
    return settings;
  }

  private updateSettingsEntry(
    entries: SettingsResponse["webui"],
    name: string,
    value: string,
    configured: boolean,
    source: SettingsResponse["webui"][number]["source"],
  ): void {
    const entry = entries.find((item) => item.name === name);
    if (!entry) {
      return;
    }
    entry.value = value;
    entry.configured = configured;
    entry.source = source;
  }

  doctor(): DoctorResponse {
    return clone(fixtures.doctor);
  }

  diagnosticsSupportBundle(): DiagnosticsSupportBundleResponse {
    return {
      ...clone(fixtures.diagnostics),
      wudup_version: DEMO_VERSION,
      settings: this.settings(),
      doctor_result: this.doctor(),
      pending_summary: this.pendingResponse(),
      last_run_status: this.runs[0] ? clone(this.runs[0]) : null,
    };
  }

  onboardingChecklist(): OnboardingChecklistResponse {
    return clone(fixtures.onboarding);
  }

  pendingResponse(): PendingResponse {
    return filterPendingResponse(this.activePendingLineKeys);
  }

  pendingMetadata(
    request: PendingMetadataRefreshRequest,
  ): PendingMetadataRefreshResponse {
    const pending = this.pendingResponse();
    if ((pending.source_hash ?? "") !== request.source_hash) {
      return this.stalePendingMetadata(pending);
    }
    const byLine = new Map(pending.items.map((item) => [item.line_no, item]));
    const items: PendingMetadataRefreshResponse["items"] = [];
    for (const line of request.lines) {
      const item = byLine.get(line.line_no);
      if (item?.raw !== line.raw || item?.source_id !== line.source_id) {
        return this.stalePendingMetadata(pending);
      }
      items.push({
        line_no: item.line_no,
        raw: item.raw,
        source_id: item.source_id,
        wud_metadata: item.wud_metadata ?? null,
      });
    }
    return {
      status: "ready",
      requires_pending_reload: false,
      source_hash: pending.source_hash ?? "",
      source: pending.source,
      wud_api: pending.wud_api,
      items,
    };
  }

  private stalePendingMetadata(
    pending: PendingResponse,
  ): PendingMetadataRefreshResponse {
    return {
      status: "stale",
      requires_pending_reload: true,
      source_hash: pending.source_hash ?? "",
      source: pending.source,
      wud_api: pending.wud_api,
      items: [],
    };
  }

  updateTargets(): UpdateTargetsResponse {
    return clone(fixtures.updateTargets);
  }

  retagTargets(): RetagTargetsResponse {
    return clone(fixtures.retagTargets);
  }

  createRetagPlan(choices: RetagChoiceRequest[]): RetagPlanResponse {
    return retagPlanFromChoices(fixtures.retagTargets.items, choices);
  }

  createRetagPreviewJob(choices: RetagChoiceRequest[]): RetagPreviewJobResponse {
    const previewJobId = `demo-retag-preview-${this.nextRetagPreview++}`;
    const plan = this.createRetagPlan(choices);
    const complete: RetagPreviewJobResponse = {
      preview_job_id: previewJobId,
      status: plan.issues.length ? "failure" : "success",
      plan,
      warnings: plan.warnings,
      error: plan.issues[0]?.message ?? "",
      progress: [
        {
          job_id: previewJobId,
          phase: "compose-retag",
          status: plan.issues.length ? "failure" : "success",
          message: plan.issues.length
            ? "Demo retag preview found an invalid selection."
            : "Demo retag preview generated from current fixture data.",
          created_at: "2026-05-30T20:12:26+00:00",
          stack: plan.stacks[0]?.stack ?? "",
          services: plan.stacks.flatMap((stack) => stack.services),
          line_numbers: [],
        },
      ],
    };
    this.retagPreviewJobs.set(previewJobId, complete);
    return clone(complete);
  }

  retagPreviewJob(previewJobId: string): RetagPreviewJobResponse {
    const job = this.retagPreviewJobs.get(previewJobId);
    if (!job) {
      throw new Error("Demo retag preview job was not found.");
    }
    return clone(job);
  }

  releaseNotes(): ReleaseNotesResponse {
    const activeLines = activeLineNumbers(this.activePendingLineKeys);
    const fixture = clone(fixtures.releaseNotes);
    const response: ReleaseNotesResponse = {
      ...fixture,
      items: [],
    };
    response.items = fixture.items
      .filter((item) => activeLines.has(item.line_no))
      .map((item) => {
        const notificationKey =
          item.notification_key || demoNotificationKey(item.line_no);
        return {
          ...item,
          notification_key: notificationKey,
          notification_status: item.notification_status || "new",
          notification_last_sent_at: item.notification_last_sent_at || "",
          notification_send_count: item.notification_send_count || 0,
          notification_skipped_reason:
            item.notification_skipped_reason || "",
        };
      });
    response.count = response.items.length;
    return response;
  }

  securityScans(): SecurityScansResponse {
    const pending = this.pendingResponse();
    let seenReviewCandidate = false;
    const items = pending.items.map((item) => {
      const exactCandidate = Boolean(
        normalizeSecurityDigest(item.digest) && pendingItemPlatform(item),
      );
      const reviewCandidate =
        exactCandidate && !seenReviewCandidate;
      seenReviewCandidate ||= reviewCandidate;
      return this.securityScanInfo(item, reviewCandidate);
    });
    return {
      source_file: pending.source_file,
      source: clone(pending.source),
      source_hash: pending.source_hash ?? "",
      scanning_enabled: true,
      scanner: "trivy",
      scan_mode: "registry",
      count: items.length,
      items,
      warnings: [],
    };
  }

  securityScanJob(jobId = "demo-security-scan"): SecurityScanJobResponse {
    const result = this.securityScans();
    return {
      job_id: jobId,
      status: "success",
      total_count: result.count,
      completed_count: result.count,
      result,
      error: "",
    };
  }

  private securityScanInfo(
    item: PendingItem,
    firstExact: boolean,
  ): SecurityScanInfo {
    return demoSecurityScanInfo(item, firstExact);
  }

  selfUpdate(): SelfUpdateResponse {
    return clone(fixtures.selfUpdate);
  }

  selfUpdatePlan(): SelfUpdatePlanResponse {
    return clone(fixtures.selfUpdatePlan);
  }

  createPlan(
    lineNumbers: number[],
    allowTagUpdates: boolean,
    tagOverrides: TagOverrideRequest[],
    digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[] = [],
    options: PlanMutationOptions = {},
  ): PlanResponse {
    const pending = this.pendingResponse();
    const selections = options.selections ?? [];
    const normalizedSelections = normalizeDemoSelections(pending, selections);
    const selectedLineNumbers = uniqueSortedNumbers(
      normalizedSelections.length
        ? normalizedSelections.map((selection) => selection.line_no)
        : lineNumbers,
    );
    this.requireActiveLines(selectedLineNumbers);
    return readOnlyPlanFromPending(
      pending,
      selectedLineNumbers,
      allowTagUpdates,
      tagOverrides,
      digestPinLabelRewriteApprovals,
      { ...options, selections: normalizedSelections },
    );
  }

  rescanPending(
    scope: PendingRescanScope,
    lines: PendingRescanLine[],
  ): PendingRescanResponse {
    return {
      status: "blocked",
      audit_run_id: 0,
      scope,
      requested_count: scope === "selected" ? lines.length : 0,
      watched_count: 0,
      skipped: [],
      wud_api: {
        ...clone(fixtures.pending.wud_api),
        detail: "Static demo mode cannot trigger WUD rescans.",
      },
    };
  }

  servicePolicies(): ServicePolicyRecord[] {
    return clone(this.policies);
  }

  snoozeRecords(state: SnoozeState): SnoozeRecord[] {
    const records = this.snoozes.map((snooze) => {
      const active =
        snooze.kind === "dependency" && snooze.wait_for_service_key
          ? this.dependencySnoozeActive(snooze)
          : snooze.active;
      return { ...snooze, active };
    });
    return clone(
      records.filter((snooze) => {
        if (state === "active") {
          return snooze.active;
        }
        if (state === "expired") {
          return !snooze.active;
        }
        return true;
      }),
    );
  }

  private dependencySnoozeActive(snooze: SnoozeRecord): boolean {
    const [stackName, serviceName] = snooze.wait_for_service_key.split("/", 2);
    if (!stackName || !serviceName) {
      return snooze.active;
    }
    const createdAt = Date.parse(snooze.created_at);
    if (Number.isNaN(createdAt)) {
      return snooze.active;
    }
    const satisfied = this.runs.some((run) =>
      run.events.some((event) => {
        const eventCreatedAt = Date.parse(event.created_at);
        return (
          event.status === "success" &&
          event.stack_name === stackName &&
          event.service_name === serviceName &&
          !Number.isNaN(eventCreatedAt) &&
          eventCreatedAt >= createdAt
        );
      }),
    );
    return !satisfied;
  }

  tagExclusionRecords(status: TagExclusionStatusFilter): TagExclusionRuleRecord[] {
    return clone(
      this.tagExclusions.filter((rule) =>
        status === "all" ? true : rule.status === status,
      ),
    );
  }

  runSummaries(): RunSummary[] {
    return clone(this.runs.map(run => ({
      ...run,
      verification: this.runDetails.get(run.id)?.verification ?? null,
    })));
  }

  runDetail(runId: number): RunDetail {
    const detail = this.runDetails.get(runId);
    if (!detail) {
      throw new Error(`Demo run ${runId} was not found`);
    }
    return clone(detail);
  }

  runLog(runId: number): RunLogResponse {
    const log = this.runLogs.get(runId);
    if (!log) {
      throw new Error(`Demo run ${runId} was not found`);
    }
    return clone(log);
  }

  rollbackPlan(runId: number): RollbackPlanResponse {
    const plan = fixtures.runs.rollbackPlans[String(runId)];
    if (!plan) {
      throw new Error(`Demo run ${runId} was not found`);
    }
    return clone(plan);
  }


  private requireActiveLines(lineNumbers: number[]): void {
    const active = activeLineNumbers(this.activePendingLineKeys);
    if (!lineNumbers.every((lineNo) => active.has(lineNo))) {
      throw new Error("Demo fixture line is no longer active.");
    }
  }

}
