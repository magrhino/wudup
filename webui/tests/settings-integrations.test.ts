import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { webApi } from "../src/api/client";
import type { ReadonlyTokenStatus } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import SettingsIntegrationsSection from "../src/views/settings/SettingsIntegrationsSection.vue";
import { settingsResponse } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";
import { setupStores } from "./helpers/viewSecurity";

const GENERATED = "wudup_ro_example-generated-token";

function tokenStatus(overrides: Partial<ReadonlyTokenStatus> = {}): ReadonlyTokenStatus {
  return {
    source: "none",
    fingerprint: "",
    created_at: "",
    editable: true,
    disabled_reason: "",
    ...overrides,
  };
}

function mountSection(status: ReadonlyTokenStatus) {
  const stores = setupStores(true);
  stores.settings.settings = settingsResponse({ readonly_token: status });
  vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf");
  const wrapper = mountWithApp(SettingsIntegrationsSection, { pinia: stores.pinia });
  return { ...stores, wrapper };
}

function clickButton(
  wrapper: ReturnType<typeof mountWithApp>,
  text: string,
  scope = "",
) {
  const root = scope ? wrapper.find(scope) : wrapper;
  const button = root.findAll("button").find((item) => item.text() === text);
  if (!button) {
    throw new Error(`Missing button ${text}`);
  }
  return button.trigger("click");
}

describe("settings integrations section", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("generates a token, shows it once, and keeps it out of the store", async () => {
    const created = tokenStatus({
      source: "webui",
      fingerprint: "7d2d66ce",
      created_at: "2026-11-02T22:11:37+00:00",
    });
    const request = vi.spyOn(webApi, "readonlyToken").mockResolvedValue({
      status: created,
      token: GENERATED,
      audit_run_id: 3,
    });
    const { wrapper, settings } = mountSection(tokenStatus());

    expect(wrapper.find('[data-test="readonly-token-source"]').text()).toBe("Not set");
    await wrapper.find('[data-test="readonly-token-rotate"]').trigger("click");
    await clickButton(wrapper, "Generate token", '[role="dialog"]');
    await flushPromises();

    expect(request).toHaveBeenCalledWith("rotate", "csrf");
    expect(wrapper.find('[data-test="readonly-token-once"]').text()).toContain(GENERATED);
    expect(settings.settings?.readonly_token).toEqual(created);
    expect(JSON.stringify(settings.$state)).not.toContain(GENERATED);
    expect(wrapper.text()).toContain("7d2d66ce");

    await clickButton(wrapper, "Done");
    expect(wrapper.find('[data-test="readonly-token-once"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain(GENERATED);
  });

  it("revokes a WebUI token after confirmation", async () => {
    const request = vi.spyOn(webApi, "readonlyToken").mockResolvedValue({
      status: tokenStatus(),
      token: "",
      audit_run_id: 4,
    });
    const { wrapper } = mountSection(
      tokenStatus({ source: "webui", fingerprint: "abcd1234" }),
    );

    await wrapper.find('[data-test="readonly-token-revoke"]').trigger("click");
    expect(wrapper.text()).toContain("get 401 responses");
    await clickButton(wrapper, "Revoke token", '[role="dialog"]');
    await flushPromises();

    expect(request).toHaveBeenCalledWith("revoke", "csrf");
    expect(wrapper.find('[data-test="readonly-token-source"]').text()).toBe("Not set");
    expect(wrapper.find('[data-test="readonly-token-revoke"]').exists()).toBe(false);
  });

  it("locks the controls when container config owns the token", () => {
    const request = vi.spyOn(webApi, "readonlyToken");
    const { wrapper } = mountSection(
      tokenStatus({
        source: "env",
        fingerprint: "11112222",
        editable: false,
        disabled_reason: "Set by WUD_WEB_READONLY_TOKEN in container config.",
      }),
    );

    const rotate = wrapper.find('[data-test="readonly-token-rotate"]');
    expect(wrapper.find('[data-test="readonly-token-source"]').text()).toBe(
      "Set by container config",
    );
    expect(rotate.attributes("disabled")).toBeDefined();
    expect(wrapper.find('[data-test="readonly-token-revoke"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="readonly-token-disabled"]').text()).toContain(
      "container config",
    );
    expect(request).not.toHaveBeenCalled();
  });

  it("shows why generation is unavailable when WebUI changes are off", () => {
    const { wrapper } = mountSection(
      tokenStatus({ editable: false, disabled_reason: "WebUI changes are turned off." }),
    );

    expect(
      wrapper.find('[data-test="readonly-token-rotate"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.find('[data-test="readonly-token-disabled"]').text()).toBe(
      "WebUI changes are turned off.",
    );
  });
});
