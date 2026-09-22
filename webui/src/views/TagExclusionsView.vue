<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Edit3, Save, ShieldOff } from "@lucide/vue";
import {
  NAlert,
  NButton,
  NEmpty,
  NFlex,
  NForm,
  NFormItem,
  NModal,
  NSelect,
  NTag,
} from "naive-ui";

import type {
  TagExclusionRuleRecord,
  TagExclusionScope,
  TagExclusionStatus,
  TagExclusionStatusFilter,
} from "../api/client";
import { useRouteRefresh } from "../components/app/routeRefresh";
import { useUpdateTargetOptions } from "../composables/useUpdateTargetOptions";
import { useManagementCardsBreakpoint } from "../responsive";
import { useAuthStore } from "../stores/auth";
import { useSettingsStore } from "../stores/settings";
import { useUpdatesStore } from "../stores/updates";
import { runInBackground } from "../utils/promises";

type TagExclusionTarget = Pick<
  TagExclusionRuleRecord,
  "scope" | "service_key" | "image_repo"
>;

const settings = useSettingsStore();
const route = useRoute();
const updates = useUpdatesStore();
const auth = useAuthStore();
const {
  imageRepoOptions,
  serviceKeyOptions,
  tagOptionsForImageRepo,
  targetForImageRepo,
  targetForServiceKey,
} = useUpdateTargetOptions();
const useManagementCards = useManagementCardsBreakpoint();
const statusFilter = ref<TagExclusionStatusFilter>("active");
const showSaveConfirm = ref(false);
const showStatusConfirm = ref(false);
const statusTarget = ref<TagExclusionRuleRecord | null>(null);
const nextStatus = ref<TagExclusionStatus>("disabled");

const exclusionForm = reactive({
  scope: (typeof route?.query?.service === "string" ? "service" : "image_repo") as TagExclusionScope,
  imageRepo: "",
  serviceKey: typeof route?.query?.service === "string" ? route.query.service : "",
  tag: "",
  status: "active" as TagExclusionStatus,
});

const scopeOptions = [
  { label: "Image repo", value: "image_repo" },
  { label: "Service", value: "service" },
];
const statusOptions = [
  { label: "Active", value: "active" },
  { label: "Disabled", value: "disabled" },
];
const statusFilterOptions = [
  { label: "Active", value: "active" },
  { label: "Disabled", value: "disabled" },
  { label: "All", value: "all" },
];

const mutationsEnabled = computed(
  () => auth.session?.mutations_enabled === true,
);
const tagOptions = computed(() =>
  tagOptionsForImageRepo(exclusionForm.imageRepo.trim()),
);
const saveDisabled = computed(
  () =>
    !mutationsEnabled.value ||
    !exclusionForm.imageRepo.trim() ||
    !exclusionForm.tag.trim() ||
    (exclusionForm.scope === "service" && !exclusionForm.serviceKey.trim()) ||
    settings.loading,
);
const viewError = computed(() => updates.error || settings.error);

function editExclusion(rule: TagExclusionRuleRecord): void {
  exclusionForm.scope = rule.scope as TagExclusionScope;
  exclusionForm.imageRepo = rule.image_repo;
  exclusionForm.serviceKey = rule.service_key;
  exclusionForm.tag = rule.tag;
  exclusionForm.status = rule.status as TagExclusionStatus;
}

function resetExclusionForm(): void {
  exclusionForm.scope = "image_repo";
  exclusionForm.imageRepo = "";
  exclusionForm.serviceKey = "";
  exclusionForm.tag = "";
  exclusionForm.status = "active";
}

watch(() => route?.query?.service, (value) => {
  showSaveConfirm.value = false;
  resetExclusionForm();
  if (typeof value === "string") {
    exclusionForm.scope = "service";
    applyServiceSelection(value);
  }
});

function scopeLabel(rule: TagExclusionRuleRecord): string {
  return rule.scope === "service" ? "service" : "image repo";
}

function targetLabel(
  rule: TagExclusionTarget,
): string {
  return rule.scope === "service" ? rule.service_key : rule.image_repo;
}

function selectText(value: string | number | null): string {
  return value === null ? "" : String(value);
}

function applyImageRepoSelection(value: string | number | null): void {
  const imageRepo = selectText(value);
  exclusionForm.imageRepo = imageRepo;
  const target = targetForImageRepo(imageRepo);
  if (!target) {
    return;
  }
  if (exclusionForm.scope === "service" && !exclusionForm.serviceKey.trim()) {
    exclusionForm.serviceKey = target.service_key;
  }
}

function applyServiceSelection(value: string | number | null): void {
  const serviceKey = selectText(value);
  exclusionForm.serviceKey = serviceKey;
  const target = targetForServiceKey(serviceKey);
  if (!target) {
    return;
  }
  exclusionForm.imageRepo = target.image_repo;
}

function setTagValue(value: string | number | null): void {
  exclusionForm.tag = selectText(value);
}

function openSaveConfirm(): void {
  if (saveDisabled.value) {
    return;
  }
  showSaveConfirm.value = true;
}

async function confirmSave(): Promise<void> {
  if (saveDisabled.value) {
    return;
  }
  await settings.upsertTagExclusion(
    exclusionForm.scope,
    exclusionForm.imageRepo.trim(),
    exclusionForm.scope === "service" ? exclusionForm.serviceKey.trim() : "",
    exclusionForm.tag.trim(),
    exclusionForm.status,
    statusFilter.value,
  );
}

function openStatusConfirm(
  rule: TagExclusionRuleRecord,
  status: TagExclusionStatus,
): void {
  if (!mutationsEnabled.value) {
    return;
  }
  statusTarget.value = rule;
  nextStatus.value = status;
  showStatusConfirm.value = true;
}

async function confirmStatusChange(): Promise<void> {
  if (statusTarget.value === null) {
    return;
  }
  await settings.setTagExclusionStatus(
    statusTarget.value.id,
    nextStatus.value,
    statusFilter.value,
  );
  statusTarget.value = null;
}

onMounted(() => {
  runInBackground(updates.loadUpdateTargets().then(() => {
    const service = route?.query?.service;
    if (typeof service === "string" && exclusionForm.serviceKey === service) {
      applyServiceSelection(service);
    }
  }));
  runInBackground(settings.loadTagExclusions(statusFilter.value));
});

watch(statusFilter, (nextFilter) => {
  runInBackground(settings.loadTagExclusions(nextFilter));
});

useRouteRefresh(() => settings.loadTagExclusions(statusFilter.value));
</script>

<template>
  <section class="content-stack">
    <n-alert v-if="viewError" type="error" :show-icon="false">
      {{ viewError }}
    </n-alert>
    <n-alert v-if="!mutationsEnabled" type="info" :show-icon="false">
      Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to manage tag exclusions.
    </n-alert>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Tag exclusion</p>
          <h2>{{ exclusionForm.imageRepo && exclusionForm.tag ? "Edit rule" : "New rule" }}</h2>
        </div>
      </div>
      <n-form class="management-form" @submit.prevent="openSaveConfirm">
        <n-form-item label="Scope">
          <n-select
            v-model:value="exclusionForm.scope"
            :options="scopeOptions"
            :disabled="settings.loading"
          />
        </n-form-item>
        <n-form-item
          label="Image repo"
          required
          feedback="Required. Use the repository name without a tag."
        >
          <n-select
            :value="exclusionForm.imageRepo"
            filterable
            tag
            clearable
            :options="imageRepoOptions"
            placeholder="repo/app"
            :disabled="settings.loading"
            @update:value="applyImageRepoSelection"
          />
        </n-form-item>
        <n-form-item
          v-if="exclusionForm.scope === 'service'"
          label="Service key"
          required
          feedback="Required for service-scoped exclusions. Use stack/service."
        >
          <n-select
            :value="exclusionForm.serviceKey"
            filterable
            tag
            clearable
            :options="serviceKeyOptions"
            placeholder="stack/service"
            :disabled="settings.loading"
            @update:value="applyServiceSelection"
          />
        </n-form-item>
        <n-form-item
          label="Tag"
          required
          feedback="Required. Match the tag to exclude."
        >
          <n-select
            :value="exclusionForm.tag"
            filterable
            tag
            clearable
            :options="tagOptions"
            placeholder="2.0"
            :disabled="settings.loading"
            @update:value="setTagValue"
          />
        </n-form-item>
        <n-form-item label="Status">
          <n-select
            v-model:value="exclusionForm.status"
            :options="statusOptions"
            :disabled="settings.loading"
          />
        </n-form-item>
        <div class="form-actions">
          <n-button quaternary :disabled="settings.loading" @click="resetExclusionForm">
            Clear form
          </n-button>
          <n-button
            type="primary"
            attr-type="submit"
            :disabled="saveDisabled"
            :loading="settings.loading"
          >
            <template #icon>
              <Save :size="16" />
            </template>
            Save rule
          </n-button>
        </div>
      </n-form>
    </section>

    <section class="section-panel">
      <div class="section-heading">
        <div>
          <p class="eyebrow">SQLite state</p>
          <h2>{{ settings.tagExclusions.length }} tag exclusions</h2>
        </div>
        <n-select
          v-model:value="statusFilter"
          class="filter-control"
          :options="statusFilterOptions"
          :disabled="settings.loading"
          aria-label="Filter tag exclusions by status"
        />
      </div>

      <div v-if="!useManagementCards" class="management-table exclusion-table">
        <div class="management-table-head">
          <span>Target</span>
          <span>Scope</span>
          <span>Tag</span>
          <span>Status</span>
          <span>Actions</span>
        </div>
        <div
          v-for="rule in settings.tagExclusions"
          :key="rule.id"
          class="management-row"
        >
          <strong>{{ targetLabel(rule) }}</strong>
          <span>{{ scopeLabel(rule) }}</span>
          <code>{{ rule.tag }}</code>
          <n-tag size="small" :type="rule.status === 'active' ? 'warning' : 'default'">
            {{ rule.status }}
          </n-tag>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button size="small" quaternary @click="editExclusion(rule)">
              <template #icon>
                <Edit3 :size="15" />
              </template>
              Edit
            </n-button>
            <n-button
              size="small"
              quaternary
              :type="rule.status === 'active' ? 'warning' : 'primary'"
              :disabled="!mutationsEnabled"
              @click="
                openStatusConfirm(
                  rule,
                  rule.status === 'active' ? 'disabled' : 'active',
                )
              "
            >
              <template #icon>
                <ShieldOff :size="15" />
              </template>
              {{ rule.status === "active" ? "Disable" : "Enable" }}
            </n-button>
          </n-flex>
        </div>
      </div>

      <div v-else class="mobile-list">
        <article
          v-for="rule in settings.tagExclusions"
          :key="rule.id"
          class="mobile-card"
        >
          <div class="mobile-card-title">
            <strong>{{ targetLabel(rule) }}</strong>
            <n-tag size="small" :type="rule.status === 'active' ? 'warning' : 'default'">
              {{ rule.status }}
            </n-tag>
          </div>
          <dl>
            <div>
              <dt>Scope</dt>
              <dd>{{ scopeLabel(rule) }}</dd>
            </div>
            <div>
              <dt>Tag</dt>
              <dd>{{ rule.tag }}</dd>
            </div>
            <div v-if="rule.scope === 'service'">
              <dt>Repository</dt>
              <dd>{{ rule.image_repo }}</dd>
            </div>
          </dl>
          <n-flex
            class="table-actions"
            align="center"
            :justify="useManagementCards ? 'flex-start' : 'flex-end'"
            :size="8"
          >
            <n-button size="small" quaternary @click="editExclusion(rule)">
              <template #icon>
                <Edit3 :size="15" />
              </template>
              Edit
            </n-button>
            <n-button
              size="small"
              quaternary
              :type="rule.status === 'active' ? 'warning' : 'primary'"
              :disabled="!mutationsEnabled"
              @click="
                openStatusConfirm(
                  rule,
                  rule.status === 'active' ? 'disabled' : 'active',
                )
              "
            >
              <template #icon>
                <ShieldOff :size="15" />
              </template>
              {{ rule.status === "active" ? "Disable" : "Enable" }}
            </n-button>
          </n-flex>
        </article>
      </div>
      <n-empty
        v-if="!settings.tagExclusions.length"
        class="empty-state"
        description="No tag exclusions."
        :show-icon="false"
      />
    </section>

    <n-modal
      v-model:show="showSaveConfirm"
      preset="dialog"
      title="Save tag exclusion"
      positive-text="Save rule"
      negative-text="Cancel"
      :positive-button-props="{ type: 'primary', loading: settings.loading }"
      @positive-click="confirmSave"
    >
      <div class="confirmation-list">
        <div>
          <span>Target</span>
          <strong>
            {{
              targetLabel({
                scope: exclusionForm.scope,
                image_repo: exclusionForm.imageRepo.trim(),
                service_key: exclusionForm.serviceKey.trim(),
              })
            }}
          </strong>
        </div>
        <div>
          <span>Repository</span>
          <strong>{{ exclusionForm.imageRepo.trim() }}</strong>
        </div>
        <div>
          <span>Tag</span>
          <strong>{{ exclusionForm.tag.trim() }}</strong>
        </div>
        <div>
          <span>Status</span>
          <strong>{{ exclusionForm.status }}</strong>
        </div>
      </div>
    </n-modal>

    <n-modal
      v-model:show="showStatusConfirm"
      preset="dialog"
      title="Update tag exclusion"
      positive-text="Update status"
      negative-text="Cancel"
      :positive-button-props="{ type: nextStatus === 'active' ? 'primary' : 'warning', loading: settings.loading }"
      @positive-click="confirmStatusChange"
    >
      <div v-if="statusTarget" class="confirmation-list">
        <div>
          <span>Target</span>
          <strong>{{ targetLabel(statusTarget) }}</strong>
        </div>
        <div>
          <span>Tag</span>
          <strong>{{ statusTarget.tag }}</strong>
        </div>
        <div>
          <span>Status</span>
          <strong>{{ nextStatus }}</strong>
        </div>
      </div>
    </n-modal>
  </section>
</template>

<style scoped>
.exclusion-table .management-table-head,
.exclusion-table .management-row {
  grid-template-columns:
    minmax(160px, 1.2fr) minmax(100px, 0.5fr) minmax(90px, 0.5fr) minmax(92px, 0.5fr) minmax(198px, auto);
}
</style>
