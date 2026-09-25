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
  pendingSourceInfo,
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
import { jsonResponse } from "./helpers/storeActions";


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

describe("pending view fallback and release notes", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("keeps grouped update details collapsed by default", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    expect(wrapper.find("details.stack-details").attributes("open")).toBeUndefined();
    expect(wrapper.text()).toContain("Details");
    expect(wrapper.find('summary[aria-label="Details for media"]').exists()).toBe(true);
  });

  it("renders stack services and change preview text", () => {
    const radarr = pendingGroupedItem({
      line_no: 1,
      image: "lscr.io/linuxserver/radarr:5.0",
      repo: "lscr.io/linuxserver/radarr",
      current_tag: "5.0",
      desired_tag: "",
      target_image: "lscr.io/linuxserver/radarr:5.1",
      services: ["radarr"],
      action: "recreate_service",
    });
    const updater = pendingGroupedItem({
      line_no: 2,
      image: "ghcr.io/example/wudup:1.0",
      repo: "ghcr.io/example/wudup",
      current_tag: "1.0",
      desired_tag: "2.0",
      target_image: "ghcr.io/example/wudup:2.0",
      services: ["wudup"],
    });
    const stackItem = pendingGroupedItem({
      line_no: 3,
      image: "redis:latest@sha256:abc",
      repo: "redis",
      current_tag: "latest",
      desired_tag: "",
      digest: "sha256:abc",
      target_image: "redis:latest@sha256:abc",
      compose_images: ["redis:latest"],
      services: [],
      action: "recreate_stack",
    });
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        line_no: 1,
        status: "error",
        links: [],
        error: "no supported GitHub release source found",
      }),
      releaseNoteInfo({
        line_no: 2,
        breaking: true,
        breaking_reasons: ["Major version update."],
      }),
    ]);
    settings.servicePolicies = [
      servicePolicy({ service_key: "media/wudup", auto_update: true }),
    ];
    updates.pending = {
      source_file: "/out/images.todo",
      exists: true,
      count: 3,
      items: [radarr, updater, stackItem],
      grouping: {
        ...pendingGrouping([radarr, updater, stackItem]),
        groups: [
          {
            ...pendingGrouping([radarr, updater, stackItem]).groups[0],
            services_label: "radarr, wudup",
            services: ["radarr", "wudup"],
            line_numbers: [1, 2, 3],
            items: [radarr, updater, stackItem],
          },
        ],
      },
      warnings: [],
    };
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    const card = wrapper.find(".stack-card");

    expect(card.find(".stack-identity").text()).toContain(
      "Services radarr, wudup",
    );
    const previewText = card.find(".stack-change-preview").text();
    expect(previewText).toContain("radarr");
    expect(previewText).toContain("Recreate service");
    expect(previewText).toContain("No release notes");
    expect(previewText).toContain("lscr.io/linuxserver/radarr:5.0");
    expect(previewText).toContain("lscr.io/linuxserver/radarr:5.1");
    expect(previewText).toContain("wudup");
    expect(previewText).toContain("Tag update");
    expect(previewText).toContain("Major bump");
    expect(previewText).toContain("Possible breaking");
    expect(previewText).not.toContain("Auto-update");
    expect(card.find(".stack-details").text()).toContain("Auto-update");
    expect(previewText).not.toContain("Fresh metadata");
    expect(card.find(".stack-details").text()).toContain("Fresh metadata");
    expect(previewText).toContain("Stack restart");
    expect(previewText).toContain("ghcr.io/example/wudup:1.0");
    expect(previewText).toContain("ghcr.io/example/wudup:2.0");
    expect(card.text()).toContain("Recreate stack");
    expect(card.text()).toContain("Mutable latest");
    expect(card.text()).toContain("Digest-only");
    expect(card.text()).toContain("Stack restart");
    expect(card.find(".stack-card-tags").text()).not.toContain(
      "radarr, wudup",
    );
  });

  it.each([
    ["acme/app:5.1.0", "v5.1.0", true],
    ["registry.example:5000/acme/app:v5.1.0", "5.1.0", true],
    ["acme/app:latest", "latest", false],
    ["acme/app@sha256:abc", "v5.0.0", false],
    ["acme/app:5.1", "v5.1", false],
    ["acme/app:v5.1", "v5.1", false],
  ])("uses the recreate target %s for release matching", (targetImage, releaseTag, matched) => {
    const item = pendingGroupedItem({
      image: "acme/app:5.0.0",
      repo: "acme/app",
      current_tag: "5.0.0",
      desired_tag: "",
      target_image: targetImage,
      action: "recreate_service",
    });
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = { ...pendingResponse([item]), grouping: pendingGrouping([item]) };
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({ line_no: item.line_no, release_tag: releaseTag }),
    ]);
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    const release = wrapper.find(".stack-change-release");

    expect(release.text()).toContain(matched ? "Matched to candidate" : "Upstream context");
    expect(release.text()).not.toContain(matched ? "Upstream context" : "Matched to candidate");
  });

  it("shows release access for every candidate while retaining risks and precise advisory scope", () => {
    const items = Array.from({ length: 12 }, (_, index) => pendingGroupedItem({
      line_no: index + 1,
      selection_id: `selection-${index + 1}`,
      image: `repo/service-${index + 1}:1.0`,
      repo: `repo/service-${index + 1}`,
      services: [`service-${index + 1}`],
      current_tag: "1.0",
      desired_tag: index >= 8 ? "2.0" : "1.0",
      metadata_status: index === 10 ? "retained" : "fresh",
    }));
    const { pinia, settings, updates } = setupStores(false);
    updates.pending = { ...pendingResponse(items), grouping: pendingGrouping(items) };
    updates.releaseNotes = releaseNotesResponse(items.map((item) => releaseNoteInfo({
      line_no: item.line_no,
      security: {
        outcome: item.line_no === 12 ? "verified_critical_high" : item.line_no === 11 ? "needs_review" : "ordinary",
        severity: item.line_no >= 11 ? "critical" : "",
        reason_code: "fixture",
        reason: "Fixture evidence",
        advisory_ids: [],
        lookup_truncated: false,
      },
    })));
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    const card = wrapper.find(".stack-card");

    expect(card.findAll(".stack-change-row")).toHaveLength(12);
    expect(card.findAll(".stack-change-release")).toHaveLength(12);
    expect(card.findAll(".stack-details .candidate-release")).toHaveLength(0);
    expect(card.findAll(".stack-details .pending-update-row")).toHaveLength(12);
    expect(card.find(".stack-details").attributes("open")).toBeUndefined();
    const preview = card.find(".stack-change-preview");
    expect(preview.text()).toContain("Retained metadata");
    expect(preview.text()).toContain("Release advisory: critical");
    expect(preview.findAll(".evidence-explanation")).toHaveLength(2);
    expect(preview.text()).toContain("candidate scans inspect the proposed image");
    expect(preview.text()).toContain("not a guarantee of a safe update");
    expect(preview.text()).toContain("Release advisory: needs review");
    expect(card.find(".stack-card-tags").text()).toContain("1 verified high/critical release update");
  });

  it("renders active snoozes in the snoozed pending section", () => {
    const radarr = pendingGroupedItem({
      line_no: 1,
      image: "lscr.io/linuxserver/radarr:5.0",
      repo: "lscr.io/linuxserver/radarr",
      current_tag: "5.0",
      desired_tag: "",
      target_image: "lscr.io/linuxserver/radarr:5.1",
      services: ["radarr"],
      action: "recreate_service",
    });
    const updater = pendingGroupedItem({
      line_no: 2,
      image: "ghcr.io/example/wudup:1.0",
      repo: "ghcr.io/example/wudup",
      current_tag: "1.0",
      desired_tag: "2.0",
      target_image: "ghcr.io/example/wudup:2.0",
      services: ["wudup"],
    });
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    settings.snoozes = [snooze({ service_key: "media/radarr" })];
    updates.pending = {
      source_file: "/out/images.todo",
      exists: true,
      count: 2,
      items: [radarr, updater],
      grouping: {
        ...pendingGrouping([radarr, updater]),
        groups: [
          {
            ...pendingGrouping([radarr, updater]).groups[0],
            services_label: "radarr, wudup",
            services: ["radarr", "wudup"],
            line_numbers: [1, 2],
            items: [radarr, updater],
          },
        ],
      },
      warnings: [],
    };
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    const stackCard = wrapper
      .findAll(".stack-card")
      .find((card) => card.find(".stack-identity").exists());
    const snoozedCard = wrapper
      .findAll(".stack-card")
      .find((card) => card.text().includes("Snoozed pending entries"));

    expect(stackCard?.find(".stack-identity").text()).toContain(
      "Services wudup",
    );
    expect(stackCard?.find(".stack-identity").text()).not.toContain("radarr");
    expect(stackCard?.find(".stack-change-preview").text()).not.toContain(
      "lscr.io/linuxserver/radarr:5.0",
    );
    expect(snoozedCard?.text()).toContain("Snoozed pending entries");
    expect(snoozedCard?.text()).toContain(
      "Excluded from bulk selection while snoozed.",
    );
    expect(snoozedCard?.text()).toContain("media / radarr");
    expect(snoozedCard?.text()).toContain("Snoozed");
    expect(snoozedCard?.text()).toContain("lscr.io/linuxserver/radarr:5.0");
    expect(snoozedCard?.text()).toContain("lscr.io/linuxserver/radarr:5.1");
    expect(snoozedCard?.text()).toContain("Pending file line #1");
  });

  it("shows ready preflight service impact and row tag rewrites", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        selected_line_numbers: [1, 2],
        summary: {
          target_count: 2,
          matched_target_count: 2,
          stack_count: 1,
          service_count: 2,
          skipped_count: 0,
          issue_count: 0,
        },
        stacks: [
          {
            ...planResponse().stacks[0],
            services_label: "radarr, wudup",
            services: ["radarr", "wudup"],
            pull_services: ["radarr", "wudup"],
            stop_services: ["radarr", "wudup"],
            tag_updates: [
              {
                old_image: "lscr.io/linuxserver/radarr:5.0",
                desired_tag: "5.1",
                new_image: "lscr.io/linuxserver/radarr:5.1",
                services: ["radarr"],
              },
            ],
            lines: [
              {
                line_no: 1,
                raw: "lscr.io/linuxserver/radarr:5.0",
                image: "lscr.io/linuxserver/radarr:5.0",
                resolved_image: "lscr.io/linuxserver/radarr:5.0",
                compose_image: "lscr.io/linuxserver/radarr:5.0",
                target_image: "lscr.io/linuxserver/radarr:5.1",
                service: "radarr",
                digest: "",
                desired_tag: "5.1",
                action: "tag-update",
              },
              {
                line_no: 2,
                raw: "ghcr.io/example/wudup:1.0",
                image: "ghcr.io/example/wudup:1.0",
                resolved_image: "ghcr.io/example/wudup:1.0",
                compose_image: "ghcr.io/example/wudup:1.0",
                target_image: "ghcr.io/example/wudup:1.1",
                service: "wudup",
                digest: "",
                desired_tag: "",
                action: "recreate_service",
              },
            ],
          },
        ],
      });
    });
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    const dialog = wrapper.find('[role="dialog"]');
    const readiness = dialog.find(".apply-readiness");
    const impact = dialog.find(".preflight-impact");
    expect(dialog.find("#preflight-modal-title").text()).toBe("Review media plan");
    expect(dialog.find(".preflight-impact-text").text()).toBe(
      "radarr, wudup",
    );
    expect(readiness.exists()).toBe(true);
    expect(readiness.text()).toContain("Apply readiness");
    expect(readiness.text()).toContain("Ready");
    expect(readiness.text()).toContain("9 checks passed");
    expect(readiness.text()).toContain("Docker reachable");
    expect(readiness.text()).toContain("Selected services matched");
    expect(readiness.find(".apply-readiness-passed").exists()).toBe(true);
    expect(readiness.findAll(".apply-readiness-row")).toHaveLength(0);
    expect(readiness.text()).not.toContain("docker-daemon-info");
    expect(impact.exists()).toBe(true);
    expect(impact.text()).toContain("Services and images");
    expect(impact.text()).toContain("radarr");
    expect(impact.text()).toContain("wudup");
    expect(impact.find(".tag-rewrite-detail").text()).toContain(
      "lscr.io/linuxserver/radarr:5.0 -> lscr.io/linuxserver/radarr:5.1",
    );
    expect(
      dialog
        .findAll("details.preflight-details")
        .some((details) => details.text().includes("Services and images")),
    ).toBe(false);
    expect(
      dialog
        .findAll("details.preflight-details")
        .some((details) => details.text().includes("Commands")),
    ).toBe(true);
    expect(
      dialog
        .findAll("details.preflight-details")
        .some((details) => details.text().includes("Source lines")),
    ).toBe(true);
  });

  it("shows failed apply readiness and disables apply", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        can_apply: false,
        apply_preflight: failedApplyPreflight(
          "logs-writable",
          "/logs is not a directory",
        ),
      });
    });
    const applyPlan = vi.spyOn(updates, "applyPlan");
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    const dialog = wrapper.find('[role="dialog"]');
    const readiness = dialog.find(".apply-readiness");
    expect(readiness.exists()).toBe(true);
    expect(readiness.text()).toContain("Blocked");
    expect(readiness.text()).toContain("8 checks passed");
    expect(readiness.text()).toContain("Logs writable");
    expect(readiness.text()).toContain("/logs is not a directory");
    expect(readiness.find(".apply-readiness-passed").exists()).toBe(true);
    expect(readiness.findAll(".apply-readiness-row")).toHaveLength(1);
    expect(readiness.text()).not.toContain("docker-daemon-info");
    expect(readiness.text()).toContain("/logs is not a directory");
    expect(dialog.text().split("/logs is not a directory")).toHaveLength(2);

    const applyButton = dialog
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"));
    expect(applyButton?.attributes("disabled")).toBeDefined();
    await applyButton?.trigger("click");

    expect(applyPlan).not.toHaveBeenCalled();
  });

  it("explains when degraded WUD metadata blocks apply", async () => {
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        can_apply: false,
        source: pendingSourceInfo({
          active: "api",
          configured: "api",
          fresh: false,
          degraded: true,
        }),
        apply_preflight: failedApplyPreflight(
          "wud-metadata-current",
          "1 selected update is blocked because its metadata is stale (retained). Check your WUD configuration, then run a successful WUD scan.",
        ),
      });
    });
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.find("#preflight-modal-title").text()).toBe("Apply blocked");
    expect(dialog.text().split("Apply blocked")).toHaveLength(2);
    expect(dialog.text()).toContain("Selected update metadata");
    expect(dialog.text()).toContain(
      "Check your WUD configuration, then run a successful WUD scan.",
    );
    expect(dialog.find('[aria-label="Planned changes summary"]').text()).toContain("app · 1.0 → 1.1");
    expect(dialog.text()).not.toContain("0 plan issues");
  });

  it("falls back to pending file order when grouping is unavailable", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = {
      ...pendingResponse(),
      grouping: {
        status: "unavailable",
        groups: [],
        unmatched: [],
        warnings: [],
      },
    };
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain(
      "Stack grouping is unavailable. Showing pending file order.",
    );
    expect(wrapper.find('[role="table"]').exists()).toBe(true);
  });

  it("keeps issue dump and retry actions available for degraded read-only sources", async () => {
    const { pinia, settings, updates } = setupStores(false);
    updates.pending = {
      ...pendingResponse(),
      source: pendingSourceInfo({
        configured: "auto",
        active: "file",
        fresh: false,
        degraded: true,
        fallback_reason: "WUD API is unavailable: connection refused",
      }),
    };
    mockPendingLifecycle(settings, updates);
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "pending" });
    await router.isReady();
    const routerPush = vi.spyOn(router, "push");
    const wrapper = mountWithApp(PendingView, { pinia, router });
    await flushPromises();

    expect(wrapper.text()).toContain(
      "WUD API is unavailable: connection refused",
    );
    expect(wrapper.find('[role="status"]').exists()).toBe(true);
    const viewDump = wrapper
      .findAll("button")
      .find((button) => button.text().includes("View affected containers"));
    const retry = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Refresh WUDup status"));
    expect(viewDump?.attributes("data-button-type")).toBe("primary");
    expect(viewDump?.attributes("disabled")).toBeUndefined();
    expect(retry?.attributes("data-button-type")).not.toBe("primary");
    expect(retry?.attributes("disabled")).toBeUndefined();
    expect(retry?.attributes("title")).toBe(
      "Reads current WUD status without triggering a rescan.",
    );
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text().includes("Rescan WUD"))
        ?.attributes("disabled"),
    ).toBeDefined();

    await viewDump?.trigger("click");
    expect(routerPush).toHaveBeenCalledWith({ name: "issue-dump" });
    await routerPush.mock.results.at(-1)?.value;

    expect(router.currentRoute.value.name).toBe("issue-dump");
  });

  it("refreshes degraded pending status without mutation side effects", async () => {
    const { pinia, auth, settings, updates } = setupStores(false);
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf");
    updates.pending = {
      ...pendingResponse(),
      source: pendingSourceInfo({
        configured: "api",
        active: "api",
        fresh: false,
        degraded: true,
        detail: "WUD observations are degraded",
      }),
    };
    const lifecycle = mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    await flushPromises();
    expect(lifecycle.loadPending).toHaveBeenCalledTimes(1);

    const retry = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Refresh WUDup status"));

    await retry?.trigger("click");
    await flushPromises();

    expect(lifecycle.loadPending).toHaveBeenCalledTimes(2);
    expect(ensureCsrf).not.toHaveBeenCalled();
    expect(lifecycle.loadReleaseNotes).toHaveBeenCalledTimes(1);
    expect(lifecycle.loadSecurityScans).toHaveBeenCalledTimes(1);
    expect(lifecycle.refreshReleaseNotes).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain(
      "WUDup status refreshed.",
    );
  });

  it("shows degraded pending status retry failures inline", async () => {
    const { pinia, settings, updates } = setupStores(false);
    updates.pending = {
      ...pendingResponse(),
      source: pendingSourceInfo({
        configured: "api",
        active: "api",
        fresh: false,
        degraded: true,
        detail: "WUD observations are degraded",
      }),
    };
    const lifecycle = mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    await flushPromises();
    lifecycle.loadPending.mockImplementationOnce(async () => {
      updates.error = "WUD API timed out";
      throw new Error("WUD API timed out");
    });

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Refresh WUDup status"))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain(
      "WUDup status refresh failed: WUD API timed out",
    );
  });

  it("shows safety cues in the mobile pending file order fallback", () => {
    const restore = mockMobileViewport();
    try {
      const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
      updates.pending = {
        ...pendingResponse([
          pendingItem({
            image: "redis:latest@sha256:abc",
            repo: "redis",
            current_tag: "latest",
            desired_tag: "",
            digest: "sha256:abc",
          }),
        ]),
        grouping: {
          status: "unavailable",
          groups: [],
          unmatched: [],
          warnings: [],
        },
      };
      updates.securityScans = {
        source_file: updates.pending.source_file,
        source: updates.pending.source,
        source_hash: updates.pending.source_hash ?? "",
        scanning_enabled: true,
        scanner: "trivy",
        scan_mode: "registry",
        count: 0,
        items: [],
        warnings: [],
      };
      mockPendingLifecycle(settings, updates);
      const wrapper = mountPendingView(pinia);
      const card = wrapper.find(".mobile-card");

      expect(wrapper.find('[role="table"]').exists()).toBe(false);
      expect(card.text()).toContain("Safety cues");
      expect(card.text()).toContain("Digest-only");
      expect(card.text()).toContain("Mutable latest");
      expect(card.text()).toContain("Candidate scan: unavailable");
      const evidence = card.find(".evidence-explanation");
      expect(evidence.text()).toContain("published vulnerability evidence");
      expect(evidence.text()).toContain("candidate scans inspect the proposed image");
      expect(evidence.text()).toContain("not a guarantee of a safe update");
    } finally {
      restore();
    }
  });

  it("shows a pending loading skeleton before queue data is available", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = null;
    updates.loading = true;
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain("Loading pending updates");
    expect(wrapper.find(".pending-loading-state").exists()).toBe(true);
    expect(wrapper.find(".batch-action-bar").exists()).toBe(false);
  });

  it("keeps failed pending loads recoverable without showing stale selection controls", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = null;
    const loadPending = vi
      .spyOn(updates, "loadPending")
      .mockImplementationOnce(async () => {
        updates.error = ("Network request failed");
        throw new Error("Network request failed");
      })
      .mockImplementationOnce(async () => {
        updates.error = ("");
        updates.pending = pendingResponse();
      });
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    const wrapper = mountPendingView(pinia);

    await flushPromises();

    expect(wrapper.text()).toContain("Pending updates unavailable");
    expect(wrapper.text()).toContain("Pending updates did not load");
    expect(wrapper.text()).toContain("Network request failed");
    expect(wrapper.find(".batch-action-bar").exists()).toBe(false);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Retry pending load"))
      ?.trigger("click");
    await flushPromises();

    expect(loadPending).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("1 pending update");
    expect(wrapper.find(".batch-action-bar").exists()).toBe(true);
  });

  it("shows pending safety cue loading failures", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    settings.pendingSafetyCueError = "service policies unavailable";
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain(
      "Pending safety cues are unavailable: service policies unavailable",
    );
  });

  it("deduplicates malformed pending line keys before selecting all stack updates", async () => {
    const itemOne = pendingGroupedItem({
      line_no: 1,
      image: "repo/app:1.0",
      repo: "repo/app",
    });
    const itemTwo = pendingGroupedItem({
      line_no: 2,
      image: "repo/worker:1.0",
      repo: "repo/worker",
    });
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = {
      ...pendingResponse([itemOne, itemTwo]),
      grouping: {
        ...pendingGrouping([itemOne, itemTwo]),
        groups: [
          {
            ...pendingGrouping([itemOne, itemTwo]).groups[0],
            line_numbers: [1, 1, 2],
            items: [itemOne, itemTwo],
          },
        ],
      },
    };
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Select all stack updates"))
      ?.trigger("click");
    await nextTick();

    expect(wrapper.text()).toContain("2 selected");
    expect(wrapper.text()).not.toContain("3 selected");
  });

  it("wraps long pending values in the fallback table", () => {
    const longImage =
      "registry.example.test/selfhosted/very-long-namespace-with-extra-segments/service-name-that-keeps-going:2026.06.01-build-with-extra-metadata";
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = {
      ...pendingResponse([
        pendingItem({
          image: longImage,
          repo: "registry.example.test/selfhosted/very-long-namespace-with-extra-segments/service-name-that-keeps-going",
        }),
      ]),
      grouping: {
        status: "unavailable",
        groups: [],
        unmatched: [],
        warnings: [],
      },
    };
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    const wrappedValues = wrapper.findAll(".pending-table-value");
    expect(wrappedValues.length).toBeGreaterThanOrEqual(2);
    expect(wrappedValues[0].text()).toContain(longImage);
    expect(wrappedValues[0].attributes("title")).toBe(longImage);
  });

  it("renders a clear queue state when no pending updates remain", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse([]);
    runs.runs = [runSummary({ id: 42 })];
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);

    const clearState = wrapper.find(".clear-queue-state");
    expect(clearState.exists()).toBe(true);
    expect(clearState.text()).toContain("Update queue is clear");
    expect(clearState.text()).toContain(
      "images.todo has no updates waiting for review.",
    );
    expect(clearState.text()).toContain("Review latest run #42");
    expect(clearState.find(".clear-queue-mark").exists()).toBe(true);
  });

  it.each(["ready", "blocked"] as const)("preserves release lookup states inside a %s plan", async (status) => {
    const { pinia, settings, updates } = setupStores(true);
    updates.pending = pendingResponse();
    updates.releaseNotesLoading = true;
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({ status });
    });
    const wrapper = mountPendingView(pinia);
    const review = wrapper.findAll("button").find((button) => button.text() === "Review media plan");
    expect(review).toBeDefined();
    await review!.trigger("click");
    await flushPromises();

    const plan = wrapper.find(".preflight-modal");
    const releaseRow = plan.find(".plan-line-release");
    expect(releaseRow.text()).toContain("Checking...");
    await releaseRow.find("button").trigger("click");
    const panel = plan.find(".release-panel");
    expect(panel.text()).toContain("Loading release information");
    expect(panel.text()).not.toContain("not available");

    updates.releaseNotesLoading = false;
    updates.releaseNotesError = "Release lookup failed. Try refreshing pending updates.";
    await nextTick();
    expect(releaseRow.text()).toContain("Check failed");
    expect(panel.text()).toContain(updates.releaseNotesError);

    updates.releaseNotesError = "";
    updates.releaseNotes = releaseNotesResponse([releaseNoteInfo({
      status: "unsupported", body: "", links: [],
      error: "no supported GitHub release source found",
    })]);
    await nextTick();
    expect(releaseRow.text()).toContain("Unavailable");
    expect(panel.text()).toContain("Only GHCR and mapped LinuxServer.io images have release-note links.");

    updates.releaseNotes = releaseNotesResponse([releaseNoteInfo()]);
    await nextTick();
    expect(panel.text()).toContain("Full release notes body.");
    expect(panel.text()).not.toContain("Loading release information");
    expect(panel.text()).not.toContain("Release lookup failed");
    await panel.find(".release-panel-heading button").trigger("click");
    expect(plan.exists()).toBe(true);
    expect(wrapper.find('input[aria-label="Select stack media"]').element).toHaveProperty("checked", true);
  });

  it("renders release-note links with breaking cues", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    updates.pending = pendingResponse();
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        breaking: true,
        breaking_reasons: ["Major version changes from 1 to 2."],
        security: {
          outcome: "verified_critical_high",
          severity: "high",
          reason_code: "verified_exposure",
          reason: "Verified High advisory GHSA-AAAA-BBBB-CCCC affects 1.0.0 and is patched by 2.0.0.",
          advisory_ids: ["GHSA-AAAA-BBBB-CCCC"],
          lookup_truncated: false,
        },
        links: [
          {
            label: "GitHub release",
            url: "https://github.com/acme/app/releases/tag/v2.0.0",
            kind: "github_release",
          },
          {
            label: "GHSA-AAAA-BBBB-CCCC",
            url: "https://github.com/advisories/GHSA-AAAA-BBBB-CCCC",
            kind: "security_advisory",
          },
        ],
      }),
    ]);
    vi.spyOn(updates, "loadPending").mockResolvedValue();
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    const loadChangelog = vi
      .spyOn(updates, "loadReleaseChangelog")
      .mockResolvedValue();
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain("GitHub release");
    await wrapper.find('button[aria-label^="Release notes for"]').trigger("click");
    expect(wrapper.text()).toContain("Possible breaking change");
    expect(wrapper.text()).toContain("Verified High security update");
    expect(wrapper.text()).toContain("1 verified high/critical release update");
    expect(wrapper.text()).toContain("Verified High advisory GHSA-AAAA-BBBB-CCCC");
    expect(wrapper.text()).toContain("Read changelog");
    expect(loadChangelog).not.toHaveBeenCalled();
    const link = wrapper.find(
      'a[href="https://github.com/acme/app/releases/tag/v2.0.0"]',
    );
    expect(link.exists()).toBe(true);
    expect(link.attributes("target")).toBe("_blank");
    expect(link.attributes("rel")).toBe("noopener noreferrer");
    const advisoryLink = wrapper.find(
      'a[href="https://github.com/advisories/GHSA-AAAA-BBBB-CCCC"]',
    );
    expect(advisoryLink.attributes("target")).toBe("_blank");
    expect(advisoryLink.attributes("rel")).toBe("noopener noreferrer");
    expect(
      wrapper.find('output[aria-label="Verified High security update"]').exists(),
    ).toBe(true);
  });

  it("distinguishes security signals that still need review", async () => {
    const { pinia, settings, updates } = setupStores(false);
    updates.pending = pendingResponse();
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        security: {
          outcome: "needs_review",
          severity: "critical",
          reason_code: "exposure_unverified",
          reason: "Critical advisory found; running latest version could not be matched.",
          advisory_ids: ["GHSA-DDDD-EEEE-FFFF"],
          lookup_truncated: false,
        },
      }),
    ]);
    mockPendingLifecycle(settings, updates);

    const wrapper = mountPendingView(pinia);

    await wrapper.find('button[aria-label^="Release notes for"]').trigger("click");
    expect(wrapper.text()).toContain("Security update needs review");
    expect(wrapper.text()).toContain(
      "Critical advisory found; running latest version could not be matched.",
    );
    expect(wrapper.text()).not.toContain("Verified Critical security update");
  });

  it("loads changelog notes on demand and includes them in pending search", async () => {
    const { pinia, settings, updates } = setupStores(false);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          body: "[changelog](https://github.com/t-mart/mousehole/blob/master/CHANGELOG.md)",
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          [
            "# Changelog",
            "",
            "## [v0.5.0](https://github.com/t-mart/mousehole/releases/tag/v0.5.0) - 2026-06-20",
            "",
            "- **Breaking**: Live updates use Server-Sent Events instead of WebSockets.",
            "",
            "## [v0.4.0](https://github.com/t-mart/mousehole/releases/tag/v0.4.0) - 2026-06-04",
            "",
            "- Older release",
          ].join("\n"),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    updates.pending = pendingResponse([
      pendingItem({
        image: "ghcr.io/t-mart/mousehole:0.4.0",
        repo: "ghcr.io/t-mart/mousehole",
        current_tag: "0.4.0",
        desired_tag: "0.5.0",
      }),
    ]);
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        release_tag: "v0.5.0",
        title: "v0.5.0",
        links: [
          {
            label: "GitHub release",
            url: "https://github.com/t-mart/mousehole/releases/tag/v0.5.0",
            kind: "github_release",
          },
        ],
      }),
    ]);
    vi.spyOn(updates, "loadPending").mockResolvedValue();
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "loadSecurityScans").mockResolvedValue();
    vi.spyOn(settings, "loadPendingSafetyCues").mockResolvedValue();
    const wrapper = mountPendingView(pinia);

    expect(fetchMock).not.toHaveBeenCalled();
    await wrapper.find('button[aria-label^="Release notes for"]').trigger("click");
    const readButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Read changelog"));
    expect(readButton?.exists()).toBe(true);

    await readButton?.trigger("click");
    await flushPromises();
    await nextTick();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("Changelog notes");
    expect(wrapper.text()).toContain("Server-Sent Events");
    expect(wrapper.text()).not.toContain("Older release");

    await wrapper
      .find('input[aria-label="Search pending updates"]')
      .setValue("server-sent events");
    await flushPromises();

    expect(wrapper.text()).toContain("1 visible update matched");
    expect(wrapper.text()).toContain("mousehole");
  });

  it("renders both LSIO and upstream release-note links", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    updates.pending = pendingResponse([pendingItem({ image: "linuxserver/radarr:latest" })]);
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        provider: "lsio",
        image_repo: "linuxserver/docker-radarr",
        upstream_repo: "Radarr/Radarr",
        links: [
          {
            label: "LSIO release",
            url: "https://github.com/linuxserver/docker-radarr/releases/tag/5.1.0-ls1",
            kind: "lsio_release",
          },
          {
            label: "Upstream release",
            url: "https://github.com/Radarr/Radarr/releases/tag/v5.1.0",
            kind: "github_release",
          },
        ],
      }),
    ]);
    vi.spyOn(updates, "loadPending").mockResolvedValue();
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "loadSecurityScans").mockResolvedValue();
    vi.spyOn(settings, "loadPendingSafetyCues").mockResolvedValue();
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain("LSIO release");
    expect(wrapper.text()).toContain("Upstream release");
  });

  it("renders unavailable release-note reasons without hiding LSIO links", () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    updates.pending = pendingResponse([
      pendingItem({
        line_no: 1,
        image: "advplyr/audiobookshelf:latest",
        repo: "advplyr/audiobookshelf",
      }),
      pendingItem({
        line_no: 2,
        image: "linuxserver/calibre:latest",
        repo: "linuxserver/calibre",
      }),
    ]);
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        line_no: 1,
        status: "unsupported",
        provider: "unsupported",
        image_repo: "advplyr/audiobookshelf",
        upstream_repo: "",
        links: [],
        error: "no supported GitHub release source found",
      }),
      releaseNoteInfo({
        line_no: 2,
        provider: "lsio",
        image_repo: "linuxserver/docker-calibre",
        upstream_repo: "kovidgoyal/calibre",
        links: [
          {
            label: "LSIO release",
            url: "https://github.com/linuxserver/docker-calibre/releases/tag/1.0.0-ls1",
            kind: "lsio_release",
          },
          {
            label: "Upstream project",
            url: "https://github.com/kovidgoyal/calibre",
            kind: "github_project",
          },
        ],
      }),
    ]);
    vi.spyOn(updates, "loadPending").mockResolvedValue();
    vi.spyOn(updates, "loadReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "refreshReleaseNotes").mockResolvedValue();
    vi.spyOn(updates, "loadSecurityScans").mockResolvedValue();
    vi.spyOn(settings, "loadPendingSafetyCues").mockResolvedValue();
    const wrapper = mountPendingView(pinia);

    expect(wrapper.text()).toContain("Unavailable");
    expect(wrapper.text()).toContain(
      "Only GHCR and mapped LinuxServer.io images have release-note links.",
    );
    expect(wrapper.text()).toContain("LSIO release");
    expect(wrapper.text()).toContain("Upstream project");
  });
});
