import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { webApi } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import { useRunsStore } from "../src/stores/runs";
import {
  coreUpdateTourResponse,
  onboardingChecklistResponse,
  onboardingDismissResponse,
  settingsResponse,
  servicePolicy,
  stateOperationResponse,
  snooze,
  tagExclusion,
} from "./helpers/fixtures";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(body: unknown = {}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(body)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonRequestBody(call: unknown[]): unknown {
  const body = (call[1] as RequestInit).body;
  if (typeof body !== "string") {
    throw new TypeError("Expected request body to be a string");
  }
  return JSON.parse(body);
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("settings store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("passes csrf from auth store to onboarding checks and dismissal", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(onboardingChecklistResponse()))
      .mockResolvedValueOnce(jsonResponse(onboardingDismissResponse()));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-onboarding");
    const settings = useSettingsStore();

    await settings.loadOnboarding();
    await settings.dismissOnboarding();

    expect(ensureCsrf).toHaveBeenCalledTimes(2);
    expect(settings.onboarding?.visible).toBe(false);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/onboarding/checklist");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/onboarding/dismiss");
    for (const call of fetchMock.mock.calls) {
      expect((call[1] as RequestInit).method).toBe("POST");
      expect(
        ((call[1] as RequestInit).headers as Headers).get("x-wud-csrf-token"),
      ).toBe("csrf-onboarding");
    }
  });

  it("loads and updates the server-managed core update tour", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(coreUpdateTourResponse()))
      .mockResolvedValueOnce(
        jsonResponse(
          coreUpdateTourResponse({
            status: "in_progress",
            step: "pending_select",
            updated_at: "2026-05-31T00:00:00+00:00",
          }),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-tour");
    const settings = useSettingsStore();

    await settings.loadCoreUpdateTour();
    await settings.updateCoreUpdateTour("in_progress", "pending_select");

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(settings.coreUpdateTour?.status).toBe("in_progress");
    expect(settings.coreUpdateTour?.step).toBe("pending_select");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/onboarding/core-update-tour",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/onboarding/core-update-tour",
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBeUndefined();
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("POST");
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual({
      status: "in_progress",
      step: "pending_select",
    });
    expect(
      ((fetchMock.mock.calls[1][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-tour");
  });

  it("coalesces concurrent core update tour ensure requests", async () => {
    const tourRequest = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(tourRequest.promise);
    vi.stubGlobal("fetch", fetchMock);
    const settings = useSettingsStore();

    const firstEnsure = settings.ensureCoreUpdateTour();
    const secondEnsure = settings.ensureCoreUpdateTour();
    const thirdEnsure = settings.ensureCoreUpdateTour();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    tourRequest.resolve(jsonResponse(coreUpdateTourResponse()));
    await Promise.all([firstEnsure, secondEnsure, thirdEnsure]);
    expect(settings.coreUpdateTour?.status).toBe("not_started");

    await settings.ensureCoreUpdateTour();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps fulfilled pending safety cue data when another cue source fails", async () => {
    const existingPolicy = servicePolicy({ service_key: "media/app" });
    const loadedSnooze = snooze({ service_key: "media/radarr" });
    vi.spyOn(webApi, "servicePolicies").mockRejectedValue(
      new Error("service policies unavailable"),
    );
    vi.spyOn(webApi, "snoozes").mockResolvedValue([loadedSnooze]);
    const settings = useSettingsStore();
    settings.servicePolicies = [existingPolicy];
    settings.error = "old failure";

    await settings.loadPendingSafetyCues();

    expect(settings.servicePolicies).toEqual([existingPolicy]);
    expect(settings.snoozes).toEqual([loadedSnooze]);
    expect(settings.error).toBe("old failure");
    expect(settings.pendingSafetyCueError).toBe(
      "webApi.servicePolicies() failed: service policies unavailable",
    );
  });

  it("clears only pending safety cue errors after successful loads", async () => {
    const loadedPolicy = servicePolicy({ service_key: "media/app" });
    const loadedSnooze = snooze({ service_key: "media/radarr" });
    vi.spyOn(webApi, "servicePolicies").mockResolvedValue([loadedPolicy]);
    vi.spyOn(webApi, "snoozes").mockResolvedValue([loadedSnooze]);
    const settings = useSettingsStore();
    settings.error = "old failure";
    settings.pendingSafetyCueError = "old safety cue failure";

    await settings.loadPendingSafetyCues();

    expect(settings.servicePolicies).toEqual([loadedPolicy]);
    expect(settings.snoozes).toEqual([loadedSnooze]);
    expect(settings.error).toBe("old failure");
    expect(settings.pendingSafetyCueError).toBe("");
  });

  it("passes csrf from auth store to state operations", async () => {
    const fetchMock = mockFetch(stateOperationResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-state");
    const connection = useConnectionStore();

    await connection.stateOperation({
      kind: "delete_service_policy",
      service_key: "media/app",
    });

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      kind: "delete_service_policy",
      service_key: "media/app",
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-state");
  });

  it("keeps settings loading through policy mutation and reload", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-state");
    const stateOperation = deferred<ReturnType<typeof stateOperationResponse>>();
    const reloadedPolicies = [
      servicePolicy({ service_key: "media/updated", update_mode: "live" }),
    ];
    const servicePolicies = deferred<typeof reloadedPolicies>();
    const stateOperationSpy = vi
      .spyOn(webApi, "stateOperation")
      .mockReturnValue(stateOperation.promise);
    const loadServicePolicies = vi
      .spyOn(webApi, "servicePolicies")
      .mockReturnValue(servicePolicies.promise);
    const connection = useConnectionStore();
    const settings = useSettingsStore();

    const mutation = settings.upsertServicePolicy(
      "media/app",
      "live",
      true,
      null,
      "09:30",
      ["mon"],
    );
    await flushPromises();

    expect(settings.loading).toBe(true);
    expect(connection.loading).toBe(false);
    expect(settings.error).toBe("");
    expect(loadServicePolicies).not.toHaveBeenCalled();
    expect(stateOperationSpy).toHaveBeenCalledWith(
      {
        kind: "upsert_service_policy",
        service_key: "media/app",
        update_mode: "live",
        auto_update: true,
        snooze_default_seconds: null,
        auto_update_time: "09:30",
        auto_update_days: ["mon"],
      },
      "csrf-state",
    );

    stateOperation.resolve(stateOperationResponse());
    await flushPromises();

    expect(settings.loading).toBe(true);
    expect(connection.loading).toBe(false);
    expect(loadServicePolicies).toHaveBeenCalledTimes(1);

    servicePolicies.resolve(reloadedPolicies);
    await mutation;

    expect(settings.loading).toBe(false);
    expect(settings.error).toBe("");
    expect(settings.servicePolicies).toEqual(reloadedPolicies);
  });

  it("surfaces settings mutation failures without refreshing state", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-state");
    vi.spyOn(webApi, "stateOperation").mockRejectedValue(
      new Error("state write failed"),
    );
    const loadServicePolicies = vi
      .spyOn(webApi, "servicePolicies")
      .mockResolvedValue([servicePolicy()]);
    const settings = useSettingsStore();

    await expect(settings.deleteServicePolicy("media/app")).rejects.toThrow(
      "state write failed",
    );

    expect(settings.loading).toBe(false);
    expect(settings.error).toBe("state write failed");
    expect(loadServicePolicies).not.toHaveBeenCalled();
  });

  it("clears stale errors and loading state on successful loads", async () => {
    mockFetch([]);
    const runs = useRunsStore();
    runs.error = "old failure";
    runs.loading = true;

    await runs.loadRuns();

    expect(runs.error).toBe("");
    expect(runs.loading).toBe(false);
    expect(runs.runs).toEqual([]);
  });

  it("loads read-only settings", async () => {
    const fetchMock = mockFetch(settingsResponse());
    const settings = useSettingsStore();

    await settings.loadSettings();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/settings");
    expect(settings.settings?.updater[0]?.name).toBe("DOCKER_BASE");
    expect(settings.settings?.secrets[1]?.configured).toBe(true);
  });

  it("passes csrf from auth store to managed settings updates", async () => {
    const updatedSettings = settingsResponse({
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
        {
          key: "onboarding_checklist",
          value: "visible",
          default_value: "visible",
          source: "default",
          editable: true,
          allowed_values: ["visible", "dismissed"],
          restart_required: false,
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ managed: updatedSettings.managed, audit_run_id: 44 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-settings");
    const settings = useSettingsStore();
    settings.settings = settingsResponse();

    const response = await settings.updateManagedSettings({
      theme_preference: "dark",
    });

    expect(response.audit_run_id).toBe(44);
    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(settings.settings?.managed[0]?.value).toBe("dark");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/settings/managed");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-settings");
  });

  it("passes csrf from auth store to test webhook requests", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      jsonResponse({
        sent: true,
        destination: {
          type: "discord",
          configured: true,
          source: "WebUI settings",
        },
        audit_run_id: 45,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-test-webhook");
    const settings = useSettingsStore();

    const response = await settings.testReleaseNotificationWebhook();

    expect(response.audit_run_id).toBe(45);
    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/release-notifications/test",
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-test-webhook");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "send-test-webhook",
    });
  });

  it("keeps managed settings saves successful when onboarding refresh fails", async () => {
    const updatedSettings = settingsResponse({
      managed: [
        settingsResponse().managed[0],
        {
          key: "onboarding_checklist",
          value: "dismissed",
          default_value: "visible",
          source: "configured",
          editable: true,
          allowed_values: ["visible", "dismissed"],
          restart_required: false,
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ managed: updatedSettings.managed, audit_run_id: 45 }),
      )
      .mockResolvedValueOnce(jsonResponse({ detail: "doctor unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-settings");
    const settings = useSettingsStore();
    const runs = useRunsStore();
    settings.settings = settingsResponse();
    settings.onboarding = onboardingChecklistResponse({ visible: true });

    const response = await settings.updateManagedSettings({
      onboarding_checklist: "dismissed",
    });

    expect(response.audit_run_id).toBe(45);
    expect(settings.settings?.managed[1]?.value).toBe("dismissed");
    expect(settings.onboarding?.visible).toBe(true);
    expect(runs.error).toBe("");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/v1/settings/managed",
      "/api/v1/onboarding/checklist",
    ]);
  });

  it("surfaces backend errors and always clears loading state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "db missing" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const runs = useRunsStore();

    await expect(runs.loadRuns()).rejects.toMatchObject({ message: "db missing" });

    expect(runs.error).toBe("db missing");
    expect(runs.loading).toBe(false);
  });

  it("loads service policies, snoozes, and tag exclusions with their filters", async () => {
    vi.spyOn(webApi, "servicePolicies").mockResolvedValue([
      servicePolicy({ service_key: "media/app" }),
    ]);
    vi.spyOn(webApi, "snoozes").mockResolvedValue([
      snooze({ service_key: "media/radarr", active: false }),
    ]);
    vi.spyOn(webApi, "tagExclusions").mockResolvedValue([
      tagExclusion({ tag: "2.0", status: "disabled" }),
    ]);
    const settings = useSettingsStore();

    await settings.loadServicePolicies();
    await settings.loadSnoozes("expired");
    await settings.loadTagExclusions("disabled");

    expect(settings.servicePolicies[0]?.service_key).toBe("media/app");
    expect(settings.snoozes[0]?.service_key).toBe("media/radarr");
    expect(settings.snoozeStateFilter).toBe("expired");
    expect(settings.tagExclusions[0]?.tag).toBe("2.0");
    expect(settings.tagExclusionStatusFilter).toBe("disabled");
    expect(settings.loading).toBe(false);
    expect(settings.error).toBe("");
  });

  it("creates snoozes through state operations and reloads the selected filter", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-snooze");
    const stateOperation = vi
      .spyOn(webApi, "stateOperation")
      .mockResolvedValue(stateOperationResponse());
    vi.spyOn(webApi, "snoozes").mockResolvedValue([
      snooze({ service_key: "media/radarr" }),
    ]);
    const settings = useSettingsStore();

    await settings.createSnooze(
      "media/radarr",
      "2026-06-01T00:00:00+00:00",
      "maintenance",
      "active",
    );

    expect(stateOperation).toHaveBeenCalledWith(
      {
        kind: "create_snooze",
        service_key: "media/radarr",
        snoozed_until: "2026-06-01T00:00:00+00:00",
        reason: "maintenance",
      },
      "csrf-snooze",
    );
    expect(webApi.snoozes).toHaveBeenCalledWith("active");
    expect(settings.snoozes[0]?.service_key).toBe("media/radarr");
    expect(settings.error).toBe("");
  });

  it("creates and deletes dependency snoozes through state operations", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-dependency-snooze");
    const stateOperation = vi
      .spyOn(webApi, "stateOperation")
      .mockResolvedValue(stateOperationResponse());
    vi.spyOn(webApi, "snoozes").mockResolvedValue([
      snooze({
        service_key: "media/radarr",
        wait_for_service_key: "media/prowlarr",
        snoozed_until: null,
        kind: "dependency",
      }),
    ]);
    const settings = useSettingsStore();

    await settings.createDependencySnooze(
      "media/radarr",
      "media/prowlarr",
      "wait for indexer",
      "active",
    );
    await settings.deleteSnooze(42, "active", "dependency");

    expect(stateOperation).toHaveBeenNthCalledWith(
      1,
      {
        kind: "create_dependency_snooze",
        service_key: "media/radarr",
        wait_for_service_key: "media/prowlarr",
        reason: "wait for indexer",
      },
      "csrf-dependency-snooze",
    );
    expect(stateOperation).toHaveBeenNthCalledWith(
      2,
      {
        kind: "delete_dependency_snooze",
        snooze_id: 42,
      },
      "csrf-dependency-snooze",
    );
    expect(webApi.snoozes).toHaveBeenCalledWith("active");
    expect(settings.snoozes[0]).toMatchObject({
      service_key: "media/radarr",
      wait_for_service_key: "media/prowlarr",
      kind: "dependency",
    });
  });

  it("creates and deletes release skips through state operations", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-release-skip");
    const stateOperation = vi
      .spyOn(webApi, "stateOperation")
      .mockResolvedValue(stateOperationResponse());
    vi.spyOn(webApi, "snoozes").mockResolvedValue([
      snooze({ kind: "release", release_label: "5.3.0", snoozed_until: null }),
    ]);
    const settings = useSettingsStore();
    // The Snoozes page may have left a different filter behind.
    settings.snoozeStateFilter = "expired";

    await settings.createReleaseSkip(
      {
        serviceKeys: ["media/qbittorrent"],
        image: "lscr.io/linuxserver/qbittorrent:5.2.0",
        targetTag: "5.3.0",
        digest: "",
      },
      "line",
      "stalls",
      "active",
    );
    await settings.deleteSnooze(9, "active", "release");

    expect(stateOperation).toHaveBeenNthCalledWith(
      1,
      {
        kind: "create_release_skip",
        service_keys: ["media/qbittorrent"],
        image: "lscr.io/linuxserver/qbittorrent:5.2.0",
        target_tag: "5.3.0",
        digest: "",
        scope: "line",
        reason: "stalls",
      },
      "csrf-release-skip",
    );
    expect(stateOperation).toHaveBeenNthCalledWith(
      2,
      { kind: "delete_release_skip", snooze_id: 9 },
      "csrf-release-skip",
    );
    expect(settings.snoozes[0]?.kind).toBe("release");
    expect(webApi.snoozes).toHaveBeenNthCalledWith(1, "active");
    expect(webApi.snoozes).not.toHaveBeenCalledWith("expired");

    stateOperation.mockRejectedValueOnce(new Error("media/qbittorrent already skips 5.3.0"));
    await expect(
      settings.createReleaseSkip(
        {
          serviceKeys: ["media/qbittorrent"],
          image: "lscr.io/linuxserver/qbittorrent:5.2.0",
          targetTag: "5.3.0",
          digest: "",
        },
        "release",
        "",
        "active",
      ),
    ).rejects.toThrow("already skips 5.3.0");
    expect(settings.error).toContain("already skips 5.3.0");
  });

  it("updates tag exclusions through state operations and reloads the selected filter", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-tag");
    const stateOperation = vi
      .spyOn(webApi, "stateOperation")
      .mockResolvedValue(stateOperationResponse());
    vi.spyOn(webApi, "tagExclusions").mockResolvedValue([
      tagExclusion({ scope: "service", service_key: "media/app", tag: "3.0" }),
    ]);
    const settings = useSettingsStore();

    await settings.upsertTagExclusion(
      "service",
      "repo/app",
      "media/app",
      "3.0",
      "disabled",
      "all",
    );

    expect(stateOperation).toHaveBeenCalledWith(
      {
        kind: "upsert_tag_exclusion",
        scope: "service",
        image_repo: "repo/app",
        service_key: "media/app",
        match_type: "exact",
        tag: "3.0",
        status: "disabled",
      },
      "csrf-tag",
    );
    expect(webApi.tagExclusions).toHaveBeenCalledWith("all");
    expect(settings.tagExclusionStatusFilter).toBe("all");
    expect(settings.tagExclusions[0]?.tag).toBe("3.0");
  });

  it("keeps loading active through tag-exclusion reloads", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-state");
    const stateOperation = deferred<ReturnType<typeof stateOperationResponse>>();
    const reloadedExclusions = deferred<ReturnType<typeof tagExclusion>[]>();
    vi.spyOn(webApi, "stateOperation").mockReturnValue(stateOperation.promise);
    vi.spyOn(webApi, "tagExclusions").mockReturnValue(reloadedExclusions.promise);
    const settings = useSettingsStore();

    const mutation = settings.setTagExclusionStatus(1, "disabled", "disabled");
    await flushPromises();

    expect(settings.loading).toBe(true);

    stateOperation.resolve(stateOperationResponse());
    await flushPromises();

    expect(settings.loading).toBe(true);

    reloadedExclusions.resolve([tagExclusion({ status: "disabled" })]);
    await mutation;

    expect(settings.loading).toBe(false);
    expect(settings.error).toBe("");
    expect(settings.tagExclusions[0]?.status).toBe("disabled");
  });

  it("does not reload snoozes after state operation failures", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-snooze");
    vi.spyOn(webApi, "stateOperation").mockRejectedValue(
      new Error("snooze write failed"),
    );
    const loadSnoozes = vi.spyOn(webApi, "snoozes").mockResolvedValue([]);
    const settings = useSettingsStore();

    await expect(settings.deleteSnooze(99, "active")).rejects.toThrow(
      "snooze write failed",
    );

    expect(settings.error).toBe("snooze write failed");
    expect(settings.loading).toBe(false);
    expect(loadSnoozes).not.toHaveBeenCalled();
  });
});
