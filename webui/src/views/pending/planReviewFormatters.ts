import type {
  ApplyPreflightStatus,
  DigestPinLabelRewriteApprovalRequest,
  PendingDiagnostic,
  PendingRemovalPlanLine,
  PlanAction,
  PlanCleanupItem,
  PlanIssue,
  TagStreamLabelRewriteApprovalRequest,
} from "../../api/client";
import { reviewCountLabel, summarizeList } from "./utils";

type AssistantDetailKey =
  | "preflight_findings"
  | "possible_reasons"
  | "recommended_actions";

type DiagnosticItem = {
  diagnostic?: PendingDiagnostic | null;
};

export function actionCommand(action: PlanAction): string {
  return action.args.length ? action.args.join(" ") : action.description;
}

export function issueType(issue: PlanIssue): "error" | "warning" | "info" {
  return issue.severity === "error" ? "error" : "warning";
}

export function applyPreflightCheckType(
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

export function applyPreflightCheckLabel(status: ApplyPreflightStatus): string {
  if (status === "PASS") {
    return "Pass";
  }
  if (status === "WARN") {
    return "Warn";
  }
  return "Fail";
}

export function cleanupIssueKeys(item: PlanCleanupItem): string[] {
  return [item.reason, item.diagnostic?.code]
    .filter((code): code is string => Boolean(code))
    .map((code) => `${item.line_no}:${code}`);
}

export function issueHiddenByCleanupPreview(
  issue: PlanIssue,
  cleanupKeys: ReadonlySet<string>,
): boolean {
  if (issue.line_no === null) {
    return false;
  }
  return cleanupKeys.has(`${issue.line_no}:${issue.code}`);
}

export function issueLabel(issue: PlanIssue): string {
  const target = [
    issue.line_no ? `line ${issue.line_no}` : "",
    issue.stack,
    issue.service,
  ]
    .filter(Boolean)
    .join(" / ");
  return target ? `${target}: ${issue.message}` : issue.message;
}

export function issueHint(issue: PlanIssue): string {
  return issue.hint || "";
}

export function issueDetailString(issue: PlanIssue, key: string): string {
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

export function digestPinLabelIssueProposedRegex(issue: PlanIssue): string {
  return issueDetailString(issue, "proposed_label_regex");
}

export function staleDiagnosticLabel(item: DiagnosticItem): string {
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

export function staleDiagnosticDetail(item: DiagnosticItem): string {
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

export function staleIssueSummary(items: DiagnosticItem[]): string {
  return summarizeList(items.map(staleDiagnosticLabel), 2);
}

export function staleReviewSummary(
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

export function assistantDetailList(
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

export function cleanupLineLabel(item: PlanCleanupItem): string {
  return `#${item.line_no} ${item.image}`;
}

export function removalLineLabel(item: PendingRemovalPlanLine): string {
  return `#${item.line_no} ${item.image}`;
}
