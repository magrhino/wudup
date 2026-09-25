import type { PlanLine, PlanResponse, PlanStack, ReleaseNoteInfo, SecurityScanInfo } from "../../api/client";
import { displayDigest } from "../../utils/digestProvenance";
import { pendingMetadataStatus, releaseNoteReason } from "./pendingDisplay";

export function operationalImpact(stack: PlanStack): string {
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
  return `${stack.name}: ${steps.join(" ") || "Execution steps are not recorded. Review the plan details before applying."}`;
}

function repository(image: string): string {
  const ref = image.split("@")[0] || "";
  return ref.lastIndexOf(":") > ref.lastIndexOf("/") ? ref.slice(0, ref.lastIndexOf(":")) : ref;
}

type ReviewEvidence = { supporting: string[]; unresolved: string[] };

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
  const evidence: ReviewEvidence = { supporting: [], unresolved: [] };
  if (digest) evidence.supporting.push(`planned image digest ${displayDigest(digest)}. This is a target, not proof that the image is running.`);
  else evidence.unresolved.push("no exact image digest is confirmed for this planned target.");
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
  if (!matches) return { supporting: [], unresolved: [unavailableReleaseReason(note, loading, error)] };

  const title = note.title && note.title !== note.release_tag ? ` ${note.title}.` : "";
  const evidence: ReviewEvidence = {
    supporting: [`${note.upstream_repo || note.provider || "upstream"} release ${note.release_tag} has the planned version label (upstream context).${title}`],
    unresolved: [],
  };
  if (note.security.outcome === "verified_critical_high") {
    evidence.supporting.push(`release advisory (${note.security.severity}) — ${note.security.reason}`);
  }
  if (note.breaking) {
    evidence.unresolved.push(`release notes flag breaking changes. ${note.breaking_reasons.join(" ") || "Read the release notes before applying."}`);
  }
  if (note.security.outcome === "needs_review") {
    evidence.unresolved.push(`release security evidence needs review. ${note.security.reason}`);
  }
  return evidence;
}

function scanEvidence(target: string, digest: string, scan: SecurityScanInfo | undefined): ReviewEvidence {
  // Never transfer evidence to an override or another digest just because its queue line matches.
  const matches = scan && digest && repository(scan.subject.requested_ref) === repository(target)
    && scanReportsDigest(scan, digest);
  if (!matches) return { supporting: [], unresolved: ["no candidate scan is confirmed for the planned image digest."] };
  if (scan.state !== "complete" || scan.verdict === "unknown") {
    return { supporting: [], unresolved: [`candidate scan is ${scan.state.replaceAll("_", " ")}. ${scan.error_message || "Security evidence is incomplete."}`] };
  }

  const scannedAt = scan.scanned_at ? ` at ${scan.scanned_at}` : "";
  const platform = scan.subject.platform ? ` (${scan.subject.platform})` : "";
  const provenance = `${scan.scanner || "Scanner"}${scannedAt}${platform}`;
  const evidence: ReviewEvidence = { supporting: [], unresolved: [] };
  const comparison = scan.comparison;
  if (comparison.status === "unknown") {
    evidence.supporting.push(`${provenance} reports ${scan.verdict === "none_reported" ? "no findings" : "findings"} for the candidate.`);
    evidence.unresolved.push(`installed-to-candidate comparison is unavailable. ${comparison.message}`);
  } else {
    evidence.supporting.push(`${provenance} comparison — ${comparison.fixed_findings.length} fixed, ${comparison.introduced_findings.length} introduced, ${comparison.remaining_findings.length} remaining findings.`);
  }
  evidence.unresolved.push(...scan.warnings);
  return evidence;
}

export function reviewEvidence(
  plan: PlanResponse,
  notes: ReleaseNoteInfo[],
  scans: SecurityScanInfo[],
  releaseLoading: boolean,
  releaseError: string,
): ReviewEvidence {
  const supporting: string[] = [];
  const unresolved: string[] = [];
  const notesByLine = new Map(notes.map(note => [note.line_no, note]));
  const scansByLine = new Map(scans.map(scan => [scan.line_no, scan]));
  for (const stack of plan.stacks) {
    for (const line of stack.lines) {
      const who = `${stack.name} / ${line.service || "service not recorded"}`;
      const target = line.target_image || line.resolved_image;
      const scan = scansByLine.get(line.line_no);
      const digest = plannedDigest(line, target, scan);
      const targetTag = (line.digest_provenance?.resolved_tag
        || target.split("@")[0]?.slice(repository(target).length + 1) || "").replace(/^v(?=\d)/, "");
      const evidence = [
        imageEvidence(line, digest),
        releaseEvidence(targetTag, notesByLine.get(line.line_no), releaseLoading, releaseError),
        scanEvidence(target, digest, scan),
      ];
      for (const part of evidence) {
        supporting.push(...part.supporting.map(message => `${who}: ${message}`));
        unresolved.push(...part.unresolved.map(message => `${who}: ${message}`));
      }
    }
  }
  return { supporting: [...new Set(supporting)], unresolved: [...new Set(unresolved)] };
}
