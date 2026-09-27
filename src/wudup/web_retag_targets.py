"""Compose retag target discovery, records, and eligibility.

Shared by the retag review routes in ``web_retags`` and the tracked Compose
inventory in ``web_tracking``. Owns the configured effective-config and
retag digest-pin loaders read during discovery.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

from . import web_database
from .compose import (
    ComposeCli,
    ComposeDiscoveryError,
    ComposeStack,
    ServiceImage,
)
from .compose_rewrite import (
    WUD_TAG_INCLUDE_LABEL,
    compose_unescape_dollars,
)
from .config import ConfigError, UpdaterConfig
from .digest_provenance import DigestTagProvenance
from .images import (
    image_repo_ref,
    image_tag,
    image_with_tag,
    repo_key,
    tag_value_valid,
)
from .web_models import (
    RetagRuntimeState,
    RetagTargetItem,
    RetagTargetsResponse,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_retag_identity import retag_target_id as _retag_target_id_from_values
from .web_retag_runtime import (
    _command_runner,
    _retag_compose_service_key,
    _running_retag_compose_service_keys,
)

if TYPE_CHECKING:
    from .web_retags import _RetagGitHubLatestFallback

KEEP_CURRENT_CHOICE = "keep-current"
SWITCH_TO_CONCRETE_CHOICE = "switch-to-concrete"
GITHUB_LATEST_MISSING_CACHE_WARNING = (
    "GitHub latest fallback is enabled, but no cached GitHub release "
    "metadata is available. Refresh candidates and try again."
)
_REGEX_SPECIAL_CHARS = "\\^$.*+?()[]{}|"
_GHCR_GITHUB_REPO_RE = re.compile(
    r"^ghcr[.]io/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})$",
    re.ASCII,
)


@dataclass(frozen=True)
class _RetagTargetRecord:
    item: RetagTargetItem
    stack: ComposeStack
    service_image: ServiceImage
    known_image: str
    provenance: DigestTagProvenance | None
    service_key_ambiguous: bool


class EffectiveConfigLoader(Protocol):
    def __call__(self, settings: WebSettings) -> UpdaterConfig: ...


class RetagDigestPinsLoader(Protocol):
    def __call__(self, settings: WebSettings) -> bool: ...


class GitHubLatestFallbackResolver(Protocol):
    """Cached GitHub-latest candidates, supplied by the retag routes owner."""

    def __call__(
        self,
        settings: WebSettings,
        stacks: Sequence[ComposeStack],
        known_by_service: Mapping[str, web_database.KnownDigestState],
    ) -> dict[str, _RetagGitHubLatestFallback]: ...


_effective_config_loader: EffectiveConfigLoader | None = None
_retag_digest_pins_loader: RetagDigestPinsLoader | None = None


def configure(
    *,
    effective_config_loader: EffectiveConfigLoader,
    retag_digest_pins_loader: RetagDigestPinsLoader,
) -> None:
    global _effective_config_loader, _retag_digest_pins_loader
    _effective_config_loader = effective_config_loader
    _retag_digest_pins_loader = retag_digest_pins_loader


def _retag_target_records(
    settings: WebSettings,
    *,
    github_latest_fallback: bool = False,
    github_latest_by_target_id: Mapping[str, _RetagGitHubLatestFallback] | None = None,
    github_latest_resolver: GitHubLatestFallbackResolver | None = None,
) -> tuple[_RetagTargetRecord, ...] | RetagTargetsResponse:
    stacks_or_response = _discover_retag_stacks(settings)
    if isinstance(stacks_or_response, RetagTargetsResponse):
        return stacks_or_response
    return _retag_target_records_for_stacks(
        settings, stacks_or_response,
        github_latest_fallback=github_latest_fallback,
        github_latest_by_target_id=github_latest_by_target_id,
        github_latest_resolver=github_latest_resolver,
    )


def _retag_target_records_for_stacks(
    settings: WebSettings,
    stacks: Sequence[ComposeStack],
    *,
    github_latest_fallback: bool = False,
    github_latest_by_target_id: Mapping[str, _RetagGitHubLatestFallback] | None = None,
    github_latest_resolver: GitHubLatestFallbackResolver | None = None,
) -> tuple[_RetagTargetRecord, ...]:
    known_by_service = web_database.known_digest_state_by_service(settings)
    service_counts = _retag_service_counts(stacks)
    if github_latest_by_target_id is not None:
        active_github_latest_by_target_id = dict(github_latest_by_target_id)
    elif github_latest_fallback:
        if github_latest_resolver is None:
            raise ValueError("GitHub latest fallback requires a candidate resolver")
        active_github_latest_by_target_id = github_latest_resolver(
            settings,
            stacks,
            known_by_service,
        )
    else:
        active_github_latest_by_target_id = {}
    records: list[_RetagTargetRecord] = []
    digest_pins = _retag_digest_pins(settings)
    running_service_keys = _running_retag_compose_service_keys(settings)
    for stack in stacks:
        for service_image in stack.service_images:
            service_key = _retag_service_key(stack.name, service_image.service)
            target_id = _retag_target_id(stack, service_image)
            service_key_ambiguous = service_counts[service_key] > 1
            known = _known_state_for_retag_target(
                known_by_service.get(service_key),
                service_image.image,
                service_key_ambiguous=service_key_ambiguous,
            )
            runtime_state: RetagRuntimeState = "unknown"
            if running_service_keys is not None:
                runtime_state = (
                    "running"
                    if _retag_compose_service_key(stack, service_image.service)
                    in running_service_keys
                    else "not-running"
                )
            records.append(
                _retag_target_record(
                    stack,
                    service_image,
                    target_id,
                    known,
                    active_github_latest_by_target_id.get(target_id),
                    runtime_state=runtime_state,
                    service_key_ambiguous=service_key_ambiguous,
                    github_latest_fallback=github_latest_fallback,
                    digest_pins=digest_pins,
                )
            )

    return tuple(records)


def _discover_retag_stacks(
    settings: WebSettings,
) -> tuple[ComposeStack, ...] | RetagTargetsResponse:
    config = _effective_config(settings)
    compose = ComposeCli(runner=_command_runner(settings))
    try:
        return tuple(
            compose.discover_stacks(
                config.docker_base,
                project_base=settings.host_docker_base,
                ignore_paths=config.compose_ignore_paths,
            )
        )
    except ComposeDiscoveryError as exc:
        return RetagTargetsResponse(
            status="unavailable",
            count=0,
            warnings=[
                _safe_exception_detail(
                    settings,
                    "could not discover retag targets",
                    exc,
                )
            ],
        )


def _retag_service_key(stack_name: str, service_name: str) -> str:
    return f"{stack_name}/{service_name}"


def _retag_service_counts(stacks: Sequence[ComposeStack]) -> Counter[str]:
    return Counter(
        _retag_service_key(stack.name, service_image.service)
        for stack in stacks
        for service_image in stack.service_images
    )


def _retag_target_id(stack: ComposeStack, service_image: ServiceImage) -> str:
    return _retag_target_id_from_values(
        stack.directory,
        stack.file,
        stack.project_directory,
        stack.name,
        service_image.service,
    )


def _known_state_for_retag_target(
    known: web_database.KnownDigestState | None,
    current_image: str,
    *,
    service_key_ambiguous: bool,
) -> web_database.KnownDigestState | None:
    if known is None or not service_key_ambiguous:
        return known
    if _known_state_matches_image(known, current_image):
        return known
    return None


def _known_state_matches_image(
    known: web_database.KnownDigestState,
    current_image: str,
) -> bool:
    return current_image in {
        known.image,
        known.digest_provenance.final_image,
    }


def _retag_target_record(
    stack: ComposeStack,
    service_image: ServiceImage,
    target_id: str,
    known: web_database.KnownDigestState | None,
    github_latest: _RetagGitHubLatestFallback | None,
    *,
    runtime_state: RetagRuntimeState,
    service_key_ambiguous: bool,
    github_latest_fallback: bool,
    digest_pins: bool,
) -> _RetagTargetRecord:
    service_key = _retag_service_key(stack.name, service_image.service)
    provenance = None if known is None else known.digest_provenance
    github_latest_provenance = (
        provenance is None
        and github_latest is not None
        and github_latest.provenance is not None
    )
    if provenance is None and github_latest is not None:
        provenance = github_latest.provenance
    known_image = "" if known is None else known.image
    item = _retag_target_item(
        target_id=target_id,
        service_key=service_key,
        stack=stack.name,
        service_image=service_image,
        directory=str(stack.directory),
        compose_file=stack.file,
        project_directory=(
            "" if stack.project_directory is None else str(stack.project_directory)
        ),
        known_image=known_image,
        provenance=provenance,
        github_latest=github_latest,
        github_latest_fallback=github_latest_fallback,
        allow_source_image_match=github_latest_provenance,
        runtime_state=runtime_state,
        digest_pins=digest_pins,
    )
    return _RetagTargetRecord(
        item=item,
        stack=stack,
        service_image=service_image,
        known_image=known_image,
        provenance=provenance,
        service_key_ambiguous=service_key_ambiguous,
    )


def _effective_config(settings: WebSettings) -> UpdaterConfig:
    if _effective_config_loader is None:
        return settings.config
    try:
        return _effective_config_loader(settings)
    except ConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail=_safe_exception_detail(
                settings,
                "could not read effective config",
                exc,
            ),
        ) from exc


def _retag_digest_pins(settings: WebSettings) -> bool:
    if _retag_digest_pins_loader is None:
        return False
    return _retag_digest_pins_loader(settings)


def _retag_target_item(
    *,
    target_id: str,
    service_key: str,
    stack: str,
    service_image: ServiceImage,
    directory: str,
    compose_file: str,
    project_directory: str,
    known_image: str,
    provenance: DigestTagProvenance | None,
    github_latest: _RetagGitHubLatestFallback | None,
    github_latest_fallback: bool,
    allow_source_image_match: bool,
    runtime_state: RetagRuntimeState,
    digest_pins: bool,
) -> RetagTargetItem:
    label_value = _label_value(service_image.labels, WUD_TAG_INCLUDE_LABEL)
    tracking_tag, tracking_tag_source = _tracking_tag(
        service_image.image,
        label_value=label_value,
        provenance=provenance,
    )
    retag_available, retag_reason = _retag_eligibility(
        service_image.image,
        known_image=known_image,
        tracking_tag=tracking_tag,
        tracking_tag_source=tracking_tag_source,
        label_value=label_value,
        provenance=provenance,
        allow_source_image_match=allow_source_image_match,
    )
    choices = [KEEP_CURRENT_CHOICE]
    if retag_available:
        choices.append(SWITCH_TO_CONCRETE_CHOICE)
    proposed_tag = "" if provenance is None else provenance.resolved_tag
    final_image = ""
    if provenance is not None:
        final_image = (
            provenance.final_image
            if digest_pins
            else image_with_tag(service_image.image, provenance.resolved_tag)
        )
    candidate_source = "provenance" if provenance is not None else ""
    candidate_warning = ""
    candidate_link_label = ""
    candidate_link_url = ""
    if github_latest is not None:
        candidate_source = "github-latest"
        candidate_warning = github_latest.warning
        candidate_link_label = github_latest.link_label
        candidate_link_url = github_latest.link_url
        if not proposed_tag:
            proposed_tag = github_latest.proposed_tag
    elif (
        github_latest_fallback
        and provenance is None
        and tracking_tag == "latest"
        and retag_reason == "missing-provenance"
    ):
        candidate_source = "github-latest"
        candidate_warning = GITHUB_LATEST_MISSING_CACHE_WARNING
    if not candidate_link_url:
        candidate_link_label, candidate_link_url = _inferred_candidate_link(
            service_image.image
        )
    return RetagTargetItem(
        target_id=target_id,
        service_key=service_key,
        stack=stack,
        service=service_image.service,
        image=service_image.image,
        image_repo=repo_key(service_image.image),
        current_tag=image_tag(service_image.image),
        tracking_tag=tracking_tag,
        tracking_tag_source=tracking_tag_source,
        proposed_tag=proposed_tag,
        final_image=final_image,
        candidate_source=candidate_source,
        candidate_warning=candidate_warning,
        candidate_link_label=candidate_link_label,
        candidate_link_url=candidate_link_url,
        runtime_state=runtime_state,
        retag_available=retag_available,
        retag_reason=retag_reason,
        choices=choices,
        label_key=WUD_TAG_INCLUDE_LABEL,
        label_value=label_value,
        directory=directory,
        compose_file=compose_file,
        project_directory=project_directory,
        digest_provenance=(
            None if provenance is None else asdict(provenance)
        ),
    )


def _inferred_candidate_link(image: str) -> tuple[str, str]:
    repo_ref = image_repo_ref(image)
    match = _GHCR_GITHUB_REPO_RE.fullmatch(repo_ref)
    if match:
        owner = match.group("owner")
        repo = match.group("repo")
        if repo not in {".", ".."}:
            return "GitHub tags", f"https://github.com/{owner}/{repo}/tags"
    return "", ""


def _tracking_tag(
    image: str,
    *,
    label_value: str,
    provenance: DigestTagProvenance | None,
) -> tuple[str, str]:
    if label_value:
        label_tag = _single_exact_tag(label_value)
        if label_tag:
            return label_tag, "label"
        return "", "unsupported-label"
    if provenance is not None and provenance.watch_tag:
        return provenance.watch_tag, "provenance"
    tag = image_tag(image)
    if tag:
        return tag, "image"
    return "", ""


def _retag_eligibility(
    image: str,
    *,
    known_image: str,
    tracking_tag: str,
    tracking_tag_source: str,
    label_value: str,
    provenance: DigestTagProvenance | None,
    allow_source_image_match: bool = False,
) -> tuple[bool, str]:
    if tracking_tag_source == "unsupported-label":
        return False, "unsupported-tracking-label"
    if tracking_tag != "latest":
        return False, "not-latest-tracking"
    if provenance is None:
        return False, "missing-provenance"
    if not _provenance_matches_image(
        image,
        known_image=known_image,
        provenance=provenance,
        allow_source_image_match=allow_source_image_match,
    ):
        return False, "stale-provenance"
    if not provenance.resolved_tag or provenance.resolved_tag == "latest":
        return False, "missing-concrete-tag"
    if not tag_value_valid(provenance.resolved_tag):
        return False, "invalid-candidate-tag"
    if not provenance.target_digest or not provenance.final_image:
        return False, "missing-final-image"
    if label_value and not _single_exact_tag(label_value):
        return False, "unsupported-tracking-label"
    return True, "eligible"


def _provenance_matches_image(
    image: str,
    *,
    known_image: str,
    provenance: DigestTagProvenance,
    allow_source_image_match: bool = False,
) -> bool:
    candidates = {known_image, provenance.final_image}
    if allow_source_image_match:
        candidates.add(provenance.source_image)
    return image in candidates


def _label_value(labels: tuple[tuple[str, str], ...], key: str) -> str:
    values: Mapping[str, str] = dict(labels)
    return values.get(key, "")


def _single_exact_tag(value: str) -> str:
    normalized = compose_unescape_dollars(value)
    if tag_value_valid(normalized):
        return normalized
    if not normalized.startswith("^") or not normalized.endswith("$"):
        return ""
    tag_chars: list[str] = []
    index = 1
    end = len(normalized) - 1
    while index < end:
        char = normalized[index]
        if char == "\\":
            index += 1
            if index >= end:
                return ""
            escaped = normalized[index]
            if escaped not in _REGEX_SPECIAL_CHARS:
                return ""
            tag_chars.append(escaped)
            index += 1
            continue
        if char in _REGEX_SPECIAL_CHARS:
            return ""
        tag_chars.append(char)
        index += 1
    tag = "".join(tag_chars)
    return tag if tag_value_valid(tag) else ""
