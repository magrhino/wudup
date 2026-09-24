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
  PlanSelectionRequest,
  PlanResponse,
  ReleaseNotesResponse,
  RetagChoiceRequest,
  RetagPreviewJobResponse,
  RetagPlanResponse,
  RetagTargetItem,
  RetagTargetsResponse,
  RunDetail,
  RunLogResponse,
  RollbackPlanResponse,
  RunSummary,
  SecurityScanInfo,
  SecurityScanJobResponse,
  SecurityScanSeverityCounts,
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
import type { DemoGeneratedFixtures } from "./types";
import {
  normalizeSecurityDigest,
  pendingItemPlatform,
} from "../../utils/securityScans";

const fixtures: DemoGeneratedFixtures = generatedFixtures;
const DEMO_NOW = "2026-05-31T00:00:00.000Z";
const DEMO_DB_UPDATED_AT = "2026-05-28T12:00:00+00:00";
const TAG_VALUE_PATTERN = /^\w[\w.-]{0,127}$/;
const STRICT_TAG_STREAM_PATTERN = /^(v?\d+\.\d+\.\d+)([-_.].+)?$/;
const EMPTY_SECURITY_COUNTS: SecurityScanSeverityCounts = {
  critical: 0,
  high: 0,
  medium: 0,
  low: 0,
  unknown: 0,
};
const DEMO_FINDING_SECURITY_COUNTS: SecurityScanSeverityCounts = {
  critical: 0,
  high: 1,
  medium: 0,
  low: 0,
  unknown: 0,
};
type DemoSecurityScanDecision = {
  hasFindings: boolean;
  state: SecurityScanInfo["state"];
  verdict: SecurityScanInfo["verdict"];
};

function activeLineNumbers(activeKeys: Set<string>): Set<number> {
  return new Set(
    fixtures.pending.items
      .filter((item) => activeKeys.has(cleanupLineKey(item)))
      .map((item) => item.line_no),
  );
}

function uniqueSortedNumbers(values: number[]): number[] {
  return [...new Set(values)].sort((left, right) => left - right);
}

function normalizeDemoSelections(
  pending: PendingResponse,
  selections: PlanSelectionRequest[],
): PlanSelectionRequest[] {
  const normalized = [...selections].sort(
    (left, right) =>
      left.line_no - right.line_no ||
      left.selection_id.localeCompare(right.selection_id),
  );
  const seen = new Set<string>();
  const modesByLine = new Map<number, Set<boolean>>();
  const available = new Map(
    pending.grouping.groups.flatMap((group) =>
      group.items
        .filter((item) => item.selection_id)
        .map((item) => [item.selection_id as string, item.line_no] as const),
    ),
  );
  for (const selection of normalized) {
    const key = `${selection.line_no}\0${selection.selection_id}`;
    if (seen.has(key)) {
      throw new Error(
        `selection for line ${selection.line_no} was provided more than once`,
      );
    }
    seen.add(key);
    const modes = modesByLine.get(selection.line_no) ?? new Set<boolean>();
    modes.add(Boolean(selection.selection_id));
    modesByLine.set(selection.line_no, modes);
    if (
      selection.selection_id &&
      available.get(selection.selection_id) !== selection.line_no
    ) {
      throw new Error(
        `selection for line ${selection.line_no} is stale or no longer available`,
      );
    }
  }
  const mixed = [...modesByLine.entries()]
    .filter(([, modes]) => modes.size > 1)
    .map(([lineNo]) => lineNo);
  if (mixed.length) {
    throw new Error(
      `selections cannot mix line-wide and stack-scoped entries for line(s): ${mixed.join(", ")}`,
    );
  }
  return normalized;
}

function demoNotificationKey(lineNo: number): string {
  return `demo-release-notification-${lineNo}`;
}

function retagTargetKey(item: RetagTargetItem): string {
  return item.target_id || item.service_key;
}

function retagChoiceKey(choice: RetagChoiceRequest): string {
  return choice.target_id || choice.service_key;
}

function lookupRetagChoiceTarget(
  choice: RetagChoiceRequest,
  targetById: Map<string, RetagTargetItem>,
  targetByUniqueService: Map<string, RetagTargetItem>,
): RetagTargetItem | undefined {
  if (choice.target_id) {
    return targetById.get(choice.target_id);
  }
  return targetByUniqueService.get(choice.service_key);
}

function demoIdPart(value: string): string {
  let result = "";
  let needsSeparator = false;
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    const isAllowed =
      (code >= 48 && code <= 57)
      || (code >= 65 && code <= 90)
      || (code >= 97 && code <= 122)
      || char === "_"
      || char === "."
      || char === "-";
    if (isAllowed) {
      if (needsSeparator && result !== "") {
        result += "-";
      }
      result += char;
      needsSeparator = false;
    } else if (result !== "") {
      needsSeparator = true;
    }
  }
  let start = 0;
  let end = result.length;
  while (start < end && result[start] === "-") {
    start += 1;
  }
  while (end > start && result[end - 1] === "-") {
    end -= 1;
  }
  return result.slice(start, end) || "item";
}

function replaceTagReference(value: string, defaultTag: string, tag: string): string {
  return value
    .replaceAll(`tag=${defaultTag}`, `tag=${tag}`)
    .replaceAll(`:${defaultTag}`, `:${tag}`);
}

function materializeTagOverride<T extends PendingResponse["grouping"]["unmatched"][number]>(
  item: T,
  tagOverrides: Map<number, string>,
): T {
  const tag = tagOverrides.get(item.line_no);
  if (!tag) {
    return item;
  }
  return {
    ...item,
    raw: replaceTagReference(item.raw, item.desired_tag, tag),
    desired_tag: tag,
    target_image: replaceTagReference(item.target_image, item.desired_tag, tag),
  };
}

function tagOverridesByLine(
  pending: PendingResponse,
  selectedLineNumbers: number[],
  allowTagUpdates: boolean,
  tagOverrides: TagOverrideRequest[],
): Map<number, string> {
  const overrides = new Map<number, string>();
  for (const item of tagOverrides) {
    if (overrides.has(item.line_no)) {
      throw new Error(`tag_overrides line ${item.line_no} was provided more than once`);
    }
    if (!TAG_VALUE_PATTERN.test(item.tag)) {
      throw new Error(`tag_overrides line ${item.line_no} has invalid tag: ${item.tag}`);
    }
    overrides.set(item.line_no, item.tag);
  }
  if (overrides.size === 0) {
    return overrides;
  }
  if (!allowTagUpdates) {
    throw new Error("tag_overrides require allow_tag_updates=true");
  }
  const selected = new Set(selectedLineNumbers);
  const pendingByLine = new Map(pending.items.map((item) => [item.line_no, item]));
  const missing = [...overrides.keys()].filter((lineNo) => !selected.has(lineNo));
  if (missing.length) {
    throw new Error(
      `tag_overrides must reference selected WUD tag update lines: ${missing.join(", ")}`,
    );
  }
  for (const lineNo of overrides.keys()) {
    if (!pendingByLine.get(lineNo)?.desired_tag) {
      throw new Error(`tag_overrides line ${lineNo} does not target a tag update`);
    }
  }
  return overrides;
}

type DemoPendingGroup = PendingResponse["grouping"]["groups"][number];
type DemoPendingGroupItem = DemoPendingGroup["items"][number];
type DemoTagStreamDecision = NonNullable<
  PlanMutationOptions["tagStreamDecisions"]
>[number]["decision"];

function demoTagStreamValues(item: DemoPendingGroupItem) {
  const current = STRICT_TAG_STREAM_PATTERN.exec(item.current_tag);
  const reported = STRICT_TAG_STREAM_PATTERN.exec(item.desired_tag);
  if (!item.tag_stream || !current || !reported) {
    return null;
  }
  const sameStreamTag = `${reported[1]}${current[2] ?? ""}`;
  const streamRegex = (tag: string) => {
    const parts = STRICT_TAG_STREAM_PATTERN.exec(tag);
    if (!parts) {
      return "";
    }
    const prefix = parts[1].startsWith("v") ? "v" : "";
    const suffix = (parts[2] ?? "").replace(
      /[\\^$.*+?()[\]{}|]/g,
      String.raw`\$&`,
    );
    return String.raw`^${prefix}\d+\.\d+\.\d+${suffix}$`;
  };
  return {
    sameStreamTag,
    preserveRegex: streamRegex(sameStreamTag),
    switchRegex: streamRegex(item.desired_tag),
  };
}

function demoTagStreamDecisions(
  groups: DemoPendingGroup[],
  decisions: NonNullable<PlanMutationOptions["tagStreamDecisions"]>,
): Map<number, DemoTagStreamDecision> {
  const availableLines = new Set(
    groups.flatMap((group) =>
      group.items
        .filter((item) => demoTagStreamValues(item) !== null)
        .map((item) => item.line_no),
    ),
  );
  const result = new Map<number, DemoTagStreamDecision>();
  for (const item of decisions) {
    if (result.has(item.line_no)) {
      throw new Error(`tag_stream_decisions line ${item.line_no} was provided more than once`);
    }
    if (!availableLines.has(item.line_no)) {
      throw new Error(
        `tag_stream_decisions must reference verified stream-change lines: ${item.line_no}`,
      );
    }
    result.set(item.line_no, item.decision);
  }
  return result;
}

function demoSelectedStreamTag(
  item: DemoPendingGroupItem,
  decision: DemoTagStreamDecision,
): string {
  const values = demoTagStreamValues(item);
  return decision === "preserve" && values ? values.sameStreamTag : item.desired_tag;
}

function validateDigestPinLabelRewriteApprovals(
  approvals: DigestPinLabelRewriteApprovalRequest[],
): string {
  const seen = new Set<string>();
  const parts: string[] = [];
  for (const item of approvals) {
    const key = [
      item.stack,
      item.service,
      item.label_key,
      item.current_label_value,
      item.planned_tag,
      item.proposed_label_value,
    ].join("\0");
    if (seen.has(key)) {
      throw new Error("digest_pin_label_rewrite_approvals contains a duplicate approval");
    }
    if (item.label_key !== "wud.tag.include") {
      throw new Error("digest_pin_label_rewrite_approvals can only approve wud.tag.include");
    }
    if (!TAG_VALUE_PATTERN.test(item.planned_tag)) {
      throw new Error("digest_pin_label_rewrite_approvals has an invalid planned tag");
    }
    seen.add(key);
    parts.push(`${item.stack}-${item.service}-${item.planned_tag}`);
  }
  return parts.map(demoIdPart).join("-");
}

function planIdFor(
  selectedLineNumbers: number[],
  allowTagUpdates: boolean,
  tagOverrides: Map<number, string>,
  digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
  selections: PlanSelectionRequest[] = [],
  tagStreamDecisions: NonNullable<PlanMutationOptions["tagStreamDecisions"]> = [],
): string {
  const parts = [
    `demo-session-${selectedLineNumbers.join("-") || "empty"}`,
    allowTagUpdates ? "allow-tags" : "block-tags",
  ];
  if (tagOverrides.size) {
    parts.push(
      [...tagOverrides.entries()]
        .sort(([left], [right]) => left - right)
        .map(([lineNo, tag]) => `${lineNo}-${demoIdPart(tag)}`)
        .join("-"),
    );
  }
  if (selections.length) {
    parts.push(
      selections
        .map(
          (selection) =>
            `${selection.line_no}-${demoIdPart(selection.selection_id || "line")}`,
        )
        .join("-"),
    );
  }
  if (tagStreamDecisions.length) {
    parts.push(
      [...tagStreamDecisions]
        .sort((left, right) => left.line_no - right.line_no)
        .map((item) => `${item.line_no}-stream-${item.decision}`)
        .join("-"),
    );
  }
  const approvalPart = validateDigestPinLabelRewriteApprovals(
    digestPinLabelRewriteApprovals,
  );
  if (approvalPart) {
    parts.push(approvalPart);
  }
  return parts.join("-");
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

function readOnlyPlanFromPending(
  pending: PendingResponse,
  selectedLineNumbers: number[],
  allowTagUpdates: boolean,
  tagOverrides: TagOverrideRequest[],
  digestPinLabelRewriteApprovals: DigestPinLabelRewriteApprovalRequest[],
  options: PlanMutationOptions = {},
): PlanResponse {
  const selections = options.selections ?? [];
  const selected = new Set(selectedLineNumbers);
  const scopedSelections = new Set(
    selections
      .filter((selection) => selection.selection_id)
      .map((selection) => selection.selection_id),
  );
  const broadLines = new Set(
    selections
      .filter((selection) => !selection.selection_id)
      .map((selection) => selection.line_no),
  );
  const itemSelected = (item: PendingResponse["grouping"]["unmatched"][number]) =>
    selected.has(item.line_no) &&
    (!selections.length ||
      broadLines.has(item.line_no) ||
      scopedSelections.has(item.selection_id ?? ""));
  const overrides = tagOverridesByLine(
    pending,
    selectedLineNumbers,
    allowTagUpdates,
    tagOverrides,
  );
  const selectedGroups = pending.grouping.groups
    .map((group) => ({
      ...group,
      items: group.items
        .filter(itemSelected)
        .map((item) => materializeTagOverride(item, overrides)),
    }))
    .filter((group) => group.items.length > 0);
  const selectedUnmatchedItems = pending.grouping.unmatched
    .filter(itemSelected)
    .map((item) => materializeTagOverride(item, overrides));
  const tagUpdatesDisabled = [
    ...selectedGroups.flatMap((group) => group.items),
    ...selectedUnmatchedItems,
  ].filter((item) => item.desired_tag && !allowTagUpdates);
  const matchedGroups = selectedGroups
    .map((group) => ({
      ...group,
      items: group.items.filter(
        (item) => allowTagUpdates || !item.desired_tag,
      ),
    }))
    .filter((group) => group.items.length > 0);
  const tagStreamDecisions = demoTagStreamDecisions(
    matchedGroups,
    options.tagStreamDecisions ?? [],
  );
  if (options.tagStreamLabelRewriteApprovals?.length) {
    throw new Error(
      "tag_stream_label_rewrite_approvals contains a stale or forged approval",
    );
  }
  const selectedUnmatched = selectedUnmatchedItems.filter(
    (item) => allowTagUpdates || !item.desired_tag,
  );
  const stacks = matchedGroups.map((group) =>
    readOnlyPlanStack(group, tagStreamDecisions),
  );
  const matchedTargets = stacks.flatMap((stack) =>
    stack.lines.map((line) => ({
      line_no: line.line_no,
      raw: line.raw,
      image: line.image,
      resolved_image: line.resolved_image,
      digest: line.digest,
      desired_tag: line.desired_tag,
      matched: true,
      action: line.action,
    })),
  );
  const skipped = [
    ...tagUpdatesDisabled.map((item) => ({
      line_no: item.line_no,
      raw: item.raw,
      image: item.image,
      desired_tag: item.desired_tag,
      reason: "tag-updates-disabled",
    })),
    ...selectedUnmatched.map((item) => ({
      line_no: item.line_no,
      raw: item.raw,
      image: item.image,
      desired_tag: item.desired_tag,
      reason: item.diagnostic?.code ?? "unmatched",
    })),
  ];
  const issues = [
    ...selectedUnmatched.map((item) => ({
      severity: "error",
      code: item.diagnostic?.code ?? "unmatched",
      message:
        item.diagnostic?.message ??
        "This pending update is not matched to a discovered Compose service.",
      line_no: item.line_no,
      stack: item.diagnostic?.stack ?? "",
      service: item.diagnostic?.service ?? "",
      hint: item.diagnostic?.hint ?? "",
      details: item.diagnostic?.details ?? {},
    })),
    ...matchedGroups.flatMap((group) =>
      group.items.flatMap((item) => {
        const values = demoTagStreamValues(item);
        if (!values || tagStreamDecisions.has(item.line_no)) {
          return [];
        }
        return [{
          severity: "error",
          code: "tag-stream-change",
          message:
            `WUD proposed changing the update stream from ${item.tag_stream?.current_stream} `
            + `to ${item.tag_stream?.reported_stream}. Choose whether to preserve or switch streams.`,
          line_no: item.line_no,
          stack: group.name,
          service: item.services[0] ?? "",
          hint: "",
          details: {
            current_tag: item.current_tag,
            reported_tag: item.desired_tag,
            current_stream: item.tag_stream?.current_stream ?? "",
            reported_stream: item.tag_stream?.reported_stream ?? "",
            same_stream_tag: values.sameStreamTag,
            preserve_label_regex: values.preserveRegex,
            switch_label_regex: values.switchRegex,
          },
        }];
      }),
    ),
  ];
  const serviceCount = new Set(
    stacks.flatMap((stack) =>
      stack.lines.map((line) => `${stack.name}/${line.service}`),
    ),
  ).size;

  let status: PlanResponse["status"] = "empty";
  if (issues.length > 0 || (stacks.length > 0 && skipped.length > 0)) {
    status = "blocked";
  } else if (stacks.length > 0) {
    status = "ready";
  }
  return {
    plan_id: planIdFor(
      selectedLineNumbers,
      allowTagUpdates,
      overrides,
      digestPinLabelRewriteApprovals,
      selections,
      options.tagStreamDecisions ?? [],
    ),
    dry_run: true,
    can_apply: false,
    status,
    source_file: pending.source_file,
    source: clone(pending.source),
    mode: "stop",
    max_wait: 180,
    digest_pin_updates: false,
    selected_line_numbers: selectedLineNumbers,
    selected_selections: selections,
    summary: {
      target_count: selectedLineNumbers.length,
      matched_target_count: matchedTargets.length,
      stack_count: stacks.length,
      service_count: serviceCount,
      skipped_count: skipped.length,
      issue_count: issues.length,
    },
    targets: [
      ...matchedTargets,
      ...selectedUnmatched.map((item) => ({
        line_no: item.line_no,
        raw: item.raw,
        image: item.image,
        resolved_image: item.resolved_image,
        digest: item.digest,
        desired_tag: item.desired_tag,
        matched: false,
        action: item.action,
      })),
      ...tagUpdatesDisabled.map((item) => ({
        line_no: item.line_no,
        raw: item.raw,
        image: item.image,
        resolved_image: item.resolved_image,
        digest: item.digest,
        desired_tag: item.desired_tag,
        matched: false,
        action: "tag-updates-disabled",
      })),
    ],
    stacks,
    skipped,
    issues,
    cleanup: {
      cleanup_id: "demo-session-cleanup",
      can_remove_unmatched: false,
      items: selectedUnmatched.map((item) => ({
        line_no: item.line_no,
        raw: item.raw,
        image: item.image,
        desired_tag: item.desired_tag,
        digest: item.digest,
        reason: item.diagnostic?.code ?? "unmatched",
        diagnostic: item.diagnostic,
      })),
    },
    apply_preflight: {
      ok: false,
      failures: 1,
      warnings: 0,
      checks: [
        {
          status: "FAIL",
          code: "mutations-enabled",
          label: "Mutations enabled",
          detail: STATIC_DEMO_READ_ONLY_MESSAGE,
          source_check_codes: ["webui-mutation-gate"],
        },
      ],
    },
  };
}

function readOnlyPlanStack(
  group: DemoPendingGroup,
  tagStreamDecisions: Map<number, DemoTagStreamDecision>,
): PlanResponse["stacks"][number] {
  const lines = group.items.map((item) => {
    const composeImage = item.compose_images[0] ?? item.resolved_image;
    const decision = tagStreamDecisions.get(item.line_no);
    const desiredTag = decision
      ? demoSelectedStreamTag(item, decision)
      : item.desired_tag;
    return {
      line_no: item.line_no,
      raw: item.raw,
      image: item.image,
      resolved_image: item.resolved_image,
      compose_image: composeImage,
      target_image: desiredTag
        ? replaceTagReference(
            item.target_image || item.resolved_image,
            item.desired_tag,
            desiredTag,
          )
        : item.target_image || item.resolved_image,
      service: item.services[0] ?? item.repo,
      digest: item.digest,
      desired_tag: desiredTag,
      action: item.action,
      digest_provenance: item.digest_provenance ?? null,
    };
  });
  const services = [...new Set(lines.map((line) => line.service))];
  return {
    name: group.name,
    directory: group.directory,
    compose_file: group.compose_file,
    project_directory: group.project_directory,
    services_label: group.services_label,
    services,
    pull_services: services,
    stop_services: services,
    force_recreate: lines.some((line) => line.action !== "tag-update"),
    up_no_deps: true,
    tag_updates: lines
      .filter((line) => line.action === "tag-update")
      .map((line) => ({
        old_image: line.compose_image,
        desired_tag: line.desired_tag,
        new_image: line.target_image,
        services: [line.service],
      })),
    tag_stream_updates: group.items.flatMap((item) => {
      const decision = tagStreamDecisions.get(item.line_no);
      const values = demoTagStreamValues(item);
      if (!decision || !values) {
        return [];
      }
      const selectedTag = demoSelectedStreamTag(item, decision);
      const proposedLabelRegex = decision === "preserve"
        ? values.preserveRegex
        : values.switchRegex;
      return [{
        line_no: item.line_no,
        service: item.services[0] ?? item.repo,
        current_tag: item.current_tag,
        reported_tag: item.desired_tag,
        selected_tag: selectedTag,
        decision,
        label_key: "wud.tag.include",
        current_label_value: "",
        proposed_label_value: proposedLabelRegex.replaceAll("$", "$$$$"),
        proposed_label_regex: proposedLabelRegex,
        approved: true,
        reason: "label-added",
      }];
    }),
    digest_pin_updates: [],
    digest_unpin_updates: [],
    actions: [],
    lines,
  };
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
    return this.retagPlanFromChoices(choices);
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

  private retagPlanFromChoices(choices: RetagChoiceRequest[]): RetagPlanResponse {
    const normalized = this.normalizedRetagChoices(choices);
    const selected = normalized
      .map((choice) => ({
        choice,
        item: fixtures.retagTargets.items.find(
          (item) => retagTargetKey(item) === retagChoiceKey(choice),
        ),
      }))
      .filter(
        (
          entry,
        ): entry is { choice: RetagChoiceRequest; item: RetagTargetItem } =>
          Boolean(entry.item) && entry.choice.choice === "switch-to-concrete",
      );
    const issues = selected
      .filter(({ item, choice }) => !this.retagTargetTag(item, choice))
      .map(({ item }) => ({
        severity: "error",
        code: "missing-target-tag",
        message: `${item.service_key} needs a concrete target tag.`,
        service_key: item.service_key,
        stack: item.stack,
        service: item.service,
        hint: "Choose a concrete tag before applying the retag plan.",
        details: {},
      }));
    const updates = selected
      .filter(({ item, choice }) => this.retagTargetTag(item, choice))
      .map(({ item, choice }) => this.retagPlanUpdate(item, choice));
    const stacks = fixtures.retagTargets.items
      .map((item) => ({
        stack: item.stack,
        directory: item.directory,
        compose_file: item.compose_file,
        project_directory: item.project_directory,
      }))
      .filter(
        (stack, index, stacks) =>
          stacks.findIndex(
            (candidate) =>
              candidate.stack === stack.stack &&
              candidate.directory === stack.directory &&
              candidate.compose_file === stack.compose_file &&
              candidate.project_directory === stack.project_directory,
          ) === index,
      )
      .map((stack) => ({
        ...stack,
        services: updates
          .filter((update) => update.stack === stack.stack)
          .map((update) => update.service),
        tag_updates: updates.filter(
          (update) =>
            update.stack === stack.stack &&
            fixtures.retagTargets.items.some(
              (item) =>
                item.service_key === update.service_key &&
                item.directory === stack.directory &&
                item.compose_file === stack.compose_file &&
                item.project_directory === stack.project_directory,
            ),
        ),
        digest_pin_updates: [],
      }))
      .filter((stack) => stack.tag_updates.length > 0);
    const selectedCount = updates.length;
    let status: RetagPlanResponse["status"] = "empty";
    if (selectedCount > 0) {
      status = issues.length > 0 ? "blocked" : "ready";
    }
    return {
      plan_id:
        selectedCount === 0
          ? "demo-retag-empty"
          : `demo-retag-${updates
              .map((update) => `${demoIdPart(update.service_key)}-${demoIdPart(update.target_tag)}`)
              .join("-")}`,
      status,
      can_apply: false,
      external_recreate_required: true,
      selected_count: selectedCount,
      keep_current_count: normalized.length - selectedCount,
      stacks,
      issues,
      warnings: [
        STATIC_DEMO_READ_ONLY_MESSAGE,
      ],
    };
  }

  private retagPlanUpdate(
    item: RetagTargetItem,
    choice: RetagChoiceRequest,
  ): RetagPlanResponse["stacks"][number]["tag_updates"][number] {
    const tag = this.retagTargetTag(item, choice);
    const finalImage = this.retagFinalImage(item, tag);
    return {
      target_id: retagTargetKey(item),
      service_key: item.service_key,
      stack: item.stack,
      service: item.service,
      source_image: item.image,
      target_tag: tag,
      final_image: finalImage,
      label_key: item.label_key,
      label_value: item.label_value,
      label_rewrites: item.label_key
        ? [
            {
              service: item.service,
              label_key: item.label_key,
              current_label_value: item.label_value,
              planned_tag: tag,
              proposed_label_value: item.label_value
                ? item.label_value.replace(item.tracking_tag, tag)
                : tag,
              proposed_label_regex: "",
              approved: true,
              reason: "demo",
            },
          ]
        : [],
    };
  }

  private retagTargetTag(
    item: RetagTargetItem,
    choice: RetagChoiceRequest,
  ): string {
    const tag = choice.target_tag?.trim() || item.proposed_tag;
    return tag && tag !== "latest" ? tag : "";
  }

  private retagFinalImage(item: RetagTargetItem, tag: string): string {
    if (!tag) {
      return item.final_image;
    }
    if (item.final_image.includes(`:${item.proposed_tag}`)) {
      return item.final_image.replace(`:${item.proposed_tag}`, `:${tag}`);
    }
    return `${item.image_repo}:${tag}`;
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
    const reportedDigest = normalizeSecurityDigest(item.digest);
    const platform = pendingItemPlatform(item);
    const decision = this.securityScanDecision(reportedDigest, platform, firstExact);
    const severityCounts = this.securityScanSeverityCounts(decision.hasFindings);
    const fixableCounts = this.securityScanSeverityCounts(decision.hasFindings);
    const findings = this.demoSecurityScanFindings(decision.hasFindings);
    const subject = this.demoSecurityScanSubject(item, reportedDigest, platform);
    const currentDigest = normalizeSecurityDigest(item.wud_metadata?.local_digest ?? "");
    const currentSubject = this.demoSecurityScanSubject(item, currentDigest, platform);
    const scannerVersion = decision.hasFindings ? "demo" : "";
    const scannerSchema = decision.hasFindings ? "trivy-json" : "";
    const dbRevision = decision.hasFindings ? "demo" : "";
    const dbUpdatedAt = decision.hasFindings ? DEMO_DB_UPDATED_AT : "";
    const comparison = this.demoSecurityScanComparison(
      decision,
      currentDigest,
      reportedDigest,
      platform,
      currentSubject,
      findings,
      Boolean(scannerVersion && scannerSchema && dbRevision && dbUpdatedAt),
    );

    return {
      line_no: item.line_no,
      state: decision.state,
      verdict: decision.verdict,
      scanner: "trivy",
      scanner_version: scannerVersion,
      scanner_schema: scannerSchema,
      scanned_at: decision.hasFindings ? DEMO_NOW : "",
      db_revision: dbRevision,
      db_updated_at: dbUpdatedAt,
      severity_counts: severityCounts,
      advisory_counts: severityCounts,
      advisory_counts_known: true,
      fixable_counts: fixableCounts,
      unfixed_count: 0,
      findings,
      subject,
      comparison,
      warnings:
        decision.hasFindings
          ? ["Demo finding for candidate and installed-digest comparison display."]
          : [],
      error_code: "",
      error_message: "",
    };
  }

  private demoSecurityScanFindings(
    hasFindings: boolean,
  ): SecurityScanInfo["findings"] {
    if (!hasFindings) {
      return [];
    }
    return [
      {
        target: "debian:12",
        target_class: "os-pkgs",
        target_type: "debian",
        vulnerability_id: "CVE-2026-0001",
        package_name: "demo-package",
        installed_version: "1.0.0",
        fixed_version: "1.0.1",
        severity: "high",
        title: "Demo vulnerability for candidate advisory review",
        primary_url: "https://avd.aquasec.com/nvd/cve-2026-0001",
      },
    ];
  }

  private demoSecurityScanSubject(
    item: PendingItem,
    digest: string,
    platform: string,
  ): SecurityScanInfo["subject"] {
    return {
      requested_ref: item.image,
      reported_digest: digest,
      index_digest: digest,
      manifest_digest: digest,
      immutable_ref: digest ? `${item.image.split("@")[0]}@${digest}` : "",
      platform,
    };
  }

  private demoSecurityScanComparison(
    decision: DemoSecurityScanDecision,
    currentDigest: string,
    reportedDigest: string,
    platform: string,
    currentSubject: SecurityScanInfo["subject"],
    findings: SecurityScanInfo["findings"],
    provenanceKnown: boolean,
  ): SecurityScanInfo["comparison"] {
    const comparisonReady =
      decision.state === "complete"
      && Boolean(currentDigest && reportedDigest && platform)
      && provenanceKnown;
    let message = "";
    if (comparisonReady) {
      message = "Demo comparison: installed and candidate findings are unchanged.";
    } else if (decision.state === "complete") {
      message = "Installed digest is unavailable in the demo fixture.";
    }
    return {
      status: comparisonReady ? "unchanged" : "unknown",
      current_subject: comparisonReady
        ? currentSubject
        : {
            requested_ref: "",
            reported_digest: "",
            index_digest: "",
            manifest_digest: "",
            immutable_ref: "",
            platform: "",
          },
      fixed_findings: [],
      remaining_findings: comparisonReady ? findings : [],
      introduced_findings: [],
      message,
    };
  }

  private securityScanDecision(
    reportedDigest: string,
    platform: string,
    reviewCandidate: boolean,
  ): DemoSecurityScanDecision {
    const exact = Boolean(reportedDigest && platform);
    const hasFindings = reviewCandidate && exact;

    if (hasFindings) {
      return {
        hasFindings,
        state: "complete",
        verdict: "findings",
      };
    }

    if (!exact) {
      return {
        hasFindings,
        state: "unsupported",
        verdict: "unknown",
      };
    }
    return {
      hasFindings,
      state: "not_scanned",
      verdict: "unknown",
    };
  }

  private securityScanSeverityCounts(
    hasFindings: boolean,
  ): SecurityScanSeverityCounts {
    if (hasFindings) {
      return { ...DEMO_FINDING_SECURITY_COUNTS };
    }
    return { ...EMPTY_SECURITY_COUNTS };
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

  private normalizedRetagChoices(
    choices: RetagChoiceRequest[],
  ): RetagChoiceRequest[] {
    const serviceCounts = new Map<string, number>();
    for (const item of fixtures.retagTargets.items) {
      serviceCounts.set(item.service_key, (serviceCounts.get(item.service_key) ?? 0) + 1);
    }
    const targetById = new Map(
      fixtures.retagTargets.items.map((item) => [retagTargetKey(item), item]),
    );
    const targetByUniqueService = new Map(
      fixtures.retagTargets.items
        .filter((item) => serviceCounts.get(item.service_key) === 1)
        .map((item) => [item.service_key, item]),
    );
    const byTarget = new Map<string, RetagChoiceRequest>();
    for (const choice of choices) {
      const targetId = choice.target_id ?? "";
      if (!targetId && serviceCounts.get(choice.service_key) !== 1) {
        throw new Error(
          "retag choices for duplicate service(s) must include target_id: "
            + choice.service_key,
        );
      }
      const target = lookupRetagChoiceTarget(choice, targetById, targetByUniqueService);
      if (!target) {
        throw new Error(
          targetId
            ? "Static demo retag target was not found."
            : "Static demo retag service was not found.",
        );
      }
      if (target.service_key !== choice.service_key) {
        throw new Error("Static demo retag target does not match service_key.");
      }
      const key = retagTargetKey(target);
      if (byTarget.has(key)) {
        throw new Error("retag choices contain duplicate target(s): " + choice.service_key);
      }
      byTarget.set(key, choice);
    }
    return fixtures.retagTargets.items.map((item) => {
      const requested = byTarget.get(retagTargetKey(item));
      const choice = requested?.choice ?? "keep-current";
      const targetTag = requested?.target_tag?.trim() ?? "";
      const normalized: RetagChoiceRequest = {
        service_key: item.service_key,
        choice,
      };
      if (item.target_id) {
        normalized.target_id = item.target_id;
      }
      if (choice === "switch-to-concrete" && targetTag) {
        normalized.target_tag = targetTag;
      }
      if (choice === "switch-to-concrete" && requested?.allow_start) {
        normalized.allow_start = true;
      }
      return normalized;
    });
  }

  private requireActiveLines(lineNumbers: number[]): void {
    const active = activeLineNumbers(this.activePendingLineKeys);
    if (!lineNumbers.every((lineNo) => active.has(lineNo))) {
      throw new Error("Demo fixture line is no longer active.");
    }
  }

}
