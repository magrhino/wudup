import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PendingGroupedItem,
  PendingResponse,
  PendingStackGroup,
} from "../src/api/client";
import {
  coreUpdateTourResponse,
  pendingGroupedItem,
  pendingResponse,
  releaseNoteInfo,
  releaseNotesResponse,
} from "./helpers/fixtures";
import {
  mockPendingLifecycle,
  mountPendingView,
  setupStores,
} from "./helpers/viewSecurity";

function stackGroup(
  name: string,
  items: PendingGroupedItem[],
): PendingStackGroup {
  const services = [...new Set(items.flatMap((item) => item.services))];
  return {
    name,
    directory: `/docker/${name}`,
    compose_file: "docker-compose.yml",
    project_directory: `/docker/${name}`,
    project_name: name,
    services_label: services.join(", "),
    services,
    line_numbers: items.map((item) => item.line_no),
    items,
  };
}

function pendingWithGroups(groups: PendingStackGroup[]): PendingResponse {
  const items = groups.flatMap((group) => group.items);
  return {
    ...pendingResponse(items),
    grouping: {
      status: "ready",
      groups,
      unmatched: [],
      warnings: [],
    },
  };
}

function radarrItem(): PendingGroupedItem {
  return pendingGroupedItem({
    line_no: 1,
    raw: "linuxserver/radarr:4.0 tag=5.0",
    image: "linuxserver/radarr:4.0",
    key: "linuxserver/radarr",
    repo: "linuxserver/radarr",
    current_tag: "4.0",
    desired_tag: "5.0",
    services: ["radarr"],
    action: "tag-update",
  });
}

function postgresItem(): PendingGroupedItem {
  return pendingGroupedItem({
    line_no: 2,
    raw: "postgres:16 tag=17",
    image: "postgres:16",
    key: "postgres",
    repo: "postgres",
    current_tag: "16",
    desired_tag: "17",
    services: ["postgres"],
    action: "tag-update",
  });
}

function cacheDigestItem(): PendingGroupedItem {
  return pendingGroupedItem({
    line_no: 3,
    raw: "repo/cache:latest sha256=feedface1234",
    image: "repo/cache:latest",
    key: "repo/cache",
    repo: "repo/cache",
    current_tag: "latest",
    desired_tag: "",
    digest: "sha256:feedface1234",
    services: ["cache"],
    action: "recreate_service",
  });
}

function mountFilteredPendingView(
  response: PendingResponse,
  mutationsEnabled = false,
) {
  const { pinia, settings, updates } = setupStores(mutationsEnabled);
  updates.pending = response;
  mockPendingLifecycle(settings, updates);
  const wrapper = mountPendingView(pinia);
  return { wrapper, updates };
}

function searchInput(wrapper: ReturnType<typeof mountPendingView>) {
  return wrapper.find('input[aria-label="Search pending updates"]');
}

async function mountDefaultPendingView() {
  const mounted = mountFilteredPendingView(
    pendingWithGroups([
      stackGroup("media", [radarrItem()]),
      stackGroup("data", [postgresItem()]),
    ]),
  );
  await flushPromises();
  return mounted;
}

async function setPendingSearch(
  wrapper: ReturnType<typeof mountPendingView>,
  value: string,
) {
  await searchInput(wrapper).setValue(value);
  await flushPromises();
}

describe("pending view search filter", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setActivePinia(createPinia());
  });

  it("filters grouped pending updates by service and image text", async () => {
    const { wrapper } = await mountDefaultPendingView();

    await setPendingSearch(wrapper, "postgres");

    expect(wrapper.text()).toContain("postgres:16");
    expect(wrapper.text()).toContain("1 visible update matched");
    expect(wrapper.text()).not.toContain("linuxserver/radarr:4.0");
  });

  it("filters grouped pending updates by digest fragment", async () => {
    const wrapper = mountFilteredPendingView(
      pendingWithGroups([
        stackGroup("media", [radarrItem()]),
        stackGroup("cache", [cacheDigestItem()]),
      ]),
    ).wrapper;
    await flushPromises();

    await setPendingSearch(wrapper, "feedface");

    expect(wrapper.text()).toContain("repo/cache:latest");
    expect(wrapper.text()).toContain("1 visible update matched");
    expect(wrapper.text()).not.toContain("linuxserver/radarr:4.0");
  });

  it("finds stopped updates by runtime state", async () => {
    const stopped = pendingGroupedItem({
      line_no: 4,
      image: "repo/worker:1.0",
      repo: "repo/worker",
      services: ["worker"],
      runtime_state: "not-running",
      stopped_services: ["worker"],
    });
    const wrapper = mountFilteredPendingView(
      pendingWithGroups([
        stackGroup("media", [radarrItem()]),
        stackGroup("jarvis", [stopped]),
      ]),
    ).wrapper;
    await flushPromises();

    await setPendingSearch(wrapper, "stopped");

    expect(wrapper.text()).toContain("Stopped or unverified containers");
    expect(wrapper.text()).toContain("repo/worker:1.0");
    expect(wrapper.text()).toContain("1 visible update matched");
    expect(wrapper.text()).not.toContain("linuxserver/radarr:4.0");
  });

  it("keeps stack preview scoped to the full stack after filtering", async () => {
    const { wrapper, updates } = mountFilteredPendingView(
      pendingWithGroups([stackGroup("media", [radarrItem(), postgresItem()])]),
      true,
    );
    const createPlan = vi.spyOn(updates, "createPlan").mockResolvedValue();
    await flushPromises();

    await setPendingSearch(wrapper, "postgres");
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Review media plan"))
      ?.trigger("click");

    expect(createPlan).toHaveBeenCalledWith(
      [1, 2],
      true,
      [],
      [],
      [
        { line_no: 1, selection_id: "selection-1" },
        { line_no: 2, selection_id: "selection-2" },
      ],
    );
  });

  it("shows and clears an empty filtered result state", async () => {
    const { wrapper } = await mountDefaultPendingView();

    await setPendingSearch(wrapper, "not-a-pending-update");

    expect(wrapper.text()).toContain("No pending updates match search");
    expect(wrapper.text()).toContain("not-a-pending-update");
    expect(wrapper.text()).not.toContain("Update queue is clear");

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Clear search"))
      ?.trigger("click");
    await flushPromises();

    expect(wrapper.text()).not.toContain("No pending updates match search");
    expect(wrapper.text()).toContain("linuxserver/radarr:4.0");
    expect(wrapper.text()).toContain("postgres:16");
  });

  it("labels stack counts as matched only while search is active", async () => {
    const { pinia, settings, updates } = setupStores(false);
    updates.pending = pendingWithGroups([
      stackGroup("media", [radarrItem()]),
      stackGroup("data", [postgresItem()]),
    ]);
    settings.coreUpdateTour = coreUpdateTourResponse({
      status: "in_progress",
      step: "pending_select",
    });
    mockPendingLifecycle(settings, updates);
    const wrapper = mountPendingView(pinia);
    await flushPromises();

    expect(wrapper.text()).toContain("2 stacks");
    expect(wrapper.text()).not.toContain("2 stacks matched");

    await setPendingSearch(wrapper, "postgres");

    expect(wrapper.text()).toContain("1 stack matched");
  });

  it("keeps hidden selected rows clear when filtering", async () => {
    const { wrapper, updates } = await mountDefaultPendingView();

    await wrapper.find('input[aria-label="Select stack media"]').setValue(true);
    await setPendingSearch(wrapper, "postgres");

    expect(wrapper.text()).toContain("1 selected");
    expect(wrapper.text()).toContain("1 selected update hidden by search");
    expect(wrapper.text()).toContain(
      "1 selected update hidden by search; included in review.",
    );
    expect(wrapper.text()).toContain("Review selected (");
    expect(wrapper.find(".queue-tools").text()).toContain("These counts reflect the current search.");
    const createPlan = vi.spyOn(updates, "createPlan").mockResolvedValue();
    await wrapper.findAll("button").find((button) => button.text().includes("Review selected (1)"))!.trigger("click");
    expect(createPlan.mock.calls[0]?.[0]).toEqual([1]);
    await wrapper.findAll("button").find((button) => button.text().includes("Clear selection"))!.trigger("click");
    expect(wrapper.find(".batch-action-bar").text()).toContain("Select updates to review");
    expect(wrapper.find(".batch-action-bar").text()).not.toContain("hidden by search");

    expect(wrapper.text()).toContain("postgres:16");
    expect(wrapper.text()).not.toContain("linuxserver/radarr:4.0");
  });

  it("counts a selected shared-line stack as hidden when its peer is visible", async () => {
    const activeItem = pendingGroupedItem({
      line_no: 1,
      selection_id: "selection-active",
      image: "repo/shared:latest",
      repo: "repo/shared",
      services: ["app"],
    });
    const backupItem = pendingGroupedItem({
      line_no: 1,
      selection_id: "selection-backup",
      image: "repo/shared:latest",
      repo: "repo/shared",
      services: ["app"],
    });
    const { wrapper } = mountFilteredPendingView(
      pendingWithGroups([
        stackGroup("active", [activeItem]),
        stackGroup("backup", [backupItem]),
      ]),
    );
    await flushPromises();

    await wrapper.find('input[aria-label="Select stack active"]').setValue(true);
    await setPendingSearch(wrapper, "backup");

    expect(wrapper.text()).toContain("1 selected update hidden by search");
    expect(wrapper.text()).toContain("backup");
    expect(wrapper.text()).not.toContain("Review active plan");
  });

  it("filters by loaded release-note unavailable reasons", async () => {
    const radarr = radarrItem();
    const postgres = postgresItem();
    const { wrapper, updates } = mountFilteredPendingView(
      pendingWithGroups([
        stackGroup("media", [radarr]),
        stackGroup("data", [postgres]),
      ]),
    );
    updates.releaseNotes = releaseNotesResponse([
      releaseNoteInfo({
        line_no: postgres.line_no,
        links: [],
        status: "unsupported",
        error: "no supported GitHub release source found",
      }),
    ]);
    await flushPromises();

    await setPendingSearch(wrapper, "Only GHCR");

    expect(wrapper.text()).toContain("postgres:16");
    expect(wrapper.text()).toContain("Only GHCR and mapped LinuxServer.io images");
    expect(wrapper.text()).not.toContain("linuxserver/radarr:4.0");
  });
});
