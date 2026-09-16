"""Structured GitHub release-note metadata for the WebUI."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .db import utc_timestamp
from .images import image_repo_ref, image_tag
from .lsio_updates import (
    LSIOUpdateClassification,
    classification_from_mapping,
    classify_lsio_update,
    normalize_lsio_version,
    parse_lsio_tag,
)
from .wud_file import WudTarget

SUCCESS_CACHE_TTL_SECONDS = 21_600
ERROR_CACHE_TTL_SECONDS = 900
DEFAULT_GITHUB_TIMEOUT_SECONDS = 6.0
LSIO_RELEASE_SCAN_MAX_PAGES = 10
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OCI_SOURCE_LABEL = "org.opencontainers.image.source"
SEMVER_RE = re.compile(
    r"(?<![0-9A-Za-z])v?([0-9]+)(?:\.[0-9]+){1,3}"
    r"(?:[._-][0-9A-Za-z]+)*(?![0-9A-Za-z])"
)
COMPOSITE_UPSTREAM_RE = re.compile(r"^([vV]?\d+(?:\.\d+){1,3})_v?\d", re.ASCII)
BREAKING_RE = re.compile(
    r"breaking|migration|incompatible|manual step|major change|"
    r"requires [^ \n]+ [0-9]|deprecated[^.\n]*remov|remove[ds] feature",
    re.IGNORECASE,
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
GHSA_ID_RE = re.compile(
    r"^GHSA-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}$",
    re.IGNORECASE | re.ASCII,
)
CVE_ID_RE = re.compile(r"^CVE-[0-9]{4}-[0-9]{4,}$", re.IGNORECASE | re.ASCII)
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
SECURITY_ADVISORY_FETCH_MAX = 4
SECURITY_BACKFILL_FAILURE_REASON_CODE = "security_backfill_failed"
SECURITY_RETRYABLE_REASON_CODES = frozenset(
    {
        "advisory_lookup_failed",
        "advisory_unresolved",
        SECURITY_BACKFILL_FAILURE_REASON_CODE,
    }
)

ReleaseNoteStatus = Literal[
    "cached",
    "ready",
    "missing",
    "unsupported",
    "not_found",
    "error",
]
ReleaseSecurityOutcome = Literal[
    "verified_critical_high",
    "needs_review",
    "ordinary",
]
ReleaseSecuritySeverity = Literal[
    "critical",
    "high",
    "moderate",
    "low",
    "unknown",
    "none",
]


@dataclass(frozen=True)
class ReleaseNoteLink:
    label: str
    url: str
    kind: str


@dataclass(frozen=True)
class ReleaseSecurityAssessment:
    outcome: ReleaseSecurityOutcome = "ordinary"
    severity: ReleaseSecuritySeverity = "none"
    reason_code: str = "no_security_signal"
    reason: str = "No security urgency signal was found in the release notes."
    advisory_ids: list[str] = field(default_factory=list)
    lookup_truncated: bool = False


@dataclass(frozen=True)
class ReleaseNoteInfo:
    line_no: int
    status: ReleaseNoteStatus
    provider: str
    image_repo: str
    upstream_repo: str
    release_tag: str = ""
    title: str = ""
    published_at: str = ""
    breaking: bool = False
    breaking_reasons: list[str] = field(default_factory=list)
    links: list[ReleaseNoteLink] = field(default_factory=list)
    refreshed_at: str = ""
    error: str = ""
    body: str = ""
    classification: LSIOUpdateClassification = field(
        default_factory=LSIOUpdateClassification
    )
    security: ReleaseSecurityAssessment = field(
        default_factory=ReleaseSecurityAssessment
    )


@dataclass(frozen=True)
class ReleaseNoteContext:
    line_no: int
    cache_key: str
    provider: str
    image_repo: str
    upstream_repo: str
    current_tag: str
    target_tag: str
    target_digest: str = ""
    error: str = ""


@dataclass(frozen=True)
class GitHubLatestCandidate:
    release_tag: str
    link_label: str
    link_url: str


ReleaseNoteSourceResolver = Callable[[WudTarget], str]
ReleaseNoteTargetTagResolver = Callable[[WudTarget], str]


class GitHubClient:
    """Small GitHub Releases API client using only the standard library."""

    def __init__(
        self,
        *,
        token: str = "",
        timeout: float = DEFAULT_GITHUB_TIMEOUT_SECONDS,
        fetch_json: Callable[[str], object] | None = None,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self._fetch_json = fetch_json

    def get_json(self, url: str) -> object:
        if self._fetch_json is not None:
            return self._fetch_json(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "wudup-webui-release-notes/1.0",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"message": "Not Found"}
            raise

    def repository_advisory(self, repo: str, advisory_id: str) -> object:
        if not _github_repo_valid(repo) or not GHSA_ID_RE.fullmatch(advisory_id):
            raise ValueError("invalid GitHub repository advisory lookup")
        return self.get_json(
            f"https://api.github.com/repos/{repo}/security-advisories/"
            f"{advisory_id.upper()}"
        )

    def global_advisory(self, advisory_id: str) -> object:
        if not GHSA_ID_RE.fullmatch(advisory_id):
            raise ValueError("invalid GitHub advisory lookup")
        return self.get_json(
            f"https://api.github.com/advisories/{advisory_id.upper()}"
        )

    def global_advisories_for_cve(self, cve_id: str) -> object:
        if not CVE_ID_RE.fullmatch(cve_id):
            raise ValueError("invalid CVE advisory lookup")
        query = urllib.parse.urlencode(
            {"cve_id": cve_id.upper(), "per_page": str(SECURITY_ADVISORY_FETCH_MAX)}
        )
        return self.get_json(f"https://api.github.com/advisories?{query}")


def cached_release_notes(
    conn: sqlite3.Connection,
    targets: Iterable[WudTarget],
    environ: Mapping[str, str],
    *,
    source_resolver: ReleaseNoteSourceResolver | None = None,
    target_tag_resolver: ReleaseNoteTargetTagResolver | None = None,
) -> list[ReleaseNoteInfo]:
    """Return cached release-note metadata without touching the network."""

    contexts = release_note_contexts(
        targets,
        environ,
        source_resolver=source_resolver,
        target_tag_resolver=target_tag_resolver,
    )
    return [_cached_info(conn, context) for context in contexts]


def release_note_placeholders(
    targets: Iterable[WudTarget],
    environ: Mapping[str, str],
    *,
    source_resolver: ReleaseNoteSourceResolver | None = None,
    target_tag_resolver: ReleaseNoteTargetTagResolver | None = None,
) -> list[ReleaseNoteInfo]:
    """Return missing/unsupported metadata without requiring a database."""

    return [
        _placeholder_info(context)
        for context in release_note_contexts(
            targets,
            environ,
            source_resolver=source_resolver,
            target_tag_resolver=target_tag_resolver,
        )
    ]


def refresh_release_notes(
    conn: sqlite3.Connection,
    targets: Iterable[WudTarget],
    environ: Mapping[str, str],
    *,
    client: GitHubClient | None = None,
    now: str | None = None,
    source_resolver: ReleaseNoteSourceResolver | None = None,
    target_tag_resolver: ReleaseNoteTargetTagResolver | None = None,
    redact_error: Callable[[str], str] | None = None,
    force: bool = False,
) -> list[ReleaseNoteInfo]:
    """Refresh missing or stale release-note metadata and return current rows."""

    active_client = client or GitHubClient(token=environ.get("GITHUB_TOKEN", ""))
    timestamp = now or utc_timestamp()
    infos: list[ReleaseNoteInfo] = []
    contexts = release_note_contexts(
        targets,
        environ,
        source_resolver=source_resolver,
        target_tag_resolver=target_tag_resolver,
    )
    _prune_digest_cache(conn, contexts)
    for context in contexts:
        cached = _cached_info(conn, context)
        legacy_metadata_cache = (
            cached.status != "missing"
            and _cache_metadata_incomplete(conn, context)
        )
        if context.provider == "unsupported":
            infos.append(cached)
            continue
        if (
            not force
            and cached.status != "missing"
            and not legacy_metadata_cache
            and not _cache_stale(cached, timestamp)
        ):
            infos.append(cached)
            continue
        try:
            info = _fetch_release_note(context, active_client, timestamp)
        except Exception as exc:  # noqa: BLE001 - surfaced as structured metadata.
            if cached.status == "ready" and (
                legacy_metadata_cache
                or cached.security.reason_code
                == SECURITY_BACKFILL_FAILURE_REASON_CODE
            ):
                info = replace(
                    cached,
                    refreshed_at=timestamp,
                    security=ReleaseSecurityAssessment(
                        outcome="needs_review",
                        severity="unknown",
                        reason_code=SECURITY_BACKFILL_FAILURE_REASON_CODE,
                        reason=(
                            "Existing release metadata was preserved, but GitHub "
                            "security evidence could not be checked."
                        ),
                    ),
                )
            else:
                error = str(exc)
                if redact_error is not None:
                    error = redact_error(error)
                info = ReleaseNoteInfo(
                    line_no=context.line_no,
                    status="error",
                    provider=context.provider,
                    image_repo=context.image_repo,
                    upstream_repo=context.upstream_repo,
                    refreshed_at=timestamp,
                    error=error,
                )
        _upsert_cache(conn, context, info, timestamp)
        infos.append(info)
    return infos


def release_note_contexts(
    targets: Iterable[WudTarget],
    environ: Mapping[str, str],
    *,
    source_resolver: ReleaseNoteSourceResolver | None = None,
    target_tag_resolver: ReleaseNoteTargetTagResolver | None = None,
) -> list[ReleaseNoteContext]:
    upstreams = _load_upstream_map(environ)
    contexts: list[ReleaseNoteContext] = []
    for target in targets:
        current_tag = image_tag(target.first)
        target_tag = target.desired_tag or (
            target_tag_resolver(target) if target_tag_resolver is not None else ""
        )
        lsio_repo = _lsio_repo(target.repo)
        if lsio_repo:
            upstream_repo = upstreams.get(lsio_repo, "")
            if not upstream_repo:
                contexts.append(
                    _context(
                        target,
                        provider="unsupported",
                        image_repo=lsio_repo,
                        upstream_repo="",
                        current_tag=current_tag,
                        target_tag=target_tag,
                        error=f"missing LSIO upstream mapping for {lsio_repo}",
                    )
                )
                continue
            contexts.append(
                _context(
                    target,
                    provider="lsio",
                    image_repo=lsio_repo,
                    upstream_repo=upstream_repo,
                    current_tag=current_tag,
                    target_tag=target_tag,
                )
            )
            continue

        source_repo = github_repo_from_source(
            source_resolver(target) if source_resolver is not None else ""
        )
        if source_repo:
            contexts.append(
                _context(
                    target,
                    provider="github",
                    image_repo=source_repo,
                    upstream_repo=source_repo,
                    current_tag=current_tag,
                    target_tag=target_tag,
                )
            )
            continue

        ghcr_repo = github_repo_from_ghcr_image(target.first)
        if ghcr_repo:
            contexts.append(
                _context(
                    target,
                    provider="github",
                    image_repo=ghcr_repo,
                    upstream_repo=ghcr_repo,
                    current_tag=current_tag,
                    target_tag=target_tag,
                )
            )
            continue

        contexts.append(
            _context(
                target,
                provider="unsupported",
                image_repo=image_repo_ref(target.first),
                upstream_repo="",
                current_tag=current_tag,
                target_tag=target_tag,
                error="no supported GitHub release source found",
            )
        )
    return contexts


def detect_breaking(body: str, current_tag: str, release_tag: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if BREAKING_RE.search(body):
        reasons.append("Release notes mention a migration, incompatibility, or removal.")
    current_major = _semver_major(current_tag)
    release_major = _semver_major(release_tag)
    if (
        current_major is not None
        and release_major is not None
        and release_major > current_major
    ):
        reasons.append(f"Major version changes from {current_major} to {release_major}.")
    return bool(reasons), reasons


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
        vulnerable_range = str(vulnerability.get("vulnerable_version_range") or "")
        if not _version_in_range(current, vulnerable_range):
            continue
        if _version_in_range(target, vulnerable_range):
            continue
        patched_versions = _patched_versions(vulnerability)
        if patched_versions and any(target >= patched for patched in patched_versions):
            return True
    return False


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


def _unique_links(links: Iterable[ReleaseNoteLink]) -> list[ReleaseNoteLink]:
    unique: list[ReleaseNoteLink] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        key = (link.kind, link.url)
        if not link.url or key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def github_latest_candidate_from_info(
    info: ReleaseNoteInfo,
) -> GitHubLatestCandidate | None:
    """Return a Docker-retag candidate from GitHub release-note metadata."""

    if info.provider == "lsio" and info.status in {"ready", "not_found"}:
        release_link = next(
            (link for link in info.links if link.kind == "lsio_release"),
            None,
        )
        if release_link is None:
            return None
        release_tag = _github_release_link_tag(release_link.url)
        if not release_tag:
            return None
        return GitHubLatestCandidate(
            release_tag=release_tag,
            link_label=release_link.label,
            link_url=release_link.url,
        )

    if info.provider != "github" or info.status != "ready" or not info.release_tag:
        return None
    release_link = next(
        (link for link in info.links if link.kind == "github_release"),
        None,
    )
    if release_link is None and info.links:
        release_link = info.links[0]
    return GitHubLatestCandidate(
        release_tag=info.release_tag,
        link_label="" if release_link is None else release_link.label,
        link_url="" if release_link is None else release_link.url,
    )


def _github_release_link_tag(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() != "github.com":
        return ""
    marker = "/releases/tag/"
    if marker not in parsed.path:
        return ""
    tag = parsed.path.split(marker, 1)[1].strip("/")
    return urllib.parse.unquote(tag)


def _context(
    target: WudTarget,
    *,
    provider: str,
    image_repo: str,
    upstream_repo: str,
    current_tag: str,
    target_tag: str,
    error: str = "",
) -> ReleaseNoteContext:
    key_payload = {
        "provider": provider,
        "image_repo": image_repo,
        "upstream_repo": upstream_repo,
        "current_tag": current_tag,
        "target_tag": target_tag,
    }
    if target.digest:
        key_payload["target_digest"] = target.digest
    cache_key = hashlib.sha256(
        json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReleaseNoteContext(
        line_no=target.line_no,
        cache_key=cache_key,
        provider=provider,
        image_repo=image_repo,
        upstream_repo=upstream_repo,
        current_tag=current_tag,
        target_tag=target_tag,
        target_digest=target.digest,
        error=error,
    )


def _cached_info(conn: sqlite3.Connection, context: ReleaseNoteContext) -> ReleaseNoteInfo:
    if context.provider == "unsupported":
        return _placeholder_info(context)
    row = conn.execute(
        """
        SELECT *
        FROM release_note_cache
        WHERE cache_key = ?
        """,
        (context.cache_key,),
    ).fetchone()
    if row is None:
        return _placeholder_info(context)
    return _row_to_info(row, line_no=context.line_no)


def _placeholder_info(context: ReleaseNoteContext) -> ReleaseNoteInfo:
    if context.provider == "unsupported":
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="unsupported",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            error=context.error,
            classification=_classify_context(context),
        )
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="missing",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        classification=_classify_context(context),
    )


def _row_to_info(row: sqlite3.Row, *, line_no: int) -> ReleaseNoteInfo:
    metadata = _json_object(str(row["metadata_json"]))
    classification = classification_from_mapping(metadata.get("classification"))
    if "classification" not in metadata:
        classification = _classify_cache_row(row)
    security = security_assessment_from_mapping(metadata.get("security"))
    return ReleaseNoteInfo(
        line_no=line_no,
        status=str(row["status"]),  # type: ignore[arg-type]
        provider=str(row["provider"]),
        image_repo=str(row["image_repo"]),
        upstream_repo=str(row["upstream_repo"]),
        release_tag=str(row["release_tag"]),
        title=str(row["title"]),
        published_at=str(row["published_at"]),
        breaking=bool(row["breaking"]),
        breaking_reasons=_json_list(str(row["breaking_reasons_json"])),
        links=[
            ReleaseNoteLink(
                label=str(item.get("label", "")),
                url=str(item.get("url", "")),
                kind=str(item.get("kind", "")),
            )
            for item in _json_object_list(str(row["links_json"]))
        ],
        refreshed_at=str(row["updated_at"]),
        error=str(row["error"]),
        body=str(row["body"]),
        classification=classification,
        security=security,
    )


def _cache_metadata_incomplete(
    conn: sqlite3.Connection,
    context: ReleaseNoteContext,
) -> bool:
    row = conn.execute(
        "SELECT metadata_json FROM release_note_cache WHERE cache_key = ?",
        (context.cache_key,),
    ).fetchone()
    if row is None:
        return False
    metadata = _json_object(str(row["metadata_json"]))
    return "classification" not in metadata or "security" not in metadata


def _classify_cache_row(row: sqlite3.Row) -> LSIOUpdateClassification:
    return classify_lsio_update(
        image_repo=str(row["image_repo"]),
        current_tag=str(row["current_tag"]),
        target_tag=str(row["target_tag"]),
        lsio_tag=_lsio_release_tag_from_links(str(row["links_json"])),
        upstream_version=str(row["release_tag"]),
    )


def _lsio_release_tag_from_links(raw: str) -> str:
    for item in _json_object_list(raw):
        if str(item.get("kind") or "") == "lsio_release":
            tag = _github_release_link_tag(str(item.get("url") or ""))
            if tag:
                return tag
    return ""


def _prune_digest_cache(
    conn: sqlite3.Connection,
    contexts: Iterable[ReleaseNoteContext],
) -> None:
    active_digests: dict[tuple[str, str, str, str, str], set[str]] = {}
    for context in contexts:
        if context.target_digest:
            identity = (
                context.provider,
                context.image_repo,
                context.upstream_repo,
                context.current_tag,
                context.target_tag,
            )
            active_digests.setdefault(identity, set()).add(context.target_digest)

    with conn:
        for identity, digests in active_digests.items():
            rows = conn.execute(
                """
                SELECT cache_key, target_digest
                FROM release_note_cache
                WHERE provider = ?
                  AND image_repo = ?
                  AND upstream_repo = ?
                  AND current_tag = ?
                  AND target_tag = ?
                  AND target_digest != ''
                """,
                identity,
            )
            conn.executemany(
                "DELETE FROM release_note_cache WHERE cache_key = ?",
                ((row["cache_key"],) for row in rows if row["target_digest"] not in digests),
            )


def _upsert_cache(
    conn: sqlite3.Connection,
    context: ReleaseNoteContext,
    info: ReleaseNoteInfo,
    timestamp: str,
) -> None:
    links_json = json.dumps([asdict(link) for link in info.links], sort_keys=True)
    reasons_json = json.dumps(info.breaking_reasons, sort_keys=True)
    metadata_json = json.dumps(
        {
            "line_no": context.line_no,
            "classification": asdict(info.classification),
            "security": asdict(info.security),
        },
        sort_keys=True,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO release_note_cache (
                cache_key,
                provider,
                image_repo,
                upstream_repo,
                current_tag,
                target_tag,
                status,
                release_tag,
                title,
                published_at,
                breaking,
                breaking_reasons_json,
                links_json,
                error,
                body,
                created_at,
                updated_at,
                metadata_json,
                target_digest
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                provider = excluded.provider,
                image_repo = excluded.image_repo,
                upstream_repo = excluded.upstream_repo,
                current_tag = excluded.current_tag,
                target_tag = excluded.target_tag,
                status = excluded.status,
                release_tag = excluded.release_tag,
                title = excluded.title,
                published_at = excluded.published_at,
                breaking = excluded.breaking,
                breaking_reasons_json = excluded.breaking_reasons_json,
                links_json = excluded.links_json,
                error = excluded.error,
                body = excluded.body,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json,
                target_digest = excluded.target_digest
            """,
            (
                context.cache_key,
                context.provider,
                context.image_repo,
                context.upstream_repo,
                context.current_tag,
                context.target_tag,
                info.status,
                info.release_tag,
                info.title,
                info.published_at,
                1 if info.breaking else 0,
                reasons_json,
                links_json,
                info.error,
                info.body,
                timestamp,
                timestamp,
                metadata_json,
                context.target_digest,
            ),
        )


def _cache_stale(info: ReleaseNoteInfo, now: str) -> bool:
    if not info.refreshed_at:
        return True
    try:
        updated = datetime.fromisoformat(info.refreshed_at)
        current = datetime.fromisoformat(now)
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    retryable_security = info.security.reason_code in SECURITY_RETRYABLE_REASON_CODES
    ttl = (
        ERROR_CACHE_TTL_SECONDS
        if info.status == "error" or retryable_security
        else SUCCESS_CACHE_TTL_SECONDS
    )
    return (current - updated).total_seconds() >= ttl


def _fetch_release_note(
    context: ReleaseNoteContext,
    client: GitHubClient,
    timestamp: str,
) -> ReleaseNoteInfo:
    if context.provider == "lsio":
        info = _fetch_lsio_release_note(context, client, timestamp)
    else:
        info = _fetch_github_release_note(context, client, timestamp)
    if info.status != "ready":
        return info
    security, advisory_links = assess_release_security(context, info, client)
    return replace(
        info,
        security=security,
        links=_unique_links([*info.links, *advisory_links]),
    )


def _fetch_github_release_note(
    context: ReleaseNoteContext,
    client: GitHubClient,
    timestamp: str,
) -> ReleaseNoteInfo:
    release = _fetch_release(client, context.upstream_repo, context.target_tag)
    if release is None:
        project_url = _project_url(client, context.upstream_repo)
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="not_found",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            release_tag=context.target_tag,
            title=f"{context.upstream_repo} releases",
            links=[ReleaseNoteLink("GitHub project", project_url, "github_project")],
            refreshed_at=timestamp,
        )
    body = str(release.get("body") or "")
    release_tag = str(release.get("tag_name") or "")
    breaking, reasons = detect_breaking(body, context.current_tag, release_tag)
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=release_tag,
        title=str(release.get("name") or release_tag),
        published_at=str(release.get("published_at") or release.get("created_at") or ""),
        breaking=breaking,
        breaking_reasons=reasons,
        links=[
            ReleaseNoteLink(
                "GitHub release",
                str(release.get("html_url") or _github_url(context.upstream_repo)),
                "github_release",
            )
        ],
        refreshed_at=timestamp,
        body=body,
    )


def _fetch_lsio_release_note(
    context: ReleaseNoteContext,
    client: GitHubClient,
    timestamp: str,
) -> ReleaseNoteInfo:
    lsio_release = _fetch_lsio_release(client, context)
    if lsio_release is None:
        return ReleaseNoteInfo(
            line_no=context.line_no,
            status="error",
            provider=context.provider,
            image_repo=context.image_repo,
            upstream_repo=context.upstream_repo,
            refreshed_at=timestamp,
            error=f"LSIO release not found for {context.image_repo}",
            classification=_classify_context(context),
        )
    lsio_body = str(lsio_release.get("body") or "")
    lsio_tag = str(lsio_release.get("tag_name") or "")
    upstream_version = _lsio_context_upstream_version(context, lsio_body, lsio_tag)
    classification = _classify_context(
        context,
        lsio_tag=lsio_tag,
        upstream_version=upstream_version,
    )
    links = [
        ReleaseNoteLink(
            "LSIO release",
            str(lsio_release.get("html_url") or _github_url(context.image_repo)),
            "lsio_release",
        )
    ]
    breaking, reasons = detect_breaking(lsio_body, context.current_tag, lsio_tag)
    lsio_info = ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=lsio_tag,
        title=str(lsio_release.get("name") or lsio_tag),
        published_at=str(
            lsio_release.get("published_at")
            or lsio_release.get("created_at")
            or ""
        ),
        breaking=breaking,
        breaking_reasons=reasons,
        links=links,
        refreshed_at=timestamp,
        body=lsio_body,
        classification=classification,
    )
    if context.upstream_repo == context.image_repo or _lsio_only_update(
        context, classification
    ):
        return lsio_info
    upstream_release = _fetch_lsio_upstream_release(
        client,
        context.upstream_repo,
        upstream_version,
    )
    if upstream_release is None:
        return lsio_info
    body = str(upstream_release.get("body") or "")
    release_tag = str(upstream_release.get("tag_name") or "")
    breaking, reasons = detect_breaking(
        "\n".join([body, lsio_body]),
        context.current_tag,
        release_tag,
    )
    links.append(
        ReleaseNoteLink(
            "Upstream release",
            str(upstream_release.get("html_url") or _github_url(context.upstream_repo)),
            "github_release",
        )
    )
    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="ready",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        release_tag=release_tag,
        title=str(upstream_release.get("name") or release_tag),
        published_at=str(
            upstream_release.get("published_at")
            or upstream_release.get("created_at")
            or ""
        ),
        breaking=breaking,
        breaking_reasons=reasons,
        links=links,
        refreshed_at=timestamp,
        body="\n\n".join(part for part in (body, lsio_body) if part),
        classification=classification,
    )


def _lsio_only_update(
    context: ReleaseNoteContext,
    classification: LSIOUpdateClassification,
) -> bool:
    """Return true for classified rebuilds and same-tag digest-only updates."""

    if classification.change_type == "image_rebuild":
        return True
    return bool(
        context.target_digest
        and context.current_tag
        and context.current_tag.lower() == context.target_tag.lower()
    )


def _fetch_lsio_release(
    client: GitHubClient,
    context: ReleaseNoteContext,
) -> dict[str, Any] | None:
    arch, branch, upstream_version, build_suffix = _lsio_release_target(context)
    target = parse_lsio_tag(context.target_tag)
    if not branch:
        if target.kind == "build" and target.build_suffix:
            url = (
                f"https://api.github.com/repos/{context.image_repo}/"
                f"releases/tags/{context.target_tag}"
            )
            return _object_or_none(client.get_json(url))
        if not arch:
            return _fetch_latest(client, context.image_repo)
    for page in range(1, LSIO_RELEASE_SCAN_MAX_PAGES + 1):
        releases = _object_list(
            client.get_json(_lsio_releases_url(context.image_repo, page))
        )
        release = _matching_lsio_release(
            releases,
            arch=arch,
            branch=branch,
            upstream_version=upstream_version,
            build_suffix=build_suffix,
        )
        if release is not None:
            return release
        if len(releases) < 30:
            return None
    return None


def _matching_lsio_release(
    releases: Iterable[dict[str, Any]],
    *,
    arch: str,
    branch: str,
    upstream_version: str,
    build_suffix: str,
) -> dict[str, Any] | None:
    for release in releases:
        parts = parse_lsio_tag(str(release.get("tag_name") or ""))
        if parts.arch.lower() != arch.lower():
            continue
        if parts.branch != branch:
            continue
        if upstream_version and normalize_lsio_version(
            parts.upstream_version
        ) != normalize_lsio_version(upstream_version):
            continue
        if build_suffix and parts.build_suffix != build_suffix:
            continue
        return release
    return None


def _lsio_releases_url(repo: str, page: int) -> str:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    if page > 1:
        url = f"{url}&page={page}"
    return url


def _lsio_release_target(context: ReleaseNoteContext) -> tuple[str, str, str, str]:
    target = parse_lsio_tag(context.target_tag)
    if target.kind in {"build", "version"} and (target.arch or target.branch):
        return (
            target.arch,
            target.branch,
            target.upstream_version,
            target.build_suffix,
        )
    arch, branch = _lsio_tracking_target(context)
    return arch, branch, "", ""


def _lsio_tracking_target(context: ReleaseNoteContext) -> tuple[str, str]:
    for tag in (context.target_tag, context.current_tag):
        parts = parse_lsio_tag(tag)
        if parts.kind in {"build", "version"} and (parts.arch or parts.branch):
            return parts.arch, parts.branch
    return "", ""


def _lsio_context_upstream_version(
    context: ReleaseNoteContext,
    lsio_body: str,
    lsio_tag: str,
) -> str:
    target = parse_lsio_tag(context.target_tag)
    if target.kind in {"build", "version"}:
        return target.upstream_version
    return _lsio_upstream_version(lsio_body, lsio_tag)


def _fetch_lsio_upstream_release(
    client: GitHubClient,
    repo: str,
    tag: str,
) -> dict[str, Any] | None:
    release = _fetch_release(client, repo, tag)
    if release is not None:
        return release
    fallback = _composite_upstream_base(tag)
    if fallback and fallback != tag:
        return _fetch_release(client, repo, fallback)
    return None


def _classify_context(
    context: ReleaseNoteContext,
    *,
    lsio_tag: str = "",
    upstream_version: str = "",
) -> LSIOUpdateClassification:
    return classify_lsio_update(
        image_repo=context.image_repo,
        current_tag=context.current_tag,
        target_tag=context.target_tag,
        lsio_tag=lsio_tag,
        upstream_version=upstream_version,
    )


def _fetch_release(
    client: GitHubClient,
    repo: str,
    tag: str,
) -> dict[str, Any] | None:
    if tag.lower() == "latest":
        return _fetch_latest(client, repo)
    if tag:
        candidates = [tag] if tag[:1].lower() == "v" else [f"v{tag}", tag]
        for candidate in candidates:
            release = _object_or_none(
                client.get_json(f"https://api.github.com/repos/{repo}/releases/tags/{candidate}")
            )
            if release is not None:
                return release
        return None
    return _fetch_latest(client, repo)


def _fetch_latest(client: GitHubClient, repo: str) -> dict[str, Any] | None:
    return _object_or_none(
        client.get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    )


def _project_url(client: GitHubClient, repo: str) -> str:
    repo_json = _object_or_none(client.get_json(f"https://api.github.com/repos/{repo}"))
    if repo_json is not None:
        html_url = str(repo_json.get("html_url") or "")
        if html_url.startswith("https://github.com/"):
            return html_url
    return _github_url(repo)


def _object_or_none(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("message") or "") == "Not Found":
        return None
    return value


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _lsio_upstream_version(body: str, lsio_tag: str) -> str:
    remote = _extract_block(body, "remote changes:")
    match = re.search(r"[Uu]pdat(?:e|ing)[^0-9vV]*(v?[0-9][0-9A-Za-z._-]*)", remote)
    if match:
        found = _first_semver(match.group(1))
        if found:
            return found
    return _strip_lsio_suffix(_first_semver(lsio_tag))


def _extract_block(body: str, header: str) -> str:
    target = header.lower()
    lines = body.replace("\r", "").splitlines()
    collecting = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        header = _block_header_text(stripped)
        is_header = bool(
            header
            and not _markdown_bullet(stripped)
            and re.fullmatch(r"[A-Za-z0-9 _-]+:", header)
        )
        if header.lower() == target:
            collecting = True
            result.append(line)
            continue
        if collecting and is_header:
            break
        if collecting:
            result.append(line)
    return "\n".join(result)


def _block_header_text(value: str) -> str:
    header = re.sub(r"^#{1,6}\s*", "", value.strip())
    match = re.fullmatch(r"\*\*([^*]+:)\*\*", header)
    if match:
        header = match.group(1)
    return header.strip()


def _markdown_bullet(value: str) -> bool:
    return bool(re.match(r"^([*+-]|•)\s+", value))


def _load_upstream_map(environ: Mapping[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in _upstream_map_paths(environ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if _github_repo_valid(key) and _github_repo_valid(value):
                mapping[key] = value
        if mapping:
            return mapping
    return mapping


def _upstream_map_paths(environ: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name in ("WUD_WEB_UPSTREAM_MAP", "UPSTREAM_MAP"):
        value = environ.get(name, "")
        if value:
            paths.append(Path(value))
    scripts_dir = environ.get("WUD_SCRIPTS_DIR", "/managed-wud")
    if scripts_dir:
        paths.append(Path(scripts_dir) / "upstreams.txt")
    app_dir = environ.get("WUD_APP_DIR", "/app")
    if app_dir:
        paths.append(Path(app_dir) / "wud" / "upstreams.txt")
    paths.append(Path("/app/wud/upstreams.txt"))
    paths.append(Path(__file__).resolve().parents[2] / "wud" / "upstreams.txt")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _lsio_repo(repo: str) -> str:
    if not repo.startswith("linuxserver/"):
        return ""
    name = repo.split("/", 1)[1]
    if not name:
        return ""
    if name.startswith("docker-"):
        return f"linuxserver/{name}"
    return f"linuxserver/docker-{name}"


def github_repo_from_ghcr_image(image: str) -> str:
    repo = image_repo_ref(image)
    registry, sep, candidate = repo.partition("/")
    if registry.lower() != "ghcr.io" or not sep:
        return ""
    return candidate if _github_repo_valid(candidate) else ""


def github_repo_from_source(source: str) -> str:
    return _github_source_repo(source)


def _github_source_repo(source: str) -> str:
    value = source.strip()
    if not value or "github.com" not in value.lower():
        return ""
    if value.startswith("git@github.com:"):
        candidate = value.removeprefix("git@github.com:")
    else:
        parse_value = value if "://" in value else f"//{value}"
        parsed = urllib.parse.urlsplit(parse_value)
        if parsed.netloc.lower() != "github.com":
            return ""
        candidate = parsed.path.lstrip("/")
    candidate = candidate.removesuffix(".git").strip("/")
    parts = [part for part in candidate.split("/") if part]
    if len(parts) < 2:
        return ""
    repo = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return repo if _github_repo_valid(repo) else ""


def _github_repo_valid(value: str) -> bool:
    return bool(GITHUB_REPO_RE.fullmatch(value))


def _github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _first_semver(value: str) -> str:
    match = SEMVER_RE.search(value)
    return match.group(0) if match else ""


def _strip_lsio_suffix(value: str) -> str:
    return re.sub(r"(?i)[._-]ls[0-9]+(?:[._-][0-9A-Za-z]+)*$", "", value)


def _composite_upstream_base(value: str) -> str:
    match = COMPOSITE_UPSTREAM_RE.match(value)
    return match.group(1) if match else ""


def _semver_major(value: str) -> int | None:
    match = SEMVER_RE.search(value)
    if not match:
        return None
    return int(match.group(1))


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_object_list(raw: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _json_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
