import { mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h, type Component, type VNodeChild } from "vue";
import { describe, expect, it, vi } from "vitest";

import type {
  PendingItem,
  SecurityScanFinding,
  SecurityScanInfo,
} from "../src/api/client";
import { DemoApiState } from "../src/api/demo/state";
import PendingStackCard from "../src/components/pending/PendingStackCard.vue";
import PendingEvidenceExplanation from "../src/components/pending/PendingEvidenceExplanation.vue";
import PendingCleanupModal from "../src/components/pending/PendingCleanupModal.vue";
import PendingPlanReviewModal from "../src/components/pending/PendingPlanReviewModal.vue";
import PendingRemovalModal from "../src/components/pending/PendingRemovalModal.vue";
import PendingSecurityScanDetails from "../src/components/pending/PendingSecurityScanDetails.vue";
import {
  digestProvenanceDisplay,
  displayDigest,
} from "../src/utils/digestProvenance";
import {
  securityScanMaintainerReport,
  securityScanSummaryDisplay,
} from "../src/utils/securityScans";
import { safetyCues } from "../src/views/pending/safetyCues";
import { createPendingColumns } from "../src/views/pending/tableColumns";
import {
  groupedItemActionLabel,
  groupedItemActionTagType,
  groupedItemServiceKeys,
  groupedItemTarget,
  itemsBreakingCount,
  pendingSourceFileName,
  releaseNoteReason,
  releaseNoteStatus,
  tagInputProps,
  uniqueSorted,
  uniqueStrings,
} from "../src/views/pending/pendingDisplay";
import {
  filterSnoozedCandidates,
  filterPendingStackGroups,
  normalizePendingSearch,
  pendingItemMatchesSearch,
  type PendingSearchContext,
} from "../src/views/pending/pendingFilter";
import {
  planLineDigestPinLabel,
  planLineDigestUnpinLabel,
} from "../src/views/pending/utils";
import {
  pendingGroupedItem,
  pendingGrouping,
  pendingItem,
  pendingResponse,
  pendingSnoozedCandidate,
  planResponse,
  releaseNoteInfo,
  securityScanInfo,
  servicePolicy,
  snooze,
  wudContainerMetadata,
} from "./helpers/fixtures";
import { mountWithApp, naiveStubs } from "./helpers/mount";
import {
  activeSnoozedServiceKeys,
  runtimeDeferredItemsForGroups,
  snoozedItemsForGroups,
  stackGroupsWithoutRuntimeDeferredItems,
} from "../src/views/pending/snoozeSelection";

type RenderColumn = {
  key?: string;
  render?: (row: PendingItem) => VNodeChild;
};

type DemoSecurityScanBuilder = {
  securityScanInfo: (item: PendingItem, firstExact: boolean) => SecurityScanInfo;
};

function mountPendingModal(
  component: Component,
  props: Record<string, unknown>,
  stubs: Record<string, Component> = {},
): VueWrapper {
  return mount(component, {
    props,
    global: {
      stubs: {
        ...naiveStubs,
        ...stubs,
        CoreUpdateTourPanel: { template: "<div />" },
      },
    },
  });
}

const tagTypeStub: Component = {
  props: {
    type: String,
  },
  setup(props, { slots }) {
    return () =>
      h("span", { "data-tag-type": props.type }, [slots.default?.()]);
  },
};

function securityFinding(
  index: number,
  severity: SecurityScanFinding["severity"] = "high",
  overrides: Partial<SecurityScanFinding> = {},
): SecurityScanFinding {
  const id = String(index).padStart(4, "0");
  return {
    target: "debian:12",
    target_class: "os-pkgs",
    target_type: "debian",
    vulnerability_id: `CVE-2026-${id}`,
    package_name: `package-${id}`,
    installed_version: "1.0.0",
    fixed_version: "1.0.1",
    severity,
    title: `${severity} vulnerability ${id}`,
    primary_url: "",
    ...overrides,
  };
}

function securityScanWithFindings(
  findings: SecurityScanFinding[],
): SecurityScanInfo {
  const severity_counts = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    unknown: 0,
  };
  for (const finding of findings) {
    severity_counts[finding.severity] += 1;
  }
  return securityScanInfo({
    state: "complete",
    verdict: "findings",
    findings,
    severity_counts,
  });
}

function pendingPlanReviewModalProps(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    actionCommand: () => "docker compose pull app",
    applyButtonLabel: "Apply 1 update",
    applyDisabled: false,
    applyPreflight: null,
    applyPreflightAttentionChecks: [],
    applyPreflightCheckDetail: () => "",
    applyPreflightCheckLabel: () => "Ready",
    applyPreflightCheckType: () => "success",
    applyPreflightPassedChecks: [],
    applyPreflightPassedText: "",
    applyReadinessStatusLabel: "",
    applyReadinessStatusType: "success",
    applyReadinessSummary: "",
    applyVisible: false,
    cleanupAvailable: false,
    cleanupButtonLabel: "Remove 0 unmatched entries",
    cleanupDisabled: true,
    cleanupDisabledMessage: "",
    cleanupItems: [],
    cleanupReviewSummary: "",
    digestPinLabelApprovalApproved: () => false,
    digestPinLabelApprovalIssues: [],
    digestPinLabelIssueProposedRegex: () => "",
    tagStreamDecisionIssues: [],
    tagStreamDecisionSelected: () => false,
    tagStreamLabelApprovalApproved: () => false,
    tagStreamLabelApprovalIssues: [],
    issueDetailString: () => "",
    issueHint: () => "",
    issueLabel: () => "",
    issueType: () => "warning",
    loading: false,
    mutationDisabledMessage: "",
    plan: planResponse(),
    planActions: [],
    planAlertType: "info",
    planDigestPinLabelRewrites: [],
    planDigestUnpinUpdates: [],
    releaseNotes: [],
    releaseNotesLoading: false,
    releaseNotesError: "",
    securityScans: [],
    planMetadataWarning: "",
    planStatusLabel: "Ready",
    planLines: [],
    planTagStreamUpdates: [],
    preflightDigestPinNotice: "",
    preflightDigestUnpinNotice: "",
    preflightServiceImpactLabel: "",
    preflightSummary: "1 service ready to update.",
    preflightTagRewriteNotice: "",
    preflightTitle: "Review selected updates",
    show: true,
    staleDiagnosticDetail: () => "",
    staleDiagnosticLabel: () => "",
    visiblePlanIssues: [],
    ...overrides,
  };
}

async function emitModalShowUpdate(wrapper: VueWrapper, value: boolean): Promise<void> {
  const modal = wrapper.getComponent(naiveStubs.NModal as Component);
  await modal.vm.$emit("update:show", value);
}

describe("pending helper modules", () => {
  it("formats digest provenance with tag context and truncated digest detail", () => {
    const digest = "sha256:abcdefghijklmnopqrstuvwxyz0123456789";
    const provenance = {
      source_image: "repo/app:stable",
      resolved_tag: "latest",
      watch_tag: "stable",
      target_digest: digest,
      final_image: `repo/app@${digest}`,
      provenance_source: "plan",
      provenance_confidence: "verified",
    };

    expect(displayDigest(digest)).toBe(
      "sha256:abcdefghijklm...yz0123456789",
    );
    expect(digestProvenanceDisplay(provenance)).toMatchObject({
      primary: "repo/app: stable -> latest",
      digest: "Digest: sha256:abcdefghijklm...yz0123456789",
    });
    expect(digestProvenanceDisplay(provenance)?.title).toContain(
      `Digest: ${digest}`,
    );
    expect(
      planLineDigestPinLabel({
        line_no: 1,
        raw: `repo/app:stable ${digest}`,
        image: "repo/app:stable",
        resolved_image: "repo/app:latest",
        compose_image: "repo/app@sha256:old",
        target_image: `repo/app@${digest}`,
        service: "app",
        digest,
        desired_tag: "",
        action: "digest-pin",
        digest_provenance: provenance,
      }),
    ).toBe(
      "repo/app: stable -> latest (Digest: sha256:abcdefghijklm...yz0123456789)",
    );

    expect(
      planLineDigestUnpinLabel({
        ...planResponse().stacks[0].lines[0],
        action: "digest-unpin",
        compose_image: `repo/app@${digest}`,
        target_image: "repo/app:latest",
      }),
    ).toBe(
      "repo/app@sha256:abcdefghijklmnopqrstuvwxyz0123456789 -> repo/app:latest",
    );
  });

  it("finds matched active snoozes", () => {
    const radarr = pendingGroupedItem({
      line_no: 1,
      services: ["radarr"],
    });
    const groups = pendingGrouping([radarr]).groups;
    const activeKeys = activeSnoozedServiceKeys([
      snooze({ service_key: "media/radarr" }),
      snooze({
        kind: "dependency",
        service_key: "media/sonarr",
        wait_for_service_key: "media/prowlarr",
        snoozed_until: null,
      }),
      snooze({ active: false, service_key: "media/expired" }),
    ]);

    expect([...activeKeys]).toEqual(["media/radarr", "media/sonarr"]);
    expect(
      snoozedItemsForGroups(groups, activeKeys).map(({ item }) => item.line_no),
    ).toEqual([1]);
  });

  it("separates stopped updates from bulk-selectable stack groups", () => {
    const running = pendingGroupedItem({
      line_no: 1,
      services: ["api"],
      running_services: ["api"],
    });
    const stopped = pendingGroupedItem({
      line_no: 2,
      services: ["worker"],
      runtime_state: "not-running",
      running_services: [],
      stopped_services: ["worker"],
    });
    const groups = pendingGrouping([running, stopped]).groups;

    expect(
      runtimeDeferredItemsForGroups(groups).map(({ item }) => item.line_no),
    ).toEqual([2]);
    expect(
      stackGroupsWithoutRuntimeDeferredItems(groups).flatMap((group) =>
        group.items.map((item) => item.line_no),
      ),
    ).toEqual([1]);
  });

  it("builds safety cues from versions, release notes, policies, and snoozes", () => {
    const major = pendingGroupedItem({
      line_no: 1,
      current_tag: "1.2.3",
      desired_tag: "2.0.0",
      services: ["app"],
    });
    const minor = pendingGroupedItem({
      line_no: 2,
      current_tag: "1.2.3",
      desired_tag: "1.3.0",
      services: ["worker"],
    });
    const patch = pendingGroupedItem({
      line_no: 3,
      current_tag: "1.2.3",
      desired_tag: "1.2.4",
      services: ["api"],
    });
    const digestLatest = pendingGroupedItem({
      line_no: 4,
      current_tag: "latest",
      desired_tag: "",
      digest: "sha256:abc",
      action: "recreate_stack",
      services: ["cache"],
    });
    const pending = pendingResponse([major, minor, patch, digestLatest]);
    const note = releaseNoteInfo({
      line_no: 1,
      breaking: true,
      breaking_reasons: ["Major version update."],
      security: {
        outcome: "verified_critical_high",
        severity: "critical",
        reason_code: "verified_exposure",
        reason: "Verified Critical advisory affects 1.2.3 and is patched by 2.0.0.",
        advisory_ids: ["GHSA-AAAA-BBBB-CCCC"],
        lookup_truncated: false,
      },
    });
    const noReleaseNote = releaseNoteInfo({
      line_no: 4,
      status: "unsupported",
      links: [],
      error: "no supported GitHub release source found",
    });

    const majorLabels = safetyCues(major, {
      pending,
      releaseNote: note,
      releaseNotesLoaded: true,
      releaseNotesLoading: false,
      securityScan: null,
      securityScansCurrent: false,
      securityScansEnabled: false,
      securityScansLoaded: false,
      securityScansLoading: false,
      servicePolicies: [servicePolicy({ service_key: "media/app", auto_update: true })],
      snoozes: [snooze({ service_key: "media/app" })],
    }).map((cue) => cue.label);
    expect(majorLabels).toContain("Major bump");
    expect(majorLabels).toContain("Release advisory: critical");
    expect(majorLabels).toContain("Possible breaking");
    expect(majorLabels).toContain("Snoozed");
    expect(majorLabels).toContain("Auto-update");

    const streamChange = pendingItem({
      current_tag: "2.33.5-distroless",
      desired_tag: "2.34.4",
      tag_stream: {
        current_stream: "distroless",
        reported_stream: "default",
      },
    });
    const streamCues = safetyCues(streamChange, {
      pending,
      releaseNote: null,
      releaseNotesLoaded: false,
      releaseNotesLoading: false,
      securityScan: null,
      securityScansCurrent: false,
      securityScansEnabled: false,
      securityScansLoaded: false,
      securityScansLoading: false,
      servicePolicies: [],
      snoozes: [],
    });
    expect(streamCues).toContainEqual({
      key: "possible-stream-change",
      label: "Possible stream change",
      type: "error",
    });

    expect(
      safetyCues(minor, {
        pending,
        releaseNote: null,
        releaseNotesLoaded: false,
        releaseNotesLoading: false,
        securityScan: null,
        securityScansCurrent: false,
        securityScansEnabled: false,
        securityScansLoaded: false,
        securityScansLoading: false,
        servicePolicies: [],
        snoozes: [],
      }).map((cue) => cue.label),
    ).toContain("Minor bump");
    expect(
      safetyCues(patch, {
        pending,
        releaseNote: null,
        releaseNotesLoaded: false,
        releaseNotesLoading: false,
        securityScan: null,
        securityScansCurrent: false,
        securityScansEnabled: false,
        securityScansLoaded: false,
        securityScansLoading: false,
        servicePolicies: [],
        snoozes: [],
      }).map((cue) => cue.label),
    ).toContain("Patch bump");

    const digestLabels = safetyCues(digestLatest, {
      pending,
      releaseNote: noReleaseNote,
      releaseNotesLoaded: true,
      releaseNotesLoading: false,
      securityScan: null,
      securityScansCurrent: false,
      securityScansEnabled: false,
      securityScansLoaded: false,
      securityScansLoading: false,
      servicePolicies: [],
      snoozes: [],
    }).map((cue) => cue.label);
    expect(digestLabels).toContain("Digest-only");
    expect(digestLabels).toContain("Mutable latest");
    expect(digestLabels).toContain("Stack restart");
    expect(digestLabels).toContain("No release notes");
  });

  it("adds candidate security scan cues without implying safety", () => {
    const item = pendingGroupedItem({ line_no: 3, image: "repo/app:1.0" });
    const pending = pendingResponse([item]);
    const securityCuesFor = (
      securityScan: SecurityScanInfo | null,
      overrides: Partial<Parameters<typeof safetyCues>[1]> = {},
    ) =>
      safetyCues(item, {
        pending,
        releaseNote: null,
        releaseNotesLoaded: false,
        releaseNotesLoading: false,
        securityScan,
        securityScansCurrent: true,
        securityScansEnabled: true,
        securityScansLoaded: true,
        securityScansLoading: false,
        servicePolicies: [],
        snoozes: [],
        ...overrides,
      }).filter((cue) => cue.key.startsWith("security-"));
    const completeFindingsScan = securityScanInfo({
      line_no: item.line_no,
      state: "complete",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 1,
        medium: 0,
        low: 0,
        unknown: 0,
      },
    });
    const completeLowerSeverityScan = securityScanInfo({
      line_no: item.line_no,
      state: "complete",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 0,
        medium: 2,
        low: 1,
        unknown: 1,
      },
    });
    const mixedComparisonScan = securityScanInfo({
      line_no: item.line_no,
      state: "complete",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 1,
        medium: 0,
        low: 0,
        unknown: 0,
      },
      comparison: {
        status: "mixed",
        current_subject: {
          requested_ref: "repo/app:1.0",
          reported_digest: "sha256:installed",
          manifest_digest: "sha256:installed-child",
          platform: "linux/amd64",
        },
        fixed_findings: [securityFinding(1, "medium")],
        remaining_findings: [securityFinding(2, "high")],
        introduced_findings: [securityFinding(3, "high")],
        message: "1 finding fixed, 1 remains, and 1 introduced.",
      },
    });
    const noneReportedScan = securityScanInfo({
      line_no: item.line_no,
      state: "complete",
      verdict: "none_reported",
    });
    const notScannedScan = securityScanInfo({
      line_no: item.line_no,
      state: "not_scanned",
      verdict: "unknown",
    });
    const staleScan = securityScanInfo({
      line_no: item.line_no,
      state: "stale",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 0,
        medium: 1,
        low: 0,
        unknown: 0,
      },
    });
    const disabledScan = securityScanInfo({
      line_no: item.line_no,
      state: "disabled",
      verdict: "unknown",
    });

    expect(securityCuesFor(completeFindingsScan)).toContainEqual(
      expect.objectContaining({
        key: "security-findings",
        label: "Candidate scan: Findings",
        type: "error",
      }),
    );
    expect(securityCuesFor(completeLowerSeverityScan)).toContainEqual(
      expect.objectContaining({
        key: "security-findings",
        label: "Candidate scan: Findings",
        type: "warning",
      }),
    );
    expect(securityCuesFor(mixedComparisonScan)).toContainEqual(
      expect.objectContaining({
        key: "security-mixed",
        label: "Candidate scan: Findings changed",
        type: "error",
      }),
    );
    expect(securityCuesFor(noneReportedScan)).toContainEqual(
      expect.objectContaining({
        key: "security-none-reported",
        label: "Candidate scan: None reported",
        type: "success",
      }),
    );
    expect(securityCuesFor(noneReportedScan).map((cue) => cue.label)).not.toContain(
      "Safe",
    );
    expect(securityCuesFor(notScannedScan)).toContainEqual(
      expect.objectContaining({
        key: "security-not-scanned",
        label: "Candidate scan: Not scanned",
        type: "default",
      }),
    );
    expect(securityCuesFor(staleScan)).toContainEqual(
      expect.objectContaining({
        key: "security-stale",
        label: "Candidate scan: stale",
        type: "warning",
      }),
    );
    expect(
      securityCuesFor(null, { securityScansCurrent: false }),
    ).toContainEqual(
      expect.objectContaining({
        key: "security-stale",
        label: "Candidate scan: stale",
        type: "warning",
      }),
    );
    expect(securityCuesFor(null)).toContainEqual(
      expect.objectContaining({
        key: "security-unknown",
        label: "Candidate scan: unavailable",
        type: "warning",
      }),
    );
    expect(
      securityCuesFor(null, {
        releaseNote: releaseNoteInfo({
          security: {
            outcome: "verified_critical_high",
            severity: "high",
            reason_code: "verified_exposure",
            reason: "Structured advisory evidence matches this update.",
            advisory_ids: ["GHSA-AAAA-BBBB-CCCC"],
            lookup_truncated: false,
          },
        }),
      }),
    ).toContainEqual(
      expect.objectContaining({
        key: "security-unknown",
        label: "Candidate scan: unavailable",
        type: "warning",
      }),
    );
    expect(securityCuesFor(disabledScan)).toEqual([]);
    expect(
      securityCuesFor(completeFindingsScan, { securityScansLoaded: false }),
    ).toEqual([]);
    expect(
      securityCuesFor(completeFindingsScan, { securityScansEnabled: false }),
    ).toEqual([]);
  });

  it("renders candidate security scan vulnerability rows", () => {
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanInfo({
        state: "complete",
        verdict: "findings",
        scanned_at: "2026-06-26T00:00:00+00:00",
        scanner_version: "0.71.2",
        severity_counts: {
          critical: 0,
          high: 1,
          medium: 0,
          low: 0,
          unknown: 0,
        },
        fixable_counts: {
          critical: 0,
          high: 1,
          medium: 0,
          low: 0,
          unknown: 0,
        },
        findings: [
          {
            target: "debian:12",
            target_class: "os-pkgs",
            target_type: "debian",
            vulnerability_id: "CVE-2026-0001",
            package_name: "openssl",
            installed_version: "1.0.0",
            fixed_version: "1.0.1",
            severity: "high",
            title: "demo vulnerability",
            primary_url: "https://avd.aquasec.com/nvd/cve-2026-0001",
          },
        ],
      }),
    });

    expect(wrapper.text()).toContain("1 finding");
    expect(wrapper.text()).toContain("1 High");
    expect(wrapper.text()).toContain("1 fixable finding");
    expect(wrapper.text()).toContain("1 raw occurrence");
    expect(wrapper.text()).toContain("1 unique advisory");
    expect(wrapper.text()).toContain(
      "Scanner trivy; version 0.71.2; schema Unknown",
    );
    expect(wrapper.text()).toContain("DB revision Unknown; updated Unknown");
    expect(wrapper.text()).toContain("debian:12");
    expect(wrapper.text()).toContain("os-pkgs");
    expect(wrapper.text()).toContain("debian");
    expect(wrapper.text()).toContain("CVE-2026-0001");
    expect(wrapper.text()).toContain("openssl");
    expect(wrapper.text()).toContain("1.0.0");
    expect(wrapper.text()).toContain("1.0.1");
    expect(wrapper.find("a").attributes("href")).toBe(
      "https://avd.aquasec.com/nvd/cve-2026-0001",
    );
  });

  it("renders unavailable when cached unique advisory counts are unknown", () => {
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanInfo({
        state: "complete",
        verdict: "findings",
        advisory_counts_known: false,
        severity_counts: {
          critical: 0,
          high: 2,
          medium: 0,
          low: 0,
          unknown: 0,
        },
      }),
    });

    expect(wrapper.text()).toContain("Unique advisories unavailable");
    expect(wrapper.text()).not.toContain("0 unique advisories");
  });

  it("renders security scan comparison deltas and report copy action", () => {
    const fixed = securityFinding(1, "medium", {
      package_name: "libssl",
    });
    const remaining = securityFinding(2, "high", {
      package_name: "openssl",
    });
    const introduced = securityFinding(3, "critical", {
      package_name: "curl",
    });
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanInfo({
        state: "complete",
        verdict: "findings",
        scanner_version: "0.71.2",
        scanner_schema: "trivy-json",
        db_revision: "trivy-db-2026-06-26",
        db_updated_at: "2026-06-26T12:00:00Z",
        subject: {
          requested_ref: "repo/app:2.0",
          reported_digest: "sha256:candidate",
          index_digest: "sha256:candidate",
          manifest_digest: "sha256:candidate-child",
          immutable_ref: "repo/app@sha256:candidate-child",
          platform: "linux/amd64",
        },
        severity_counts: {
          critical: 1,
          high: 1,
          medium: 0,
          low: 0,
          unknown: 0,
        },
        findings: [remaining, introduced],
        comparison: {
          status: "mixed",
          current_subject: {
            requested_ref: "repo/app:1.0",
            reported_digest: "sha256:installed",
            index_digest: "sha256:installed",
            manifest_digest: "sha256:installed-child",
            immutable_ref: "repo/app@sha256:installed-child",
            platform: "linux/amd64",
          },
          fixed_findings: [fixed],
          remaining_findings: [remaining],
          introduced_findings: [introduced],
          message: "1 finding fixed, 1 remains, and 1 introduced.",
        },
      }),
    });

    const text = wrapper.text();
    expect(text).toContain("Update comparison");
    expect(text).toContain("Mixed");
    expect(text).toContain("1 finding fixed, 1 remains, and 1 introduced.");
    expect(text).toContain("1 fixed finding");
    expect(text).toContain("1 remaining finding");
    expect(text).toContain("1 introduced finding");
    expect(text).toContain("Installed");
    expect(text).toContain("Candidate");
    expect(text).toContain("linux/amd64");
    expect(text).toContain("DB revision trivy-db-2026-06-26");
    expect(text).toContain("updated 2026-06-26T12:00:00Z");
    expect(text).toContain("Copy report");
    expect(text).toContain("CVE-2026-0002");
    expect(text).toContain("CVE-2026-0003");
  });

  it("maps security scan comparison status to the comparison tag type", async () => {
    const wrapper = mountPendingModal(
      PendingSecurityScanDetails,
      {
        scan: securityScanInfo(),
      },
      {
        NTag: tagTypeStub,
        Tag: tagTypeStub,
      },
    );
    const cases: Array<{
      expected: string;
      label: string;
      remaining: SecurityScanFinding[];
      status: SecurityScanInfo["comparison"]["status"];
    }> = [
      {
        expected: "success",
        label: "Improved",
        remaining: [],
        status: "improved",
      },
      { expected: "error", label: "Worse", remaining: [], status: "worse" },
      { expected: "warning", label: "Mixed", remaining: [], status: "mixed" },
      {
        expected: "warning",
        label: "Unchanged",
        remaining: [securityFinding(1)],
        status: "unchanged",
      },
      {
        expected: "success",
        label: "Unchanged",
        remaining: [],
        status: "unchanged",
      },
      { expected: "default", label: "Unknown", remaining: [], status: "unknown" },
    ];

    for (const item of cases) {
      await wrapper.setProps({
        scan: securityScanInfo({
          state: "complete",
          verdict: "none_reported",
          comparison: {
            status: item.status,
            current_subject: {
              requested_ref: "repo/app:1.0",
              reported_digest: "sha256:installed",
              manifest_digest: "sha256:installed-child",
              platform: "linux/amd64",
            },
            fixed_findings: [],
            remaining_findings: item.remaining,
            introduced_findings: [],
            message: "Comparison is available.",
          },
        }),
      });

      const tag = wrapper
        .findAll("[data-tag-type]")
        .find((candidate) => candidate.text() === item.label);
      expect(tag?.attributes("data-tag-type")).toBe(item.expected);
    }
  });

  it("keeps demo comparison empty for unscanned security rows", () => {
    const scan = (new DemoApiState() as unknown as DemoSecurityScanBuilder).securityScanInfo(
      pendingItem({
        digest: "sha256:candidate",
        platform: "linux/amd64",
        platform_os: "linux",
        platform_architecture: "amd64",
        platform_variant: "",
        wud_metadata: wudContainerMetadata({
          local_digest: "sha256:installed",
          platform: "linux/amd64",
          platform_os: "linux",
          platform_architecture: "amd64",
          platform_variant: "",
        }),
      }),
      false,
    );

    expect(scan.state).toBe("not_scanned");
    expect(scan.comparison.status).toBe("unknown");
    expect(scan.comparison.message).toBe("");
    expect(scan.comparison.remaining_findings).toEqual([]);
  });

  it("includes provenance before comparing completed demo scans", () => {
    const scan = (new DemoApiState() as unknown as DemoSecurityScanBuilder).securityScanInfo(
      pendingItem({
        digest: "sha256:candidate",
        platform: "linux/amd64",
        wud_metadata: wudContainerMetadata({
          local_digest: "sha256:installed",
          platform: "linux/amd64",
        }),
      }),
      true,
    );

    expect(scan.state).toBe("complete");
    expect(scan.scanner_version).toBe("demo");
    expect(scan.scanner_schema).toBe("trivy-json");
    expect(scan.db_revision).toBe("demo");
    expect(scan.db_updated_at).toBe("2026-05-28T12:00:00+00:00");
    expect(scan.comparison.status).toBe("unchanged");
    expect(scan.comparison.remaining_findings).toHaveLength(1);
  });

  it("filters candidate security scan findings by present severity categories", async () => {
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanWithFindings([
        securityFinding(1, "critical"),
        securityFinding(2, "high"),
        securityFinding(3, "high"),
        securityFinding(4, "low"),
      ]),
    });

    const options = wrapper
      .find('select[aria-label="Security finding category filter"]')
      .findAll("option")
      .map((option) => option.text());

    expect(options).toEqual([
      "All categories (4)",
      "Critical (1)",
      "High (2)",
      "Low (1)",
    ]);
    expect(options).not.toContain("Medium (0)");
    expect(options).not.toContain("Unknown (0)");

    await wrapper
      .find('select[aria-label="Security finding category filter"]')
      .setValue("high");

    expect(wrapper.text()).toContain("CVE-2026-0002");
    expect(wrapper.text()).toContain("CVE-2026-0003");
    expect(wrapper.text()).not.toContain("CVE-2026-0001");
    expect(wrapper.text()).not.toContain("CVE-2026-0004");
  });

  it("clears stale security scan severity filters after scan refresh", async () => {
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanWithFindings([
        securityFinding(1, "critical"),
        securityFinding(2, "high"),
      ]),
    });

    await wrapper
      .find('select[aria-label="Security finding category filter"]')
      .setValue("critical");
    await wrapper.setProps({
      scan: securityScanWithFindings([securityFinding(3, "high")]),
    });

    expect(wrapper.text()).toContain("CVE-2026-0003");
    expect(wrapper.text()).not.toContain("CVE-2026-0001");
  });

  it("paginates candidate security scan findings", async () => {
    const wrapper = mountPendingModal(PendingSecurityScanDetails, {
      scan: securityScanWithFindings(
        Array.from({ length: 12 }, (_, index) =>
          securityFinding(index + 1, "high", { package_name: "openssl" }),
        ),
      ),
    });

    expect(wrapper.text()).toContain("Showing 1-10 of 12 findings");
    expect(wrapper.text()).toContain("Showing 10 of 12 occurrences");
    expect(wrapper.text()).toContain("Showing 10 of 12 advisory occurrences");
    expect(wrapper.text()).toContain("CVE-2026-0001");
    expect(wrapper.text()).toContain("CVE-2026-0010");
    expect(wrapper.text()).not.toContain("CVE-2026-0011");

    const pageTwo = wrapper.findAll("button").find((button) => button.text() === "2");
    expect(pageTwo).toBeTruthy();
    await pageTwo?.trigger("click");

    expect(wrapper.text()).toContain("Showing 11-12 of 12 findings");
    expect(wrapper.text()).toContain("Showing 2 of 12 occurrences");
    expect(wrapper.text()).toContain("Showing 2 of 12 advisory occurrences");
    expect(wrapper.text()).not.toContain("CVE-2026-0001");
    expect(wrapper.text()).toContain("CVE-2026-0011");
    expect(wrapper.text()).toContain("CVE-2026-0012");

    await wrapper.setProps({
      scan: securityScanWithFindings(
        Array.from({ length: 11 }, (_, index) => securityFinding(index + 101)),
      ),
    });

    expect(wrapper.text()).toContain("Showing 1-10 of 11 findings");
    expect(wrapper.text()).toContain("CVE-2026-0101");
    expect(wrapper.text()).toContain("CVE-2026-0110");
    expect(wrapper.text()).not.toContain("CVE-2026-0111");

    const pageOne = wrapper.findAll("button").find((button) => button.text() === "1");
    expect(pageOne?.attributes("disabled")).toBeDefined();
  });

  it("summarizes candidate security scans with shared severity semantics", () => {
    const highImpactScan = securityScanInfo({
      line_no: 1,
      state: "complete",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 1,
        medium: 0,
        low: 0,
        unknown: 0,
      },
    });
    const lowerSeverityScan = securityScanInfo({
      line_no: 2,
      state: "complete",
      verdict: "findings",
      severity_counts: {
        critical: 0,
        high: 0,
        medium: 1,
        low: 0,
        unknown: 0,
      },
    });
    const cleanScan = securityScanInfo({
      line_no: 3,
      state: "complete",
      verdict: "none_reported",
    });
    const staleScan = securityScanInfo({
      line_no: 4,
      state: "stale",
      verdict: "findings",
    });

    expect(
      securityScanSummaryDisplay({
        securityScans: null,
        securityScansCurrent: false,
        items: [],
      }),
    ).toEqual({ label: "Security scans loading", type: "default" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: false },
        securityScansCurrent: false,
        items: [],
      }),
    ).toEqual({ label: "Security scans off", type: "default" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: false,
        items: [highImpactScan],
      }),
    ).toEqual({ label: "Security scans stale", type: "warning" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [highImpactScan],
      }),
    ).toEqual({ label: "1 candidate with findings", type: "error" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [staleScan],
      }),
    ).toEqual({ label: "Security scans stale", type: "warning" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [lowerSeverityScan],
      }),
    ).toEqual({ label: "1 candidate with findings", type: "warning" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [highImpactScan, lowerSeverityScan],
      }),
    ).toEqual({ label: "2 candidates with findings", type: "error" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [cleanScan],
      }),
    ).toEqual({ label: "1 candidate scanned", type: "info" });
    expect(
      securityScanSummaryDisplay({
        securityScans: { scanning_enabled: true },
        securityScansCurrent: true,
        items: [],
      }),
    ).toEqual({ label: "No candidate scans yet", type: "info" });
  });

  it("includes fixed findings in the maintainer report", () => {
    const report = securityScanMaintainerReport(
      securityScanInfo({
        state: "complete",
        verdict: "none_reported",
        scanner_version: "0.71.2",
        scanner_schema: "trivy-json",
        db_revision: "trivy-db-2026-06-26",
        db_updated_at: "2026-06-26T12:00:00Z",
        comparison: {
          status: "improved",
          current_subject: {
            requested_ref: "repo/app:1.0",
            reported_digest: "sha256:installed",
            manifest_digest: "sha256:installed-child",
            platform: "linux/amd64",
          },
          fixed_findings: [
            securityFinding(1, "high", {
              package_name: "openssl",
            }),
          ],
          remaining_findings: [],
          introduced_findings: [],
          message: "Candidate removes 1 reported finding(s).",
        },
      }),
    );

    expect(report).toContain("Fixed installed findings (1):");
    expect(report).toContain(
      "database revision trivy-db-2026-06-26; updated 2026-06-26T12:00:00Z",
    );
    expect(report).toContain("CVE-2026-0001 in openssl");
    expect(report).not.toContain("Remaining candidate findings");
    expect(report).not.toContain("Introduced candidate findings");
  });

  it("formats pending queue display helpers without store dependencies", () => {
    const item = pendingGroupedItem({
      image: "repo/app:1.0",
      target_image: "repo/app:2.0",
      resolved_image: "repo/app:2.0",
      services: ["app"],
      action: "tag-update",
    });
    const breakingNote = releaseNoteInfo({
      line_no: item.line_no,
      breaking: true,
      breaking_reasons: ["Major version update."],
    });

    expect(uniqueSorted([3, 1, 3, 2])).toEqual([1, 2, 3]);
    expect(uniqueStrings(["worker", "", "api", "worker"])).toEqual([
      "api",
      "worker",
    ]);
    expect(pendingSourceFileName("/out/images.todo")).toBe("images.todo");
    expect(groupedItemServiceKeys({ name: "media" }, item)).toEqual([
      "media/app",
    ]);
    expect(groupedItemTarget(item)).toBe("repo/app:2.0");
    expect(groupedItemActionLabel(item)).toBe("Tag update");
    expect(groupedItemActionTagType(item)).toBe("warning");
    expect(itemsBreakingCount([item], () => breakingNote)).toBe(1);
    expect(tagInputProps(item)).toEqual({ "aria-label": "New tag for repo/app:1.0" });
    expect(
      releaseNoteReason(releaseNoteInfo({
        links: [],
        error: "missing LSIO upstream mapping for linuxserver/radarr",
      })),
    ).toBe(
      "Add a LinuxServer.io upstream map entry for linuxserver/radarr.",
    );
    expect(
      releaseNoteStatus(
        releaseNoteInfo({ links: [], status: "error" }),
        false,
      ),
    ).toBe("Check failed");
  });

  it("matches pending search fields from items, groups, safety cues, diagnostics, and release notes", () => {
    const app = pendingGroupedItem({
      line_no: 7,
      raw: "repo/app:latest sha256=feedface",
      image: "repo/app:latest",
      repo: "repo/app",
      key: "repo/app",
      current_tag: "latest",
      desired_tag: "",
      digest: "sha256:feedface",
      services: ["worker"],
      action: "digest-pin",
      diagnostic: {
        code: "compose-label-active-file-missing",
        message: "Container worker was created from stack media.",
        hint: "Restore docker-compose.archive.yml before applying.",
        stack: "media",
        service: "worker",
        compose_file: "docker-compose.yml",
        found_files: ["docker-compose.archive.yml"],
        details: {
          preflight_findings: ["Docker labels reference docker-compose.yml."],
        },
      },
    });
    const db = pendingGroupedItem({
      line_no: 8,
      image: "postgres:16",
      repo: "postgres",
      services: ["db"],
    });
    const group = {
      name: "media",
      directory: "/docker/media",
      compose_file: "docker-compose.yml",
      project_directory: "/docker/media",
      services_label: "worker, db",
      services: ["worker", "db"],
      line_numbers: [7, 8],
      items: [app, db],
    };
    const note = releaseNoteInfo({
      line_no: 7,
      links: [],
      status: "unsupported",
      error: "no supported GitHub release source found",
    });
    const context = {
      releaseChangelogFor: () => ({
        status: "ready" as const,
        body: "Server-Sent Events replace WebSocket live updates.",
        sourceUrl: "https://raw.githubusercontent.com/t-mart/mousehole/master/CHANGELOG.md",
        error: "",
      }),
      releaseNoteFor: () => note,
      releaseNoteReason,
      releaseNoteStatus: (value: typeof note | null) =>
        releaseNoteStatus(value, false),
      riskCues: () => [
        { key: "mutable-latest", label: "Mutable latest", type: "warning" as const },
      ],
    };

    expect(normalizePendingSearch("  Mutable   Latest ")).toBe("mutable latest");
    expect(pendingItemMatchesSearch(app, "digest pin", context)).toBe(true);
    expect(pendingItemMatchesSearch(app, "feedface", context)).toBe(true);
    expect(pendingItemMatchesSearch(app, "docker-compose.archive", context)).toBe(true);
    expect(pendingItemMatchesSearch(app, "Only GHCR", context)).toBe(true);
    expect(pendingItemMatchesSearch(app, "server-sent events", context)).toBe(true);
    expect(pendingItemMatchesSearch(app, "mutable latest", context)).toBe(true);

    const itemMatchedGroups = filterPendingStackGroups([group], "worker", context);
    expect(itemMatchedGroups).toHaveLength(1);
    expect(itemMatchedGroups[0].items).toEqual([app]);
    expect(itemMatchedGroups[0].visibleLineNumbers).toEqual([7]);
    expect(itemMatchedGroups[0].line_numbers).toEqual([7, 8]);

    const groupMatchedGroups = filterPendingStackGroups(
      [group],
      "docker/media",
      context,
    );
    expect(groupMatchedGroups[0].items).toEqual([app, db]);
    expect(groupMatchedGroups[0].visibleLineNumbers).toEqual([7, 8]);
  });

  it("matches display-only snoozed candidates by visible fields", () => {
    const candidate = pendingSnoozedCandidate({
      service_key: "media/hidden",
      image: "repo/hidden:1.0",
      target_image: "repo/hidden:1.1",
      source_id: "docker.local.hidden",
      reason: "maintenance window",
      wud_metadata: wudContainerMetadata({
        link: "https://metadata-only.example/releases",
      }),
    });

    expect(filterSnoozedCandidates([candidate], "media hidden")).toEqual([
      candidate,
    ]);
    expect(filterSnoozedCandidates([candidate], "repo/hidden")).toEqual([
      candidate,
    ]);
    expect(filterSnoozedCandidates([candidate], "docker.local.hidden")).toEqual([
      candidate,
    ]);
    expect(filterSnoozedCandidates([candidate], "metadata-only.example")).toEqual(
      [],
    );
    expect(filterSnoozedCandidates([candidate], "does-not-match")).toEqual([]);
  });

  it("matches diagnostic details without recursing through circular references", () => {
    const detailNode: Record<string, unknown> = {
      finding: "Circular diagnostic marker",
    };
    detailNode.self = detailNode;
    const item = pendingGroupedItem({
      diagnostic: {
        code: "compose-label-active-file-missing",
        message: "Container worker was created from stack media.",
        hint: "Restore docker-compose.yml before applying.",
        stack: "media",
        service: "worker",
        compose_file: "docker-compose.yml",
        found_files: [],
        details: {
          nested: detailNode,
          again: detailNode,
        },
      },
    });
    const context: PendingSearchContext = {
      releaseChangelogFor: () => null,
      releaseNoteFor: () => null,
      releaseNoteReason,
      releaseNoteStatus: (value) => releaseNoteStatus(value, false),
      riskCues: () => [],
    };

    expect(pendingItemMatchesSearch(item, "circular diagnostic", context)).toBe(
      true,
    );
  });

  it("creates fallback table renderers for tags, digests, safety, and release notes", async () => {
    const item = pendingGroupedItem({
      line_no: 1,
      image: "repo/app:1.0",
      repo: "repo/app",
      desired_tag: "2.0.0",
      digest: "sha256:abcdefghijklmnopqrstuvwxyz0123456789",
    });
    const updateTagOverride = vi.fn();
    const columns = createPendingColumns({
      displayDigest: () => "sha256:abcdef...789",
      displayValue: (value) => value || "None",
      releaseNoteFor: () =>
        releaseNoteInfo({
          breaking: true,
          breaking_reasons: ["Major version update."],
        }),
      releaseNoteReason: () => "",
      releaseNoteStatus: () => "",
      riskCues: () => [
        { key: "major-bump", label: "Major bump", type: "error" },
        { key: "security-unknown", label: "Candidate scan: unavailable", type: "warning" },
      ],
      tagInputProps: (row) => ({ "aria-label": `New tag for ${row.image}` }),
      tagOverrideValue: () => "2.0.0",
      updateTagOverride,
    });

    const renderColumn = (key: string) => {
      const column = columns.find((item) => (item as RenderColumn).key === key) as
        | RenderColumn
        | undefined;
      return column?.render?.(item) ?? null;
    };
    const TestRenderer = defineComponent({
      setup() {
        return () =>
          h("div", [
            renderColumn("desired_tag"),
            renderColumn("digest"),
            renderColumn("safety_cues"),
            renderColumn("release_notes"),
          ]);
      },
    });

    const wrapper = mountWithApp(TestRenderer);
    expect(wrapper.find("input").attributes("aria-label")).toBe(
      "New tag for repo/app:1.0",
    );
    await wrapper.find("input").setValue("2.1.0");
    expect(updateTagOverride).toHaveBeenCalledWith(item, "2.1.0");
    expect(wrapper.text()).toContain("sha256:abcdef...789");
    expect(wrapper.text()).toContain("Major bump");
    expect(wrapper.text()).toContain("Candidate scan: unavailable");
    const evidence = wrapper.find(".risk-badges-container .evidence-explanation");
    expect(evidence.text()).toContain("published vulnerability evidence");
    expect(evidence.text()).toContain("candidate scans inspect the proposed image");
    expect(evidence.text()).toContain("not a guarantee of a safe update");
    expect(wrapper.text()).toContain("GitHub release");
    await wrapper.find('button[aria-haspopup="dialog"]').trigger("click");
    expect(wrapper.text()).toContain("Possible breaking change");
    expect(wrapper.find(".release-note-link").attributes("rel")).toBe(
      "noopener noreferrer",
    );
  });

  it("renders digest provenance ahead of raw digest values in pending tables", () => {
    const digest = "sha256:abcdefghijklmnopqrstuvwxyz0123456789";
    const item = pendingGroupedItem({
      line_no: 1,
      image: "repo/app:latest",
      repo: "repo/app",
      desired_tag: "",
      digest,
      digest_provenance: {
        source_image: "repo/app:latest",
        resolved_tag: "latest",
        watch_tag: "latest",
        target_digest: digest,
        final_image: `repo/app@${digest}`,
        provenance_source: "compose",
        provenance_confidence: "recovered",
      },
    });
    const columns = createPendingColumns({
      displayDigest: () => "raw digest fallback",
      displayValue: (value) => value || "None",
      releaseNoteFor: () => null,
      releaseNoteReason: () => "",
      releaseNoteStatus: () => "Not checked",
      riskCues: () => [],
      tagInputProps: (row) => ({ "aria-label": `New tag for ${row.image}` }),
      tagOverrideValue: () => "",
      updateTagOverride: vi.fn(),
    });
    const digestColumn = columns.find((column) => {
      return (column as RenderColumn).key === "digest";
    }) as RenderColumn | undefined;
    const TestRenderer = defineComponent({
      setup() {
        return () => h("div", [digestColumn?.render?.(item) ?? null]);
      },
    });

    const wrapper = mountWithApp(TestRenderer);
    expect(wrapper.text()).toContain("repo/app: latest -> latest");
    expect(wrapper.text()).toContain(
      "Digest: sha256:abcdefghijklm...yz0123456789",
    );
    expect(wrapper.text()).not.toContain("raw digest fallback");
    expect(wrapper.find(".digest-provenance").attributes("title")).toContain(
      `Digest: ${digest}`,
    );
  });

  it("forwards pending modal update-close events to the owning view", async () => {
    const modalCases = [
      {
        component: PendingPlanReviewModal,
        props: pendingPlanReviewModalProps(),
      },
      {
        component: PendingCleanupModal,
        props: {
          assistantActions: [],
          assistantFindings: [],
          assistantReasons: [],
          cleanupButtonLabel: "Remove 0 unmatched entries",
          cleanupDisabled: true,
          cleanupItems: [],
          cleanupLineLabel: () => "#1",
          loading: false,
          pendingSourceLabel: "images.todo",
          show: true,
        },
      },
      {
        component: PendingRemovalModal,
        props: {
          loading: false,
          pendingSourceLabel: "images.todo",
          removalConfirmButtonLabel: "Remove 0 selected entries",
          removalDisabled: true,
          removalItems: [],
          removalLineLabel: () => "#1",
          show: true,
        },
      },
    ];

    for (const { component, props } of modalCases) {
      const wrapper = mountPendingModal(component, props);

      await emitModalShowUpdate(wrapper, true);
      expect(wrapper.emitted("close")).toBeUndefined();

      await emitModalShowUpdate(wrapper, false);
      expect(wrapper.emitted("close")).toHaveLength(1);
    }
  });

  it("renders digest-unpin notices and plan line labels", () => {
    const plan = planResponse();
    const line = {
      ...plan.stacks[0].lines[0],
      action: "digest-unpin" as const,
      compose_image: "repo/app@sha256:old",
      target_image: "repo/app:latest",
    };
    const update = {
      source_image: "repo/app@sha256:old",
      resolved_tag: "latest",
      tag_image: "repo/app:latest",
      current_digest: "sha256:old",
      target_digest: "sha256:new",
      watch_tag: "latest",
      marker: "wudup.resolved-tag=latest",
      label_key: "wud.tag.include",
      label_value: "^latest$$",
      services: ["app"],
    };
    const wrapper = mountPendingModal(
      PendingPlanReviewModal,
      pendingPlanReviewModalProps({
        plan: {
          ...plan,
          status: "blocked",
          stacks: [{ ...plan.stacks[0], lines: [line] }],
        },
        planDigestUnpinUpdates: [{ stack: "media", update }],
        planLines: [{ stack: "media", line }],
        preflightDigestUnpinNotice:
          "1 digest unpin migration will rewrite pinned Compose images back to their watched tag before pulling.",
      }),
    );

    expect(wrapper.text()).toContain("Digest unpin migration");
    expect(wrapper.text()).toContain("1 digest unpin migration");
    expect(wrapper.text()).toContain("repo/app@sha256:old");
    expect(wrapper.text()).toContain("repo/app:latest");
    expect(wrapper.text()).toContain("Digest unpin");
  });

  it("renders both update-stream decisions and emits the keyboard-safe choice", async () => {
    const issue = {
      severity: "error",
      code: "tag-stream-change",
      message: "Choose an update stream.",
      line_no: 9,
      stack: "jarvis",
      service: "task-runner",
      hint: "",
      details: {
        current_stream: "distroless",
        reported_stream: "default",
        reported_tag: "2.34.4",
        same_stream_tag: "2.34.4-distroless",
        preserve_label_regex: String.raw`^\d+\.\d+\.\d+-distroless$`,
      },
    };
    const wrapper = mountPendingModal(
      PendingPlanReviewModal,
      pendingPlanReviewModalProps({
        plan: planResponse({ status: "blocked", issues: [issue] }),
        tagStreamDecisionIssues: [issue],
        issueDetailString: (item: typeof issue, key: string) =>
          typeof item.details[key as keyof typeof item.details] === "string"
            ? item.details[key as keyof typeof item.details]
            : "",
      }),
    );

    expect(wrapper.text()).toContain("Update stream change");
    expect(wrapper.text()).toContain("Keep distroless");
    expect(wrapper.text()).toContain("Switch to default");
    expect(wrapper.find("fieldset.stream-choice-group legend").text()).toBe(
      "Update stream choice for line 9",
    );
    const keep = wrapper
      .findAll("button")
      .find((button) => button.text().includes("Keep distroless"));
    expect(keep).toBeDefined();
    expect(keep?.attributes("type")).toBe("button");
    await keep?.trigger("click");
    expect(wrapper.emitted("choose-tag-stream")?.[0]).toEqual([issue, "preserve"]);
  });
});


describe("queue evidence explanation", () => {
  it.each(["verified-security", "security-review", "security-stale", "security-unknown", "security-none-reported", "security-not-scanned"])(
    "keeps %s advisory and distinguishes its source from candidate scanning",
    (key) => {
      const wrapper = mount(PendingEvidenceExplanation, {
        props: { cues: [{ key, label: "Evidence", type: "warning" }] },
      });
      expect(wrapper.text()).toContain("published vulnerability evidence");
      expect(wrapper.text()).toContain("candidate scans inspect the proposed image");
      expect(wrapper.text()).toContain("Both are advisory checks, not a guarantee of a safe update");
    },
  );

  it.each([
    ["security-none-reported", "Candidate scan: None reported", "success"],
    ["security-not-scanned", "Candidate scan: Not scanned", "default"],
    ["security-stale", "Candidate scan: stale", "warning"],
  ] as const)("keeps %s visible before opening stack details", (key, label, type) => {
    const group = pendingGrouping([pendingGroupedItem()]).groups[0]!;
    const wrapper = mount(PendingStackCard, {
      global: { stubs: naiveStubs },
      props: {
        group,
        loading: false,
        releaseNoteFor: () => null,
        releaseNoteReason: () => "",
        releaseNoteStatus: () => "Not checked",
        riskCues: () => [{ key, label, type }],
        securityScanFor: () => null,
        selectedSelectionKeySet: new Set<string>(),
        stackHasSelection: false,
        stackIndeterminate: false,
        stackSelected: false,
        tagInputProps: () => ({ "aria-label": "New tag" }),
        tagOverrideValue: () => "",
        updateDisabled: false,
      },
    });
    expect(wrapper.find(".stack-change-preview").text()).toContain(label);
    expect(wrapper.find(".stack-change-preview .evidence-explanation").text()).toContain("Both are advisory checks");
    expect(wrapper.find(".stack-details").attributes("open")).toBeUndefined();
  });

  it("omits security guidance when the row has only operational cues", () => {
    const wrapper = mount(PendingEvidenceExplanation, {
      props: { cues: [{ key: "major-bump", label: "Major bump", type: "error" }] },
    });
    expect(wrapper.find("p").exists()).toBe(false);
  });
});
