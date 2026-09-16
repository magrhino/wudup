<script setup lang="ts">
import { computed } from "vue";
import { AlertTriangle, ExternalLink, FileText, ShieldAlert } from "@lucide/vue";
import { NButton, NTag } from "naive-ui";

import type { ReleaseNoteInfo } from "../../api/client";
import { useUpdatesStore } from "../../stores/updates";
import {
  notificationStatusLabel as releaseNotificationStatusLabel,
  notificationStatusType as releaseNotificationStatusType,
} from "./releaseNotificationStatus";

const props = withDefaults(defineProps<{
  releaseNote?: ReleaseNoteInfo | null;
  releaseNoteStatus?: string;
  releaseNoteReason?: string;
}>(), {
  releaseNote: null,
  releaseNoteStatus: "",
  releaseNoteReason: "",
});

const updates = useUpdatesStore();
const changelog = computed(() =>
  updates.releaseChangelogStateFor(props.releaseNote),
);
const canReadChangelog = computed(() =>
  updates.releaseChangelogCanLoad(props.releaseNote),
);
const changelogLoading = computed(() => changelog.value.status === "loading");
const changelogReady = computed(() => changelog.value.status === "ready");
const changelogProblem = computed(() =>
  changelog.value.status === "unavailable" || changelog.value.status === "error"
    ? changelog.value.error
    : "",
);
const readChangelogLabel = computed(() => {
  if (changelogReady.value) return "Changelog loaded";
  return changelog.value.status === "error" ? "Retry changelog" : "Read changelog";
});
const notificationStatusLabel = computed(() =>
  releaseNotificationStatusLabel(props.releaseNote?.notification_status ?? ""),
);
const notificationStatusType = computed(() =>
  releaseNotificationStatusType(props.releaseNote?.notification_status ?? ""),
);
const notificationStatusDetail = computed(() => {
  const reason = props.releaseNote?.notification_skipped_reason ?? "";
  if (reason) {
    return reason;
  }
  const sentAt = props.releaseNote?.notification_last_sent_at ?? "";
  return sentAt ? `Last sent ${sentAt}` : "";
});
const security = computed(() => props.releaseNote?.security);
const securityVerified = computed(
  () => security.value?.outcome === "verified_critical_high",
);
const securityNeedsReview = computed(
  () => security.value?.outcome === "needs_review",
);
const securityLabel = computed(() => {
  if (securityVerified.value) {
    return `Verified ${security.value?.severity === "critical" ? "Critical" : "High"} security update`;
  }
  return "Security update needs review";
});

function readChangelog(): Promise<void> {
  return updates.loadReleaseChangelog(props.releaseNote);
}
</script>

<template>
  <div
    v-if="releaseNote && (securityVerified || securityNeedsReview)"
    class="release-security-assessment"
    role="status"
    :aria-label="securityLabel"
  >
    <n-tag
      size="small"
      :type="securityVerified ? 'error' : 'warning'"
      class="release-security-tag"
    >
      <template #icon>
        <ShieldAlert v-if="securityVerified" :size="14" aria-hidden="true" />
        <AlertTriangle v-else :size="14" aria-hidden="true" />
      </template>
      {{ securityLabel }}
    </n-tag>
    <span class="release-security-reason wrap-anywhere">
      {{ security?.reason }}
    </span>
  </div>
  <div v-if="releaseNote?.links.length" class="release-notes-cell">
    <a
      v-for="link in releaseNote.links"
      :key="`${link.kind}-${link.url}`"
      class="release-note-link"
      :href="link.url"
      target="_blank"
      rel="noopener noreferrer"
    >
      {{ changelogProblem && link.kind === 'github_release' ? 'Open GitHub release' : link.label }}
      <ExternalLink :size="14" aria-hidden="true" />
    </a>
    <span
      v-if="releaseNote.breaking"
      class="release-breaking-cue wrap-anywhere"
      :title="releaseNote.breaking_reasons.join(' ')"
      aria-label="Possible breaking change"
    >
      <AlertTriangle :size="14" aria-hidden="true" />
      Possible breaking change
    </span>
    <n-button
      v-if="canReadChangelog"
      size="tiny"
      secondary
      class="release-changelog-button"
      :loading="changelogLoading"
      :disabled="changelogLoading"
      @click="readChangelog"
    >
      <template #icon>
        <FileText :size="14" aria-hidden="true" />
      </template>
      {{ readChangelogLabel }}
    </n-button>
    <span
      v-if="changelogProblem"
      class="release-notes-reason release-changelog-problem"
      role="status"
    >
      {{ changelogProblem }}
    </span>
    <details v-if="changelogReady" class="release-changelog-details">
      <summary class="release-changelog-summary">
        Changelog notes
      </summary>
      <pre class="release-changelog-body">{{ changelog.body }}</pre>
      <a
        v-if="changelog.sourceUrl"
        class="text-link release-changelog-source"
        :href="changelog.sourceUrl"
        target="_blank"
        rel="noopener noreferrer"
      >
        Source
        <ExternalLink :size="14" aria-hidden="true" />
      </a>
    </details>
  </div>
  <span
    v-else
    class="release-notes-muted wrap-anywhere"
    :title="releaseNoteReason || undefined"
  >
    <span class="release-notes-status wrap-anywhere">
      {{ releaseNoteStatus }}
    </span>
    <span v-if="releaseNoteReason" class="release-notes-reason">
      {{ releaseNoteReason }}
    </span>
  </span>
  <span v-if="releaseNote" class="release-notes-cell">
    <n-tag
      size="small"
      :type="notificationStatusType"
      :title="notificationStatusDetail || undefined"
    >
      {{ notificationStatusLabel }}
    </n-tag>
    <span v-if="notificationStatusDetail" class="release-notes-reason">
      {{ notificationStatusDetail }}
    </span>
  </span>
</template>
