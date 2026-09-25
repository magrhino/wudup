import { describe, expect, it } from "vitest";
import PendingReviewSummary from "../src/components/pending/PendingReviewSummary.vue";
import { operationalImpact, reviewEvidence } from "../src/views/pending/reviewSummary";
import { planResponse, releaseNoteInfo, securityScanInfo } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

function evidenceFixture() {
  const plan = planResponse();
  Object.assign(plan.stacks[0]!.lines[0]!, { target_image: "repo/app:2.0.0", digest: "sha256:candidate" });
  const note = releaseNoteInfo({ image_repo: "repo/app" });
  const scan = securityScanInfo({ state: "complete", verdict: "none_reported" });
  scan.subject.requested_ref = "repo/app:2.0.0";
  scan.comparison.status = "unchanged";
  return { plan, note, scan };
}

describe("Pending review decision summary", () => {
  it("describes the actual execution scope, including services outside the selected image lines", () => {
    const stack = planResponse().stacks[0]!;
    stack.services = ["app", "worker"];
    stack.stop_services = ["worker"];
    stack.actions.push(
      { kind: "stop", args: [], description: "", cwd: "" },
      { kind: "up", args: ["up", "--remove-orphans", "--wait"], description: "", cwd: "" },
    );
    const impact = operationalImpact(stack);
    expect(impact).toContain("Stop worker.");
    expect(impact).toContain("Start or recreate changed services: app, worker.");
    expect(impact).toContain("orphan containers");
    expect(impact).toContain("results are not available yet");
    expect(impact).not.toContain("start dependencies");
    stack.services = [];
    stack.force_recreate = true;
    expect(operationalImpact(stack)).toContain("Force-recreate all services in this stack.");
    stack.services = ["app"];
    stack.up_no_deps = false;
    expect(operationalImpact(stack)).toContain("Compose may also start dependencies.");
  });

  it("does not invent execution or health steps when the plan lacks them", () => {
    const stack = planResponse().stacks[0]!;
    expect(operationalImpact(stack)).not.toMatch(/Stop|recreate|Health/);
    stack.actions = [];
    expect(operationalImpact(stack)).toContain("Execution steps are not recorded");
    stack.actions = ["pause", "up", "unpause", "health-wait"].map(kind => ({ kind, args: [], description: "", cwd: "" }));
    expect(operationalImpact(stack)).toContain("Pause app.");
    expect(operationalImpact(stack)).toContain("Unpause app.");
  });

  it("shows version context and comparable scan counts, including explicit zero introduced findings", () => {
    const { plan, note, scan } = evidenceFixture();
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).toContain("release v2.0.0 has the planned version label (upstream context)");
    expect(result.supporting.join(" ")).toContain("0 fixed, 0 introduced, 0 remaining findings");
    expect(result.supporting.join(" ")).toContain("not proof that the image is running");
    expect(result.unresolved).toEqual([]);
    const line = plan.stacks[0]!.lines[0]!;
    line.target_image = "repo/app@sha256:candidate";
    line.digest_provenance = {
      source_image: line.compose_image, resolved_tag: "2.0.0", watch_tag: "2.0.0",
      target_digest: "sha256:candidate", final_image: line.target_image,
      provenance_source: "registry", provenance_confidence: "verified",
    };
    const pinned = reviewEvidence(plan, [note], [scan], false, "");
    expect(pinned.supporting.join(" ")).toContain("release v2.0.0 has the planned version label");
    expect(pinned.supporting.join(" ")).toContain("0 introduced");
    expect(pinned.unresolved).toEqual([]);
  });

  it("does not apply another candidate's release or scan evidence to an override on the same queue line", () => {
    const { plan, note, scan } = evidenceFixture();
    Object.assign(plan.stacks[0]!.lines[0]!, { target_image: "repo/app:3.0.0", digest: "sha256:new" });
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("has the planned version label");
    expect(result.supporting.join(" ")).not.toContain("0 introduced");
    expect(result.unresolved.join(" ")).toContain("not confirmed for this planned version");
    expect(result.unresolved.join(" ")).toContain("no candidate scan is confirmed");
    plan.stacks[0]!.lines[0]!.target_image = "other/app@sha256:candidate";
    expect(reviewEvidence(plan, [note], [scan], false, "").supporting.join(" ")).not.toContain("0 introduced");
  });

  it("does not treat a retained WUD digest as evidence for an overridden tag", () => {
    const { plan, note, scan } = evidenceFixture();
    const line = plan.stacks[0]!.lines[0]!;
    // The planner replaces desired_tag but retains the original WUD digest.
    Object.assign(line, { target_image: "repo/app:3.0.0", resolved_image: "repo/app:3.0.0", desired_tag: "3.0.0" });
    for (const scans of [[scan], []]) {
      const result = reviewEvidence(plan, [note], scans, false, "");
      expect(result.supporting).toEqual([]);
      expect(result.unresolved.join(" ")).toContain("no exact image digest is confirmed for this planned target");
      expect(result.unresolved.join(" ")).toContain("no candidate scan is confirmed");
    }
    // Even a scan for the new tag cannot confirm a different retained digest.
    scan.subject.requested_ref = line.target_image;
    scan.subject.reported_digest = "sha256:new";
    scan.subject.index_digest = "sha256:new";
    scan.subject.manifest_digest = "sha256:new";
    expect(reviewEvidence(plan, [note], [scan], false, "").supporting).toEqual([]);
    // An explicit digest in the plan remains usable without scan metadata.
    line.target_image = "repo/app@sha256:new";
    expect(reviewEvidence(plan, [], [], false, "").supporting.join(" ")).toContain("planned image digest sha256:new");
  });

  it.each(["stale", "partial", "error"] as const)("keeps %s scan results unknown despite cached positive comparisons", state => {
    const { plan, note, scan } = evidenceFixture();
    scan.state = state;
    scan.error_message = "Refresh the scan to compare this image.";
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("0 introduced");
    expect(result.unresolved.join(" ")).toContain("Refresh the scan to compare this image.");
  });

  it("keeps comparison provenance gaps, breaking changes and release security warnings explicit", () => {
    const { plan, note, scan } = evidenceFixture();
    scan.comparison.status = "unknown";
    scan.comparison.message = "The vulnerability database revisions differ.";
    note.breaking = true;
    note.breaking_reasons = ["Database migration requires manual review."];
    note.security = { ...note.security, outcome: "needs_review", reason: "Advisory lookup was incomplete." };
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("0 introduced");
    expect(result.unresolved.join(" ")).toContain("database revisions differ");
    expect(result.unresolved.join(" ")).toContain("Database migration requires manual review");
    expect(result.unresolved.join(" ")).toContain("Advisory lookup was incomplete");
  });

  it("shows missing identity, stale metadata and failed release lookups without a clean result", () => {
    const plan = planResponse();
    plan.stacks[0]!.lines[0]!.metadata_status = "recovered";
    const result = reviewEvidence(plan, [], [], false, "Release provider is unavailable.");
    expect(result.supporting).toEqual([]);
    expect(result.unresolved.join(" ")).toContain("no exact image digest");
    expect(result.unresolved.join(" ")).toContain("metadata is recovered");
    expect(result.unresolved.join(" ")).toContain("Release provider is unavailable");
  });

  it("prioritizes loading and request errors when release information does not match", () => {
    const { plan, note, scan } = evidenceFixture();
    note.release_tag = "v3.0.0";
    const loading = reviewEvidence(plan, [note], [scan], true, "Release lookup failed.");
    expect(loading.unresolved).toEqual(["media / app: Release information is loading."]);
    const failed = reviewEvidence(plan, [note], [scan], false, "Release lookup failed.");
    expect(failed.unresolved).toEqual(["media / app: Release lookup failed."]);
  });

  it("keeps an unknown verdict unresolved even when the scan completed", () => {
    const { plan, note, scan } = evidenceFixture();
    scan.verdict = "unknown";
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("comparison");
    expect(result.unresolved.join(" ")).toContain("Security evidence is incomplete.");
  });

  it("renders scoped reasons as text and discloses all evidence for larger selections", () => {
    const { plan, note, scan } = evidenceFixture();
    plan.stacks.push({ ...plan.stacks[0]!, name: "other" });
    const reasons = ["Approval required: review the label replacement.", "Skipped: service is stopped.", "<script>unsafe()</script>", "The Compose file changed. Preview again."];
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan, reasons }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :release-notes-loading="false" release-notes-error="" :reasons="reasons" />',
    });
    expect(wrapper.text()).toContain("other / app:");
    expect(wrapper.text()).toContain("Approval required: review the label replacement.");
    expect(wrapper.text()).toContain("Skipped: service is stopped.");
    expect(wrapper.text()).toContain("<script>unsafe()</script>");
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.findAll("details").some(details => details.text().includes("The Compose file changed. Preview again."))).toBe(true);
    expect(wrapper.text()).toContain("evidence is advisory");
    wrapper.unmount();
  });
});
