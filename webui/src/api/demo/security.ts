import type {
  PendingItem,
  SecurityScanInfo,
  SecurityScanSeverityCounts,
} from "../types";
import {
  normalizeSecurityDigest,
  pendingItemPlatform,
} from "../../utils/securityScans";

const DEMO_NOW = "2026-05-31T00:00:00.000Z";
const DEMO_DB_UPDATED_AT = "2026-05-28T12:00:00+00:00";
const EMPTY_SECURITY_COUNTS: SecurityScanSeverityCounts = {
  critical: 0,
  high: 0,
  medium: 0,
  low: 0,
  unknown: 0,
};
const DEMO_FINDING_SECURITY_COUNTS: SecurityScanSeverityCounts = {
  critical: 0,
  high: 1,
  medium: 0,
  low: 0,
  unknown: 0,
};
type DemoSecurityScanDecision = {
  hasFindings: boolean;
  state: SecurityScanInfo["state"];
  verdict: SecurityScanInfo["verdict"];
};


export function securityScanInfo(
  item: PendingItem,
  firstExact: boolean,
): SecurityScanInfo {
  const reportedDigest = normalizeSecurityDigest(item.digest);
  const platform = pendingItemPlatform(item);
  const decision = securityScanDecision(reportedDigest, platform, firstExact);
  const severityCounts = securityScanSeverityCounts(decision.hasFindings);
  const fixableCounts = securityScanSeverityCounts(decision.hasFindings);
  const findings = demoSecurityScanFindings(decision.hasFindings);
  const subject = demoSecurityScanSubject(item, reportedDigest, platform);
  const currentDigest = normalizeSecurityDigest(item.wud_metadata?.local_digest ?? "");
  const currentSubject = demoSecurityScanSubject(item, currentDigest, platform);
  const scannerVersion = decision.hasFindings ? "demo" : "";
  const scannerSchema = decision.hasFindings ? "trivy-json" : "";
  const dbRevision = decision.hasFindings ? "demo" : "";
  const dbUpdatedAt = decision.hasFindings ? DEMO_DB_UPDATED_AT : "";
  const comparison = demoSecurityScanComparison(
    decision,
    currentDigest,
    reportedDigest,
    platform,
    currentSubject,
    findings,
    Boolean(scannerVersion && scannerSchema && dbRevision && dbUpdatedAt),
  );

  return {
    line_no: item.line_no,
    state: decision.state,
    verdict: decision.verdict,
    scanner: "trivy",
    scanner_version: scannerVersion,
    scanner_schema: scannerSchema,
    scanned_at: decision.hasFindings ? DEMO_NOW : "",
    db_revision: dbRevision,
    db_updated_at: dbUpdatedAt,
    severity_counts: severityCounts,
    advisory_counts: severityCounts,
    advisory_counts_known: true,
    fixable_counts: fixableCounts,
    unfixed_count: 0,
    findings,
    subject,
    comparison,
    warnings:
      decision.hasFindings
        ? ["Demo finding for candidate and installed-digest comparison display."]
        : [],
    error_code: "",
    error_message: "",
  };
}

function demoSecurityScanFindings(
  hasFindings: boolean,
): SecurityScanInfo["findings"] {
  if (!hasFindings) {
    return [];
  }
  return [
    {
      target: "debian:12",
      target_class: "os-pkgs",
      target_type: "debian",
      vulnerability_id: "CVE-2026-0001",
      package_name: "demo-package",
      installed_version: "1.0.0",
      fixed_version: "1.0.1",
      severity: "high",
      title: "Demo vulnerability for candidate advisory review",
      primary_url: "https://avd.aquasec.com/nvd/cve-2026-0001",
    },
  ];
}

function demoSecurityScanSubject(
  item: PendingItem,
  digest: string,
  platform: string,
): SecurityScanInfo["subject"] {
  return {
    requested_ref: item.image,
    reported_digest: digest,
    index_digest: digest,
    manifest_digest: digest,
    immutable_ref: digest ? `${item.image.split("@")[0]}@${digest}` : "",
    platform,
  };
}

function demoSecurityScanComparison(
  decision: DemoSecurityScanDecision,
  currentDigest: string,
  reportedDigest: string,
  platform: string,
  currentSubject: SecurityScanInfo["subject"],
  findings: SecurityScanInfo["findings"],
  provenanceKnown: boolean,
): SecurityScanInfo["comparison"] {
  const comparisonReady =
    decision.state === "complete"
    && Boolean(currentDigest && reportedDigest && platform)
    && provenanceKnown;
  let message = "";
  if (comparisonReady) {
    message = "Demo comparison: installed and candidate findings are unchanged.";
  } else if (decision.state === "complete") {
    message = "Installed digest is unavailable in the demo fixture.";
  }
  return {
    status: comparisonReady ? "unchanged" : "unknown",
    current_subject: comparisonReady
      ? currentSubject
      : {
          requested_ref: "",
          reported_digest: "",
          index_digest: "",
          manifest_digest: "",
          immutable_ref: "",
          platform: "",
        },
    fixed_findings: [],
    remaining_findings: comparisonReady ? findings : [],
    introduced_findings: [],
    message,
  };
}

function securityScanDecision(
  reportedDigest: string,
  platform: string,
  reviewCandidate: boolean,
): DemoSecurityScanDecision {
  const exact = Boolean(reportedDigest && platform);
  const hasFindings = reviewCandidate && exact;

  if (hasFindings) {
    return {
      hasFindings,
      state: "complete",
      verdict: "findings",
    };
  }

  if (!exact) {
    return {
      hasFindings,
      state: "unsupported",
      verdict: "unknown",
    };
  }
  return {
    hasFindings,
    state: "not_scanned",
    verdict: "unknown",
  };
}

function securityScanSeverityCounts(
  hasFindings: boolean,
): SecurityScanSeverityCounts {
  if (hasFindings) {
    return { ...DEMO_FINDING_SECURITY_COUNTS };
  }
  return { ...EMPTY_SECURITY_COUNTS };
}
