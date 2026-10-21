"""Shared digest-pin planning helpers for updater and WebUI plans."""

from __future__ import annotations

from collections.abc import Sequence

from .compose_rewrite import (
    DIGEST_PIN_MARKER_PREFIX,
    WUD_TAG_INCLUDE_LABEL,
    compose_escape_dollars,
    exact_tags_regex,
)
from .digest_verifier import DigestResolveResult, DigestVerifier
from .images import (
    image_tag,
    image_with_digest,
    image_with_tag,
    normalize_digest,
)
from .updater_models import (
    DigestPinCandidate,
    DigestPinUpdate,
    Match,
    TagUpdate,
    UpdaterError,
)
from .wud_file import is_digest_target_line


def digest_pin_update_from_values(
    *,
    old_image: str,
    resolved_tag: str,
    planned_digest: str,
    services: Sequence[str],
) -> DigestPinUpdate:
    tag_update = TagUpdate(
        old_image=old_image,
        desired_tag=resolved_tag,
        new_image=image_with_tag(old_image, resolved_tag),
        services=tuple(sorted(services)),
    )
    return _digest_pin_update_from_tag_update(tag_update, planned_digest)


def _digest_pin_match_tag(match: Match) -> str:
    if match.target.desired_tag:
        return match.target.desired_tag
    if not is_digest_target_line(match.target):
        return ""
    return image_tag(match.target.first)


def _digest_pin_candidates(
    matches: Sequence[Match],
) -> tuple[DigestPinCandidate, ...]:
    services_by_key: dict[tuple[str, str, str], set[str]] = {}
    digests_by_key: dict[tuple[str, str, str], set[str]] = {}
    for match in matches:
        resolved_tag = _digest_pin_match_tag(match)
        if not resolved_tag:
            continue
        resolved_image = image_with_tag(match.compose_image, resolved_tag)
        key = (match.compose_image, resolved_tag, resolved_image)
        services_by_key.setdefault(key, set())
        if match.service:
            services_by_key[key].add(match.service)
        if not match.target.desired_tag:
            digests_by_key.setdefault(key, set()).add(
                normalize_digest(match.target.digest)
            )

    candidates: list[DigestPinCandidate] = []
    for key, services in sorted(services_by_key.items()):
        old_image, resolved_tag, resolved_image = key
        digests = sorted(digests_by_key.get(key, set()))
        if len(digests) > 1:
            raise UpdaterError(
                "Conflicting digest-pin digests for "
                f"{resolved_image}: {', '.join(digests)}"
            )
        candidates.append(
            DigestPinCandidate(
                old_image=old_image,
                resolved_tag=resolved_tag,
                resolved_image=resolved_image,
                planned_digest=digests[0] if digests else "",
                services=tuple(sorted(services)),
            )
        )
    return tuple(candidates)


def _digest_pin_tag_materialization_updates(
    updates: Sequence[DigestPinUpdate],
) -> tuple[TagUpdate, ...]:
    tag_updates: list[TagUpdate] = []
    for update in updates:
        if (
            update.old_image == update.resolved_image
            or "@sha256:" not in update.old_image
        ):
            continue
        tag_updates.append(
            TagUpdate(
                old_image=update.old_image,
                desired_tag=update.resolved_tag,
                new_image=update.resolved_image,
                services=update.services,
            )
        )
    return tuple(tag_updates)


def _resolve_digest_pin_candidate(
    verifier: DigestVerifier,
    candidate: DigestPinCandidate,
) -> DigestResolveResult:
    if candidate.planned_digest:
        return verifier.verify_tag_digest(
            candidate.resolved_image,
            candidate.planned_digest,
        )
    return verifier.resolve_tag_digest(candidate.resolved_image)


def _digest_pin_resolve_error(
    resolved_image: str,
    result: DigestResolveResult,
) -> str:
    if result.reason == "stale-digest":
        current = f", current {normalize_digest(result.digest)}" if result.digest else ""
        return (
            f"Digest-pin target moved for {resolved_image}: "
            f"planned digest is no longer current{current}"
        )
    if result.reason == "tag-digest-unavailable":
        return (
            f"Could not confirm digest-pin target for {resolved_image}: the "
            "registry did not report which digest the tag points to, and the "
            "planned digest could not be read for comparison. Check that the "
            "registry is reachable and retry"
        )
    return f"Could not resolve digest-pin target for {resolved_image}: {result.reason}"


def _digest_pin_update_from_tag_update(
    update: TagUpdate,
    planned_digest: str,
) -> DigestPinUpdate:
    digest = normalize_digest(planned_digest)
    watch_tag = update.desired_tag
    return DigestPinUpdate(
        old_image=update.old_image,
        resolved_tag=update.desired_tag,
        resolved_image=update.new_image,
        planned_digest=digest,
        final_image=image_with_digest(update.old_image, digest),
        watch_tag=watch_tag,
        marker=f"{DIGEST_PIN_MARKER_PREFIX}{watch_tag}",
        label_key=WUD_TAG_INCLUDE_LABEL,
        label_value=compose_escape_dollars(exact_tags_regex((watch_tag,))),
        services=update.services,
    )
