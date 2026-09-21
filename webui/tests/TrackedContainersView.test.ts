import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TrackedContainerItem } from "../src/api/client";
import { createWudRouter } from "../src/router";
import { useAuthStore } from "../src/stores/auth";
import { useTrackingStore } from "../src/stores/tracking";
import TrackedContainersView from "../src/views/TrackedContainersView.vue";
import { authSession } from "./helpers/fixtures";
import { mountWithApp } from "./helpers/mount";

const item: TrackedContainerItem = {
  target_id: "target-radarr",
  service_key: "media/radarr",
  stack: "media",
  service: "radarr",
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
    expect(wrapper.text()).toContain("All history");
    expect(wrapper.get("summary").text()).toBe("Manage service");
    expect(wrapper.get("details").text()).toContain("Policy");
    expect(wrapper.text()).toContain("Fix tracking");
    expect(wrapper.text()).toContain("What this filter would match");
    expect(wrapper.text()).toContain("new major version");
    expect(wrapper.text()).toContain("Illustrative excluded tag latest");
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

    await wrapper.findAll("button").find((button) => button.text().includes("Preview repair"))?.trigger("click");
    await flushPromises();
    expect(preview).toHaveBeenCalledWith(item.target_id, item.suggested_regex);
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
