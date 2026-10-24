<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch, type Component } from "vue";
import { RouterLink, useRoute } from "vue-router";
import {
  BellOff,
  Clock3,
  Container,
  LayoutDashboard,
  ListChecks,
  Repeat2,
  Settings2,
  SlidersHorizontal,
  Stethoscope,
  Tags,
} from "@lucide/vue";
import { NTag } from "naive-ui";
import { useMediaQuery } from "@vueuse/core";

import { responsiveMediaQueries } from "../../responsive";
import { useConnectionStore } from "../../stores/connection";
import { useSettingsStore } from "../../stores/settings";
import { useTrackingStore } from "../../stores/tracking";
import { useUpdatesStore } from "../../stores/updates";
import { runInBackground } from "../../utils/promises";
import AppBrandMark from "./AppBrandMark.vue";

const route = useRoute();
const connection = useConnectionStore();
const settings = useSettingsStore();
const tracking = useTrackingStore();
const updates = useUpdatesStore();
const navListRef = ref<HTMLElement | null>(null);
const appShellLayout = useMediaQuery(responsiveMediaQueries.appShell);

const RELEASES_URL = "https://github.com/magrhino/wudup/releases";
const COMMIT_URL = "https://github.com/magrhino/wudup/commit";
const VERSION_RELEASE_RE = /^v?\d+\.\d+/;
const EDGE_BUILD_RE = /^edge-([0-9a-f]{7,40})$/;

const edgeCommit = computed(
  () => EDGE_BUILD_RE.exec(connection.status?.build_version ?? "")?.[1] ?? "",
);
const appVersion = computed(() => connection.status?.version ?? "");
const appVersionIsRelease = computed(() =>
  VERSION_RELEASE_RE.test(appVersion.value),
);
const appVersionLabel = computed(() => {
  if (edgeCommit.value) {
    return `edge ${edgeCommit.value}`;
  }
  if (!appVersion.value) {
    return "";
  }
  if (appVersionIsRelease.value) {
    return appVersion.value.startsWith("v")
      ? appVersion.value
      : `v${appVersion.value}`;
  }
  return appVersion.value;
});
const appVersionHref = computed(() => {
  if (edgeCommit.value) {
    return `${COMMIT_URL}/${edgeCommit.value}`;
  }
  if (!appVersion.value || !appVersionIsRelease.value) {
    return RELEASES_URL;
  }
  return `${RELEASES_URL}/tag/${appVersionLabel.value}`;
});
const appVersionTitle = computed(() => {
  if (edgeCommit.value) {
    return `Edge build of commit ${edgeCommit.value}; open it on GitHub`;
  }
  return appVersionIsRelease.value
    ? `Open ${appVersionLabel.value} release notes`
    : "Open WUDup releases";
});

type NavItem = {
  to: string;
  label: string;
  icon: Component;
  activeRouteNames: string[];
};

type NavBadge = {
  text: string;
  tone: "neutral" | "attention" | "unknown";
  description: string;
};

const NAV_BADGE_MAX = 99;

// Wait for managed settings so a disabled preference never flashes counts.
const navBadgesEnabled = computed(
  () =>
    settings.settings !== null &&
    settings.settings.managed.find((entry) => entry.key === "sidebar_badges")
      ?.value !== "false",
);
const pendingCount = computed(
  () => updates.pending?.count ?? connection.status?.pending_count ?? 0,
);
// An unavailable or failed inventory is unknown, not "nothing to fix",
// including a failed refresh that left an older inventory in place.
const containerCountStale = computed(
  () => tracking.inventory?.status !== "ready" || tracking.inventoryLoadFailed,
);
const containerCountUnknown = computed(
  () =>
    !tracking.loading &&
    (tracking.inventory?.status === "unavailable" || tracking.inventoryLoadFailed),
);

function navBadge(
  count: number,
  tone: NavBadge["tone"],
  description: string,
): NavBadge | undefined {
  if (count <= 0) {
    return undefined;
  }
  return {
    text: count > NAV_BADGE_MAX ? `${NAV_BADGE_MAX}+` : String(count),
    tone,
    description,
  };
}

function containersBadge(): NavBadge | undefined {
  if (containerCountUnknown.value) {
    return {
      text: "?",
      tone: "unknown",
      description: "tracking status unavailable",
    };
  }
  const repairs = tracking.needsRepairCount;
  const subject = repairs === 1 ? "container needs" : "containers need";
  return navBadge(repairs, "attention", `${repairs} ${subject} a tracking fix`);
}

const navBadges = computed<Partial<Record<string, NavBadge>>>(() => {
  if (!navBadgesEnabled.value) {
    return {};
  }
  return {
    "/pending": navBadge(
      pendingCount.value,
      "neutral",
      `${pendingCount.value} pending ${pendingCount.value === 1 ? "update" : "updates"}`,
    ),
    "/containers": containersBadge(),
  };
});

function navItemLabel(item: NavItem): string {
  const badge = navBadges.value[item.to];
  return badge ? `${item.label}, ${badge.description}` : item.label;
}

const navItems: NavItem[] = [
  {
    to: "/",
    label: "Dashboard",
    icon: LayoutDashboard,
    activeRouteNames: ["dashboard"],
  },
  {
    to: "/pending",
    label: "Pending",
    icon: ListChecks,
    activeRouteNames: ["pending"],
  },
  {
    to: "/containers",
    label: "Containers",
    icon: Container,
    activeRouteNames: ["containers"],
  },
  {
    to: "/retags",
    label: "Retags",
    icon: Repeat2,
    activeRouteNames: ["retags"],
  },
  {
    to: "/runs",
    label: "History",
    icon: Clock3,
    activeRouteNames: ["runs", "audit", "run-detail", "run-log"],
  },
  {
    to: "/policies",
    label: "Policies",
    icon: Settings2,
    activeRouteNames: ["policies"],
  },
  {
    to: "/snoozes",
    label: "Snoozes",
    icon: BellOff,
    activeRouteNames: ["snoozes"],
  },
  {
    to: "/tag-exclusions",
    label: "Exclusions",
    icon: Tags,
    activeRouteNames: ["tag-exclusions"],
  },
  {
    to: "/settings",
    label: "Settings",
    icon: SlidersHorizontal,
    activeRouteNames: ["settings"],
  },
  {
    to: "/doctor",
    label: "Doctor",
    icon: Stethoscope,
    activeRouteNames: ["doctor"],
  },
];

function isNavItemActive(item: NavItem): boolean {
  return (
    typeof route.name === "string" &&
    item.activeRouteNames.includes(route.name)
  );
}

function scrollNavElementIntoView(element: HTMLElement): void {
  element.scrollIntoView?.({
    block: "nearest",
    inline: "nearest",
  });
}

function scrollNavItemIntoView(event: FocusEvent): void {
  if (event.currentTarget instanceof HTMLElement) {
    scrollNavElementIntoView(event.currentTarget);
  }
}

async function scrollActiveNavItemIntoView(): Promise<void> {
  await nextTick();
  const activeItem = navListRef.value?.querySelector<HTMLElement>(
    ".nav-item-active",
  );
  if (activeItem) {
    scrollNavElementIntoView(activeItem);
  }
}

watch(
  [() => route.name, appShellLayout],
  () => runInBackground(scrollActiveNavItemIntoView()),
);

watch(
  [navBadgesEnabled, () => route.name],
  ([enabled, routeName]) => {
    // Retry a failed or unavailable inventory on navigation. The Containers
    // view loads its own inventory; avoid a duplicate discovery there.
    if (
      enabled &&
      containerCountStale.value &&
      !tracking.loading &&
      routeName !== "containers"
    ) {
      runInBackground(tracking.load());
    }
  },
  { immediate: true },
);

onMounted(() => {
  if (connection.status === null) {
    runInBackground(connection.loadStatus());
  }
  runInBackground(scrollActiveNavItemIntoView());
});
</script>

<template>
  <aside class="sidebar">
    <RouterLink class="brand" to="/" aria-label="WUDup dashboard">
      <AppBrandMark :size="28" />
      <span>WUDup</span>
    </RouterLink>

    <nav ref="navListRef" class="nav-list">
      <RouterLink
        v-for="item in navItems"
        :key="item.to"
        class="nav-item"
        :class="{ 'nav-item-active': isNavItemActive(item) }"
        :to="item.to"
        :title="navItemLabel(item)"
        :aria-label="navItemLabel(item)"
        :aria-current="isNavItemActive(item) ? 'page' : undefined"
        @focus="scrollNavItemIntoView"
      >
        <component :is="item.icon" :size="18" />
        <span>{{ item.label }}</span>
        <span
          v-if="navBadges[item.to]"
          class="nav-badge"
          :class="`nav-badge-${navBadges[item.to]?.tone}`"
          aria-hidden="true"
        >
          {{ navBadges[item.to]?.text }}
        </span>
      </RouterLink>
    </nav>

    <div class="sidebar-footer">
      <n-tag
        v-if="appVersionLabel"
        class="version-tag"
        size="small"
      >
        <a
          class="version-link"
          :href="appVersionHref"
          target="_blank"
          rel="noopener noreferrer"
          :title="appVersionTitle"
          :aria-label="appVersionTitle"
        >
          {{ appVersionLabel }}
        </a>
      </n-tag>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 28px;
  min-height: 100vh;
  padding: 22px 16px;
  background: var(--color-sidebar);
  color: var(--color-sidebar-text);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 40px;
  padding: 0 10px;
  font-weight: 700;
}

.nav-list {
  display: grid;
  gap: 6px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 0 10px;
  border-radius: 7px;
  color: var(--color-sidebar-muted);
  transition:
    background-color var(--motion-base) var(--ease-out-quart),
    color var(--motion-base) var(--ease-out-quart),
    transform var(--motion-fast) var(--ease-out-quart);
}

.nav-item.router-link-active,
.nav-item.nav-item-active,
.nav-item:hover {
  background: var(--color-sidebar-hover);
  color: var(--color-sidebar-text);
  transform: translateX(2px);
}

.nav-badge {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 20px;
  margin-left: auto;
  padding: 0 6px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: var(--text-label-size);
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  line-height: 1;
}

.nav-badge-neutral {
  border-color: rgba(247, 251, 252, 0.18);
  background: rgba(247, 251, 252, 0.12);
  color: var(--color-sidebar-text);
}

.nav-badge-attention {
  border-color: color-mix(in srgb, var(--color-warning-fg) 40%, transparent);
  background: var(--color-warning-bg);
  color: var(--color-warning-fg);
}

.nav-badge-unknown {
  border-color: rgba(247, 251, 252, 0.32);
  border-style: dashed;
  color: var(--color-sidebar-muted);
}

.sidebar-footer {
  margin-top: auto;
  padding: 0 10px;
}

.version-tag {
  max-width: 100%;
  border-color: rgba(247, 251, 252, 0.18);
  background: rgba(247, 251, 252, 0.08);
  color: var(--color-sidebar-muted);
}

.version-link {
  display: inline-flex;
  max-width: 100%;
  color: inherit;
  font-weight: 700;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.version-link:hover,
.version-link:focus-visible {
  color: var(--color-sidebar-text);
  text-decoration: underline;
}

@media (--wud-app-shell) {
  .sidebar {
    position: sticky;
    top: 0;
    z-index: 10;
    min-height: auto;
    flex-direction: row;
    align-items: center;
    gap: 12px;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    padding: 10px 12px;
    overflow: hidden;
  }

  .brand span,
  .sidebar-footer {
    display: none;
  }

  .brand {
    min-width: var(--size-touch-target);
    min-height: var(--size-touch-target);
  }

  .nav-list {
    flex: 1 1 auto;
    grid-auto-flow: column;
    grid-auto-columns: max-content;
    margin: -4px;
    min-width: 0;
    max-width: 100%;
    padding: 4px;
    overflow-x: auto;
    overflow-y: hidden;
    scroll-padding-inline: 4px;
  }

  .nav-item {
    flex: 0 0 auto;
    gap: 8px;
    min-height: var(--size-touch-target);
    padding: 0 12px;
    scroll-margin-inline: 4px;
    white-space: nowrap;
  }
}
</style>
