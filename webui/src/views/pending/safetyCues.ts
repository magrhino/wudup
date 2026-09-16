import type {
  PendingGroupedItem,
  PendingItem,
  PendingResponse,
  ReleaseNoteInfo,
  SecurityScanInfo,
  ServicePolicyRecord,
  SnoozeRecord,
} from "../../api/client";
import { securityScanCueDisplay } from "../../utils/securityScans";

export type SafetyCue = {
  key: string;
  label: string;
  type: "default" | "error" | "info" | "success" | "warning";
};

export type SafetyCueContext = {
  pending: PendingResponse | null;
  releaseNote: ReleaseNoteInfo | null;
  releaseNotesLoaded: boolean;
  releaseNotesLoading: boolean;
  securityScan: SecurityScanInfo | null;
  securityScansCurrent: boolean;
  securityScansEnabled: boolean;
  securityScansLoaded: boolean;
  securityScansLoading: boolean;
  servicePolicies: Pick<ServicePolicyRecord, "auto_update" | "service_key">[];
  snoozes: Pick<SnoozeRecord, "service_key">[];
};

type ParsedVersion = {
  major: number;
  minor: number;
  patch: number;
};

export function getPendingGroupedItem(
  pending: PendingResponse | null | undefined,
  lineNo: number,
): PendingGroupedItem | undefined {
  if (!pending?.grouping) {
    return undefined;
  }
  for (const group of pending.grouping.groups) {
    const found = group.items.find((item) => item.line_no === lineNo);
    if (found) {
      return found;
    }
  }
  return pending.grouping.unmatched.find((item) => item.line_no === lineNo);
}

export function pendingServiceKeys(
  pending: PendingResponse | null | undefined,
  row: Pick<PendingItem, "line_no">,
): string[] {
  if (!pending?.grouping) {
    return [];
  }
  const keys: string[] = [];
  for (const group of pending.grouping.groups) {
    const item = group.items.find((groupItem) => groupItem.line_no === row.line_no);
    if (!item) {
      continue;
    }
    for (const service of item.services) {
      keys.push(`${group.name}/${service}`);
    }
  }
  const unmatched = pending.grouping.unmatched.find(
    (item) => item.line_no === row.line_no,
  );
  if (unmatched?.diagnostic?.stack && unmatched.diagnostic.service) {
    keys.push(`${unmatched.diagnostic.stack}/${unmatched.diagnostic.service}`);
  }
  return [...new Set(keys)];
}

export function safetyCues(
  row: PendingItem,
  context: SafetyCueContext,
): SafetyCue[] {
  const groupedItem = getPendingGroupedItem(context.pending, row.line_no);
  const serviceKeys = pendingServiceKeys(context.pending, row);
  const cues: SafetyCue[] = [];
  const addCue = (key: string, label: string, type: SafetyCue["type"]) => {
    cues.push({ key, label, type });
  };

  addReleaseNoteSecurityCue(context.releaseNote, addCue);
  addVersionBumpCue(row, addCue);

  if (row.tag_stream) {
    addCue("possible-stream-change", "Possible stream change", "error");
  }

  if (!row.desired_tag && row.digest) {
    addCue("digest-only", "Digest-only", "info");
  }

  if (row.desired_tag === "latest" || (!row.desired_tag && row.current_tag === "latest")) {
    addCue("mutable-latest", "Mutable latest", "warning");
  }

  if (groupedItem?.action === "recreate_stack") {
    addCue("stack-restart", "Stack restart", "warning");
  }

  addReleaseNoteCues(context, addCue);
  addSecurityScanCues(context, addCue);
  addPolicyCues(serviceKeys, context, addCue);

  return cues;
}

function addVersionBumpCue(
  row: Pick<PendingItem, "current_tag" | "desired_tag">,
  addCue: (key: string, label: string, type: SafetyCue["type"]) => void,
): void {
  if (!row.current_tag || !row.desired_tag || row.current_tag === row.desired_tag) {
    return;
  }
  const current = parseVersion(row.current_tag);
  const desired = parseVersion(row.desired_tag);
  if (!current || !desired) {
    return;
  }
  if (current.major !== desired.major) {
    addCue("major-bump", "Major bump", "error");
  } else if (current.minor !== desired.minor) {
    addCue("minor-bump", "Minor bump", "warning");
  } else if (current.patch !== desired.patch) {
    addCue("patch-bump", "Patch bump", "info");
  }
}

function addReleaseNoteCues(
  context: Pick<
    SafetyCueContext,
    "releaseNote" | "releaseNotesLoaded" | "releaseNotesLoading"
  >,
  addCue: (key: string, label: string, type: SafetyCue["type"]) => void,
): void {
  const note = context.releaseNote;
  if (note?.breaking) {
    addCue("possible-breaking", "Possible breaking", "warning");
  }
  if (
    context.releaseNotesLoaded &&
    !context.releaseNotesLoading &&
    (!note?.links.length || note.status === "error" || note.status === "unsupported")
  ) {
    addCue("no-release-notes", "No release notes", "warning");
  }
}

function addReleaseNoteSecurityCue(
  note: ReleaseNoteInfo | null,
  addCue: (key: string, label: string, type: SafetyCue["type"]) => void,
): void {
  if (note?.security?.outcome === "verified_critical_high") {
    const severity = note.security.severity === "critical" ? "Critical" : "High";
    addCue("verified-security", `Release advisory: ${severity.toLowerCase()}`, "error");
  } else if (note?.security?.outcome === "needs_review") {
    addCue("security-review", "Release advisory: needs review", "warning");
  }
}

function addSecurityScanCues(
  context: Pick<
    SafetyCueContext,
    | "releaseNote"
    | "securityScan"
    | "securityScansCurrent"
    | "securityScansEnabled"
    | "securityScansLoaded"
    | "securityScansLoading"
  >,
  addCue: (key: string, label: string, type: SafetyCue["type"]) => void,
): void {
  if (
    !context.securityScansLoaded ||
    context.securityScansLoading ||
    !context.securityScansEnabled
  ) {
    return;
  }
  if (!context.securityScansCurrent) {
    addCue("security-stale", "Candidate scan: stale", "warning");
    return;
  }
  const scan = context.securityScan;
  if (!scan) {
    addCue("security-unknown", "Candidate scan: unavailable", "warning");
    return;
  }
  const display = securityScanCueDisplay(scan);
  if (display) {
    addCue(display.key, `Candidate scan: ${display.key === "security-stale" ? "stale" : display.label}`, display.type);
  }
}

function addPolicyCues(
  serviceKeys: string[],
  context: Pick<SafetyCueContext, "servicePolicies" | "snoozes">,
  addCue: (key: string, label: string, type: SafetyCue["type"]) => void,
): void {
  if (context.snoozes.some((snooze) => serviceKeys.includes(snooze.service_key))) {
    addCue("snoozed", "Snoozed", "default");
  }
  const policy = context.servicePolicies.find((item) =>
    serviceKeys.includes(item.service_key),
  );
  if (policy?.auto_update) {
    addCue("auto-update", "Auto-update", "success");
  }
}

function parseVersion(tag: string): ParsedVersion | null {
  const match = tag.match(/^v?(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!match) {
    return null;
  }
  const [, majorStr = "0", minorStr = "0", patchStr = "0"] = match;
  return {
    major: parseInt(majorStr, 10),
    minor: parseInt(minorStr, 10),
    patch: parseInt(patchStr, 10),
  };
}
