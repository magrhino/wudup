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
} from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

const adminPassword = "correct horse battery staple";

type WrapperQuery = VueWrapper | DOMWrapper<Element>;

type AppStores = {
  pinia: Pinia;
  connection: ReturnType<typeof useConnectionStore>;
  settings: ReturnType<typeof useSettingsStore>;
  updates: ReturnType<typeof useUpdatesStore>;
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
  stores.updates.selfUpdate = selfUpdateResponse({
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
    stores.updates.selfUpdate = selfUpdateResponse({
      release_notes_truncated: true,
    });
    stubAppShellLoads(stores);
    const applySelfUpdate = vi
      .spyOn(stores.updates, "applySelfUpdate")
      .mockResolvedValue(selfUpdateApplyResponse());

    const { wrapper } = await mountAppAt(stores);
    await flushPromises();

    expectTextToContain(
      wrapper,
      "Update available: v0.24.2 → v0.25.0",
      "ghcr.io/magrhino/wudup:latest",
    );
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

  it("shows pinned self-update tag prepare preview before applying", async () => {
    const stores = createAppStores(true);
    primePinnedSelfUpdate(stores);
    stubAppShellLoads(stores);
    const planSelfUpdate = vi
      .spyOn(stores.updates, "planSelfUpdate")
      .mockImplementation(async () => {
        const plan = selfUpdatePlanResponse();
        stores.updates.selfUpdatePlan = plan;
        return plan;
      });
    const applySelfUpdate = vi
      .spyOn(stores.updates, "applySelfUpdate")
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
    vi.spyOn(stores.updates, "planSelfUpdate").mockImplementation(async () => {
      const plan = await planPromise;
      stores.updates.selfUpdatePlan = plan;
      return plan;
    });
    const applySelfUpdate = vi
      .spyOn(stores.updates, "applySelfUpdate")
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
    vi.spyOn(stores.updates, "loadSelfUpdate").mockResolvedValue();
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
