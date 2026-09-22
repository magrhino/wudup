import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { webApi } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useRetagsStore } from "../src/stores/retags";
import { useUpdatesStore } from "../src/stores/updates";
import { deferred, jsonRequestBody, jsonResponse, mockFetch } from "./helpers/storeActions";
import {
  applyJobLogResponse,
  applyJobResponse,
  retagPlanResponse,
  retagPreviewJobResponse,
  retagTarget,
  retagTargetsResponse,
} from "./helpers/fixtures";

describe("retags store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("clears the displayed apply job for retag preview without losing recovery", async () => {
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    useUpdatesStore().setApplyJob(applyJobResponse({ job_id: "previous-job" }));
    useUpdatesStore().setApplyJobLog(applyJobLogResponse());
    vi.spyOn(webApi, "startRetagPreview").mockImplementation(async () => {
      expect(useUpdatesStore().applyJob).toBeNull();
      expect(useUpdatesStore().applyJobLog).toBeNull();
      expect(useUpdatesStore().rememberedApplyJobId).toBe("previous-job");
      return retagPreviewJobResponse();
    });

    await retags.createRetagPlan();

    expect(retags.retagPlan?.plan_id).toBe("retag-plan-test");
    expect(globalThis.sessionStorage.getItem("applyJobId")).toBe("previous-job");
  });

  it("loads retag targets for read-only review", async () => {
    const response = retagTargetsResponse();
    const fetchMock = mockFetch(response);
    const retags = useRetagsStore();

    await retags.loadRetagTargets();

    expect(retags.retagTargets?.items[0]?.service_key).toBe("media/app");
    expect(retags.retagTargetTags[response.items[0].target_id]).toBe("1.1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-targets");
  });

  it("bulk selects only retag targets with valid concrete tags", async () => {
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget(),
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:5.21.1",
        image_repo: "repo/radarr",
        current_tag: "5.21.1",
        tracking_tag: "5.21.1",
        tracking_tag_source: "image",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
      retagTarget({
        service_key: "media/bad",
        service: "bad",
        image: "repo/bad:latest",
        image_repo: "repo/bad",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.retagTargetTags = {
      [retagItems[1].target_id]: "5.22.4",
      [retagItems[2].target_id]: "-bad",
    };
    retags.retagChoices = { [retagItems[2].target_id]: "switch-to-concrete" };
    retags.retagPlan = retagPlanResponse();

    retags.setRetagChoicesForItems(
      retagItems,
      "switch-to-concrete",
    );

    expect(retags.retagPlan).toBeNull();
    expect(retags.retagChoices).toMatchObject({
      [retagItems[0].target_id]: "switch-to-concrete",
      [retagItems[1].target_id]: "switch-to-concrete",
      [retagItems[2].target_id]: "keep-current",
    });
    expect(retags.retagChoiceRequests()).toEqual([
      {
        service_key: "media/app",
        target_id: retagItems[0].target_id,
        choice: "switch-to-concrete",
        target_tag: "1.1",
      },
      {
        service_key: "media/bad",
        target_id: retagItems[2].target_id,
        choice: "keep-current",
      },
      {
        service_key: "media/radarr",
        target_id: retagItems[1].target_id,
        choice: "switch-to-concrete",
        target_tag: "5.22.4",
      },
    ]);
  });

  it("approves starts only for selected inactive retag targets", () => {
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget({ service_key: "media/running", service: "running" }),
      retagTarget({
        service_key: "media/stopped",
        service: "stopped",
        runtime_state: "not-running",
      }),
      retagTarget({
        service_key: "media/unknown",
        service: "unknown",
        runtime_state: "unknown",
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.resetRetagChoices();
    for (const item of retagItems) {
      retags.setRetagChoice(item.target_id, "switch-to-concrete");
    }

    expect(retags.retagChoiceRequests()).toEqual([
      {
        service_key: "media/running",
        target_id: retagItems[0].target_id,
        choice: "switch-to-concrete",
        target_tag: "1.1",
      },
      {
        service_key: "media/stopped",
        target_id: retagItems[1].target_id,
        choice: "switch-to-concrete",
        allow_start: true,
        target_tag: "1.1",
      },
      {
        service_key: "media/unknown",
        target_id: retagItems[2].target_id,
        choice: "switch-to-concrete",
        allow_start: true,
        target_tag: "1.1",
      },
    ]);
  });

  it("loads cached retag GitHub latest fallback candidates", async () => {
    const fetchMock = mockFetch(retagTargetsResponse([
      retagTarget({
        candidate_source: "github-latest",
        candidate_warning: "GitHub latest fallback will update latest tracking to v1.1.",
        candidate_link_label: "GitHub release",
        candidate_link_url: "https://github.com/acme/app/releases/tag/v1.1",
      }),
    ]));
    const retags = useRetagsStore();

    await retags.setRetagGithubLatestFallback(true);

    expect(retags.retagGithubLatestFallback).toBe(true);
    expect(retags.retagTargets?.items[0]?.candidate_source).toBe("github-latest");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets?github_latest_fallback=true",
    );
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "true",
    );
  });

  it("refreshes retag GitHub latest fallback candidates explicitly", async () => {
    const fetchMock = mockFetch(retagTargetsResponse([
      retagTarget({
        candidate_source: "github-latest",
        candidate_warning: "GitHub latest fallback will update latest tracking to v1.1.",
        candidate_link_label: "GitHub release",
        candidate_link_url: "https://github.com/acme/app/releases/tag/v1.1",
      }),
    ]));
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();

    await retags.refreshRetagGithubLatest();

    expect(retags.retagGithubLatestFallback).toBe(true);
    expect(retags.retagTargets?.items[0]?.candidate_source).toBe("github-latest");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets/github-latest/refresh",
    );
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "true",
    );
  });

  it("remembers the cached retag fallback preference", async () => {
    globalThis.localStorage.setItem("retagGithubLatestFallback", "true");
    const fetchMock = mockFetch(retagTargetsResponse());
    const retags = useRetagsStore();

    expect(retags.retagGithubLatestFallback).toBe(true);
    await retags.loadRetagTargets();
    await retags.setRetagGithubLatestFallback(false);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets?github_latest_fallback=true",
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/retag-targets");
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "false",
    );
  });

  it("previews retag choices through the retag store", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          retagPreviewJobResponse({
            status: "queued",
            plan: null,
            warnings: [],
            progress: [
              {
                job_id: "retag-preview-test",
                phase: "refresh",
                status: "running",
                message: "Refreshing retag candidates.",
                created_at: "2026-01-02T00:00:00Z",
                stack: "",
                services: [],
                line_numbers: [],
              },
            ],
          }),
        ),
      )
      .mockResolvedValueOnce(jsonResponse(retagPreviewJobResponse()));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget(),
      retagTarget({ service_key: "media/radarr", service: "radarr" }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");

    try {
      const planPromise = retags.createRetagPlan();
      await flushPromises();
      await vi.advanceTimersByTimeAsync(400);
      const plan = await planPromise;

      expect(ensureCsrf).toHaveBeenCalledTimes(1);
      expect(plan?.plan_id).toBe("retag-plan-test");
      expect(retags.retagPlan?.selected_count).toBe(1);
      expect(retags.retagPreviewJob?.status).toBe("success");
      expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-plans/preview");
      expect(fetchMock.mock.calls[1][0]).toBe(
        "/api/v1/retag-plans/preview/retag-preview-test",
      );
      expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
        choices: [
          {
            service_key: "media/app",
            target_id: retagItems[0].target_id,
            choice: "switch-to-concrete",
            target_tag: "1.1",
          },
          {
            service_key: "media/radarr",
            target_id: retagItems[1].target_id,
            choice: "keep-current",
          },
        ],
        github_latest_fallback: false,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels an overlapping preview when GitHub refresh finishes, then previews the new selection", async () => {
    vi.useFakeTimers();
    vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-retag");
    const refresh = deferred<ReturnType<typeof retagTargetsResponse>>();
    vi.spyOn(webApi, "refreshRetagGithubLatest").mockReturnValue(refresh.promise);
    const start = vi
      .spyOn(webApi, "startRetagPreview")
      .mockResolvedValueOnce(
        retagPreviewJobResponse({ status: "running", plan: null }),
      )
      .mockResolvedValue(retagPreviewJobResponse());
    const poll = vi
      .spyOn(webApi, "retagPreviewJob")
      .mockResolvedValue(retagPreviewJobResponse({ status: "running", plan: null }));
    const retags = useRetagsStore();
    try {
      const refreshRun = retags.refreshRetagGithubLatest();
      await flushPromises();
      const preview = retags.createRetagPlan();
      await flushPromises();
      await vi.advanceTimersByTimeAsync(400);
      refresh.resolve(retagTargetsResponse());
      await refreshRun;
      expect(retags.loading).toBe(true);
      await vi.advanceTimersByTimeAsync(400);
      await expect(preview).resolves.toBeNull();
      expect(poll).toHaveBeenCalledTimes(1);
      expect(retags.retagPreviewJob).toBeNull();
      expect(retags.retagPlan).toBeNull();
      expect(retags.error).toBe("");
      expect(retags.loading).toBe(false);
      const target = retags.retagTargets!.items[0];
      retags.setRetagTargetTag(target.target_id, "2.0");
      await expect(retags.createRetagPlan()).resolves.toEqual(retagPlanResponse());
      expect(start.mock.calls[1][0][0].target_tag).toBe("2.0");
      expect(retags.retagPlan?.can_apply).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not submit a cancelled preview after CSRF resolves and allows a fresh retry", async () => {
    const csrf = deferred<string>();
    vi.spyOn(useAuthStore(), "ensureCsrf").mockReturnValue(csrf.promise);
    const start = vi.spyOn(webApi, "startRetagPreview").mockResolvedValue(
      retagPreviewJobResponse(),
    );
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    const preview = retags.createRetagPlan();
    retags.setRetagTargetTag(retags.retagTargets.items[0].target_id, "2.0");
    csrf.resolve("csrf-retag");
    await expect(preview).resolves.toBeNull();
    expect(start).not.toHaveBeenCalled();
    expect(retags.retagPlan).toBeNull();
    expect(retags.retagPreviewJob).toBeNull();
    expect(retags.error).toBe("");
    await expect(retags.createRetagPlan()).resolves.toEqual(retagPlanResponse());
    expect(start).toHaveBeenCalledTimes(1);
    expect(start.mock.calls[0][0][0].target_tag).toBe("2.0");
  });

  it("does not submit an older preview when its CSRF resolves after a newer preview", async () => {
    const csrf = deferred<string>();
    vi.spyOn(useAuthStore(), "ensureCsrf")
      .mockReturnValueOnce(csrf.promise)
      .mockResolvedValue("csrf-new");
    const start = vi.spyOn(webApi, "startRetagPreview").mockResolvedValue(
      retagPreviewJobResponse(),
    );
    const retags = useRetagsStore();
    const oldPreview = retags.createRetagPlan();
    await expect(retags.createRetagPlan()).resolves.toEqual(retagPlanResponse());
    csrf.resolve("csrf-old");
    await expect(oldPreview).resolves.toBeNull();
    expect(start).toHaveBeenCalledTimes(1);
    expect(start.mock.calls[0][1]).toBe("csrf-new");
    expect(retags.retagPlan).toEqual(retagPlanResponse());
    expect(retags.error).toBe("");
    expect(retags.loading).toBe(false);
  });

  it.each(["start", "poll"] as const)(
    "discards a late %s plan after the selection changes",
    async (phase) => {
      vi.useFakeTimers();
      vi.spyOn(useAuthStore(), "ensureCsrf").mockResolvedValue("csrf-retag");
      const stale = deferred<ReturnType<typeof retagPreviewJobResponse>>();
      vi.spyOn(webApi, "startRetagPreview").mockImplementation(() =>
        phase === "start"
          ? stale.promise
          : Promise.resolve(retagPreviewJobResponse({ status: "running", plan: null })),
      );
      vi.spyOn(webApi, "retagPreviewJob").mockReturnValue(stale.promise);
      const retags = useRetagsStore();
      retags.retagTargets = retagTargetsResponse();
      try {
        const preview = retags.createRetagPlan();
        await flushPromises();
        if (phase === "poll") {
          await vi.advanceTimersByTimeAsync(400);
        }
        retags.setRetagTargetTag(retags.retagTargets.items[0].target_id, "2.0");
        stale.resolve(retagPreviewJobResponse());
        await expect(preview).resolves.toBeNull();
        expect(retags.retagPlan).toBeNull();
        expect(retags.retagPreviewJob).toBeNull();
        expect(retags.error).toBe("");
        await expect(retags.applyRetagPlan()).rejects.toThrow(
          "Retag preview must be loaded before applying",
        );
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("keeps duplicate retag service keys separate by target id", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse([
      retagTarget({ target_id: "target-a", service_key: "media/app" }),
      retagTarget({ target_id: "target-b", service_key: "media/app" }),
    ]);
    retags.resetRetagChoices();

    retags.setRetagChoice("target-b", "switch-to-concrete");
    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: "target-a",
          choice: "keep-current",
        },
        {
          service_key: "media/app",
          target_id: "target-b",
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("uses service-key fallback only when the retag target is unique", () => {
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget(),
      retagTarget({ service_key: "media/radarr", service: "radarr" }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.resetRetagChoices();

    retags.setRetagChoice("media/app", "switch-to-concrete");
    retags.setRetagTargetTag("media/app", "1.2");

    expect(retags.retagChoices[retagItems[0].target_id]).toBe(
      "switch-to-concrete",
    );
    expect(retags.retagTargetTags[retagItems[0].target_id]).toBe("1.2");
  });

  it("does not use service-key fallback for duplicate retag targets", () => {
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse([
      retagTarget({ target_id: "target-a", service_key: "media/app" }),
      retagTarget({ target_id: "target-b", service_key: "media/app" }),
    ]);
    retags.resetRetagChoices();

    retags.setRetagChoice("media/app", "switch-to-concrete");
    retags.setRetagTargetTag("media/app", "2.0");

    expect(retags.retagChoiceRequests()).toEqual([
      {
        service_key: "media/app",
        target_id: "target-a",
        choice: "keep-current",
      },
      {
        service_key: "media/app",
        target_id: "target-b",
        choice: "keep-current",
      },
    ]);
    expect(retags.retagTargetTags["target-a"]).toBe("1.1");
    expect(retags.retagTargetTags["target-b"]).toBe("1.1");
  });

  it("sends retag fallback state when previewing", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    retags.retagGithubLatestFallback = true;
    const retagItems = [retagTarget()];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");

    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: true,
    });
  });

  it("falls back to keep-current for stale ineligible retag choices", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget({
        proposed_tag: "",
        retag_available: false,
        retag_reason: "missing-provenance",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);

    retags.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");
    expect(retags.retagChoices[retagItems[0].target_id]).toBe("keep-current");

    retags.retagChoices = { [retagItems[0].target_id]: "switch-to-concrete" };
    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "keep-current",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("sends manual retag target tags for fallback rows", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:5.21.1",
        image_repo: "repo/radarr",
        current_tag: "5.21.1",
        tracking_tag: "5.21.1",
        tracking_tag_source: "image",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);

    retags.setRetagTargetTag(retagItems[0].target_id, "5.22.4");
    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/radarr",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "5.22.4",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("uses edited automatch target tags as manual overrides", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    const retagItems = [retagTarget()];
    retags.retagTargets = retagTargetsResponse(retagItems);

    retags.setRetagTargetTag(retagItems[0].target_id, "1.2");
    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.2",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("clears blank automatch overrides back to the proposed tag", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    const retagItems = [retagTarget()];
    retags.retagTargets = retagTargetsResponse(retagItems);

    retags.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");
    retags.setRetagTargetTag(retagItems[0].target_id, "1.2");
    retags.setRetagTargetTag(retagItems[0].target_id, "   ");
    await retags.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("applies a retag plan as a tracked apply job", async () => {
    const fetchMock = mockFetch(applyJobResponse({ job_id: "retag-job" }));
    const auth = useAuthStore();
    const csrf = deferred<string>();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockReturnValue(csrf.promise);
    const retags = useRetagsStore();
    const retagItems = [retagTarget()];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.retagChoices = { [retagItems[0].target_id]: "switch-to-concrete" };
    retags.retagPlan = retagPlanResponse();

    const applying = retags.applyRetagPlan();
    expect(fetchMock).not.toHaveBeenCalled();
    csrf.resolve("csrf-retag");
    const job = await applying;

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(job.job_id).toBe("retag-job");
    expect(useUpdatesStore().applyJob?.job_id).toBe("retag-job");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-plans/apply");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      plan_id: "retag-plan-test",
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
      confirmation: "apply-retags",
    });
  });

  it.each(["selection", "refresh", "replacement", "fallback"] as const)(
    "does not apply after %s invalidates the confirmation during CSRF setup",
    async (change) => {
      const csrf = deferred<string>();
      vi.spyOn(useAuthStore(), "ensureCsrf").mockReturnValue(csrf.promise);
      const apply = vi.spyOn(webApi, "applyRetagPlan").mockResolvedValue(
        applyJobResponse({ job_id: "unexpected-retag-job" }),
      );
      vi.spyOn(webApi, "retagTargets").mockResolvedValue(retagTargetsResponse());
      const retags = useRetagsStore();
      retags.retagTargets = retagTargetsResponse();
      retags.setRetagChoice(
        retags.retagTargets.items[0].target_id,
        "switch-to-concrete",
      );
      retags.retagPlan = retagPlanResponse();
      const applying = retags.applyRetagPlan();
      const expectedError =
        "Retag preview changed. Preview the current selection again.";
      const rejected = expect(applying).rejects.toThrow(expectedError);

      if (change === "selection") {
        retags.setRetagTargetTag(retags.retagTargets.items[0].target_id, "2.0");
      } else if (change === "refresh") {
        await retags.loadRetagTargets();
      } else if (change === "replacement") {
        retags.retagPlan = retagPlanResponse({ plan_id: "replacement-plan" });
      } else {
        retags.retagGithubLatestFallback = true;
      }
      csrf.resolve("csrf-retag");
      await rejected;

      expect(apply).not.toHaveBeenCalled();
      expect(useUpdatesStore().applyJob).toBeNull();
      expect(retags.error).toBe(expectedError);
      expect(retags.loading).toBe(false);
    },
  );

  it("surfaces retag target loading errors", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "retag targets unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const retags = useRetagsStore();

    await expect(retags.loadRetagTargets()).rejects.toThrow(
      "retag targets unavailable",
    );

    expect(retags.error).toBe("retag targets unavailable");
  });

});
