import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useReleaseChangelogStore } from "../src/stores/releaseChangelog";
import { useUpdatesStore } from "../src/stores/updates";
import { jsonResponse } from "./helpers/storeActions";
import { releaseNoteInfo } from "./helpers/fixtures";

const TEST_RELEASE_TAG = "v0.5.0";
const TEST_RELEASE_URL =
  "https://github.com/t-mart/mousehole/releases/tag/v0.5.0";
const TEST_CHANGELOG_URL =
  "https://raw.githubusercontent.com/t-mart/mousehole/master/CHANGELOG.md";
const TEST_CHANGELOG_LINK =
  "[changelog](https://github.com/t-mart/mousehole/blob/master/CHANGELOG.md)";

function githubReleaseNote() {
  return releaseNoteInfo({
    release_tag: TEST_RELEASE_TAG,
    links: [
      {
        label: "GitHub release",
        url: TEST_RELEASE_URL,
        kind: "github_release",
      },
    ],
  });
}

function releaseChangelogMarkdown(entry: string, includeOlder = false): string {
  const lines = [
    "# Changelog",
    "",
    `## [${TEST_RELEASE_TAG}](${TEST_RELEASE_URL}) - 2026-06-20`,
    "",
    entry,
  ];
  if (includeOlder) {
    lines.push(
      "",
      "## [v0.4.0](https://github.com/t-mart/mousehole/releases/tag/v0.4.0) - 2026-06-04",
      "",
      "- Older release",
    );
  }
  return lines.join("\n");
}

function mockReleaseChangelogFetch(entry: string, includeOlder = false) {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse({ body: TEST_CHANGELOG_LINK }))
    .mockResolvedValueOnce(
      new Response(releaseChangelogMarkdown(entry, includeOlder)),
    );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function expectReleaseChangelogFetches(fetchMock: ReturnType<typeof vi.fn>): void {
  expect(fetchMock.mock.calls).toHaveLength(2);
  expect(fetchMock.mock.calls[0][0]).toBe(
    "https://api.github.com/repos/t-mart/mousehole/releases/tags/v0.5.0",
  );
  expect(fetchMock.mock.calls[1][0]).toBe(TEST_CHANGELOG_URL);
}

describe("release changelog store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("loads release changelogs on demand", async () => {
    const fetchMock = mockReleaseChangelogFetch(
      "- **Breaking**: Live updates use Server-Sent Events instead of WebSockets.",
      true,
    );
    const releaseChangelog = useReleaseChangelogStore();
    const note = githubReleaseNote();

    await releaseChangelog.loadReleaseChangelog(note);

    const changelog = releaseChangelog.releaseChangelogStateFor(note);
    expectReleaseChangelogFetches(fetchMock);
    expect(changelog.status).toBe("ready");
    expect(changelog.body).toContain("Server-Sent Events");
    expect(changelog.body).not.toContain("Older release");
  });

  it("deduplicates concurrent release changelog loads", async () => {
    const fetchMock = mockReleaseChangelogFetch("- Concurrent entry");
    const releaseChangelog = useReleaseChangelogStore();
    const note = githubReleaseNote();

    await Promise.all([
      releaseChangelog.loadReleaseChangelog(note),
      releaseChangelog.loadReleaseChangelog(note),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expectReleaseChangelogFetches(fetchMock);
    expect(releaseChangelog.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      body: expect.stringContaining("Concurrent entry"),
    });
  });

  it("short-circuits release changelog loads when ready", async () => {
    const fetchMock = mockReleaseChangelogFetch("- Cached entry");
    const releaseChangelog = useReleaseChangelogStore();
    const note = githubReleaseNote();

    await releaseChangelog.loadReleaseChangelog(note);
    expect(releaseChangelog.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      body: expect.stringContaining("Cached entry"),
    });

    await releaseChangelog.loadReleaseChangelog(note);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps changelog failures scoped to the release row", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValueOnce(new Error("network failed")),
    );
    const releaseChangelog = useReleaseChangelogStore();
    const updates = useUpdatesStore();
    const note = githubReleaseNote();

    await releaseChangelog.loadReleaseChangelog(note);

    expect(releaseChangelog.releaseChangelogStateFor(note)).toMatchObject({
      status: "error",
      error: "Could not load notes. Try again or open the GitHub release.",
    });
    expect(updates.releaseNotesError).toBe("");

    const retryFetch = mockReleaseChangelogFetch("- Notes after retry");
    await releaseChangelog.loadReleaseChangelog(note);
    expectReleaseChangelogFetches(retryFetch);
    expect(releaseChangelog.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      error: "",
      body: expect.stringContaining("Notes after retry"),
    });
  });
});
