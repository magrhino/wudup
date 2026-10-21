import type { PlanLine, PlanResponse, PlanStack, ReleaseNoteInfo, SecurityScanInfo } from "../../api/client";
import { displayDigest } from "../../utils/digestProvenance";
import { pendingMetadataStatus, releaseNoteReason } from "./pendingDisplay";

function operationalSteps(stack: PlanStack): string {
  const scope = stack.services.length ? stack.services.join(", ") : "all services in this stack";
  const has = (kind: string) => stack.actions.some(action => action.kind === kind);
  const steps: string[] = [];
  if (has("pull")) steps.push(`Pull images for ${stack.pull_services.join(", ") || "all services in this stack"}.`);
  if (has("pause")) steps.push(`Pause ${scope}.`);
  if (has("stop")) steps.push(`Stop ${stack.stop_services.join(", ") || "all services in this stack"}.`);
  if (has("up")) {
    steps.push(`${stack.force_recreate ? "Force-recreate" : "Start or recreate changed services:"} ${scope}. Service interruption is possible.`);
    if (stack.services.length && !stack.up_no_deps) steps.push("Compose may also start dependencies.");
    if (stack.actions.some(action => action.kind === "up" && action.args.includes("--remove-orphans"))) {
      steps.push("Compose may remove orphan containers in this stack.");
    }
  }
  if (has("unpause")) steps.push(`Unpause ${scope}.`);
  if (has("health-wait") || stack.actions.some(action => action.kind === "up" && action.args.includes("--wait"))) {
    steps.push("Health checks are planned after recreation; results are not available yet.");
  }
  return steps.join(" ") || "Execution steps are not recorded. Review the plan details before applying.";
}

export function operationalImpact(stack: PlanStack): string {
  return `${stack.name}: ${operationalSteps(stack)}`;
}

export type StackImpact = {
  mode: "recreate" | "force-recreate" | "pause";
  wholeStack: boolean;
  // Services interrupted without an image update of their own, such as network providers or label-driven stack recreates.
  alsoRestarted: string[];
  dependenciesMayStart: boolean;
};

// Structured impact for a recreate or pause plan; null when the plan has no short form to trust.
export function stackImpact(stack: PlanStack): StackImpact | null {
  const has = (kind: string) => stack.actions.some(action => action.kind === kind);
  const mode = has("up") ? (stack.force_recreate ? "force-recreate" : "recreate") : has("pause") ? "pause" : null;
  if (!mode) return null;
  const updated = new Set(stack.lines.map(line => line.service));
  const interrupted = [...stack.services, ...(has("stop") ? stack.stop_services : [])];
  return {
    mode,
    wholeStack: !stack.services.length,
    alsoRestarted: [...new Set(interrupted.filter(service => !updated.has(service)))].sort(),
    dependenciesMayStart: has("up") && !!stack.services.length && !stack.up_no_deps,
  };
}

// A plain recreate of the updated services is what the services list already implies.
export function stackImpactIsNotable(impact: StackImpact): boolean {
  return impact.mode !== "recreate" || impact.wholeStack || !!impact.alsoRestarted.length || impact.dependenciesMayStart;
}

// Header label for services interrupted without their own update; "interrupts" covers both restart and pause.
export function extraInterruptionLabel(plan: PlanResponse): string {
  const count = plan.stacks.reduce((total, stack) => total + (stackImpact(stack)?.alsoRestarted.length ?? 0), 0);
  return count ? `Also interrupts ${count} ${count === 1 ? "service" : "services"}` : "";
}

// Text fallback for stacks without a structured impact; the services list shows the rest.
export function impactSummary(stack: PlanStack, showStack: boolean): string {
  const prefix = showStack ? `${stack.name}: ` : "";
  if (!stack.actions.length) return `${prefix}Execution steps are not recorded for this plan.`;
  return `${prefix}${operationalSteps(stack)}`;
}

function repository(image: string): string {
  const ref = image.split("@")[0] || "";
  return ref.lastIndexOf(":") > ref.lastIndexOf("/") ? ref.slice(0, ref.lastIndexOf(":")) : ref;
}

// unresolved: needs the operator's attention before applying. gaps: evidence WUDup could not gather.
type ReviewEvidence = { supporting: string[]; unresolved: string[]; gaps: string[] };

function scanReportsDigest(scan: SecurityScanInfo, digest: string): boolean {
  const digests = new Set([scan.subject.reported_digest, scan.subject.index_digest, scan.subject.manifest_digest]);
  return digests.has(digest);
}

function plannedDigest(line: PlanLine, target: string, scan: SecurityScanInfo | undefined): string {
  const explicitDigest = target.split("@")[1] || line.digest_provenance?.target_digest;
  if (explicitDigest) return explicitDigest;
  // Tag overrides retain the original WUD digest; only reuse it with evidence for this exact target.
  if (scan?.subject.requested_ref === target && scanReportsDigest(scan, line.digest)) return line.digest;
  return "";
}

function imageEvidence(line: PlanLine, digest: string): ReviewEvidence {
  const evidence: ReviewEvidence = { supporting: [], unresolved: [], gaps: [] };
  // Tag-based updates rarely carry a digest, so only a known digest is worth a line.
  if (digest) evidence.supporting.push(`planned image digest ${displayDigest(digest)}. This is a target, not proof that the image is running.`);
  const metadataStatus = pendingMetadataStatus(line);
  if (metadataStatus !== "fresh") {
    evidence.unresolved.push(`update metadata is ${metadataStatus}. Refresh the update information and review again.`);
  }
  return evidence;
}

function unavailableReleaseReason(note: ReleaseNoteInfo | undefined, loading: boolean, error: string): string {
  if (loading) return "Release information is loading.";
  const reason = error || releaseNoteReason(note ?? null);
  if (reason) return reason;
  return note?.status === "ready"
    ? "Available release notes are upstream context, not confirmed for this planned version."
    : "Release notes are unavailable for this planned version.";
}

function releaseEvidence(
  targetTag: string, note: ReleaseNoteInfo | undefined, loading: boolean, error: string,
): ReviewEvidence {
  // Release versions provide context; they cannot establish image contents.
  const matches = note?.status === "ready"
    && /^\d+\.\d+\.\d+(?:[-+].+)?$/.test(targetTag)
    && targetTag === note.release_tag.replace(/^v(?=\d)/, "");
  if (!matches) {
    // Loading stays visible: breaking or security flags may still arrive.
    const reason = unavailableReleaseReason(note, loading, error);
    return loading ? { supporting: [], unresolved: [reason], gaps: [] } : { supporting: [], unresolved: [], gaps: [reason] };
  }

  const title = note.title && note.title !== note.release_tag ? ` ${note.title}.` : "";
  const evidence: ReviewEvidence = {
    supporting: [`${note.upstream_repo || note.provider || "upstream"} release ${note.release_tag} has the planned version label (upstream context).${title}`],
    unresolved: [],
    gaps: [],
  };
  if (note.security.outcome === "verified_critical_high") {
    evidence.unresolved.push(`release advisory (${note.security.severity}) — ${note.security.reason}`);
  }
  if (note.breaking) {
    evidence.unresolved.push(`release notes flag breaking changes. ${note.breaking_reasons.join(" ") || "Read the release notes before applying."}`);
  }
  if (note.security.outcome === "needs_review") {
    // The reason carries severity (e.g. a High advisory vs. security wording only), so keep it visible.
    evidence.unresolved.push(`release notes mention security changes that need review. ${note.security.reason}`.trim());
  }
  return evidence;
}

// States where no scan of this candidate was attempted; the review stays silent about security.
const UNSCANNED_STATES = new Set(["disabled", "not_scanned", "unsupported"]);

function scanEvidence(target: string, digest: string, scan: SecurityScanInfo | undefined): ReviewEvidence {
  // Never transfer evidence to an override or another digest just because its queue line matches.
  const matches = scan && digest && repository(scan.subject.requested_ref) === repository(target)
    && scanReportsDigest(scan, digest);
  if (!matches || UNSCANNED_STATES.has(scan.state)) return { supporting: [], unresolved: [], gaps: [] };
  if (scan.state !== "complete" || scan.verdict === "unknown") {
    return { supporting: [], unresolved: [], gaps: [`candidate scan is ${scan.state.replaceAll("_", " ")}. ${scan.error_message || "Security evidence is incomplete."}`] };
  }

  const scannedAt = scan.scanned_at ? ` at ${scan.scanned_at}` : "";
  const platform = scan.subject.platform ? ` (${scan.subject.platform})` : "";
  const provenance = `${scan.scanner || "Scanner"}${scannedAt}${platform}`;
  const evidence: ReviewEvidence = { supporting: [], unresolved: [], gaps: [] };
  const comparison = scan.comparison;
  if (comparison.status === "unknown") {
    evidence.supporting.push(`${provenance} reports ${scan.verdict === "none_reported" ? "no findings" : "findings"} for the candidate.`);
    evidence.gaps.push(`installed-to-candidate comparison is unavailable. ${comparison.message}`);
  } else {
    const counts = `${provenance} comparison — ${comparison.fixed_findings.length} fixed, ${comparison.introduced_findings.length} introduced, ${comparison.remaining_findings.length} remaining findings.`;
    // Findings the running image does not have are the ones that can change the apply decision.
    (comparison.introduced_findings.length ? evidence.unresolved : evidence.supporting).push(counts);
  }
  evidence.gaps.push(...scan.warnings);
  return evidence;
}

export function reviewEvidence(
  plan: PlanResponse,
  notes: ReleaseNoteInfo[],
  // null: scanning is off, or the scan request is loading or failed, so per-line scan evidence is skipped.
  scans: SecurityScanInfo[] | null,
  releaseLoading: boolean,
  releaseError: string,
): ReviewEvidence {
  const supporting: string[] = [];
  const unresolved: string[] = [];
  const gaps: string[] = [];
  // Name the service only when the selection has more than one.
  const named = plan.stacks.reduce((count, stack) => count + stack.lines.length, 0) > 1;
  const notesByLine = new Map(notes.map(note => [note.line_no, note]));
  const scansByLine = new Map((scans ?? []).map(scan => [scan.line_no, scan]));
  for (const stack of plan.stacks) {
    for (const line of stack.lines) {
      const who = `${stack.name} / ${line.service || "service not recorded"}`;
      const label = (message: string) => named ? `${who}: ${message}` : message.charAt(0).toUpperCase() + message.slice(1);
      const target = line.target_image || line.resolved_image;
      const scan = scansByLine.get(line.line_no);
      const digest = plannedDigest(line, target, scan);
      const targetTag = (line.digest_provenance?.resolved_tag
        || target.split("@")[0]?.slice(repository(target).length + 1) || "").replace(/^v(?=\d)/, "");
      const evidence = [
        imageEvidence(line, digest),
        releaseEvidence(targetTag, notesByLine.get(line.line_no), releaseLoading, releaseError),
        ...(scans ? [scanEvidence(target, digest, scan)] : []),
      ];
      for (const part of evidence) {
        supporting.push(...part.supporting.map(label));
        unresolved.push(...part.unresolved.map(label));
        gaps.push(...part.gaps.map(label));
      }
    }
  }
  return { supporting: [...new Set(supporting)], unresolved: [...new Set(unresolved)], gaps: [...new Set(gaps)] };
}
