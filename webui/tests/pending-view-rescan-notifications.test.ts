import { createPinia, setActivePinia } from "pinia";
import { computed, ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PendingItem } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useUpdatesStore } from "../src/stores/updates";
import { usePendingReleaseNotifications } from "../src/views/pending/usePendingReleaseNotifications";
import { usePendingRescan } from "../src/views/pending/usePendingRescan";
import {
  applyJobResponse,
  authSession,
  pendingItem,
  pendingRescanResponse,
  pendingResponse,
  releaseNotesResponse,
  releaseNotificationResponse,
  wudApiStatus,
  wudContainerMetadata,
} from "./helpers/fixtures";

describe("usePendingReleaseNotifications", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("offers apply-job notifications only for a successful run", () => {
    const updates = useUpdatesStore();
    const state = usePendingReleaseNotifications();

    expect(state.applyJobReleaseNotificationsVisible.value).toBe(false);
    expect(state.applyJobReleaseNotificationsDisabledMessage.value).toBe("");
    expect(state.applyJobReleaseNotificationsDisabled.value).toBe(true);

    updates.applyJob = applyJobResponse({ status: "success", run_id: 7 });
    expect(state.applyJobReleaseNotificationsVisible.value).toBe(true);
    expect(state.applyJobReleaseNotificationsDisabled.value).toBe(false);

    updates.releaseNotificationLoading = true;
    expect(state.applyJobReleaseNotificationsDisabledMessage.value).toBe(
      "Release-note notification preview is loading.",
    );
    expect(state.applyJobReleaseNotificationsDisabled.value).toBe(true);

    updates.releaseNotificationLoading = false;
    updates.releaseNotes = {
      ...releaseNotesResponse(),
      notifications_enabled: false,
      notifications_disabled_reason: "",
    };
    expect(state.applyJobReleaseNotificationsDisabledMessage.value).toBe(
      "Release-note notifications are disabled in Settings.",
    );

    updates.releaseNotes = {
      ...releaseNotesResponse(),
      notifications_enabled: false,
      notifications_disabled_reason: "Notifications are paused.",
    };
    expect(state.applyJobReleaseNotificationsDisabledMessage.value).toBe(
      "Notifications are paused.",
    );
  });

  it("explains why sending is disabled", () => {
    const updates = useUpdatesStore();
    const auth = useAuthStore();
    const state = usePendingReleaseNotifications();

    expect(state.releaseNotificationSendDisabledMessage.value).toBe(
      "Preview release-note notifications before sending.",
    );
    updates.releaseNotificationLoading = true;
    expect(state.releaseNotificationSendDisabledMessage.value).toBe("");
    updates.releaseNotificationLoading = false;

    updates.releaseNotification = releaseNotificationResponse({ sent: true });
    expect(state.releaseNotificationSendDisabledMessage.value).toBe(
      "Release-note notifications were sent.",
    );

    updates.releaseNotification = releaseNotificationResponse({ enabled: false });
    expect(state.releaseNotificationSendDisabledMessage.value).toBe(
      "Release-note notifications are disabled in Settings.",
    );

    updates.releaseNotification = releaseNotificationResponse({
      destination: { type: "discord", configured: false, source: "" },
    });
    expect(state.releaseNotificationSendDisabledMessage.value).toContain(
      "Configure a Discord webhook",
    );

    updates.releaseNotification = releaseNotificationResponse();
    auth.session = authSession({ mutations_enabled: false });
    expect(state.releaseNotificationSendDisabledMessage.value).toContain(
      "Read-only mode is active.",
    );

    auth.session = authSession({ mutations_enabled: true });
    expect(state.releaseNotificationSendDisabledMessage.value).toBe("");

    const item = releaseNotificationResponse().items[0];
    updates.releaseNotification = releaseNotificationResponse({
      sendable_count: 0,
      skipped_count: 1,
      items: [{ ...item, skipped_reason: "duplicate", notification_status: "skipped_duplicate" }],
    });
    expect(state.releaseNotificationSendDisabledMessage.value).toBe(
      "Duplicate notifications are skipped. Preview resend to send them again.",
    );

    updates.releaseNotification = releaseNotificationResponse({
      sendable_count: 0,
      skipped_count: 1,
      items: [{ ...item, skipped_reason: "cooldown", notification_status: "skipped_cooldown" }],
    });
    expect(state.releaseNotificationSendDisabledMessage.value).toContain(
      "skipped by the resend policy",
    );

    updates.releaseNotification = releaseNotificationResponse({
      sendable_count: 0,
      skipped_count: 0,
    });
    expect(state.releaseNotificationSendDisabledMessage.value).toBe(
      "No release-note notifications are available to send.",
    );
  });

  it("previews, resends, sends, and closes through the updates store", async () => {
    const updates = useUpdatesStore();
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const preview = vi
      .spyOn(updates, "previewReleaseNotifications")
      .mockResolvedValue(releaseNotificationResponse());
    const send = vi
      .spyOn(updates, "sendReleaseNotifications")
      .mockRejectedValue(new Error("delivery failed"));
    const clear = vi.spyOn(updates, "clearReleaseNotification");
    const state = usePendingReleaseNotifications();

    await state.previewApplyJobReleaseNotifications();
    await state.previewReleaseNotificationResend();
    await state.sendReleaseNotifications();
    expect(preview).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();

    updates.applyJob = applyJobResponse({ status: "success", run_id: 7 });
    await state.previewApplyJobReleaseNotifications();
    expect(preview).toHaveBeenLastCalledWith({ run_id: 7 });
    expect(state.showReleaseNotificationModal.value).toBe(true);

    await state.previewReleaseNotificationResend();
    expect(preview).toHaveBeenLastCalledWith({ run_id: 7, resend: true });

    updates.releaseNotification = releaseNotificationResponse();
    expect(state.releaseNotificationSendDisabled.value).toBe(false);
    await expect(state.sendReleaseNotifications()).resolves.toBeUndefined();
    expect(send).toHaveBeenCalledWith({ run_id: 7, resend: true });

    state.closeReleaseNotificationModal();
    expect(state.showReleaseNotificationModal.value).toBe(false);
    expect(state.releaseNotificationSendDisabled.value).toBe(true);
    expect(clear).toHaveBeenCalledTimes(1);
  });
});

describe("usePendingRescan", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  function setup(items: PendingItem[] = [pendingItem()], selected: number[] = []) {
    const updates = useUpdatesStore();
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    updates.pending = pendingResponse(items);
    const selectedLineNumbers = ref(selected);
    const clearPreflight = vi.fn();
    const rescan = vi.spyOn(updates, "rescanPending").mockResolvedValue(undefined);
    const state = usePendingRescan({
      pendingItems: computed(() => updates.pending?.items ?? []),
      selectedLineNumbers,
      clearPreflight,
    });
    return { updates, auth, state, selectedLineNumbers, clearPreflight, rescan };
  }

  it("explains why WUD rescans are unavailable", () => {
    const { updates, auth, state } = setup();
    expect(state.wudRescanUnavailableMessage.value).toBe("");
    expect(state.globalRescanDisabled.value).toBe(false);

    auth.session = authSession({ mutations_enabled: false });
    expect(state.wudRescanUnavailableMessage.value).toContain("Read-only mode is active.");
    auth.session = authSession({ mutations_enabled: true });

    const pending = updates.pending!;
    updates.pending = { ...pending, wud_api: undefined as never };
    expect(state.wudRescanUnavailableMessage.value).toBe("WUD API status is unavailable.");

    updates.pending = { ...pending, wud_api: wudApiStatus({ available: false, detail: "" }) };
    expect(state.wudRescanUnavailableMessage.value).toBe("WUD API is unavailable.");

    updates.pending = { ...pending, wud_api: wudApiStatus({ state: "auth_required", detail: "" }) };
    expect(state.wudRescanUnavailableMessage.value).toBe("WUD API requires authentication.");

    updates.pending = { ...pending, wud_api: wudApiStatus({ state: "degraded" as never, detail: "" }) };
    expect(state.wudRescanUnavailableMessage.value).toBe("WUD API is not ready.");
    expect(state.globalRescanDisabled.value).toBe(true);

    updates.pending = null;
    expect(state.wudRescanUnavailableMessage.value).toBe("");
  });

  it("gates selected rescans on WUD metadata and container IDs", async () => {
    const withId = pendingItem({ line_no: 1, wud_metadata: wudContainerMetadata() });
    const withoutId = pendingItem({ line_no: 2, wud_metadata: null });
    const { updates, state, selectedLineNumbers, clearPreflight, rescan } = setup(
      [withId, withoutId],
      [],
    );

    expect(state.selectedRescanVisible.value).toBe(false);
    expect(state.selectedRescanDisabledMessage.value).toBe("");

    selectedLineNumbers.value = [2];
    expect(state.selectedRescanVisible.value).toBe(true);
    expect(state.selectedRescanDisabledMessage.value).toBe(
      "Selected entries do not have WUD container IDs.",
    );
    await state.rescanSelectedPending();
    expect(rescan).not.toHaveBeenCalled();

    const pending = updates.pending!;
    updates.pending = {
      ...pending,
      wud_api: wudApiStatus({ metadata_available: false, detail: "" }),
    };
    expect(state.selectedRescanDisabledMessage.value).toBe("WUD API metadata is unavailable.");
    updates.pending = pending;

    selectedLineNumbers.value = [1, 2];
    expect(state.selectedRescanDisabled.value).toBe(false);
    await state.rescanSelectedPending();
    expect(clearPreflight).toHaveBeenCalledTimes(1);
    expect(rescan).toHaveBeenCalledWith("selected", [1, 2]);

    await state.rescanAllPending();
    expect(clearPreflight).toHaveBeenCalledTimes(2);
    expect(rescan).toHaveBeenLastCalledWith("all");
  });

  it("summarizes rescan results", () => {
    const { updates, state } = setup();
    expect(state.pendingRescanMessage.value).toBe("");

    updates.pendingRescan = pendingRescanResponse({ scope: "all" });
    expect(state.pendingRescanAlertType.value).toBe("info");
    expect(state.pendingRescanMessage.value).toBe(
      "Full WUD scan requested. Waiting for fresh WUD results.",
    );

    updates.pendingRescan = pendingRescanResponse({ status: "blocked" });
    expect(state.pendingRescanAlertType.value).toBe("warning");
    expect(state.pendingRescanMessage.value).toContain("WUD scan request did not run.");

    const skipped = [{ line_no: 2, reason: "missing_container_id", detail: "" }] as never;
    updates.pendingRescan = pendingRescanResponse({
      scope: "selected",
      requested_count: 2,
      watched_count: 1,
      skipped,
    });
    expect(state.pendingRescanAlertType.value).toBe("warning");
    expect(state.pendingRescanMessage.value).toBe(
      "WUD rescan requested for 1 container. 1 selected entry skipped. Review request details.",
    );

    updates.pendingRescan = pendingRescanResponse({
      status: "partial",
      scope: "selected",
      requested_count: 2,
      watched_count: 1,
      skipped,
    });
    expect(state.pendingRescanMessage.value).toBe(
      "WUD reported a partial scan result. 2 selected entries; 1 container scan request sent. 1 selected entry skipped. Review request details.",
    );

    updates.pendingRescan = pendingRescanResponse({ status: "partial", scope: "all" });
    expect(state.pendingRescanMessage.value).toBe(
      "Full WUD scan requested. WUD reported a partial scan result. Review request details.",
    );
  });
});
