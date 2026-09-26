import { computed, type ComputedRef, type Ref } from "vue";

import type { PendingItem } from "../../api/client";
import { useAuthStore } from "../../stores/auth";
import { useUpdatesStore } from "../../stores/updates";
import { pluralize } from "./utils";

type UsePendingRescanOptions = {
  pendingItems: ComputedRef<PendingItem[]>;
  selectedLineNumbers: Ref<number[]>;
  clearPreflight: () => void;
};

export function usePendingRescan(options: UsePendingRescanOptions) {
  const updates = useUpdatesStore();
  const auth = useAuthStore();

  const wudRescanUnavailableMessage = computed(() => {
    if (!updates.pending) {
      return "";
    }
    if (auth.session?.mutations_enabled === false) {
      return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to rescan WUD.";
    }
    const status = updates.pending.wud_api;
    if (!status) {
      return "WUD API status is unavailable.";
    }
    if (!status.available) {
      return status.detail || "WUD API is unavailable.";
    }
    if (status.state === "auth_required") {
      return status.detail || "WUD API requires authentication.";
    }
    if (status.state !== "ready") {
      return status.detail || "WUD API is not ready.";
    }
    return "";
  });
  const wudMetadataUnavailableMessage = computed(() => {
    const status = updates.pending?.wud_api;
    if (!status) {
      return "WUD API status is unavailable.";
    }
    if (!status.metadata_available) {
      return status.detail || "WUD API metadata is unavailable.";
    }
    return "";
  });
  const selectedWudRescanLineNumbers = computed(() => {
    const byLine = new Map(options.pendingItems.value.map((item) => [item.line_no, item]));
    return options.selectedLineNumbers.value.filter((lineNo) =>
      Boolean(byLine.get(lineNo)?.wud_metadata?.id),
    );
  });
  const globalRescanDisabled = computed(
    () => updates.loading || Boolean(wudRescanUnavailableMessage.value),
  );
  const selectedRescanVisible = computed(() => options.selectedLineNumbers.value.length > 0);
  const selectedRescanDisabledMessage = computed(() => {
    if (!options.selectedLineNumbers.value.length) {
      return "";
    }
    if (wudRescanUnavailableMessage.value) {
      return wudRescanUnavailableMessage.value;
    }
    if (wudMetadataUnavailableMessage.value) {
      return wudMetadataUnavailableMessage.value;
    }
    if (!selectedWudRescanLineNumbers.value.length) {
      return "Selected entries do not have WUD container IDs.";
    }
    return "";
  });
  const selectedRescanDisabled = computed(
    () => updates.loading || Boolean(selectedRescanDisabledMessage.value),
  );
  const pendingRescanAlertType = computed(() =>
    updates.pendingRescan?.status !== "success" || updates.pendingRescan?.skipped.length
      ? "warning" : "info",
  );
  const pendingRescanMessage = computed(() => {
    const rescan = updates.pendingRescan;
    if (!rescan) {
      return "";
    }
    if (rescan.status === "blocked") {
      return "WUD scan request did not run. Review current WUD health and request details.";
    }
    const requested = rescan.scope === "all"
      ? "Full WUD scan requested."
      : `WUD rescan requested for ${pluralize(rescan.watched_count, "container")}.`;
    if (rescan.status === "partial") {
      const counts = rescan.scope === "selected"
        ? ` ${pluralize(rescan.requested_count, "selected entry", "selected entries")}; ${pluralize(rescan.watched_count, "container scan request")} sent.`
        : "";
      const skipped = rescan.skipped.length
        ? ` ${pluralize(rescan.skipped.length, "selected entry", "selected entries")} skipped.`
        : "";
      const prefix = rescan.scope === "all" ? `${requested} ` : "";
      return `${prefix}WUD reported a partial scan result.${counts}${skipped} Review request details.`;
    }
    if (rescan.skipped.length) {
      return `${requested} ${pluralize(rescan.skipped.length, "selected entry", "selected entries")} skipped. Review request details.`;
    }
    return `${requested} Waiting for fresh WUD results.`;
  });

  async function rescanAllPending(): Promise<void> {
    if (globalRescanDisabled.value) {
      return;
    }
    options.clearPreflight();
    await updates.rescanPending("all");
  }

  async function rescanSelectedPending(): Promise<void> {
    if (selectedRescanDisabled.value) {
      return;
    }
    options.clearPreflight();
    await updates.rescanPending("selected", options.selectedLineNumbers.value);
  }

  return {
    globalRescanDisabled,
    pendingRescanAlertType,
    pendingRescanMessage,
    rescanAllPending,
    rescanSelectedPending,
    selectedRescanDisabled,
    selectedRescanDisabledMessage,
    selectedRescanVisible,
    wudRescanUnavailableMessage,
  };
}
