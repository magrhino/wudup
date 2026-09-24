<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { AlertTriangle, CircleCheck, RefreshCw, Search } from "@lucide/vue";
import { NAlert, NButton, NCheckbox, NInput, NSelect, NTag } from "naive-ui";
import { RouterLink, useRoute } from "vue-router";

import type { TrackedContainerItem } from "../api/client";
import { useRouteRefresh } from "../components/app/routeRefresh";
import { prefersReducedMotion } from "../responsive";
import { useAuthStore } from "../stores/auth";
import { useTrackingStore } from "../stores/tracking";
import { runInBackground } from "../utils/promises";
import { explainTrackingPattern } from "./trackingPattern";

const route = useRoute();
const auth = useAuthStore();
const tracking = useTrackingStore();
const search = ref(typeof route.query.search === "string" ? route.query.search : "");
watch(() => route.query.search, (value) => {
  search.value = typeof value === "string" ? value : "";
});
const filter = ref("all");
const selectedId = ref("");
const editor = ref("");
const sampleTag = ref("");
const approved = ref(false);
const detailRef = ref<HTMLElement | null>(null);
const inventoryRef = ref<HTMLElement | null>(null);
const planRef = ref<HTMLElement | null>(null);
const applyStatusRef = ref<HTMLElement | null>(null);
const appliedTargetId = ref("");
const previousJobId = ref("");
let inspectTrigger: HTMLElement | null = null;
const isDemoMode = import.meta.env.MODE === "demo" || import.meta.env.VITE_WUD_DEMO_MODE === "true";

const filterOptions = computed(() => [
  { label: "All services", value: "all" },
  { label: "Needs review", value: "attention" },
  ...(isDemoMode ? [] : [
    { label: "WUD update available", value: "update" },
    { label: "Not in WUD", value: "untracked" },
    { label: "WUD status unknown", value: "unknown" },
  ]),
]);

const items = computed(() => tracking.inventory?.items ?? []);
const wudInventoryReady = computed(() => tracking.inventory?.wud_status?.metadata_available === true);
const selected = computed(() => items.value.find((item) => item.target_id === selectedId.value) ?? null);
const inlineJob = computed(() =>
  tracking.job?.job_id === previousJobId.value ? null : tracking.job,
);
const showInlineRepairStatus = computed(() =>
  appliedTargetId.value === selectedId.value && Boolean(appliedTargetId.value) &&
  (tracking.applying || Boolean(tracking.applyError) || Boolean(inlineJob.value)),
);
const showGlobalJobStatus = computed(() => !showInlineRepairStatus.value || !inlineJob.value);
const frozenCount = computed(() => items.value.filter((item) => item.tracking_health === "frozen").length);
const untrackedCount = computed(() => items.value.filter((item) => item.wud_match_state === "untracked").length);
function hasUnknownWudStatus(item: TrackedContainerItem): boolean {
  return ["unknown", "ambiguous"].includes(item.wud_match_state) ||
    (item.wud_match_state === "watching" && item.wud_update_available === null);
}
const unknownCount = computed(() => items.value.filter(hasUnknownWudStatus).length);
const updateCount = computed(() => items.value.filter((item) => item.wud_update_available === true).length);
const filtered = computed(() => {
  const query = search.value.trim().toLowerCase();
  return items.value.filter((item) => {
    if (filter.value === "attention" && !["frozen", "digest-pinned", "custom", "no-image", "image-unresolved"].includes(item.tracking_health)) return false;
    if (filter.value === "update" && item.wud_update_available !== true) return false;
    if (filter.value === "untracked" && item.wud_match_state !== "untracked") return false;
    if (filter.value === "unknown" && !hasUnknownWudStatus(item)) return false;
    return !query || [item.service_key, item.image, item.current_tag, item.tracking_regex, item.wud?.name ?? ""]
      .some((value) => value.toLowerCase().includes(query));
  });
});
const canPreview = computed(() =>
  !isDemoMode &&
  selected.value?.runtime_state === "running" &&
  !["digest-pinned", "no-image", "image-unresolved"].includes(selected.value?.tracking_health ?? "") &&
  Boolean(editor.value.trim()) &&
  patternGuide.value?.matches(selected.value?.current_tag ?? "") === true &&
  editor.value.trim() !== selected.value?.tracking_regex,
);
const sameAsCurrent = computed(() => Boolean(editor.value.trim()) && editor.value.trim() === selected.value?.tracking_regex);
const patternGuide = computed(() => explainTrackingPattern(editor.value.trim(), selected.value?.current_tag));
const sampleMatch = computed(() => patternGuide.value?.matches(sampleTag.value) ?? null);
const samplePlaceholder = computed(() => {
  const example = patternGuide.value?.examples.find((item) => item.matches)?.tag || selected.value?.current_tag;
  return example ? `e.g. ${example}` : "Enter a tag to test";
});
const observedCandidate = computed(() =>
  selected.value?.wud_update_available === true ? selected.value.wud?.remote_tag || "" : "",
);

function matchLabel(tag: string): string {
  const result = patternGuide.value?.matches(tag);
  return result === null || result === undefined ? "Cannot test" : result ? "Matches" : "Does not match";
}

watch(selected, (item) => {
  editor.value = item?.suggested_regex || item?.tracking_regex || "";
  sampleTag.value = "";
  approved.value = false;
  tracking.clearPlan();
});
watch(editor, () => {
  approved.value = false;
  tracking.clearPlan();
});

onMounted(() => runInBackground(tracking.load()));
useRouteRefresh(tracking.load);

async function select(item: TrackedContainerItem, event: MouseEvent): Promise<void> {
  inspectTrigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  appliedTargetId.value = "";
  selectedId.value = item.target_id;
  await nextTick();
  detailRef.value?.focus({ preventScroll: true });
  detailRef.value?.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "start",
  });
}

async function closeDetails(): Promise<void> {
  appliedTargetId.value = "";
  selectedId.value = "";
  await nextTick();
  const target = inspectTrigger?.isConnected ? inspectTrigger : inventoryRef.value;
  target?.focus({ preventScroll: true });
  target?.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "center",
  });
  inspectTrigger = null;
}

function dateLabel(value: string): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function trackingTagType(item: TrackedContainerItem): "warning" | "error" | "success" | "default" {
  if (item.tracking_health === "frozen") return "error";
  if (["digest-pinned", "custom", "no-image", "image-unresolved"].includes(item.tracking_health)) return "warning";
  if (item.tracking_health === "version-pattern") return "success";
  return "default";
}

function trackingLabel(item: TrackedContainerItem): string {
  return {
    frozen: "Frozen filter",
    "exact-tag": "Exact tag",
    "digest-pinned": "Digest pinned",
    "version-pattern": "Version pattern",
    custom: "Custom filter",
    "no-filter": "No filter",
    "no-image": "No image",
    "image-unresolved": "Image unknown",
  }[item.tracking_health] ?? "Review";
}

function wudLabel(item: TrackedContainerItem): string {
  if (isDemoMode) return "Not in demo sample";
  if (item.wud_match_state === "unknown") return "Unknown";
  if (item.wud_match_state === "ambiguous") return "Ambiguous";
  if (item.wud_match_state === "untracked") return "Not seen";
  if (item.wud_update_available === null) return "Update unknown";
  return item.wud_update_available
    ? `→ ${item.wud?.remote_tag || item.wud?.remote_digest || "Update"}`
    : "Watching";
}

function wudExplanation(item: TrackedContainerItem): string {
  if (isDemoMode) return "";
  if (!wudInventoryReady.value) return "WUD inventory unavailable.";
  if (item.wud_match_state === "ambiguous") return "Multiple WUD containers match this service.";
  if (item.wud_match_state === "unknown") return "WUD could not verify this Compose service.";
  if (item.wud_match_state === "watching" && item.wud_update_available === null) return "WUD sees this service, but its update status is unknown.";
  return "";
}

async function preview(): Promise<void> {
  if (!selected.value || !canPreview.value) return;
  appliedTargetId.value = "";
  await tracking.preview(selected.value.target_id, editor.value.trim());
  if (tracking.plan) {
    await nextTick();
    planRef.value?.focus({ preventScroll: true });
    planRef.value?.scrollIntoView({
      behavior: prefersReducedMotion() ? "auto" : "smooth",
      block: "start",
    });
  }
}

async function apply(): Promise<void> {
  if (!approved.value || !tracking.plan?.can_apply || tracking.applying) return;
  appliedTargetId.value = selectedId.value;
  previousJobId.value = tracking.rememberedJobId || tracking.job?.job_id || "";
  const applyPromise = tracking.apply();
  await nextTick();
  applyStatusRef.value?.focus({ preventScroll: true });
  applyStatusRef.value?.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "start",
  });
  await applyPromise;
  approved.value = false;
}
</script>

<template>
  <section class="content-stack tracked-containers">
    <div class="tracked-intro">
      <div>
        <p>Compose is the inventory. WUD shows what it sees; WUDup shows what changed and whether the tracking filter can see future tags.</p>
      </div>
      <n-button secondary :loading="tracking.loading" @click="tracking.load">
        <template #icon><RefreshCw :size="16" aria-hidden="true" /></template>
        Refresh
      </n-button>
    </div>

    <n-alert v-if="tracking.error" type="error" :show-icon="false">{{ tracking.error }}</n-alert>
    <n-alert v-if="tracking.applyError && !showInlineRepairStatus && inlineJob?.status !== 'failure'" type="error" :show-icon="false">{{ tracking.applyError }}</n-alert>
    <n-alert v-for="warning in tracking.inventory?.warnings ?? []" :key="warning" type="warning" :show-icon="false">{{ warning }}</n-alert>
    <n-alert v-if="showGlobalJobStatus && tracking.job?.status === 'success'" type="success" :show-icon="false">
      {{ showInlineRepairStatus ? `Earlier tracking repair job ${tracking.job.job_id} completed.` : "Tracking repaired. WUD may need its next watch cycle before the new filter is reflected here." }}
      <RouterLink v-if="tracking.job.run_id" :to="{ name: 'run-detail', params: { id: tracking.job.run_id } }">Review run</RouterLink>
    </n-alert>
    <n-alert v-if="showGlobalJobStatus && (tracking.job?.status === 'queued' || tracking.job?.status === 'running')" type="info" :show-icon="false">
      {{ showInlineRepairStatus ? "Earlier tracking repair" : "Tracking repair" }} is still running. {{ tracking.job.progress.at(-1)?.message }} Job {{ tracking.job.job_id }}. You can refresh later.
    </n-alert>
    <n-alert v-if="showGlobalJobStatus && tracking.job?.status === 'failure'" type="error" :show-icon="false">
      {{ showInlineRepairStatus ? `Earlier tracking repair job ${tracking.job.job_id} failed: ` : "" }}{{ tracking.job.error || "Tracking repair failed. Review the job and run history before retrying." }}
    </n-alert>

    <div v-if="tracking.inventory?.status === 'ready'" class="tracked-summary" aria-label="Inventory summary">
      <span><strong>{{ items.length }}</strong> Compose services</span>
      <span v-if="frozenCount"><strong>{{ frozenCount }}</strong> frozen filters</span>
      <template v-if="wudInventoryReady">
        <span v-if="untrackedCount"><strong>{{ untrackedCount }}</strong> confirmed not seen by WUD</span>
        <span v-if="unknownCount"><strong>{{ unknownCount }}</strong> WUD status unknown</span>
        <span><strong>{{ updateCount }}</strong> WUD candidates</span>
      </template>
      <span v-else-if="isDemoMode" class="tracked-summary-context">This sample shows Compose services and tracking filters. WUD observations are not included.</span>
      <span v-else class="tracked-summary-context">WUD inventory unavailable. <RouterLink :to="{ name: 'doctor' }">Check Doctor</RouterLink></span>
    </div>

    <section v-if="selected" ref="detailRef" tabindex="-1" class="section-panel tracked-detail" aria-label="Selected container details">
      <div class="tracked-detail-heading">
        <div><h3>{{ selected.service }}</h3><p>{{ selected.stack }} · {{ selected.image }}</p></div>
        <n-button text aria-label="Close container details" @click="closeDetails">Close</n-button>
      </div>
      <div class="tracked-source"><span>Compose file</span><code>{{ selected.compose_path }}</code></div>
      <div class="tracked-evidence">
        <div><span>Runtime</span><strong>{{ selected.runtime_state }}</strong></div>
        <div><span>WUD status</span><strong>{{ wudLabel(selected) }}</strong><small v-if="wudExplanation(selected)">{{ wudExplanation(selected) }} <RouterLink :to="{ name: 'doctor' }">Check Doctor</RouterLink></small></div>
        <div><span>Last image recorded</span><strong>{{ dateLabel(selected.last_image_recorded_at) }}</strong></div>
        <div><span>Last WUDup action</span><strong>{{ dateLabel(selected.last_action_at) }}<template v-if="selected.last_action_status"> · {{ selected.last_action_status }}</template></strong></div>
      </div>
      <p class="tracked-diagnosis"><AlertTriangle v-if="selected.tracking_health === 'frozen'" :size="16" aria-hidden="true" />{{ selected.tracking_detail }}</p>
      <div class="tracked-regex"><span>Current WUD tag filter</span><code>{{ selected.tracking_regex || "(none)" }}</code></div>
      <div v-if="selected.wud" class="tracked-wud"><span>WUD observed {{ selected.wud.local_tag || "unknown tag" }}</span><span v-if="selected.wud_update_available">Candidate {{ selected.wud.remote_tag || selected.wud.remote_digest }} · {{ selected.wud.update_kind || "update" }}</span><span v-if="selected.wud.error">{{ selected.wud.error }}</span></div>
      <nav class="tracked-links" aria-label="Related workflows">
        <RouterLink :to="{ name: 'retags', query: { search: selected.service_key } }">Retag</RouterLink>
        <RouterLink :to="{ name: 'pending', query: { search: selected.service_key } }">Pending</RouterLink>
        <RouterLink v-if="selected.last_action_run_id" :to="{ name: 'run-detail', params: { id: selected.last_action_run_id } }">Last action</RouterLink>
        <RouterLink v-else :to="{ name: 'runs' }">All history</RouterLink>
        <details class="tracked-links-more">
          <summary>Manage service</summary>
          <div class="tracked-links-more-items">
            <RouterLink :to="{ name: 'policies', query: { service: selected.service_key } }">Policy</RouterLink>
            <RouterLink :to="{ name: 'snoozes', query: { service: selected.service_key } }">Snooze</RouterLink>
            <RouterLink :to="{ name: 'tag-exclusions', query: { service: selected.service_key } }">Exclusions</RouterLink>
          </div>
        </details>
      </nav>

      <section class="tracked-repair" aria-label="Tracking repair">
        <div><h4>{{ selected.tracking_health === "exact-tag" ? "Review tracking" : "Fix tracking" }}</h4><p>Changing this service’s Compose label leaves its image tag unchanged.</p></div>
        <n-alert v-if="selected.tracking_health === 'digest-pinned'" type="warning" :show-icon="false">Digest-pinned services need manual review; automatic tracking repair is unavailable.</n-alert>
        <n-alert v-else-if="selected.tracking_health === 'no-image' || selected.tracking_health === 'image-unresolved'" type="warning" :show-icon="false">An image must be resolved before WUD tracking can be repaired. Review this Compose service manually.</n-alert>
        <template v-else>
          <label for="tracking-regex-editor">Proposed WUD tag regex</label>
          <n-input id="tracking-regex-editor" v-model:value="editor" placeholder="e.g. ^v\d+(?:\.\d+)+$" :input-props="{ 'aria-label': 'Proposed WUD tag regex' }" :disabled="tracking.applying || isDemoMode" />
          <p v-if="selected.suggested_regex" class="tracked-suggestion">Suggested from installed tag: <code>{{ selected.suggested_regex }}</code></p>
          <div v-if="patternGuide" class="tracked-pattern-guide" aria-label="Proposed filter explanation">
            <strong>What this filter would match</strong>
            <p>{{ patternGuide.summary }}</p>
            <p>Installed tag <code>{{ selected.current_tag }}</code>: <strong>{{ matchLabel(selected.current_tag) }}</strong></p>
            <p v-if="observedCandidate">WUD-observed candidate <code>{{ observedCandidate }}</code>: <strong>{{ matchLabel(observedCandidate) }}</strong></p>
            <details :key="selected.target_id" class="tracked-pattern-details">
              <summary>More examples and tag tests</summary>
              <p>{{ patternGuide.description }}</p>
              <table v-if="patternGuide.examples.length" class="tracked-examples" aria-label="Illustrative tag matches">
                <caption>Illustrative examples — not fetched from the registry.</caption>
                <thead><tr><th scope="col">Example tag</th><th scope="col">Result</th></tr></thead>
                <tbody><tr v-for="example in patternGuide.examples" :key="example.tag">
                  <td><code>{{ example.tag }}</code><small>{{ example.change }}</small></td>
                  <td>{{ example.matches ? "Matches" : "Excluded" }}</td>
                </tr></tbody>
              </table>
              <label for="tracking-tag-sample">Test another tag (optional)</label>
              <n-input v-model:value="sampleTag" :placeholder="samplePlaceholder" :input-props="{ id: 'tracking-tag-sample', 'aria-describedby': 'tracking-tag-result', maxlength: 128 }" />
              <div id="tracking-tag-result" role="status" aria-atomic="true">
                <p v-if="sampleTag" class="tracked-sample-result" :class="sampleMatch === true ? 'is-match' : 'is-warning'">
                  <CircleCheck v-if="sampleMatch === true" :size="18" aria-hidden="true" />
                  <AlertTriangle v-else :size="18" aria-hidden="true" />
                  <strong>{{ sampleMatch === null ? "Enter a valid Docker tag (letters, numbers, dots, dashes, or underscores)." : sampleMatch ? "This tag matches the proposed filter." : "This tag does not match the proposed filter." }}</strong>
                </p>
                <span v-else>Enter a tag to check whether it matches. The result updates as you type.</span>
              </div>
              <small>Only tags already shown by WUD are observed. This check does not fetch repository tags or confirm a tag exists.</small>
            </details>
          </div>
          <p v-else-if="editor.trim()" class="tracked-muted">This pattern is outside WUDup’s built-in tester and repair syntax. It may still work in WUD; manage it in Compose or enter an anchored exact tag or supported numeric pattern here.</p>
          <p v-if="patternGuide && selected.current_tag && patternGuide.matches(selected.current_tag) !== true" class="tracked-muted">The proposed filter must match the installed tag before repair can be previewed.</p>
          <n-button class="tracked-preview-button" :type="tracking.plan?.target_id === selected.target_id ? 'default' : 'primary'" :secondary="tracking.plan?.target_id === selected.target_id" :disabled="!canPreview || tracking.applying" :loading="tracking.planning" @click="preview">Preview repair</n-button>
          <p v-if="!editor.trim() && !isDemoMode" class="tracked-muted">Enter a tag filter to enable the preview.</p>
          <p v-if="sameAsCurrent" class="tracked-muted">This is already the current filter; there is no label change to preview.</p>
        </template>
        <p v-if="isDemoMode" class="tracked-muted">The public demo is read-only; repair previews require a live WUDup instance.</p>
        <p v-else-if="auth.session?.mutations_enabled !== true" class="tracked-muted">This WebUI is read-only. You can preview a repair, but apply requires mutations to be enabled.</p>
        <p v-else-if="selected.runtime_state !== 'running'" class="tracked-muted">Automatic repair requires a confirmed running service; it will not start a stopped or unknown service.</p>

        <div v-if="tracking.plan?.target_id === selected.target_id" ref="planRef" tabindex="-1" class="tracked-plan" aria-label="Tracking repair preview">
          <h4>Review the change</h4>
          <p><strong>{{ tracking.plan.service_key }}</strong> · Compose label only · Recreate this service without pulling or building an image.</p>
          <n-alert v-for="issue in tracking.plan.issues" :key="issue" type="warning" :show-icon="false">{{ issue }}</n-alert>
          <pre :aria-label="'Tracking label preview for ' + tracking.plan.service_key">{{ tracking.plan.compose_diff }}</pre>
          <n-checkbox v-model:checked="approved" :disabled="!tracking.plan.can_apply || tracking.applying">I reviewed the diff and understand this will recreate {{ selected.service }}.</n-checkbox>
          <n-button type="primary" :disabled="!approved || !tracking.plan.can_apply || tracking.applying || auth.session?.mutations_enabled !== true" :loading="tracking.applying" @click="apply">Apply tracking repair</n-button>
        </div>
        <div v-if="showInlineRepairStatus" ref="applyStatusRef" tabindex="-1" class="tracked-apply-status" aria-label="Tracking repair status" aria-live="polite">
          <n-alert v-if="inlineJob?.status === 'success'" type="success" :show-icon="false">
            Tracking repaired for {{ selected.service }}. WUD may need its next watch cycle before the new filter is reflected here.
            <RouterLink v-if="inlineJob.run_id" :to="{ name: 'run-detail', params: { id: inlineJob.run_id } }">Review run</RouterLink>
          </n-alert>
          <n-alert v-if="inlineJob?.status === 'failure' || tracking.applyError" type="error" :show-icon="false">
            {{ tracking.applyError || inlineJob?.error || "Tracking repair failed. Review run history before retrying." }}
          </n-alert>
          <n-alert v-if="!tracking.applyError && inlineJob?.status !== 'success' && inlineJob?.status !== 'failure'" type="info" :show-icon="false">
            {{ inlineJob ? `Tracking repair for ${selected.service} is still running. ${inlineJob.progress.at(-1)?.message || ""} Job ${inlineJob.job_id}. You can refresh later.` : `Starting tracking repair for ${selected.service}…` }}
          </n-alert>
        </div>
      </section>
    </section>

    <section ref="inventoryRef" tabindex="-1" class="section-panel tracked-inventory" aria-label="Compose inventory">
      <div class="tracked-toolbar">
        <n-input v-model:value="search" clearable placeholder="Search stack, service, image, or tag" :input-props="{ 'aria-label': 'Search tracked containers' }">
          <template #prefix><Search :size="16" aria-hidden="true" /></template>
        </n-input>
        <n-select v-model:value="filter" :options="filterOptions" aria-label="Tracking filter" />
      </div>

      <div v-if="tracking.loading && !tracking.inventory" class="tracked-empty" aria-live="polite">Loading Compose services and WUD observations…</div>
      <div v-else-if="tracking.inventory?.status === 'unavailable'" class="tracked-empty" role="alert">Compose inventory is unavailable. Check the warning above, then refresh.</div>
      <div v-else-if="!filtered.length" class="tracked-empty">{{ items.length ? "No services match these filters." : "No Compose services were discovered." }}</div>
      <div v-else class="tracked-table-wrap">
        <table class="tracked-table">
          <thead><tr><th scope="col">Service</th><th scope="col">Installed</th><th scope="col">WUD</th><th scope="col">Tracking</th><th scope="col">Last WUDup action</th><th scope="col"><span class="sr-only">Details</span></th></tr></thead>
          <tbody>
            <tr v-for="item in filtered" :key="item.target_id" :class="{ selected: selectedId === item.target_id }">
              <td><strong>{{ item.service }}</strong><small>{{ item.stack }}</small></td>
              <td><span class="tracked-version">{{ item.current_tag || "—" }}</span><small class="tracked-image">{{ item.image || "Image not resolved" }}</small></td>
              <td><n-tag v-if="item.wud" size="small" :type="item.wud_update_available === true ? 'warning' : item.wud_update_available === false ? 'success' : 'default'">{{ wudLabel(item) }}</n-tag><span v-else class="tracked-muted">{{ wudLabel(item) }}</span><small v-if="wudExplanation(item)" class="tracked-wud-explanation">{{ wudExplanation(item) }}</small></td>
              <td><n-tag size="small" :type="trackingTagType(item)">{{ trackingLabel(item) }}</n-tag></td>
              <td><span class="tracked-muted">{{ dateLabel(item.last_action_at) }}</span></td>
              <td><n-button text type="primary" :aria-label="`Inspect ${item.service_key}`" @click="select(item, $event)">Inspect</n-button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="filtered.length && tracking.inventory?.status === 'ready'" class="tracked-mobile-list">
        <article v-for="item in filtered" :key="item.target_id" class="tracked-mobile-item" :class="{ selected: selectedId === item.target_id }">
          <div class="tracked-mobile-heading"><div><strong>{{ item.service }}</strong><small>{{ item.stack }} · {{ item.current_tag || "image not resolved" }}</small></div><n-tag size="small" :type="trackingTagType(item)">{{ trackingLabel(item) }}</n-tag></div>
          <p>WUD: {{ wudLabel(item) }}<small v-if="wudExplanation(item)" class="tracked-wud-explanation">{{ wudExplanation(item) }}</small></p>
          <n-button text type="primary" @click="select(item, $event)">Inspect {{ item.service }}</n-button>
        </article>
      </div>
    </section>

  </section>
</template>

<style scoped>
.tracked-containers, .tracked-detail, .tracked-repair, .tracked-pattern-guide, .tracked-plan { min-width: 0; }
.tracked-containers, .tracked-detail, .tracked-repair, .tracked-plan { grid-template-columns: minmax(0, 1fr); }
.tracked-intro, .tracked-detail-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.tracked-detail h3 { margin: 0; color: var(--color-ink); font-size: var(--text-title-size); }
.tracked-intro p, .tracked-detail-heading p, .tracked-repair p { margin: 0; color: var(--color-text-secondary); }
.tracked-intro p { max-width: 72ch; }
.tracked-summary { display: flex; flex-wrap: wrap; gap: 8px 24px; padding: 10px 16px; border: 1px solid var(--color-border); border-radius: 8px; background: var(--color-surface); }
.tracked-summary span { color: var(--color-text-secondary); }
.tracked-summary strong { color: var(--color-ink); font-variant-numeric: tabular-nums; }
.tracked-summary-context { flex-basis: 100%; }
.tracked-summary a, .tracked-evidence a { color: var(--color-action-blue); text-underline-offset: 3px; }
.tracked-inventory { padding: 0; overflow: hidden; }
.tracked-toolbar { display: flex; gap: 12px; padding: 16px; border-bottom: 1px solid var(--color-border); }
.tracked-toolbar .n-input { max-width: 420px; }
.tracked-toolbar .n-select { width: 210px; flex: 0 0 auto; }
.tracked-table-wrap { overflow-x: auto; }
.tracked-table { width: 100%; border-collapse: collapse; text-align: left; }
.tracked-table th, .tracked-table td { padding: 12px 15px; border-bottom: 1px solid var(--color-border); vertical-align: middle; }
/* Contain the absolute sr-only heading inside the table's scroll area. */
.tracked-table th { position: relative; color: var(--color-text-secondary); font-weight: 600; white-space: nowrap; }
.tracked-table tr.selected { background: var(--color-surface-raised, var(--color-surface)); }
.tracked-table tbody tr:last-child td { border-bottom: 0; }
.tracked-mobile-list { display: none; }
.tracked-table td:first-child strong, .tracked-table small { display: block; }
.tracked-table small, .tracked-muted { color: var(--color-muted-text); }
.tracked-wud-explanation { display: block; margin-top: 4px; color: var(--color-muted-text); }
.tracked-image { max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tracked-version { font-weight: 600; font-variant-numeric: tabular-nums; }
.tracked-empty { padding: 36px 16px; text-align: center; color: var(--color-text-secondary); }
.tracked-detail { display: grid; gap: 18px; scroll-margin-top: 16px; }
.tracked-detail-heading p { overflow-wrap: anywhere; }
.tracked-source { display: grid; gap: 4px; min-width: 0; }
.tracked-source span { color: var(--color-muted-text); font-size: var(--text-metadata-size); }
.tracked-source code { font-family: var(--font-mono); overflow-wrap: anywhere; }
.tracked-evidence { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.tracked-evidence div { display: grid; gap: 4px; padding: 10px 0; border-top: 1px solid var(--color-border); }
.tracked-evidence span, .tracked-regex span { color: var(--color-muted-text); font-size: var(--text-metadata-size); }
.tracked-evidence strong { font-size: var(--text-body-size); overflow-wrap: anywhere; }
.tracked-evidence small { color: var(--color-muted-text); overflow-wrap: anywhere; }
.tracked-diagnosis { display: flex; gap: 8px; align-items: flex-start; margin: 0; }
.tracked-diagnosis svg { flex: 0 0 auto; color: var(--color-warning, currentColor); }
.tracked-regex, .tracked-wud { display: flex; gap: 8px 16px; align-items: baseline; flex-wrap: wrap; }
.tracked-regex code, .tracked-suggestion code, .tracked-plan pre { font-family: var(--font-mono); overflow-wrap: anywhere; }
.tracked-links { display: flex; flex-wrap: wrap; gap: 8px 18px; border-top: 1px solid var(--color-border); padding-top: 14px; }
.tracked-links a { color: var(--color-action-blue); font-weight: 600; text-underline-offset: 3px; }
.tracked-links summary { color: var(--color-action-blue); font-weight: 600; cursor: pointer; }
.tracked-links-more[open] { flex-basis: 100%; }
.tracked-links-more-items { display: flex; flex-wrap: wrap; gap: 8px 18px; padding-top: 8px; }
.tracked-links a:focus-visible, .tracked-links summary:focus-visible { outline: 2px solid var(--color-action-blue); outline-offset: 3px; }
.tracked-repair { display: grid; justify-items: start; gap: 12px; border-top: 1px solid var(--color-border); padding-top: 18px; }
.tracked-repair h4, .tracked-plan h4 { margin: 0; font-size: var(--text-body-size); }
.tracked-repair .n-input { width: min(100%, 580px); min-width: 0; }
.tracked-repair label { font-weight: 600; }
.tracked-suggestion { font-size: var(--text-metadata-size); }
.tracked-pattern-guide { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; width: 100%; max-width: 72ch; padding: 12px; border-radius: 7px; background: var(--color-surface-raised, var(--color-surface)); }
.tracked-pattern-guide code { font-family: var(--font-mono); overflow-wrap: anywhere; }
.tracked-pattern-guide small { color: var(--color-muted-text); }
.tracked-pattern-guide .n-input { width: min(100%, 420px); }
.tracked-sample-result { display: flex; align-items: flex-start; gap: 8px; margin: 0; }
.tracked-sample-result svg { flex-shrink: 0; margin-top: 3px; }
.tracked-sample-result.is-match { color: var(--color-operational-teal); }
.tracked-sample-result.is-warning { color: var(--color-warning-fg); }
.tracked-examples { width: 100%; table-layout: fixed; border-collapse: collapse; text-align: left; font-size: var(--text-metadata-size); }
.tracked-examples caption { padding-bottom: 8px; text-align: left; color: var(--color-muted-text); }
.tracked-examples th, .tracked-examples td { padding: 6px 0; border-bottom: 1px solid var(--color-border); vertical-align: top; }
.tracked-examples th:last-child, .tracked-examples td:last-child { width: 6em; padding-left: 12px; }
.tracked-examples small { display: block; }
.tracked-pattern-details summary { cursor: pointer; color: var(--color-action-blue); font-weight: 600; }
.tracked-pattern-details summary:focus-visible { outline: 2px solid var(--color-action-blue); outline-offset: 3px; }
.tracked-pattern-details[open] summary { margin-bottom: 8px; }
.tracked-plan { display: grid; justify-items: start; gap: 12px; width: 100%; border-top: 1px solid var(--color-border); padding-top: 16px; }
.tracked-apply-status { display: grid; gap: 8px; width: 100%; scroll-margin-top: 16px; }
.tracked-plan pre { box-sizing: border-box; width: 100%; max-height: 350px; overflow: auto; margin: 0; padding: 14px; border: 1px solid var(--color-border); border-radius: 6px; background: var(--color-page, var(--color-surface)); font-size: var(--text-data-size); white-space: pre-wrap; }
@media (--wud-app-shell) { .tracked-detail, .tracked-plan, .tracked-apply-status { scroll-margin-top: 80px; } .tracked-pattern-details summary { min-height: var(--size-touch-target); padding-block: 10px; } .tracked-evidence { grid-template-columns: repeat(2, minmax(0, 1fr)); } .tracked-table { min-width: 760px; } }
@media (--wud-data-cards) { .tracked-intro { display: none; } .tracked-summary { gap: 4px 12px; padding: 8px 12px; font-size: var(--text-metadata-size); line-height: 1.3; } .tracked-toolbar { flex-direction: column; } .tracked-toolbar .n-input, .tracked-toolbar .n-select { max-width: none; width: 100%; } .tracked-evidence { grid-template-columns: 1fr; } .tracked-table-wrap { display: none; } .tracked-mobile-list { display: block; } .tracked-mobile-item { display: grid; gap: 7px; padding: 15px 16px; border-bottom: 1px solid var(--color-border); } .tracked-mobile-item.selected { background: var(--color-surface-raised, var(--color-surface)); } .tracked-mobile-item:last-child { border-bottom: 0; } .tracked-mobile-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; } .tracked-mobile-heading small { display: block; color: var(--color-muted-text); } .tracked-mobile-item p { margin: 0; color: var(--color-text-secondary); } .tracked-mobile-item .n-button { justify-self: start; } }
@media (--wud-data-cards) { .tracked-preview-button { width: 100%; min-height: var(--size-touch-target); } .tracked-links a { display: inline-flex; align-items: center; min-height: var(--size-touch-target); } .tracked-links summary { box-sizing: border-box; min-height: var(--size-touch-target); padding-block: 10px; } }
</style>
