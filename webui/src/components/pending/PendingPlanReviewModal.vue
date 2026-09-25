<script setup lang="ts">
import { AlertTriangle, Check, CheckCircle2, Play, Trash2, XCircle } from "@lucide/vue";
import { NAlert, NButton, NTag } from "naive-ui";
import { computed } from "vue";
import UpdateScopeSummary from "../UpdateScopeSummary.vue";
import { planChanges } from "../../utils/updateSummary";

import type {
  ApplyPreflightCheck,
  ApplyPreflightResponse,
  ApplyPreflightStatus,
  PlanAction,
  PlanCleanupItem,
  PlanIssue,
  PlanResponse,
  ReleaseNoteInfo,
  SecurityScanInfo,
  PlanTagStreamUpdate,
  TagStreamDecision,
} from "../../api/client";
import {
  pendingMetadataStatusLabel,
  pendingMetadataStatusTagType,
  pendingMetadataStatusTitle,
  releaseNoteReason,
  releaseNoteStatus,
} from "../../views/pending/pendingDisplay";
import {
  planLineDigestPinLabel,
  planLineDigestUnpinLabel,
  planLineServiceLabel,
  planLineTagRewriteLabel,
  pluralize,
  type PlanActionView,
  type PlanDigestPinLabelRewriteView,
  type PlanDigestUnpinUpdateView,
  type PlanLineView,
} from "../../views/pending/utils";
import { tagStreamLabelApprovalIssueKey } from "../../views/pending/usePendingPlanReviewState";
import PendingReleaseNotes from "./PendingReleaseNotes.vue";
import PendingReviewSummary from "./PendingReviewSummary.vue";
import CoreUpdateTourPanel from "../CoreUpdateTourPanel.vue";
import PreflightFooterActions from "../preflight/PreflightFooterActions.vue";
import PreflightModalShell from "../preflight/PreflightModalShell.vue";
import PreflightNoticeList from "../preflight/PreflightNoticeList.vue";

type TagType = "default" | "error" | "info" | "success" | "warning";

const props = defineProps<{
  actionCommand: (action: PlanAction) => string;
  applyButtonLabel: string;
  applyDisabled: boolean;
  applyPreflight: ApplyPreflightResponse | null;
  applyPreflightAttentionChecks: ApplyPreflightCheck[];
  applyPreflightCheckDetail: (check: ApplyPreflightCheck) => string;
  applyPreflightCheckLabel: (status: ApplyPreflightStatus) => string;
  applyPreflightCheckType: (status: ApplyPreflightStatus) => TagType;
  applyPreflightPassedChecks: ApplyPreflightCheck[];
  applyPreflightPassedText: string;
  applyReadinessStatusLabel: string;
  applyReadinessStatusType: "success" | "warning" | "error";
  applyReadinessSummary: string;
  applyVisible: boolean;
  cleanupAvailable: boolean;
  cleanupButtonLabel: string;
  cleanupDisabled: boolean;
  cleanupDisabledMessage: string;
  cleanupItems: PlanCleanupItem[];
  cleanupReviewSummary: string;
  digestPinLabelApprovalApproved: (issue: PlanIssue) => boolean;
  digestPinLabelApprovalIssues: PlanIssue[];
  digestPinLabelIssueProposedRegex: (issue: PlanIssue) => string;
  tagStreamDecisionIssues: PlanIssue[];
  tagStreamDecisionSelected: (issue: PlanIssue, decision: TagStreamDecision) => boolean;
  tagStreamLabelApprovalApproved: (issue: PlanIssue) => boolean;
  tagStreamLabelApprovalIssues: PlanIssue[];
  issueDetailString: (issue: PlanIssue, key: string) => string;
  issueHint: (issue: PlanIssue) => string;
  issueLabel: (issue: PlanIssue) => string;
  issueType: (issue: PlanIssue) => "error" | "warning" | "info";
  loading: boolean;
  mutationDisabledMessage: string;
  plan: PlanResponse;
  planActions: PlanActionView[];
  planAlertType: TagType;
  planDigestPinLabelRewrites: PlanDigestPinLabelRewriteView[];
  planDigestUnpinUpdates: PlanDigestUnpinUpdateView[];
  planLines: PlanLineView[];
  releaseNotes: ReleaseNoteInfo[];
  releaseNotesLoading: boolean;
  releaseNotesError: string;
  securityScans: SecurityScanInfo[];
  securityScansLoading: boolean;
  securityScansError: string;
  planTagStreamUpdates: { stack: string; update: PlanTagStreamUpdate }[];
  planMetadataWarning: string;
  planStatusLabel: string;
  preflightDigestPinNotice: string;
  preflightDigestUnpinNotice: string;
  preflightServiceImpactLabel: string;
  preflightSummary: string;
  preflightTagRewriteNotice: string;
  preflightTitle: string;
  show: boolean;
  staleDiagnosticDetail: (item: PlanCleanupItem) => string;
  staleDiagnosticLabel: (item: PlanCleanupItem) => string;
  visiblePlanIssues: PlanIssue[];
}>();

const emit = defineEmits<{
  (event: "apply"): void;
  (event: "approve-digest-pin-label-rewrite", issue: PlanIssue): void;
  (event: "approve-tag-stream-label-rewrite", issue: PlanIssue): void;
  (event: "choose-tag-stream", issue: PlanIssue, decision: TagStreamDecision): void;
  (event: "close"): void;
  (event: "open-cleanup"): void;
}>();

const changes = computed(() => planChanges(props.plan));
const reviewReasons = computed(() => {
  const cleanupLines = new Set(props.cleanupItems.map(item => item.line_no));
  return [
    ...[...props.visiblePlanIssues, ...props.tagStreamDecisionIssues,
      ...props.tagStreamLabelApprovalIssues, ...props.digestPinLabelApprovalIssues].map(issue => [
      props.issueLabel(issue),
      props.issueHint(issue),
    ].filter(Boolean).join(" — ")),
    ...props.cleanupItems.map(item => `${item.image}: ${props.staleDiagnosticLabel(item)}. ${props.staleDiagnosticDetail(item)}`),
    ...props.plan.skipped.filter(item => !cleanupLines.has(item.line_no))
      .map(item => `${item.image}: skipped. ${item.reason}`),
    props.planMetadataWarning,
  ].filter(Boolean);
});

function releaseNoteProps(lineNo: number) {
  const note = props.releaseNotes.find((item) => item.line_no === lineNo) ?? null;
  const lookupError = note ? "" : props.releaseNotesError;
  return {
    releaseNote: note,
    releaseNoteStatus: lookupError ? "Check failed" : releaseNoteStatus(note, props.releaseNotesLoading),
    releaseNoteReason: releaseNoteReason(note) || lookupError,
  };
}

function tagStreamDecisionsComplete(): boolean {
  return props.tagStreamDecisionIssues.every(
    (issue) =>
      props.tagStreamDecisionSelected(issue, "preserve") ||
      props.tagStreamDecisionSelected(issue, "switch"),
  );
}

function selectedTagStreamUpdate(issue: PlanIssue): PlanTagStreamUpdate | undefined {
  return props.planTagStreamUpdates.find(
    (item) => item.update.line_no === issue.line_no,
  )?.update;
}

function tagStreamRulePreview(issue: PlanIssue): string {
  return selectedTagStreamUpdate(issue)?.proposed_label_regex
    ?? props.issueDetailString(issue, "preserve_label_regex");
}
</script>

<template>
  <PreflightModalShell
    :show="show"
    eyebrow="Preflight"
    title-id="preflight-modal-title"
    :title="preflightTitle"
    :summary="preflightSummary"
    :status-label="planStatusLabel === preflightTitle ? '' : planStatusLabel"
    :status-type="planAlertType"
    @close="emit('close')"
  >
    <section class="preflight-block" aria-label="Planned changes summary">
      <UpdateScopeSummary :changes="changes" fallback="No matched image changes" />
      <p v-if="preflightServiceImpactLabel" class="preflight-summary-text">Service impact: <span class="preflight-impact-text">{{ preflightServiceImpactLabel }}</span></p>
      <p class="preflight-summary-text">Review only. Nothing has been applied.</p>
    </section>

    <PendingReviewSummary
      :plan="plan"
      :release-notes="releaseNotes"
      :release-notes-loading="releaseNotesLoading"
      :release-notes-error="releaseNotesError"
      :security-scans="securityScans"
      :security-scans-loading="securityScansLoading"
      :security-scans-error="securityScansError"
      :reasons="reviewReasons"
    >
      <section
        v-if="applyPreflight"
        class="apply-readiness preflight-block"
        aria-labelledby="apply-readiness-title"
      >
        <div class="apply-readiness-heading">
          <div>
            <strong id="apply-readiness-title">Apply readiness</strong>
            <span v-if="applyReadinessSummary !== preflightSummary">{{ applyReadinessSummary }}</span>
          </div>
          <n-tag size="small" :type="applyReadinessStatusType">
            {{ applyReadinessStatusLabel }}
          </n-tag>
        </div>
        <div
          v-if="applyPreflightPassedChecks.length"
          class="apply-readiness-passed"
        >
          <CheckCircle2 :size="16" aria-hidden="true" />
          <p class="wrap-anywhere">
            <strong>
              {{ pluralize(applyPreflightPassedChecks.length, "check") }} passed:
            </strong>
            <span class="apply-readiness-pass-list">
              {{ applyPreflightPassedText }}
            </span>
          </p>
        </div>
        <div
          v-if="applyPreflightAttentionChecks.length"
          class="apply-readiness-list"
        >
          <div
            v-for="check in applyPreflightAttentionChecks"
            :key="check.code"
            class="apply-readiness-row"
            :class="`status-${check.status.toLowerCase()}`"
          >
            <CheckCircle2
              v-if="check.status === 'PASS'"
              :size="16"
              aria-hidden="true"
            />
            <AlertTriangle
              v-else-if="check.status === 'WARN'"
              :size="16"
              aria-hidden="true"
            />
            <XCircle v-else :size="16" aria-hidden="true" />
            <strong class="wrap-anywhere">{{ check.label }}</strong>
            <n-tag size="small" :type="applyPreflightCheckType(check.status)">
              {{ applyPreflightCheckLabel(check.status) }}
            </n-tag>
            <span
              v-if="applyPreflightCheckDetail(check)"
              class="apply-readiness-detail wrap-anywhere"
            >
              {{ applyPreflightCheckDetail(check) }}
            </span>
          </div>
        </div>
      </section>
      <p v-else class="preflight-summary-text">Apply readiness has not been checked. Preview the plan again before applying.</p>
    </PendingReviewSummary>

      <CoreUpdateTourPanel
        step="pending_preflight"
        title="Read the plan like a checklist"
        detail="Matched services and images show what will change. Issues block apply, tag rewrites are called out, and cleanup actions only edit the pending file after confirmation."
        next-label="Continue to apply guidance"
        next-step="pending_apply"
        @advanced="emit('close')"
      />

      <n-alert
        v-if="mutationDisabledMessage"
        class="preflight-block"
        type="warning"
      >
        {{ mutationDisabledMessage }}
      </n-alert>

      <section
        v-if="tagStreamDecisionIssues.length"
        class="preflight-impact preflight-block stream-decision-section"
        aria-labelledby="tag-stream-decision-title"
      >
        <div class="preflight-impact-heading">
          <strong id="tag-stream-decision-title">Update stream change</strong>
          <n-tag
            size="small"
            :type="tagStreamDecisionsComplete() ? 'success' : 'warning'"
          >
            {{ tagStreamDecisionsComplete() ? "Decision selected" : "Decision required" }}
          </n-tag>
        </div>
        <div
          v-for="issue in tagStreamDecisionIssues"
          :key="`stream-${issue.line_no}`"
          class="stream-decision"
        >
          <p>
            WUD proposed
            <code>{{ issueDetailString(issue, "reported_tag") }}</code>, which changes
            {{ issueDetailString(issue, "current_stream") }} to
            {{ issueDetailString(issue, "reported_stream") }}. The same-stream tag was verified.
          </p>
          <fieldset class="stream-choice-group">
            <legend class="sr-only">Update stream choice for line {{ issue.line_no }}</legend>
            <div class="stream-choice-grid">
              <n-button
                type="primary"
                :loading="loading"
                :disabled="tagStreamDecisionSelected(issue, 'preserve')"
                @click="emit('choose-tag-stream', issue, 'preserve')"
              >
                <template v-if="tagStreamDecisionSelected(issue, 'preserve')" #icon>
                  <Check :size="16" aria-hidden="true" />
                </template>
                Keep {{ issueDetailString(issue, "current_stream") }} —
                {{ issueDetailString(issue, "same_stream_tag") }}
              </n-button>
              <n-button
                secondary
                :loading="loading"
                :disabled="tagStreamDecisionSelected(issue, 'switch')"
                @click="emit('choose-tag-stream', issue, 'switch')"
              >
                <template v-if="tagStreamDecisionSelected(issue, 'switch')" #icon>
                  <Check :size="16" aria-hidden="true" />
                </template>
                Switch to {{ issueDetailString(issue, "reported_stream") }} —
                {{ issueDetailString(issue, "reported_tag") }}
              </n-button>
            </div>
          </fieldset>
          <div class="stream-rule-preview">
            <span>{{ selectedTagStreamUpdate(issue) ? "Resulting label" : "Recommended label" }}</span>
            <code>wud.tag.include={{ tagStreamRulePreview(issue) }}</code>
          </div>
        </div>
      </section>

      <section
        v-if="planTagStreamUpdates.length"
        class="preflight-impact preflight-block"
        aria-labelledby="tag-stream-updates-title"
      >
        <div class="preflight-impact-heading">
          <strong id="tag-stream-updates-title">Selected update stream</strong>
          <n-tag size="small" type="success">Resolved</n-tag>
        </div>
        <div class="compact-list">
          <div
            v-for="item in planTagStreamUpdates"
            :key="`${item.stack}-${item.update.service}-${item.update.line_no}`"
            class="list-row plan-line-row"
          >
            <span>{{ item.update.decision }}</span>
            <strong>{{ item.stack }} / {{ item.update.service }}</strong>
            <em>
              <code>{{ item.update.current_tag }} -> {{ item.update.selected_tag }}</code>
              <code>{{ item.update.label_key }}={{ item.update.proposed_label_regex }}</code>
            </em>
          </div>
        </div>
      </section>

      <section
        v-if="tagStreamLabelApprovalIssues.length"
        class="preflight-impact preflight-block"
        aria-labelledby="tag-stream-label-approvals-title"
      >
        <div class="preflight-impact-heading">
          <strong id="tag-stream-label-approvals-title">Update-stream label approval</strong>
          <n-tag size="small" type="warning">
            {{ pluralize(tagStreamLabelApprovalIssues.length, "approval") }}
          </n-tag>
        </div>
        <p class="preflight-summary-text">
          This service has a custom include expression. Review and approve the exact replacement; WUDup will not merge regular expressions.
        </p>
        <div class="compact-list">
          <div
            v-for="issue in tagStreamLabelApprovalIssues"
            :key="tagStreamLabelApprovalIssueKey(issue)"
            class="list-row plan-line-row digest-pin-approval-row"
          >
            <span>Review</span>
            <strong>
              {{ issue.stack }} / {{ issueDetailString(issue, "compose_file") }} /
              {{ issue.service }}
            </strong>
            <em>
              <code>{{ issueDetailString(issue, "current_label_value") }}</code>
              <span aria-hidden="true"> -> </span>
              <code>{{ issueDetailString(issue, "proposed_label_regex") }}</code>
              <n-button
                size="small"
                secondary
                type="primary"
                :disabled="tagStreamLabelApprovalApproved(issue)"
                :loading="loading"
                @click="emit('approve-tag-stream-label-rewrite', issue)"
              >
                <template #icon><Check :size="16" /></template>
                {{ tagStreamLabelApprovalApproved(issue) ? "Approved" : "Approve label rewrite" }}
              </n-button>
            </em>
          </div>
        </div>
      </section>
      <n-alert
        v-if="planMetadataWarning"
        class="preflight-block"
        type="warning"
      >
        {{ planMetadataWarning }}
      </n-alert>
      <n-alert
        v-if="preflightTagRewriteNotice"
        class="preflight-block"
        type="info"
      >
        {{ preflightTagRewriteNotice }}
      </n-alert>
      <n-alert
        v-if="preflightDigestPinNotice"
        class="preflight-block"
        type="info"
      >
        {{ preflightDigestPinNotice }}
      </n-alert>
      <n-alert
        v-if="preflightDigestUnpinNotice"
        class="preflight-block"
        type="info"
      >
        {{ preflightDigestUnpinNotice }}
      </n-alert>

      <section
        v-if="planDigestUnpinUpdates.length"
        class="preflight-impact preflight-block"
        aria-labelledby="digest-unpin-updates-title"
      >
        <div class="preflight-impact-heading">
          <strong id="digest-unpin-updates-title">Digest unpin migration</strong>
          <n-tag size="small" type="info">
            {{ pluralize(planDigestUnpinUpdates.length, "rewrite") }}
          </n-tag>
        </div>
        <div class="compact-list">
          <div
            v-for="item in planDigestUnpinUpdates"
            :key="`${item.stack}-${item.update.source_image}-${item.update.resolved_tag}`"
            class="list-row plan-line-row"
          >
            <span>Unpin</span>
            <strong>{{ item.stack }} / {{ item.update.services.join(", ") }}</strong>
            <em>
              <code>{{ item.update.source_image }}</code>
              <span aria-hidden="true"> -> </span>
              <code>{{ item.update.tag_image }}</code>
            </em>
          </div>
        </div>
      </section>

      <section
        v-if="planDigestPinLabelRewrites.length"
        class="preflight-impact preflight-block"
        aria-labelledby="digest-pin-label-rewrites-title"
      >
        <div class="preflight-impact-heading">
          <strong id="digest-pin-label-rewrites-title">Digest-pin label updates</strong>
          <n-tag size="small">{{ pluralize(planDigestPinLabelRewrites.length, "label") }}</n-tag>
        </div>
        <div class="compact-list">
          <div
            v-for="item in planDigestPinLabelRewrites"
            :key="`${item.stack}-${item.rewrite.service}-${item.rewrite.label_key}-${item.rewrite.current_label_value}`"
            class="list-row plan-line-row"
          >
            <span>Label</span>
            <strong>{{ item.stack }} / {{ item.rewrite.service }}</strong>
            <em>
              <code>{{ item.rewrite.label_key }}={{ item.rewrite.current_label_value }}</code>
              <span aria-hidden="true"> -> </span>
              <code>{{ item.rewrite.proposed_label_regex }}</code>
            </em>
          </div>
        </div>
      </section>

      <section
        v-if="digestPinLabelApprovalIssues.length"
        class="preflight-impact preflight-block"
        aria-labelledby="digest-pin-label-approvals-title"
      >
        <div class="preflight-impact-heading">
          <strong id="digest-pin-label-approvals-title">Digest-pin label approvals</strong>
          <n-tag size="small" type="warning">
            {{ pluralize(digestPinLabelApprovalIssues.length, "approval") }}
          </n-tag>
        </div>
        <p class="preflight-summary-text">
          Approve replacing each current include rule with the exact planned tag before applying.
        </p>
        <div class="compact-list">
          <div
            v-for="issue in digestPinLabelApprovalIssues"
            :key="`${issue.stack}-${issue.service}-${issueDetailString(issue, 'current_label_value')}`"
            class="list-row plan-line-row digest-pin-approval-row"
          >
            <span>Review</span>
            <strong>{{ issue.stack }} / {{ issue.service }}</strong>
            <em>
              <code>{{ issueDetailString(issue, "label_key") }}={{ issueDetailString(issue, "current_label_value") }}</code>
              <span aria-hidden="true"> -> </span>
              <code>{{ digestPinLabelIssueProposedRegex(issue) }}</code>
              <n-button
                size="small"
                secondary
                type="primary"
                :disabled="digestPinLabelApprovalApproved(issue)"
                :loading="loading"
                @click="emit('approve-digest-pin-label-rewrite', issue)"
              >
                <template #icon>
                  <Check :size="16" />
                </template>
                {{ digestPinLabelApprovalApproved(issue) ? "Approved" : "Approve label rewrite" }}
              </n-button>
            </em>
          </div>
        </div>
      </section>

      <n-alert
        v-if="cleanupDisabledMessage"
        class="preflight-block"
        type="warning"
      >
        {{ cleanupDisabledMessage }}
      </n-alert>

      <section
        v-if="cleanupAvailable"
        class="preflight-impact preflight-block"
        aria-labelledby="cleanup-preview-title"
      >
        <div class="preflight-impact-heading">
          <strong id="cleanup-preview-title">Unmatched pending entries</strong>
          <n-tag size="small" type="warning">
            {{ pluralize(cleanupItems.length, "entry", "entries") }}
          </n-tag>
        </div>
        <p class="preflight-summary-text">{{ cleanupReviewSummary }}</p>
        <div class="compact-list">
          <div
            v-for="item in cleanupItems"
            :key="`cleanup-${item.line_no}`"
            class="list-row plan-line-row"
          >
            <span>#{{ item.line_no }}</span>
            <strong class="plan-line-heading">
              <span>{{ item.image }}</span>
              <n-tag size="small" type="warning">
                {{ staleDiagnosticLabel(item) }}
              </n-tag>
            </strong>
            <em>
              <span>{{ staleDiagnosticDetail(item) }}</span>
            </em>
          </div>
        </div>
      </section>

      <section
        v-if="plan.status === 'ready'"
        class="preflight-impact preflight-block"
        aria-labelledby="preflight-impact-title"
      >
        <div class="preflight-impact-heading">
          <strong id="preflight-impact-title">Services and images</strong>
          <n-tag size="small">{{ pluralize(planLines.length, "service") }}</n-tag>
        </div>
        <div v-if="planLines.length" class="compact-list">
          <div
            v-for="{ stack, line } in planLines"
            :key="`${stack}-${line.line_no}-${line.service}`"
            class="list-row plan-line-row"
          >
            <span>#{{ line.line_no }}</span>
            <strong class="plan-line-heading">
              <span>{{ planLineServiceLabel(plan.summary.stack_count, stack, line) }}</span>
              <n-tag
                size="small"
                :type="pendingMetadataStatusTagType(line)"
                :title="pendingMetadataStatusTitle(line)"
              >
                {{ pendingMetadataStatusLabel(line) }} metadata
              </n-tag>
            </strong>
            <em>
              <span v-if="planLineTagRewriteLabel(line)" class="tag-rewrite-detail">
                <n-tag size="small" type="warning">Tag rewrite</n-tag>
                {{ planLineTagRewriteLabel(line) }}
              </span>
              <span
                v-else-if="planLineDigestPinLabel(line)"
                class="tag-rewrite-detail"
              >
                <n-tag size="small" type="info">Digest pin</n-tag>
                {{ planLineDigestPinLabel(line) }}
              </span>
              <span
                v-else-if="planLineDigestUnpinLabel(line)"
                class="tag-rewrite-detail"
              >
                <n-tag size="small" type="info">Digest unpin</n-tag>
                {{ planLineDigestUnpinLabel(line) }}
              </span>
              <template v-else>
                <code>{{ line.compose_image }}</code>
                <span aria-hidden="true"> -> </span>
                <code>{{ line.target_image }}</code>
              </template>
            </em>
            <PendingReleaseNotes class="plan-line-release"
              :candidate-label="`${stack} / ${line.service} · ${line.target_image}`"
              :candidate-tag="line.target_image.split('@')[0]?.split('/').pop()?.split(':')[1] || ''"
              v-bind="releaseNoteProps(line.line_no)"
            />
          </div>
        </div>
        <div v-else class="empty-state">No matched services.</div>
      </section>

      <PreflightNoticeList
        v-if="visiblePlanIssues.length"
        class="preflight-block"
        :warnings="[]"
        :issues="visiblePlanIssues.map((issue) => ({
          severity: issueType(issue),
          code: issue.code,
          message: issueLabel(issue),
          hint: issueHint(issue),
          service_key: `${issue.line_no ?? ''}-${issue.stack}-${issue.service}`,
        }))"
      />

      <div class="preflight-details-list">
        <details
          v-if="plan.status !== 'ready'"
          class="preflight-details"
          :open="plan.status === 'blocked'"
        >
          <summary class="disclosure-summary disclosure-summary-triangle">
            Services and images
          </summary>
          <div v-if="planLines.length" class="compact-list">
            <div
              v-for="{ stack, line } in planLines"
              :key="`${stack}-${line.line_no}-${line.service}`"
              class="list-row plan-line-row"
            >
              <span>#{{ line.line_no }}</span>
              <strong class="plan-line-heading">
                <span>{{ planLineServiceLabel(plan.summary.stack_count, stack, line) }}</span>
                <n-tag
                  size="small"
                  :type="pendingMetadataStatusTagType(line)"
                  :title="pendingMetadataStatusTitle(line)"
                >
                  {{ pendingMetadataStatusLabel(line) }} metadata
                </n-tag>
              </strong>
              <em>
                <span v-if="planLineTagRewriteLabel(line)" class="tag-rewrite-detail">
                  <n-tag size="small" type="warning">Tag rewrite</n-tag>
                  {{ planLineTagRewriteLabel(line) }}
                </span>
                <span
                  v-else-if="planLineDigestPinLabel(line)"
                  class="tag-rewrite-detail"
                >
                  <n-tag size="small" type="info">Digest pin</n-tag>
                  {{ planLineDigestPinLabel(line) }}
                </span>
                <span
                  v-else-if="planLineDigestUnpinLabel(line)"
                  class="tag-rewrite-detail"
                >
                  <n-tag size="small" type="info">Digest unpin</n-tag>
                  {{ planLineDigestUnpinLabel(line) }}
                </span>
                <template v-else>
                  <code>{{ line.compose_image }}</code>
                  <span aria-hidden="true"> -> </span>
                  <code>{{ line.target_image }}</code>
                </template>
              </em>
              <PendingReleaseNotes class="plan-line-release"
                :candidate-label="`${stack} / ${line.service} · ${line.target_image}`"
                :candidate-tag="line.target_image.split('@')[0]?.split('/').pop()?.split(':')[1] || ''"
                v-bind="releaseNoteProps(line.line_no)"
              />
            </div>
          </div>
          <div v-else class="empty-state">No matched services.</div>
        </details>

        <details v-if="planActions.length" class="preflight-details">
          <summary class="disclosure-summary disclosure-summary-triangle">
            Commands
          </summary>
          <div class="plan-actions">
            <div
              v-for="{ stack, action } in planActions"
              :key="`${stack}-${action.kind}-${actionCommand(action)}`"
              class="plan-action"
            >
              <n-tag size="small">{{ action.kind }}</n-tag>
              <code class="wrap-anywhere">{{ actionCommand(action) }}</code>
            </div>
          </div>
        </details>

        <details v-if="plan.skipped.length" class="preflight-details" open>
          <summary class="disclosure-summary disclosure-summary-triangle">
            Skipped
          </summary>
          <div class="compact-list">
            <div v-for="item in plan.skipped" :key="item.line_no" class="list-row">
              <span>#{{ item.line_no }}</span>
              <strong>{{ item.image }}</strong>
              <em>{{ item.reason }}</em>
            </div>
          </div>
        </details>

        <details class="preflight-details">
          <summary class="disclosure-summary disclosure-summary-triangle">
            Source lines
          </summary>
          <div class="compact-list">
            <div
              v-for="lineNo in plan.selected_line_numbers"
              :key="lineNo"
              class="list-row"
            >
              <span>Line</span>
              <strong>#{{ lineNo }}</strong>
              <em>{{ plan.source_file }}</em>
            </div>
          </div>
        </details>
      </div>

    <template #footer>
      <PreflightFooterActions @secondary="emit('close')">
        <n-button
          v-if="cleanupAvailable"
          type="warning"
          size="small"
          secondary
          :disabled="cleanupDisabled"
          :loading="loading"
          @click="emit('open-cleanup')"
        >
          <template #icon>
            <Trash2 :size="16" />
          </template>
          {{ cleanupButtonLabel }}
        </n-button>
        <template #primary>
          <n-button
            v-if="applyVisible"
            type="primary"
            size="small"
            :disabled="applyDisabled"
            :loading="loading"
            @click="emit('apply')"
          >
            <template #icon>
              <Play :size="16" />
            </template>
            {{ applyButtonLabel }}
          </n-button>
        </template>
      </PreflightFooterActions>
    </template>
  </PreflightModalShell>
</template>

<style scoped>
.plan-line-release {
  grid-column: 2 / -1;
}

.plan-actions {
  display: grid;
  gap: 8px;
}

.plan-action {
  display: grid;
  grid-template-columns: minmax(96px, auto) minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-height: 38px;
  padding: 8px 10px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-panel-tint);
}

.plan-action code {
  color: var(--color-code-text);
  font-family: var(--font-mono);
  font-size: 0.82rem;
}

.digest-pin-approval-row em {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.stream-decision {
  display: grid;
  gap: 10px;
}

.stream-decision p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.stream-choice-group {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.stream-choice-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.stream-rule-preview {
  display: grid;
  gap: 4px;
  color: var(--color-muted-text);
  font-size: 0.82rem;
}

.stream-rule-preview code {
  color: var(--color-code-text);
}

.apply-readiness {
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--color-border-subtle);
  border-radius: 7px;
  background: var(--color-surface);
}

.apply-readiness-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.apply-readiness-heading > div {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.apply-readiness-heading span {
  color: var(--color-muted-text);
  font-size: 0.84rem;
}

.apply-readiness-passed {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
  color: var(--color-text-secondary);
  font-size: 0.84rem;
  line-height: 1.45;
}

.apply-readiness-passed > svg {
  flex: 0 0 auto;
  margin-top: 1px;
  color: var(--color-operational-teal);
}

.apply-readiness-passed p {
  margin: 0;
}

.apply-readiness-passed strong {
  color: var(--color-ink);
}

.apply-readiness-list {
  display: grid;
  border-top: 1px solid var(--color-border-subtle);
}

.apply-readiness-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 8px 0;
  border-bottom: 1px solid var(--color-border-subtle);
}

.apply-readiness-row:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

.apply-readiness-row > svg {
  color: var(--color-operational-teal);
}

.apply-readiness-row.status-warn > svg {
  color: var(--color-warning-fg);
}

.apply-readiness-row.status-fail > svg {
  color: var(--color-error-fg);
}

.apply-readiness-detail {
  grid-column: 2 / -1;
  color: var(--color-text-secondary);
  font-size: 0.84rem;
  line-height: 1.45;
}

@media (--wud-compact) {
  .plan-line-release {
    grid-column: 1 / -1;
  }

  .plan-action {
    grid-template-columns: 1fr;
  }

  .stream-choice-grid :deep(.n-button) {
    width: 100%;
    min-height: var(--size-touch-target);
    white-space: normal;
  }
}
</style>
