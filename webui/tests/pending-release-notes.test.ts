import { createPinia } from "pinia";
import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import PendingReleaseNotes from "../src/components/pending/PendingReleaseNotes.vue";
import { useUpdatesStore } from "../src/stores/updates";
import { releaseNoteInfo } from "./helpers/fixtures";
import { naiveStubs } from "./helpers/mount";

function setup(overrides = {}, candidateTag = "2.0.0") {
  const pinia = createPinia();
  const updates = useUpdatesStore(pinia);
  const load = vi.spyOn(updates, "loadReleaseChangelog").mockResolvedValue();
  const wrapper = mount(PendingReleaseNotes, {
    props: { releaseNote: releaseNoteInfo(overrides), candidateLabel: "app · acme/app:2.0.0", candidateTag },
    global: { plugins: [pinia], stubs: naiveStubs },
  });
  return { wrapper, updates, load };
}

describe("candidate release panel", () => {
  it("opens release bodies directly, escapes upstream HTML, and filters unsafe source URLs", async () => {
    const body = '<img src=x onerror="alert(1)"> Useful release notes';
    const { wrapper, load } = setup({ body, links: [{ kind: "github_release", label: "Unsafe", url: "javascript:alert(1)" }] });
    expect(wrapper.findAll("a")).toHaveLength(0);
    expect(wrapper.find(".release-panel").exists()).toBe(false);
    await wrapper.find("button").trigger("click");
    expect(wrapper.find("dialog[open][aria-modal=true]").exists()).toBe(true);
    expect(wrapper.find(".release-markdown").text()).toBe("Useful release notes");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.html()).not.toContain("onerror");
    expect(wrapper.text()).toContain("Matched to candidate");
    expect(load).not.toHaveBeenCalled();
    await wrapper.find(".release-panel-heading button").trigger("click");
    expect(wrapper.find(".release-panel").exists()).toBe(false);
  });

  it.each(["latest", "16", "", "3.0.0"])("labels %s as upstream context", async (tag) => {
    const { wrapper } = setup({}, tag);
    await wrapper.find("button").trigger("click");
    expect(wrapper.text()).toContain("not confirmed for this candidate or digest");
    expect(wrapper.text()).not.toContain("Matched to candidate");
  });

  it("keeps source access during loading, errors, empty and ready changelog states", async () => {
    const { wrapper, updates, load } = setup({ body: "" });
    await wrapper.find("button").trigger("click");
    expect(load).toHaveBeenCalledOnce();
    for (const [status, text] of [["loading", "Loading changelog notes"], ["error", "Could not load notes"], ["unavailable", "No changelog found"], ["ready", "Extracted content"]] as const) {
      vi.spyOn(updates, "releaseChangelogStateFor").mockReturnValue({ status, body: status === "ready" ? text : "", error: status === "error" || status === "unavailable" ? text : "", sourceUrl: "https://github.com/acme/app" });
      await wrapper.setProps({ releaseNote: releaseNoteInfo({ body: "", title: status }) });
      expect(wrapper.find(".release-panel").text()).toContain(text);
      expect(wrapper.find('.release-panel a[href="https://github.com/acme/app/releases/tag/v2.0.0"]').exists()).toBe(true);
      expect(wrapper.find(".release-panel details").exists()).toBe(false);
    }
  });

  it("explains unsupported releases in the same location and panel", async () => {
    const { wrapper, load } = setup({ status: "unsupported", body: "", links: [], error: "No supported source for this image." });
    expect(wrapper.find(".candidate-release-actions").text()).toContain("No supported source");
    await wrapper.find("button").trigger("click");
    expect(wrapper.find(".release-panel").text()).toContain("No supported source");
    expect(load).not.toHaveBeenCalled();
  });

  it("renders GitHub release markdown as structured text", async () => {
    const body = [
      "<!-- Release notes generated using configuration in .github/release.yml at v2.10.8 -->",
      "",
      "## What's Changed",
      "### Bug Fixes",
      "* Fix rTorrent seeding rules by @Flaminel in https://github.com/Cleanuparr/Cleanuparr/pull/808",
      "",
      "**Full Changelog**: https://github.com/Cleanuparr/Cleanuparr/compare/v2.10.7...v2.10.8",
    ].join("\n");
    const { wrapper } = setup({ body });
    await wrapper.find("button").trigger("click");
    const notes = wrapper.find(".release-markdown");
    expect(notes.text()).not.toContain("<!--");
    expect(notes.find("h4").text()).toBe("What's Changed");
    expect(notes.find("h5").text()).toBe("Bug Fixes");
    expect(notes.find("li").text()).toContain("Fix rTorrent seeding rules by @Flaminel");
    expect(notes.find('a[href="https://github.com/Cleanuparr/Cleanuparr/pull/808"]').attributes("rel")).toBe("noopener noreferrer");
    expect(notes.find("strong").text()).toBe("Full Changelog");
  });

  it("keeps release-body line breaks but not changelog soft breaks", async () => {
    const { wrapper, updates } = setup({ body: "Fixed X\nFixed Y" });
    await wrapper.find("button").trigger("click");
    expect(wrapper.find(".release-markdown p br").exists()).toBe(true);

    vi.spyOn(updates, "releaseChangelogStateFor").mockReturnValue({ status: "ready", body: "Line A\nLine B", error: "", sourceUrl: "https://github.com/acme/app" });
    await wrapper.setProps({ releaseNote: releaseNoteInfo({ body: "" }) });
    const changelog = wrapper.find(".release-panel section .release-markdown");
    expect(changelog.text()).toBe("Line A\nLine B");
    expect(changelog.find("br").exists()).toBe(false);
  });
});
