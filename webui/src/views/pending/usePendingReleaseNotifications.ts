import { computed, ref } from "vue";

import type { ReleaseNotificationSource } from "../../api/client";
import { useAuthStore } from "../../stores/auth";
import { useUpdatesStore } from "../../stores/updates";

export function usePendingReleaseNotifications() {
  const updates = useUpdatesStore();
  const auth = useAuthStore();
  const showReleaseNotificationModal = ref(false);
  const releaseNotificationSource = ref<ReleaseNotificationSource | null>(null);

  const releaseNotificationsDisabledReason = computed(() => {
    if (updates.releaseNotes?.notifications_enabled === false) {
      return (
        updates.releaseNotes.notifications_disabled_reason ||
        "Release-note notifications are disabled in Settings."
      );
    }
    return "";
  });
  const applyJobReleaseNotificationsVisible = computed(
    () => updates.applyJob?.status === "success" && Boolean(updates.applyJob.run_id),
  );
  const applyJobReleaseNotificationsDisabledMessage = computed(() => {
    if (!applyJobReleaseNotificationsVisible.value) {
      return "";
    }
    if (releaseNotificationsDisabledReason.value) {
      return releaseNotificationsDisabledReason.value;
    }
    if (updates.releaseNotificationLoading) {
      return "Release-note notification preview is loading.";
    }
    return "";
  });
  const applyJobReleaseNotificationsDisabled = computed(
    () =>
      !updates.applyJob?.run_id ||
      updates.releaseNotificationLoading ||
      Boolean(applyJobReleaseNotificationsDisabledMessage.value),
  );
  const releaseNotificationSendDisabledMessage = computed(() => {
    const response = updates.releaseNotification;
    if (!response) {
      return updates.releaseNotificationLoading
        ? ""
        : "Preview release-note notifications before sending.";
    }
    if (response.sent) {
      return "Release-note notifications were sent.";
    }
    if (!response.enabled) {
      return "Release-note notifications are disabled in Settings.";
    }
    if (!response.destination.configured) {
      return "Configure a Discord webhook in Settings or set DISCORD_WEBHOOK in the WebUI runtime.";
    }
    if (auth.session?.mutations_enabled === false) {
      return "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to send notifications.";
    }
    if (!response.sendable_count) {
      if (response.skipped_count) {
        const skippedItems = response.items.filter((item) => item.skipped_reason);
        const duplicatesOnly = skippedItems.every(
          (item) => item.notification_status === "skipped_duplicate",
        );
        return duplicatesOnly
          ? "Duplicate notifications are skipped. Preview resend to send them again."
          : "Release-note notifications are skipped by the resend policy. Preview resend to review them.";
      }
      return "No release-note notifications are available to send.";
    }
    return "";
  });
  const releaseNotificationSendDisabled = computed(
    () =>
      updates.releaseNotificationLoading ||
      releaseNotificationSource.value === null ||
      Boolean(releaseNotificationSendDisabledMessage.value),
  );

  async function previewApplyJobReleaseNotifications(): Promise<void> {
    const runId = updates.applyJob?.run_id;
    if (applyJobReleaseNotificationsDisabled.value || !runId) {
      return;
    }
    await previewReleaseNotifications({ run_id: runId });
  }

  async function previewReleaseNotifications(
    source: ReleaseNotificationSource,
  ): Promise<void> {
    releaseNotificationSource.value = source;
    showReleaseNotificationModal.value = true;
    await updates.previewReleaseNotifications(source).catch(() => undefined);
  }

  async function previewReleaseNotificationResend(): Promise<void> {
    if (releaseNotificationSource.value === null) {
      return;
    }
    const source = {
      ...releaseNotificationSource.value,
      resend: true,
    } as ReleaseNotificationSource;
    await previewReleaseNotifications(source);
  }

  function closeReleaseNotificationModal(): void {
    showReleaseNotificationModal.value = false;
    releaseNotificationSource.value = null;
    updates.clearReleaseNotification();
  }

  async function sendReleaseNotifications(): Promise<void> {
    if (releaseNotificationSendDisabled.value || releaseNotificationSource.value === null) {
      return;
    }
    await updates.sendReleaseNotifications(releaseNotificationSource.value).catch(() => undefined);
  }

  return {
    applyJobReleaseNotificationsDisabled,
    applyJobReleaseNotificationsDisabledMessage,
    applyJobReleaseNotificationsVisible,
    closeReleaseNotificationModal,
    previewApplyJobReleaseNotifications,
    previewReleaseNotificationResend,
    releaseNotificationSendDisabled,
    releaseNotificationSendDisabledMessage,
    sendReleaseNotifications,
    showReleaseNotificationModal,
  };
}
