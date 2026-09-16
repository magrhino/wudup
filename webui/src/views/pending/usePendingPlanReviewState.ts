import { computed, ref, type ComputedRef, type Ref } from "vue";

import type {
  ApplyPreflightCheck,
  ApplyPreflightStatus,
  DigestPinLabelRewriteApprovalRequest,
  PendingDiagnostic,
  PendingGroupedItem,
  PendingRemovalPlanLine,
  PendingStackGroup,
  PlanAction,
  PlanCleanupItem,
  PlanIssue,
  PlanSelectionRequest,
  TagOverrideRequest,
  TagStreamDecision,
  TagStreamDecisionRequest,
  TagStreamLabelRewriteApprovalRequest,
} from "../../api/client";
import { useAuthStore } from "../../stores/auth";
import { useUpdatesStore } from "../../stores/updates";
import { pendingMetadataStatus } from "./pendingDisplay";
import {
  pendingPlanContextLabel,
  planActionsFromPlan,
  planDigestPinLabelRewritesFromPlan,
  planDigestUnpinUpdatesFromPlan,
  planLinesFromPlan,
  planTagUpdatesFromPlan,
  pluralize,
  reviewCountLabel,
  summarizeList,
} from "./utils";
import {
  pendingSelectionForItem,
  pendingSelectionKey,
} from "./usePendingSelectionState";

export type PendingUpdateIntent = {
  title: string;
  contextLabel: string;
  lineNumbers: number[];
  selections: PlanSelectionRequest[];
  allowTagUpdates: boolean;
  tagOverrides: TagOverrideRequest[];
  digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[];
  tagStreamDecisions: TagStreamDecisionRequest[];
  tagStreamLabelRewriteApprovals: TagStreamLabelRewriteApprovalRequest[];
  blockedMetadataCount?: number;
};

export type PendingApplyPlanPayload = {
  allowTagUpdates: boolean;
  tagOverrides: TagOverrideRequest[];
  digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[];
  tagStreamDecisions: TagStreamDecisionRequest[];
  tagStreamLabelRewriteApprovals: TagStreamLabelRewriteApprovalRequest[];
};

type AssistantDetailKey =
  | "preflight_findings"
  | "possible_reasons"
  | "recommended_actions";

type DiagnosticItem = {
  diagnostic?: PendingDiagnostic | null;
};

export type UsePendingPlanReviewStateOptions = {
  selectedLineNumbers: Ref<number[]>;
  selectedSelections: Ref<PlanSelectionRequest[]>;
  selectedSelectionKeySet: ComputedRef<Set<string>>;
  stackGroups: ComputedRef<PendingStackGroup[]>;
  unmatchedItems: ComputedRef<PendingGroupedItem[]>;
  pendingSourceLabel: ComputedRef<string>;
  tagOverrideErrorForLines: (lineNumbers: number[]) => string;
};

export function usePendingPlanReviewState(
  options: UsePendingPlanReviewStateOptions,
) {
  const updates = useUpdatesStore();
  const auth = useAuthStore();
  const updateIntent = ref<PendingUpdateIntent | null>(null);
  const retainedTagStreamDecisionIssues = ref<PlanIssue[]>([]);

  const mutationStateLabel = computed(() =>
    auth.session?.mutations_enabled ? "Mutations enabled" : "Read-only",
  );
  const mutationStateType = computed(() =>
    auth.session?.mutations_enabled ? "warning" : "success",
  );
  const pendingApplyTourDetail = computed(() =>
    auth.session?.mutations_enabled
      ? "Apply starts a server-side job, streams the live log, and writes a run record you can verify afterward."
      : "Read-only mode keeps Apply disabled. You can still preview impact now, then enable browser mutations server-side when you are ready to apply.",
  );
  const pendingSourceAllowsFileEdits = computed(
    () => (updates.pending?.source?.active ?? "file") === "file",
  );
  const selectedFreshLineNumbers = computed(() => {
    const byLine = new Map(
      (updates.pending?.items ?? []).map((item) => [item.line_no, item]),
    );
    return options.selectedSelections.value
      .filter(
        (selection) =>
          pendingMetadataStatus(byLine.get(selection.line_no) ?? {}) === "fresh",
      )
      .map((selection) => selection.line_no);
  });
  const selectedFreshCount = computed(
    () => selectedFreshLineNumbers.value.length,
  );
  const selectedTagOverrideError = computed(() =>
    options.tagOverrideErrorForLines(selectedFreshLineNumbers.value),
  );
  const selectedBlockedMetadataCount = computed(
    () => options.selectedSelections.value.length - selectedFreshCount.value,
  );
  const updateSelectedDisabled = computed(
    () =>
      selectedFreshCount.value === 0 ||
      updates.loading ||
      Boolean(selectedTagOverrideError.value),
  );
  const removeSelectedDisabled = computed(
    () =>
      options.selectedLineNumbers.value.length === 0 ||
      updates.loading ||
      !auth.session?.mutations_enabled ||
      !pendingSourceAllowsFileEdits.value,
  );
  const removeSelectedDisabledMessage = computed(() => {
    if (!options.selectedLineNumbers.value.length) {
      return "";
    }
    if (!pendingSourceAllowsFileEdits.value) {
      return `${options.pendingSourceLabel.value} entries cannot be removed from the WebUI because this source is read from WUD.`;
    }
    if (auth.session?.mutations_enabled) {
      return "";
    }
    return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to remove selected entries.";
  });
  const planAlertType = computed(() => {
    if (
      updates.plan?.status === "blocked" ||
      (updates.plan?.status === "ready" && !updates.plan.can_apply)
    ) {
      return "error";
    }
    if (updates.plan?.status === "empty") {
      return "warning";
    }
    return "info";
  });
  const planStatusLabel = computed(() => {
    if (updates.plan?.status === "ready" && !updates.plan.can_apply) {
      return "Apply blocked";
    }
    return updates.plan?.status ?? "";
  });
  const planContextLabel = computed(() => {
    return pendingPlanContextLabel(
      updates.plan,
      updateIntent.value?.contextLabel ?? "selected updates",
    );
  });
  const preflightTitle = computed(() => {
    if (!updates.plan) {
      return updateIntent.value?.title ?? "Review selected plan";
    }
    if (updates.plan.status === "blocked") {
      return "Plan blocked";
    }
    if (!updates.plan.can_apply) {
      return "Apply blocked";
    }
    if (updates.plan.status === "empty") {
      return "No changes to apply";
    }
    const context = planContextLabel.value;
    if (context === "selected updates") {
      return "Review selected updates";
    }
    if (/^\d+ stacks?$/.test(context)) {
      return `Review ${context}`;
    }
    return `Review ${context} plan`;
  });
  const preflightSummary = computed(() => {
    if (!updates.plan) {
      return "";
    }
    if (updates.plan.status === "blocked") {
      const issueCount =
        updates.plan.summary.issue_count || updates.plan.issues.length;
      return `${pluralize(issueCount, "issue")} must be fixed before applying.`;
    }
    if (updates.plan.status === "empty") {
      return "No selected services need changes.";
    }
    if (!updates.plan.can_apply && updates.plan.apply_preflight.failures) {
      return `${pluralize(updates.plan.apply_preflight.failures, "failed check")} must be fixed before applying.`;
    }
    const serviceCount =
      updates.plan.summary.service_count ||
      updates.plan.summary.target_count ||
      updates.plan.selected_line_numbers.length;
    return `${pluralize(serviceCount, "service")} ready to update.`;
  });
  const preflightServiceImpactLabel = computed(() => {
    if (!updates.plan || updates.plan.status !== "ready") {
      return "";
    }
    return summarizeList(
      planLines.value.map(({ stack, line }) =>
        updates.plan && updates.plan.summary.stack_count > 1
          ? `${stack} / ${line.service || "stack-level"}`
          : line.service || "stack-level",
      ),
      4,
    );
  });
  const applyPreflight = computed(() => updates.plan?.apply_preflight ?? null);
  const applyPreflightPassedChecks = computed(
    () =>
      applyPreflight.value?.checks.filter((check) => check.status === "PASS") ??
      [],
  );
  const applyPreflightAttentionChecks = computed(
    () =>
      applyPreflight.value?.checks.filter((check) => check.status !== "PASS") ??
      [],
  );
  const applyPreflightPassedText = computed(() =>
    applyPreflightPassedChecks.value.map((check) => check.label).join(", "),
  );
  const applyReadinessStatusLabel = computed(() => {
    if (!applyPreflight.value) {
      return "";
    }
    if (!applyPreflight.value.ok) {
      return "Blocked";
    }
    return applyPreflight.value.warnings > 0 ? "Warnings" : "Ready";
  });
  const applyReadinessStatusType = computed<"success" | "warning" | "error">(
    () => {
      if (!applyPreflight.value?.ok) {
        return "error";
      }
      return applyPreflight.value.warnings > 0 ? "warning" : "success";
    },
  );
  const applyReadinessSummary = computed(() => {
    if (!applyPreflight.value) {
      return "";
    }
    if (applyPreflight.value.failures > 0) {
      return `${pluralize(applyPreflight.value.failures, "failed check")} must be fixed before applying.`;
    }
    if (applyPreflight.value.warnings > 0) {
      return `${pluralize(applyPreflight.value.warnings, "warning")} to review before applying.`;
    }
    return "Required resources are reachable.";
  });
  const applyVisible = computed(() => updates.plan?.status === "ready");
  const applyAvailable = computed(
    () => applyVisible.value && !!updates.plan?.can_apply,
  );
  const applyDisabled = computed(() => !applyAvailable.value || updates.loading);
  const applyButtonLabel = computed(() => {
    const count =
      (updates.plan?.selected_selections?.length ?? 0) ||
      (updates.plan?.selected_line_numbers.length ?? 0);
    const updateLabel = updateIntent.value?.blockedMetadataCount
      ? "verified update"
      : "update";
    return count
      ? `Apply ${pluralize(count, updateLabel)}`
      : "Apply selected updates";
  });
  const cleanupItems = computed(() => updates.plan?.cleanup.items ?? []);
  const cleanupAvailable = computed(() => cleanupItems.value.length > 0);
  const allPlanIssues = computed(() => {
    const issues = updates.plan?.issues ?? [];
    if (!cleanupItems.value.length) {
      return issues;
    }
    const cleanupKeys = new Set(cleanupItems.value.flatMap(cleanupIssueKeys));
    return issues.filter(
      (issue) => !issueHiddenByCleanupPreview(issue, cleanupKeys),
    );
  });
  const digestPinLabelApprovalIssues = computed(() =>
    allPlanIssues.value.filter(
      (issue) =>
        issue.code === "compose-digest-pin-label-rewrite-unapproved" &&
        digestPinLabelApprovalFromIssue(issue) !== null,
    ),
  );
  const tagStreamDecisionIssues = computed(() => {
    const issuesByLine = new Map(
      retainedTagStreamDecisionIssues.value.map((issue) => [issue.line_no, issue]),
    );
    for (const issue of allPlanIssues.value) {
      if (issue.code === "tag-stream-change") {
        issuesByLine.set(issue.line_no, issue);
      }
    }
    return [...issuesByLine.values()];
  });
  const tagStreamLabelApprovalIssues = computed(() =>
    allPlanIssues.value.filter(
      (issue) =>
        issue.code === "compose-tag-stream-label-rewrite-unapproved" &&
        tagStreamLabelApprovalFromIssue(issue) !== null,
    ),
  );
  const visiblePlanIssues = computed(() =>
    allPlanIssues.value.filter(
      (issue) =>
        issue.code !== "tag-stream-change" &&
        (issue.code !== "compose-tag-stream-label-rewrite-unapproved" ||
          tagStreamLabelApprovalFromIssue(issue) === null) &&
        (issue.code !== "compose-digest-pin-label-rewrite-unapproved" ||
          digestPinLabelApprovalFromIssue(issue) === null),
    ),
  );
  const planTagStreamUpdates = computed(() =>
    updates.plan?.stacks.flatMap((stack) =>
      (stack.tag_stream_updates ?? []).map((update) => ({
        stack: stack.name,
        update,
      })),
    ) ?? [],
  );
  const planDigestPinLabelRewrites = computed(() =>
    planDigestPinLabelRewritesFromPlan(updates.plan),
  );
  const planDigestUnpinUpdates = computed(() =>
    planDigestUnpinUpdatesFromPlan(updates.plan),
  );
  const unmatchedReviewSummary = computed(() =>
    staleReviewSummary(options.unmatchedItems.value, "pending line", "pending lines"),
  );
  const unmatchedReviewCountLabel = computed(() =>
    reviewCountLabel(options.unmatchedItems.value.length, "item"),
  );
  const unmatchedIssueSummary = computed(() =>
    staleIssueSummary(options.unmatchedItems.value),
  );
  const cleanupAssistantFindings = computed(() =>
    assistantDetailList(cleanupItems.value, "preflight_findings"),
  );
  const cleanupAssistantReasons = computed(() =>
    assistantDetailList(cleanupItems.value, "possible_reasons"),
  );
  const cleanupAssistantActions = computed(() =>
    assistantDetailList(cleanupItems.value, "recommended_actions"),
  );
  const cleanupReviewSummary = computed(() => {
    const summary = staleReviewSummary(cleanupItems.value, "entry", "entries");
    return summary
      ? `${summary} Cleanup only removes WUD pending lines.`
      : "Cleanup only removes WUD pending lines.";
  });
  const cleanupButtonLabel = computed(
    () =>
      `Remove ${pluralize(cleanupItems.value.length, "unmatched entry", "unmatched entries")}`,
  );
  const cleanupDisabled = computed(
    () => !updates.plan?.cleanup.can_remove_unmatched || updates.loading,
  );
  const cleanupDisabledMessage = computed(() => {
    if (
      !updates.plan ||
      !cleanupAvailable.value ||
      updates.plan.cleanup.can_remove_unmatched
    ) {
      return "";
    }
    if (!auth.session?.mutations_enabled) {
      return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to remove stale pending entries.";
    }
    if (!pendingSourceAllowsFileEdits.value) {
      return `${options.pendingSourceLabel.value} entries cannot be removed from the WebUI because this source is read from WUD.`;
    }
    return "These pending entries cannot be removed right now.";
  });
  const pendingCleanupMessage = computed(() => {
    if (!updates.pendingCleanup) {
      return "";
    }
    return `${pluralize(updates.pendingCleanup.removed_count, "pending entry", "pending entries")} removed from ${options.pendingSourceLabel.value}.`;
  });
  const removalItems = computed(() => updates.pendingRemovalPlan?.lines ?? []);
  const removalButtonLabel = computed(
    () =>
      `Remove ${pluralize(options.selectedLineNumbers.value.length, "selected entry", "selected entries")}`,
  );
  const removalConfirmButtonLabel = computed(
    () =>
      `Remove ${pluralize(removalItems.value.length, "selected entry", "selected entries")}`,
  );
  const removalDisabled = computed(
    () => !updates.pendingRemovalPlan?.can_remove || updates.loading,
  );
  const mutationDisabledMessage = computed(() => {
    if (!updates.plan || updates.plan.status !== "ready" || updates.plan.can_apply) {
      return "";
    }
    if (!auth.session?.mutations_enabled) {
      const detail = updates.plan.apply_preflight.checks.find(
        (check) => check.code === "mutations-enabled",
      )?.detail;
      if (detail) {
        return `Read-only mode is active. ${detail}`;
      }
      return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to apply updates.";
    }
    if (!updates.plan.apply_preflight.ok) {
      const failed = updates.plan.apply_preflight.checks.find(
        (check) => check.status === "FAIL",
      );
      return failed?.detail
        ? `${failed.label}: ${failed.detail}`
        : "Fix the failed apply readiness check before applying updates.";
    }
    return "This plan cannot be applied.";
  });
  const selectedStackNames = computed(() =>
    options.stackGroups.value
      .filter((group) =>
        group.items.some((item) =>
          options.selectedSelectionKeySet.value.has(
            pendingSelectionKey(pendingSelectionForItem(item)),
          ),
        ),
      )
      .map((group) => group.name),
  );
  const selectedUpdateContext = computed(() => {
    if (selectedStackNames.value.length === 1) {
      return selectedStackNames.value[0];
    }
    if (selectedStackNames.value.length > 1) {
      return pluralize(selectedStackNames.value.length, "stack");
    }
    return "selected updates";
  });
  const batchSummaryLabel = computed(() => {
    const count = pluralize(options.selectedSelections.value.length, "update");
    const context = selectedUpdateContext.value === "selected updates"
      ? `${count} selected`
      : `${count} selected in ${selectedUpdateContext.value}`;
    if (!selectedBlockedMetadataCount.value) {
      return context;
    }
    return `${context} · ${pluralize(selectedFreshCount.value, "verified update")} · ${pluralize(selectedBlockedMetadataCount.value, "blocked update")}`;
  });
  const updateSelectedButtonLabel = computed(() =>
    selectedBlockedMetadataCount.value
      ? `Review ${pluralize(selectedFreshCount.value, "verified update")}`
      : `Review selected (${options.selectedSelections.value.length})`,
  );
  const selectedMetadataWarning = computed(() => {
    const count = selectedBlockedMetadataCount.value;
    if (!count) {
      return "";
    }
    return `${pluralize(count, "selected update")} ${count === 1 ? "is" : "are"} blocked because ${count === 1 ? "its" : "their"} metadata is stale. Check your WUD configuration. Blocked updates will stay selected and pending.`;
  });
  const planMetadataWarning = computed(() => {
    const count = updateIntent.value?.blockedMetadataCount ?? 0;
    if (!count) {
      return "";
    }
    return `${pluralize(count, "selected update")} ${count === 1 ? "is" : "are"} blocked because ${count === 1 ? "its" : "their"} metadata is stale. Check your WUD configuration. ${count === 1 ? "It" : "They"} will stay pending and will not be applied.`;
  });
  const planLines = computed(
    () => planLinesFromPlan(updates.plan),
  );
  const planActions = computed(
    () => planActionsFromPlan(updates.plan),
  );
  const planTagUpdates = computed(
    () => planTagUpdatesFromPlan(updates.plan),
  );
  const planDigestPinUpdates = computed(
    () =>
      updates.plan?.stacks.flatMap((stack) =>
        (stack.digest_pin_updates ?? []).map((update) => ({
          stack: stack.name,
          update,
        })),
      ) ?? [],
  );
  const plannedTagRewriteLines = computed(() =>
    planLines.value.filter(
      ({ line }) => Boolean(line.desired_tag) && line.action !== "digest-pin",
    ),
  );
  const plannedDigestPinLines = computed(() =>
    planLines.value.filter(({ line }) => line.action === "digest-pin"),
  );
  const plannedDigestUnpinLines = computed(() =>
    planLines.value.filter(({ line }) => line.action === "digest-unpin"),
  );
  const visibleTagRewriteCount = computed(
    () => planTagUpdates.value.length || plannedTagRewriteLines.value.length,
  );
  const visibleDigestPinCount = computed(
    () => planDigestPinUpdates.value.length || plannedDigestPinLines.value.length,
  );
  const visibleDigestUnpinCount = computed(
    () =>
      planDigestUnpinUpdates.value.length || plannedDigestUnpinLines.value.length,
  );
  const preflightTagRewriteNotice = computed(() => {
    if (
      !updateIntent.value?.allowTagUpdates ||
      !visibleTagRewriteCount.value ||
      !updates.plan
    ) {
      return "";
    }
    return `${pluralize(visibleTagRewriteCount.value, "tag rewrite")} will be applied before recreating selected services.`;
  });
  const preflightDigestPinNotice = computed(() => {
    if (!visibleDigestPinCount.value || !updates.plan?.digest_pin_updates) {
      return "";
    }
    return `${pluralize(visibleDigestPinCount.value, "digest-pin rewrite")} will pin approved tag updates after pull verification.`;
  });
  const preflightDigestUnpinNotice = computed(() => {
    if (!visibleDigestUnpinCount.value || !updates.plan) {
      return "";
    }
    return `${pluralize(visibleDigestUnpinCount.value, "digest unpin migration")} will rewrite pinned Compose images back to their watched tag before pulling.`;
  });

  function applyPreflightCheckDetail(check: ApplyPreflightCheck): string {
    if (check.status === "PASS") {
      return "";
    }
    if (
      check.code === "selected-services-matched" &&
      check.detail === "unmatched"
    ) {
      return cleanupItems.value.length
        ? staleReviewSummary(cleanupItems.value, "entry", "entries")
        : "Selected update is unmatched.";
    }
    return check.detail;
  }

  function digestPinLabelApprovalApproved(issue: PlanIssue): boolean {
    const approval = digestPinLabelApprovalFromIssue(issue);
    const intent = updateIntent.value;
    if (!approval || !intent) {
      return false;
    }
    const key = digestPinLabelApprovalKey(approval);
    return intent.digestPinLabelRewriteApprovals.some(
      (item) => digestPinLabelApprovalKey(item) === key,
    );
  }

  function setUpdateIntent(intent: PendingUpdateIntent): void {
    retainedTagStreamDecisionIssues.value = [];
    updateIntent.value = {
      ...intent,
      tagStreamDecisions: intent.tagStreamDecisions ?? [],
      tagStreamLabelRewriteApprovals:
        intent.tagStreamLabelRewriteApprovals ?? [],
    };
  }

  function clearUpdateIntent(): void {
    retainedTagStreamDecisionIssues.value = [];
    updateIntent.value = null;
  }

  function applyPlanPayload(fallback: {
    allowTagUpdates: boolean;
    tagOverrides: TagOverrideRequest[];
  }): PendingApplyPlanPayload {
    const intent = updateIntent.value;
    return {
      allowTagUpdates: intent?.allowTagUpdates ?? fallback.allowTagUpdates,
      tagOverrides: intent?.tagOverrides ?? fallback.tagOverrides,
      digestPinLabelRewriteApprovals:
        intent?.digestPinLabelRewriteApprovals ?? [],
      tagStreamDecisions: intent?.tagStreamDecisions ?? [],
      tagStreamLabelRewriteApprovals:
        intent?.tagStreamLabelRewriteApprovals ?? [],
    };
  }

  async function approveDigestPinLabelRewrite(
    issue: PlanIssue,
  ): Promise<boolean> {
    const approval = digestPinLabelApprovalFromIssue(issue);
    const intent = updateIntent.value;
    if (!approval || !intent || updates.loading) {
      return false;
    }
    const approvalsByKey = new Map(
      intent.digestPinLabelRewriteApprovals.map((item) => [
        digestPinLabelApprovalKey(item),
        item,
      ]),
    );
    approvalsByKey.set(digestPinLabelApprovalKey(approval), approval);
    const nextIntent: PendingUpdateIntent = {
      ...intent,
      digestPinLabelRewriteApprovals: [...approvalsByKey.values()],
    };
    await replanIntent(nextIntent);
    if (updateIntent.value !== intent) {
      return false;
    }
    updateIntent.value = nextIntent;
    return true;
  }

  async function chooseTagStream(
    issue: PlanIssue,
    decision: TagStreamDecision,
  ): Promise<boolean> {
    const intent = updateIntent.value;
    if (issue.line_no === null || !intent || updates.loading) {
      return false;
    }
    retainedTagStreamDecisionIssues.value = [
      ...retainedTagStreamDecisionIssues.value.filter(
        (item) => item.line_no !== issue.line_no,
      ),
      issue,
    ];
    const decisions = new Map(
      intent.tagStreamDecisions.map((item) => [item.line_no, item]),
    );
    decisions.set(issue.line_no, { line_no: issue.line_no, decision });
    const nextIntent: PendingUpdateIntent = {
      ...intent,
      tagStreamDecisions: [...decisions.values()],
      tagStreamLabelRewriteApprovals: intent.tagStreamLabelRewriteApprovals.filter(
        (item) => item.line_no !== issue.line_no,
      ),
    };
    await replanIntent(nextIntent);
    if (updateIntent.value !== intent) {
      return false;
    }
    updateIntent.value = nextIntent;
    return true;
  }

  async function approveTagStreamLabelRewrite(issue: PlanIssue): Promise<boolean> {
    const approval = tagStreamLabelApprovalFromIssue(issue);
    const intent = updateIntent.value;
    if (!approval || !intent || updates.loading) {
      return false;
    }
    const approvals = new Map(
      intent.tagStreamLabelRewriteApprovals.map((item) => [
        tagStreamLabelApprovalKey(item),
        item,
      ]),
    );
    approvals.set(tagStreamLabelApprovalKey(approval), approval);
    const nextIntent: PendingUpdateIntent = {
      ...intent,
      tagStreamLabelRewriteApprovals: [...approvals.values()],
    };
    await replanIntent(nextIntent);
    if (updateIntent.value !== intent) {
      return false;
    }
    updateIntent.value = nextIntent;
    return true;
  }

  function tagStreamDecisionSelected(
    issue: PlanIssue,
    decision: TagStreamDecision,
  ): boolean {
    return updateIntent.value?.tagStreamDecisions.some(
      (item) => item.line_no === issue.line_no && item.decision === decision,
    ) ?? false;
  }

  function tagStreamLabelApprovalApproved(issue: PlanIssue): boolean {
    const approval = tagStreamLabelApprovalFromIssue(issue);
    return Boolean(
      approval &&
        updateIntent.value?.tagStreamLabelRewriteApprovals.some(
          (item) => tagStreamLabelApprovalKey(item) === tagStreamLabelApprovalKey(approval),
        ),
    );
  }

  async function replanIntent(intent: PendingUpdateIntent): Promise<void> {
    const createArgs = [
      intent.lineNumbers,
      intent.allowTagUpdates,
      intent.tagOverrides,
      intent.digestPinLabelRewriteApprovals,
      intent.selections ?? [],
    ] as const;
    if (
      intent.tagStreamDecisions.length ||
      intent.tagStreamLabelRewriteApprovals.length
    ) {
      await updates.createPlan(
        ...createArgs,
        intent.tagStreamDecisions,
        intent.tagStreamLabelRewriteApprovals,
      );
      return;
    }
    await updates.createPlan(...createArgs);
  }

  return {
    actionCommand,
    applyButtonLabel,
    applyDisabled,
    applyPreflight,
    applyPreflightAttentionChecks,
    applyPreflightCheckDetail,
    applyPreflightCheckLabel,
    applyPreflightCheckType,
    applyPreflightPassedChecks,
    applyPreflightPassedText,
    applyPlanPayload,
    applyReadinessStatusLabel,
    applyReadinessStatusType,
    applyReadinessSummary,
    applyVisible,
    batchSummaryLabel,
    cleanupAssistantActions,
    cleanupAssistantFindings,
    cleanupAssistantReasons,
    cleanupAvailable,
    cleanupButtonLabel,
    cleanupDisabled,
    cleanupDisabledMessage,
    cleanupItems,
    cleanupLineLabel,
    cleanupReviewSummary,
    approveDigestPinLabelRewrite,
    approveTagStreamLabelRewrite,
    clearUpdateIntent,
    digestPinLabelApprovalApproved,
    digestPinLabelApprovalIssues,
    digestPinLabelIssueProposedRegex,
    issueDetailString,
    issueHint,
    issueLabel,
    issueType,
    mutationDisabledMessage,
    mutationStateLabel,
    mutationStateType,
    pendingApplyTourDetail,
    pendingCleanupMessage,
    planActions,
    planAlertType,
    planContextLabel,
    planDigestPinLabelRewrites,
    planDigestUnpinUpdates,
    planLines,
    planTagStreamUpdates,
    planMetadataWarning,
    planStatusLabel,
    preflightDigestPinNotice,
    preflightDigestUnpinNotice,
    preflightServiceImpactLabel,
    preflightSummary,
    preflightTagRewriteNotice,
    preflightTitle,
    pluralize,
    removalButtonLabel,
    removalConfirmButtonLabel,
    removalDisabled,
    removalItems,
    removalLineLabel,
    removeSelectedDisabled,
    removeSelectedDisabledMessage,
    selectedTagOverrideError,
    selectedMetadataWarning,
    selectedUpdateContext,
    setUpdateIntent,
    chooseTagStream,
    staleDiagnosticDetail,
    staleDiagnosticLabel,
    unmatchedIssueSummary,
    unmatchedReviewCountLabel,
    unmatchedReviewSummary,
    updateSelectedDisabled,
    tagStreamDecisionIssues,
    tagStreamDecisionSelected,
    tagStreamLabelApprovalApproved,
    tagStreamLabelApprovalIssues,
    updateSelectedButtonLabel,
    visiblePlanIssues,
  };
}

function actionCommand(action: PlanAction): string {
  return action.args.length ? action.args.join(" ") : action.description;
}

function issueType(issue: PlanIssue): "error" | "warning" | "info" {
  return issue.severity === "error" ? "error" : "warning";
}

function applyPreflightCheckType(
  status: ApplyPreflightStatus,
): "success" | "warning" | "error" {
  if (status === "PASS") {
    return "success";
  }
  if (status === "WARN") {
    return "warning";
  }
  return "error";
}

function applyPreflightCheckLabel(status: ApplyPreflightStatus): string {
  if (status === "PASS") {
    return "Pass";
  }
  if (status === "WARN") {
    return "Warn";
  }
  return "Fail";
}

function cleanupIssueKeys(item: PlanCleanupItem): string[] {
  return [item.reason, item.diagnostic?.code]
    .filter((code): code is string => Boolean(code))
    .map((code) => `${item.line_no}:${code}`);
}

function issueHiddenByCleanupPreview(
  issue: PlanIssue,
  cleanupKeys: ReadonlySet<string>,
): boolean {
  if (issue.line_no === null) {
    return false;
  }
  return cleanupKeys.has(`${issue.line_no}:${issue.code}`);
}

function issueLabel(issue: PlanIssue): string {
  const target = [
    issue.line_no ? `line ${issue.line_no}` : "",
    issue.stack,
    issue.service,
  ]
    .filter(Boolean)
    .join(" / ");
  return target ? `${target}: ${issue.message}` : issue.message;
}

function issueHint(issue: PlanIssue): string {
  return issue.hint || "";
}

function issueDetailString(issue: PlanIssue, key: string): string {
  const value = issue.details[key];
  return typeof value === "string" ? value : "";
}

export function digestPinLabelApprovalFromIssue(
  issue: PlanIssue,
): DigestPinLabelRewriteApprovalRequest | null {
  if (issue.code !== "compose-digest-pin-label-rewrite-unapproved") {
    return null;
  }
  const approval = {
    stack: issueDetailString(issue, "stack") || issue.stack,
    service: issueDetailString(issue, "service") || issue.service,
    label_key: issueDetailString(issue, "label_key"),
    current_label_value: issueDetailString(issue, "current_label_value"),
    planned_tag: issueDetailString(issue, "planned_tag"),
    proposed_label_value: issueDetailString(issue, "proposed_label_value"),
  };
  return Object.values(approval).every((value) => value.trim())
    ? approval
    : null;
}

export function digestPinLabelApprovalKey(
  approval: DigestPinLabelRewriteApprovalRequest,
): string {
  return [
    approval.stack,
    approval.service,
    approval.label_key,
    approval.current_label_value,
    approval.planned_tag,
    approval.proposed_label_value,
  ].join("\u0000");
}

export function tagStreamLabelApprovalFromIssue(
  issue: PlanIssue,
): TagStreamLabelRewriteApprovalRequest | null {
  if (
    issue.code !== "compose-tag-stream-label-rewrite-unapproved" ||
    issue.line_no === null
  ) {
    return null;
  }
  const approval = {
    line_no: issue.line_no,
    stack: issue.stack,
    stack_directory: issueDetailString(issue, "stack_directory"),
    compose_file: issueDetailString(issue, "compose_file"),
    service: issue.service,
    label_key: issueDetailString(issue, "label_key"),
    current_label_value: issueDetailString(issue, "current_label_value"),
    selected_tag: issueDetailString(issue, "selected_tag"),
    proposed_label_value: issueDetailString(issue, "proposed_label_value"),
  };
  return Object.values(approval).every((value) => String(value).trim())
    ? approval
    : null;
}

export function tagStreamLabelApprovalKey(
  approval: TagStreamLabelRewriteApprovalRequest,
): string {
  return [
    approval.line_no,
    approval.stack,
    approval.stack_directory,
    approval.compose_file,
    approval.service,
    approval.label_key,
    approval.current_label_value,
    approval.selected_tag,
    approval.proposed_label_value,
  ].join("\u0000");
}

export function tagStreamLabelApprovalIssueKey(issue: PlanIssue): string {
  const approval = tagStreamLabelApprovalFromIssue(issue);
  return approval === null
    ? [issue.line_no, issue.stack, issue.service].join("\u0000")
    : tagStreamLabelApprovalKey(approval);
}

function digestPinLabelIssueProposedRegex(issue: PlanIssue): string {
  return issueDetailString(issue, "proposed_label_regex");
}

function staleDiagnosticLabel(item: DiagnosticItem): string {
  switch (item.diagnostic?.code) {
    case "compose-label-active-file-missing":
      return "Compose file missing";
    case "compose-label-undiscovered-active-file":
      return "Stack not discovered";
    case "matching-container-without-compose-labels":
      return "Missing Compose labels";
    case "unmatched":
      return "No Compose match";
    default:
      return item.diagnostic ? "Unmatched source" : "No Compose match";
  }
}

function staleDiagnosticDetail(item: DiagnosticItem): string {
  switch (item.diagnostic?.code) {
    case "compose-label-active-file-missing":
      return "Running container exists, but its Compose file is missing or archived.";
    case "compose-label-undiscovered-active-file":
      return "Running container exists, but Compose discovery does not include its stack.";
    case "matching-container-without-compose-labels":
      return "Running container exists, but Docker did not report Compose labels.";
    case "unmatched":
      return "No discovered Compose service or running container matched this line.";
    default:
      return (
        item.diagnostic?.message || "No discovered Compose service matched this line."
      );
  }
}

function staleIssueSummary(items: DiagnosticItem[]): string {
  return summarizeList(items.map(staleDiagnosticLabel), 2);
}

function staleReviewSummary(
  items: DiagnosticItem[],
  singular: string,
  plural: string,
): string {
  if (!items.length) {
    return "";
  }
  const count = reviewCountLabel(items.length, singular, plural);
  const issue = staleIssueSummary(items);
  return issue ? `${count}: ${issue}.` : `${count}.`;
}

function assistantDetailList(
  items: DiagnosticItem[],
  key: AssistantDetailKey,
): string[] {
  const values: string[] = [];
  const seenValues = new Set<string>();
  for (const item of items) {
    for (const value of diagnosticDetailList(item.diagnostic, key)) {
      if (!seenValues.has(value)) {
        seenValues.add(value);
        values.push(value);
      }
    }
  }
  return values;
}

function diagnosticDetailList(
  diagnostic: PendingDiagnostic | null | undefined,
  key: AssistantDetailKey,
): string[] {
  const value = diagnostic?.details?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((entry) => {
    if (typeof entry !== "string") {
      return [];
    }
    const cleaned = entry.trim();
    return cleaned ? [cleaned] : [];
  });
}

function cleanupLineLabel(item: PlanCleanupItem): string {
  return `#${item.line_no} ${item.image}`;
}

function removalLineLabel(item: PendingRemovalPlanLine): string {
  return `#${item.line_no} ${item.image}`;
}
