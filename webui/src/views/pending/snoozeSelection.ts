import type {
  PendingGroupedItem,
  PendingStackGroup,
  SnoozeRecord,
} from "../../api/client";
import {
  groupedItemServiceKeys,
  uniqueSorted,
  uniqueStrings,
} from "./pendingDisplay";
import { releaseSkipMatches } from "./releaseSkips";

export type SnoozeSelectionRecord = Pick<SnoozeRecord, "active" | "service_key"> &
  Partial<Pick<SnoozeRecord, "kind">>;

export type SnoozedPendingItem = {
  group: PendingStackGroup;
  item: PendingGroupedItem;
};

export type RuntimeDeferredPendingItem = SnoozedPendingItem;

export type SelfUpdatePendingItem = SnoozedPendingItem;

export function activeSnoozedServiceKeys(
  snoozes: SnoozeSelectionRecord[],
): Set<string> {
  return new Set(
    snoozes
      // Release skips hide one release, matched per item by the backend.
      .filter(
        (snooze) =>
          snooze.active && snooze.service_key && snooze.kind !== "release",
      )
      .map((snooze) => snooze.service_key),
  );
}

export function pendingServiceKeysForGroups(
  groups: PendingStackGroup[],
): Set<string> {
  const keys: string[] = [];
  for (const group of groups) {
    for (const item of group.items) {
      keys.push(...groupedItemServiceKeys(group, item));
    }
  }
  return new Set(keys);
}

export function matchingSnoozedServiceKeys(
  snoozedServiceKeys: Set<string>,
  pendingServiceKeys: Set<string>,
): Set<string> {
  return new Set(
    [...snoozedServiceKeys].filter((serviceKey) =>
      pendingServiceKeys.has(serviceKey),
    ),
  );
}

export function snoozedItemsForGroups(
  groups: PendingStackGroup[],
  snoozedServiceKeys: Set<string>,
): SnoozedPendingItem[] {
  return groups.flatMap((group) =>
    group.items
      .filter((item) => itemHasSnoozedService(group, item, snoozedServiceKeys))
      .map((item) => ({ group, item })),
  );
}

export function stackGroupsWithoutSnoozedItems(
  groups: PendingStackGroup[],
  snoozedServiceKeys: Set<string>,
): PendingStackGroup[] {
  return groups
    .map((group) =>
      stackGroupWithItems(
        group,
        group.items.filter(
          (item) => !itemHasSnoozedService(group, item, snoozedServiceKeys),
        ),
      ),
    )
    .filter((group) => group.items.length > 0);
}

export function runtimeDeferredItemsForGroups(
  groups: PendingStackGroup[],
): RuntimeDeferredPendingItem[] {
  return groups.flatMap((group) =>
    group.items
      .filter((item) => item.runtime_state !== "running")
      .map((item) => ({ group, item })),
  );
}

export function stackGroupsWithoutRuntimeDeferredItems(
  groups: PendingStackGroup[],
): PendingStackGroup[] {
  return groups
    .map((group) =>
      stackGroupWithItems(
        group,
        group.items.filter((item) => item.runtime_state === "running"),
      ),
    )
    .filter((group) => group.items.length > 0);
}

export function itemUpdatesWudup(item: PendingGroupedItem): boolean {
  return Boolean(item.self_update_services?.length);
}

export function selfUpdateItemsForGroups(
  groups: PendingStackGroup[],
): SelfUpdatePendingItem[] {
  return groups.flatMap((group) =>
    group.items
      .filter(itemUpdatesWudup)
      .map((item) => ({ group, item })),
  );
}

export function stackGroupsWithoutSelfUpdateItems(
  groups: PendingStackGroup[],
): PendingStackGroup[] {
  return groups
    .map((group) =>
      stackGroupWithItems(
        group,
        group.items.filter((item) => !itemUpdatesWudup(item)),
      ),
    )
    .filter((group) => group.items.length > 0);
}

export function selectableLineNumbersForGroups(
  groups: PendingStackGroup[],
): number[] {
  return uniqueSorted(groups.flatMap((group) => group.line_numbers));
}

function itemHasSnoozedService(
  group: PendingStackGroup,
  item: PendingGroupedItem,
  snoozedServiceKeys: Set<string>,
): boolean {
  if (releaseSkipMatches(item)) {
    return true;
  }
  for (const key of groupedItemServiceKeys(group, item)) {
    if (snoozedServiceKeys.has(key)) {
      return true;
    }
  }
  return false;
}

function stackGroupWithItems(
  group: PendingStackGroup,
  items: PendingGroupedItem[],
): PendingStackGroup {
  const services = uniqueStrings(items.flatMap((item) => item.services));
  return {
    ...group,
    items,
    services,
    services_label: services.length ? services.join(", ") : group.services_label,
    line_numbers: items.map((item) => item.line_no),
  };
}
