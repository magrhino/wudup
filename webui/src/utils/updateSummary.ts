import type { PlanResponse, RunSummary, RunVerificationItem } from "../api/client";
import { displayDigest } from "./digestProvenance";

export type ImageChange = { stack: string; service: string; before: string; after: string };

export function uniqueChanges(changes: ImageChange[]): ImageChange[] {
  return [...new Map(changes.map(change => [JSON.stringify(change), change])).values()];
}

export function planChanges(plan: PlanResponse): ImageChange[] {
  return uniqueChanges(plan.stacks.flatMap(stack => stack.lines.map(line => ({
    stack: stack.name,
    service: line.service,
    before: line.compose_image || line.image,
    after: line.target_image || line.resolved_image,
  }))));
}

function isImageUpdate(run: RunSummary): boolean {
  return ["pause", "stop", "live", "apply", "auto-update", "cli", "web-retag"].includes(run.mode);
}

export function runChanges(run: RunSummary): ImageChange[] {
  if (!isImageUpdate(run)) return [];
  const items = run.verification?.items ?? [];
  const matchedItems = new Map(items.filter(item => item.event_id != null).map(item => [item.event_id, item]));
  const eventIds = new Set(run.events.map(event => event.id));
  // Use the backend's match to recover legacy names without guessing identities.
  const events = run.events.map(event => ({
    stack: event.stack_name || matchedItems.get(event.id)?.stack_name || "",
    service: event.service_name || matchedItems.get(event.id)?.service_name || "",
    before: event.image,
    after: event.target_image,
  }));
  const recorded = new Set(events.map(change => JSON.stringify([change.stack, change.service || change.before])));
  // Retain unmatched pending records and events; exact identities also support older APIs.
  const pending = items.filter(item => !(item.event_id != null && eventIds.has(item.event_id))
    && !recorded.has(JSON.stringify([item.stack_name, item.service_name || item.image])));
  return uniqueChanges([...events, ...pending.map(item => ({ stack: item.stack_name, service: item.service_name, before: item.image, after: item.target_image }))]);
}

function imageParts(image: string): { repo: string; version: string } {
  const [reference = "", digest] = image.split("@");
  const colon = reference.lastIndexOf(":");
  const tagged = colon > reference.lastIndexOf("/");
  return {
    repo: tagged ? reference.slice(0, colon) : reference,
    version: digest ? displayDigest(digest) : tagged ? reference.slice(colon + 1) : "tag not recorded",
  };
}

export function imageTransition(change: ImageChange): string {
  if (!change.before || !change.after) return "Image transition not recorded";
  const before = imageParts(change.before);
  const after = imageParts(change.after);
  if (before.repo !== after.repo) return `${change.before} → ${change.after}`;
  if (change.before === change.after) return `${before.version} · same image reference`;
  return `${before.version} → ${after.version}`;
}

export function scopeTitle(changes: ImageChange[], fallback: string): string {
  if (!changes.length) return fallback;
  if (changes.length === 1) {
    const change = changes[0]!;
    return `${change.service || change.stack || "Image update"} · ${imageTransition(change)}`;
  }
  const stacks = new Set(changes.map(change => change.stack || "Unknown stack"));
  const services = new Set(changes.map(change => `${change.stack}\0${change.service || change.before}`));
  const count = `${services.size} ${services.size === 1 ? "service" : "services"}`;
  return stacks.size === 1 ? `${[...stacks][0]} · ${count}` : `${count} across ${stacks.size} stacks`;
}

export type ResultSignal = { text: string; tone: "success" | "warning" | "default" };

function verificationGroups(run: RunSummary): (RunVerificationItem | null)[][] {
  const items = run.verification?.items ?? [];
  const events = new Map(run.events.map(event => [event.id, event]));
  const covered = new Set<number>();
  const groups = new Map<string, (RunVerificationItem | null)[]>();
  const key = (stack: string, service: string, image: string) => JSON.stringify([stack, service || image]);
  const add = (identity: string, item: RunVerificationItem | null) => {
    const group = groups.get(identity) ?? [];
    group.push(item);
    groups.set(identity, group);
  };
  for (const item of items) {
    const event = item.event_id != null ? events.get(item.event_id) : undefined;
    const identity = key(event?.stack_name || item.stack_name, event?.service_name || item.service_name, event?.image || item.image);
    add(identity, item);
    if (event) covered.add(event.id);
    // Older APIs lack correlation IDs: only an unambiguous exact identity covers an event.
    if (item.event_id === undefined) {
      const matches = run.events.filter(candidate => key(candidate.stack_name, candidate.service_name, candidate.image) === identity);
      if (matches.length === 1) covered.add(matches[0]!.id);
    }
  }
  for (const event of run.events) {
    if (!covered.has(event.id)) add(key(event.stack_name, event.service_name, event.image), null);
  }
  return [...groups.values()];
}

export function resultSignals(run: RunSummary): ResultSignal[] {
  if (!isImageUpdate(run)) return [{ text: `Action ${run.status}`, tone: run.status === "success" ? "default" : "warning" }];
  if (run.dry_run) return [{ text: "Dry run · no changes applied", tone: "default" }];
  if (!run.finished_at) return [{ text: "Run in progress · verification pending", tone: "default" }];
  const items = run.verification?.items ?? [];
  if (!items.length) return [{ text: "Verification not recorded", tone: "default" }];
  const groups = verificationGroups(run);
  const total = groups.length;
  const imageKnown = groups.filter(group => group.every(item => item && ["new_image_running", "already_current"].includes(item.image_status))).length;
  const imageFailed = groups.filter(group => group.some(item => item?.image_status === "failed")).length;
  const healthPassed = groups.filter(group => group.every(item => item?.health_status === "passed")).length;
  const healthFailed = groups.filter(group => group.some(item => item && ["failed", "timed_out", "service_disappeared"].includes(item.health_status))).length;
  const healthSkipped = groups.filter(group => group.some(item => item?.health_status === "skipped")
    && group.every(item => item && ["passed", "skipped"].includes(item.health_status))).length;
  const imageUnknown = total - imageKnown - imageFailed;
  const healthUnknown = total - healthPassed - healthFailed - healthSkipped;
  const skippedEvidence = groups.filter(group => group.some(item => item?.health_status === "skipped")).length;
  const skippedSuffix = skippedEvidence ? ` · ${skippedEvidence} skipped` : "";
  const signals: ResultSignal[] = [
    {
      text: imageFailed ? (total === 1 ? "Image needs attention" : `Image needs attention · ${imageFailed}/${total}${imageUnknown ? ` · ${imageUnknown} unknown` : ''}`)
        : imageKnown === total ? (total === 1 ? "Image verified" : `Images verified · ${imageKnown}/${total}`)
        : `Image evidence incomplete · ${imageKnown}/${total} verified`,
      tone: imageKnown === total ? "success" : "warning",
    },
    {
      text: healthFailed ? `${total === 1 ? "Health needs attention" : `Health needs attention · ${healthFailed}/${total}${healthUnknown ? ` · ${healthUnknown} unknown` : ''}`}${skippedSuffix}`
        : healthPassed === total ? "Health checks passed"
        : healthSkipped === total ? "Health checks skipped"
        : healthPassed + healthSkipped === total ? `Health checks · ${healthPassed} passed · ${healthSkipped} skipped`
        : `Health evidence incomplete · ${healthPassed}/${total} passed${skippedSuffix}`,
      tone: healthPassed === total ? "success" : healthPassed + healthSkipped === total ? "default" : "warning",
    },
  ];
  if (items.some(item => item.follow_up_needed)) signals.push({ text: "Follow-up needed", tone: "warning" });
  return signals;
}

export function runAction(run: RunSummary): string {
  const mode = run.mode || "Unknown";
  switch (mode) {
    case "cli": return run.dry_run ? "CLI (dry run)" : "CLI";
    case "apply": return "Apply";
    case "stop": case "pause": case "live": return "Update";
    case "web-retag": return "Version change";
    case "auto-update": return "Auto update";
    case "cleanup": return "Cleanup";
    case "snooze-created": return "Snooze created";
    case "snooze-removed": return "Snooze removed";
    case "service-policy-upserted": return "Policy changed";
    case "service-policy-deleted": return "Policy removed";
    case "tag-exclusion-upserted": return "Tag exclusion saved";
    case "tag-exclusion-status": return "Tag exclusion status changed";
    case "web-auth": return "Web auth";
    case "web-state": return "Web state";
    case "web-pending-cleanup": return "Pending cleanup";
    case "web-pending-removal": return "Pending removal";
    case "web-settings": return "Settings changed";
    case "container-restart": return "Container restarted";
    default: return mode;
  }
}
