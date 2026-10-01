import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiPrefixFromBasePath,
  normalizeApiPrefix,
  webApi,
  type PendingRescanLine,
  type SelfUpdatePlanResponse,
  type SelfUpdateResponse,
  type StateOperation,
} from "../src/api/client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(body?: unknown): ReturnType<typeof vi.fn> {
  const responseBody = body === undefined ? {} : body;
  const fetchMock = vi
    .fn()
    .mockImplementation(() => Promise.resolve(jsonResponse(responseBody)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestInit(call: unknown[]): RequestInit {
  return call[1] as RequestInit;
}

function jsonRequestBody(call: unknown[]): unknown {
  const body = requestInit(call).body;
  if (typeof body !== "string") {
    throw new TypeError("Expected request body to be a string");
  }
  return JSON.parse(body);
}

function pendingRescanLine(
  overrides: Partial<PendingRescanLine> = {},
): PendingRescanLine {
  return {
    line_no: 1,
    raw: "repo/app:1.0",
    source_id: "file:1",
    source_hash: "pending-source-hash",
    container_id: "docker.local.app",
    ...overrides,
  };
}

function selfUpdateStatus(): SelfUpdateResponse {
  return {
    status: "available",
    strategy: "pull_image",
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    current_image: "ghcr.io/magrhino/wudup:latest",
    target_image: "ghcr.io/magrhino/wudup:latest",
    restart_container: "wudup",
    release_notes: [],
    release_notes_truncated: false,
    release_notes_cap: 10,
    can_update: true,
    disabled_reason: "",
    external_recreate_required: false,
    warnings: [],
  };
}

function selfUpdatePlanStatus(): SelfUpdatePlanResponse {
  return {
    strategy: "prepare_tag_update",
    plan: {
      plan_id: "self-plan",
      dry_run: true,
      can_apply: true,
      status: "ready",
      source_file: "/out/self-update.todo",
      mode: "stop",
      max_wait: 120,
      selected_line_numbers: [1],
      summary: {
        target_count: 1,
        matched_target_count: 1,
        stack_count: 1,
        service_count: 1,
        skipped_count: 0,
        issue_count: 0,
      },
      targets: [],
      stacks: [],
      skipped: [],
      issues: [],
      cleanup: {
        items: [],
      },
      apply_preflight: {
        ok: true,
        failures: 0,
        warnings: 0,
        checks: [],
      },
    },
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    current_image: "ghcr.io/magrhino/wudup:v0.24.2",
    target_image: "ghcr.io/magrhino/wudup:v0.25.0",
    restart_container: "wudup",
    external_recreate_required: true,
    warning:
      "This updates the Compose image tag and pulls the image. Recreate the WUDup container from outside the WebUI to run it.",
  };
}

describe("webApi", () => {
  it("builds API prefixes from the app base path", () => {
    expect(apiPrefixFromBasePath("/")).toBe("/api/v1");
    expect(apiPrefixFromBasePath("/wud/")).toBe("/wud/api/v1");
    expect(apiPrefixFromBasePath("wud")).toBe("/wud/api/v1");
  });

  it("normalizes configured API prefixes", () => {
    expect(normalizeApiPrefix("/wud/api/v1/")).toBe("/wud/api/v1");
    expect(normalizeApiPrefix("wud/api/v1")).toBe("/wud/api/v1");
    expect(normalizeApiPrefix("https://example.test/wud/api/v1/")).toBe(
      "https://example.test/wud/api/v1",
    );
  });

  it("uses a configured API prefix for fetch requests", async () => {
    vi.resetModules();
    const fetchMock = mockFetch({});
    const globals = globalThis as typeof globalThis & { WUD_API_PREFIX?: string };
    const originalApiPrefix = globals.WUD_API_PREFIX;
    globals.WUD_API_PREFIX = "/wud/api/v1/";

    try {
      const { webApi: configuredWebApi } = await import("../src/api/client");

      await configuredWebApi.status();

      expect(fetchMock.mock.calls[0][0]).toBe("/wud/api/v1/status");
    } finally {
      if (originalApiPrefix === undefined) {
        delete globals.WUD_API_PREFIX;
      } else {
        globals.WUD_API_PREFIX = originalApiPrefix;
      }
      vi.resetModules();
    }
  });

  it("uses same-origin credentials for every fetch request", async () => {
    const fetchMock = mockFetch([]);
    const operation: StateOperation = {
      kind: "delete_service_policy",
      service_key: "media/app",
    };

    await Promise.all([
      webApi.csrf(),
      webApi.setupStatus(),
      webApi.setupClaim("claim", "admin", "password", "csrf"),
      webApi.resetAdminClaim("claim", "admin", "password", "csrf"),
      webApi.session(),
      webApi.login("admin", "password", "csrf"),
      webApi.logout("csrf"),
      webApi.status(),
      webApi.settings(),
      webApi.updateManagedSettings({ theme_preference: "dark" }, "csrf"),
      webApi.onboardingChecklist("csrf"),
      webApi.dismissOnboarding("csrf"),
      webApi.coreUpdateTour(),
      webApi.updateCoreUpdateTour("in_progress", "dashboard", "csrf"),
      webApi.pending(),
      webApi.updateTargets(),
      webApi.retagTargets(),
      webApi.startRetagPreview(
        [{ service_key: "media/app", choice: "switch-to-concrete" }],
        "csrf",
      ),
      webApi.retagPreviewJob("retag-preview"),
      webApi.createRetagPlan(
        [{ service_key: "media/app", choice: "switch-to-concrete" }],
        "csrf",
      ),
      webApi.applyRetagPlan(
        "retag-plan",
        [{ service_key: "media/app", choice: "switch-to-concrete" }],
        "csrf",
      ),
      webApi.rescanPending("selected", [pendingRescanLine()], "csrf"),
      webApi.servicePolicies(),
      webApi.snoozes("active"),
      webApi.tagExclusions("active"),
      webApi.stateOperation(operation, "csrf"),
      webApi.selfUpdate(),
      webApi.planSelfUpdate("csrf"),
      webApi.applySelfUpdate("csrf", selfUpdateStatus()),
      webApi.prepareSelfUpdate("csrf", selfUpdateStatus(), selfUpdatePlanStatus()),
      webApi.restartContainer("csrf"),
      webApi.releaseNotes(),
      webApi.refreshReleaseNotes("csrf"),
      webApi.previewReleaseNotifications({ line_numbers: [1, 2] }, "csrf"),
      webApi.sendReleaseNotifications({ run_id: 7 }, "csrf"),
      webApi.createPlan([1], true, [{ line_no: 1, tag: "1.1" }], [], "csrf"),
      webApi.createJob("plan", [1], true, [{ line_no: 1, tag: "1.1" }], [], "csrf"),
      webApi.applyPlan("plan", [1], true, [{ line_no: 1, tag: "1.1" }], [], "csrf"),
      webApi.job("job"),
      webApi.applyJob("job"),
      webApi.runs(),
      webApi.runDetail(1),
      webApi.rollbackPlan(1),
      webApi.runLog(1),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(44);
    for (const call of fetchMock.mock.calls) {
      expect(requestInit(call).credentials).toBe("include");
    }
  });

  it("loads rollback plans through a read-only run endpoint", async () => {
    const fetchMock = mockFetch({ status: "blocked", items: [] });

    await webApi.rollbackPlan(42);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/runs/42/rollback-plan");
    expect(requestInit(fetchMock.mock.calls[0]).method).toBeUndefined();
    expect(requestInit(fetchMock.mock.calls[0]).body).toBeUndefined();
  });

  it("loads retag targets through a read-only GET request", async () => {
    const fetchMock = mockFetch({
      status: "ready",
      count: 0,
      items: [],
      warnings: [],
    });

    await webApi.retagTargets();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-targets");
    expect(requestInit(fetchMock.mock.calls[0]).method).toBeUndefined();
    expect(
      (requestInit(fetchMock.mock.calls[0]).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBeNull();
  });

  it("serializes retag preview and apply payloads exactly", async () => {
    const fetchMock = mockFetch({});
    const choices = [
      {
        service_key: "media/app",
        choice: "switch-to-concrete" as const,
        target_tag: "1.2.3",
      },
      { service_key: "media/radarr", choice: "keep-current" as const },
    ];

    await webApi.startRetagPreview(choices, "csrf-retag");
    await webApi.retagPreviewJob("preview job");
    await webApi.createRetagPlan(choices, "csrf-retag");
    await webApi.applyRetagPlan("retag-plan-id", choices, "csrf-retag");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-plans/preview");
    expect(requestInit(fetchMock.mock.calls[0]).method).toBe("POST");
    expect(
      (requestInit(fetchMock.mock.calls[0]).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-retag");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices,
      github_latest_fallback: false,
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/retag-plans/preview/preview%20job",
    );
    expect(requestInit(fetchMock.mock.calls[1]).method).toBeUndefined();
    expect(fetchMock.mock.calls[2][0]).toBe("/api/v1/retag-plans");
    expect(jsonRequestBody(fetchMock.mock.calls[2])).toEqual({
      choices,
      github_latest_fallback: false,
    });
    expect(fetchMock.mock.calls[3][0]).toBe("/api/v1/retag-plans/apply");
    expect(jsonRequestBody(fetchMock.mock.calls[3])).toEqual({
      plan_id: "retag-plan-id",
      choices,
      github_latest_fallback: false,
      confirmation: "apply-retags",
    });

    await webApi.retagTargets({ github_latest_fallback: true });
    await webApi.refreshRetagGithubLatest("csrf-retag");
    await webApi.startRetagPreview(choices, "csrf-retag", {
      github_latest_fallback: true,
    });
    await webApi.createRetagPlan(choices, "csrf-retag", {
      github_latest_fallback: true,
    });
    await webApi.applyRetagPlan("retag-plan-id", choices, "csrf-retag", {
      github_latest_fallback: true,
    });

    expect(fetchMock.mock.calls[4][0]).toBe(
      "/api/v1/retag-targets?github_latest_fallback=true",
    );
    expect(fetchMock.mock.calls[5][0]).toBe(
      "/api/v1/retag-targets/github-latest/refresh",
    );
    expect(jsonRequestBody(fetchMock.mock.calls[6])).toEqual({
      choices,
      github_latest_fallback: true,
    });
    expect(jsonRequestBody(fetchMock.mock.calls[7])).toEqual({
      choices,
      github_latest_fallback: true,
    });
    expect(jsonRequestBody(fetchMock.mock.calls[8])).toEqual({
      plan_id: "retag-plan-id",
      choices,
      github_latest_fallback: true,
      confirmation: "apply-retags",
    });
  });

  it("sends csrf headers on mutating requests", async () => {
    const fetchMock = mockFetch({});
    const operation: StateOperation = {
      kind: "delete_snooze",
      snooze_id: 2,
    };

    await webApi.setupClaim("claim", "admin", "password", "csrf-token");
    await webApi.resetAdminClaim("claim", "admin", "password", "csrf-token");
    await webApi.login("admin", "password", "csrf-token");
    await webApi.logout("csrf-token");
    await webApi.updateManagedSettings({ theme_preference: "dark" }, "csrf-token");
    await webApi.onboardingChecklist("csrf-token");
    await webApi.dismissOnboarding("csrf-token");
    await webApi.updateCoreUpdateTour(
      "in_progress",
      "pending_select",
      "csrf-token",
    );
    await webApi.rescanPending("selected", [pendingRescanLine()], "csrf-token");
    await webApi.stateOperation(operation, "csrf-token");
    await webApi.planSelfUpdate("csrf-token");
    await webApi.applySelfUpdate("csrf-token", selfUpdateStatus());
    await webApi.prepareSelfUpdate(
      "csrf-token",
      selfUpdateStatus(),
      selfUpdatePlanStatus(),
    );
    await webApi.restartContainer("csrf-token");
    await webApi.refreshReleaseNotes("csrf-token");
    await webApi.previewReleaseNotifications({ line_numbers: [1] }, "csrf-token");
    await webApi.sendReleaseNotifications({ run_id: 7 }, "csrf-token");
    await webApi.testReleaseNotificationWebhook("csrf-token");
    await webApi.refreshRetagGithubLatest("csrf-token");
    await webApi.startRetagPreview(
      [{ service_key: "media/app", choice: "switch-to-concrete" }],
      "csrf-token",
    );
    await webApi.createRetagPlan(
      [{ service_key: "media/app", choice: "switch-to-concrete" }],
      "csrf-token",
    );
    await webApi.applyRetagPlan(
      "retag-plan",
      [{ service_key: "media/app", choice: "switch-to-concrete" }],
      "csrf-token",
    );
    await webApi.createPlan([1], false, [], [], "csrf-token");
    await webApi.createJob("plan", [1], false, [], [], "csrf-token");
    await webApi.applyPlan("plan", [1], false, [], [], "csrf-token");

    for (const call of fetchMock.mock.calls) {
      const headers = requestInit(call).headers as Headers;
      expect(headers.get("x-wud-csrf-token")).toBe("csrf-token");
    }
  });

  it("serializes settings, plan, and job payloads exactly", async () => {
    const fetchMock = mockFetch({});
    const tagOverrides = [{ line_no: 4, tag: "2.0" }];

    await webApi.updateManagedSettings(
      { theme_preference: "dark", onboarding_checklist: "dismissed" },
      "csrf",
    );
    await webApi.updateCoreUpdateTour("in_progress", "pending_preflight", "csrf");
    await webApi.planSelfUpdate("csrf");
    await webApi.applySelfUpdate("csrf", selfUpdateStatus());
    await webApi.prepareSelfUpdate("csrf", selfUpdateStatus(), selfUpdatePlanStatus());
    await webApi.restartContainer("csrf");
    await webApi.createPlan([4], true, tagOverrides, [], "csrf");
    await webApi.createJob("plan-id", [4], true, tagOverrides, [], "csrf");
    await webApi.applyPlan("plan-id", [4], true, tagOverrides, [], "csrf");

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      values: {
        theme_preference: "dark",
        onboarding_checklist: "dismissed",
      },
    });
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual({
      status: "in_progress",
      step: "pending_preflight",
    });
    expect(requestInit(fetchMock.mock.calls[2]).body).toBeUndefined();
    expect(jsonRequestBody(fetchMock.mock.calls[3])).toEqual({
      confirmation: "pull_image",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:latest",
      restart_container: "wudup",
    });
    expect(jsonRequestBody(fetchMock.mock.calls[4])).toEqual({
      confirmation: "prepare_tag_update",
      plan_id: "self-plan",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:latest",
      restart_container: "wudup",
    });
    expect(jsonRequestBody(fetchMock.mock.calls[5])).toEqual({
      confirmation: "restart_container",
    });
    expect(jsonRequestBody(fetchMock.mock.calls[6])).toEqual({
      line_numbers: [4],
      allow_tag_updates: true,
      tag_overrides: tagOverrides,
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
    });
    expect(jsonRequestBody(fetchMock.mock.calls[7])).toEqual({
      plan_id: "plan-id",
      line_numbers: [4],
      allow_tag_updates: true,
      tag_overrides: tagOverrides,
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
      confirmation: "apply",
    });
    expect(jsonRequestBody(fetchMock.mock.calls[8])).toEqual({
      plan_id: "plan-id",
      line_numbers: [4],
      allow_tag_updates: true,
      tag_overrides: tagOverrides,
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
      confirmation: "apply",
    });
  });

  it("serializes stack-scoped plan and apply selections without broad lines", async () => {
    const fetchMock = mockFetch({});
    const selections = [
      { line_no: 1, selection_id: "selection-active" },
      { line_no: 1, selection_id: "selection-backup" },
    ];

    await webApi.createPlan([1], false, [], [], "csrf", { selections });
    await webApi.createJob(
      "plan-id",
      [1],
      false,
      [],
      [],
      "csrf",
      { selections },
    );
    await webApi.applyPlan(
      "plan-id",
      [1],
      false,
      [],
      [],
      "csrf",
      { selections },
    );

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      selections,
      allow_tag_updates: false,
      tag_overrides: [],
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
    });
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual({
      plan_id: "plan-id",
      selections,
      allow_tag_updates: false,
      tag_overrides: [],
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
      confirmation: "apply",
    });
    expect(jsonRequestBody(fetchMock.mock.calls[2])).toEqual({
      plan_id: "plan-id",
      selections,
      allow_tag_updates: false,
      tag_overrides: [],
      tag_stream_decisions: [],
      tag_stream_label_rewrite_approvals: [],
      digest_pin_label_rewrite_approvals: [],
      confirmation: "apply",
    });
  });

  it("serializes populated tag stream decisions and label approvals", async () => {
    const fetchMock = mockFetch({});
    const decisions = [{ line_no: 4, decision: "preserve" as const }];
    const approvals = [
      {
        line_no: 4,
        stack: "jarvis",
        stack_directory: "/docker/jarvis",
        compose_file: "docker-compose.yml",
        service: "task-runner",
        label_key: "wud.tag.include",
        current_label_value: "^stable-.+$",
        selected_tag: "2.34.4-distroless",
        proposed_label_value: String.raw`^\d+\.\d+\.\d+-distroless$$`,
      },
    ];
    const options = {
      tagStreamDecisions: decisions,
      tagStreamLabelRewriteApprovals: approvals,
    };

    await webApi.createPlan([4], true, [], [], "csrf", options);
    await webApi.createJob("plan-id", [4], true, [], [], "csrf", options);
    await webApi.applyPlan("plan-id", [4], true, [], [], "csrf", options);

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      line_numbers: [4],
      allow_tag_updates: true,
      tag_overrides: [],
      tag_stream_decisions: decisions,
      tag_stream_label_rewrite_approvals: approvals,
      digest_pin_label_rewrite_approvals: [],
    });
    const expectedApplyBody = {
      plan_id: "plan-id",
      line_numbers: [4],
      allow_tag_updates: true,
      tag_overrides: [],
      tag_stream_decisions: decisions,
      tag_stream_label_rewrite_approvals: approvals,
      digest_pin_label_rewrite_approvals: [],
      confirmation: "apply",
    };
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual(expectedApplyBody);
    expect(jsonRequestBody(fetchMock.mock.calls[2])).toEqual(expectedApplyBody);
  });

  it("serializes pending rescan payload exactly", async () => {
    const fetchMock = mockFetch({});

    await webApi.rescanPending(
      "selected",
      [
        pendingRescanLine({
          line_no: 3,
          raw: "repo/app:1.0 tag=1.1",
          source_id: "file:3",
        }),
      ],
      "csrf",
    );

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/pending/rescan");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "rescan_wud",
      scope: "selected",
      line_numbers: [3],
      lines: [
        {
          line_no: 3,
          raw: "repo/app:1.0 tag=1.1",
          source_id: "file:3",
          source_hash: "pending-source-hash",
          container_id: "docker.local.app",
        },
      ],
    });
    expect(
      (requestInit(fetchMock.mock.calls[0]).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf");
  });

  it("serializes pending global rescan payload exactly", async () => {
    const fetchMock = mockFetch({});

    await webApi.rescanPending("all", [], "csrf");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/pending/rescan");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "rescan_wud",
      scope: "all",
      line_numbers: [],
      lines: [],
    });
    expect(
      (requestInit(fetchMock.mock.calls[0]).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf");
  });

  it("serializes release notification preview and send payloads exactly", async () => {
    const fetchMock = mockFetch({});

    await webApi.previewReleaseNotifications({ line_numbers: [1, 2] }, "csrf");
    await webApi.sendReleaseNotifications({ run_id: 9 }, "csrf");
    await webApi.testReleaseNotificationWebhook("csrf");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/release-notifications/preview",
    );
    expect(requestInit(fetchMock.mock.calls[0]).method).toBe("POST");
    expect(
      (requestInit(fetchMock.mock.calls[0]).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      line_numbers: [1, 2],
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/release-notifications/send",
    );
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual({
      run_id: 9,
      confirmation: "send-release-notes",
    });
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/v1/release-notifications/test",
    );
    expect(jsonRequestBody(fetchMock.mock.calls[2])).toEqual({
      confirmation: "send-test-webhook",
    });
  });

  it("opens job streams with browser credentials", () => {
    const constructed: Array<{ url: string; init: EventSourceInit }> = [];
    function MockEventSource(this: EventSource, url: string, init: EventSourceInit) {
      constructed.push({ url, init });
    }
    vi.stubGlobal("EventSource", MockEventSource);

    webApi.openJobStream("job id");

    expect(constructed).toEqual([
      {
        url: "/api/v1/jobs/job%20id/stream?log_tail_bytes=65536",
        init: { withCredentials: true },
      },
    ]);
  });

  it("surfaces backend error details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "denied" }, 403));
    vi.stubGlobal("fetch", fetchMock);

    await expect(webApi.status()).rejects.toEqual(new ApiError(403, "denied"));
  });

  it("includes digest_pin_label_rewrite_approvals in createPlan request", async () => {
    const fetchMock = mockFetch({});
    const approvals = [
      {
        stack: "media",
        service: "plex",
        label_key: "wud.tag.include",
        current_label_value: "^beta|^stable",
        planned_tag: "2.0",
        proposed_label_value: String.raw`^2\.0$$`,
      },
    ];

    await webApi.createPlan([1], false, [], approvals, "csrf-token");

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual(
      expect.objectContaining({
        digest_pin_label_rewrite_approvals: approvals,
      }),
    );
  });

  it("includes digest_pin_label_rewrite_approvals in createJob request", async () => {
    const fetchMock = mockFetch({});
    const approvals = [
      {
        stack: "media",
        service: "plex",
        label_key: "wud.tag.include",
        current_label_value: "^beta|^stable",
        planned_tag: "2.0",
        proposed_label_value: String.raw`^2\.0$$`,
      },
    ];

    await webApi.createJob("plan-id", [1], false, [], approvals, "csrf-token");

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual(
      expect.objectContaining({
        digest_pin_label_rewrite_approvals: approvals,
        confirmation: "apply",
        plan_id: "plan-id",
      }),
    );
  });

  it("includes digest_pin_label_rewrite_approvals in applyPlan request", async () => {
    const fetchMock = mockFetch({});
    const approvals = [
      {
        stack: "media",
        service: "plex",
        label_key: "wud.tag.include",
        current_label_value: "^beta|^stable",
        planned_tag: "2.0",
        proposed_label_value: String.raw`^2\.0$$`,
      },
    ];

    await webApi.applyPlan("plan-id", [1], false, [], approvals, "csrf-token");

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual(
      expect.objectContaining({
        digest_pin_label_rewrite_approvals: approvals,
        confirmation: "apply",
        plan_id: "plan-id",
      }),
    );
  });

  it("sends empty digest_pin_label_rewrite_approvals when none given", async () => {
    const fetchMock = mockFetch({});

    await webApi.createPlan([1], false, [], [], "csrf-token");

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual(
      expect.objectContaining({
        digest_pin_label_rewrite_approvals: [],
      }),
    );
  });
});
