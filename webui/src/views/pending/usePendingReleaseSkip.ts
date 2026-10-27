import { computed, ref } from "vue";

import type { ReleaseSkipScope } from "../../api/client";
import { useSettingsStore } from "../../stores/settings";
import { errorMessage } from "../../stores/storeState";
import { useUpdatesStore } from "../../stores/updates";
import { groupedItemServiceKeys } from "./pendingDisplay";
import type { ReleaseSkipTarget } from "./releaseSkips";

export function usePendingReleaseSkip() {
  const settings = useSettingsStore();
  const updates = useUpdatesStore();
  const releaseSkipTarget = ref<ReleaseSkipTarget | null>(null);
  const releaseSkipError = ref("");
  const releaseSkipSaving = ref(false);
  const showReleaseSkipModal = computed(() => releaseSkipTarget.value !== null);

  function openReleaseSkip(target: ReleaseSkipTarget): void {
    releaseSkipError.value = "";
    releaseSkipTarget.value = target;
  }

  function closeReleaseSkip(): void {
    if (releaseSkipSaving.value) {
      return;
    }
    releaseSkipTarget.value = null;
    releaseSkipError.value = "";
  }

  async function confirmReleaseSkip(
    scope: ReleaseSkipScope,
    reason: string,
  ): Promise<void> {
    const target = releaseSkipTarget.value;
    if (!target || releaseSkipSaving.value) {
      return;
    }
    releaseSkipSaving.value = true;
    releaseSkipError.value = "";
    try {
      await settings.createReleaseSkip(
        {
          serviceKeys: groupedItemServiceKeys(target.group, target.item),
          image: target.item.image,
          targetTag: target.item.desired_tag,
          digest: target.item.digest,
        },
        scope,
        reason,
        // Pending reads the active snooze list, whatever the Snoozes page last showed.
        "active",
      );
    } catch (error_) {
      releaseSkipError.value = `Could not skip this release: ${errorMessage(error_)}`;
      return;
    } finally {
      releaseSkipSaving.value = false;
    }
    releaseSkipTarget.value = null;
    await updates.loadPending().catch(() => undefined);
  }

  return {
    closeReleaseSkip,
    confirmReleaseSkip,
    openReleaseSkip,
    releaseSkipError,
    releaseSkipSaving,
    releaseSkipTarget,
    showReleaseSkipModal,
  };
}
