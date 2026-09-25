import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createMemoryHistory } from "vue-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, webApi } from "../src/api/client";
import { createWudRouter } from "../src/router";
import DashboardView from "../src/views/DashboardView.vue";
import DoctorView from "../src/views/DoctorView.vue";
import PendingView from "../src/views/PendingView.vue";
import PoliciesView from "../src/views/PoliciesView.vue";
import SettingsView from "../src/views/SettingsView.vue";
import SnoozesView from "../src/views/SnoozesView.vue";
import TagExclusionsView from "../src/views/TagExclusionsView.vue";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useUpdatesStore, APPLY_JOB_RECOVERY_MESSAGE } from "../src/stores/updates";
import { useRunsStore } from "../src/stores/runs";
import {
  applyPreflightResponse,
  applyJobLogResponse,
  applyJobResponse,
  authSession,
  coreUpdateTourResponse,
  doctorResponse,
  onboardingChecklistResponse,
  pendingGroupedItem,
  pendingGrouping,
  pendingItem,
  pendingResponse,
  planResponse,
  releaseNoteInfo,
  releaseNotesResponse,
  runVerification,
  runSummary,
  servicePolicy,
  settingsResponse,
  snooze,
  statusResponse,
  tagExclusion,
  updateTarget,
  updateTargetsResponse,
} from "./helpers/fixtures";
import { mountWithApp, naiveStubs } from "./helpers/mount";


import {
  buttonByText,
  emitSelectValue,
  failedApplyPreflight,
  mockApplyJobStream,
  mockMobileViewport,
  mockPendingLifecycle,
  mountPendingView,
  pendingWithUnmatched,
  setupStores,
  stalePendingPreflightFindings,
  stalePendingPossibleReasons,
  stalePendingRecommendedActions,
  unmatchedPendingItem,
} from "./helpers/viewSecurity";

describe("onboarding and tour views", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("renders first-run onboarding checklist with copyable suggestions", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({
      items: [
        {
          key: "docker-access",
          title: "Docker daemon access",
          status: "FAIL",
          detail: "Docker daemon info: info failed: <redacted>",
          check_codes: ["docker-daemon-info"],
          suggestions: [
            {
              label: "Wire Docker access",
              description: "Mount the Docker socket or configure DOCKER_HOST.",
              snippet: "DOCKER_HOST=unix:///var/run/docker.sock",
            },
          ],
          docs: [
            {
              label: "Deployment Docker access",
              url: "https://github.com/magrhino/wudup/blob/main/docs/DEPLOYMENT.md#requirements",
            },
          ],
        },
      ],
    });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    vi.spyOn(settings, "loadOnboarding").mockResolvedValue();
    vi.spyOn(settings, "dismissOnboarding").mockResolvedValue({
      dismissed: true,
      dismissed_at: "2026-05-31T00:00:00+00:00",
    });

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const text = wrapper.text();
    const restartIndex = text.indexOf("Restart WebUI container");
    const checklistIndex = text.indexOf("Setup checklist");

    expect(text).toContain("Setup checklist");
    expect(restartIndex).toBeGreaterThanOrEqual(0);
    expect(restartIndex).toBeLessThan(checklistIndex);
    expect(text).toContain("Docker daemon access");
    expect(text).toContain("info failed: <redacted>");
    expect(text).toContain("Wire Docker access");
    expect(text).toContain("Copy");
    expect(text).not.toContain("github-token-secret");
    expect(
      wrapper.find(
        'a[href="https://github.com/magrhino/wudup/blob/main/docs/DEPLOYMENT.md#requirements"]',
      ).exists(),
    ).toBe(true);
  });

  it("keeps non-actionable onboarding source checks out of the visible error chips", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({
      items: [
        {
          key: "compose-discovery",
          title: "Compose stack discovery",
          status: "FAIL",
          detail:
            "compose config /srv/docker/actual/docker-compose.yml: exit 1: additional properties 'labels' not allowed",
          check_codes: [
            "compose-config-srv-docker-actual-docker-compose-yml",
            "compose-config-srv-docker-arr-docker-compose-yml",
            "compose-config-srv-docker-backrest-docker-compose-yml",
          ],
          suggestions: [],
          docs: [],
        },
      ],
    });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    vi.spyOn(settings, "loadOnboarding").mockResolvedValue();

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();
    const row = wrapper.find(".onboarding-check-row.status-fail");
    const primaryCodes = row.find(".onboarding-check-codes.is-primary");
    const diagnostics = row.find(".onboarding-check-diagnostics");

    expect(primaryCodes.text()).toContain(
      "compose-config-srv-docker-actual-docker-compose-yml",
    );
    expect(primaryCodes.text()).not.toContain(
      "compose-config-srv-docker-arr-docker-compose-yml",
    );
    expect(diagnostics.find("summary").text()).toContain("Source check codes (3)");
    expect(diagnostics.text()).toContain(
      "compose-config-srv-docker-arr-docker-compose-yml",
    );
  });

  it("starts the core update tour once setup has no failing checks", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({
      items: [
        {
          key: "admin-setup",
          title: "Admin setup",
          status: "PASS",
          detail: "The first admin account exists.",
          check_codes: ["settings-authentication"],
          suggestions: [],
          docs: [],
        },
        {
          key: "mutation-mode",
          title: "Browser mutation mode",
          status: "WARN",
          detail: "Browser apply controls are server-side enabled.",
          check_codes: ["settings-mutation-gate"],
          suggestions: [],
          docs: [],
        },
      ],
    });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    vi.spyOn(settings, "loadOnboarding").mockResolvedValue();
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockResolvedValue(
        coreUpdateTourResponse({
          status: "in_progress",
          step: "dashboard",
        }),
      );

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("Setup is ready for the update tour");
    const startButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Start update tour"));
    await startButton?.trigger("click");
    await flushPromises();

    expect(updateCoreUpdateTour).toHaveBeenCalledWith("in_progress", "dashboard");
  });

  it("focuses the setup checklist once onboarding deep link data renders", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    settings.settings = null;
    settings.onboarding = null;
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const focus = vi
      .spyOn(HTMLElement.prototype, "focus")
      .mockImplementation(() => undefined);
    vi.spyOn(settings, "loadSettings").mockImplementation(async () => {
      settings.settings = settingsResponse();
    });
    const loadOnboarding = vi.spyOn(settings, "loadOnboarding").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push("/settings?onboarding=1");
    await router.isReady();

    const wrapper = mountWithApp(SettingsView, { pinia, router });
    document.body.appendChild(wrapper.element);
    await flushPromises();
    settings.onboarding = onboardingChecklistResponse();
    await nextTick();
    await flushPromises();

    expect(loadOnboarding).toHaveBeenCalled();
    expect(wrapper.find("#onboarding-checklist").exists()).toBe(true);
    expect(scrollIntoView).toHaveBeenCalled();
    expect(focus).toHaveBeenCalled();
  });

  it("replays and dismisses the core update tour from settings", async () => {
    const { pinia, settings } = setupStores(true);
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: false });
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "completed",
      step: "runs_history",
      updated_at: "2026-05-31T00:00:00+00:00",
    });
    vi.spyOn(settings, "loadSettings").mockResolvedValue();
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockResolvedValue(
        coreUpdateTourResponse({
          status: "dismissed",
          step: "runs_history",
        }),
      );

    const wrapper = mountWithApp(SettingsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("Core update tour");
    expect(wrapper.text()).toContain("State: Completed. Step: History.");
    const replayButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Replay tour"));
    await replayButton?.trigger("click");
    await flushPromises();

    expect(updateCoreUpdateTour).toHaveBeenCalledWith("in_progress", "dashboard");
    const dismissButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Dismiss tour"));
    await dismissButton?.trigger("click");
    await flushPromises();

    expect(updateCoreUpdateTour).toHaveBeenCalledWith("dismissed", "runs_history");
  });

  it("shows the dashboard step of the core update tour", async () => {
    const { pinia, connection, settings, updates, runs } = setupStores(true);
    connection.status = statusResponse({
      pending_count: 2,
      db_ready: true,
      mutations_enabled: true,
    });
    updates.pending = pendingResponse();
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "in_progress",
      step: "dashboard",
    });
    vi.spyOn(connection, "loadStatus").mockResolvedValue(undefined as any);
vi.spyOn(updates, "loadPending").mockResolvedValue(undefined as any);
vi.spyOn(runs, "loadRuns").mockResolvedValue(undefined as any);
vi.spyOn(settings, "loadServicePolicies").mockResolvedValue(undefined as any);
vi.spyOn(settings, "loadSnoozes").mockResolvedValue(undefined as any);
vi.spyOn(settings, "loadTagExclusions").mockResolvedValue(undefined as any);
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockResolvedValue(
        coreUpdateTourResponse({
          status: "in_progress",
          step: "pending_select",
        }),
      );

    const wrapper = mountWithApp(DashboardView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("Start from current state");
    expect(wrapper.text()).toContain("Pending: 1");
    const nextButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Open pending updates"));
    await nextButton?.trigger("click");
    await flushPromises();

    expect(updateCoreUpdateTour).toHaveBeenCalledWith(
      "in_progress",
      "pending_select",
    );
  });

  it("closes the preflight modal before showing apply tour guidance", async () => {
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = pendingResponse();
    updates.releaseNotes = releaseNotesResponse([]);
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "in_progress",
      step: "pending_preflight",
    });
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({ can_apply: true });
    });
    const updateCoreUpdateTour = vi
      .spyOn(settings, "updateCoreUpdateTour")
      .mockImplementation(async (status, step) => {
        const response = coreUpdateTourResponse({ status, step });
        settings.coreUpdateTour = response;
        return response;
      });

    const wrapper = mountPendingView(pinia);
    await wrapper
      .find('input[aria-label="Select stack media"]')
      .setValue(true);
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review selected ("))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Continue to apply guidance"))
      ?.trigger("click");
    await flushPromises();

    expect(updateCoreUpdateTour).toHaveBeenCalledWith(
      "in_progress",
      "pending_apply",
    );
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("Apply only after the plan is clear");
  });

  it("shows read-only pending tour guidance and the empty queue fallback", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    updates.pending = pendingResponse([]);
    updates.releaseNotes = releaseNotesResponse([]);
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "in_progress",
      step: "pending_apply",
    });
    mockPendingLifecycle(settings, updates);
    vi.spyOn(runs, "loadRuns").mockResolvedValue();

    const wrapper = mountPendingView(pinia);
    await flushPromises();
    const text = wrapper.text();

    expect(text).toContain("Apply only after the plan is clear");
    expect(text).toContain("Read-only mode keeps Apply disabled");
    expect(text).toContain("Update queue is clear");
    expect(text).toContain("New WUD entries will appear here");
    expect(text).toContain("Open setup checklist");
  });
});
