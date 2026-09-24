import { describe, expect, it } from "vitest";
import { imageTransition, planChanges, resultSignals, runChanges, scopeTitle } from "../src/utils/updateSummary";
import { planResponse, runSummary, runVerification } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";
import RunResultSummary from "../src/components/RunResultSummary.vue";

const change = { stack: "home", service: "home-assistant", before: "ghcr.io/home-assistant/home-assistant:2026.5.1", after: "ghcr.io/home-assistant/home-assistant:2026.5.3" };
const run = () => runSummary({ dry_run: false, finished_at: "2026-05-28T12:12:00Z", verification: runVerification() });
const texts = (value: ReturnType<typeof run>) => resultSignals(value).map(signal => signal.text).join(" · ");

describe("update summaries", () => {
  it("scales from a version transition to service and stack totals, keeping identities distinct", () => {
    expect(scopeTitle([change], "Review")).toBe("home-assistant · 2026.5.1 → 2026.5.3");
    expect(scopeTitle([change, { ...change, service: "db" }], "Review")).toBe("home · 2 services");
    expect(scopeTitle([change, { ...change, stack: "other" }], "Review")).toBe("2 services across 2 stacks");
    expect(scopeTitle([], "No changes")).toBe("No changes");
    const plan = planResponse();
    plan.stacks[0].lines.push({ ...plan.stacks[0].lines[0] });
    expect(planChanges(plan)).toHaveLength(1);
  });

  it("does not confuse registry ports, digest pins, same-tag refreshes or repository switches with versions", () => {
    expect(imageTransition({ ...change, before: "localhost:5000/app:1", after: "localhost:5000/app:2" })).toBe("1 → 2");
    expect(imageTransition({ ...change, before: "app:stable", after: "app:stable" })).toBe("stable · same image reference");
    expect(imageTransition({ ...change, before: "app@sha256:abc", after: "app@sha256:def" })).toBe("sha256:abc → sha256:def");
    expect(imageTransition({ ...change, after: "other:2" })).toContain("other:2");
    expect(imageTransition({ ...change, after: "" })).toBe("Image transition not recorded");
  });

  it("keeps image verification separate from failed, skipped and unknown health evidence", () => {
    const value = run();
    expect(texts(value)).toBe("Image verified · Health checks passed");
    value.verification!.items[0].health_status = "timed_out";
    value.verification!.items[0].follow_up_needed = true;
    expect(texts(value)).toBe("Image verified · Health needs attention · Follow-up needed");
    value.verification!.items[0].health_status = "skipped";
    expect(texts(value)).toContain("Health checks skipped");
    value.verification!.items[0].health_status = "unknown";
    expect(texts(value)).toContain("Health evidence incomplete");
  });

  it("distinguishes mixed passed and skipped health checks from missing evidence", () => {
    const value = run();
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "other", image_status: "already_current", health_status: "skipped" });
    expect(resultSignals(value)[1]).toEqual({ text: "Health checks · 1 passed · 1 skipped", tone: "default" });
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "third", health_status: "unknown", follow_up_needed: true });
    expect(resultSignals(value)[1]).toEqual({ text: "Health evidence incomplete · 1/3 passed · 1 skipped", tone: "warning" });
  });

  it("keeps skipped checks visible alongside failures and unknown evidence", () => {
    const value = run();
    value.verification!.items[0].health_status = "failed";
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "skipped", health_status: "skipped" });
    expect(resultSignals(value)[1]).toEqual({ text: "Health needs attention · 1/2 · 1 skipped", tone: "warning" });
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "unknown", health_status: "unknown" });
    expect(resultSignals(value)[1]).toEqual({ text: "Health needs attention · 1/3 · 1 unknown · 1 skipped", tone: "warning" });
  });

  it.each(["failed", "timed_out", "unknown"] as const)("retains skipped evidence within a service that also has %s health evidence", healthStatus => {
    const value = run();
    value.verification!.items[0].health_status = healthStatus;
    value.verification!.items.push({ ...value.verification!.items[0], health_status: "skipped" });
    expect(resultSignals(value)[1]).toEqual({
      text: healthStatus === "unknown" ? "Health evidence incomplete · 0/1 passed · 1 skipped" : "Health needs attention · 1 skipped",
      tone: "warning",
    });
  });

  it.each([10, undefined])("does not let duplicate verification rows cover an event-only service (event ID %s)", eventId => {
    const value = run();
    const item = { ...value.verification!.items[0], event_id: eventId, stack_name: "media", service_name: "db", image: "postgres:16", target_image: "postgres:17" };
    value.verification!.items = [item, { ...item, line_no: 2 }];
    value.events = [{ id: 10, run_id: 1, created_at: "", stack_name: "media", service_name: "db", image: "postgres:16", target_image: "postgres:17", old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }];
    expect(texts(value)).toBe("Image verified · Health checks passed");
    value.events.push({ ...value.events[0], id: 11, stack_name: "home" });
    expect(resultSignals(value).slice(0, 2)).toEqual([
      { text: "Image evidence incomplete · 1/2 verified", tone: "warning" },
      { text: "Health evidence incomplete · 1/2 passed", tone: "warning" },
    ]);
    Object.assign(value.verification!.items[1], { image_status: "failed", health_status: "failed", follow_up_needed: true });
    expect(texts(value)).toContain("Image needs attention · 1/2 · 1 unknown");
    expect(texts(value)).toContain("Health needs attention · 1/2 · 1 unknown");
    Object.assign(value.verification!.items[1], { image_status: "unknown", health_status: "unknown" });
    expect(texts(value)).toContain("0/2 verified");
    expect(texts(value)).toContain("0/2 passed");
  });

  it("keeps an uncovered event unknown even when its service already has verified evidence", () => {
    const value = run();
    Object.assign(value.verification!.items[0], { event_id: 10, stack_name: "media", service_name: "db" });
    value.events = [10, 11].map(id => ({ id, run_id: 1, created_at: "", stack_name: "media", service_name: "db", image: "postgres:16", target_image: `postgres:${id}`, old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }));
    expect(texts(value)).toContain("0/1 verified");
    expect(texts(value)).toContain("0/1 passed");
  });

  it("does not turn dry runs, unfinished runs or missing verification into verified success", () => {
    const value = run();
    value.dry_run = true;
    expect(texts(value)).toBe("Dry run · no changes applied");
    value.dry_run = false;
    value.finished_at = null;
    expect(texts(value)).toContain("verification pending");
    value.finished_at = "2026-05-28T12:12:00Z";
    value.verification = null;
    expect(texts(value)).toBe("Verification not recorded");
  });

  it("aggregates mixed multi-stack results without hiding missing or failed services", () => {
    const value = run();
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "other", image_status: "failed", health_status: "failed", follow_up_needed: true });
    expect(scopeTitle(runChanges(value), "Run")).toBe("2 services across 2 stacks");
    expect(texts(value)).toContain("Image needs attention · 1/2");
    expect(texts(value)).toContain("Health needs attention · 1/2");
  });

  it("keeps an unrecorded target explicit in run detail and compact History", () => {
    const value = run();
    value.status = "failure";
    value.events = [];
    Object.assign(value.verification!.items[0], { service_name: "app", image: "repo/app:1.0", target_image: "", image_status: "failed", health_status: "unknown" });
    for (const compact of [false, true]) {
      const wrapper = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value, compact }), template: '<RunResultSummary :run="value" :compact="compact" />' });
      expect(wrapper.get(".scope-title").text()).toBe("app · Image transition not recorded");
      expect(wrapper.text()).not.toContain("same image reference");
      wrapper.unmount();
    }
  });

  it("keeps unknown image and health evidence explicit alongside failures", () => {
    const value = run();
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "failed", image_status: "failed", health_status: "failed", follow_up_needed: true });
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "unknown", image_status: "unknown", health_status: "unknown", follow_up_needed: true });
    expect(resultSignals(value).slice(0, 2)).toEqual([
      { text: "Image needs attention · 1/3 · 1 unknown", tone: "warning" },
      { text: "Health needs attention · 1/3 · 1 unknown", tone: "warning" },
    ]);
    value.events = [{ id: 1, run_id: 1, created_at: "", stack_name: "event-only", service_name: "db", image: "db:1", target_image: "db:2", old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }];
    expect(resultSignals(value)[0].text).toBe("Image needs attention · 1/4 · 2 unknown");
    expect(resultSignals(value)[1].text).toBe("Health needs attention · 1/4 · 2 unknown");
  });

  it.each([
    ["", "db"],
    ["media", ""],
    ["", ""],
  ])("counts a matched legacy event once with stack=%s service=%s", (stack, service) => {
    const value = run();
    Object.assign(value.verification!.items[0], { event_id: 10, stack_name: "media", service_name: "db", image: "postgres:16", target_image: "postgres:17" });
    value.events = [{ id: 10, run_id: 1, created_at: "", stack_name: stack, service_name: service, image: "postgres:16", target_image: "postgres:17", old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }];
    expect(runChanges(value)).toEqual([{ stack: "media", service: "db", before: "postgres:16", after: "postgres:17" }]);
    expect(texts(value)).toBe("Image verified · Health checks passed");
    for (const compact of [false, true]) {
      const wrapper = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value, compact }), template: '<RunResultSummary :run="value" :compact="compact" />' });
      expect(wrapper.get(".scope-title").text()).toBe("db · 16 → 17");
      wrapper.unmount();
    }
    // Identical image references alone must not swallow another event-only service.
    value.events.push({ ...value.events[0], id: 11, stack_name: "other", service_name: "db" });
    expect(scopeTitle(runChanges(value), "Run")).toBe("2 services across 2 stacks");
    expect(texts(value)).toContain("Image evidence incomplete · 1/2 verified");
    expect(texts(value)).toContain("Health evidence incomplete · 1/2 passed");
  });

  it("retains unresolved legacy events and verification without its event", () => {
    const value = run();
    Object.assign(value.verification!.items[0], { event_id: null, stack_name: "media", service_name: "db", target_image: "", image_status: "unknown", health_status: "unknown" });
    value.events = [{ id: 10, run_id: 1, created_at: "", stack_name: "", service_name: "db", image: "postgres:16", target_image: "postgres:17", old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }];
    expect(runChanges(value)).toHaveLength(2);
    expect(texts(value)).toContain("0/2 verified");
    value.verification!.items[0].event_id = 99;
    expect(runChanges(value)).toHaveLength(2);
  });

  it("retains event-only services and treats incomplete verification coverage as unknown", () => {
    const value = run();
    value.events = [{ id: 1, run_id: 1, created_at: "", stack_name: "other", service_name: "db", image: "db:1", target_image: "db:2", old_image_id: "", new_image_id: "", old_digest: "", new_digest: "", status: "success", metadata: {} }];
    expect(runChanges(value)).toHaveLength(2);
    expect(texts(value)).toContain("Image evidence incomplete · 1/2 verified");
    expect(texts(value)).toContain("Health evidence incomplete · 1/2 passed");
  });


  it("does not interpret audit event targets as container images", () => {
    const value = run();
    value.mode = "web-settings";
    value.verification = null;
    expect(runChanges(value)).toEqual([]);
    expect(texts(value)).toBe("Action success");
  });

  it("reuses the result summary with bounded History content and expandable full detail", () => {
    const value = run();
    value.verification!.items.push({ ...value.verification!.items[0], stack_name: "other" });
    const full = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value }), template: '<RunResultSummary :run="value" />' });
    expect(full.get("details").attributes("open")).toBeUndefined();
    expect(full.text()).toContain("2 services across 2 stacks");
    expect(full.findAll(".scope-stack")).toHaveLength(2);
    const compact = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value }), template: '<RunResultSummary :run="value" compact />' });
    expect(compact.find("details").exists()).toBe(false);
    expect(compact.text()).toContain("2 services across 2 stacks");
    expect(compact.get("time").attributes("title")).toBe(value.finished_at);
    full.unmount();
    compact.unmount();
  });

  it("keeps omitted History evidence explicit without claiming a partial scope was verified", () => {
    const value = run();
    value.verification_omitted_count = 10000;
    // Even affirmative evidence supplied by an older cache cannot override the omission marker.
    expect(runChanges(value)).toEqual([]);
    expect(resultSignals(value)).toEqual([{ text: "10000 update records · open run for verification", tone: "warning" }]);
    const wrapper = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value }), template: '<RunResultSummary :run="value" compact />' });
    expect(wrapper.get(".scope-title").text()).toContain(`Run #${value.id}`);
    expect(wrapper.text()).toContain("10000 update records");
    expect(wrapper.text()).not.toContain("Image verified");
    expect(wrapper.text()).not.toContain("Health checks passed");
    wrapper.unmount();
  });

  it("exposes exact start and finish times in a native run-detail disclosure", () => {
    const value = run();
    const wrapper = mountWithApp({ components: { RunResultSummary }, setup: () => ({ value }), template: '<RunResultSummary :run="value" />' });
    const disclosure = wrapper.get("details.result-times");
    expect(disclosure.get("summary").text()).toBe("Exact timestamps");
    expect(disclosure.findAll("time").map(time => time.text())).toEqual([value.started_at, value.finished_at]);
    expect(disclosure.findAll("time").map(time => time.attributes("datetime"))).toEqual([value.started_at, value.finished_at]);
    wrapper.unmount();
  });
});
