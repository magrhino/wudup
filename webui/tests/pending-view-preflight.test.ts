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

describe("pending view preflight safety", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("allows read-only pending preflight but blocks apply", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(false);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    const createPlan = vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        can_apply: false,
        apply_preflight: failedApplyPreflight(
          "mutations-enabled",
          "Set WUD_WEB_MUTATIONS_ENABLED=true on the server to apply updates.",
        ),
      });
    });
    const applyPlan = vi.spyOn(updates, "applyPlan");
    const wrapper = mountPendingView(pinia);

    await wrapper
      .find('input[aria-label="Select stack media"]')
      .setValue(true);
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review selected ("))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("Read-only mode is active");
    expect(createPlan).toHaveBeenCalledWith(
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-1" }],
    );
    const applyButton = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Apply 1 update"));
    expect(applyButton?.exists()).toBe(true);
    expect(applyButton?.attributes("disabled")).toBeDefined();
    const modal = wrapper.find(".preflight-modal");
    expect(modal.text().split("1 failed check must be fixed before applying.")).toHaveLength(2);
    expect(modal.text().split("Set WUD_WEB_MUTATIONS_ENABLED=true on the server to apply updates.")).toHaveLength(2);
    expect(modal.find(".apply-readiness").text()).toContain("Read-only mode is active.");
    const summary = modal.find('[aria-label="Update review decision summary"]');
    expect(summary.text()).toContain("Operational impact");
    expect(summary.text()).toContain("Supporting evidence");
    expect(summary.text()).toContain("Unresolved before apply");
    expect(summary.find(".apply-readiness").text()).toContain("Read-only mode is active.");
    expect(modal.classes()).toContain("preflight-modal-fixed-footer");
    expect(modal.element.tagName).toBe("DIV");
    expect(modal.attributes("aria-modal")).toBe("true");
    expect(modal.find(".preflight-scroll-content .preflight-footer").exists()).toBe(false);
    expect(modal.find(":scope > .preflight-footer").text()).toContain("Close");
    expect(modal.find(":scope > .preflight-footer").text()).toContain("Apply 1 update");

    expect(applyPlan).not.toHaveBeenCalled();
  });

  it.each([
    { mutationsEnabled: true, code: "docker-reachable", detail: "Docker is unavailable. Check the Docker connection.", expected: "Docker is unavailable. Check the Docker connection." },
    { mutationsEnabled: false, code: "mutations-enabled", detail: "", expected: "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to apply updates." },
    { mutationsEnabled: false, code: "docker-reachable", detail: "Docker is unavailable.", expected: "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on the server to apply updates." },
    { mutationsEnabled: true, code: "docker-reachable", detail: "", expected: "Fix the failed apply readiness check before applying updates." },
  ])("keeps one explanation and fallback guidance for $code ($mutationsEnabled, $detail)", async ({ mutationsEnabled, code, detail, expected }) => {
    const { pinia, settings, updates } = setupStores(mutationsEnabled);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({ can_apply: false, apply_preflight: failedApplyPreflight(code, detail) });
    });
    const applyPlan = vi.spyOn(updates, "applyPlan");
    const wrapper = mountPendingView(pinia);
    await wrapper.findAll("button").find((button) => button.text() === "Review media plan")!.trigger("click");
    await flushPromises();

    const modal = wrapper.find(".preflight-modal");
    expect(modal.text().split(expected)).toHaveLength(2);
    if (detail) expect(modal.find(".apply-readiness").text()).toContain(detail);
    expect(modal.findAll("button").find((button) => button.text() === "Apply 1 update")!.attributes("disabled")).toBeDefined();
    expect(applyPlan).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("shows blocked preflight errors without an apply action", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    const createPlan = vi.spyOn(updates, "createPlan").mockImplementation(async () => {
      updates.plan = planResponse({
        can_apply: false,
        status: "blocked",
        summary: {
          target_count: 1,
          matched_target_count: 0,
          stack_count: 0,
          service_count: 0,
          skipped_count: 1,
          issue_count: 1,
        },
        stacks: [],
        issues: [
          {
            severity: "error",
            code: "unmatched",
            message: "No Compose service matched repo/app:1.0.",
            line_no: 1,
            stack: "",
            service: "",
            hint: "",
            details: {},
          },
        ],
        skipped: [
          {
            line_no: 1,
            raw: "repo/app:1.0",
            image: "repo/app:1.0",
            desired_tag: "1.1",
            reason: "unmatched",
          },
        ],
      });
    });
    const applyPlan = vi.spyOn(updates, "applyPlan");
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    expect(createPlan).toHaveBeenCalledWith(
      [1],
      true,
      [],
      [],
      [{ line_no: 1, selection_id: "selection-1" }],
    );
    expect(wrapper.find('[role="dialog"]').text()).toContain("Plan blocked");
    expect(wrapper.find('[role="dialog"]').text()).toContain(
      "No Compose service matched repo/app:1.0.",
    );
    expect(
      wrapper
        .find('[role="dialog"]')
        .findAll("button")
        .some((button) => button.text().includes("Apply 1 update")),
    ).toBe(false);
    expect(applyPlan).not.toHaveBeenCalled();
  });

  it("rebuilds digest-pin label rewrite plans after approval", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    const approval = {
      stack: "media",
      service: "app",
      label_key: "wud.tag.include",
      current_label_value: "latest|stable",
      planned_tag: "latest",
      proposed_label_value: "^latest$$",
    };
    const issue = {
      severity: "error",
      code: "compose-digest-pin-label-rewrite-unapproved",
      message:
        'media wud.tag.include is "latest|stable"; approve replacing it with "^latest$" before pinning the digest.',
      line_no: null,
      stack: "media",
      service: "app",
      hint: "Approve the label rewrite.",
      details: {
        ...approval,
        compose_file: "docker-compose.yml",
        proposed_label_regex: "^latest$",
        explanation:
          "WUDup can only overwrite this include rule after explicit approval.",
      },
    };
    const createPlan = vi
      .spyOn(updates, "createPlan")
      .mockImplementation(async (_lines, _allow, _tags, approvals = []) => {
        const base = planResponse();
        updates.plan = approvals.length
          ? planResponse({
              stacks: [
                {
                  ...base.stacks[0],
                  digest_pin_updates: [
                    {
                      source_image: "repo/app:latest",
                      resolved_tag: "latest",
                      planned_digest: "sha256:abc123",
                      final_image: "repo/app@sha256:abc123",
                      watch_tag: "latest",
                      marker: "wud.updater.digest-pin.repo-app",
                      label_key: "wud.tag.include",
                      label_value: "^latest$$",
                      services: ["app"],
                      label_rewrites: [
                        {
                          service: "app",
                          label_key: "wud.tag.include",
                          current_label_value: "latest|stable",
                          planned_tag: "latest",
                          proposed_label_value: "^latest$$",
                          proposed_label_regex: "^latest$",
                          approved: true,
                          reason: "approved",
                        },
                      ],
                    },
                  ],
                },
              ],
            })
          : planResponse({
              can_apply: false,
              status: "blocked",
              summary: {
                ...base.summary,
                issue_count: 1,
              },
              issues: [issue],
              apply_preflight: failedApplyPreflight(
                "selected-services-matched",
                issue.message,
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
    expect(dialog.text()).toContain("Digest-pin label approvals");
    expect(dialog.text()).toContain("wud.tag.include=latest|stable");
    expect(dialog.text()).toContain("^latest$");

    await dialog
      .findAll("button")
      .find((button) => button.text().includes("Approve label rewrite"))
      ?.trigger("click");
    await flushPromises();

    const selections = [{ line_no: 1, selection_id: "selection-1" }];
    expect(createPlan).toHaveBeenNthCalledWith(
      1,
      [1],
      true,
      [],
      [],
      selections,
    );
    expect(createPlan).toHaveBeenNthCalledWith(
      2,
      [1],
      true,
      [],
      [approval],
      selections,
    );
    expect(wrapper.find('[role="dialog"]').text()).toContain(
      "Digest-pin label updates",
    );
    expect(wrapper.find('[role="dialog"]').text()).toContain(
      "wud.tag.include=latest|stable",
    );
  });

  it("does not reopen digest-pin approval preflight after close", async () => {
    const { pinia, auth, connection, settings, updates, runs } = setupStores(true);
    updates.pending = pendingResponse();
    mockPendingLifecycle(settings, updates);
    let resolveSecondPlan: () => void = () => {};
    const secondPlan = new Promise<void>((resolve) => {
      resolveSecondPlan = resolve;
    });
    const approval = {
      stack: "media",
      service: "app",
      label_key: "wud.tag.include",
      current_label_value: "latest|stable",
      planned_tag: "latest",
      proposed_label_value: "^latest$$",
    };
    const issue = {
      severity: "error",
      code: "compose-digest-pin-label-rewrite-unapproved",
      message:
        'media wud.tag.include is "latest|stable"; approve replacing it with "^latest$" before pinning the digest.',
      line_no: null,
      stack: "media",
      service: "app",
      hint: "Approve the label rewrite.",
      details: {
        ...approval,
        compose_file: "docker-compose.yml",
        proposed_label_regex: "^latest$",
        explanation:
          "WUDup can only overwrite this include rule after explicit approval.",
      },
    };
    const createPlan = vi
      .spyOn(updates, "createPlan")
      .mockImplementation(async (_lines, _allow, _tags, approvals = []) => {
        const base = planResponse();
        if (approvals.length) {
          await secondPlan;
          updates.plan = planResponse({
            stacks: [
              {
                ...base.stacks[0],
                digest_pin_updates: [
                  {
                    source_image: "repo/app:latest",
                    resolved_tag: "latest",
                    planned_digest: "sha256:abc123",
                    final_image: "repo/app@sha256:abc123",
                    watch_tag: "latest",
                    marker: "wud.updater.digest-pin.repo-app",
                    label_key: "wud.tag.include",
                    label_value: "^latest$$",
                    services: ["app"],
                    label_rewrites: [
                      {
                        service: "app",
                        label_key: "wud.tag.include",
                        current_label_value: "latest|stable",
                        planned_tag: "latest",
                        proposed_label_value: "^latest$$",
                        proposed_label_regex: "^latest$",
                        approved: true,
                        reason: "approved",
                      },
                    ],
                  },
                ],
              },
            ],
          });
          return;
        }
        updates.plan = planResponse({
          can_apply: false,
          status: "blocked",
          summary: {
            ...base.summary,
            issue_count: 1,
          },
          issues: [issue],
          apply_preflight: failedApplyPreflight(
            "selected-services-matched",
            issue.message,
          ),
        });
      });
    const wrapper = mountPendingView(pinia);

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");
    await flushPromises();

    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Approve label rewrite"))
      ?.trigger("click");
    await wrapper
      .find('[role="dialog"]')
      .findAll("button")
      .find((button) => button.text().includes("Close"))
      ?.trigger("click");

    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    resolveSecondPlan();
    await flushPromises();

    const selections = [{ line_no: 1, selection_id: "selection-1" }];
    expect(createPlan).toHaveBeenNthCalledWith(
      1,
      [1],
      true,
      [],
      [],
      selections,
    );
    expect(createPlan).toHaveBeenNthCalledWith(
      2,
      [1],
      true,
      [],
      [approval],
      selections,
    );
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
  });

  it("replans a mobile stream decision before enabling apply", async () => {
    const restoreViewport = mockMobileViewport();
    try {
      const { pinia, settings, updates } = setupStores(true);
      updates.pending = pendingResponse([
        pendingItem({
          current_tag: "1.0.0-distroless",
          desired_tag: "1.1.0",
          tag_stream: {
            current_stream: "distroless",
            reported_stream: "default",
          },
        }),
      ]);
      mockPendingLifecycle(settings, updates);
      const issue = {
        severity: "error",
        code: "tag-stream-change",
        message: "Choose whether to preserve or switch streams.",
        line_no: 1,
        stack: "media",
        service: "app",
        hint: "",
        details: {
          current_stream: "distroless",
          reported_stream: "default",
          reported_tag: "1.1.0",
          same_stream_tag: "1.1.0-distroless",
          preserve_label_regex: String.raw`^\d+\.\d+\.\d+-distroless$`,
        },
      };
      const createPlan = vi
        .spyOn(updates, "createPlan")
        .mockImplementation(
          async (_lines, _allow, _overrides, _digestApprovals, _selections, decisions = []) => {
            const base = planResponse();
            const decision = decisions[0]?.decision ?? "preserve";
            updates.plan = decisions.length
              ? planResponse({
                  stacks: [
                    {
                      ...base.stacks[0],
                      tag_stream_updates: [
                        {
                          line_no: 1,
                          service: "app",
                          current_tag: "1.0.0-distroless",
                          reported_tag: "1.1.0",
                          selected_tag:
                            decision === "preserve"
                              ? "1.1.0-distroless"
                              : "1.1.0",
                          decision,
                          label_key: "wud.tag.include",
                          current_label_value: "",
                          proposed_label_value:
                            decision === "preserve"
                              ? String.raw`^\d+\.\d+\.\d+-distroless$$`
                              : String.raw`^\d+\.\d+\.\d+$$`,
                          proposed_label_regex:
                            decision === "preserve"
                              ? String.raw`^\d+\.\d+\.\d+-distroless$`
                              : String.raw`^\d+\.\d+\.\d+$`,
                          approved: true,
                          reason: "label-added",
                        },
                      ],
                    },
                  ],
                })
              : planResponse({
                  can_apply: false,
                  status: "blocked",
                  issues: [issue],
                  summary: { ...base.summary, issue_count: 1 },
                });
          },
        );
      const wrapper = mountPendingView(pinia);

      const chooseStream = wrapper
        .findAll("button")
        .find((button) => button.text().includes("Choose stream"));
      expect(chooseStream?.attributes("aria-haspopup")).toBe("dialog");
      expect(
        wrapper.find('input[aria-label="New tag for repo/app:1.0"]').exists(),
      ).toBe(false);
      await chooseStream?.trigger("click");
      await flushPromises();
      const dialog = wrapper.find('[role="dialog"]');
      expect(dialog.text()).toContain("Keep distroless");
      expect(
        dialog
          .findAll("button")
          .some((button) => button.text().includes("Apply 1 update")),
      ).toBe(false);

      await dialog
        .findAll("button")
        .find((button) => button.text().includes("Keep distroless"))
        ?.trigger("click");
      await flushPromises();

      expect(createPlan).toHaveBeenLastCalledWith(
        [1],
        true,
        [],
        [],
        [{ line_no: 1, selection_id: "selection-1" }],
        [{ line_no: 1, decision: "preserve" }],
        [],
      );
      expect(wrapper.find('[role="dialog"]').text()).toContain(
        "Selected update stream",
      );
      expect(wrapper.find('[role="dialog"]').text()).toContain(
        "Decision selected",
      );
      const switchStream = wrapper
        .find('[role="dialog"]')
        .findAll("button")
        .find((button) => button.text().includes("Switch to default"));
      expect(switchStream?.attributes("disabled")).toBeUndefined();
      await switchStream?.trigger("click");
      await flushPromises();
      expect(createPlan).toHaveBeenLastCalledWith(
        [1],
        true,
        [],
        [],
        [{ line_no: 1, selection_id: "selection-1" }],
        [{ line_no: 1, decision: "switch" }],
        [],
      );
      expect(
        wrapper
          .find('[role="dialog"]')
          .findAll("button")
          .find((button) => button.text().includes("Switch to default"))
          ?.attributes("disabled"),
      ).toBeDefined();
      expect(wrapper.find('[role="dialog"]').text()).toContain(
        String.raw`Resulting labelwud.tag.include=^\d+\.\d+\.\d+$`,
      );
      expect(
        wrapper
          .find('[role="dialog"]')
          .findAll("button")
          .some((button) => button.text().includes("Apply 1 update")),
      ).toBe(true);
      await wrapper
        .find('[role="dialog"]')
        .findAll("button")
        .find((button) => button.text().includes("Keep distroless"))
        ?.trigger("click");
      await flushPromises();
      expect(createPlan).toHaveBeenLastCalledWith(
        [1],
        true,
        [],
        [],
        [{ line_no: 1, selection_id: "selection-1" }],
        [{ line_no: 1, decision: "preserve" }],
        [],
      );
      expect(wrapper.find('[role="dialog"]').text()).toContain(
        String.raw`Resulting labelwud.tag.include=^\d+\.\d+\.\d+-distroless$`,
      );
    } finally {
      restoreViewport();
    }
  });
});
