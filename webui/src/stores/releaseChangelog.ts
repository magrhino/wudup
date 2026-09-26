// webui/src/stores/releaseChangelog.ts
import { ref } from "vue";
import { defineStore } from "pinia";
import type { ReleaseNoteInfo } from "../api/client";
import {
  fetchReleaseChangelog,
  IDLE_RELEASE_CHANGELOG,
  releaseChangelogKey,
  type ReleaseChangelogState,
} from "../utils/releaseChangelog";

// On-demand GitHub changelog cache for release notes, keyed by release URL.
// It holds no pending-update state, so it lives outside the updates store.
export const useReleaseChangelogStore = defineStore("releaseChangelog", () => {
  const releaseChangelogs = ref<Record<string, ReleaseChangelogState>>({});
  const releaseChangelogRequests = new Map<string, Promise<void>>();

  function releaseChangelogStateFor(
    note: ReleaseNoteInfo | null,
  ): ReleaseChangelogState {
    const key = releaseChangelogKeyFor(note);
    return key
      ? releaseChangelogs.value[key] ?? IDLE_RELEASE_CHANGELOG
      : IDLE_RELEASE_CHANGELOG;
  }

  async function loadReleaseChangelog(note: ReleaseNoteInfo | null): Promise<void> {
    const link = releaseChangelogLinkFor(note);
    if (link === "") {
      return;
    }
    const key = releaseChangelogKey(link);
    if (key === "") {
      return;
    }
    const currentState = releaseChangelogs.value[key];
    if (currentState?.status === "ready") {
      return;
    }
    const pendingRequest = releaseChangelogRequests.get(key);
    if (pendingRequest) {
      await pendingRequest;
      return;
    }
    setReleaseChangelogState(key, {
      status: "loading",
      body: "",
      sourceUrl: "",
      error: "",
    });
    const request = fetchReleaseChangelog(link, note?.release_tag ?? "")
      .then((result) => {
        if (result.status === "ready") {
          setReleaseChangelogState(key, {
            status: "ready",
            body: result.body,
            sourceUrl: result.sourceUrl,
            error: "",
          });
          return;
        }
        setReleaseChangelogState(key, {
          status: "unavailable",
          body: "",
          sourceUrl: "",
          error: result.error,
        });
      })
      .catch(() => {
        setReleaseChangelogState(key, {
          status: "error",
          body: "",
          sourceUrl: "",
          error: "Could not load notes. Try again or open the GitHub release.",
        });
      })
      .finally(() => {
        releaseChangelogRequests.delete(key);
      });
    releaseChangelogRequests.set(key, request);
    await request;
  }

  function setReleaseChangelogState(
    key: string,
    state: ReleaseChangelogState,
  ): void {
    releaseChangelogs.value = {
      ...releaseChangelogs.value,
      [key]: state,
    };
  }

  return {
    releaseChangelogs,
    releaseChangelogStateFor,
    releaseChangelogCanLoad,
    loadReleaseChangelog,
  };
});

function releaseChangelogKeyFor(note: ReleaseNoteInfo | null): string {
  const link = releaseChangelogLinkFor(note);
  return link ? releaseChangelogKey(link) : "";
}

function releaseChangelogCanLoad(note: ReleaseNoteInfo | null): boolean {
  const link = releaseChangelogLinkFor(note);
  return Boolean(link && releaseChangelogKey(link));
}

function releaseChangelogLinkFor(note: ReleaseNoteInfo | null): string {
  return note?.links.find((link) => link.kind === "github_release")?.url ?? "";
}
