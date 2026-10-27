import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PendingGroupedItem,
  PendingReleaseKey,
  PendingReleaseSkip,
} from "../src/api/client";
import PendingReleaseSkipModal from "../src/components/pending/PendingReleaseSkipModal.vue";
import PendingSnoozedEntriesPanel from "../src/components/pending/PendingSnoozedEntriesPanel.vue";
import PendingStackCard from "../src/components/pending/PendingStackCard.vue";
import PendingUpdateRow from "../src/components/pending/PendingUpdateRow.vue";
import { useSettingsStore } from "../src/stores/settings";
import { useUpdatesStore } from "../src/stores/updates";
import { usePendingReleaseSkip } from "../src/views/pending/usePendingReleaseSkip";
import {
  canSkipRelease,
  releaseSkipCueText,
  releaseSkipReturnText,
} from "../src/views/pending/releaseSkips";
import { safetyCues } from "../src/views/pending/safetyCues";
import {
  activeSnoozedServiceKeys,
  snoozedItemsForGroups,
  stackGroupsWithoutSnoozedItems,
} from "../src/views/pending/snoozeSelection";
import {
  pendingGroupedItem,
  pendingGrouping,
  pendingResponse,
  snooze,
} from "./helpers/fixtures";
import { naiveStubs } from "./helpers/mount";

const versionKey: PendingReleaseKey = {
  basis: "version",
  value: "5.3.0",
  line: "5.3",
  label: "5.3.0",
};

function releaseSkip(overrides: Partial<PendingReleaseSkip> = {}): PendingReleaseSkip {
  return {
    id: 7,
    service_key: "media/qbittorrent",
    basis: "version",
    value: "5.3.0",
    scope: "release",
    label: "5.3.0",
    skipped_tag: "5.3.0-r0-ls412",
    reason: "stalls on magnet links",
    created_at: "2026-10-26T10:00:00+00:00",
    matches: true,
    ...overrides,
  };
}

function qbitItem(overrides: Partial<PendingGroupedItem> = {}): PendingGroupedItem {
  return pendingGroupedItem({
    line_no: 1,
    image: "lscr.io/linuxserver/qbittorrent:5.2.0-r0-ls400",
    repo: "lscr.io/linuxserver/qbittorrent",
    desired_tag: "5.3.0-r0-ls412",
    services: ["qbittorrent"],
    release_key: versionKey,
    ...overrides,
  });
}

describe("release skips", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("routes matched release skips to the snoozed section without snoozing the service", () => {
    const skipped = qbitItem({ release_skip: releaseSkip() });
    const resurfaced = qbitItem({
      line_no: 2,
      services: ["sonarr"],
      release_skip: releaseSkip({ matches: false }),
    });
    const groups = pendingGrouping([skipped, resurfaced]).groups;
    const serviceKeys = activeSnoozedServiceKeys([
      snooze({ kind: "release", service_key: "media/sonarr", snoozed_until: null }),
    ]);

    expect([...serviceKeys]).toEqual([]);
    expect(snoozedItemsForGroups(groups, serviceKeys).map(({ item }) => item.line_no)).toEqual([1]);
    expect(
      stackGroupsWithoutSnoozedItems(groups, serviceKeys).flatMap((group) =>
        group.items.map((item) => item.line_no),
      ),
    ).toEqual([2]);
    expect(canSkipRelease(skipped)).toBe(false);
    expect(canSkipRelease(resurfaced)).toBe(true);
  });

  it("describes when a skipped release returns", () => {
    expect(releaseSkipReturnText(versionKey, "release")).toBe(
      "Returns to Pending when WUD offers a release other than 5.3.0.",
    );
    expect(releaseSkipReturnText(versionKey, "line")).toBe(
      "Returns to Pending when WUD offers a release outside 5.3.x, such as 5.4.",
    );
    expect(
      releaseSkipReturnText(
        { basis: "digest", value: "sha256:abc", line: "", label: "build sha256:abc" },
        "release",
      ),
    ).toBe("Returns to Pending when WUD offers a different build.");
    expect(releaseSkipCueText(releaseSkip({ matches: false }))).toBe(
      'You skipped 5.3.0 on 2026-10-26: "stalls on magnet links". Retest before applying.',
    );
    expect(releaseSkipCueText(releaseSkip({ reason: "" }))).toBe(
      "You skipped 5.3.0 on 2026-10-26. Retest before applying.",
    );
  });

  it("adds a skipped-before cue and ignores release skips as service snoozes", () => {
    const item = qbitItem({ release_skip: releaseSkip({ matches: false }) });
    const pending = pendingResponse([item]);
    pending.grouping = pendingGrouping([item]);

    const cues = safetyCues(item, {
      pending,
      releaseNote: null,
      releaseNotesLoaded: true,
      releaseNotesLoading: false,
      securityScan: null,
      securityScansCurrent: false,
      securityScansEnabled: false,
      securityScansLoaded: false,
      securityScansLoading: false,
      servicePolicies: [],
      snoozes: [{ service_key: "media/qbittorrent", kind: "release" }],
    });

    expect(cues.map((cue) => cue.key)).toContain("skipped-before");
    expect(cues.find((cue) => cue.key === "skipped-before")?.label).toBe(
      "Skipped 5.3.0 before",
    );
    expect(cues.map((cue) => cue.key)).not.toContain("snoozed");
  });

  it("offers the skip action and the retest reminder on the stack summary", async () => {
    const item = qbitItem({ release_skip: releaseSkip({ matches: false }) });
    const group = pendingGrouping([item]).groups[0]!;
    const wrapper = mount(PendingStackCard, {
      global: { stubs: naiveStubs },
      props: {
        group,
        loading: false,
        releaseNoteFor: () => null,
        releaseNoteReason: () => "",
        releaseNoteStatus: () => "Not checked",
        riskCues: () => [],
        securityScanFor: () => null,
        selectedSelectionKeySet: new Set<string>(),
        stackHasSelection: false,
        stackIndeterminate: false,
        stackSelected: false,
        snoozeDisabled: false,
        snoozeDisabledMessage: "",
        tagInputProps: () => ({ "aria-label": "New tag" }),
        tagOverrideValue: () => "",
        updateDisabled: false,
      },
    });

    const reminder =
      'You skipped 5.3.0 on 2026-10-26: "stalls on magnet links". Retest before applying.';
    expect(wrapper.find(".stack-change-preview").text()).toContain(reminder);
    expect(wrapper.find(".stack-details").text()).not.toContain(reminder);
    const holdTrigger = wrapper.get(`button[aria-label="Snooze or skip ${item.image}"]`);
    expect(holdTrigger.attributes("aria-haspopup")).toBe("menu");
    const menuItem = (key: string) => wrapper.get(`[role="menuitem"][data-key="${key}"]`);
    expect(wrapper.findAll('[role="menuitem"]').map((option) => option.text())).toEqual([
      "Snooze for a whileEvery update for this service",
      "Skip 5.3.0Until WUD offers a different release",
    ]);
    await menuItem("skip").trigger("click");
    expect(wrapper.emitted("skipRelease")).toEqual([[group, item]]);
    await menuItem("snooze").trigger("click");
    expect(wrapper.emitted("snoozeItem")).toEqual([[group, item]]);

    await wrapper.setProps({ skipReleaseDisabledReason: "Read-only mode is active." });
    expect(menuItem("skip").attributes("disabled")).toBeDefined();

    await wrapper.setProps({
      snoozeDisabled: true,
      snoozeDisabledMessage: "Read-only mode is active.",
    });
    expect(holdTrigger.attributes("disabled")).toBeDefined();
    expect(holdTrigger.attributes("title")).toBe("Read-only mode is active.");

    await wrapper.setProps({
      group: pendingGrouping([
        qbitItem({
          release_key: { basis: "digest", value: "sha256:abc", line: "", label: "build sha256:abc" },
        }),
      ]).groups[0]!,
    });
    expect(menuItem("skip").text()).toContain("Until WUD offers a different build");

    // Once the release is skipped, only the plain snooze remains.
    await wrapper.setProps({
      group: pendingGrouping([qbitItem({ release_skip: releaseSkip() })]).groups[0]!,
      snoozeDisabled: false,
      snoozeDisabledMessage: "",
    });
    expect(wrapper.find('[role="menuitem"]').exists()).toBe(false);
    expect(wrapper.find(`button[aria-label="Snooze ${item.image}"]`).exists()).toBe(true);
  });

  it("shows the retest reminder on standalone pending rows", () => {
    const wrapper = mount(PendingUpdateRow, {
      props: {
        item: qbitItem({ release_skip: releaseSkip({ matches: false, reason: "" }) }),
        selected: false,
        serviceLabel: "qbittorrent",
        statusLabel: "Stopped",
        statusTagType: "warning",
      },
      global: { stubs: naiveStubs },
    });

    expect(wrapper.text()).toContain("You skipped 5.3.0 on 2026-10-26. Retest before applying.");
  });

  it("confirms the chosen scope and trimmed reason from the skip dialog", async () => {
    const item = qbitItem();
    const wrapper = mount(PendingReleaseSkipModal, {
      props: {
        error: "",
        loading: false,
        show: true,
        target: { group: pendingGrouping([item]).groups[0]!, item },
      },
      global: { stubs: naiveStubs },
    });

    expect(wrapper.text()).toContain("Skip 5.3.0 for media/qbittorrent?");
    expect(wrapper.text()).toContain("lscr.io/linuxserver/qbittorrent:5.3.0-r0-ls412");
    expect(wrapper.text()).toContain("Rebuilds of a skipped release stay skipped.");
    expect(wrapper.text()).toContain("Compose files and WUD labels are not changed.");

    await wrapper.get('input[value="line"]').trigger("change");
    expect(wrapper.text()).toContain("such as 5.4");
    await wrapper.get("input:not([type=radio])").setValue("  stalls on magnet links  ");
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Skip 5.3.x")!
      .trigger("click");

    expect(wrapper.emitted("confirm")).toEqual([["line", "stalls on magnet links"]]);
  });

  it("saves a skip from Pending against the active snooze list and reloads pending", async () => {
    const settings = useSettingsStore();
    const updates = useUpdatesStore();
    settings.snoozeStateFilter = "expired";
    const createReleaseSkip = vi.spyOn(settings, "createReleaseSkip").mockResolvedValue();
    const loadPending = vi.spyOn(updates, "loadPending").mockResolvedValue();
    const item = qbitItem();
    const skip = usePendingReleaseSkip();

    skip.openReleaseSkip({ group: pendingGrouping([item]).groups[0]!, item });
    await skip.confirmReleaseSkip("release", "stalls");

    expect(createReleaseSkip).toHaveBeenCalledWith(
      {
        serviceKeys: ["media/qbittorrent"],
        image: item.image,
        targetTag: item.desired_tag,
        digest: item.digest,
      },
      "release",
      "stalls",
      "active",
    );
    expect(loadPending).toHaveBeenCalled();
    expect(skip.showReleaseSkipModal.value).toBe(false);

    createReleaseSkip.mockRejectedValueOnce(new Error("media/qbittorrent already skips 5.3.0"));
    skip.openReleaseSkip({ group: pendingGrouping([item]).groups[0]!, item });
    await skip.confirmReleaseSkip("release", "");
    expect(skip.releaseSkipError.value).toBe(
      "Could not skip this release: media/qbittorrent already skips 5.3.0",
    );
    expect(skip.showReleaseSkipModal.value).toBe(true);
  });

  it("labels skipped releases in the snoozed entries with their note", () => {
    const skipped = qbitItem({ release_skip: releaseSkip() });
    const snoozed = qbitItem({ line_no: 2, services: ["sonarr"], release_key: null });
    const group = pendingGrouping([skipped, snoozed]).groups[0]!;
    const wrapper = mount(PendingSnoozedEntriesPanel, {
      props: {
        riskCues: () => [],
        securityScanFor: () => null,
        selectedSelectionKeySet: new Set<string>(),
        snoozedCandidates: [],
        snoozedItems: [
          { group, item: skipped },
          { group, item: snoozed },
        ],
        stoppedItems: [],
        tagInputProps: () => ({}),
        tagOverrideValue: () => "",
      },
      global: { stubs: naiveStubs },
    });

    expect(wrapper.text()).toContain("Skipped release");
    expect(wrapper.text()).toContain(
      "Skipped 5.3.0. Waiting for a different release. Reason: stalls on magnet links",
    );
    expect(wrapper.text()).toContain("Snoozed");
  });

  it("closes the skip dialog unless a save is in flight", async () => {
    const settings = useSettingsStore();
    let finishSave: () => void = () => undefined;
    vi.spyOn(settings, "createReleaseSkip").mockImplementation(
      () => new Promise<void>((resolve) => { finishSave = resolve; }),
    );
    vi.spyOn(useUpdatesStore(), "loadPending").mockResolvedValue();
    const item = qbitItem();
    const skip = usePendingReleaseSkip();

    skip.openReleaseSkip({ group: pendingGrouping([item]).groups[0]!, item });
    skip.closeReleaseSkip();
    expect(skip.showReleaseSkipModal.value).toBe(false);

    skip.openReleaseSkip({ group: pendingGrouping([item]).groups[0]!, item });
    const saving = skip.confirmReleaseSkip("release", "");
    skip.closeReleaseSkip();
    await skip.confirmReleaseSkip("release", "");
    expect(skip.releaseSkipSaving.value).toBe(true);
    expect(skip.showReleaseSkipModal.value).toBe(true);
    expect(settings.createReleaseSkip).toHaveBeenCalledTimes(1);
    finishSave();
    await saving;
    expect(skip.showReleaseSkipModal.value).toBe(false);
  });

  it("closes the dialog from cancel and describes digest and line skips", async () => {
    const item = qbitItem({
      desired_tag: "",
      digest: "sha256:0123456789abcdef0123",
      release_key: {
        basis: "digest",
        value: "sha256:0123456789abcdef0123",
        line: "",
        label: "build sha256:0123456789ab",
      },
    });
    const wrapper = mount(PendingReleaseSkipModal, {
      props: {
        error: "",
        loading: false,
        show: true,
        target: { group: pendingGrouping([item]).groups[0]!, item },
      },
      global: { stubs: naiveStubs },
    });

    expect(wrapper.text()).toContain("Skip build sha256:0123456789ab for media/qbittorrent?");
    expect(wrapper.text()).toContain("Returns to Pending when WUD offers a different build.");
    expect(wrapper.find('input[type="radio"]').exists()).toBe(false);
    await wrapper.getComponent(naiveStubs.NModal as never).vm.$emit("update:show", false);
    expect(wrapper.emitted("close")).toHaveLength(1);

    await wrapper.setProps({ target: null, show: false });
    expect(wrapper.text()).not.toContain("Skip release");
  });

  it("hides the line choice when the release has no minor version", () => {
    const item = qbitItem({
      desired_tag: "mainline",
      release_key: { basis: "tag", value: "mainline", line: "", label: "mainline" },
    });
    const wrapper = mount(PendingReleaseSkipModal, {
      props: {
        error: "media/qbittorrent already skips mainline",
        loading: false,
        show: true,
        target: { group: pendingGrouping([item]).groups[0]!, item },
      },
      global: { stubs: naiveStubs },
    });

    expect(wrapper.find('input[type="radio"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("Returns to Pending when WUD offers a release other than mainline.");
    expect(wrapper.text()).not.toContain("Rebuilds of a skipped release");
    expect(wrapper.text()).toContain("media/qbittorrent already skips mainline");
  });
});
