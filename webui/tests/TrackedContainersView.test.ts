import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TrackedContainerItem, TrackingRepairPlan } from "../src/api/client";
import { createWudRouter } from "../src/router";
import { useAuthStore } from "../src/stores/auth";
import { useTrackingStore } from "../src/stores/tracking";
import TrackedContainersView from "../src/views/TrackedContainersView.vue";
import { applyJobResponse, authSession } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

const item: TrackedContainerItem = {
  target_id: "target-radarr",
  service_key: "media/radarr",
  stack: "media",
  service: "radarr",
  compose_path: "/stacks/media/docker-compose.yml",
  image: "repo/radarr:v1.36.2",
  current_tag: "v1.36.2",
  runtime_state: "running",
  tracking_regex: String.raw`^v1\.36\.2$`,
  tracking_health: "frozen",
  tracking_detail: "The filter matches only the installed tag.",
  suggested_regex: String.raw`^v\d+(?:\.\d+)+$`,
  wud: null,
  wud_match_state: "untracked",
  wud_update_available: null,
  last_image_recorded_at: "",
  last_action_at: "",
  last_action_status: "",
  last_action_run_id: null,
  retag_available: false,
};
const repairPlan: TrackingRepairPlan = {
  plan_id: "plan-radarr", source_hash: "source", rendered_hash: "rendered",
  target_id: item.target_id, service_key: item.service_key, stack: item.stack,
  service: item.service, image: item.image, current_regex: item.tracking_regex,
  proposed_regex: item.suggested_regex, compose_diff: "+wud.tag.include=^v\\d+$",
  will_recreate: true, can_apply: true, issues: [],
};
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

describe("TrackedContainersView", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    HTMLElement.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    document.body.replaceChildren();
  });

  it("shows Compose inventory and a plan-first repair for the selected service", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true, mutations_enabled: true });
    const tracking = useTrackingStore();
    tracking.inventory = {
      status: "ready",
      count: 1,
      items: [item],
      wud_status: { state: "ready", available: true, metadata_available: true, last_checked_at: "", detail: "" },
      warnings: [],
    };
    vi.spyOn(tracking, "load").mockResolvedValue();
    const preview = vi.spyOn(tracking, "preview").mockImplementation(async () => {
      tracking.plan = {
        plan_id: "plan-radarr",
        source_hash: "source-radarr",
        rendered_hash: "rendered-radarr",
        target_id: item.target_id,
        service_key: item.service_key,
        stack: item.stack,
        service: item.service,
        image: item.image,
        current_regex: item.tracking_regex,
        proposed_regex: item.suggested_regex,
        compose_diff: "--- current Compose\n+++ proposed Compose\n+wud.tag.include=^v\\d+(?:\\.\\d+)+$",
        will_recreate: true,
        can_apply: true,
        issues: [],
      };
    });
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();

    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    await flushPromises();
    expect(wrapper.text()).toContain("1 Compose services");
    expect(wrapper.text()).toContain("1 frozen filters");
    expect(wrapper.text()).toContain("1 confirmed not seen by WUD");

    await router.push({ name: "containers", query: { search: "radarr" } });
    await flushPromises();
    expect(wrapper.get('input[aria-label="Search tracked containers"]').element).toHaveProperty("value", "radarr");
    await router.push({ name: "containers" });
    await flushPromises();
    expect(wrapper.get('input[aria-label="Search tracked containers"]').element).toHaveProperty("value", "");

    await wrapper.get('button[aria-label="Inspect media/radarr"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[aria-label="Selected container details"]').text()).toContain("radarr");
    expect(wrapper.get(".tracked-source").text()).toContain("/stacks/media/docker-compose.yml");
    expect(wrapper.text()).toContain("All history");
    expect(wrapper.get("summary").text()).toBe("Manage service");
    expect(wrapper.get("details").text()).toContain("Policy");
    expect(wrapper.text()).toContain("Fix tracking");
    expect(wrapper.text()).toContain("What this filter would match");
    expect(wrapper.text()).toContain("new major version");
    expect(wrapper.get('.tracked-pattern-details').attributes("open")).toBeUndefined();
    expect(wrapper.get('.tracked-pattern-details summary').text()).toBe("More examples and tag tests");
    await wrapper.get('.tracked-pattern-details summary').trigger("click");
    expect(wrapper.get('.tracked-examples').text()).toContain("latest");
    expect(wrapper.text()).toContain("not fetched from the registry");

    const sample = wrapper.get('input[aria-label="Tag to test against proposed filter"]');
    await sample.setValue("v1.37");
    expect(wrapper.text()).toContain("This tag matches the proposed filter.");
    await sample.setValue("v1.37-rc1");
    expect(wrapper.text()).toContain("This tag does not match the proposed filter.");

    tracking.inventory.items[0] = {
      ...item,
      wud_match_state: "watching",
      wud_update_available: true,
      wud: {
        id: "wud-radarr", name: "radarr", display_name: "Radarr", status: "running",
        watcher: "local", local_tag: "v1.36.2", local_digest: "",
        remote_tag: "v1.37", remote_digest: "", update_kind: "tag",
        semver_diff: "minor", link: "", error: "", platform: "",
        platform_os: "", platform_architecture: "", platform_variant: "",
      },
    };
    await flushPromises();
    expect(wrapper.text()).toContain("WUD-observed candidate v1.37: Matches");

    tracking.inventory.items[0] = {
      ...tracking.inventory.items[0]!,
      wud: { ...tracking.inventory.items[0]!.wud!, remote_tag: "invalid/tag" },
    };
    await flushPromises();
    expect(wrapper.text()).toContain("WUD-observed candidate invalid/tag: Cannot test");

    const regexEditor = wrapper.get('input[aria-label="Proposed WUD tag regex"]');
    await regexEditor.setValue("^other$");
    expect(wrapper.text()).toContain("must match the installed tag");
    expect(wrapper.findAll("button").find((button) => button.text().includes("Preview repair"))?.attributes("disabled")).toBeDefined();
    await regexEditor.setValue(item.suggested_regex);

    const previewButton = wrapper.findAll("button").find((button) => button.text().includes("Preview repair"));
    expect(previewButton?.attributes("data-button-type")).toBe("primary");
    await previewButton?.trigger("click");
    await flushPromises();
    expect(preview).toHaveBeenCalledWith(item.target_id, item.suggested_regex);
    expect(previewButton?.attributes("data-button-type")).toBe("default");
    expect(wrapper.get('[aria-label="Tracking repair preview"]').text()).toContain("Review the change");
    expect(wrapper.get('[aria-label="Tracking repair preview"]').text()).toContain("Compose label only");
    expect(wrapper.get('[aria-label="Tracking repair preview"]').text()).toContain("will recreate radarr");

    tracking.inventory.items[0] = {
      ...item,
      image: "repo/radarr:latest",
      current_tag: "latest",
      tracking_regex: "^latest$",
      tracking_health: "exact-tag",
      tracking_detail: "The filter matches this tag only.",
      suggested_regex: "",
    };
    await flushPromises();
    expect(wrapper.text()).toContain("Review tracking");
    expect(wrapper.text()).toContain("Matches only the tag “latest”");
    expect(wrapper.text()).toContain("This is already the current filter; there is no label change to preview.");
    expect(wrapper.text()).not.toContain("This pattern cannot be safely tested here.");
  });

  it("does not present WUD-dependent zeros as confirmed when inventory is unavailable", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true });
    const tracking = useTrackingStore();
    tracking.inventory = {
      status: "ready",
      count: 1,
      items: [{ ...item, wud_match_state: "unknown" }],
      wud_status: { state: "unavailable", available: false, metadata_available: false, last_checked_at: "", detail: "WUD API unavailable" },
      warnings: [],
    };
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();

    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    await flushPromises();

    expect(wrapper.get('[aria-label="Inventory summary"]').text()).toContain("WUD inventory unavailable. Check Doctor");
    expect(wrapper.get('[aria-label="Inventory summary"]').text()).not.toContain("not seen by WUD");
    expect(wrapper.get('[aria-label="Inventory summary"]').text()).not.toContain("WUD candidates");
    expect(wrapper.get("tbody").text()).toContain("UnknownWUD inventory unavailable.");

    await wrapper.get('button[aria-label="Inspect media/radarr"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[aria-label="Selected container details"]').text()).toContain("WUD inventory unavailable. Check Doctor");
  });

  it("keeps the unknown-status filter consistent with its counter and excludes confirmed untracked services", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const tracking = useTrackingStore();
    const states: TrackedContainerItem["wud_match_state"][] = ["untracked", "unknown", "ambiguous", "watching", "watching"];
    tracking.inventory = {
      status: "ready", count: states.length, warnings: [],
      wud_status: { state: "ready", available: true, metadata_available: true, last_checked_at: "", detail: "" },
      items: states.map((state, index) => ({ ...item, target_id: `target-${index}`, service: `app-${index}`, wud_match_state: state, wud_update_available: index === 4 ? false : null })),
    };
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();
    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    await flushPromises();
    expect(wrapper.text()).toContain("3 WUD status unknown");
    await wrapper.get('select[aria-label="Tracking filter"]').setValue("unknown");
    expect(wrapper.findAll(".tracked-table tbody tr")).toHaveLength(3);
    expect(wrapper.get(".tracked-table").text()).not.toContain("app-0");
    expect(wrapper.get(".tracked-table").text()).not.toContain("app-4");
  });

  it("shows a still-running repair as progress without an error alert", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true });
    const tracking = useTrackingStore();
    tracking.job = applyJobResponse({ status: "running" });
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();

    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    await flushPromises();

    expect(wrapper.get('[data-alert-type="info"]').text()).toContain("Tracking repair is still running.");
    expect(wrapper.get('[data-alert-type="info"]').text()).toContain("Job job-test. You can refresh later.");
    expect(wrapper.find('[data-alert-type="error"]').exists()).toBe(false);
  });

  it("keeps repair progress beside the action and moves focus after apply", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true, mutations_enabled: true });
    const tracking = useTrackingStore();
    tracking.inventory = { status: "ready", count: 1, items: [item], wud_status: null, warnings: [] };
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();
    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    document.body.append(wrapper.element);
    await flushPromises();

    await wrapper.get('button[aria-label="Inspect media/radarr"]').trigger("click");
    tracking.plan = repairPlan;
    await flushPromises();
    await wrapper.get('input[type="checkbox"]').setValue(true);
    let finishApply!: () => void;
    const applyFinished = new Promise<void>((resolve) => { finishApply = resolve; });
    vi.spyOn(tracking, "apply").mockImplementation(async () => {
      tracking.plan = null;
      tracking.applying = true;
      tracking.job = applyJobResponse({ status: "running" });
      await applyFinished;
      tracking.job = applyJobResponse({ status: "success" });
      tracking.applying = false;
    });

    await wrapper.findAll("button").find((button) => button.text().includes("Apply tracking repair"))?.trigger("click");
    await flushPromises();
    const status = wrapper.get('[aria-label="Tracking repair status"]');
    expect(document.activeElement).toBe(status.element);
    expect(status.element.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(status.text()).toContain("still running");
    expect(wrapper.find('[aria-label="Tracking repair preview"]').exists()).toBe(false);

    finishApply();
    await flushPromises();
    expect(status.text()).toContain("Tracking repaired for radarr");
    tracking.error = "Inventory unavailable";
    await flushPromises();
    expect(status.text()).not.toContain("Inventory unavailable");
    expect(status.find('[data-alert-type="error"]').exists()).toBe(false);
    expect(wrapper.get('[data-alert-type="error"]').text()).toContain("Inventory unavailable");
    tracking.error = "";
    tracking.job = applyJobResponse({ status: "failure", error: "Compose failed" });
    await flushPromises();
    expect(status.text()).toContain("Compose failed");
    expect(wrapper.findAll('[data-alert-type="error"]')).toHaveLength(1);
    wrapper.unmount();
  });

  it("does not show an older recovered job as the new repair result", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true, mutations_enabled: true });
    const tracking = useTrackingStore();
    tracking.inventory = { status: "ready", count: 1, items: [item], wud_status: null, warnings: [] };
    tracking.rememberedJobId = "older-job";
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();
    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    await flushPromises();

    await wrapper.get('button[aria-label="Inspect media/radarr"]').trigger("click");
    tracking.plan = repairPlan;
    await flushPromises();
    await wrapper.get('input[type="checkbox"]').setValue(true);
    vi.spyOn(tracking, "apply").mockImplementation(async () => {
      tracking.plan = null;
      tracking.applying = true;
      await Promise.resolve();
      tracking.job = applyJobResponse({ job_id: "older-job", status: "success" });
      tracking.applyError = "Could not start tracking repair: Rejected";
      tracking.applying = false;
    });

    await wrapper.findAll("button").find((button) => button.text().includes("Apply tracking repair"))?.trigger("click");
    await flushPromises();
    const status = wrapper.get('[aria-label="Tracking repair status"]');
    expect(status.text()).toContain("Could not start tracking repair: Rejected");
    expect(status.text()).not.toContain("Tracking repaired");
    expect(wrapper.get('[data-alert-type="success"]').text()).toContain("Earlier tracking repair job older-job completed.");
  });

  it("returns focus and scroll to the originating Inspect button on close", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useAuthStore().session = authSession({ authenticated: true });
    const tracking = useTrackingStore();
    tracking.inventory = { status: "ready", count: 1, items: [item], wud_status: null, warnings: [] };
    vi.spyOn(tracking, "load").mockResolvedValue();
    const router = createWudRouter(createMemoryHistory());
    await router.push({ name: "containers" });
    await router.isReady();

    const wrapper = mountWithApp(TrackedContainersView, { pinia, router });
    document.body.append(wrapper.element);
    await flushPromises();
    const inspect = wrapper.get('button[aria-label="Inspect media/radarr"]');
    await inspect.trigger("click");
    await flushPromises();
    expect(wrapper.get('[aria-label="Selected container details"]').exists()).toBe(true);

    await wrapper.get('button[aria-label="Close container details"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[aria-label="Selected container details"]').exists()).toBe(false);
    expect(document.activeElement).toBe(inspect.element);
    expect(inspect.element.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    wrapper.unmount();
  });
});
