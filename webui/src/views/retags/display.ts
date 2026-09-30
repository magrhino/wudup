import type {
  RetagPlanDigestPinUpdate,
  RetagPlanResponse,
  RetagPlanStack,
  RetagPlanTagUpdate,
  RetagTargetItem,
} from "../../api/client";
import { retagRuntimeChoiceWarning } from "../../utils/retagChoices";

export type TagType = "default" | "success" | "warning" | "error" | "info";

const reasonLabels: Record<string, string> = {
  eligible: "Retag available",
  "missing-provenance": "Missing provenance",
  "not-latest-tracking": "Concrete tracking",
  "missing-concrete-tag": "Missing concrete tag",
  "missing-final-image": "Missing final image",
  "invalid-candidate-tag": "Invalid candidate tag",
  "stale-provenance": "Stale provenance",
  "unsupported-tracking-label": "Unsupported label",
};

const reasonDetails: Record<string, string> = {
  eligible: "A concrete target tag is available.",
  "missing-provenance": "No stored digest provenance is available for this service.",
  "not-latest-tracking": "This service already tracks a concrete tag.",
  "missing-concrete-tag": "Stored provenance does not include a concrete tag.",
  "missing-final-image": "Stored provenance is missing a digest or final image.",
  "invalid-candidate-tag": "The proposed tag is not a valid Docker tag value.",
  "stale-provenance": "Stored provenance does not match the current service image.",
  "unsupported-tracking-label": "The tracking label is not a single exact tag.",
};

export function reasonLabel(code: string): string {
  return reasonLabels[code] ?? "Unavailable reason";
}

export function reasonDetail(code: string): string {
  return reasonDetails[code] ?? "The backend did not provide a recognized reason.";
}

export function reasonTagType(item: RetagTargetItem): TagType {
  if (item.retag_available) {
    return "success";
  }
  if (
    item.retag_reason === "stale-provenance" ||
    item.retag_reason === "invalid-candidate-tag"
  ) {
    return "warning";
  }
  return "default";
}

export function trackingTagType(item: RetagTargetItem): TagType {
  return item.tracking_tag === "latest" ? "info" : "default";
}

export function trackingLabel(item: RetagTargetItem): string {
  return item.tracking_tag || "Unknown";
}

export function trackingSourceLabel(item: RetagTargetItem): string {
  return item.tracking_tag_source
    ? `Source: ${item.tracking_tag_source}`
    : "Source unavailable";
}

export function currentTagLabel(item: RetagTargetItem): string {
  return item.current_tag
    ? `Current tag: ${item.current_tag}`
    : "Current tag unavailable";
}

export function candidateLabel(item: RetagTargetItem): string {
  if (item.retag_available || item.proposed_tag) {
    return `latest -> ${item.proposed_tag}`;
  }
  return reasonDetail(item.retag_reason);
}

export function composeLocation(item: RetagTargetItem): string {
  return [item.directory, item.compose_file].filter(Boolean).join("/");
}

export function runtimeStateLabel(item: RetagTargetItem): string {
  if (item.runtime_state === "running") {
    return "Running";
  }
  if (item.runtime_state === "not-running") {
    return "Not running";
  }
  return "Unknown";
}

export function runtimeStateDetail(item: RetagTargetItem): string {
  return item.runtime_state === "running"
    ? "Matching Compose container is running."
    : retagRuntimeChoiceWarning(item);
}

export function runtimeStateTagType(item: RetagTargetItem): TagType {
  if (item.runtime_state === "running") {
    return "success";
  }
  if (item.runtime_state === "not-running") {
    return "warning";
  }
  return "default";
}

export function compareRetagTargets(
  left: RetagTargetItem,
  right: RetagTargetItem,
): number {
  return (
    retagTargetSortRank(left) - retagTargetSortRank(right) ||
    left.stack.localeCompare(right.stack) ||
    left.service.localeCompare(right.service) ||
    left.service_key.localeCompare(right.service_key) ||
    (left.target_id ?? "").localeCompare(right.target_id ?? "")
  );
}

function retagTargetSortRank(item: RetagTargetItem): number {
  const availabilityRank = item.retag_available ? 0 : 3;
  let runtimeRank = 2;
  if (item.runtime_state === "running") {
    runtimeRank = 0;
  } else if (item.runtime_state === "not-running") {
    runtimeRank = 1;
  }
  return availabilityRank + runtimeRank;
}

export function searchableText(item: RetagTargetItem): string {
  return [
    item.service_key,
    item.stack,
    item.service,
    item.image,
    item.image_repo,
    item.current_tag,
    item.tracking_tag,
    item.proposed_tag,
    item.final_image,
    item.candidate_source,
    item.candidate_warning,
    item.candidate_link_label,
    item.candidate_link_url,
    item.runtime_state,
    runtimeStateLabel(item),
    runtimeStateDetail(item),
    item.retag_reason,
    reasonLabel(item.retag_reason),
    reasonDetail(item.retag_reason),
  ]
    .join(" ")
    .toLowerCase();
}

export function planStatusType(plan: RetagPlanResponse | null): TagType {
  if (plan?.status === "ready") {
    return "success";
  }
  if (plan?.status === "blocked") {
    return "error";
  }
  if (plan?.status === "unavailable") {
    return "warning";
  }
  return "default";
}

export function planLocation(stack: {
  directory: string;
  compose_file: string;
}): string {
  return [stack.directory, stack.compose_file].filter(Boolean).join("/");
}

export function retagPlanContextLabel(plan: RetagPlanResponse): string {
  if (plan.stacks.length === 1) {
    return plan.stacks[0].stack || "retag plan";
  }
  if (plan.stacks.length > 1) {
    return `${plan.stacks.length} stacks`;
  }
  return "retag plan";
}

export function retagPlanSourceFile(plan: RetagPlanResponse): string {
  const locations = plan.stacks.map(planLocation).filter(Boolean);
  if (locations.length === 1) {
    return locations[0];
  }
  if (locations.length > 1) {
    return `${locations.length} Compose files`;
  }
  return "Retag plan";
}

export function digestPinSummary(update: RetagPlanDigestPinUpdate): string {
  return `${update.source_image} -> ${update.final_image}`;
}

export type RetagPlanImageUpdate =
  | RetagPlanDigestPinUpdate
  | RetagPlanTagUpdate;

export function retagPlanStackUpdates(
  stack: RetagPlanStack,
): RetagPlanImageUpdate[] {
  return stack.tag_updates?.length ? stack.tag_updates : stack.digest_pin_updates;
}

export function retagUpdateSummary(update: RetagPlanImageUpdate): string {
  return `${update.source_image} -> ${update.final_image}`;
}

export function retagUpdateModeLabel(update: RetagPlanImageUpdate): string {
  return "planned_digest" in update ? "Digest pin" : "Selected tag";
}

export function labelRewriteSummary(update: RetagPlanImageUpdate): string {
  const changes = update.label_rewrites.map(
    (rewrite) =>
      `${rewrite.label_key}: ${rewrite.current_label_value} -> ${rewrite.proposed_label_value}`,
  );
  if (update.transform_label_value) {
    changes.push(`wud.tag.transform: (not set) -> ${update.transform_label_value}`);
  }
  return changes.length ? changes.join("; ") : "No label rewrite";
}

export function pluralize(count: number, noun: string, plural = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : plural}`;
}
