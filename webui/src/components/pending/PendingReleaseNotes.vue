<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { AlertTriangle, ExternalLink, FileText, ShieldAlert } from "@lucide/vue";
import { NButton, NModal, NTag } from "naive-ui";

import type { ReleaseNoteInfo } from "../../api/client";
import { useUpdatesStore } from "../../stores/updates";
import { safeUrl } from "../../utils/safeUrl";
import ReleaseNotesMarkdown from "./ReleaseNotesMarkdown";
import {
  notificationStatusLabel as releaseNotificationStatusLabel,
  notificationStatusType as releaseNotificationStatusType,
} from "./releaseNotificationStatus";

const props = withDefaults(defineProps<{
  candidateLabel?: string;
  candidateTag?: string;
  releaseNote?: ReleaseNoteInfo | null;
  releaseNoteStatus?: string;
  releaseNoteReason?: string;
}>(), {
  candidateLabel: "update",
  candidateTag: "",
  releaseNote: null,
  releaseNoteStatus: "",
  releaseNoteReason: "",
});

const show = ref(false);
const titleId = useId();
const releaseBody = computed(() => props.releaseNote?.body?.trim() ?? "");
const links = computed(() => (props.releaseNote?.links ?? [])
  .filter((link) => safeUrl(link.url))
  .map((link) => {
    if (link.kind !== "github_release") return link;
    const label = link.label === "Upstream release" ? "Upstream release (GitHub)" : "GitHub release";
    return { ...link, label };
  }),
);
const sourceLinks = computed(() => links.value.filter((link) => link.kind !== "security_advisory"));
const matched = computed(() => {
  const tag = props.candidateTag.replace(/^v(?=\d)/, "");
  const release = (props.releaseNote?.release_tag ?? "").replace(/^v(?=\d)/, "");
  // Matching a version label does not establish that a mutable tag or digest contains it.
  return props.releaseNote?.status === "ready" && /^\d+\.\d+\.\d+(?:[-+].+)?$/.test(tag) && tag === release;
});
const contextLabel = computed(() => matched.value ? "Matched to candidate" : "Upstream context");
const unavailableReason = computed(() => props.releaseNoteReason || props.releaseNote?.error ||
  (props.releaseNoteStatus === "Checking..." ? "Loading release information…" : "Release notes are not available for this candidate."));

function openNotes(): void {
  show.value = true;
  if (!releaseBody.value && canReadChangelog.value) void readChangelog();
}

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
  <div class="candidate-release" :aria-label="`Release information for ${candidateLabel}`">
    <div class="candidate-release-actions">
      <n-button size="small" secondary aria-haspopup="dialog"
        :aria-label="`Release notes for ${candidateLabel}`" @click="openNotes">
        <template #icon><FileText :size="14" aria-hidden="true" /></template>
        Release notes
      </n-button>
      <a v-for="link in sourceLinks" :key="link.url" class="release-note-link"
        :href="link.url" target="_blank" rel="noopener noreferrer">
        {{ link.label }}
        <ExternalLink :size="14" aria-hidden="true" />
      </a>
      <span v-if="releaseNote?.status === 'ready'" class="release-notes-reason">{{ contextLabel }}</span>
      <output v-else class="release-notes-reason">
        <strong v-if="releaseNoteStatus">{{ releaseNoteStatus }}. </strong>{{ unavailableReason }}
      </output>
    </div>
    <n-modal v-model:show="show">
      <!-- Naive UI's focus trap requires a div as the modal content root. -->
      <div class="release-modal">
        <dialog open aria-modal="true" :aria-labelledby="titleId" class="release-panel">
          <header class="release-panel-heading">
            <div>
              <h2 :id="titleId">Release notes</h2>
              <p class="wrap-anywhere">{{ candidateLabel }}</p>
            </div>
            <n-button size="small" @click="show = false">Close release notes</n-button>
          </header>
          <div class="release-panel-content">
            <strong v-if="releaseNote?.release_tag" class="wrap-anywhere">
              {{ releaseNote.release_tag }}<template v-if="releaseNote.title && releaseNote.title !== releaseNote.release_tag"> — {{ releaseNote.title }}</template>
            </strong>
            <p v-if="releaseNote?.release_tag" class="release-context">
              <strong>{{ contextLabel }}.</strong>
              {{ matched ? 'The release version matches this candidate tag.' : 'This release is general upstream information; it is not confirmed for this candidate or digest.' }}
            </p>
            <p v-if="releaseNote?.upstream_repo" class="wrap-anywhere">Source: {{ releaseNote.upstream_repo }}</p>
            <div class="candidate-release-actions">
              <a v-for="link in links" :key="link.url" class="release-note-link"
                :href="link.url" target="_blank" rel="noopener noreferrer">
                {{ link.label }}
                <ExternalLink :size="14" aria-hidden="true" />
              </a>
            </div>
            <div v-if="releaseNote?.breaking" class="release-evidence">
              <strong><AlertTriangle :size="14" aria-hidden="true" /> Possible breaking change</strong>
              <p v-for="reason in releaseNote.breaking_reasons" :key="reason">{{ reason }}</p>
            </div>
            <output v-if="securityVerified || securityNeedsReview" class="release-evidence"
              :aria-label="securityLabel">
              <strong><ShieldAlert :size="14" aria-hidden="true" /> {{ securityLabel }}</strong>
              <span>{{ security?.reason }}</span>
            </output>
            <ReleaseNotesMarkdown v-if="releaseBody" class="release-panel-body" :source="releaseBody" breaks />
            <output v-else-if="!changelogLoading && !changelogReady && !changelogProblem">{{ unavailableReason }}</output>
            <output v-if="changelogLoading">Loading changelog notes…</output>
            <output v-if="changelogProblem" class="release-changelog-problem">{{ changelogProblem }}</output>
            <section v-if="changelogReady">
              <h3>Changelog notes</h3>
              <ReleaseNotesMarkdown class="release-panel-body" :source="changelog.body" :heading-level="4" />
              <a v-if="safeUrl(changelog.sourceUrl)" class="release-note-link" :href="changelog.sourceUrl"
                target="_blank" rel="noopener noreferrer">Changelog source <ExternalLink :size="14" aria-hidden="true" /></a>
            </section>
            <n-button v-if="canReadChangelog && !changelogReady" size="small" secondary
              :loading="changelogLoading" :disabled="changelogLoading" @click="readChangelog">
              {{ readChangelogLabel }}
            </n-button>
            <div v-if="releaseNote" class="release-notes-cell">
              <n-tag size="small" :type="notificationStatusType" :title="notificationStatusDetail || undefined">{{ notificationStatusLabel }}</n-tag>
              <span v-if="notificationStatusDetail" class="release-notes-reason">{{ notificationStatusDetail }}</span>
            </div>
          </div>
        </dialog>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.candidate-release {
  min-width: 0;
}

.candidate-release-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
}

.release-modal {
  border-radius: 8px;
}

.release-panel {
  position: static;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  width: min(680px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-ink);
}

.release-panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  border-bottom: 1px solid var(--color-border-subtle);
}

.release-panel-heading > div {
  min-width: 0;
}

.release-panel-heading h2 {
  margin: 0;
  font-size: var(--text-title-size);
}

.release-panel-heading p {
  margin: 4px 0 0;
  color: var(--color-muted-text);
  font-size: var(--text-metadata-size);
}

.release-panel-content {
  display: grid;
  gap: 12px;
  min-height: 0;
  overflow-y: auto;
  overflow-wrap: anywhere;
  padding: 16px;
}

.release-panel-content p,
.release-panel-content h3 {
  margin: 0;
}

.release-context {
  color: var(--color-text-secondary);
}

.release-evidence {
  display: grid;
  gap: 6px;
}

.release-evidence strong {
  display: flex;
  align-items: center;
  gap: 6px;
}

.release-panel-body {
  display: grid;
  gap: 8px;
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: var(--text-metadata-size);
  line-height: 1.6;
}

.release-panel-body :deep(:is(h3, h4, h5, h6)) {
  margin: 8px 0 0;
  font-size: var(--text-body-size);
}

.release-panel-body :deep(:is(p, ul, ol, blockquote, pre, hr)) {
  margin: 0;
}

.release-panel-body :deep(:is(ul, ol)) {
  padding-inline-start: 20px;
}

.release-panel-body :deep(a) {
  font-weight: inherit;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.release-panel-body :deep(li.task) {
  list-style: none;
}

.release-panel-body :deep(blockquote) {
  padding-inline-start: 12px;
  border-inline-start: 3px solid var(--color-border);
  color: var(--color-text-secondary);
}

.release-panel-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.95em;
}

.release-panel-body :deep(pre) {
  padding: 8px 12px;
  overflow-x: auto;
  border-radius: 6px;
  background: var(--color-panel-tint);
  white-space: pre;
  overflow-wrap: normal;
}

.release-panel-body :deep(hr) {
  border: 0;
  border-top: 1px solid var(--color-border-subtle);
}

.release-panel-body :deep(.release-markdown-table) {
  overflow-x: auto;
}

.release-panel-body :deep(table) {
  border-collapse: collapse;
}

.release-panel-body :deep(:is(th, td)) {
  padding: 4px 8px;
  border: 1px solid var(--color-border-subtle);
  text-align: start;
  overflow-wrap: normal;
}

.release-panel-content section {
  display: grid;
  gap: 8px;
}

@media (--wud-compact) {
  .candidate-release-actions :deep(.n-button),
  .candidate-release-actions a,
  .release-panel :deep(.n-button) {
    min-height: var(--size-touch-target);
  }

  .candidate-release-actions a {
    display: inline-flex;
    align-items: center;
  }

  .release-panel-heading {
    flex-wrap: wrap;
  }
}
</style>
