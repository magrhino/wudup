import type { PlanResponse, PlanStack, ReleaseNoteInfo, SecurityScanInfo } from "../../api/client";
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

export function reviewEvidence(
  plan: PlanResponse,
  notes: ReleaseNoteInfo[],
  scans: SecurityScanInfo[],
  releaseLoading: boolean,
  releaseError: string,
): { supporting: string[]; unresolved: string[] } {
  const supporting: string[] = [];
  const unresolved: string[] = [];
  const notesByLine = new Map(notes.map(note => [note.line_no, note]));
  const scansByLine = new Map(scans.map(scan => [scan.line_no, scan]));
  for (const stack of plan.stacks) {
    for (const line of stack.lines) {
      const who = `${stack.name} / ${line.service || "service not recorded"}`;
      const target = line.target_image || line.resolved_image;
      const scan = scansByLine.get(line.line_no);
      const scanDigests = [scan?.subject.reported_digest, scan?.subject.index_digest, scan?.subject.manifest_digest];
      // Tag overrides retain the original WUD digest; only reuse it with evidence for this exact target.
      const digest = target.split("@")[1] || line.digest_provenance?.target_digest
        || (scan?.subject.requested_ref === target && scanDigests.includes(line.digest) ? line.digest : "");
      if (digest) supporting.push(`${who}: planned image digest ${displayDigest(digest)}. This is a target, not proof that the image is running.`);
      else unresolved.push(`${who}: no exact image digest is confirmed for this planned target.`);
      if (pendingMetadataStatus(line) !== "fresh") {
        unresolved.push(`${who}: update metadata is ${pendingMetadataStatus(line)}. Refresh the update information and review again.`);
      }

      const note = notesByLine.get(line.line_no);
      // Release versions provide context; they cannot establish image contents.
      const targetTag = (line.digest_provenance?.resolved_tag
        || target.split("@")[0]?.slice(repository(target).length + 1) || "").replace(/^v(?=\d)/, "");
      const releaseMatches = note?.status === "ready"
        && /^\d+\.\d+\.\d+(?:[-+].+)?$/.test(targetTag)
        && targetTag === note.release_tag.replace(/^v(?=\d)/, "");
      if (releaseMatches) {
        supporting.push(`${who}: ${note.upstream_repo || note.provider || "upstream"} release ${note.release_tag} has the planned version label (upstream context).${note.title && note.title !== note.release_tag ? ` ${note.title}.` : ""}`);
        if (note.security.outcome === "verified_critical_high") {
          supporting.push(`${who}: release advisory (${note.security.severity}) — ${note.security.reason}`);
        }
        if (note.breaking) unresolved.push(`${who}: release notes flag breaking changes. ${note.breaking_reasons.join(" ") || "Read the release notes before applying."}`);
        if (note.security.outcome === "needs_review") unresolved.push(`${who}: release security evidence needs review. ${note.security.reason}`);
      } else {
        const reason = releaseLoading ? "Release information is loading."
          : releaseError || releaseNoteReason(note ?? null)
            || (note?.status === "ready" ? "Available release notes are upstream context, not confirmed for this planned version." : "Release notes are unavailable for this planned version.");
        unresolved.push(`${who}: ${reason}`);
      }

      // Never transfer evidence to an override or another digest just because its queue line matches.
      const scanMatches = scan && digest && repository(scan.subject.requested_ref) === repository(target)
        && scanDigests.includes(digest);
      if (!scanMatches) {
        unresolved.push(`${who}: no candidate scan is confirmed for the planned image digest.`);
      } else if (scan.state !== "complete" || scan.verdict === "unknown") {
        unresolved.push(`${who}: candidate scan is ${scan.state.replaceAll("_", " ")}. ${scan.error_message || "Security evidence is incomplete."}`);
      } else {
        const provenance = `${scan.scanner || "Scanner"}${scan.scanned_at ? ` at ${scan.scanned_at}` : ""}${scan.subject.platform ? ` (${scan.subject.platform})` : ""}`;
        if (scan.comparison.status === "unknown") {
          supporting.push(`${who}: ${provenance} reports ${scan.verdict === "none_reported" ? "no findings" : "findings"} for the candidate.`);
          unresolved.push(`${who}: installed-to-candidate comparison is unavailable. ${scan.comparison.message}`);
        } else {
          const comparison = scan.comparison;
          supporting.push(`${who}: ${provenance} comparison — ${comparison.fixed_findings.length} fixed, ${comparison.introduced_findings.length} introduced, ${comparison.remaining_findings.length} remaining findings.`);
        }
        unresolved.push(...scan.warnings.map(warning => `${who}: ${warning}`));
      }
    }
  }
  return { supporting: [...new Set(supporting)], unresolved: [...new Set(unresolved)] };
}
