import { createPinia, setActivePinia, type Pinia } from "pinia";
import {
  flushPromises,
  type DOMWrapper,
  type VueWrapper,
} from "@vue/test-utils";
import { createMemoryHistory, type Router } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";

import type { SetupStatusResponse } from "../src/api/types";
import App from "../src/App.vue";
import { createWudRouter } from "../src/router";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useRetagsStore } from "../src/stores/retags";
import { useTrackingStore } from "../src/stores/tracking";
import { useSelfUpdateStore } from "../src/stores/selfUpdate";
import { useUpdatesStore } from "../src/stores/updates";
import { useRunsStore } from "../src/stores/runs";
import SetupView from "../src/views/SetupView.vue";
import ResetAdminView from "../src/views/ResetAdminView.vue";
import { themeStorageKey } from "../src/theme";
import {
  authSession,
  coreUpdateTourResponse,
  retagTargetsResponse,
  selfUpdateApplyResponse,
  selfUpdatePlanResponse,
  selfUpdatePrepareResponse,
  selfUpdateResponse,
  settingsResponse,
  statusResponse,
  trackedContainerItem,
  trackedContainersResponse,
} from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

const adminPassword = "correct horse battery staple";

type WrapperQuery = VueWrapper | DOMWrapper<Element>;

type AppStores = {
  pinia: Pinia;
  connection: ReturnType<typeof useConnectionStore>;
  settings: ReturnType<typeof useSettingsStore>;
  updates: ReturnType<typeof useUpdatesStore>;
  selfUpdate: ReturnType<typeof useSelfUpdateStore>;
  runs: ReturnType<typeof useRunsStore>;
};

async function createReadyRouter(path: string): Promise<Router> {
  const router = createWudRouter(createMemoryHistory());
  await router.push(path);
  await router.isReady();
  return router;
}

function unauthenticatedSession(setupRequired: boolean) {
  return authSession({
    authenticated: false,
    setup_required: setupRequired,
    username: null,
  });
}

function setupStatusResponse(
  overrides: Partial<SetupStatusResponse>,
): SetupStatusResponse {
  return {
    setup_required: false,
    claim_required: false,
    authenticated: false,
    auth_required: true,
    dev_auth_bypass: false,
    mutations_enabled: false,
    password_min_length: 12,
    ...overrides,
  };
}

async function submitAdminCredentials(wrapper: VueWrapper): Promise<void> {
  await wrapper
    .find('input[name="username"][autocomplete="username"]')
    .setValue("admin");
  await wrapper
    .find('input[name="password"][autocomplete="new-password"]')
    .setValue(adminPassword);
  await wrapper
    .find('input[name="confirm-password"][autocomplete="new-password"]')
    .setValue(adminPassword);
  await wrapper.find("form").trigger("submit");
  await flushPromises();
}

function createAppStores(mutationsEnabled = false): AppStores {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.session = authSession({ mutations_enabled: mutationsEnabled });

  return {
    pinia,
    connection: useConnectionStore(),
    settings: useSettingsStore(),
    updates: useUpdatesStore(),
    selfUpdate: useSelfUpdateStore(),
    runs: useRunsStore(),
  };
}

async function mountAppAt(stores: AppStores, path = "/") {
  const router = await createReadyRouter(path);
  const wrapper = mountWithApp(App, { pinia: stores.pinia, router });
  return { router, wrapper };
}

function stubAppShellLoads({
  connection,
  settings,
  updates,
  runs,
}: AppStores): void {
  vi.spyOn(connection, "loadStatus").mockResolvedValue(undefined);
  vi.spyOn(updates, "loadPending").mockResolvedValue(undefined);
  vi.spyOn(runs, "loadRuns").mockResolvedValue(undefined);
  vi.spyOn(settings, "loadServicePolicies").mockResolvedValue(undefined);
  vi.spyOn(settings, "loadSnoozes").mockResolvedValue(undefined);
  vi.spyOn(settings, "loadTagExclusions").mockResolvedValue(undefined);
}

function buttonByText(wrapper: WrapperQuery, text: string) {
  const button = wrapper
    .findAll("button")
    .find((candidate) => candidate.text().includes(text));
  if (!button) {
    throw new Error(`button containing "${text}" was not rendered`);
  }
  return button;
}

function buttonByAriaLabel(wrapper: WrapperQuery, label: string) {
  const button = wrapper
    .findAll("button")
    .find((candidate) =>
      candidate.attributes("aria-label")?.includes(label),
    );
  if (!button) {
    throw new Error(
      `button with aria label containing "${label}" was not rendered`,
    );
  }
  return button;
}

async function clickButtonByText(
  wrapper: WrapperQuery,
  text: string,
): Promise<void> {
  await buttonByText(wrapper, text).trigger("click");
  await flushPromises();
}

function expectTextToContain(wrapper: WrapperQuery, ...fragments: string[]): void {
  const text = wrapper.text();
  for (const fragment of fragments) {
    expect(text).toContain(fragment);
  }
}

async function expectNextTheme(
  themeButton: () => DOMWrapper<HTMLButtonElement>,
  preference: string,
  activeTheme: string,
  label: string,
): Promise<void> {
  await themeButton().trigger("click");
  await nextTick();

  expect(globalThis.localStorage.getItem(themeStorageKey)).toBe(preference);
  expect(document.documentElement.dataset.theme).toBe(activeTheme);
  expect(themeButton().attributes("aria-label")).toContain(label);
}

function primePinnedSelfUpdate(stores: AppStores): void {
  stores.connection.status = statusResponse({ version: "0.24.2" });
  stores.settings.coreUpdateTour = coreUpdateTourResponse();
  stores.selfUpdate.selfUpdate = selfUpdateResponse({
    strategy: "prepare_tag_update",
    current_image: "ghcr.io/magrhino/wudup:v0.24.2",
    target_image: "ghcr.io/magrhino/wudup:v0.25.0",
    external_recreate_required: true,
  });
}

describe("router auth guard", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("routes setup-required users to setup", async () => {
    const auth = useAuthStore();
    auth.session = unauthenticatedSession(true);
    const router = await createReadyRouter("/pending");

    expect(router.currentRoute.value.name).toBe("setup");
  });

  it("routes unauthenticated users to login with redirect", async () => {
    const auth = useAuthStore();
    auth.session = unauthenticatedSession(false);
    const router = await createReadyRouter("/pending");

    expect(router.currentRoute.value.name).toBe("login");
    expect(router.currentRoute.value.query.redirect).toBe("/pending");
  });

  it("routes authenticated users away from login and setup", async () => {
    const auth = useAuthStore();
    auth.session = authSession({ authenticated: true });
    const router = await createReadyRouter("/login");

    expect(router.currentRoute.value.name).toBe("dashboard");

    await router.push("/setup");

    expect(router.currentRoute.value.name).toBe("dashboard");
  });

  it("allows unauthenticated users to open admin recovery", async () => {
    const auth = useAuthStore();
    auth.session = unauthenticatedSession(false);
    const router = await createReadyRouter(
      "/reset-admin?claim=recovery&user=admin",
    );

    expect(router.currentRoute.value.name).toBe("reset-admin");
    expect(router.currentRoute.value.query.claim).toBe("recovery");
  });

  it("routes first admin setup success to Settings onboarding", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = unauthenticatedSession(true);
    auth.setupStatus = setupStatusResponse({
      setup_required: true,
      claim_required: true,
    });
    vi.spyOn(auth, "loadSetupStatus").mockResolvedValue();
    vi.spyOn(auth, "claimSetup").mockImplementation(async () => {
      auth.session = authSession({ authenticated: true, setup_required: false });
      auth.setupStatus = null;
    });
    const router = await createReadyRouter("/setup?claim=claim");
    const replace = vi.spyOn(router, "replace").mockResolvedValue(undefined);
    const wrapper = mountWithApp(SetupView, { pinia, router });

    await submitAdminCredentials(wrapper);

    expect(auth.claimSetup).toHaveBeenCalledWith(
      "claim",
      "admin",
      adminPassword,
    );
    expect(replace).toHaveBeenCalledWith({
      name: "settings",
      query: { onboarding: "1" },
    });
  });

  it("routes admin reset success to Dashboard", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = unauthenticatedSession(false);
    auth.setupStatus = setupStatusResponse({});
    vi.spyOn(auth, "loadSetupStatus").mockResolvedValue();
    vi.spyOn(auth, "resetAdmin").mockImplementation(async () => {
      auth.session = authSession({ authenticated: true, setup_required: false });
      auth.setupStatus = null;
    });
    const router = await createReadyRouter(
      "/reset-admin?claim=recovery&user=admin",
    );
    const replace = vi.spyOn(router, "replace").mockResolvedValue(undefined);
    const wrapper = mountWithApp(ResetAdminView, { pinia, router });

    await submitAdminCredentials(wrapper);

    expect(auth.resetAdmin).toHaveBeenCalledWith(
      "recovery",
      "admin",
      adminPassword,
    );
    expect(replace).toHaveBeenCalledWith({ name: "dashboard" });
  });
});

describe("app shell", () => {
  const scrollIntoViewDescriptor = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "scrollIntoView",
  );

  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    if (scrollIntoViewDescriptor) {
      Object.defineProperty(
        Element.prototype,
        "scrollIntoView",
        scrollIntoViewDescriptor,
      );
    } else {
      Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    }
  });

  it("shows a linked release tag in the shell footer", async () => {
    const stores = createAppStores(true);
    stores.connection.status = statusResponse({ version: "0.24.2" });
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stubAppShellLoads(stores);

    const { wrapper } = await mountAppAt(stores);
    const versionLink = wrapper.find(
      'a[href="https://github.com/magrhino/wudup/releases/tag/v0.24.2"]',
    );

    expect(versionLink.exists()).toBe(true);
    expect(versionLink.text()).toBe("v0.24.2");
    expect(wrapper.text()).not.toContain("Mutations enabled");

    stores.connection.status = statusResponse({ version: "dev-build" });
    await nextTick();

    const buildLink = wrapper.find(
      'a[href="https://github.com/magrhino/wudup/releases"]',
    );
    expect(buildLink.exists()).toBe(true);
    expect(buildLink.text()).toBe("dev-build");

    stores.connection.status = statusResponse({
      version: "0.24.2",
      build_version: "edge-0123abc",
    });
    await nextTick();

    const edgeLink = wrapper.find(
      'a[href="https://github.com/magrhino/wudup/commit/0123abc"]',
    );
    expect(edgeLink.exists()).toBe(true);
    expect(edgeLink.text()).toBe("edge 0123abc");
    expect(edgeLink.attributes("title")).toBe(
      "Edge build of commit 0123abc; open it on GitHub",
    );
  });

  it("shows sidebar counts for pending updates and containers that need a tracking fix", async () => {
    const stores = createAppStores();
    stores.connection.status = statusResponse({ pending_count: 5 });
    stores.settings.settings = settingsResponse();
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const tracking = useTrackingStore(stores.pinia);
    const loadTracking = vi.spyOn(tracking, "load").mockImplementation(async () => {
      tracking.inventory = trackedContainersResponse([
        trackedContainerItem({ target_id: "a", tracking_health: "frozen" }),
        trackedContainerItem({ target_id: "b", tracking_health: "needs-transform" }),
        trackedContainerItem({ target_id: "c" }),
      ]);
    });

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    const navItems = wrapper.findAll(".nav-item");
    const pendingItem = navItems.find((item) => item.text().includes("Pending"));
    const containersItem = navItems.find((item) => item.text().includes("Containers"));
    expect(loadTracking).toHaveBeenCalledTimes(1);
    expect(pendingItem?.find(".nav-badge").text()).toBe("5");
    expect(pendingItem?.attributes("aria-label")).toBe("Pending, 5 pending updates");
    expect(containersItem?.find(".nav-badge-attention").text()).toBe("2");
    expect(containersItem?.attributes("aria-label")).toBe(
      "Containers, 2 containers need a tracking fix",
    );
    expect(wrapper.findAll(".nav-badge")).toHaveLength(2);

    stores.connection.status = statusResponse({ pending_count: 120 });
    await nextTick();
    expect(pendingItem?.find(".nav-badge").text()).toBe("99+");

    stores.connection.status = statusResponse({ pending_count: 0 });
    await nextTick();
    expect(pendingItem?.find(".nav-badge").exists()).toBe(false);
    expect(pendingItem?.attributes("aria-label")).toBe("Pending");
  });

  it("hides sidebar counts when the preference is off and skips the inventory load", async () => {
    const stores = createAppStores();
    stores.connection.status = statusResponse({ pending_count: 5 });
    const settings = settingsResponse();
    stores.settings.settings = {
      ...settings,
      managed: settings.managed.map((entry) =>
        entry.key === "sidebar_badges"
          ? { ...entry, value: "false", source: "configured" as const }
          : entry,
      ),
    };
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const loadTracking = vi
      .spyOn(useTrackingStore(stores.pinia), "load")
      .mockResolvedValue();

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    expect(wrapper.find(".nav-badge").exists()).toBe(false);
    expect(loadTracking).not.toHaveBeenCalled();
  });

  it("leaves the Containers page inventory load to the view", async () => {
    const stores = createAppStores();
    stores.settings.settings = settingsResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const loadTracking = vi
      .spyOn(useTrackingStore(stores.pinia), "load")
      .mockResolvedValue();

    await mountAppAt(stores, "/containers");
    await flushPromises();

    // Only TrackedContainersView loads; the sidebar skips a duplicate discovery.
    expect(loadTracking).toHaveBeenCalledTimes(1);
  });

  it("retries a failed sidebar inventory load on navigation until it is ready", async () => {
    const stores = createAppStores();
    stores.settings.settings = settingsResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const tracking = useTrackingStore(stores.pinia);
    const loadTracking = vi.spyOn(tracking, "load").mockResolvedValue();

    const { router, wrapper } = await mountAppAt(stores);
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".nav-badge-attention").exists()).toBe(false);
    // Not loaded yet with no error: nothing is known, so no badge.
    expect(wrapper.find(".nav-badge-unknown").exists()).toBe(false);

    // A repair-job error alone says nothing about the inventory.
    tracking.error = "Could not check tracking repair job job-1";
    await nextTick();
    expect(wrapper.find(".nav-badge-unknown").exists()).toBe(false);

    tracking.inventoryLoadFailed = true;
    await nextTick();
    const containersItem = wrapper
      .findAll(".nav-item")
      .find((item) => item.text().includes("Containers"));
    expect(containersItem?.find(".nav-badge-unknown").text()).toBe("?");
    expect(containersItem?.attributes("aria-label")).toBe(
      "Containers, tracking status unavailable",
    );

    loadTracking.mockImplementation(async () => {
      tracking.inventory = trackedContainersResponse([
        trackedContainerItem({ tracking_health: "frozen" }),
      ]);
      tracking.inventoryLoadFailed = false;
    });
    await router.push("/runs");
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(2);
    expect(wrapper.find(".nav-badge-attention").text()).toBe("1");
    expect(wrapper.find(".nav-badge-unknown").exists()).toBe(false);

    await router.push("/audit");
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(2);
  });

  it("shows unknown status and retries after a failed refresh of a ready inventory", async () => {
    const stores = createAppStores();
    stores.settings.settings = settingsResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const tracking = useTrackingStore(stores.pinia);
    tracking.inventory = trackedContainersResponse([
      trackedContainerItem({ tracking_health: "frozen" }),
    ]);
    const loadTracking = vi.spyOn(tracking, "load").mockResolvedValue();

    const { router, wrapper } = await mountAppAt(stores);
    await flushPromises();
    expect(loadTracking).not.toHaveBeenCalled();
    expect(wrapper.find(".nav-badge-attention").text()).toBe("1");

    // The older inventory stays in place, but its count is no longer trusted.
    tracking.inventoryLoadFailed = true;
    await nextTick();
    expect(wrapper.find(".nav-badge-attention").exists()).toBe(false);
    expect(wrapper.find(".nav-badge-unknown").text()).toBe("?");

    await router.push("/runs");
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(1);
  });

  it("retries an unavailable sidebar inventory on navigation", async () => {
    const stores = createAppStores();
    stores.settings.settings = settingsResponse();
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const tracking = useTrackingStore(stores.pinia);
    tracking.inventory = trackedContainersResponse([], { status: "unavailable" });
    const loadTracking = vi.spyOn(tracking, "load").mockResolvedValue();

    const { router, wrapper } = await mountAppAt(stores);
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".nav-badge-unknown").text()).toBe("?");

    await router.push("/runs");
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(2);
  });

  it("loads the sidebar inventory when sidebar counts are turned on", async () => {
    const stores = createAppStores();
    const settings = settingsResponse();
    const withSidebarBadges = (value: string) => ({
      ...settings,
      managed: settings.managed.map((entry) =>
        entry.key === "sidebar_badges"
          ? { ...entry, value, source: "configured" as const }
          : entry,
      ),
    });
    stores.settings.settings = withSidebarBadges("false");
    stubAppShellLoads(stores);
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const loadTracking = vi
      .spyOn(useTrackingStore(stores.pinia), "load")
      .mockResolvedValue();

    await mountAppAt(stores);
    await flushPromises();
    expect(loadTracking).not.toHaveBeenCalled();

    stores.settings.settings = withSidebarBadges("true");
    await flushPromises();
    expect(loadTracking).toHaveBeenCalledTimes(1);
  });

  it("cycles theme preference from system to light to dark", async () => {
    const stores = createAppStores();
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stubAppShellLoads(stores);

    const { wrapper } = await mountAppAt(stores);
    const themeButton = () => buttonByAriaLabel(wrapper, "theme");

    expect(themeButton().attributes("aria-label")).toContain("System theme");
    expect(themeButton().attributes("title")).toBe("Theme: System theme (light)");

    await expectNextTheme(themeButton, "light", "light", "Light theme");
    await expectNextTheme(themeButton, "dark", "dark", "Dark theme");
    await expectNextTheme(themeButton, "auto", "light", "System theme");
  });

  it("hydrates configured managed theme preference after authentication", async () => {
    const stores = createAppStores(true);
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stores.settings.settings = settingsResponse({
      managed: [
        {
          key: "theme_preference",
          value: "dark",
          default_value: "system",
          source: "configured",
          editable: true,
          allowed_values: ["system", "light", "dark"],
          restart_required: false,
        },
        settingsResponse().managed[1],
      ],
    });
    stubAppShellLoads(stores);

    const { wrapper } = await mountAppAt(stores);
    await nextTick();

    expect(globalThis.localStorage.getItem(themeStorageKey)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(buttonByAriaLabel(wrapper, "Dark theme").exists()).toBe(true);
  });

  it("shows self-update banner and confirms release notes before applying", async () => {
    const stores = createAppStores(true);
    stores.connection.status = statusResponse({ version: "0.24.2" });
    stores.settings.coreUpdateTour = coreUpdateTourResponse();
    stores.selfUpdate.selfUpdate = selfUpdateResponse({
      release_notes_truncated: true,
    });
    stubAppShellLoads(stores);
    const applySelfUpdate = vi
      .spyOn(stores.selfUpdate, "applySelfUpdate")
      .mockResolvedValue(selfUpdateApplyResponse());

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    expectTextToContain(
      wrapper,
      "WUDup update available: v0.24.2 → v0.25.0",
      "ghcr.io/magrhino/wudup:latest",
    );
    const notice = wrapper.find("details.self-update-banner");
    expect(notice.attributes("open")).toBeUndefined();
    expect(notice.find("summary").text()).toContain("WUDup update available");
    notice.element.setAttribute("open", "");
    await clickButtonByText(wrapper, "Pull image");

    const dialog = wrapper.find('[role="dialog"]');
    expectTextToContain(
      dialog,
      "Update WUDup",
      "Update Plan",
      "Release Notes",
    );
    expect(dialog.text()).not.toContain("Adds self-update review");
    expect(applySelfUpdate).not.toHaveBeenCalled();

    await clickButtonByText(dialog, "Release Notes");

    expectTextToContain(
      dialog,
      "Release notes",
      "Cap 10",
      "Adds self-update review",
    );

    await clickButtonByText(dialog, "Pull image");

    expect(applySelfUpdate).toHaveBeenCalledTimes(1);
  });

  it("keeps self-update failures visible outside the collapsed notice", async () => {
    const stores = createAppStores(true);
    stores.selfUpdate.selfUpdate = selfUpdateResponse({
      can_update: false,
      disabled_reason: "WebUI mutations are disabled.",
    });
    stores.selfUpdate.selfUpdateError = "Image pull failed. Check registry access and try again.";
    stubAppShellLoads(stores);
    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    const notice = wrapper.find("details.self-update-banner");
    expect(notice.attributes("open")).toBeUndefined();
    expect(notice.text()).not.toContain("Image pull failed");
    expect(notice.text()).toContain("WebUI mutations are disabled.");
    expect(notice.find("button").attributes("disabled")).toBeDefined();
    expect(wrapper.find(".self-update-message").text()).toContain("Check registry access and try again");
  });

  it("shows pinned self-update tag prepare preview before applying", async () => {
    const stores = createAppStores(true);
    primePinnedSelfUpdate(stores);
    stubAppShellLoads(stores);
    const planSelfUpdate = vi
      .spyOn(stores.selfUpdate, "planSelfUpdate")
      .mockImplementation(async () => {
        const plan = selfUpdatePlanResponse();
        stores.selfUpdate.selfUpdatePlan = plan;
        return plan;
      });
    const applySelfUpdate = vi
      .spyOn(stores.selfUpdate, "applySelfUpdate")
      .mockResolvedValue(selfUpdatePrepareResponse());

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    await clickButtonByText(wrapper, "Prepare tag update");

    const dialog = wrapper.find('[role="dialog"]');
    expect(planSelfUpdate).toHaveBeenCalledTimes(1);
    expectTextToContain(
      dialog,
      "This updates the Compose image tag",
      "Compose tag update",
      "wudup",
      "ghcr.io/magrhino/wudup:v0.25.0",
    );

    await clickButtonByText(dialog, "Prepare tag update");

    expect(applySelfUpdate).toHaveBeenCalledTimes(1);
  });

  it("keeps pinned self-update confirmation disabled until preview loads", async () => {
    const stores = createAppStores(true);
    primePinnedSelfUpdate(stores);
    stubAppShellLoads(stores);
    let resolvePlan: (plan: ReturnType<typeof selfUpdatePlanResponse>) => void;
    const planPromise = new Promise<ReturnType<typeof selfUpdatePlanResponse>>(
      (resolve) => {
        resolvePlan = resolve;
      },
    );
    vi.spyOn(stores.selfUpdate, "planSelfUpdate").mockImplementation(async () => {
      const plan = await planPromise;
      stores.selfUpdate.selfUpdatePlan = plan;
      return plan;
    });
    const applySelfUpdate = vi
      .spyOn(stores.selfUpdate, "applySelfUpdate")
      .mockResolvedValue(selfUpdatePrepareResponse());

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    await clickButtonByText(wrapper, "Prepare tag update");

    const dialog = wrapper.find('[role="dialog"]');
    const confirmButton = buttonByText(dialog, "Prepare tag update");
    expect(confirmButton.attributes("disabled")).toBeDefined();
    await confirmButton.trigger("click");
    expect(applySelfUpdate).not.toHaveBeenCalled();

    resolvePlan!(selfUpdatePlanResponse());
    await flushPromises();
    expect(confirmButton.attributes("disabled")).toBeUndefined();
  });

  it("shows settings navigation and refreshes the settings route", async () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const stores = createAppStores();
    vi.spyOn(stores.connection, "loadStatus").mockResolvedValue();
    const loadSettings = vi
      .spyOn(stores.settings, "loadSettings")
      .mockResolvedValue();
    const loadOnboarding = vi
      .spyOn(stores.settings, "loadOnboarding")
      .mockResolvedValue();
    const loadCoreUpdateTour = vi
      .spyOn(stores.settings, "loadCoreUpdateTour")
      .mockResolvedValue();

    const { wrapper } = await mountAppAt(stores, "/settings");
    await flushPromises();

    expect(wrapper.text()).toContain("Settings");
    const settingsItem = wrapper
      .findAll(".nav-item")
      .find((item) => item.text().includes("Settings"));
    expect(scrollIntoView.mock.contexts).toContain(settingsItem?.element);
    loadSettings.mockClear();
    loadCoreUpdateTour.mockClear();
    await wrapper.find('button[aria-label="Refresh current view status"]').trigger("click");
    expect(loadSettings).toHaveBeenCalledTimes(1);
    expect(loadOnboarding).toHaveBeenCalledTimes(1);
    expect(loadCoreUpdateTour).toHaveBeenCalledTimes(1);
  });

  it("refreshes the audit route from the shell", async () => {
    const stores = createAppStores();
    vi.spyOn(stores.connection, "loadStatus").mockResolvedValue();
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    const loadRuns = vi.spyOn(stores.runs, "loadRuns").mockResolvedValue();

    const { wrapper } = await mountAppAt(stores, "/audit");
    await flushPromises();

    const navItems = wrapper.findAll(".nav-item");
    const historyItem = navItems.find((item) => item.text().includes("History"));
    expect(historyItem?.classes()).toContain("nav-item-active");
    expect(historyItem?.attributes("aria-current")).toBe("page");
    expect(navItems.some((item) => item.text().includes("Audit log"))).toBe(false);
    expect(wrapper.find("h1").text()).toBe("History");
    loadRuns.mockClear();
    await wrapper.find('button[aria-label="Refresh current view status"]').trigger("click");
    expect(loadRuns).toHaveBeenCalledTimes(1);
  });

  it("shows retag navigation and refreshes the retags route", async () => {
    const stores = createAppStores();
    const retags = useRetagsStore(stores.pinia);
    retags.retagTargets = retagTargetsResponse();
    vi.spyOn(stores.connection, "loadStatus").mockResolvedValue();
    vi.spyOn(stores.settings, "loadSettings").mockResolvedValue();
    vi.spyOn(stores.settings, "loadCoreUpdateTour").mockResolvedValue();
    vi.spyOn(stores.selfUpdate, "loadSelfUpdate").mockResolvedValue();
    const loadRetagTargets = vi
      .spyOn(retags, "loadRetagTargets")
      .mockResolvedValue();

    const { router, wrapper } = await mountAppAt(stores, "/retags");
    await flushPromises();

    const navItems = wrapper.findAll(".nav-item");
    const retagsItem = navItems.find((item) => item.text().includes("Retags"));
    expect(router.currentRoute.value.name).toBe("retags");
    expect(retagsItem?.classes()).toContain("nav-item-active");
    expect(retagsItem?.attributes("aria-current")).toBe("page");
    expect(wrapper.find("h1").text()).toBe("Retags");
    loadRetagTargets.mockClear();
    await wrapper.find('button[aria-label="Refresh current view status"]').trigger("click");
    expect(loadRetagTargets).toHaveBeenCalledTimes(1);
  });
});
