import { describe, expect, it } from "vitest";

import {
  candidateLabel,
  compareRetagTargets,
  composeLocation,
  currentTagLabel,
  digestPinSummary,
  labelRewriteSummary,
  planStatusType,
  pluralize,
  reasonDetail,
  reasonLabel,
  reasonTagType,
  retagPlanContextLabel,
  retagPlanSourceFile,
  retagPlanStackUpdates,
  retagUpdateModeLabel,
  retagUpdateSummary,
  searchableText,
  runtimeStateDetail,
  runtimeStateLabel,
  runtimeStateTagType,
  trackingLabel,
  trackingSourceLabel,
  trackingTagType,
} from "../src/views/retags/display";
import { retagPlanResponse, retagTarget } from "./helpers/fixtures";

describe("retag display helpers", () => {
  it("formats target state for review and search", () => {
    const available = retagTarget();
    const stale = retagTarget({
      current_tag: "",
      tracking_tag: "",
      tracking_tag_source: "",
      proposed_tag: "1.2.0",
      final_image: "",
      candidate_warning: "GitHub latest fallback could not resolve the digest.",
      retag_available: false,
      retag_reason: "stale-provenance",
      choices: ["keep-current"],
      compose_file: "",
      digest_provenance: null,
    });

    expect(reasonLabel("eligible")).toBe("Retag available");
    expect(reasonLabel("backend-added-reason")).toBe("Unavailable reason");
    expect(reasonDetail("backend-added-reason")).toBe(
      "The backend did not provide a recognized reason.",
    );
    expect(reasonTagType(available)).toBe("success");
    expect(reasonTagType(stale)).toBe("warning");

    expect(trackingTagType(available)).toBe("info");
    expect(trackingLabel(stale)).toBe("Unknown");
    expect(trackingSourceLabel(stale)).toBe("Source unavailable");
    expect(currentTagLabel(stale)).toBe("Current tag unavailable");
    expect(candidateLabel(available)).toBe("latest -> 1.1");
    expect(candidateLabel(stale)).toBe("latest -> 1.2.0");
    expect(composeLocation(stale)).toBe("/docker/media");
    expect(searchableText(stale)).toContain("stale provenance");
    expect(searchableText(stale)).toContain("could not resolve");
  });

  it("labels and sorts runtime state for operator review", () => {
    const runningReady = retagTarget({
      service_key: "zeta/ready",
      stack: "zeta",
      service: "ready",
    });
    const runningAttention = retagTarget({
      service_key: "alpha/attention",
      stack: "alpha",
      service: "attention",
      retag_available: false,
    });
    const notRunningReady = retagTarget({
      service_key: "beta/ready",
      stack: "beta",
      service: "ready",
      runtime_state: "not-running",
    });
    const notRunningAttention = retagTarget({
      service_key: "alpha/stopped",
      stack: "alpha",
      service: "stopped",
      runtime_state: "not-running",
      retag_available: false,
    });
    const unknown = retagTarget({
      service_key: "aardvark/unknown",
      stack: "aardvark",
      service: "unknown",
      runtime_state: "unknown",
    });

    expect(runtimeStateLabel(runningReady)).toBe("Running");
    expect(runtimeStateTagType(runningReady)).toBe("success");
    expect(runtimeStateLabel(notRunningReady)).toBe("Not running");
    expect(runtimeStateTagType(notRunningReady)).toBe("warning");
    expect(runtimeStateDetail(notRunningReady)).toContain(
      "will create or recreate and start",
    );
    expect(runtimeStateLabel(unknown)).toBe("Unknown");
    expect(runtimeStateTagType(unknown)).toBe("default");
    expect(searchableText(unknown)).toContain("unknown runtime state");

    expect(
      [unknown, notRunningAttention, runningAttention, notRunningReady, runningReady]
        .sort(compareRetagTargets)
        .map((item) => item.service_key),
    ).toEqual([
      "zeta/ready",
      "beta/ready",
      "aardvark/unknown",
      "alpha/attention",
      "alpha/stopped",
    ]);
  });

  it("summarizes retag plans and digest-pin changes", () => {
    const plan = retagPlanResponse();
    const update = plan.stacks[0].digest_pin_updates[0];
    const twoStackPlan = retagPlanResponse({
      stacks: [
        plan.stacks[0],
        {
          ...plan.stacks[0],
          stack: "data",
          directory: "/docker/data",
          project_directory: "/docker/data",
          services: ["postgres"],
        },
      ],
    });
    const emptyPlan = retagPlanResponse({ status: "empty", stacks: [] });

    expect(planStatusType(plan)).toBe("success");
    expect(planStatusType(retagPlanResponse({ status: "blocked" }))).toBe("error");
    expect(planStatusType(retagPlanResponse({ status: "unavailable" }))).toBe(
      "warning",
    );
    expect(planStatusType(emptyPlan)).toBe("default");
    expect(planStatusType(null)).toBe("default");

    expect(retagPlanContextLabel(plan)).toBe("media");
    expect(retagPlanContextLabel(twoStackPlan)).toBe("2 stacks");
    expect(retagPlanContextLabel(emptyPlan)).toBe("retag plan");
    expect(retagPlanSourceFile(plan)).toBe("/docker/media/docker-compose.yml");
    expect(retagPlanSourceFile(twoStackPlan)).toBe("2 Compose files");
    expect(retagPlanSourceFile(emptyPlan)).toBe("Retag plan");
    expect(digestPinSummary(update)).toBe("repo/app:latest -> repo/app@sha256:abc123");
    expect(retagUpdateSummary(update)).toBe(
      "repo/app:latest -> repo/app@sha256:abc123",
    );
    expect(retagUpdateModeLabel(update)).toBe("Digest pin");
    const tagUpdate = {
      target_id: "media/app",
      service_key: "media/app",
      stack: "media",
      service: "app",
      source_image: "repo/app:latest",
      target_tag: "1.1",
      final_image: "repo/app:1.1",
      label_key: "wud.tag.include",
      label_value: String.raw`^1\.1$$`,
      label_rewrites: [],
    };
    expect(retagUpdateSummary(tagUpdate)).toBe("repo/app:latest -> repo/app:1.1");
    expect(retagUpdateModeLabel(tagUpdate)).toBe("Selected tag");
    expect(retagPlanStackUpdates(plan.stacks[0])).toEqual([update]);
    expect(
      retagPlanStackUpdates({
        ...plan.stacks[0],
        tag_updates: [tagUpdate],
      }),
    ).toEqual([tagUpdate]);
    expect(labelRewriteSummary(update)).toBe(
      String.raw`wud.tag.include: ^latest$$ -> ^1\.1$$`,
    );
    expect(labelRewriteSummary({ ...update, label_rewrites: [] })).toBe(
      "No label rewrite",
    );
    const transform = String.raw`^(\d+)\.(\d+)\.(\d+)\.(\d+)-ls(\d+)$ => $1.$2.$3-$4.$5`;
    expect(labelRewriteSummary({ ...tagUpdate, transform_label_value: transform })).toBe(
      `wud.tag.transform: (not set) -> ${transform}`,
    );
    expect(labelRewriteSummary({ ...update, transform_label_value: transform })).toBe(
      String.raw`wud.tag.include: ^latest$$ -> ^1\.1$$; ` + `wud.tag.transform: (not set) -> ${transform}`,
    );
    expect(pluralize(1, "service")).toBe("1 service");
    expect(pluralize(2, "policy", "policies")).toBe("2 policies");
  });
});
