"""Structured release-note API, context construction, and orchestration.

The public imports remain stable while provider fetching, advisory assessment,
and SQLite cache policy each have a dedicated owner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace

from .db import utc_timestamp
from .images import image_repo_ref, image_tag
from .release_note_cache import (
    ERROR_CACHE_TTL_SECONDS,  # noqa: F401 - compatibility re-export
    SUCCESS_CACHE_TTL_SECONDS,  # noqa: F401 - compatibility re-export
    _cache_metadata_incomplete,
    _cache_stale,
    _cached_info,
    _placeholder_info,
    _prune_digest_cache,
    _upsert_cache,
)
from .release_note_models import (
    GitHubLatestCandidate,
    ReleaseNoteContext,
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseNoteSourceResolver,
    ReleaseNoteStatus,  # noqa: F401 - compatibility re-export
    ReleaseNoteTargetTagResolver,
    ReleaseSecurityAssessment,
    ReleaseSecurityOutcome,  # noqa: F401 - compatibility re-export
    ReleaseSecuritySeverity,  # noqa: F401 - compatibility re-export
)
from .release_note_providers import (
    BREAKING_RE,  # noqa: F401 - compatibility re-export
    COMPOSITE_UPSTREAM_RE,  # noqa: F401 - compatibility re-export
    CVE_ID_RE,  # noqa: F401 - compatibility re-export
    DEFAULT_GITHUB_TIMEOUT_SECONDS,  # noqa: F401 - compatibility re-export
    GHSA_ID_RE,  # noqa: F401 - compatibility re-export
    GITHUB_REPO_RE,  # noqa: F401 - compatibility re-export
    LSIO_RELEASE_SCAN_MAX_PAGES,  # noqa: F401 - compatibility re-export
    SECURITY_ADVISORY_FETCH_MAX,  # noqa: F401 - compatibility re-export
    SEMVER_RE,  # noqa: F401 - compatibility re-export
    GitHubClient,
    _fetch_github_release_note,
    _fetch_lsio_release,  # noqa: F401 - compatibility re-export
    _fetch_lsio_release_note,
    _github_release_link_tag,
    _load_upstream_map,
    _lsio_repo,
    detect_breaking,  # noqa: F401 - compatibility re-export
    github_repo_from_ghcr_image,
    github_repo_from_source,
)
from .release_note_security import (
    ADVISORY_ID_RE,  # noqa: F401 - compatibility re-export
    SECURITY_ADVISORY_ID_MAX,  # noqa: F401 - compatibility re-export
    SECURITY_BACKFILL_FAILURE_REASON_CODE,
    SECURITY_RETRYABLE_REASON_CODES,  # noqa: F401 - compatibility re-export
    SECURITY_SIGNAL_RE,  # noqa: F401 - compatibility re-export
    SECURITY_SIGNAL_SCAN_MAX_CHARS,  # noqa: F401 - compatibility re-export
    STRICT_VERSION_RE,  # noqa: F401 - compatibility re-export
    VERSION_COMPARISON_RE,  # noqa: F401 - compatibility re-export
    _version_in_range,  # noqa: F401 - compatibility re-export
    assess_release_security,
    security_assessment_from_mapping,  # noqa: F401 - compatibility re-export
)
from .wud_file import WudTarget

OCI_SOURCE_LABEL = "org.opencontainers.image.source"


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
