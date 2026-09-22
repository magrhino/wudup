import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import RetagsView from "../src/views/RetagsView.vue";
import { webApi } from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useUpdatesStore } from "../src/stores/updates";
import { useRetagsStore } from "../src/stores/retags";
import {
  applyJobResponse,
  authSession,
  retagPlanResponse,
  retagPreviewJobResponse,
  retagTarget,
  retagTargetsResponse,
} from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

describe("RetagsView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setActivePinia(createPinia());
  });

  it("renders retag choices and previews selected changes", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget({
        candidate_source: "github-latest",
        candidate_warning:
          "GitHub latest fallback will update latest tracking to 1.1.",
        candidate_link_label: "GitHub release",
        candidate_link_url: "https://github.com/acme/app/releases/tag/1.1",
      }),
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:latest",
        image_repo: "repo/radarr",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "missing-provenance",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems, {
      warnings: ["compose warning"],
    });
    const loadRetagTargets = vi
      .spyOn(retags, "loadRetagTargets")
      .mockResolvedValue();
    const createRetagPlan = vi.spyOn(retags, "createRetagPlan").mockImplementation(
      async () => {
        const plan = retagPlanResponse();
        retags.retagPlan = plan;
        return plan;
      },
    );
    const setRetagGithubLatestFallback = vi
      .spyOn(retags, "setRetagGithubLatestFallback")
      .mockResolvedValue();
    const refreshRetagGithubLatest = vi
      .spyOn(retags, "refreshRetagGithubLatest")
      .mockResolvedValue();

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const text = wrapper.text();
    expect(loadRetagTargets).toHaveBeenCalledTimes(1);
    expect(text).toContain("Retag review");
    expect(text).toContain("Total services");
    expect(text).toContain("Retag candidates");
    expect(wrapper.find(".retag-summary-strip").text()).toContain("1 running now");
    expect(text).toContain("Needs attention");
    expect(text).toContain("compose warning");
    expect(text).toContain("media/app");
    expect(text).toContain("Retag available");
    expect(text).toContain("Automatch ready");
    expect(text).toContain("GitHub release");
    expect(text).toContain("Use cached GitHub latest fallback");
    expect(text).toContain("GitHub latest fallback will update latest tracking to 1.1.");
    expect(text).toContain("media/radarr");
    expect(text).toContain("Missing provenance");

    await wrapper
      .find('input[aria-label="Use cached GitHub latest fallback"]')
      .setValue(true);
    expect(setRetagGithubLatestFallback).toHaveBeenCalledWith(true);

    await wrapper
      .get('button[aria-label="Refresh GitHub latest candidates"]')
      .trigger("click");
    expect(refreshRetagGithubLatest).toHaveBeenCalledTimes(1);

    const switchControls = wrapper.findAll('input[value="switch-to-concrete"]');
    expect(switchControls).toHaveLength(2);
    expect(switchControls[0].attributes("disabled")).toBeUndefined();
    expect(switchControls[1].attributes("disabled")).toBeDefined();
    await switchControls[0].setValue();
    expect(retags.retagChoices[retagItems[0].target_id]).toBe(
      "switch-to-concrete",
    );

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Preview retag changes"))
      ?.trigger("click");
    await flushPromises();

    expect(createRetagPlan).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("Review retag preview");
    expect(wrapper.text()).toContain("repo/app:latest -> repo/app@sha256:abc123");
    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    expect(applyButton).toBeDefined();
    expect(applyButton?.attributes("disabled")).toBeUndefined();
  });

  it("keeps demo previews interactive while apply and refresh stay read-only", async () => {
    vi.stubEnv("VITE_WUD_DEMO_MODE", "true");
    vi.resetModules();
    try {
      const [
        { default: DemoRetagsView },
        { useAuthStore: useDemoAuthStore },
        { useRetagsStore: useDemoRetagsStore },
      ] = await Promise.all([
        import("../src/views/RetagsView.vue"),
        import("../src/stores/auth"),
        import("../src/stores/retags"),
      ]);
      const pinia = createPinia();
      setActivePinia(pinia);
      const auth = useDemoAuthStore();
      auth.session = authSession({ mutations_enabled: false });
      const retags = useDemoRetagsStore();
      retags.retagTargets = retagTargetsResponse();
      vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
      const createRetagPlan = vi
        .spyOn(retags, "createRetagPlan")
        .mockImplementation(async () => {
          const plan = retagPlanResponse({ can_apply: false });
          retags.retagPlan = plan;
          return plan;
        });
      const setRetagGithubLatestFallback = vi
        .spyOn(retags, "setRetagGithubLatestFallback")
        .mockResolvedValue();
      const refreshRetagGithubLatest = vi
        .spyOn(retags, "refreshRetagGithubLatest")
        .mockResolvedValue();

      const wrapper = mountWithApp(DemoRetagsView, { pinia });
      await flushPromises();

      const githubLatestFallbackSwitch = wrapper.get(
        'input[aria-label="Use cached GitHub latest fallback"]',
      );
      expect(githubLatestFallbackSwitch.attributes("disabled")).toBeUndefined();
      await githubLatestFallbackSwitch.setValue(true);
      expect(setRetagGithubLatestFallback).toHaveBeenCalledWith(true);

      await wrapper
        .get('button[aria-label="Refresh GitHub latest candidates"]')
        .trigger("click");
      expect(refreshRetagGithubLatest).not.toHaveBeenCalled();

      const retagControl = wrapper.get('input[value="switch-to-concrete"]');
      expect(retagControl.attributes("disabled")).toBeUndefined();
      await retagControl.setValue();
      await wrapper
        .findAll("button")
        .find((button) => button.text().includes("Preview retag changes"))
        ?.trigger("click");
      await flushPromises();

      expect(createRetagPlan).toHaveBeenCalledTimes(1);
      expect(wrapper.text()).toContain(
        "Static demo mode is read-only. Preview stays available; apply is disabled.",
      );
      const applyButtons = wrapper
        .findAll("button")
        .filter((button) => button.text().includes("Apply selected retags"));
      expect(applyButtons.length).toBeGreaterThan(0);
      expect(
        applyButtons.every((button) => button.attributes("disabled") !== undefined),
      ).toBe(true);
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });

  it("selects one service from the per-row retag action without previewing", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget(),
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:latest",
        image_repo: "repo/radarr",
        proposed_tag: "5.22.4",
        final_image: "repo/radarr@sha256:def456",
        digest_provenance: {
          source_image: "repo/radarr:latest",
          resolved_tag: "5.22.4",
          watch_tag: "latest",
          target_digest: "sha256:def456",
          final_image: "repo/radarr@sha256:def456",
          provenance_source: "test",
          provenance_confidence: "high",
        },
      }),
    ];
    const appTarget = retagItems[0];
    const radarrTarget = retagItems[1];
    retags.retagTargets = retagTargetsResponse(retagItems);
    retags.setRetagChoice(appTarget.target_id, "switch-to-concrete");
    retags.setRetagChoice(radarrTarget.target_id, "switch-to-concrete");
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const setRetagChoice = vi.spyOn(retags, "setRetagChoice");
    const createRetagPlan = vi.spyOn(retags, "createRetagPlan").mockResolvedValue(
      retagPlanResponse(),
    );

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    await wrapper
      .get('button[aria-label="Retag media/radarr"]')
      .trigger("click");
    await flushPromises();

    expect(setRetagChoice).toHaveBeenCalledWith(
      radarrTarget.target_id,
      "switch-to-concrete",
    );
    expect(retags.retagChoices[appTarget.target_id]).toBe("switch-to-concrete");
    expect(retags.retagChoices[radarrTarget.target_id]).toBe("switch-to-concrete");
    expect(createRetagPlan).not.toHaveBeenCalled();
    expect(wrapper.text()).not.toContain("Review retag preview");
  });

  it("requires a selected service before previewing", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse([
      retagTarget({ runtime_state: "not-running" }),
    ]);
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const createRetagPlan = vi.spyOn(retags, "createRetagPlan");

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const previewButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Preview retag changes"));
    expect(previewButton?.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("Select at least one service to preview.");
    expect(wrapper.find(".retag-summary-strip").text()).toContain("0 running now");
    expect(wrapper.get(".retag-selected-count").attributes("aria-live")).toBe(
      "polite",
    );
    expect(wrapper.text()).toContain(
      "No running retag candidates are available. Select stopped candidates individually.",
    );
    await previewButton?.trigger("click");
    expect(createRetagPlan).not.toHaveBeenCalled();
  });

  it("bulk adds running eligible rows without replacing hidden choices", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget(),
      retagTarget({
        service_key: "data/postgres",
        stack: "data",
        service: "postgres",
        image: "postgres:16",
        image_repo: "postgres",
        current_tag: "16",
        tracking_tag: "16",
        proposed_tag: "16.1",
        final_image: "postgres@sha256:feed",
      }),
      retagTarget({
        service_key: "archive/legacy",
        stack: "archive",
        service: "legacy",
        runtime_state: "not-running",
      }),
      retagTarget({
        service_key: "archive/unknown",
        stack: "archive",
        service: "unknown",
        runtime_state: "unknown",
      }),
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
    retags.resetRetagChoices();
    retags.setRetagChoice(retagItems[2].target_id, "switch-to-concrete");
    retags.setRetagChoice(retagItems[3].target_id, "switch-to-concrete");
    retags.retagPlan = retagPlanResponse();
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const createRetagPlan = vi.spyOn(retags, "createRetagPlan").mockResolvedValue(
      retagPlanResponse(),
    );
    const applyRetagPlan = vi.spyOn(retags, "applyRetagPlan").mockResolvedValue(
      applyJobResponse({ job_id: "bulk-retag-job" }),
    );

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    const buttonByText = (text: string) =>
      wrapper.findAll("button").find((button) => button.text().includes(text));

    expect(wrapper.text()).toContain(
      "Targets are discovered Compose services; standalone docker run containers are not included.",
    );
    expect(buttonByText("Add running candidates")?.attributes("title")).toContain(
      "Not-running and unknown choices stay unchanged",
    );

    await wrapper
      .find('input[aria-label="Search retag targets"]')
      .setValue("postgres");
    await buttonByText("Add running in results")?.trigger("click");
    await flushPromises();

    expect(retags.retagPlan).toBeNull();
    expect(retags.retagChoices).toMatchObject({
      [retagItems[0].target_id]: "keep-current",
      [retagItems[1].target_id]: "switch-to-concrete",
      [retagItems[2].target_id]: "switch-to-concrete",
      [retagItems[3].target_id]: "switch-to-concrete",
      [retagItems[4].target_id]: "keep-current",
    });
    expect(wrapper.find(".retag-summary-strip").text()).toContain(
      "Selected retags3",
    );
    expect(createRetagPlan).not.toHaveBeenCalled();
    expect(applyRetagPlan).not.toHaveBeenCalled();

    retags.retagPlan = retagPlanResponse();
    await wrapper.find('input[aria-label="Search retag targets"]').setValue("");
    await buttonByText("Add running candidates")?.trigger("click");
    await flushPromises();

    expect(retags.retagPlan).toBeNull();
    expect(retags.retagChoices).toMatchObject({
      [retagItems[0].target_id]: "switch-to-concrete",
      [retagItems[1].target_id]: "switch-to-concrete",
      [retagItems[2].target_id]: "switch-to-concrete",
      [retagItems[3].target_id]: "switch-to-concrete",
      [retagItems[4].target_id]: "keep-current",
    });
    expect(wrapper.find(".retag-summary-strip").text()).toContain(
      "Selected retags4",
    );

    await buttonByText("Clear selection")?.trigger("click");
    await flushPromises();

    expect(retags.retagChoices).toMatchObject({
      [retagItems[0].target_id]: "keep-current",
      [retagItems[1].target_id]: "keep-current",
      [retagItems[2].target_id]: "keep-current",
      [retagItems[3].target_id]: "keep-current",
      [retagItems[4].target_id]: "keep-current",
    });
    expect(wrapper.find(".retag-summary-strip").text()).toContain(
      "Selected retags0",
    );
    expect(createRetagPlan).not.toHaveBeenCalled();
    expect(applyRetagPlan).not.toHaveBeenCalled();
  });

  it("carries explicit non-running warnings through preview and confirmation", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    const retagItems = [
      retagTarget({
        service_key: "archive/legacy",
        stack: "archive",
        service: "legacy",
        runtime_state: "not-running",
      }),
      retagTarget({
        service_key: "archive/unknown",
        stack: "archive",
        service: "unknown",
        runtime_state: "unknown",
      }),
    ];
    retags.retagTargets = retagTargetsResponse(retagItems);
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    vi.spyOn(retags, "createRetagPlan").mockImplementation(async () => {
      const plan = retagPlanResponse({ selected_count: 2, keep_current_count: 0 });
      retags.retagPlan = plan;
      return plan;
    });

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const switchControls = wrapper.findAll('input[value="switch-to-concrete"]');
    expect(switchControls).toHaveLength(2);
    await switchControls[0].setValue();
    await switchControls[1].setValue();
    await flushPromises();

    const summary = wrapper.get(".retag-summary-panel");
    expect(summary.text()).toContain(
      "archive/legacy) is not running. Apply will create or recreate and start it.",
    );
    expect(summary.text()).toContain(
      "archive/unknown) has unknown runtime state. Apply may create or recreate and start it.",
    );

    await summary
      .findAll("button")
      .find((button) => button.text().includes("Preview retag changes"))
      ?.trigger("click");
    await flushPromises();

    const preview = wrapper.get(".preflight-modal");
    expect(preview.text()).toContain(
      "archive/legacy) is not running. Apply will create or recreate and start it.",
    );
    expect(preview.text()).toContain(
      "archive/unknown) has unknown runtime state. Apply may create or recreate and start it.",
    );
    await preview
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"))
      ?.trigger("click");
    await flushPromises();

    const confirmation = wrapper.get(".retag-confirm-modal");
    expect(confirmation.text()).toContain(
      "Applying rewrites Compose image metadata, pulls images, and recreates selected services.",
    );
    expect(confirmation.text()).toContain(
      "archive/legacy) is not running. Apply will create or recreate and start it.",
    );
    expect(confirmation.text()).toContain(
      "archive/unknown) has unknown runtime state. Apply may create or recreate and start it.",
    );
  });

  it("shows recovery guidance and blocks apply when a refresh cancels the preview", async () => {
    vi.useFakeTimers();
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    retags.setRetagChoice(
      retags.retagTargets.items[0].target_id,
      "switch-to-concrete",
    );
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    vi.spyOn(webApi, "startRetagPreview").mockResolvedValue(
      retagPreviewJobResponse({ status: "running", plan: null }),
    );
    vi.spyOn(webApi, "refreshRetagGithubLatest").mockResolvedValue(
      retagTargetsResponse(),
    );
    const apply = vi.spyOn(webApi, "applyRetagPlan");
    const wrapper = mountWithApp(RetagsView, { pinia });
    try {
      await flushPromises();
      await wrapper
        .findAll("button")
        .find((button) => button.text().includes("Preview retag changes"))!
        .trigger("click");
      await flushPromises();
      expect(wrapper.get(".preflight-modal").text()).toContain(
        "Building a preview",
      );
      await retags.refreshRetagGithubLatest();
      await vi.advanceTimersByTimeAsync(400);
      await flushPromises();
      const preview = wrapper.get(".preflight-modal");
      expect(preview.text()).toContain(
        "Retag targets or selections changed. Close this preview and preview your current selection again.",
      );
      expect(preview.text()).not.toContain("Retag preview did not return a plan");
      expect(preview.text()).not.toContain("Building a preview");
      const applyButton = preview
        .findAll("button")
        .find((button) => button.text().includes("Apply selected retags"))!;
      expect(applyButton.attributes("disabled")).toBeDefined();
      expect(apply).not.toHaveBeenCalled();
    } finally {
      wrapper.unmount();
      vi.useRealTimers();
    }
  });

  it("shows preview start failures in the review modal", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    retags.setRetagChoice(
      retags.retagTargets.items[0].target_id,
      "switch-to-concrete",
    );
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    vi.spyOn(retags, "createRetagPlan").mockImplementation(async () => {
      retags.error = "retag preview is already running";
      throw new Error("retag preview is already running");
    });

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Preview retag changes"))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("Review retag preview");
    expect(wrapper.text()).toContain("retag preview is already running");
    expect(wrapper.text()).not.toContain(
      "Building a preview from the selected candidates.",
    );
    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    expect(applyButton?.attributes("disabled")).toBeDefined();
  });

  it.each([
    [
      "duplicate service choices",
      "422: retag choices contain duplicate service(s): media/app",
    ],
    [
      "missing target identity",
      "422: retag choices for duplicate service(s) must include target_id: media/app",
    ],
    [
      "duplicate target choices",
      "422: retag choices contain duplicate target(s): media/app (media/app)",
    ],
  ])(
    "shows duplicate retag service recovery guidance with affected rows for %s",
    async (_label, previewError) => {
      const pinia = createPinia();
      setActivePinia(pinia);
      const auth = useAuthStore();
      auth.session = authSession({ mutations_enabled: true });
      const retags = useRetagsStore();
      retags.retagTargets = retagTargetsResponse([
        retagTarget(),
        retagTarget({
          image: "repo/app:latest-staging",
          directory: "/docker/media-staging",
          project_directory: "/docker/media-staging",
        }),
      ]);
      for (const item of retags.retagTargets.items) {
        retags.setRetagChoice(item.target_id, "switch-to-concrete");
      }
      vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
      vi.spyOn(retags, "createRetagPlan").mockImplementation(async () => {
        retags.error = previewError;
        throw new Error(retags.error);
      });

      const wrapper = mountWithApp(RetagsView, { pinia });
      await flushPromises();

      await wrapper
        .findAll("button")
        .find((button) => button.text().includes("Preview retag changes"))
        ?.trigger("click");
      await flushPromises();

      const text = wrapper.text();
      expect(text).toContain("Duplicate service key: media/app.");
      expect(text).toContain(
        "Retag preview stopped because 2 discovered targets share this Compose project/service identity.",
      );
      expect(text).toContain(
        "Keep only one target for this key, or update Compose so each project/service pair is unique",
      );
      expect(text).toContain("/docker/media/docker-compose.yml");
      expect(text).toContain("/docker/media-staging/docker-compose.yml");
      expect(text).toContain("repo/app:latest-staging");
    },
  );

  it("lets fallback rows retag with a manually entered target tag", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
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
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const createRetagPlan = vi.spyOn(retags, "createRetagPlan").mockResolvedValue(
      retagPlanResponse(),
    );

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const targetInput = wrapper.find<HTMLInputElement>(
      'input[aria-label="Target tag for media/radarr"]',
    );
    expect(targetInput.element.value).toBe("");
    await targetInput.setValue("5.22.4");
    await flushPromises();

    expect(retags.retagChoices[retagItems[0].target_id]).toBe(
      "switch-to-concrete",
    );
    expect(retags.retagChoiceRequests()).toEqual([
      {
        service_key: "media/radarr",
        target_id: retagItems[0].target_id,
        choice: "switch-to-concrete",
        target_tag: "5.22.4",
      },
    ]);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Preview retag changes"))
      ?.trigger("click");
    await flushPromises();
    expect(createRetagPlan).toHaveBeenCalledTimes(1);

    await targetInput.setValue("-bad");
    await flushPromises();
    expect(wrapper.text()).toContain("media/radarr has an invalid target tag");
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text().includes("Preview retag changes"))
        ?.attributes("disabled"),
    ).toBeDefined();
  });

  it("disables switch and apply controls in read-only mode", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: false });
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    retags.retagPlan = retagPlanResponse();
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const setRetagGithubLatestFallback = vi.spyOn(
      retags,
      "setRetagGithubLatestFallback",
    );
    const setRetagChoicesForItems = vi.spyOn(retags, "setRetagChoicesForItems");
    const applyRetagPlan = vi.spyOn(retags, "applyRetagPlan").mockResolvedValue(
      applyJobResponse({ job_id: "blocked-retag-job" }),
    );

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    expect(wrapper.text()).toContain("Read-only mode keeps retag switch/apply disabled.");
    expect(
      wrapper.find('input[value="switch-to-concrete"]').attributes("disabled"),
    ).toBeDefined();
    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    const retagAllButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Add running candidates"));
    const retagFilteredButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Add running in results"));
    const keepAllButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Clear selection"));
    const refreshButton = wrapper.get(
      'button[aria-label="Refresh GitHub latest candidates"]',
    );
    const githubLatestFallbackSwitch = wrapper.get(
      'input[aria-label="Use cached GitHub latest fallback"]',
    );
    expect(retagAllButton?.attributes("disabled")).toBeDefined();
    expect(retagFilteredButton?.attributes("disabled")).toBeDefined();
    expect(keepAllButton?.attributes("disabled")).toBeDefined();
    expect(refreshButton.attributes("disabled")).toBeDefined();
    expect(githubLatestFallbackSwitch.attributes("disabled")).toBeDefined();
    await githubLatestFallbackSwitch.setValue(true);
    await retagAllButton?.trigger("click");
    await retagFilteredButton?.trigger("click");
    await keepAllButton?.trigger("click");
    await flushPromises();
    expect(setRetagGithubLatestFallback).not.toHaveBeenCalled();
    expect(setRetagChoicesForItems).not.toHaveBeenCalled();
    expect(applyButton).toBeDefined();
    expect(applyButton?.attributes("disabled")).toBeDefined();
    await applyButton?.trigger("click");
    await flushPromises();
    expect(applyRetagPlan).not.toHaveBeenCalled();
  });

  it("confirms and tracks retag apply jobs after submit", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    retags.retagPlan = retagPlanResponse();
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    const eventSource: EventSource = {
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
      onmessage: null,
      onopen: null,
      readyState: 1,
      url: "",
      withCredentials: true,
      CONNECTING: 0,
      OPEN: 1,
      CLOSED: 2,
      dispatchEvent: vi.fn(),
      removeEventListener: vi.fn(),
    };
    const openJobStream = vi
      .spyOn(webApi, "openJobStream")
      .mockReturnValue(eventSource);
    const applyRetagPlan = vi.spyOn(retags, "applyRetagPlan").mockImplementation(
      async () => {
        const job = applyJobResponse({
          job_id: "retag-job",
          selected_line_numbers: [],
          status: "queued",
        });
        useUpdatesStore().setApplyJob(job);
        return job;
      },
    );

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    expect(applyButton).toBeDefined();
    expect(applyButton?.attributes("disabled")).toBeUndefined();

    await applyButton?.trigger("click");
    await flushPromises();

    expect(applyRetagPlan).not.toHaveBeenCalled();
    expect(wrapper.find("dialog").exists()).toBe(true);
    expect(wrapper.text()).toContain("Confirm retag apply");
    expect(wrapper.text()).toContain(
      "Applying rewrites Compose image metadata, pulls images, and recreates selected services.",
    );
    expect(wrapper.text()).toContain("1 service in media");
    expect(wrapper.text()).toContain("media/app");
    expect(wrapper.text()).toContain("repo/app:latest -> repo/app@sha256:abc123");
    expect(wrapper.text()).toContain(String.raw`wud.tag.include: ^latest$$ -> ^1\.1$$`);

    const confirmButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Confirm and apply"));
    expect(confirmButton).toBeDefined();
    await confirmButton?.trigger("click");
    await flushPromises();

    expect(applyRetagPlan).toHaveBeenCalledTimes(1);
    expect(openJobStream).toHaveBeenCalledWith("retag-job");
    expect(wrapper.text()).toContain("Applying 1 retag");
    expect(wrapper.text()).toContain("Waiting for the updater job to start.");
    expect(wrapper.text()).toContain("Revalidate");
    expect(wrapper.text()).toContain("repo/app:latest -> repo/app@sha256:abc123");
    expect(wrapper.text()).toContain("wud.tag.include");
    const disabledApplyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    expect(disabledApplyButton?.attributes("disabled")).toBeDefined();
  });

  it("shows stale apply errors in the confirmation and rebuilds the preview", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse();
    retags.retagPlan = retagPlanResponse();
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    vi.spyOn(retags, "applyRetagPlan").mockImplementation(async () => {
      retags.error = "409: retag plan is stale";
      throw new Error(retags.error);
    });
    const createRetagPlan = vi
      .spyOn(retags, "createRetagPlan")
      .mockImplementation(async () => {
        const plan = retagPlanResponse({ plan_id: "rebuilt-retag-plan" });
        retags.retagPlan = plan;
        return plan;
      });

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"))
      ?.trigger("click");
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Confirm and apply"))
      ?.trigger("click");
    await flushPromises();

    const confirmation = wrapper.get(".retag-confirm-modal");
    const applyError = confirmation.get('[role="alert"]');
    expect(applyError.text()).toContain(
      "Service state changed since this preview. Rebuild the preview before applying.",
    );
    expect(applyError.attributes("tabindex")).toBe("-1");

    await confirmation
      .findAll("button")
      .find((button) => button.text().includes("Rebuild preview"))
      ?.trigger("click");
    await flushPromises();

    expect(createRetagPlan).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".retag-confirm-modal").exists()).toBe(false);
    expect(wrapper.text()).toContain("Review retag preview");
  });

  it("keeps duplicate service apply snapshot rows keyed by target id", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const pinia = createPinia();
    setActivePinia(pinia);
    const auth = useAuthStore();
    auth.session = authSession({ mutations_enabled: true });
    const retags = useRetagsStore();
    const basePlan = retagPlanResponse();
    const baseStack = basePlan.stacks[0];
    const baseUpdate = baseStack.digest_pin_updates[0];
    retags.retagTargets = retagTargetsResponse([
      retagTarget({ target_id: "target-a", service_key: "media/app" }),
      retagTarget({
        target_id: "target-b",
        service_key: "media/app",
        image: "repo/app:latest-staging",
        directory: "/docker/media-staging",
        project_directory: "/docker/media-staging",
      }),
    ]);
    retags.retagPlan = retagPlanResponse({
      selected_count: 2,
      stacks: [
        {
          ...baseStack,
          digest_pin_updates: [
            { ...baseUpdate, target_id: "target-a" },
            {
              ...baseUpdate,
              target_id: "target-b",
              source_image: "repo/app:latest-staging",
              planned_digest: "sha256:def456",
              final_image: "repo/app@sha256:def456",
              digest_provenance: {
                ...baseUpdate.digest_provenance,
                target_digest: "sha256:def456",
                final_image: "repo/app@sha256:def456",
              },
            },
          ],
        },
      ],
    });
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();
    vi.spyOn(webApi, "openJobStream").mockReturnValue({
      addEventListener: vi.fn(),
      close: vi.fn(),
      onerror: null,
      onmessage: null,
      onopen: null,
      readyState: 1,
      url: "",
      withCredentials: true,
      CONNECTING: 0,
      OPEN: 1,
      CLOSED: 2,
      dispatchEvent: vi.fn(),
      removeEventListener: vi.fn(),
    });
    vi.spyOn(retags, "applyRetagPlan").mockImplementation(async () => {
      const job = applyJobResponse({
        job_id: "retag-duplicate-job",
        selected_line_numbers: [],
        status: "queued",
      });
      useUpdatesStore().setApplyJob(job);
      return job;
    });

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply selected retags"));
    expect(applyButton).toBeDefined();
    await applyButton?.trigger("click");
    await flushPromises();

    const confirmButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Confirm and apply"));
    expect(confirmButton).toBeDefined();
    await confirmButton?.trigger("click");
    await flushPromises();

    expect(wrapper.findAll(".apply-job-impact .list-row")).toHaveLength(2);
    expect(
      warn.mock.calls
        .map((call) => call.map((value) => String(value)).join(" "))
        .join("\n"),
    ).not.toContain("Duplicate keys");
  });

  it("filters retag targets by search text and review status", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse([
      retagTarget(),
      retagTarget({
        service_key: "data/postgres",
        stack: "data",
        service: "postgres",
        image: "postgres:16",
        image_repo: "postgres",
        current_tag: "16",
        tracking_tag: "16",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ]);
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();

    await wrapper
      .find('input[aria-label="Search retag targets"]')
      .setValue("postgres");
    await flushPromises();

    expect(wrapper.text()).toContain("data/postgres");
    expect(wrapper.text()).not.toContain("media/app");

    await wrapper.find('input[aria-label="Search retag targets"]').setValue("");
    await wrapper.find("select").setValue("available");
    await flushPromises();

    expect(wrapper.text()).toContain("media/app");
    expect(wrapper.text()).not.toContain("data/postgres");

    await wrapper.find("select").setValue("attention");
    await flushPromises();

    expect(wrapper.text()).not.toContain("media/app");
    expect(wrapper.text()).toContain("data/postgres");
  });

  it("shows every Compose service by default and sorts and filters runtime state", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const retags = useRetagsStore();
    retags.retagTargets = retagTargetsResponse([
      retagTarget({
        service_key: "aardvark/unknown",
        stack: "aardvark",
        service: "unknown",
        runtime_state: "unknown",
      }),
      retagTarget({
        service_key: "alpha/stopped-attention",
        stack: "alpha",
        service: "stopped-attention",
        runtime_state: "not-running",
        retag_available: false,
      }),
      retagTarget({
        service_key: "alpha/running-attention",
        stack: "alpha",
        service: "running-attention",
        retag_available: false,
      }),
      retagTarget({
        service_key: "zeta/stopped-ready",
        stack: "zeta",
        service: "stopped-ready",
        runtime_state: "not-running",
      }),
      retagTarget({
        service_key: "zeta/running-ready",
        stack: "zeta",
        service: "running-ready",
      }),
    ]);
    vi.spyOn(retags, "loadRetagTargets").mockResolvedValue();

    const wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    const visibleServices = () =>
      wrapper
        .findAll(".retag-service-cell strong")
        .map((service) => service.text());

    expect(visibleServices()).toEqual([
      "zeta/running-ready",
      "zeta/stopped-ready",
      "aardvark/unknown",
      "alpha/running-attention",
      "alpha/stopped-attention",
    ]);

    await wrapper
      .get('select[aria-label="Retag runtime filter"]')
      .setValue("not-running");
    await flushPromises();
    expect(visibleServices()).toEqual([
      "zeta/stopped-ready",
      "alpha/stopped-attention",
    ]);

    await wrapper
      .get('select[aria-label="Retag runtime filter"]')
      .setValue("unknown");
    await flushPromises();
    expect(visibleServices()).toEqual(["aardvark/unknown"]);

    await wrapper
      .get('select[aria-label="Retag runtime filter"]')
      .setValue("running");
    await flushPromises();
    expect(visibleServices()).toEqual([
      "zeta/running-ready",
      "alpha/running-attention",
    ]);
  });

  it("renders empty, unavailable, loading, and error states", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const retags = useRetagsStore();
    const loadRetagTargets = vi
      .spyOn(retags, "loadRetagTargets")
      .mockResolvedValue();

    retags.loading = true;
    let wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    expect(wrapper.text()).toContain("Loading retag targets");
    expect(loadRetagTargets).toHaveBeenCalledTimes(1);

    retags.loading = false;
    retags.error = "retag targets unavailable";
    wrapper.unmount();
    wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    expect(wrapper.text()).toContain("retag targets unavailable");
    expect(wrapper.text()).toContain("The backend could not load retag review state.");

    retags.error = "";
    retags.retagTargets = retagTargetsResponse([]);
    wrapper.unmount();
    wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    expect(wrapper.text()).toContain("No Compose services found");

    retags.retagTargets = retagTargetsResponse([], {
      status: "unavailable",
      warnings: ["compose discovery failed"],
    });
    wrapper.unmount();
    wrapper = mountWithApp(RetagsView, { pinia });
    await flushPromises();
    expect(wrapper.text()).toContain("compose discovery failed");
    expect(wrapper.text()).toContain("Compose discovery unavailable");
  });
});
