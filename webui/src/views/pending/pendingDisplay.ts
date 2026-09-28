import type {
  PendingGroupedItem,
  PendingItem,
  PendingStackGroup,
  ReleaseNoteInfo,
} from "../../api/client";
import type { SafetyCue } from "./safetyCues";

export type PendingTagInputProps = { "aria-label": string };

export function rowKey(row: PendingItem): number {
  return row.line_no;
}

export function displayValue(value: string): string {
  return value || "None";
}

export function pendingMetadataStatus(
  item: Pick<PendingItem, "metadata_status">,
): "fresh" | "retained" | "recovered" {
  return item.metadata_status ?? "fresh";
}

export function pendingMetadataStatusLabel(
  item: Pick<PendingItem, "metadata_status">,
): string {
  const status = pendingMetadataStatus(item);
  return status[0].toUpperCase() + status.slice(1);
}

export function pendingMetadataStatusTitle(
  item: Pick<PendingItem, "metadata_status">,
): string {
  switch (pendingMetadataStatus(item)) {
    case "retained":
      return "Last-known update metadata kept because the current WUD check failed.";
    case "recovered":
      return "Update metadata recovered from the pending file because the current WUD check failed.";
    default:
      return "Verified by the latest WUD scan.";
  }
}

export function pendingMetadataStatusTagType(
  item: Pick<PendingItem, "metadata_status">,
): SafetyCue["type"] {
  return pendingMetadataStatus(item) === "fresh" ? "success" : "warning";
}

export function previewImageLabel(
  value: string,
  displayDigest: (digest: string) => string,
): string {
  return value.includes("sha256:") ? displayDigest(value) : value;
}

function splitImageReference(value: string): { repository: string; reference: string } {
  const at = value.indexOf("@");
  const name = at >= 0 ? value.slice(0, at) : value;
  const digest = at >= 0 ? value.slice(at + 1) : "";
  const colon = name.lastIndexOf(":");
  if (colon <= name.lastIndexOf("/")) {
    return { repository: name, reference: digest };
  }
  const tag = name.slice(colon + 1);
  return { repository: name.slice(0, colon), reference: digest ? `${tag}@${digest}` : tag };
}

// Show only the tag or digest when both sides share a repository, so rows read as version changes.
export function previewChangeLabels(
  current: string,
  target: string,
  displayDigest: (digest: string) => string,
): { current: string; target: string } {
  const from = splitImageReference(current);
  const to = splitImageReference(target);
  if (from.repository !== to.repository || !from.reference || !to.reference) {
    return {
      current: previewImageLabel(current, displayDigest),
      target: previewImageLabel(target, displayDigest),
    };
  }
  return {
    current: previewReferenceLabel(from.reference, displayDigest),
    target: previewReferenceLabel(to.reference, displayDigest),
  };
}

// Shorten only the digest of a pinned "tag@digest" reference so the tag stays readable.
function previewReferenceLabel(
  reference: string,
  displayDigest: (digest: string) => string,
): string {
  const at = reference.indexOf("@");
  if (at <= 0) {
    return previewImageLabel(reference, displayDigest);
  }
  return `${reference.slice(0, at)}@${displayDigest(reference.slice(at + 1))}`;
}

export function uniqueSorted(values: number[]): number[] {
  return [...new Set(values)].sort((left, right) => left - right);
}

export function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort((left, right) =>
    left.localeCompare(right),
  );
}

export function groupedItemServiceKeys(
  group: Pick<PendingStackGroup, "name">,
  item: Pick<PendingGroupedItem, "services">,
): string[] {
  return item.services
    .filter(Boolean)
    .map((service) => `${group.name}/${service}`);
}

export function pendingSourceFileName(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "Pending file") {
    return "Pending file";
  }
  const pathParts = trimmed.split(/[\\/]/);
  for (let index = pathParts.length - 1; index >= 0; index -= 1) {
    const part = pathParts[index];
    if (part) {
      return part;
    }
  }
  return trimmed;
}

export function releaseNoteStatus(
  note: ReleaseNoteInfo | null,
  releaseNotesLoading: boolean,
): string {
  if (note?.links.length) {
    return "";
  }
  if (releaseNotesLoading) {
    return "Checking...";
  }
  if (note?.status === "unsupported") {
    return "Unavailable";
  }
  if (note?.status === "error") {
    return "Check failed";
  }
  return "Not checked";
}

export function releaseNoteReason(note: ReleaseNoteInfo | null): string {
  const error = note?.error.trim() ?? "";
  if (!error) {
    return "";
  }
  const missingMapping = /^missing LSIO upstream mapping for (.+)$/.exec(error);
  if (missingMapping?.[1]) {
    return `Add a LinuxServer.io upstream map entry for ${missingMapping[1]}.`;
  }
  if (error === "no supported GitHub release source found") {
    return "Only GHCR and mapped LinuxServer.io images have release-note links.";
  }
  return error;
}

export function tagInputProps(
  item: Pick<PendingItem, "image">,
): PendingTagInputProps {
  return { "aria-label": `New tag for ${item.image}` };
}

export function itemsBreakingCount(
  items: PendingGroupedItem[],
  releaseNoteFor: (item: PendingGroupedItem) => ReleaseNoteInfo | null,
): number {
  return items.filter((item) => releaseNoteFor(item)?.breaking).length;
}

export function itemsVerifiedSecurityCount(
  items: PendingGroupedItem[],
  releaseNoteFor: (item: PendingGroupedItem) => ReleaseNoteInfo | null,
): number {
  return items.filter(
    (item) => releaseNoteFor(item)?.security?.outcome === "verified_critical_high",
  ).length;
}

export function groupedItemServices(item: PendingGroupedItem): string {
  return item.services.length ? item.services.join(", ") : "stack-level";
}

export function groupedItemTarget(item: PendingGroupedItem): string {
  return item.target_image || item.resolved_image || item.image;
}

export function groupChangePreviewItems(
  group: PendingStackGroup,
): PendingGroupedItem[] {
  return group.items.slice(0, 2);
}

export function groupChangeOverflowCount(group: PendingStackGroup): number {
  return Math.max(0, group.items.length - groupChangePreviewItems(group).length);
}

export function groupedItemActionLabel(item: PendingGroupedItem): string {
  switch (item.action) {
    case "tag-update":
      return "Tag update";
    case "recreate_service":
      return "Recreate service";
    case "recreate_stack":
      return "Recreate stack";
    case "unmatched":
      return "Needs review";
    default:
      return "Image update";
  }
}

export function groupedItemActionTagType(
  item: PendingGroupedItem,
): SafetyCue["type"] {
  switch (item.action) {
    case "tag-update":
    case "recreate_stack":
    case "unmatched":
      return "warning";
    case "recreate_service":
      return "info";
    default:
      return "default";
  }
}

