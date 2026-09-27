import type {
  DigestPinLabelRewriteApprovalRequest,
  PlanMutationOptions,
  PlanResponse,
  PlanSelectionRequest,
  PendingResponse,
  TagOverrideRequest,
} from "../types";
import { STATIC_DEMO_READ_ONLY_MESSAGE } from "./constants";
import { clone, demoIdPart } from "./helpers";

const TAG_VALUE_PATTERN = /^\w[\w.-]{0,127}$/;
const STRICT_TAG_STREAM_PATTERN = /^(v?\d+\.\d+\.\d+)([-_.].+)?$/;

export function uniqueSortedNumbers(values: number[]): number[] {
  return [...new Set(values)].sort((left, right) => left - right);
}

export function normalizeDemoSelections(
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

export function readOnlyPlanFromPending(
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
