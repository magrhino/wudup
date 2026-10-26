import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h } from "vue";
import { createMemoryHistory } from "vue-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DiagnosticsSupportBundleResponse } from "../src/api/client";
import { createWudRouter } from "../src/router";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useManagedPreferences } from "../src/views/settings/useManagedPreferences";
import { useSettingsDiagnostics } from "../src/views/settings/useSettingsDiagnostics";
import {
  authSession,
  coreUpdateTourResponse,
  doctorResponse,
  pendingResponse,
  settingsResponse,
  wudApiConfigurationDiagnostics,
  wudApiObservationDiagnostics,
} from "./helpers/fixtures";

const clipboardCopy = vi.hoisted(() => vi.fn());

vi.mock("@vueuse/core", () => ({
  useClipboard: () => ({
    copy: clipboardCopy,
    isSupported: true,
  }),
}));

function supportBundle(): DiagnosticsSupportBundleResponse {
  return {
    wudup_version: "0.24.2",
    settings: settingsResponse(),
    doctor_result: doctorResponse(),
    wud_api_diagnostics: wudApiConfigurationDiagnostics(),
    wud_api_observations: wudApiObservationDiagnostics(),
    pending_summary: pendingResponse(),
    last_run_status: null,
    diagnostics_warnings: [],
    discovery_warnings: [],
    log_tail: null,
  };
}

function mountManagedPreferencesHarness(mutationsEnabled: boolean) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.session = authSession({ mutations_enabled: mutationsEnabled });
  const settings = useSettingsStore();
  settings.settings = settingsResponse();
  settings.coreUpdateTour = coreUpdateTourResponse({
    status: "completed",
    step: "runs_history",
  });
  const router = createWudRouter(createMemoryHistory());
  let preferences!: ReturnType<typeof useManagedPreferences>;
  const Harness = defineComponent({
    setup() {
      preferences = useManagedPreferences();
      return () => h("div");
    },
  });

  mount(Harness, {
    global: {
      plugins: [pinia, router],
    },
  });

  return { preferences, settings };
}

function mountDiagnosticsHarness() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const connection = useConnectionStore();
  vi.spyOn(connection, "diagnosticsSupportBundle").mockResolvedValue(
    supportBundle(),
  );
  let diagnostics!: ReturnType<typeof useSettingsDiagnostics>;
  const Harness = defineComponent({
    setup() {
      diagnostics = useSettingsDiagnostics();
      return () => h("div");
    },
  });

  mount(Harness, {
    global: {
      plugins: [pinia],
    },
  });

  return { connection, diagnostics };
}

describe("settings composables", () => {
  beforeEach(() => {
    clipboardCopy.mockResolvedValue(undefined);
  });

  it("guards managed preference mutations when preference controls are disabled", async () => {
    const { preferences, settings } = mountManagedPreferencesHarness(false);
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse().managed,
        audit_run_id: 77,
      });
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockResolvedValue(coreUpdateTourResponse());

    preferences.themePreferenceValue.value = "dark";
    await preferences.saveManagedPreferences();
    await preferences.relaunchOnboardingChecklist();
    await preferences.replayCoreUpdateTour();
    await preferences.dismissCoreUpdateTour();

    expect(updateManagedSettings).not.toHaveBeenCalled();
    expect(updateCoreUpdateTour).not.toHaveBeenCalled();
  });

  it("saves retag digest pins as an explicit opt-in preference", async () => {
    const { preferences, settings } = mountManagedPreferencesHarness(true);
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse().managed,
        audit_run_id: 78,
      });

    preferences.retagDigestPinsValue.value = "true";
    await preferences.saveManagedPreferences();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      retag_digest_pins: "true",
    });
  });

  it("saves the sidebar counts preference", async () => {
    const { preferences, settings } = mountManagedPreferencesHarness(true);
    const updateManagedSettings = vi
      .spyOn(settings, "updateManagedSettings")
      .mockResolvedValue({
        managed: settingsResponse().managed,
        audit_run_id: 79,
      });

    expect(preferences.sidebarBadgesValue.value).toBe("true");
    preferences.sidebarBadgesValue.value = "false";
    expect(preferences.preferencesDirty.value).toBe(true);
    await preferences.saveManagedPreferences();

    expect(updateManagedSettings).toHaveBeenCalledWith({
      sidebar_badges: "false",
    });
  });

  it("surfaces clipboard failures while copying diagnostics", async () => {
    clipboardCopy.mockRejectedValue(new Error("Clipboard permission denied"));
    const { connection, diagnostics } = mountDiagnosticsHarness();

    await diagnostics.copySupportBundle();

    expect(connection.diagnosticsSupportBundle).toHaveBeenCalled();
    expect(diagnostics.diagnosticsMessage.value).toBe("");
    expect(diagnostics.diagnosticsError.value).toBe(
      "Clipboard permission denied",
    );
  });

  it("loads fresh diagnostics for sequential settings actions", async () => {
    const { connection, diagnostics } = mountDiagnosticsHarness();

    await diagnostics.copySupportBundle();
    await diagnostics.copySupportBundle();

    expect(connection.diagnosticsSupportBundle).toHaveBeenCalledTimes(2);
  });

  it("surfaces DOM failures while downloading diagnostics", async () => {
    const createObjectURL = vi.fn(() => {
      throw new Error("Object URL unavailable");
    });
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL,
      revokeObjectURL: vi.fn(),
    });
    const { connection, diagnostics } = mountDiagnosticsHarness();

    await diagnostics.downloadSupportBundle();

    expect(connection.diagnosticsSupportBundle).toHaveBeenCalled();
    expect(createObjectURL).toHaveBeenCalled();
    expect(diagnostics.diagnosticsMessage.value).toBe("");
    expect(diagnostics.diagnosticsError.value).toBe("Object URL unavailable");
  });
});
