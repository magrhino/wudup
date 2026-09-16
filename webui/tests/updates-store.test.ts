import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  webApi,
  type SecurityScanJobResponse,
  type SecurityScanInfo,
  type SecurityScansResponse,
} from "../src/api/client";
import { useAuthStore } from "../src/stores/auth";
import { useConnectionStore } from "../src/stores/connection";
import { useSettingsStore } from "../src/stores/settings";
import {
  useUpdatesStore,
  APPLY_JOB_RECOVERY_MESSAGE,
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
  retagPlanResponse,
  retagPreviewJobResponse,
  retagTarget,
  retagTargetsResponse,
  securityScanInfo as baseSecurityScanInfo,
  planResponse,
  selfUpdateApplyResponse,
  selfUpdatePlanResponse,
  selfUpdatePrepareResponse,
  selfUpdateResponse,
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
      wud_api: wudApiStatus({ last_checked_at: "new-check" }),
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/metadata") {
        return Promise.resolve(
          jsonResponse({
            status: "stale",
            requires_pending_reload: true,
            source_hash: sourceHash,
            source: refreshed.source,
            wud_api: wudApiStatus({ last_checked_at: "new-check" }),
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
    expect(updates.pendingWudMetadataCheckedAt).toBe("new-check");
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/metadata") {
        return Promise.resolve(
          jsonResponse({
            status: "stale",
            requires_pending_reload: true,
            source_hash: current.source_hash,
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/metadata") {
        return Promise.resolve(
          jsonResponse({
            status: "stale",
            requires_pending_reload: true,
            source_hash: refreshed.source_hash,
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/rescan") {
        return Promise.resolve(jsonResponse(pendingRescanResponse()));
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/rescan") {
        return Promise.resolve(jsonResponse(pendingRescanResponse()));
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/pending/rescan") {
        return Promise.resolve(
          jsonResponse(
            pendingRescanResponse({
              status: "blocked",
              scope: "selected",
              requested_count: 1,
              watched_count: 0,
              wud_api: wudApiStatus({
                state: "auth_required",
                metadata_available: false,
              }),
            }),
          ),
        );
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

  it("loads retag targets for read-only review", async () => {
    const response = retagTargetsResponse();
    const fetchMock = mockFetch(response);
    const updates = useUpdatesStore();

    await updates.loadRetagTargets();

    expect(updates.retagTargets?.items[0]?.service_key).toBe("media/app");
    expect(updates.retagTargetTags[response.items[0].target_id]).toBe("1.1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-targets");
  });

  it("bulk selects only retag targets with valid concrete tags", async () => {
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget(),
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:5.21.1",
        image_repo: "repo/radarr",
        current_tag: "5.21.1",
        tracking_tag: "5.21.1",
        tracking_tag_source: "image",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
      retagTarget({
        service_key: "media/bad",
        service: "bad",
        image: "repo/bad:latest",
        image_repo: "repo/bad",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.retagTargetTags = {
      [retagItems[1].target_id]: "5.22.4",
      [retagItems[2].target_id]: "-bad",
    };
    updates.retagChoices = { [retagItems[2].target_id]: "switch-to-concrete" };
    updates.retagPlan = retagPlanResponse();

    updates.setRetagChoicesForItems(
      retagItems,
      "switch-to-concrete",
    );

    expect(updates.retagPlan).toBeNull();
    expect(updates.retagChoices).toMatchObject({
      [retagItems[0].target_id]: "switch-to-concrete",
      [retagItems[1].target_id]: "switch-to-concrete",
      [retagItems[2].target_id]: "keep-current",
    });
    expect(updates.retagChoiceRequests()).toEqual([
      {
        service_key: "media/app",
        target_id: retagItems[0].target_id,
        choice: "switch-to-concrete",
        target_tag: "1.1",
      },
      {
        service_key: "media/bad",
        target_id: retagItems[2].target_id,
        choice: "keep-current",
      },
      {
        service_key: "media/radarr",
        target_id: retagItems[1].target_id,
        choice: "switch-to-concrete",
        target_tag: "5.22.4",
      },
    ]);
  });

  it("approves starts only for selected inactive retag targets", () => {
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget({ service_key: "media/running", service: "running" }),
      retagTarget({
        service_key: "media/stopped",
        service: "stopped",
        runtime_state: "not-running",
      }),
      retagTarget({
        service_key: "media/unknown",
        service: "unknown",
        runtime_state: "unknown",
      }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.resetRetagChoices();
    for (const item of retagItems) {
      updates.setRetagChoice(item.target_id, "switch-to-concrete");
    }

    expect(updates.retagChoiceRequests()).toEqual([
      {
        service_key: "media/running",
        target_id: retagItems[0].target_id,
        choice: "switch-to-concrete",
        target_tag: "1.1",
      },
      {
        service_key: "media/stopped",
        target_id: retagItems[1].target_id,
        choice: "switch-to-concrete",
        allow_start: true,
        target_tag: "1.1",
      },
      {
        service_key: "media/unknown",
        target_id: retagItems[2].target_id,
        choice: "switch-to-concrete",
        allow_start: true,
        target_tag: "1.1",
      },
    ]);
  });

  it("loads cached retag GitHub latest fallback candidates", async () => {
    const fetchMock = mockFetch(retagTargetsResponse([
      retagTarget({
        candidate_source: "github-latest",
        candidate_warning: "GitHub latest fallback will update latest tracking to v1.1.",
        candidate_link_label: "GitHub release",
        candidate_link_url: "https://github.com/acme/app/releases/tag/v1.1",
      }),
    ]));
    const updates = useUpdatesStore();

    await updates.setRetagGithubLatestFallback(true);

    expect(updates.retagGithubLatestFallback).toBe(true);
    expect(updates.retagTargets?.items[0]?.candidate_source).toBe("github-latest");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets?github_latest_fallback=true",
    );
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "true",
    );
  });

  it("refreshes retag GitHub latest fallback candidates explicitly", async () => {
    const fetchMock = mockFetch(retagTargetsResponse([
      retagTarget({
        candidate_source: "github-latest",
        candidate_warning: "GitHub latest fallback will update latest tracking to v1.1.",
        candidate_link_label: "GitHub release",
        candidate_link_url: "https://github.com/acme/app/releases/tag/v1.1",
      }),
    ]));
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();

    await updates.refreshRetagGithubLatest();

    expect(updates.retagGithubLatestFallback).toBe(true);
    expect(updates.retagTargets?.items[0]?.candidate_source).toBe("github-latest");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets/github-latest/refresh",
    );
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "true",
    );
  });

  it("remembers the cached retag fallback preference", async () => {
    globalThis.localStorage.setItem("retagGithubLatestFallback", "true");
    const fetchMock = mockFetch(retagTargetsResponse());
    const updates = useUpdatesStore();

    expect(updates.retagGithubLatestFallback).toBe(true);
    await updates.loadRetagTargets();
    await updates.setRetagGithubLatestFallback(false);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/retag-targets?github_latest_fallback=true",
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/retag-targets");
    expect(globalThis.localStorage.getItem("retagGithubLatestFallback")).toBe(
      "false",
    );
  });

  it("previews retag choices through the updates store", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          retagPreviewJobResponse({
            status: "queued",
            plan: null,
            warnings: [],
            progress: [
              {
                job_id: "retag-preview-test",
                phase: "refresh",
                status: "running",
                message: "Refreshing retag candidates.",
                created_at: "2026-01-02T00:00:00Z",
                stack: "",
                services: [],
                line_numbers: [],
              },
            ],
          }),
        ),
      )
      .mockResolvedValueOnce(jsonResponse(retagPreviewJobResponse()));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget(),
      retagTarget({ service_key: "media/radarr", service: "radarr" }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");

    try {
      const planPromise = updates.createRetagPlan();
      await flushPromises();
      await vi.advanceTimersByTimeAsync(400);
      const plan = await planPromise;

      expect(ensureCsrf).toHaveBeenCalledTimes(1);
      expect(plan.plan_id).toBe("retag-plan-test");
      expect(updates.retagPlan?.selected_count).toBe(1);
      expect(updates.retagPreviewJob?.status).toBe("success");
      expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-plans/preview");
      expect(fetchMock.mock.calls[1][0]).toBe(
        "/api/v1/retag-plans/preview/retag-preview-test",
      );
      expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
        choices: [
          {
            service_key: "media/app",
            target_id: retagItems[0].target_id,
            choice: "switch-to-concrete",
            target_tag: "1.1",
          },
          {
            service_key: "media/radarr",
            target_id: retagItems[1].target_id,
            choice: "keep-current",
          },
        ],
        github_latest_fallback: false,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps duplicate retag service keys separate by target id", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    updates.retagTargets = retagTargetsResponse([
      retagTarget({ target_id: "target-a", service_key: "media/app" }),
      retagTarget({ target_id: "target-b", service_key: "media/app" }),
    ]);
    updates.resetRetagChoices();

    updates.setRetagChoice("target-b", "switch-to-concrete");
    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: "target-a",
          choice: "keep-current",
        },
        {
          service_key: "media/app",
          target_id: "target-b",
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("uses service-key fallback only when the retag target is unique", () => {
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget(),
      retagTarget({ service_key: "media/radarr", service: "radarr" }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.resetRetagChoices();

    updates.setRetagChoice("media/app", "switch-to-concrete");
    updates.setRetagTargetTag("media/app", "1.2");

    expect(updates.retagChoices[retagItems[0].target_id]).toBe(
      "switch-to-concrete",
    );
    expect(updates.retagTargetTags[retagItems[0].target_id]).toBe("1.2");
  });

  it("does not use service-key fallback for duplicate retag targets", () => {
    const updates = useUpdatesStore();
    updates.retagTargets = retagTargetsResponse([
      retagTarget({ target_id: "target-a", service_key: "media/app" }),
      retagTarget({ target_id: "target-b", service_key: "media/app" }),
    ]);
    updates.resetRetagChoices();

    updates.setRetagChoice("media/app", "switch-to-concrete");
    updates.setRetagTargetTag("media/app", "2.0");

    expect(updates.retagChoiceRequests()).toEqual([
      {
        service_key: "media/app",
        target_id: "target-a",
        choice: "keep-current",
      },
      {
        service_key: "media/app",
        target_id: "target-b",
        choice: "keep-current",
      },
    ]);
    expect(updates.retagTargetTags["target-a"]).toBe("1.1");
    expect(updates.retagTargetTags["target-b"]).toBe("1.1");
  });

  it("sends retag fallback state when previewing", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    updates.retagGithubLatestFallback = true;
    const retagItems = [retagTarget()];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");

    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: true,
    });
  });

  it("falls back to keep-current for stale ineligible retag choices", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget({
        proposed_tag: "",
        retag_available: false,
        retag_reason: "missing-provenance",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);

    updates.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");
    expect(updates.retagChoices[retagItems[0].target_id]).toBe("keep-current");

    updates.retagChoices = { [retagItems[0].target_id]: "switch-to-concrete" };
    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "keep-current",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("sends manual retag target tags for fallback rows", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [
      retagTarget({
        service_key: "media/radarr",
        service: "radarr",
        image: "repo/radarr:5.21.1",
        image_repo: "repo/radarr",
        current_tag: "5.21.1",
        tracking_tag: "5.21.1",
        tracking_tag_source: "image",
        proposed_tag: "",
        final_image: "",
        retag_available: false,
        retag_reason: "not-latest-tracking",
        choices: ["keep-current"],
        digest_provenance: null,
      }),
    ];
    updates.retagTargets = retagTargetsResponse(retagItems);

    updates.setRetagTargetTag(retagItems[0].target_id, "5.22.4");
    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/radarr",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "5.22.4",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("uses edited automatch target tags as manual overrides", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [retagTarget()];
    updates.retagTargets = retagTargetsResponse(retagItems);

    updates.setRetagTargetTag(retagItems[0].target_id, "1.2");
    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.2",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("clears blank automatch overrides back to the proposed tag", async () => {
    const fetchMock = mockFetch(retagPreviewJobResponse());
    const auth = useAuthStore();
    vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [retagTarget()];
    updates.retagTargets = retagTargetsResponse(retagItems);

    updates.setRetagChoice(retagItems[0].target_id, "switch-to-concrete");
    updates.setRetagTargetTag(retagItems[0].target_id, "1.2");
    updates.setRetagTargetTag(retagItems[0].target_id, "   ");
    await updates.createRetagPlan();

    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
    });
  });

  it("applies a retag plan as a tracked apply job", async () => {
    const fetchMock = mockFetch(applyJobResponse({ job_id: "retag-job" }));
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-retag");
    const updates = useUpdatesStore();
    const retagItems = [retagTarget()];
    updates.retagTargets = retagTargetsResponse(retagItems);
    updates.retagChoices = { [retagItems[0].target_id]: "switch-to-concrete" };
    updates.retagPlan = retagPlanResponse();

    const job = await updates.applyRetagPlan();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(job.job_id).toBe("retag-job");
    expect(updates.applyJob?.job_id).toBe("retag-job");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/retag-plans/apply");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      plan_id: "retag-plan-test",
      choices: [
        {
          service_key: "media/app",
          target_id: retagItems[0].target_id,
          choice: "switch-to-concrete",
          target_tag: "1.1",
        },
      ],
      github_latest_fallback: false,
      confirmation: "apply-retags",
    });
  });

  it("surfaces retag target loading errors", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "retag targets unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const updates = useUpdatesStore();

    await expect(updates.loadRetagTargets()).rejects.toThrow(
      "retag targets unavailable",
    );

    expect(updates.error).toBe("retag targets unavailable");
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

  it("loads self-update status for the shell banner", async () => {
    const fetchMock = mockFetch(selfUpdateResponse());
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    await updates.loadSelfUpdate();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update");
    expect(updates.selfUpdate?.latest_tag).toBe("v0.25.0");
  });

  it("loads self-update tag prepare plan", async () => {
    const fetchMock = mockFetch(selfUpdatePlanResponse());
    const auth = useAuthStore();
    const ensureCsrf = vi.spyOn(auth, "ensureCsrf").mockResolvedValue("csrf-plan");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();

    const response = await updates.planSelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update/plan");
    expect(updates.selfUpdatePlan?.plan.plan_id).toBe("self-update-plan-test");
    expect(response.external_recreate_required).toBe(true);
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-plan");
  });

  it("passes csrf from auth store to self-update apply", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(selfUpdateApplyResponse()))
      .mockResolvedValueOnce(jsonResponse(selfUpdateResponse()));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.selfUpdate = selfUpdateResponse();

    const response = await updates.applySelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(response.container).toBe("wudup");
    expect(updates.selfUpdateMessage).toBe(
      "Image prepared, but the running container still uses the previous image. Recreate the WUDup container to run the new version.",
    );
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "pull_image",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:latest",
      restart_container: "wudup",
    });
    expect(
      ((fetchMock.mock.calls[0][1] as RequestInit).headers as Headers).get(
        "x-wud-csrf-token",
      ),
    ).toBe("csrf-self-update");
  });

  it("prepares pinned self-update tags from cached plan", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(selfUpdatePrepareResponse()))
      .mockResolvedValueOnce(jsonResponse(selfUpdateResponse({ strategy: "prepare_tag_update" })));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.selfUpdate = selfUpdateResponse({
      strategy: "prepare_tag_update",
      current_image: "ghcr.io/magrhino/wudup:v0.24.2",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      external_recreate_required: true,
    });
    updates.selfUpdatePlan = selfUpdatePlanResponse();

    const response = await updates.applySelfUpdate();

    expect(ensureCsrf).toHaveBeenCalledTimes(1);
    expect(response.status).toBe("tag_prepared");
    expect(updates.selfUpdateMessage).toBe(
      "Tag updated and image pulled. Recreate the WUDup container from outside the WebUI to run the new version. Tagged deployments are recommended for predictable updates.",
    );
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/self-update/prepare");
    expect(jsonRequestBody(fetchMock.mock.calls[0])).toEqual({
      confirmation: "prepare_tag_update",
      plan_id: "self-update-plan-test",
      current_tag: "v0.24.2",
      latest_tag: "v0.25.0",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      restart_container: "wudup",
    });
  });

  it("requires a loaded self-update tag prepare plan before applying", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();
    const ensureCsrf = vi
      .spyOn(auth, "ensureCsrf")
      .mockResolvedValue("csrf-self-update");
    useConnectionStore();
    useSettingsStore();
    const updates = useUpdatesStore();
    useRunsStore();
    updates.selfUpdate = selfUpdateResponse({
      strategy: "prepare_tag_update",
      current_image: "ghcr.io/magrhino/wudup:v0.24.2",
      target_image: "ghcr.io/magrhino/wudup:v0.25.0",
      external_recreate_required: true,
    });

    await expect(updates.applySelfUpdate()).rejects.toThrow(
      "Self-update tag update preview must be loaded before applying",
    );

    expect(ensureCsrf).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(updates.selfUpdateError).toBe(
      "Self-update tag update preview must be loaded before applying",
    );
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
    expect(updates.applyJobRecovery).toBe(APPLY_JOB_RECOVERY_MESSAGE);
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
