import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  webApi,
  type PendingResponse,
  type PendingRescanResponse,
  type SecurityScanJobResponse,
  type SecurityScanInfo,
  type SecurityScansResponse,
} from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import {
  useUpdatesStore,
  SECURITY_SCAN_POLL_INTERVAL_MS,
  SECURITY_SCAN_POLL_MAX_ATTEMPTS,
} from "../src/stores/updates";
import { useRunsStore } from "../src/stores/runs";
import {
  deferred,
  jsonRequestBody,
  jsonResponse,
  mockFetch,
} from "./helpers/storeActions";
import {
  applyJobLogResponse,
  applyJobResponse,
  pendingItem,
  pendingResponse,
  pendingRescanResponse,
  wudApiStatus,
  wudContainerMetadata,
  releaseNoteInfo,
  releaseNotificationResponse,
  releaseNotesResponse,
  securityScanInfo as baseSecurityScanInfo,
  planResponse,
  updateTargetsResponse,
} from "./helpers/fixtures";

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

function mockStaleMetadataReload(
  refreshed: PendingResponse,
  sourceHash: PendingResponse["source_hash"],
) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/pending/metadata") {
      return Promise.resolve(
        jsonResponse({
          status: "stale",
          requires_pending_reload: true,
          source_hash: sourceHash,
          source: refreshed.source,
          wud_api: refreshed.wud_api,
          items: [],
        }),
      );
    }
    if (url === "/api/v1/pending") {
      return Promise.resolve(jsonResponse(refreshed));
    }
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mockPendingRescanFetch(overrides: Partial<PendingRescanResponse> = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/pending/rescan") {
      return Promise.resolve(jsonResponse(pendingRescanResponse(overrides)));
    }
    if (url === "/api/v1/pending") {
      return Promise.resolve(jsonResponse(pendingResponse()));
    }
    if (
      url === "/api/v1/release-notes" ||
      url === "/api/v1/release-notes/refresh"
    ) {
      return Promise.resolve(jsonResponse(releaseNotesResponse()));
    }
    if (url === "/api/v1/security-scans") {
      return Promise.resolve(jsonResponse(securityScansResponse([])));
    }
    if (url === "/api/v1/runs") {
      return Promise.resolve(jsonResponse([]));
    }
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function completeSecurityScanInfo(
  overrides: Partial<SecurityScanInfo> = {},
): SecurityScanInfo {
  return baseSecurityScanInfo({
    state: "complete",
    verdict: "findings",
    scanner_version: "test",
    scanner_schema: "2",
    scanned_at: "2026-06-26T00:00:00+00:00",
    severity_counts: { critical: 0, high: 1, medium: 0, low: 0, unknown: 0 },
    fixable_counts: { critical: 0, high: 1, medium: 0, low: 0, unknown: 0 },
    subject: {
      requested_ref: "repo/app:1.0",
      reported_digest: "sha256:test",
      manifest_digest: "sha256:test-child",
      platform: "linux/amd64",
    },
    ...overrides,
  });
}

function securityScansResponse(
  items: SecurityScanInfo[],
  overrides: Partial<SecurityScansResponse> = {},
): SecurityScansResponse {
  return {
    source_file: "/out/images.todo",
    source: {
      configured: "file",
      active: "file",
      label: "Pending file",
      fresh: true,
      degraded: false,
      fallback_reason: "",
      detail: "",
    },
    source_hash: "pending-source-hash",
    scanning_enabled: true,
    scanner: "trivy",
    scan_mode: "registry",
    count: items.length,
    items,
    warnings: [],
    ...overrides,
  };
}

function securityScanJobResponse(
  overrides: Partial<SecurityScanJobResponse> = {},
): SecurityScanJobResponse {
  return {
    job_id: "security-scan-test",
    status: "success",
    total_count: 1,
    completed_count: 1,
    result: securityScansResponse([completeSecurityScanInfo()]),
    error: "",
    ...overrides,
  };
}

describe("updates store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("passes csrf from auth store to plan creation", async () => {
    const fetchMock = mockFetch(planResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-plan");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    await updates.createPlan([1], true, [{ line_no: 1, tag: "1.1" }]);

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(updates.plan?.plan_id).toBe("plan-test");
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-plan");
  });

  it("passes csrf from auth store to pending cleanup", async () => {
    const fetchMock = mockFetch({
      status: "success",
      audit_run_id: 12,
      removed_count: 1,
      removed: [
        {
          line_no: 3,
          raw: "repo/old:latest",
          image: "repo/old:latest",
          reason: "unmatched",
        },
      ],
    });
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-cleanup");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    await updates.cleanupPending("cleanup-test", [
      { line_no: 3, raw: "repo/old:latest" },
    ]);

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(updates.pendingCleanup?.audit_run_id).toBe(12);
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      cleanup_id: "cleanup-test",
      lines: [{ line_no: 3, raw: "repo/old:latest" }],
      confirmation: "remove_unmatched",
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-cleanup");
  });

  it("preserves cleanup success while refreshing pending state when requested", async () => {
    mockFetch(pendingResponse());
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pendingCleanup = {
      status: "success",
      audit_run_id: 12,
      removed_count: 1,
      removed: [
        {
          line_no: 3,
          raw: "repo/old:latest",
          image: "repo/old:latest",
          reason: "unmatched",
        },
      ],
    };

    await updates.loadPending({ preserveCleanup: true });

    expect(updates.pendingCleanup?.audit_run_id).toBe(12);

    await updates.loadPending();

    expect(updates.pendingCleanup).toBeNull();
  });

  it("coalesces pending loads and one fresh trailing reload", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const competing = deferred<Response>();
    let activeRequests = 0;
    let maxActiveRequests = 0;
    const pendingRequest = (request: { promise: Promise<Response> }) => {
      activeRequests += 1;
      maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
      return request.promise.finally(() => {
        activeRequests -= 1;
      });
    };
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => pendingRequest(first))
      .mockImplementationOnce(() => pendingRequest(second))
      .mockImplementationOnce(() => pendingRequest(competing));
    vi.stubGlobal("fetch", fetchMock);
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pendingCleanup = {
      status: "success",
      audit_run_id: 12,
      removed_count: 0,
      removed: [],
    };

    const initial = updates.loadPending({ preserveCleanup: true });
    const joinedDuringHandoff = initial.then(() =>
      updates.loadPending({ preserveCleanup: true }),
    );
    const joined = updates.loadPending({ preserveCleanup: true });
    const trailing = updates.loadPending({
      preserveCleanup: true,
      freshAfterCurrent: true,
    });
    const joinedTrailing = updates.loadPending({
      preserveCleanup: true,
      freshAfterCurrent: true,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(updates.loading).toBe(true);

    first.resolve(
      jsonResponse({ ...pendingResponse(), source_hash: "first-generation" }),
    );
    await initial;
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(activeRequests).toBe(1);
    second.resolve(
      jsonResponse({ ...pendingResponse(), source_hash: "second-generation" }),
    );
    competing.resolve(
      jsonResponse({ ...pendingResponse(), source_hash: "competing-generation" }),
    );
    await Promise.all([joined, trailing, joinedTrailing, joinedDuringHandoff]);

    expect(maxActiveRequests).toBe(1);
    expect(updates.pending?.source_hash).toBe("second-generation");
    expect(updates.pendingCleanup?.audit_run_id).toBe(12);
    expect(updates.loading).toBe(false);
  });

  it("refreshes pending WUD metadata in place and clears release-note display", async () => {
    const oldMetadata = wudContainerMetadata({ remote_tag: "1.1" });
    const newMetadata = wudContainerMetadata({ remote_tag: "1.2" });
    const groupedItem = pendingItem({
      line_no: 1,
      raw: "repo/app:1.0",
      wud_metadata: oldMetadata,
    });
    const unmatchedItem = pendingItem({
      line_no: 2,
      raw: "repo/old:1.0",
      image: "repo/old:1.0",
      key: "repo/old",
      repo: "repo/old",
      source_id: "file:2",
      wud_metadata: oldMetadata,
    });
    const untrackedItem = pendingItem({
      line_no: 3,
      raw: "repo/untracked:1.0",
      image: "repo/untracked:1.0",
      key: "repo/untracked",
      repo: "repo/untracked",
      source_id: "file:3",
      wud_metadata: null,
    });
    const pending = {
      ...pendingResponse([groupedItem, unmatchedItem, untrackedItem]),
      wud_api: wudApiStatus({ last_checked_at: "old-check" }),
      grouping: {
        status: "ready" as const,
        groups: [
          {
            name: "media",
            directory: "/docker/media",
            compose_file: "docker-compose.yml",
            project_directory: "/docker/media",
            services_label: "app",
            services: ["app"],
            line_numbers: [1],
            items: [
              {
                ...groupedItem,
                resolved_image: groupedItem.image,
                target_image: `${groupedItem.repo}:${groupedItem.desired_tag}`,
                compose_images: [groupedItem.image],
                services: ["app"],
                action: "tag-update",
                diagnostic: null,
              },
            ],
          },
        ],
        unmatched: [
          {
            ...unmatchedItem,
            resolved_image: unmatchedItem.image,
            target_image: unmatchedItem.image,
            compose_images: [],
            services: [],
            action: "recreate_stack",
            diagnostic: null,
          },
        ],
        warnings: [],
      },
    };
    const plan = planResponse();
    const removalPlan = {
      removal_id: "removal-test",
      source_file: pending.source_file,
      can_remove: true,
      selected_line_numbers: [2],
      lines: [
        {
          line_no: 2,
          raw: "repo/old:1.0",
          image: "repo/old:1.0",
          desired_tag: "",
          digest: "",
        },
      ],
    };
    const rescan = pendingRescanResponse();
    const notes = releaseNotesResponse();
    const notification = releaseNotificationResponse();
    const scans = securityScansResponse([completeSecurityScanInfo()]);
    const fetchMock = mockFetch({
      status: "ready",
      requires_pending_reload: false,
      source_hash: pending.source_hash,
      source: {
        ...pending.source,
        degraded: true,
        fresh: false,
        detail: "1 unrelated WUD observation is unresolved.",
      },
      wud_api: wudApiStatus({ last_checked_at: "new-check" }),
      items: [
        {
          line_no: 1,
          raw: "repo/app:1.0",
          source_id: "file:1",
          wud_metadata: newMetadata,
          metadata_status: "retained",
        },
        {
          line_no: 2,
          raw: "repo/old:1.0",
          source_id: "file:2",
          wud_metadata: null,
        },
        {
          line_no: 3,
          raw: "repo/untracked:1.0",
          source_id: "file:3",
          wud_metadata: null,
        },
      ],
    });
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pending;
    updates.plan = plan;
    updates.pendingRemovalPlan = removalPlan;
    updates.pendingRescan = rescan;
    updates.releaseNotes = notes;
    updates.releaseNotesError = "stale notes";
    updates.releaseNotification = notification;
    updates.releaseNotificationError = "stale preview";
    updates.securityScans = scans;

    await updates.refreshPendingMetadata();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      source_hash: pending.source_hash,
      lines: [
        { line_no: 1, raw: "repo/app:1.0", source_id: "file:1" },
        { line_no: 2, raw: "repo/old:1.0", source_id: "file:2" },
        { line_no: 3, raw: "repo/untracked:1.0", source_id: "file:3" },
      ],
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-metadata");
    expect(updates.pending?.items[0].wud_metadata?.remote_tag).toBe("1.2");
    expect(updates.pending?.items[0].metadata_status).toBe("retained");
    expect(updates.pending?.items[1].wud_metadata).toBeNull();
    expect(updates.pending?.items[2].wud_metadata).toBeNull();
    expect(
      updates.pending?.grouping.groups[0].items[0].wud_metadata?.remote_tag,
    ).toBe("1.2");
    expect(
      updates.pending?.grouping.groups[0].items[0].metadata_status,
    ).toBe("retained");
    expect(updates.pending?.grouping.unmatched[0].wud_metadata).toBeNull();
    expect(updates.pendingWudMetadataCheckedAt).toBe("new-check");
    expect(updates.pending?.source).toMatchObject({
      degraded: true,
      fresh: false,
      detail: "1 unrelated WUD observation is unresolved.",
    });
    expect(updates.plan).toBeNull();
    expect(updates.error).toBe(
      "Selected update metadata changed. Review the warnings and preview the plan again.",
    );
    expect(updates.pendingRemovalPlan).toEqual(removalPlan);
    expect(updates.pendingRescan).toEqual(rescan);
    expect(updates.releaseNotes).toBeNull();
    expect(updates.releaseNotesError).toBe("");
    expect(updates.releaseNotification).toBeNull();
    expect(updates.releaseNotificationError).toBe("");
    expect(updates.securityScans).toEqual(scans);
  });

  it("keeps release-note display when pending WUD metadata is unchanged", async () => {
    const metadata = wudContainerMetadata({ remote_tag: "1.1" });
    const item = pendingItem({ wud_metadata: metadata });
    const pending = {
      ...pendingResponse([item]),
      wud_api: wudApiStatus({ last_checked_at: "old-check" }),
    };
    const notes = releaseNotesResponse();
    const notification = releaseNotificationResponse();
    const fetchMock = mockFetch({
      status: "ready",
      requires_pending_reload: false,
      source_hash: pending.source_hash,
      source: pending.source,
      wud_api: wudApiStatus({ last_checked_at: "new-check" }),
      items: [
        {
          line_no: item.line_no,
          raw: item.raw,
          source_id: item.source_id,
          wud_metadata: metadata,
        },
      ],
    });
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pending;
    updates.releaseNotes = notes;
    updates.releaseNotification = notification;

    await updates.refreshPendingMetadata();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(updates.pendingWudMetadataCheckedAt).toBe("new-check");
    expect(updates.releaseNotes).toEqual(notes);
    expect(updates.releaseNotification).toEqual(notification);
  });

  it("invalidates a fresh-subset plan when another selected line recovers", async () => {
    const fresh = pendingItem({ line_no: 1, metadata_status: "fresh" });
    const retained = pendingItem({
      line_no: 2,
      raw: "repo/worker:1.0",
      image: "repo/worker:1.0",
      key: "repo/worker",
      repo: "repo/worker",
      source_id: "api:worker",
      metadata_status: "retained",
    });
    const current = pendingResponse([fresh, retained]);
    const fetchMock = mockFetch({
      status: "ready",
      requires_pending_reload: false,
      source_hash: current.source_hash,
      source: current.source,
      wud_api: wudApiStatus({ last_checked_at: "new-check" }),
      items: [
        {
          line_no: fresh.line_no,
          raw: fresh.raw,
          source_id: fresh.source_id,
          wud_metadata: fresh.wud_metadata,
          metadata_status: "fresh",
        },
        {
          line_no: retained.line_no,
          raw: retained.raw,
          source_id: retained.source_id,
          wud_metadata: retained.wud_metadata,
          metadata_status: "fresh",
        },
      ],
    });
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = current;
    updates.plan = planResponse({ selected_line_numbers: [1] });

    await updates.refreshPendingMetadata([1, 2]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(updates.pending?.items[1].metadata_status).toBe("fresh");
    expect(updates.plan).toBeNull();
    expect(updates.error).toBe(
      "Selected update metadata changed. Review the warnings and preview the plan again.",
    );
  });

  it("rejects metadata refresh failures without changing main loading state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "metadata failed" }, 503)),
    );
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse();
    updates.error = "keep me";
    updates.loading = false;

    await expect(updates.refreshPendingMetadata()).rejects.toMatchObject({
      message: "metadata failed",
    });

    expect(updates.loading).toBe(false);
    expect(updates.error).toBe("keep me");
  });

  it("reloads recovered entries when WUD returns with the same source hash", async () => {
    const sourceHash = "same-source-hash";
    const recovered = pendingItem({
      metadata_status: "recovered",
      wud_metadata: null,
    });
    const current = {
      ...pendingResponse([recovered]),
      source_hash: sourceHash,
      source: {
        configured: "auto" as const,
        active: "file" as const,
        label: "Pending file",
        fresh: false,
        degraded: true,
        fallback_reason: "WUD API unavailable",
        detail: "WUD API unavailable",
      },
    };
    const refreshed = {
      ...pendingResponse([pendingItem({ metadata_status: "fresh" })]),
      source_hash: sourceHash,
      source: {
        ...current.source,
        active: "api" as const,
        label: "WUD API",
        fresh: true,
        degraded: false,
        fallback_reason: "",
        detail: "",
      },
      wud_api: wudApiStatus({ last_checked_at: "2026-01-02T00:01:00+00:00" }),
    };
    const fetchMock = mockStaleMetadataReload(refreshed, sourceHash);
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = current;
    updates.plan = planResponse();
    updates.pendingRescan = pendingRescanResponse();
    updates.releaseNotes = releaseNotesResponse();
    updates.releaseNotification = releaseNotificationResponse();
    updates.pendingCleanup = {
      status: "success",
      audit_run_id: 12,
      removed_count: 0,
      removed: [],
    };

    await updates.refreshPendingMetadata();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      source_hash: sourceHash,
      lines: [
        {
          line_no: recovered.line_no,
          raw: recovered.raw,
          source_id: recovered.source_id,
        },
      ],
    });
    expect(updates.pending?.source_hash).toBe(sourceHash);
    expect(updates.pending?.source.active).toBe("api");
    expect(updates.pending?.items[0].metadata_status).toBe("fresh");
    expect(updates.pendingWudMetadataCheckedAt).toBe("2026-01-02T00:01:00+00:00");
    expect(updates.pendingCleanup?.audit_run_id).toBe(12);
    expect(updates.plan).toBeNull();
    expect(updates.error).toBe(
      "Selected update metadata changed. Review the warnings and preview the plan again.",
    );
    expect(updates.pendingRescan).toBeNull();
    expect(updates.releaseNotes).toBeNull();
    expect(updates.releaseNotification).toBeNull();
  });

  it("preserves an open plan when only an unrelated source identity changes", async () => {
    const selected = pendingItem({
      line_no: 1,
      source_id: "docker.local.app",
    });
    const unrelated = pendingItem({
      line_no: 2,
      raw: "repo/worker:1.0",
      image: "repo/worker:1.0",
      key: "repo/worker",
      repo: "repo/worker",
      source_id: "docker.local.worker-old",
    });
    const current = pendingResponse([selected, unrelated]);
    const refreshed = pendingResponse([
      selected,
      { ...unrelated, source_id: "docker.local.worker-new" },
    ]);
    const fetchMock = mockStaleMetadataReload(refreshed, current.source_hash);
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    const openPlan = planResponse({ selected_line_numbers: [1] });
    updates.pending = current;
    updates.plan = openPlan;

    await updates.refreshPendingMetadata([1]);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(updates.pending?.items[1].source_id).toBe(
      "docker.local.worker-new",
    );
    expect(updates.plan).toEqual(openPlan);
    expect(updates.error).toBe("");
  });

  it("explains plan invalidation when the pending source hash changes", async () => {
    const current = pendingResponse();
    const refreshed = {
      ...pendingResponse(),
      source_hash: "changed-source-hash",
    };
    const fetchMock = mockStaleMetadataReload(refreshed, refreshed.source_hash);
    useConnectionStore();
    useSettingsStore();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-metadata");
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = current;
    updates.plan = planResponse({ selected_line_numbers: [1] });

    await updates.refreshPendingMetadata([1]);

    expect(updates.pending?.source_hash).toBe("changed-source-hash");
    expect(updates.plan).toBeNull();
    expect(updates.error).toBe(
      "Selected update metadata changed. Review the warnings and preview the plan again.",
    );
  });

  it("matches security scans only for the current pending source and line", () => {
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    const item = pendingItem({
      line_no: 1,
      raw: "repo/app:1.0 platform=linux/amd64 sha256=abc",
      image: "repo/app:1.0",
      digest: "abc",
      platform: "linux/amd64",
    });
    const scan = completeSecurityScanInfo();
    updates.pending = pendingResponse([item]);
    updates.securityScans = securityScansResponse([scan]);

    expect(updates.securityScansCurrent).toBe(true);
    expect(updates.currentSecurityScans?.source_hash).toBe("pending-source-hash");
    expect(updates.currentSecurityScanItems).toEqual([scan]);
    expect(updates.securityScanFor(item)).toEqual(scan);

    updates.pending = {
      ...pendingResponse([item]),
      source_hash: "changed-source-hash",
    };
    expect(updates.securityScansCurrent).toBe(false);
    expect(updates.currentSecurityScans).toBeNull();
    expect(updates.currentSecurityScanItems).toEqual([]);
    expect(updates.securityScanFor(item)).toBeNull();

    const changedLine = pendingItem({
      ...item,
      line_no: 2,
      raw: "repo/other:1.0 platform=linux/amd64 sha256=abc",
      image: "repo/other:1.0",
    });
    updates.pending = pendingResponse([changedLine]);
    updates.securityScans = securityScansResponse([scan]);
    expect(updates.currentSecurityScanItems).toEqual([]);
    expect(updates.securityScanFor(changedLine)).toBeNull();
  });

  it("refreshes security scans through a bounded job poll", async () => {
    vi.useFakeTimers();
    const scan = completeSecurityScanInfo();
    const result = securityScansResponse([scan]);
    const queuedJob = securityScanJobResponse({
      status: "queued",
      completed_count: 0,
      result: null,
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/security-scans/refresh") {
        return Promise.resolve(jsonResponse(queuedJob));
      }
      if (url === "/api/v1/security-scans/jobs/security-scan-test") {
        return Promise.resolve(jsonResponse(securityScanJobResponse({ result })));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-security");
    const updates = useUpdatesStore();

    try {
      const refreshPromise = updates.refreshSecurityScans();
      await flushPromises();
      expect(updates.securityScanJob?.status).toBe("queued");

      await vi.advanceTimersByTimeAsync(500);
      await refreshPromise;

      expect(updates.securityScanJob?.status).toBe("success");
      expect(updates.securityScans).toEqual(result);
      expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
        "/api/v1/security-scans/refresh",
        "/api/v1/security-scans/jobs/security-scan-test",
      ]);
      expect(
        ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
          "x-wud-csrf-token",
        ),
      ).toBe("csrf-security");
    } finally {
      vi.useRealTimers();
    }
  });

  it("times out security scan refresh polling instead of polling forever", async () => {
    vi.useFakeTimers();
    const queuedJob = securityScanJobResponse({
      status: "queued",
      completed_count: 0,
      result: null,
    });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (
        url === "/api/v1/security-scans/refresh" ||
        url === "/api/v1/security-scans/jobs/security-scan-test"
      ) {
        return Promise.resolve(jsonResponse(queuedJob));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-security");
    const updates = useUpdatesStore();

    try {
      const refreshPromise = updates.refreshSecurityScans().then(
        () => null,
        (caughtError: unknown) => caughtError,
      );
      await flushPromises();
      await vi.advanceTimersByTimeAsync(
        SECURITY_SCAN_POLL_MAX_ATTEMPTS * SECURITY_SCAN_POLL_INTERVAL_MS,
      );

      const caughtError = await refreshPromise;
      expect(caughtError).toBeInstanceOf(Error);
      expect((caughtError as Error).message).toBe(
        "Security scan refresh timed out",
      );
      expect(updates.securityScansLoading).toBe(false);
      expect(updates.securityScansError).toBe("Security scan refresh timed out");
      expect(
        fetchMock.mock.calls.filter(
          (call) => call[0] === "/api/v1/security-scans/jobs/security-scan-test",
        ),
      ).toHaveLength(SECURITY_SCAN_POLL_MAX_ATTEMPTS);
    } finally {
      vi.useRealTimers();
    }
  });

  it("extends security scan refresh polling for multi-candidate jobs", async () => {
    vi.useFakeTimers();
    const result = securityScansResponse([completeSecurityScanInfo()]);
    const runningJob = securityScanJobResponse({
      status: "running",
      total_count: 2,
      completed_count: 0,
      result: null,
    });
    let jobPolls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/security-scans/refresh") {
        return Promise.resolve(jsonResponse(runningJob));
      }
      if (url === "/api/v1/security-scans/jobs/security-scan-test") {
        jobPolls += 1;
        return Promise.resolve(
          jsonResponse(
            jobPolls > SECURITY_SCAN_POLL_MAX_ATTEMPTS
              ? securityScanJobResponse({ result })
              : runningJob,
          ),
        );
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-security");
    const updates = useUpdatesStore();

    try {
      const refreshPromise = updates.refreshSecurityScans();
      await flushPromises();
      await vi.advanceTimersByTimeAsync(
        (SECURITY_SCAN_POLL_MAX_ATTEMPTS + 1) *
          SECURITY_SCAN_POLL_INTERVAL_MS,
      );
      await refreshPromise;

      expect(updates.securityScans).toEqual(result);
      expect(updates.securityScansError).toBe("");
      expect(jobPolls).toBe(SECURITY_SCAN_POLL_MAX_ATTEMPTS + 1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("rescans pending updates and refreshes dependent state", async () => {
    const fetchMock = mockPendingRescanFetch();
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-rescan");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse([
      pendingItem({ wud_metadata: wudContainerMetadata() }),
    ]);

    const response = await updates.rescanPending("selected", [1]);

    expect(ensureCsrf).toHaveBeenCalledTimes(2);
    expect(response.audit_run_id).toBe(24);
    expect(updates.pendingRescan?.audit_run_id).toBe(24);
    expect(updates.pending?.count).toBe(1);
    expect(updates.securityScans?.source_hash).toBe("pending-source-hash");
    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls.slice(0, 4)).toEqual([
      "/api/v1/pending/rescan",
      "/api/v1/pending",
      "/api/v1/release-notes",
      "/api/v1/security-scans",
    ]);
    expect(new Set(urls.slice(4))).toEqual(
      new Set(["/api/v1/release-notes/refresh", "/api/v1/runs"]),
    );
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "rescan_wud",
      scope: "selected",
      line_numbers: [1],
      lines: [
        {
          line_no: 1,
          raw: "repo/app:1.0 sha256=abc",
          source_id: "file:1",
          source_hash: "pending-source-hash",
          container_id: "docker.local.app",
        },
      ],
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-rescan");
  });

  it("rescans all pending updates without selected lines", async () => {
    const fetchMock = mockPendingRescanFetch();
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-rescan-all");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse([
      pendingItem({ wud_metadata: wudContainerMetadata() }),
    ]);

    const response = await updates.rescanPending("all");

    expect(response.scope).toBe("all");
    expect(updates.pendingRescan?.scope).toBe("all");
    expect(updates.pending?.count).toBe(1);
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "rescan_wud",
      scope: "all",
      line_numbers: [],
      lines: [],
    });
    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls.slice(0, 4)).toEqual([
      "/api/v1/pending/rescan",
      "/api/v1/pending",
      "/api/v1/release-notes",
      "/api/v1/security-scans",
    ]);
    expect(new Set(urls.slice(4))).toEqual(
      new Set(["/api/v1/release-notes/refresh", "/api/v1/runs"]),
    );
  });

  it("rejects selected pending rescans without selected lines before requesting csrf", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf");
    const updates = useUpdatesStore();

    await expect(updates.rescanPending("selected", [])).rejects.toThrow(
      "Select at least one pending update to rescan.",
    );

    expect(ensureCsrf).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(updates.pendingRescan).toBeNull();
    expect(updates.error).toBe("Select at least one pending update to rescan.");
  });

  it("stores blocked pending rescan responses and refreshes dependent state", async () => {
    const fetchMock = mockPendingRescanFetch({
      status: "blocked",
      scope: "selected",
      requested_count: 1,
      watched_count: 0,
      wud_api: wudApiStatus({
        state: "auth_required",
        metadata_available: false,
      }),
    });
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-rescan-blocked");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse([
      pendingItem({ wud_metadata: wudContainerMetadata() }),
    ]);

    const response = await updates.rescanPending("selected", [1]);

    expect(response.status).toBe("blocked");
    expect(updates.pendingRescan?.status).toBe("blocked");
    expect(updates.pending?.count).toBe(1);
    const urls = fetchMock.mock.calls.map((call) => call[0]);
    expect(urls.slice(0, 4)).toEqual([
      "/api/v1/pending/rescan",
      "/api/v1/pending",
      "/api/v1/release-notes",
      "/api/v1/security-scans",
    ]);
    expect(new Set(urls.slice(4))).toEqual(
      new Set(["/api/v1/release-notes/refresh", "/api/v1/runs"]),
    );
  });

  it("retains a successful rescan when the pending reload fails", async () => {
    const rescan = pendingRescanResponse();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/rescan") {
        return Promise.resolve(jsonResponse(rescan));
      }
      if (url === "/api/v1/pending") {
        return Promise.resolve(
          jsonResponse({ detail: "Pending reload failed" }, 503),
        );
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-rescan");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse();

    await expect(updates.rescanPending("selected", [1])).rejects.toThrow(
      "Pending reload failed",
    );

    expect(updates.pendingRescan).toEqual(rescan);
    expect(updates.error).toBe("Pending reload failed");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/v1/pending/rescan",
      "/api/v1/pending",
    ]);
  });

  it("skips dependent refreshes when pending rescan fails", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/rescan") {
        return Promise.resolve(jsonResponse({ detail: "WUD rescan failed" }, 503));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-rescan-error");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.pending = pendingResponse([
      pendingItem({ line_no: 9, source_id: "file:9" }),
    ]);

    await expect(updates.rescanPending("selected", [9])).rejects.toThrow(
      "WUD rescan failed",
    );

    expect(updates.pendingRescan).toBeNull();
    expect(updates.pending?.items[0]?.line_no).toBe(9);
    expect(updates.error).toBe("WUD rescan failed");
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/v1/pending/rescan",
    ]);
  });

  it("loads update targets for management selectors", async () => {
    const fetchMock = mockFetch(updateTargetsResponse());
    const updates = useUpdatesStore();

    await updates.loadUpdateTargets();

    expect(updates.updateTargets?.items[0]?.service_key).toBe("media/app");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/update-targets");
  });

  it("passes csrf from auth store to release-note refresh", async () => {
    const fetchMock = mockFetch(releaseNotesResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-notes");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    await updates.refreshReleaseNotes();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(updates.releaseNotes?.items[0].release_tag).toBe("v2.0.0");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/release-notes/refresh");
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-notes");
  });

  it("remembers active apply jobs and clears terminal jobs", async () => {
    mockFetch(applyJobResponse({ job_id: "job-active", status: "running" }));
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-job");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    await updates.createJob("plan-test", [1], false, []);

    expect(updates.rememberedApplyJobId).toBe("job-active");
    expect(globalThis.sessionStorage.getItem("applyJobId")).toBe("job-active");

    updates.setApplyJobLog(applyJobLogResponse({ job_id: "job-active" }));
    expect(updates.applyJobLog?.content).toContain("docker-update-from-wud-v2");

    updates.setApplyJob(applyJobResponse({ job_id: "job-active", status: "success" }));

    expect(updates.rememberedApplyJobId).toBe("");
    expect(globalThis.sessionStorage.getItem("applyJobId")).toBeNull();
  });

  it("applies plans through the plan apply endpoint", async () => {
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-plan-apply");
    const applyPlan = vi
      .spyOn(webApi, "applyPlan")
      .mockResolvedValue(applyJobResponse({ job_id: "job-plan", status: "running" }));
    const createJob = vi
      .spyOn(webApi, "createJob")
      .mockRejectedValue(new Error("wrong endpoint"));
    const updates = useUpdatesStore();

    const job = await updates.applyPlan("plan-test", [1], false, [], []);

    expect(applyPlan).toHaveBeenCalledWith(
      "plan-test",
      [1],
      false,
      [],
      [],
      "csrf-plan-apply",
      {},
    );
    expect(createJob).not.toHaveBeenCalled();
    expect(job.job_id).toBe("job-plan");
    expect(updates.applyJob?.job_id).toBe("job-plan");
    expect(updates.rememberedApplyJobId).toBe("job-plan");
  });

  it("loads a terminal apply job log from the persisted run log", async () => {
    const fetchMock = mockFetch({
      run_id: 10,
      log_file: "/out/logs/run-10.log",
      exists: true,
      content: "fallback run log\n",
      truncated: false,
      max_bytes: 65_536,
    });
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    const log = await updates.loadApplyJobLogFromRun(
      applyJobResponse({
        job_id: "job-terminal",
        run_id: 10,
        log_file: "/out/logs/job-terminal.log",
      }),
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/runs/10/log?tail_bytes=65536",
      expect.any(Object),
    );
    expect(log).toEqual({
      job_id: "job-terminal",
      log_file: "/out/logs/run-10.log",
      exists: true,
      content: "fallback run log\n",
      truncated: false,
      max_bytes: 65_536,
      error: "",
    });
    expect(updates.applyJobLog?.content).toBe("fallback run log\n");
  });

  it("marks recovery when a remembered apply job is missing", async () => {
    globalThis.sessionStorage.setItem("applyJobId", "job-lost");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "apply job not found" }, 404));
    vi.stubGlobal("fetch", fetchMock);
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    const runs = useRunsStore();

    const job = await updates.loadApplyJob("job-lost", { recoverMissing: true });

    expect(job).toBeNull();
    expect(updates.applyJob).toBeNull();
    expect(updates.applyJobLog).toBeNull();
    expect(updates.applyJobRecoveries).toEqual([{ jobId: "job-lost", runId: null, acknowledged: false }]);
    expect(updates.rememberedApplyJobId).toBe("");
    expect(runs.error).toBe("");
    expect(globalThis.sessionStorage.getItem("applyJobId")).toBeNull();
  });
});

describe("connection store focused coverage", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("loads release notes with independent loading state", async () => {
    const fetchMock = mockFetch(releaseNotesResponse());
    const updates = useUpdatesStore();

    await updates.loadReleaseNotes();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/release-notes");
    expect(updates.releaseNotes?.items[0]?.release_tag).toBe("v2.0.0");
    expect(updates.releaseNotesLoading).toBe(false);
    expect(updates.releaseNotesError).toBe("");
    expect(updates.loading).toBe(false);
  });

  it("previews and sends release notifications with csrf and resend intent", async () => {
    const fetchMock = mockFetch(releaseNotificationResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-release");
    const updates = useUpdatesStore();

    await updates.previewReleaseNotifications({
      line_numbers: [1, 2],
      resend: true,
    });
    await updates.sendReleaseNotifications({ run_id: 14, resend: true });

    expect(ensureCsrf).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/release-notifications/preview",
    );
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-release");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      line_numbers: [1, 2],
      resend: true,
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/release-notifications/send",
    );
    expect(jsonRequestBody(fetchMock.mock.calls[1])).toEqual({
      run_id: 14,
      resend: true,
      confirmation: "send-release-notes",
    });
    expect(updates.releaseNotification?.sendable_count).toBe(1);
    expect(updates.releaseNotificationError).toBe("");
  });

  it("clears stale release notification previews when preview fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "preview failed" }, 503)),
    );
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-release");
    const updates = useUpdatesStore();
    updates.releaseNotification = releaseNotificationResponse({ sendable_count: 2 });

    await expect(
      updates.previewReleaseNotifications({ line_numbers: [1] }),
    ).rejects.toMatchObject({
      message: "preview failed",
    });

    expect(updates.releaseNotification).toBeNull();
    expect(updates.releaseNotificationError).toBe("preview failed");
    expect(updates.releaseNotificationLoading).toBe(false);
  });

  it("surfaces release note errors without changing main loading state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "notes unavailable" }, 503)),
    );
    const updates = useUpdatesStore();

    await expect(updates.loadReleaseNotes()).rejects.toMatchObject({
      message: "notes unavailable",
    });

    expect(updates.releaseNotesError).toBe("notes unavailable");
    expect(updates.releaseNotesLoading).toBe(false);
    expect(updates.loading).toBe(false);
  });

  it("loads release changelogs on demand", async () => {
    const fetchMock = mockReleaseChangelogFetch(
      "- **Breaking**: Live updates use Server-Sent Events instead of WebSockets.",
      true,
    );
    const updates = useUpdatesStore();
    const note = githubReleaseNote();

    await updates.loadReleaseChangelog(note);

    const changelog = updates.releaseChangelogStateFor(note);
    expectReleaseChangelogFetches(fetchMock);
    expect(changelog.status).toBe("ready");
    expect(changelog.body).toContain("Server-Sent Events");
    expect(changelog.body).not.toContain("Older release");
  });

  it("deduplicates concurrent release changelog loads", async () => {
    const fetchMock = mockReleaseChangelogFetch("- Concurrent entry");
    const updates = useUpdatesStore();
    const note = githubReleaseNote();

    await Promise.all([
      updates.loadReleaseChangelog(note),
      updates.loadReleaseChangelog(note),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expectReleaseChangelogFetches(fetchMock);
    expect(updates.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      body: expect.stringContaining("Concurrent entry"),
    });
  });

  it("short-circuits release changelog loads when ready", async () => {
    const fetchMock = mockReleaseChangelogFetch("- Cached entry");
    const updates = useUpdatesStore();
    const note = githubReleaseNote();

    await updates.loadReleaseChangelog(note);
    expect(updates.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      body: expect.stringContaining("Cached entry"),
    });

    await updates.loadReleaseChangelog(note);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps changelog failures scoped to the release row", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValueOnce(new Error("network failed")),
    );
    const updates = useUpdatesStore();
    const note = githubReleaseNote();

    await updates.loadReleaseChangelog(note);

    expect(updates.releaseChangelogStateFor(note)).toMatchObject({
      status: "error",
      error: "Could not load notes. Try again or open the GitHub release.",
    });
    expect(updates.releaseNotesError).toBe("");

    const retryFetch = mockReleaseChangelogFetch("- Notes after retry");
    await updates.loadReleaseChangelog(note);
    expectReleaseChangelogFetches(retryFetch);
    expect(updates.releaseChangelogStateFor(note)).toMatchObject({
      status: "ready",
      error: "",
      body: expect.stringContaining("Notes after retry"),
    });
  });

  it("sets stream errors through the updates store action", () => {
    const updates = useUpdatesStore();

    updates.setError("Job status stream returned invalid data.");

    expect(updates.error).toBe("Job status stream returned invalid data.");
  });
});
