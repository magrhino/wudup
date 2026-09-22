import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PoliciesView from "../src/views/PoliciesView.vue";
import SettingsView from "../src/views/SettingsView.vue";
import SettingsJumpNav from "../src/views/settings/SettingsJumpNav.vue";
import SnoozesView from "../src/views/SnoozesView.vue";
import TagExclusionsView from "../src/views/TagExclusionsView.vue";
import {
  RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND,
} from "../src/releaseNotifications";
import { SETTINGS_NAV_GROUPS } from "../src/views/settings/settingsDisplay";
import {
  coreUpdateTourResponse,
  onboardingChecklistResponse,
  servicePolicy,
  settingsResponse,
  snooze,
  statusResponse,
  tagExclusion,
  updateTarget,
  updateTargetsResponse,
} from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";
import {
  buttonByText,
  emitSelectValue,
  setupStores,
} from "./helpers/viewSecurity";

function mutationEnabledSettingsResponse(
  overrides: Parameters<typeof settingsResponse>[0] = {},
) {
  return settingsResponse({
    webui: settingsResponse().webui.map((entry) =>
      entry.name === "WUD_WEB_MUTATIONS_ENABLED"
        ? { ...entry, value: "true", configured: true, source: "configured" as const }
        : entry,
    ),
    ...overrides,
  });
}

function configuredWebhookSettingsResponse() {
  return mutationEnabledSettingsResponse({
    managed: settingsResponse().managed.map((entry) =>
      entry.key === "release_notifications_discord_webhook"
        ? { ...entry, configured: true, source: "configured" as const }
        : entry,
    ),
  });
}

function mockContainerRestart(connection: ReturnType<typeof setupStores>["connection"]) {
  return vi.spyOn(connection, "restartContainer").mockResolvedValue({
    status: "scheduled",
    audit_run_id: 42,
    container: "wudup",
  });
}

describe("settings mutation views", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("disables policy mutations in read-only mode", async () => {
    const { pinia, settings, updates } = setupStores(false);
    settings.servicePolicies = [servicePolicy()];
    updates.updateTargets = updateTargetsResponse();
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();
    const deletePolicy = vi.spyOn(settings, "deleteServicePolicy");
    const wrapper = mountWithApp(PoliciesView, { pinia });

    expect(wrapper.text()).toContain("Read-only mode is active");
    expect(buttonByText(wrapper.text(), "Save")).toBe(true);
    expect(
      wrapper.findAll("button").find((button) => button.text().includes("Save"))?.attributes(
        "disabled",
      ),
    ).toBeDefined();
    const deleteButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Delete"));
    expect(deleteButton?.attributes("disabled")).toBeDefined();
    await deleteButton?.trigger("click");
    expect(deletePolicy).not.toHaveBeenCalled();
  });

  it("shows policy target loading errors from the updates store", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.servicePolicies = [];
    vi.spyOn(updates, "loadUpdateTargets").mockImplementation(async () => {
      updates.error = "update targets unavailable";
      throw new Error("update targets unavailable");
    });
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();

    const wrapper = mountWithApp(PoliciesView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("update targets unavailable");
  });

  it("shows policy status loading errors from the connection store", async () => {
    const { pinia, connection, settings, updates } = setupStores(true);
    connection.status = null;
    settings.servicePolicies = [];
    vi.spyOn(connection, "loadStatus").mockImplementation(async () => {
      connection.error = "status unavailable";
      throw new Error("status unavailable");
    });
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();

    const wrapper = mountWithApp(PoliciesView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("status unavailable");
  });

  it("does not assume UTC while policy schedule timezone is loading", async () => {
    const { pinia, connection, settings, updates } = setupStores(true);
    connection.status = null;
    settings.servicePolicies = [servicePolicy()];
    updates.updateTargets = updateTargetsResponse();
    vi.spyOn(connection, "loadStatus").mockResolvedValue();
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();
    const wrapper = mountWithApp(PoliciesView, { pinia });
    await flushPromises();

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Edit"))
      ?.trigger("click");
    await flushPromises();

    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save policy"));
    expect(wrapper.text()).toContain("Update time (loading)");
    expect(wrapper.text()).not.toContain("Update time (UTC)");
    expect(saveButton?.attributes("disabled")).toBeDefined();

    connection.status = statusResponse({
      mutations_enabled: true,
      timezone: "America/Chicago",
    });
    await nextTick();

    expect(wrapper.text()).toContain("Update time (America/Chicago)");
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text().includes("Save policy"))
        ?.attributes("disabled"),
    ).toBeUndefined();
  });

  it("saves scheduled policy fields after server timezone is known", async () => {
    const { pinia, connection, settings, updates } = setupStores(true);
    connection.status = statusResponse({
      mutations_enabled: true,
      timezone: "America/Chicago",
    });
    settings.servicePolicies = [
      servicePolicy({
        auto_update_time: "09:30",
        auto_update_days: ["mon", "fri"],
      }),
    ];
    updates.updateTargets = updateTargetsResponse();
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();
    const upsertPolicy = vi
      .spyOn(settings, "upsertServicePolicy")
      .mockResolvedValue();
    const wrapper = mountWithApp(PoliciesView, { pinia });
    await flushPromises();

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Edit"))
      ?.trigger("click");
    await flushPromises();
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Save policy"))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("Update time (America/Chicago)");
    expect(upsertPolicy).toHaveBeenCalledWith(
      "media/app",
      "stop",
      true,
      null,
      "09:30",
      ["mon", "fri"],
    );
  });

  it("offers discovered services and image repositories on management forms", async () => {
    const { pinia, settings, updates } = setupStores(true);
    updates.updateTargets = updateTargetsResponse([
      updateTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "lscr.io/linuxserver/radarr:5.21.1",
        image_repo: "linuxserver/radarr",
        current_tag: "5.21.1",
      }),
    ]);
    settings.tagExclusions = [];
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadTagExclusions").mockResolvedValue();
    const wrapper = mountWithApp(TagExclusionsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("linuxserver/radarr");

    await wrapper.findAll("select")[1]?.setValue("linuxserver/radarr");
    await nextTick();

    expect(wrapper.findAll("select")[2]?.text()).toContain("5.21.1");
  });

  it("keeps clearable management selects string-safe", async () => {
    {
      const { pinia, settings, updates } = setupStores(true);
      settings.servicePolicies = [servicePolicy()];
      updates.updateTargets = updateTargetsResponse();
      vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
      vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();
      const wrapper = mountWithApp(PoliciesView, { pinia });
      await flushPromises();

      emitSelectValue(wrapper, 0, null);
      await nextTick();

      expect(
        wrapper
          .findAll("button")
          .find((button) => button.text().includes("Save policy"))
          ?.attributes("disabled"),
      ).toBeDefined();
    }

    {
      const { pinia, settings, updates } = setupStores(true);
      settings.snoozes = [];
      updates.updateTargets = updateTargetsResponse();
      vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
      vi.spyOn(settings, "loadSnoozes").mockResolvedValue();
      const wrapper = mountWithApp(SnoozesView, { pinia });
      await flushPromises();

      emitSelectValue(wrapper, 1, null);
      await nextTick();

      expect(
        wrapper
          .findAll("button")
          .find((button) => button.text().includes("Create snooze"))
          ?.attributes("disabled"),
      ).toBeDefined();
    }

    {
      const { pinia, settings, updates } = setupStores(true);
      settings.tagExclusions = [];
      updates.updateTargets = updateTargetsResponse();
      vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
      vi.spyOn(settings, "loadTagExclusions").mockResolvedValue();
      const wrapper = mountWithApp(TagExclusionsView, { pinia });
      await flushPromises();

      emitSelectValue(wrapper, 1, null);
      emitSelectValue(wrapper, 2, null);
      await nextTick();

      expect(
        wrapper
          .findAll("button")
          .find((button) => button.text().includes("Save rule"))
          ?.attributes("disabled"),
      ).toBeDefined();
    }
  });

  it("creates dependency snoozes from the snooze form", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.snoozes = [];
    updates.updateTargets = updateTargetsResponse([
      updateTarget({ service_key: "media/app", service: "app" }),
      updateTarget({ service_key: "media/db", service: "db" }),
    ]);
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadSnoozes").mockResolvedValue();
    const createDependencySnooze = vi
      .spyOn(settings, "createDependencySnooze")
      .mockResolvedValue();
    const wrapper = mountWithApp(SnoozesView, { pinia });
    await flushPromises();

    emitSelectValue(wrapper, 0, "dependency");
    await nextTick();
    emitSelectValue(wrapper, 1, "media/app");
    emitSelectValue(wrapper, 2, "media/db");
    await wrapper.find('input[placeholder="maintenance"]').setValue("wait for db");
    await wrapper.find("form").trigger("submit");
    await flushPromises();

    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.text()).toContain("Dependency");
    expect(dialog.text()).toContain("Until media/db updates successfully");

    await dialog
      .findAll("button")
      .find((button) => button.text().includes("Create snooze"))
      ?.trigger("click");
    await flushPromises();

    expect(createDependencySnooze).toHaveBeenCalledWith(
      "media/app",
      "media/db",
      "wait for db",
      "active",
    );
  });

  it("disables snooze mutations in read-only mode", async () => {
    const { pinia, settings, updates } = setupStores(false);
    settings.snoozes = [snooze()];
    updates.updateTargets = updateTargetsResponse();
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadSnoozes").mockResolvedValue();
    const deleteSnooze = vi.spyOn(settings, "deleteSnooze");
    const wrapper = mountWithApp(SnoozesView, { pinia });

    expect(wrapper.text()).toContain("Read-only mode is active");
    expect(
      wrapper.findAll("button").find((button) => button.text().includes("Create"))?.attributes(
        "disabled",
      ),
    ).toBeDefined();
    const deleteButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Delete"));
    expect(deleteButton?.attributes("disabled")).toBeDefined();
    await deleteButton?.trigger("click");
    expect(deleteSnooze).not.toHaveBeenCalled();
  });

  it("shows snooze target loading errors from the updates store", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.snoozes = [];
    vi.spyOn(updates, "loadUpdateTargets").mockImplementation(async () => {
      updates.error = "snooze targets unavailable";
      throw new Error("snooze targets unavailable");
    });
    vi.spyOn(settings, "loadSnoozes").mockResolvedValue();

    const wrapper = mountWithApp(SnoozesView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("snooze targets unavailable");
  });

  it("disables tag exclusion mutations in read-only mode", async () => {
    const { pinia, settings, updates } = setupStores(false);
    settings.tagExclusions = [tagExclusion()];
    updates.updateTargets = updateTargetsResponse();
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadTagExclusions").mockResolvedValue();
    const setStatus = vi.spyOn(settings, "setTagExclusionStatus");
    const wrapper = mountWithApp(TagExclusionsView, { pinia });

    expect(wrapper.text()).toContain("Read-only mode is active");
    expect(
      wrapper.findAll("button").find((button) => button.text().includes("Save"))?.attributes(
        "disabled",
      ),
    ).toBeDefined();
    const disableButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Disable"));
    expect(disableButton?.attributes("disabled")).toBeDefined();
    await disableButton?.trigger("click");
    expect(setStatus).not.toHaveBeenCalled();
  });

  it("shows tag exclusion target loading errors from the updates store", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.tagExclusions = [];
    vi.spyOn(updates, "loadUpdateTargets").mockImplementation(async () => {
      updates.error = "tag targets unavailable";
      throw new Error("tag targets unavailable");
    });
    vi.spyOn(settings, "loadTagExclusions").mockResolvedValue();

    const wrapper = mountWithApp(TagExclusionsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("tag targets unavailable");
  });

  it("resolves an exclusion deep link after targets load and when its service changes", async () => {
    const { pinia, settings, updates } = setupStores(true);
    let finishLoad: (() => void) | undefined;
    vi.spyOn(updates, "loadUpdateTargets").mockImplementation(async () => {
      await new Promise<void>((resolve) => { finishLoad = resolve; });
      updates.updateTargets = updateTargetsResponse([
        updateTarget(),
        updateTarget({ service_key: "media/other", image_repo: "repo/other" }),
      ]);
    });
    vi.spyOn(settings, "loadTagExclusions").mockResolvedValue();
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/tag-exclusions", component: TagExclusionsView }],
    });
    await router.push("/tag-exclusions?service=media/app");
    await router.isReady();
    const wrapper = mountWithApp(TagExclusionsView, { pinia, router });

    expect((wrapper.find('select[placeholder="repo/app"]').element as HTMLSelectElement).value).toBe("");
    finishLoad?.();
    await flushPromises();
    expect((wrapper.find('select[placeholder="repo/app"]').element as HTMLSelectElement).value).toBe("repo/app");

    await router.push("/tag-exclusions?service=media/other");
    await flushPromises();
    expect((wrapper.find('select[placeholder="repo/app"]').element as HTMLSelectElement).value).toBe("repo/other");
  });

  it("follows the policy service deep link on mount and route changes", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.servicePolicies = [];
    updates.updateTargets = updateTargetsResponse([
      updateTarget(), updateTarget({ service_key: "media/other" }),
    ]);
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadServicePolicies").mockResolvedValue();
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/policies", component: PoliciesView }],
    });
    await router.push("/policies?service=media/app");
    await router.isReady();
    const wrapper = mountWithApp(PoliciesView, { pinia, router });
    await flushPromises();
    const service = wrapper.find('select[placeholder="stack/service"]');
    expect((service.element as HTMLSelectElement).value).toBe("media/app");

    await router.push("/policies?service=media/other");
    await flushPromises();
    expect((service.element as HTMLSelectElement).value).toBe("media/other");
  });

  it("follows the snooze service deep link on mount and route changes", async () => {
    const { pinia, settings, updates } = setupStores(true);
    settings.snoozes = [];
    updates.updateTargets = updateTargetsResponse([
      updateTarget(), updateTarget({ service_key: "media/other" }),
    ]);
    vi.spyOn(updates, "loadUpdateTargets").mockResolvedValue();
    vi.spyOn(settings, "loadSnoozes").mockResolvedValue();
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/snoozes", component: SnoozesView }],
    });
    await router.push("/snoozes?service=media/app");
    await router.isReady();
    const wrapper = mountWithApp(SnoozesView, { pinia, router });
    await flushPromises();
    const service = wrapper.findAll('select[placeholder="stack/service"]')[0];
    expect((service.element as HTMLSelectElement).value).toBe("media/app");

    await router.push("/snoozes?service=media/other");
    await flushPromises();
    expect((service.element as HTMLSelectElement).value).toBe("media/other");
  });

  it("renders read-only settings without exposing secret values or edit controls", async () => {
    const { pinia, settings } = setupStores(false);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.findComponent(SettingsJumpNav).exists()).toBe(true);
    SETTINGS_NAV_GROUPS.forEach((group) => {
      const groupElement = wrapper.find(
        `[data-test="settings-nav-group-${group.id}"]`,
      );
      expect(groupElement.exists()).toBe(true);
      expect(groupElement.find("ul").exists()).toBe(true);
      expect(groupElement.find("h2").text()).toBe(group.label);
      group.links.forEach((link) => {
        const itemElement = groupElement.find(
          `[data-test="settings-nav-item-${link.id}"]`,
        );
        expect(itemElement.exists()).toBe(true);
        expect(itemElement.element.tagName).toBe("LI");
        expect(itemElement.text()).toContain(link.label);
      });
    });

    const text = wrapper.text();
    expect(text).toContain("Settings map");
    expect(text).toContain("Runtime settings");
    expect(text).toContain("DOCKER_BASE");
    expect(text).toContain("WUD_WEB_PUBLIC_ORIGIN");
    expect(text).toContain(
      "Download or copy a redacted bundle for troubleshooting.",
    );
    expect(text).toContain(
      "Environment variables, paths, and secrets are scrubbed.",
    );
    expect(text).not.toContain("Generate a redacted support bundle");
    expect(text).not.toContain("Raw environment variables, private paths");
    expect(text).toContain("GITHUB_TOKEN");
    expect(text).toContain("Configured");
    expect(text).toContain("Not configured");
    expect(text).not.toContain("Copy env snippet");
    expect(text).not.toContain("Copy Compose override");
    expect(text).not.toContain('DOCKER_BASE="');
    expect(text).not.toContain("github-token-secret");
    expect(text).not.toContain("Save settings");
    expect(text).not.toContain("Delete settings");
    expect(
      wrapper.find(
        'a[href="https://github.com/magrhino/wudup/blob/main/docs/DEPLOYMENT.md#environment-variables"]',
      ).exists(),
    ).toBe(true);
  });

  it("disables managed preference saves in read-only settings", async () => {
    const { pinia, settings } = setupStores(false);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse().managed,
        audit_run_id: 77,
      });
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockResolvedValue(coreUpdateTourResponse());

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("WebUI preferences");
    expect(wrapper.text()).toContain("Read-only mode is active");
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save preferences"));
    const relaunchButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Relaunch onboarding"));
    const dismissTourButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Dismiss tour"));
    const replayTourButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Replay tour"));
    expect(saveButton?.attributes("disabled")).toBeDefined();
    expect(relaunchButton?.attributes("disabled")).toBeDefined();
    expect(dismissTourButton?.attributes("disabled")).toBeDefined();
    expect(replayTourButton?.attributes("disabled")).toBeDefined();
    for (const select of wrapper.findAll("select")) {
      expect(select.attributes("disabled")).toBeDefined();
    }
    await saveButton?.trigger("click");
    await relaunchButton?.trigger("click");
    await dismissTourButton?.trigger("click");
    await replayTourButton?.trigger("click");
    expect(updateManagedSettings).not.toHaveBeenCalled();
    expect(updateCoreUpdateTour).not.toHaveBeenCalled();
  });

  it("saves managed preference changes through the store", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = mutationEnabledSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse({
          managed: [
            {
              key: "theme_preference",
              value: "dark",
              default_value: "system",
              source: "configured",
              editable: true,
              allowed_values: ["system", "light", "dark"],
              restart_required: false,
              disabled_reason: "",
            },
            settingsResponse().managed[1],
          ],
        }).managed,
        audit_run_id: 77,
      });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    await wrapper.findAll("select")[0].setValue("dark");
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save preferences"));
    await saveButton?.trigger("click");
    await flushPromises();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      theme_preference: "dark",
    });
    expect(wrapper.text()).toContain("Preferences saved. Audit run #77.");
  });

  it("saves release-note notification settings from the notifications section", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = mutationEnabledSettingsResponse({
      managed: settingsResponse().managed.map((entry) =>
        entry.key === "release_notifications_mode"
          ? { ...entry, value: "per_container", source: "configured" as const }
          : entry,
      ),
    });
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse({
          managed: settingsResponse().managed.map((entry) => {
            if (entry.key === "release_notes_enabled") {
              return { ...entry, value: "true", source: "configured" as const };
            }
            if (entry.key === "release_notifications_delivery_mode") {
              return {
                ...entry,
                value: RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND,
                source: "configured" as const,
              };
            }
            if (entry.key === "release_notifications_mode") {
              return {
                ...entry,
                value: "digest",
                source: "configured" as const,
              };
            }
            if (entry.key === "release_notifications_resend_policy") {
              return { ...entry, value: "cooldown", source: "configured" as const };
            }
            if (entry.key === "release_notifications_cooldown_seconds") {
              return { ...entry, value: "60", source: "configured" as const };
            }
            if (entry.key === "release_notifications_discord_webhook") {
              return {
                ...entry,
                configured: true,
                source: "configured" as const,
              };
            }
            if (entry.key === "release_notifications_verbosity") {
              return { ...entry, value: "full", source: "configured" as const };
            }
            return entry;
          }),
        }).managed,
        audit_run_id: 78,
      });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    expect(wrapper.text()).toContain(
      "Summary sends one categorized batch and is unrelated to container image digests.",
    );
    expect(wrapper.text()).toContain(
      "Per container sends one detailed notification for each update.",
    );
    await wrapper.find('input[role="switch"]').setValue(true);
    await wrapper
      .find('input[aria-label="Discord webhook URL"]')
      .setValue("https://discord.com/api/webhooks/123/token-secret");
    emitSelectValue(wrapper, 4, RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND);
    emitSelectValue(wrapper, 5, "full");
    emitSelectValue(wrapper, 6, "digest");
    emitSelectValue(wrapper, 7, "cooldown");
    await wrapper
      .find('input[aria-label="Release notification cooldown seconds"]')
      .setValue("00060");
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save notifications"));
    await saveButton?.trigger("click");
    await flushPromises();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      release_notes_enabled: "true",
      release_notifications_delivery_mode:
        RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND,
      release_notifications_mode: "digest",
      release_notifications_resend_policy: "cooldown",
      release_notifications_cooldown_seconds: "60",
      release_notifications_discord_webhook:
        "https://discord.com/api/webhooks/123/token-secret",
      release_notifications_verbosity: "full",
    });
    expect(wrapper.text()).toContain("Notification settings saved. Audit run #78.");
  });

  it("clears a configured Discord webhook from the notifications section", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = configuredWebhookSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse().managed,
        audit_run_id: 79,
      });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const clearButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Clear webhook"));
    await clearButton?.trigger("click");
    await flushPromises();
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save notifications"));
    await saveButton?.trigger("click");
    await flushPromises();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      release_notifications_discord_webhook: "",
    });
    expect(wrapper.text()).toContain("Notification settings saved. Audit run #79.");
  });

  it("sends a configured test webhook after confirmation", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = configuredWebhookSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const testWebhook = vi
      .spyOn(settings, "testReleaseNotificationWebhook")
      .mockResolvedValue({
        sent: true,
        destination: {
          type: "discord",
          configured: true,
          source: "WebUI settings",
        },
        audit_run_id: 80,
      });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const sendButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    expect(sendButton?.attributes("disabled")).toBeUndefined();
    await sendButton?.trigger("click");

    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.text()).toContain("Send test webhook");
    expect(testWebhook).not.toHaveBeenCalled();

    const confirmButton = dialog
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    await confirmButton?.trigger("click");
    await flushPromises();

    expect(testWebhook).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("Test webhook sent. Audit run #80.");
  });

  it("shows an error when sending the test webhook fails", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = configuredWebhookSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const testWebhook = vi
      .spyOn(settings, "testReleaseNotificationWebhook")
      .mockRejectedValue(
        new Error("Discord release-note webhook is not configured"),
      );

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const sendButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    await sendButton?.trigger("click");

    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.text()).toContain("Send test webhook");

    const confirmButton = dialog
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    await confirmButton?.trigger("click");
    await flushPromises();

    expect(testWebhook).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
    expect(wrapper.find('[role="alert"]').text()).toContain(
      "Discord release-note webhook is not configured",
    );
    expect(wrapper.text()).not.toContain("Test webhook sent");
  });

  it("blocks test webhook while dirty, unconfigured, or read-only", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = configuredWebhookSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const testWebhook = vi.spyOn(settings, "testReleaseNotificationWebhook");
    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    await wrapper
      .find('input[aria-label="Discord webhook URL"]')
      .setValue("https://discord.com/api/webhooks/123/new-token-secret");
    const dirtyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    expect(dirtyButton?.attributes("disabled")).toBeDefined();
    await dirtyButton?.trigger("click");
    expect(testWebhook).not.toHaveBeenCalled();

    const unconfigured = setupStores(true);
    unconfigured.settings.settings = mutationEnabledSettingsResponse();
    unconfigured.settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(unconfigured.settings, "loadSettings").mockResolvedValue();
    const unconfiguredWrapper = mountWithApp(SettingsView, {
      pinia: unconfigured.pinia,
    });
    await flushPromises();
    const unconfiguredButton = unconfiguredWrapper
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    expect(unconfiguredButton?.attributes("disabled")).toBeDefined();

    const readOnly = setupStores(false);
    readOnly.settings.settings = settingsResponse({
      managed: settingsResponse().managed.map((entry) =>
        entry.key === "release_notifications_discord_webhook"
          ? { ...entry, configured: true, source: "configured" as const }
          : entry,
      ),
    });
    readOnly.settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(readOnly.settings, "loadSettings").mockResolvedValue();
    const readOnlyWrapper = mountWithApp(SettingsView, { pinia: readOnly.pinia });
    await flushPromises();
    const readOnlyButton = readOnlyWrapper
      .findAll("button")
      .find((button) => button.text().includes("Send test"));
    expect(readOnlyButton?.attributes("disabled")).toBeDefined();
  });

  it("validates release-note notification cooldown before saving", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi.spyOn(settings, "updateManagedSettings");

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    await wrapper
      .find('input[aria-label="Release notification cooldown seconds"]')
      .setValue("0");
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save notifications"));
    await saveButton?.trigger("click");
    await flushPromises();

    expect(updateManagedSettings).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain(
      "Release notification cooldown must be a positive integer.",
    );
  });

  it("does not dirty release-note cooldown for equivalent normalized values", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = mutationEnabledSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi.spyOn(settings, "updateManagedSettings");

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    await wrapper
      .find('input[aria-label="Release notification cooldown seconds"]')
      .setValue("086400");
    const saveButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Save notifications"));

    expect(saveButton?.attributes("disabled")).toBeDefined();
    await saveButton?.trigger("click");
    await flushPromises();
    expect(updateManagedSettings).not.toHaveBeenCalled();
  });

  it("keeps release-note notification preferences read-only when env configured", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = settingsResponse({
      managed: settingsResponse().managed.map((entry) => {
        const envNames: Record<string, string> = {
          release_notes_enabled: "WUD_RELEASE_NOTES_ENABLED",
          release_notifications_mode: "WUD_RELEASE_NOTIFICATIONS_MODE",
          release_notifications_resend_policy:
            "WUD_RELEASE_NOTIFICATIONS_RESEND_POLICY",
          release_notifications_cooldown_seconds:
            "WUD_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS",
          release_notifications_discord_webhook: "DISCORD_WEBHOOK",
        };
        const envName = envNames[entry.key];
        return envName
          ? {
              ...entry,
              value: entry.key === "release_notes_enabled" ? "true" : entry.value,
              configured:
                entry.key === "release_notifications_discord_webhook"
                  ? true
                  : entry.configured,
              source: "configured" as const,
              editable: false,
              sensitive:
                entry.key === "release_notifications_discord_webhook"
                  ? true
                  : entry.sensitive,
              disabled_reason: `Set by ${envName}; remove it to manage this from the WebUI.`,
            }
          : entry;
      }),
    });
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.find('input[role="switch"]').attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("WUD_RELEASE_NOTES_ENABLED");
    expect(wrapper.text()).toContain("WUD_RELEASE_NOTIFICATIONS_MODE");
    expect(wrapper.text()).toContain("WUD_RELEASE_NOTIFICATIONS_RESEND_POLICY");
    expect(wrapper.text()).toContain("WUD_RELEASE_NOTIFICATIONS_COOLDOWN_SECONDS");
    expect(wrapper.text()).toContain("DISCORD_WEBHOOK");
    expect(
      wrapper.find('input[aria-label="Discord webhook URL"]').attributes("disabled"),
    ).toBeDefined();
  });

  it("relaunches the onboarding checklist from settings", async () => {
    const { pinia, settings } = setupStores(true);
    const visibleOnboardingEntry = settingsResponse().managed[1];
    const dismissedOnboardingEntry = {
      ...visibleOnboardingEntry,
      value: "dismissed",
      source: "configured" as const,
    };
    const visibleSettings = settingsResponse({
      managed: [settingsResponse().managed[0], visibleOnboardingEntry],
    });
    settings.settings = settingsResponse({
      managed: [settingsResponse().managed[0], dismissedOnboardingEntry],
    });
    settings.onboarding = onboardingChecklistResponse({
      dismissed: true,
      dismissed_at: "2026-05-31T00:00:00+00:00",
      visible: false,
      items: [],
    });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockImplementation(async (values) => {
        settings.settings = visibleSettings;
        settings.onboarding = onboardingChecklistResponse();
        return {
          managed: visibleSettings.managed,
          audit_run_id: 88,
        };
      });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const relaunchButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Relaunch onboarding"));
    await relaunchButton?.trigger("click");
    await flushPromises();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      onboarding_checklist: "visible",
    });
    expect(wrapper.text()).toContain("Onboarding checklist relaunched. Audit run #88.");
    expect(wrapper.text()).toContain("Setup checklist");
  });

  it("requires warning confirmation before restarting the WebUI container", async () => {
    const { pinia, connection, settings } = setupStores(true);
    settings.settings = mutationEnabledSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const restartContainer = mockContainerRestart(connection);

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    const restartButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Restart container"));
    expect(restartButton?.attributes("disabled")).toBeUndefined();
    await restartButton?.trigger("click");

    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.text()).toContain("Restart WebUI container");
    expect(dialog.text()).toContain("wudup");
    expect(restartContainer).not.toHaveBeenCalled();

    const confirmButton = dialog
      .findAll("button")
      .find((button) => button.text().includes("Restart container"));
    await confirmButton?.trigger("click");
    await flushPromises();

    expect(restartContainer).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("Restart requested for wudup");
  });

  it("blocks container restart controls while restart is pending", async () => {
    const { pinia, connection, settings } = setupStores(true);
    settings.settings = mutationEnabledSettingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const restartContainer = mockContainerRestart(connection);

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    connection.loading = true;
    await nextTick();

    const restartButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Restart container"));
    expect(restartButton?.attributes("disabled")).toBeDefined();
    await restartButton?.trigger("click");
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);

    connection.loading = false;
    await nextTick();
    await restartButton?.trigger("click");
    await flushPromises();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);

    connection.loading = true;
    await nextTick();

    const confirmButton = wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Restart container"));
    expect(confirmButton?.attributes("disabled")).toBeDefined();
    await confirmButton?.trigger("click");

    expect(restartContainer).not.toHaveBeenCalled();
  });

  it("disables container restart in read-only settings", async () => {
    const { pinia, connection, settings } = setupStores(false);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const restartContainer = vi.spyOn(connection, "restartContainer").mockResolvedValue({
      status: "scheduled",
      audit_run_id: 42,
      container: "wudup",
    });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("Read-only mode is active");
    const restartButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Restart container"));
    expect(restartButton?.attributes("disabled")).toBeDefined();
    await restartButton?.trigger("click");
    expect(restartContainer).not.toHaveBeenCalled();
  });
});
