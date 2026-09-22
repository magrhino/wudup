"""Release advisory assessment and affected-version evidence.

Owns bounded signal scanning, advisory matching, severity, and retryable reason
codes. Fetches use the existing GitHub client; no cache writes occur here."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .release_note_models import (
    ReleaseNoteContext,
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseSecurityAssessment,
    ReleaseSecuritySeverity,
)
from .release_note_providers import (
    CVE_ID_RE,
    GHSA_ID_RE,
    SECURITY_ADVISORY_FETCH_MAX,
    GitHubClient,
    _github_repo_valid,
    _github_source_repo,
    _object_list,
    _object_or_none,
)

SECURITY_SIGNAL_RE = re.compile(
    r"\bupdate\s+asap\b|\bcritical\b|\bsecurity\b|"
    r"\bGHSA-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}\b|"
    r"\bCVE-[0-9]{4}-[0-9]{4,}\b|"
    r"github\.com/(?:[^\s/]+/[^\s/]+/security/advisories|advisories)/",
    re.IGNORECASE | re.ASCII,
)
ADVISORY_ID_RE = re.compile(
    r"\b(?:GHSA-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}|"
    r"CVE-[0-9]{4}-[0-9]{4,})\b",
    re.IGNORECASE | re.ASCII,
)
STRICT_VERSION_RE = re.compile(
    r"^[vV]?(\d{1,10})\.(\d{1,10})(?:\.(\d{1,10}))?$",
    re.ASCII,
)
VERSION_COMPARISON_RE = re.compile(
    r"^(<=|>=|<|>|=)\s*([vV]?\d{1,10}\.\d{1,10}(?:\.\d{1,10})?)$",
    re.ASCII,
)
SECURITY_SIGNAL_SCAN_MAX_CHARS = 100_000
SECURITY_ADVISORY_ID_MAX = 8
SECURITY_BACKFILL_FAILURE_REASON_CODE = "security_backfill_failed"
SECURITY_RETRYABLE_REASON_CODES = frozenset(
    {
        "advisory_lookup_failed",
        "advisory_unresolved",
        SECURITY_BACKFILL_FAILURE_REASON_CODE,
    }
)


@dataclass(frozen=True)
class _FetchedAdvisory:
    payload: dict[str, Any]
    repository_matched: bool = False


def assess_release_security(
    context: ReleaseNoteContext,
    info: ReleaseNoteInfo,
    client: GitHubClient,
) -> tuple[ReleaseSecurityAssessment, list[ReleaseNoteLink]]:
    """Classify security urgency without treating release prose as exposure proof."""

    signal_text = "\n".join(
        [info.title, info.body, *(link.url for link in info.links)]
    )
    scanned_text = signal_text[:SECURITY_SIGNAL_SCAN_MAX_CHARS]
    if not SECURITY_SIGNAL_RE.search(scanned_text):
        return ReleaseSecurityAssessment(), []

    extracted_ids = sorted(
        {match.group(0).upper() for match in ADVISORY_ID_RE.finditer(scanned_text)}
    )
    lookup_truncated = (
        len(signal_text) > SECURITY_SIGNAL_SCAN_MAX_CHARS
        or len(extracted_ids) > SECURITY_ADVISORY_ID_MAX
        or len(extracted_ids) > SECURITY_ADVISORY_FETCH_MAX
    )
    advisory_ids = extracted_ids[:SECURITY_ADVISORY_ID_MAX]
    advisories, lookup_failed, fetch_truncated = _fetch_security_advisories(
        client,
        context,
        advisory_ids[:SECURITY_ADVISORY_FETCH_MAX],
    )
    lookup_truncated = lookup_truncated or fetch_truncated
    advisory_ids = _advisory_ids(advisory_ids, advisories)
    links = _advisory_links(advisories, advisory_ids)
    severity = _highest_advisory_severity(advisories)
    verified = [
        advisory
        for advisory in advisories
        if _advisory_verifies_exposure(advisory, context, info)
    ]

    if verified:
        verified_severity = _highest_advisory_severity(verified)
        verified_ids = _advisory_ids([], verified)
        current_version, target_version = _security_versions(context, info)
        identifier = verified_ids[0] if verified_ids else "a GitHub advisory"
        incomplete = (
            " Additional advisory lookup was incomplete."
            if lookup_failed or lookup_truncated
            else ""
        )
        return (
            ReleaseSecurityAssessment(
                outcome="verified_critical_high",
                severity=verified_severity,
                reason_code="verified_exposure",
                reason=(
                    f"Verified {verified_severity.title()} advisory {identifier} "
                    f"affects {current_version} and is patched by {target_version}."
                    f"{incomplete}"
                ),
                advisory_ids=advisory_ids,
                lookup_truncated=lookup_truncated,
            ),
            links,
        )
    if lookup_truncated:
        return (
            ReleaseSecurityAssessment(
                outcome="needs_review",
                severity=severity,
                reason_code="advisory_lookup_truncated",
                reason=(
                    "Security signals were found, but advisory lookup was capped; "
                    "review the linked advisories."
                ),
                advisory_ids=advisory_ids,
                lookup_truncated=True,
            ),
            links,
        )
    if lookup_failed:
        return (
            ReleaseSecurityAssessment(
                outcome="needs_review",
                severity=severity,
                reason_code="advisory_lookup_failed",
                reason=(
                    "Security signals were found, but GitHub advisory evidence "
                    "could not be checked."
                ),
                advisory_ids=advisory_ids,
            ),
            links,
        )
    reason_code, reason = _unverified_security_reason(advisories, advisory_ids, severity)
    return (
        ReleaseSecurityAssessment(
            outcome="needs_review",
            severity=severity,
            reason_code=reason_code,
            reason=reason,
            advisory_ids=advisory_ids,
        ),
        links,
    )


def _unverified_security_reason(
    advisories: list[_FetchedAdvisory],
    advisory_ids: list[str],
    severity: ReleaseSecuritySeverity,
) -> tuple[str, str]:
    if not advisories:
        if advisory_ids:
            reason_code = "advisory_unresolved"
            reason = (
                "Security language or advisory identifiers were found, but no "
                "structured advisory could be resolved."
            )
        else:
            reason_code = "security_signal_only"
            reason = (
                "Security language was found, but no advisory identifier was "
                "available for structured exposure verification."
            )
    elif severity in {"low", "moderate", "unknown", "none"}:
        reason_code = "severity_below_high"
        reason = (
            f"A {severity.title()} advisory was found, but Critical/High "
            "exposure was not verified."
        )
    else:
        reason_code = "exposure_unverified"
        reason = (
            f"{severity.title()} advisory found; the running and target versions "
            "could not be matched to structured affected-version evidence."
        )
    return reason_code, reason


def security_assessment_from_mapping(value: object) -> ReleaseSecurityAssessment:
    if not isinstance(value, Mapping):
        return ReleaseSecurityAssessment()
    outcome = str(value.get("outcome") or "ordinary")
    if outcome not in {"verified_critical_high", "needs_review", "ordinary"}:
        outcome = "ordinary"
    severity = str(value.get("severity") or "none")
    if severity not in {"critical", "high", "moderate", "low", "unknown", "none"}:
        severity = "unknown"
    advisory_ids = value.get("advisory_ids")
    return ReleaseSecurityAssessment(
        outcome=outcome,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        reason_code=str(value.get("reason_code") or "no_security_signal"),
        reason=str(
            value.get("reason")
            or "No security urgency signal was found in the release notes."
        ),
        advisory_ids=(
            sorted({str(item).upper() for item in advisory_ids if str(item)})
            if isinstance(advisory_ids, list)
            else []
        ),
        lookup_truncated=bool(value.get("lookup_truncated")),
    )


def _fetch_security_advisories(
    client: GitHubClient,
    context: ReleaseNoteContext,
    advisory_ids: Iterable[str],
) -> tuple[list[_FetchedAdvisory], bool, bool]:
    advisories: list[_FetchedAdvisory] = []
    failed = False
    seen: set[str] = set()
    identifiers = list(advisory_ids)
    for index, advisory_id in enumerate(identifiers):
        try:
            fetched, item_truncated = _fetch_security_advisory(
                client,
                context,
                advisory_id,
            )
        except Exception:  # noqa: BLE001 - failure becomes bounded review metadata.
            failed = True
            continue
        for advisory in fetched:
            key = _advisory_stable_id(advisory.payload)
            if not key or key in seen:
                continue
            seen.add(key)
            advisories.append(advisory)
            if len(advisories) >= SECURITY_ADVISORY_FETCH_MAX:
                remaining_ids = index < len(identifiers) - 1
                return advisories, failed, item_truncated or remaining_ids
        if item_truncated:
            return advisories, failed, True
    return advisories, failed, False


def _fetch_security_advisory(
    client: GitHubClient,
    context: ReleaseNoteContext,
    advisory_id: str,
) -> tuple[list[_FetchedAdvisory], bool]:
    if GHSA_ID_RE.fullmatch(advisory_id):
        repository_value = client.repository_advisory(
            context.upstream_repo,
            advisory_id,
        )
        repository_advisory = _advisory_object(repository_value)
        if repository_advisory is not None:
            return [_FetchedAdvisory(repository_advisory, repository_matched=True)], False
        global_value = client.global_advisory(advisory_id)
        global_advisory = _advisory_object(global_value)
        return (
            [] if global_advisory is None else [_FetchedAdvisory(global_advisory)],
            False,
        )

    values = client.global_advisories_for_cve(advisory_id)
    if isinstance(values, dict) and str(values.get("message") or ""):
        if str(values.get("message")) == "Not Found":
            return [], False
        raise RuntimeError("GitHub advisory lookup failed")
    matches = [
        _FetchedAdvisory(item)
        for item in _object_list(values)
        if str(item.get("cve_id") or "").upper() == advisory_id.upper()
    ]
    return (
        matches[:SECURITY_ADVISORY_FETCH_MAX],
        len(matches) >= SECURITY_ADVISORY_FETCH_MAX,
    )


def _advisory_object(value: object) -> dict[str, Any] | None:
    advisory = _object_or_none(value)
    if advisory is None:
        return None
    if not str(advisory.get("ghsa_id") or ""):
        if str(advisory.get("message") or ""):
            raise RuntimeError("GitHub advisory lookup failed")
        return None
    return advisory


def _advisory_verifies_exposure(
    advisory: _FetchedAdvisory,
    context: ReleaseNoteContext,
    info: ReleaseNoteInfo,
) -> bool:
    payload = advisory.payload
    if not _advisory_published(payload):
        return False
    if _advisory_severity(payload) not in {"critical", "high"}:
        return False
    current_version, target_version = _security_versions(context, info)
    current = _strict_version(current_version)
    target = _strict_version(target_version)
    release = _strict_version(info.release_tag)
    if current is None or target is None or release is None or target != release:
        return False
    repository_matched = advisory.repository_matched or _advisory_repository_matches(
        payload,
        context,
    )
    vulnerabilities = payload.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        return False
    for vulnerability in vulnerabilities:
        if not isinstance(vulnerability, Mapping):
            continue
        if not repository_matched and not _advisory_package_matches(
            vulnerability,
            context,
        ):
            continue
        if _vulnerability_verifies_fix(vulnerability, current, target):
            return True
    return False


def _vulnerability_verifies_fix(
    vulnerability: Mapping[str, Any],
    current: tuple[int, int, int],
    target: tuple[int, int, int],
) -> bool:
    vulnerable_range = str(vulnerability.get("vulnerable_version_range") or "")
    if not _version_in_range(current, vulnerable_range):
        return False
    if _version_in_range(target, vulnerable_range):
        return False
    patched_versions = _patched_versions(vulnerability)
    return bool(patched_versions) and any(target >= patched for patched in patched_versions)


def _security_versions(
    context: ReleaseNoteContext,
    info: ReleaseNoteInfo,
) -> tuple[str, str]:
    if context.provider == "lsio":
        current = info.classification.current.upstream_version
        target = info.classification.target.upstream_version
        return current, target
    return context.current_tag, context.target_tag


def _advisory_published(advisory: Mapping[str, Any]) -> bool:
    if advisory.get("withdrawn_at"):
        return False
    state = str(advisory.get("state") or "").lower()
    return state == "published" or (not state and bool(advisory.get("published_at")))


def _advisory_repository_matches(
    advisory: Mapping[str, Any],
    context: ReleaseNoteContext,
) -> bool:
    expected = {context.upstream_repo.lower(), context.image_repo.lower()}
    for name in ("repository_url", "source_code_location", "html_url"):
        repo = _github_repo_from_advisory_url(str(advisory.get(name) or ""))
        if repo and repo.lower() in expected:
            return True
    return False


def _github_repo_from_advisory_url(value: str) -> str:
    api_prefix = "https://api.github.com/repos/"
    if value.startswith(api_prefix):
        parts = value.removeprefix(api_prefix).split("/")
        candidate = "/".join(parts[:2])
        return candidate if _github_repo_valid(candidate) else ""
    return _github_source_repo(value)


def _advisory_package_matches(
    vulnerability: Mapping[str, Any],
    context: ReleaseNoteContext,
) -> bool:
    package = vulnerability.get("package")
    if not isinstance(package, Mapping):
        return False
    package_name = str(package.get("name") or "").lower()
    expected = {context.upstream_repo.lower(), context.image_repo.lower()}
    return bool(package_name and package_name in expected)


def _strict_version(value: str) -> tuple[int, int, int] | None:
    match = STRICT_VERSION_RE.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _version_in_range(version: tuple[int, int, int], spec: str) -> bool:
    terms = [term.strip() for term in spec.split(",") if term.strip()]
    if not terms:
        return False
    exact_versions: list[tuple[int, int, int]] = []
    for term in terms:
        exact = _strict_version(term.removeprefix("=").strip())
        if exact is None or (term[:1] in "<>" if term else False):
            exact_versions = []
            break
        exact_versions.append(exact)
    if exact_versions:
        return version in exact_versions

    comparisons: list[tuple[str, tuple[int, int, int]]] = []
    for term in terms:
        match = VERSION_COMPARISON_RE.fullmatch(term)
        if match is None:
            return False
        expected = _strict_version(match.group(2))
        if expected is None:
            return False
        comparisons.append((match.group(1), expected))
    return all(_version_comparison(version, operator, expected) for operator, expected in comparisons)


def _version_comparison(
    version: tuple[int, int, int],
    operator: str,
    expected: tuple[int, int, int],
) -> bool:
    return {
        "<": version < expected,
        "<=": version <= expected,
        ">": version > expected,
        ">=": version >= expected,
        "=": version == expected,
    }[operator]


def _patched_versions(vulnerability: Mapping[str, Any]) -> list[tuple[int, int, int]]:
    patched = vulnerability.get("first_patched_version")
    if isinstance(patched, Mapping):
        raw = str(patched.get("identifier") or "")
    elif isinstance(patched, str):
        raw = patched
    else:
        raw = str(vulnerability.get("patched_versions") or "")
    versions = [_strict_version(value.strip().removeprefix("=").strip()) for value in raw.split(",")]
    return sorted({version for version in versions if version is not None})


def _advisory_severity(advisory: Mapping[str, Any]) -> ReleaseSecuritySeverity:
    value = str(advisory.get("severity") or "").lower()
    if value == "medium":
        value = "moderate"
    if value in {"critical", "high", "moderate", "low"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _highest_advisory_severity(
    advisories: Iterable[_FetchedAdvisory],
) -> ReleaseSecuritySeverity:
    order = {"none": 0, "unknown": 1, "low": 2, "moderate": 3, "high": 4, "critical": 5}
    severities = [_advisory_severity(advisory.payload) for advisory in advisories]
    return max(severities, key=order.__getitem__, default="unknown")


def _advisory_stable_id(advisory: Mapping[str, Any]) -> str:
    return str(advisory.get("ghsa_id") or advisory.get("cve_id") or "").upper()


def _advisory_ids(
    existing: Iterable[str],
    advisories: Iterable[_FetchedAdvisory],
) -> list[str]:
    values = {value.upper() for value in existing if value}
    for advisory in advisories:
        for name in ("ghsa_id", "cve_id"):
            value = str(advisory.payload.get(name) or "").upper()
            if value:
                values.add(value)
    return sorted(values)[:SECURITY_ADVISORY_ID_MAX]


def _advisory_links(
    advisories: Iterable[_FetchedAdvisory],
    advisory_ids: Iterable[str],
) -> list[ReleaseNoteLink]:
    ghsa_ids = {
        str(advisory.payload.get("ghsa_id") or "").upper()
        for advisory in advisories
        if str(advisory.payload.get("ghsa_id") or "")
    }
    ghsa_ids.update(value for value in advisory_ids if GHSA_ID_RE.fullmatch(value))
    links = [
        ReleaseNoteLink(
            label=advisory_id,
            url=f"https://github.com/advisories/{advisory_id}",
            kind="security_advisory",
        )
        for advisory_id in sorted(ghsa_ids)
    ]
    linked_cves = {
        value
        for value in advisory_ids
        if CVE_ID_RE.fullmatch(value)
        and not any(
            str(advisory.payload.get("cve_id") or "").upper() == value
            and str(advisory.payload.get("ghsa_id") or "")
            for advisory in advisories
        )
    }
    links.extend(
        ReleaseNoteLink(
            label=cve_id,
            url=(
                "https://github.com/advisories?"
                + urllib.parse.urlencode({"query": cve_id})
            ),
            kind="security_advisory",
        )
        for cve_id in sorted(linked_cves)
    )
    return links
