"""Shared planning helpers that are not tied to updater orchestration."""

# Compatibility imports deliberately re-export these names unchanged.

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from .compose import ComposeBindMount, ComposeStack
from .images import image_with_tag
from .updater_digest_pin import (
    _digest_pin_candidates as _digest_pin_candidates,
)
from .updater_digest_pin import (
    _digest_pin_match_tag as _digest_pin_match_tag,
)
from .updater_digest_pin import (
    _digest_pin_resolve_error as _digest_pin_resolve_error,
)
from .updater_digest_pin import (
    _digest_pin_tag_materialization_updates as _digest_pin_tag_materialization_updates,
)
from .updater_digest_pin import (
    _resolve_digest_pin_candidate as _resolve_digest_pin_candidate,
)
from .updater_digest_pin import (
    digest_pin_update_from_values as digest_pin_update_from_values,
)
from .updater_matching import (
    RECREATE_STACK_LABEL as RECREATE_STACK_LABEL,
)
from .updater_matching import (
    RECREATE_STACK_LABEL_FORMAT as RECREATE_STACK_LABEL_FORMAT,
)
from .updater_matching import (
    _expand_network_mode_services as _expand_network_mode_services,
)
from .updater_matching import (
    _failed_line_numbers as _failed_line_numbers,
)
from .updater_matching import (
    _failed_match_for_line as _failed_match_for_line,
)
from .updater_matching import (
    _failure_target_lines as _failure_target_lines,
)
from .updater_matching import (
    _first_match_by_line as _first_match_by_line,
)
from .updater_matching import (
    _label_value_is_true as _label_value_is_true,
)
from .updater_matching import (
    _line_status_reason as _line_status_reason,
)
from .updater_matching import (
    _network_mode_providers as _network_mode_providers,
)
from .updater_matching import (
    _ordered_unique as _ordered_unique,
)
from .updater_matching import (
    _plan_line as _plan_line,
)
from .updater_matching import (
    _preflight_status_reason as _preflight_status_reason,
)
from .updater_matching import (
    _scope_plan_label as _scope_plan_label,
)
from .updater_matching import (
    _service_key as _service_key,
)
from .updater_matching import (
    _services_for_image as _services_for_image,
)
from .updater_matching import (
    _services_for_target_match as _services_for_target_match,
)
from .updater_matching import (
    _stacks_to_update as _stacks_to_update,
)
from .updater_matching import (
    _tag_exclusion_preflight_matches as _tag_exclusion_preflight_matches,
)
from .updater_matching import (
    _target_image_for_match as _target_image_for_match,
)
from .updater_matching import (
    _unique_matches as _unique_matches,
)
from .updater_matching import (
    _update_services as _update_services,
)
from .updater_models import Match, TagExclusionUpdate, TagUpdate

_HELPER_ONLY_MOUNT_PREFIXES = (Path("/host"), Path("/docker-host"), Path("/container-host"))


def _container_bind_mount_path_issue(
    mount: ComposeBindMount,
    *,
    docker_base: Path,
) -> str:
    source = Path(mount.source)
    if not source.is_absolute():
        return ""
    for prefix in _HELPER_ONLY_MOUNT_PREFIXES:
        if source.is_relative_to(prefix):
            base_hint = ""
            if source.is_relative_to(docker_base):
                base_hint = f" from DOCKER_BASE={docker_base}"
            return (
                f"the source path is under helper-only prefix {prefix}{base_hint}; "
                "the Docker daemon must be able to see bind sources at the same path"
            )
    return ""


def _tag_updates(matches: Sequence[Match]) -> tuple[TagUpdate, ...]:
    services_by_update: dict[tuple[str, str, str], set[str]] = {}
    for match in matches:
        if match.target.desired_tag:
            new_image = image_with_tag(match.compose_image, match.target.desired_tag)
            key = (match.compose_image, match.target.desired_tag, new_image)
            services_by_update.setdefault(key, set())
            if match.service:
                services_by_update[key].add(match.service)
    return tuple(
        TagUpdate(
            old_image=old_image,
            desired_tag=desired_tag,
            new_image=new_image,
            services=tuple(sorted(services)),
        )
        for (old_image, desired_tag, new_image), services in sorted(
            services_by_update.items()
        )
    )


def _unique_tag_exclusion_updates(
    updates: Iterable[TagExclusionUpdate],
) -> list[TagExclusionUpdate]:
    unique: dict[tuple[int, str, str, str, str], TagExclusionUpdate] = {}
    for update in updates:
        key = (
            update.stack.index,
            update.service,
            update.image_repo,
            update.tag,
            update.scope,
        )
        unique.setdefault(key, update)
    return [unique[key] for key in sorted(unique)]


def _tag_exclusion_updates_by_stack(
    updates: Sequence[TagExclusionUpdate],
) -> dict[ComposeStack, list[TagExclusionUpdate]]:
    grouped: dict[ComposeStack, list[TagExclusionUpdate]] = {}
    for update in updates:
        grouped.setdefault(update.stack, []).append(update)
    return grouped


def _first_tag_exclusion_by_line(
    updates: Sequence[TagExclusionUpdate],
) -> dict[int, TagExclusionUpdate]:
    first: dict[int, TagExclusionUpdate] = {}
    for update in updates:
        first.setdefault(update.source_line, update)
    return first
