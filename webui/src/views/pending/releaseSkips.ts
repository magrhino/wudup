import type {
  PendingGroupedItem,
  PendingReleaseKey,
  PendingReleaseSkip,
  PendingStackGroup,
  ReleaseSkipScope,
} from "../../api/client";

export type ReleaseSkipTarget = {
  group: PendingStackGroup;
  item: PendingGroupedItem;
};

export function releaseSkipMatches(item: PendingGroupedItem): boolean {
  return item.release_skip?.matches === true;
}

export function canSkipRelease(item: PendingGroupedItem): boolean {
  return Boolean(item.release_key) && !releaseSkipMatches(item);
}

export function releaseSkipActionLabel(key: PendingReleaseKey): string {
  return `Skip ${key.label}`;
}

export function releaseSkipChoiceLabel(
  key: PendingReleaseKey,
  scope: ReleaseSkipScope,
): string {
  if (key.basis === "version" && scope === "line") {
    return `${key.line}.x`;
  }
  return key.label;
}

export function releaseSkipReturnText(
  key: PendingReleaseKey,
  scope: ReleaseSkipScope,
): string {
  if (key.basis === "digest") {
    return "Returns to Pending when WUD offers a different build.";
  }
  if (key.basis === "version" && scope === "line") {
    return `Returns to Pending when WUD offers a release outside ${key.line}.x, such as ${nextLineExample(key.line)}.`;
  }
  return `Returns to Pending when WUD offers a release other than ${key.label}.`;
}

export function releaseSkipWaitingText(skip: PendingReleaseSkip): string {
  if (skip.basis === "digest") {
    return `Skipped ${skip.label}. Waiting for a different build.`;
  }
  return `Skipped ${skip.label}. Waiting for a different release.`;
}

export function releaseSkipCueText(skip: PendingReleaseSkip): string {
  const reason = skip.reason ? `: "${skip.reason}"` : "";
  return `You skipped ${skip.label} on ${skipDate(skip.created_at)}${reason}. Retest before applying.`;
}

function skipDate(createdAt: string): string {
  return createdAt.slice(0, 10) || "an earlier date";
}

function nextLineExample(line: string): string {
  const [major, minor] = line.split(".");
  const nextMinor = Number(minor) + 1;
  if (!major || !Number.isFinite(nextMinor)) {
    return "the next minor release";
  }
  return `${major}.${nextMinor}`;
}
