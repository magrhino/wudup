import { describe, expect, it } from "vitest";
import PendingPlanLineList from "../src/components/pending/PendingPlanLineList.vue";
import PendingReviewSummary from "../src/components/pending/PendingReviewSummary.vue";
import {
  extraInterruptionLabel, impactSummary, operationalImpact, reviewEvidence, stackImpact, stackImpactIsNotable,
} from "../src/views/pending/reviewSummary";
import { planLinesFromPlan } from "../src/views/pending/utils";
import { planResponse, releaseNoteInfo, securityScanInfo } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";
import type { PlanStack } from "../src/api/client";

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

  it("describes recreate impact as structured data and flags only what the services list does not show", () => {
    const stack = planResponse().stacks[0]!;
    stack.actions.push(
      { kind: "stop", args: [], description: "", cwd: "" },
      { kind: "up", args: ["up", "--remove-orphans", "--wait"], description: "", cwd: "" },
    );
    // A plain recreate of the updated service is what the services list already implies.
    const plain = stackImpact(stack)!;
    expect(plain).toEqual({ mode: "recreate", wholeStack: false, alsoRestarted: [], dependenciesMayStart: false });
    expect(stackImpactIsNotable(plain)).toBe(false);
    // A network provider joins the lifecycle scope without an image update of its own.
    stack.services = ["gluetun", "app"];
    stack.stop_services = ["app", "gluetun", "db"];
    expect(stackImpact(stack)!.alsoRestarted).toEqual(["db", "gluetun"]);
    expect(stackImpactIsNotable(stackImpact(stack)!)).toBe(true);
    // A whole-stack recreate never lists the updated service as an extra restart.
    stack.services = [];
    stack.stop_services = ["worker", "app"];
    expect(stackImpact(stack)).toMatchObject({ wholeStack: true, alsoRestarted: ["worker"] });
    stack.force_recreate = true;
    expect(stackImpact(stack)!.mode).toBe("force-recreate");
    stack.services = ["app"];
    stack.up_no_deps = false;
    expect(stackImpact(stack)!.dependenciesMayStart).toBe(true);
    stack.actions = ["pause", "unpause"].map(kind => ({ kind, args: [], description: "", cwd: "" }));
    expect(stackImpact(stack)).toMatchObject({ mode: "pause", dependenciesMayStart: false });
  });

  it("keeps a text impact only for plans without a recreate or pause step", () => {
    const stack = planResponse().stacks[0]!;
    // No recreate or pause step: show every planned step rather than a short claim.
    expect(stackImpact(stack)).toBeNull();
    expect(impactSummary(stack, false)).toMatch(/^Pull images for /);
    expect(impactSummary(stack, true)).toMatch(/^media: Pull images for /);
    stack.actions = [{ kind: "stop", args: [], description: "", cwd: "" }];
    expect(impactSummary(stack, true)).toBe("media: Stop app.");
    stack.actions = [];
    expect(stackImpact(stack)).toBeNull();
    expect(impactSummary(stack, false)).toContain("Execution steps are not recorded");
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
    expect(result.unresolved).toEqual([]);
    expect(result.gaps.join(" ")).toContain("not confirmed for this planned version");
    // A scan for another candidate is neither reused nor reported as missing.
    expect(result.gaps.join(" ")).not.toMatch(/scan/i);
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
      expect(result.gaps.join(" ")).not.toMatch(/digest|scan/i);
    }
    // Even a scan for the new tag cannot confirm a different retained digest.
    scan.subject.requested_ref = line.target_image;
    scan.subject.reported_digest = "sha256:new";
    scan.subject.index_digest = "sha256:new";
    scan.subject.manifest_digest = "sha256:new";
    expect(reviewEvidence(plan, [note], [scan], false, "").supporting).toEqual([]);
    // An explicit digest in the plan remains usable without scan metadata.
    line.target_image = "repo/app@sha256:new";
    expect(reviewEvidence(plan, [], [], false, "").supporting.join(" ")).toContain("Planned image digest sha256:new");
  });

  it.each(["stale", "partial", "error"] as const)("keeps %s scan results unknown despite cached positive comparisons", state => {
    const { plan, note, scan } = evidenceFixture();
    scan.state = state;
    scan.error_message = "Refresh the scan to compare this image.";
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("0 introduced");
    expect(result.gaps.join(" ")).toContain("Refresh the scan to compare this image.");
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
    expect(result.gaps.join(" ")).toContain("database revisions differ");
    expect(result.unresolved.join(" ")).toContain("Database migration requires manual review");
    expect(result.unresolved).toContain("Release notes mention security changes that need review. Advisory lookup was incomplete.");
    expect(result.gaps.join(" ")).not.toContain("Advisory lookup was incomplete");
  });

  it("shows missing identity, stale metadata and failed release lookups without a clean result", () => {
    const plan = planResponse();
    plan.stacks[0]!.lines[0]!.metadata_status = "recovered";
    const result = reviewEvidence(plan, [], [], false, "Release provider is unavailable.");
    expect(result.supporting).toEqual([]);
    // Tag-based plans rarely carry a digest; its absence is not reported.
    expect(result.gaps.join(" ")).not.toMatch(/digest/i);
    expect(result.unresolved.join(" ")).toContain("metadata is recovered");
    expect(result.gaps.join(" ")).toContain("Release provider is unavailable");
  });

  it("prioritizes loading and request errors when release information does not match", () => {
    const { plan, note, scan } = evidenceFixture();
    note.release_tag = "v3.0.0";
    const loading = reviewEvidence(plan, [note], [scan], true, "Release lookup failed.");
    expect(loading.unresolved).toEqual(["Release information is loading."]);
    const failed = reviewEvidence(plan, [note], [scan], false, "Release lookup failed.");
    expect(failed.unresolved).toEqual([]);
    expect(failed.gaps).toContain("Release lookup failed.");
  });

  it("keeps an unknown verdict unresolved even when the scan completed", () => {
    const { plan, note, scan } = evidenceFixture();
    scan.verdict = "unknown";
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.supporting.join(" ")).not.toContain("comparison");
    expect(result.gaps.join(" ")).toContain("Security evidence is incomplete.");
  });

  it.each([
    { loading: true, error: "", expected: "Candidate security scan information is loading." },
    { loading: false, error: "Scanner unavailable.", expected: "Candidate security scan metadata is unavailable: Scanner unavailable." },
  ])("does not present cached scan success during an unresolved request ($loading)", ({ loading, error, expected }) => {
    const { plan, note, scan } = evidenceFixture();
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan, loading, error }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="loading" :security-scans-error="error" :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(wrapper.text()).toContain(expected);
    expect(wrapper.text()).not.toContain("0 introduced");
    wrapper.unmount();
  });

  it.each(["not_scanned", "disabled", "unsupported"] as const)("says nothing about security for an unscanned candidate (%s)", state => {
    const { plan, note, scan } = evidenceFixture();
    scan.state = state;
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.unresolved).toEqual([]);
    expect([...result.supporting, ...result.gaps].join(" ")).not.toMatch(/scan|finding|trivy/i);
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="false" security-scans-error="" :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(wrapper.find(".review-attention").exists()).toBe(false);
    expect(wrapper.find("details.review-evidence summary").text()).toBe("Evidence details");
    expect(wrapper.text()).not.toMatch(/scan|security|not available/i);
    wrapper.unmount();
  });

  it("says nothing about scan requests when the backend reports scanning is off", () => {
    const { plan, note, scan } = evidenceFixture();
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="true" security-scans-error="Scanner unavailable." security-scans-disabled :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(wrapper.find(".review-attention").exists()).toBe(false);
    expect(wrapper.text()).not.toMatch(/scan|security/i);
    wrapper.unmount();
  });

  it("lists introduced scan findings and verified release advisories before applying", () => {
    const { plan, note, scan } = evidenceFixture();
    scan.comparison.status = "worse";
    scan.comparison.introduced_findings = [{
      target: "", target_class: "", target_type: "", vulnerability_id: "CVE-2026-0001", package_name: "openssl",
      installed_version: "3.0.0", fixed_version: "3.0.1", severity: "high", title: "", primary_url: "",
    }];
    note.security = { ...note.security, outcome: "verified_critical_high", severity: "critical", reason: "GHSA-xxxx affects 2.0.0." };
    const result = reviewEvidence(plan, [note], [scan], false, "");
    expect(result.unresolved.join(" ")).toContain("1 introduced");
    expect(result.unresolved.join(" ")).toContain("Release advisory (critical) — GHSA-xxxx affects 2.0.0.");
    expect(result.supporting.join(" ")).not.toMatch(/introduced|advisory/);
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="false" security-scans-error="" :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(wrapper.find(".review-attention").text()).toContain("GHSA-xxxx affects 2.0.0.");
    expect(wrapper.find(".review-attention").text()).toContain("1 introduced");
    wrapper.unmount();
  });

  it.each([
    { loading: true, error: "", attention: "Candidate security scan information is loading." },
    { loading: false, error: "Scanner unavailable.", attention: "" },
  ])("reports an unresolved scan request once, not as a per-service gap ($loading)", ({ loading, error, attention }) => {
    const { plan, note, scan } = evidenceFixture();
    const line = plan.stacks[0]!.lines[0]!;
    line.digest_provenance = {
      source_image: line.compose_image, resolved_tag: "2.0.0", watch_tag: "2.0.0",
      target_digest: "sha256:candidate", final_image: line.target_image,
      provenance_source: "registry", provenance_confidence: "verified",
    };
    plan.stacks.push({ ...plan.stacks[0]!, name: "other" });
    expect(reviewEvidence(plan, [note], null, false, "").gaps).toEqual([]);
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan, loading, error }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="loading" :security-scans-error="error" :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(wrapper.find("details.review-evidence summary").text()).toBe("Evidence details");
    expect(wrapper.text()).not.toMatch(/no candidate scan is confirmed/i);
    if (attention) expect(wrapper.find(".review-attention").text()).toContain(attention);
    else expect(wrapper.find(".review-attention").exists()).toBe(false);
    if (error) expect(wrapper.find("details.review-evidence").text()).toContain(`Candidate security scan metadata is unavailable: ${error}`);
    wrapper.unmount();
  });

  it("renders scoped reasons as text and discloses all evidence for larger selections", () => {
    const { plan, note, scan } = evidenceFixture();
    plan.stacks.push({ ...plan.stacks[0]!, name: "other" });
    const reasons = ["Approval required: review the label replacement.", "Skipped: service is stopped.", "<script>unsafe()</script>", "The Compose file changed. Preview again."];
    const wrapper = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan, note, scan, reasons }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[note]" :security-scans="[scan]" :security-scans-loading="false" security-scans-error="" :release-notes-loading="false" release-notes-error="" :reasons="reasons" />',
    });
    expect(wrapper.text()).toContain("other / app:");
    expect(wrapper.text()).toContain("Approval required: review the label replacement.");
    expect(wrapper.text()).toContain("Skipped: service is stopped.");
    expect(wrapper.text()).toContain("<script>unsafe()</script>");
    expect(wrapper.find("script").exists()).toBe(false);
    // Decision reasons are never collapsed; evidence and its limits are.
    expect(wrapper.find(".review-attention").text()).toContain("The Compose file changed. Preview again.");
    expect(wrapper.find("details.review-evidence").text()).toContain("evidence is advisory");
    wrapper.unmount();
  });

  it("draws extra restarts in the services list instead of a text line per stack", () => {
    const plan = planResponse();
    const stack = plan.stacks[0]!;
    stack.services = [];
    stack.stop_services = ["gluetun", "app", "thelounge"];
    stack.actions.push(
      { kind: "stop", args: [], description: "", cwd: "" },
      { kind: "up", args: ["up", "--wait"], description: "", cwd: "" },
    );
    const quiet = planResponse();
    quiet.stacks[0]!.name = "arr";
    quiet.stacks[0]!.actions.push({ kind: "up", args: ["up", "--wait"], description: "", cwd: "" });
    plan.stacks.push(quiet.stacks[0]!);
    const planLines = planLinesFromPlan(plan);
    const summary = mountWithApp({
      components: { PendingReviewSummary },
      setup: () => ({ plan }),
      template: '<PendingReviewSummary :plan="plan" :release-notes="[]" :security-scans="[]" :security-scans-loading="false" security-scans-error="" :release-notes-loading="false" release-notes-error="" :reasons="[]" />',
    });
    expect(summary.find(".review-impact").exists()).toBe(false);
    summary.unmount();

    const list = mountWithApp({
      components: { PendingPlanLineList },
      setup: () => ({ plan, planLines }),
      template: '<PendingPlanLineList :plan-lines="planLines" :stacks="plan.stacks" :stack-count="2" :release-notes="[]" :release-notes-loading="false" release-notes-error="" />',
    });
    const rows = list.findAll(".list-row");
    // The impact row follows its stack's last service; a plain recreate gets none.
    expect(rows.map(row => row.classes().includes("plan-impact-row"))).toEqual([false, true, false]);
    expect(rows[1]!.attributes("aria-label")).toBe("media stack impact");
    expect(rows[1]!.text()).toContain("Whole stack");
    // The updated service itself is not listed as an extra restart.
    expect(rows[1]!.find(".plan-impact-detail").text().replace(/\s+/g, "")).toBe("Alsorestartsgluetunthelounge");
    list.unmount();
  });

  it("counts paused and restarted extra services in one future-tense header label", () => {
    const plan = planResponse();
    const stack = plan.stacks[0]!;
    stack.actions.push({ kind: "up", args: ["up", "--wait"], description: "", cwd: "" });
    expect(extraInterruptionLabel(plan)).toBe("");
    stack.services = ["gluetun", "app"];
    expect(extraInterruptionLabel(plan)).toBe("Also interrupts 1 service");
    const paused = planResponse().stacks[0]!;
    paused.name = "arr";
    paused.services = ["app", "worker"];
    paused.actions = ["pause", "unpause"].map(kind => ({ kind, args: [], description: "", cwd: "" }));
    plan.stacks.push(paused);
    // A paused service is interrupted, not restarted; the header must not claim a restart.
    expect(extraInterruptionLabel(plan)).toBe("Also interrupts 2 services");
  });

  it.each([
    { name: "pause", mutate: (stack: PlanStack) => {
      stack.services = ["app", "worker"];
      stack.actions = ["pause", "unpause"].map(kind => ({ kind, args: [], description: "", cwd: "" }));
    }, tags: "Paused", detail: "Alsopausedworker" },
    { name: "force recreate", mutate: (stack: PlanStack) => {
      stack.force_recreate = true;
      stack.actions.push({ kind: "up", args: ["up"], description: "", cwd: "" });
    }, tags: "Force recreate", detail: "Recreatedevenifunchanged" },
    { name: "dependencies", mutate: (stack: PlanStack) => {
      stack.up_no_deps = false;
      stack.actions.push({ kind: "up", args: ["up"], description: "", cwd: "" });
    }, tags: "", detail: "Dependenciesmayalsostart" },
  ])("renders the $name impact row copy", ({ mutate, tags, detail }) => {
    const plan = planResponse();
    mutate(plan.stacks[0]!);
    const planLines = planLinesFromPlan(plan);
    const list = mountWithApp({
      components: { PendingPlanLineList },
      setup: () => ({ plan, planLines }),
      template: '<PendingPlanLineList :plan-lines="planLines" :stacks="plan.stacks" :stack-count="1" :release-notes="[]" :release-notes-loading="false" release-notes-error="" />',
    });
    const row = list.find(".plan-impact-row");
    expect(row.find(".plan-line-heading").text().replace(/\s+/g, "")).toBe(`media${tags.replace(/\s+/g, "")}`);
    expect(row.find(".plan-impact-detail").text().replace(/\s+/g, "")).toBe(detail);
    list.unmount();
  });
});
