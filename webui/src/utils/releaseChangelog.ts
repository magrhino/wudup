import {
  findChangelogRawUrl,
  parseGitHubReleaseUrl,
} from "./releaseChangelogGithub";
import { extractChangelogSection } from "./releaseChangelogMarkdown";

export {
  findChangelogRawUrl,
  parseGitHubReleaseUrl,
  releaseChangelogKey,
  type GitHubReleaseRef,
} from "./releaseChangelogGithub";
export { extractChangelogSection } from "./releaseChangelogMarkdown";

export type ReleaseChangelogStatus =
  | "idle"
  | "loading"
  | "ready"
  | "unavailable"
  | "error";

export type ReleaseChangelogState = {
  status: ReleaseChangelogStatus;
  body: string;
  sourceUrl: string;
  error: string;
};

export type ReleaseChangelogResult =
  | {
      status: "ready";
      body: string;
      sourceUrl: string;
    }
  | {
      status: "unavailable";
      error: string;
    };

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type FetchReleaseChangelogOptions = {
  fetch?: FetchLike;
  maxBytes?: number;
  timeoutMs?: number;
};

const DEFAULT_TIMEOUT_MS = 6000;
const DEFAULT_MAX_BYTES = 512 * 1024;

export const IDLE_RELEASE_CHANGELOG: ReleaseChangelogState = {
  status: "idle",
  body: "",
  sourceUrl: "",
  error: "",
};

export async function fetchReleaseChangelog(
  releaseUrl: string,
  releaseTag: string,
  options: FetchReleaseChangelogOptions = {},
): Promise<ReleaseChangelogResult> {
  const release = parseGitHubReleaseUrl(releaseUrl);
  if (release === null) {
    return {
      status: "unavailable",
      error: "Only public GitHub release links support changelog extraction.",
    };
  }

  const fetchImpl = options.fetch ?? fetch;
  const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const apiTag = encodeURIComponent(release.tag);
  const releaseJson = await fetchTextWithLimit(
    `https://api.github.com/repos/${release.owner}/${release.repo}/releases/tags/${apiTag}`,
    {
      accept: "application/vnd.github+json",
      fetchImpl,
      maxBytes,
      resource: "GitHub release",
      timeoutMs,
    },
  );
  const releaseBody = releaseBodyFromJson(releaseJson);
  const changelogUrl = findChangelogRawUrl(releaseBody, release);
  if (!changelogUrl) {
    return {
      status: "unavailable",
      error: "This release does not link to a changelog. Open the GitHub release for notes.",
    };
  }

  const changelogMarkdown = await fetchTextWithLimit(changelogUrl, {
    accept: "text/plain",
    fetchImpl,
    maxBytes,
    resource: "changelog",
    timeoutMs,
  });
  const tag = releaseTag.trim() || release.tag;
  const section = extractChangelogSection(changelogMarkdown, tag);
  if (!section) {
    return {
      status: "unavailable",
      error: `No changelog notes found for ${tag}. Open the GitHub release for details.`,
    };
  }
  return {
    status: "ready",
    body: section,
    sourceUrl: changelogUrl,
  };
}

function releaseBodyFromJson(raw: string): string {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error("GitHub release response was not valid JSON.");
  }
  if (typeof value !== "object" || value === null || !("body" in value)) {
    return "";
  }
  const body = (value as { body?: unknown }).body;
  return typeof body === "string" ? body : "";
}

async function fetchTextWithLimit(
  url: string,
  options: {
    accept: string;
    fetchImpl: FetchLike;
    maxBytes: number;
    resource: string;
    timeoutMs: number;
  },
): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs);
  try {
    // Call standalone: native fetch rejects the options object as its receiver.
    const { fetchImpl } = options;
    const response = await fetchImpl(url, {
      headers: { Accept: options.accept },
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`Could not fetch ${options.resource} (${response.status}).`);
    }
    const contentLength = Number(response.headers.get("content-length") || "0");
    if (contentLength > options.maxBytes) {
      throw new Error(`${options.resource} response is too large.`);
    }
    const reader = response.body?.getReader();
    if (!reader) {
      const text = await response.text();
      if (new TextEncoder().encode(text).byteLength > options.maxBytes) {
        throw new Error(`${options.resource} response is too large.`);
      }
      return text;
    }
    const chunks: Uint8Array[] = [];
    let bytesRead = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      bytesRead += value.byteLength;
      if (bytesRead > options.maxBytes) {
        controller.abort();
        throw new Error(`${options.resource} response is too large.`);
      }
      chunks.push(value);
    }
    const body = new Uint8Array(bytesRead);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return new TextDecoder().decode(body);
  } finally {
    clearTimeout(timeout);
  }
}
