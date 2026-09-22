"""WUD observation parsing, reconciliation, and restart-safe row conversion.

This owner transforms current payloads and prior observations without retaining
mutable cache state. The caller holds the refresh lock and publishes the result.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from typing import cast

from .digest_verifier import DOCKER_HUB_REGISTRIES
from .images import (
    image_has_tag,
    image_matches_resolved_target,
    image_tag,
    normalize_digest,
    strip_digest,
    tag_value_valid,
)
from .platforms import ImagePlatform, parse_platform, platform_from_parts
from .web_models import (
    PendingMetadataStatus,
    WebSettings,
    WudApiObservationDiagnostic,
    WudApiObservationOutcome,
    WudApiObservationReason,
    WudContainerMetadata,
)
from .web_wud_observation_store import WudContainerIdentity
from .web_wud_transport import _sanitize_detail
from .wud_file import WudTarget, parse_wud_text

_HTTP_STATUS_DETAIL_RE = re.compile(
    r"\b(?:HTTP(?:\s+status)?|status(?:\s+code)?)\s*(?:[:=]\s*)?([1-5]\d{2})\b",
    re.IGNORECASE,
)
_UNSUPPORTED_REGISTRY_ERROR_PREFIX = "unsupported registry "


@dataclass(frozen=True)
class WudApiContainer:
    id: str
    name: str
    display_name: str
    status: str
    watcher: str
    image: str
    local_tag: str
    local_digest: str
    remote_tag: str
    remote_digest: str
    update_kind: str
    semver_diff: str
    link: str
    error: str
    labels: Mapping[str, str] = field(default_factory=dict)
    platform: ImagePlatform | None = None
    local_image_id: str = ""
    metadata_status: PendingMetadataStatus = "fresh"
    update_available: bool | None = None

    def response(self) -> WudContainerMetadata:
        platform = self.platform
        return WudContainerMetadata(
            id=self.id,
            name=self.name,
            display_name=self.display_name,
            status=self.status,
            watcher=self.watcher,
            local_tag=self.local_tag,
            local_digest=self.local_digest,
            remote_tag=self.remote_tag,
            remote_digest=self.remote_digest,
            update_kind=self.update_kind,
            semver_diff=self.semver_diff,
            link=self.link,
            error=self.error,
            platform=platform.value if platform is not None else "",
            platform_os=platform.os if platform is not None else "",
            platform_architecture=platform.architecture if platform is not None else "",
            platform_variant=platform.variant if platform is not None else "",
        )


_PERSISTED_WUD_API_CONTAINER_FIELDS = frozenset(
    {
        "id",
        "name",
        "display_name",
        "status",
        "watcher",
        "image",
        "local_tag",
        "local_digest",
        "remote_tag",
        "remote_digest",
        "update_kind",
        "semver_diff",
        "link",
        "platform",
        "local_image_id",
    }
)
_EXCLUDED_WUD_API_CONTAINER_FIELDS = frozenset(
    {"error", "labels", "metadata_status", "update_available"}
)
assert (
    _PERSISTED_WUD_API_CONTAINER_FIELDS | _EXCLUDED_WUD_API_CONTAINER_FIELDS
    == {item.name for item in fields(WudApiContainer)}
)
assert not (
    _PERSISTED_WUD_API_CONTAINER_FIELDS & _EXCLUDED_WUD_API_CONTAINER_FIELDS
)


@dataclass(frozen=True)
class _WudContainerObservation:
    container: WudApiContainer
    update_available: bool | None
    usable_scan_result: bool
    degraded: bool
    unsupported: bool


@dataclass(frozen=True)
class _PendingObservation:
    container: WudApiContainer
    observed_at: str


def _count_phrase(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _degraded_observation_detail(
    degraded_container_count: int,
    retained_update_count: int,
    recovered_update_count: int,
) -> str:
    detail = (
        " The last WUD update check failed for "
        f"{_count_phrase(degraded_container_count, 'container')}."
    )
    if retained_update_count:
        retained = _count_phrase(retained_update_count, "update")
        detail += (
            f" {retained} {'uses the result' if retained_update_count == 1 else 'use results'} "
            "from the last successful WUD check."
        )
    if recovered_update_count:
        recovered = _count_phrase(recovered_update_count, "update")
        detail += (
            f" {recovered} {'was' if recovered_update_count == 1 else 'were'} "
            "recovered from the pending file."
        )
    unresolved_count = max(
        0,
        degraded_container_count
        - retained_update_count
        - recovered_update_count,
    )
    if unresolved_count:
        detail += (
            " Update status is unknown for "
            f"{_count_phrase(unresolved_count, 'container')}."
        )
    return detail


def _observation_status_detail(
    available_update_count: int,
    degraded_container_count: int,
    retained_update_count: int,
    recovered_update_count: int,
    unsupported_container_count: int,
) -> str:
    updates = _count_phrase(available_update_count, "update")
    detail = f"{updates} {'is' if available_update_count == 1 else 'are'} available."
    if degraded_container_count:
        detail += _degraded_observation_detail(
            degraded_container_count,
            retained_update_count,
            recovered_update_count,
        )
    if unsupported_container_count:
        containers = _count_phrase(unsupported_container_count, "container")
        registry = "its registry is" if unsupported_container_count == 1 else "their registries are"
        detail += f" WUD skipped {containers} because {registry} unsupported."
    return detail


def _stored_observation(container: WudApiContainer) -> Mapping[str, object]:
    platform = container.platform
    stored = {
        "id": container.id,
        "name": container.name,
        "display_name": container.display_name,
        "status": container.status,
        "watcher": container.watcher,
        "image": container.image,
        "local_tag": container.local_tag,
        "local_digest": container.local_digest,
        "remote_tag": container.remote_tag,
        "remote_digest": container.remote_digest,
        "update_kind": container.update_kind,
        "semver_diff": container.semver_diff,
        "link": container.link,
        "platform": platform.value if platform is not None else "",
        "local_image_id": container.local_image_id,
    }
    assert stored.keys() == _PERSISTED_WUD_API_CONTAINER_FIELDS
    return stored


def _container_from_stored_observation(
    raw: Mapping[str, object],
) -> WudApiContainer | None:
    platform_value = _string(raw.get("platform"))
    platform = parse_platform(platform_value) if platform_value else None
    container = WudApiContainer(
        id=_string(raw.get("id")),
        name=_string(raw.get("name")),
        display_name=_string(raw.get("display_name")),
        status=_string(raw.get("status")),
        watcher=_string(raw.get("watcher")),
        image=_string(raw.get("image")),
        local_tag=_string(raw.get("local_tag")),
        local_digest=_string(raw.get("local_digest")),
        remote_tag=_string(raw.get("remote_tag")),
        remote_digest=_string(raw.get("remote_digest")),
        update_kind=_string(raw.get("update_kind")),
        semver_diff=_string(raw.get("semver_diff")),
        link=_string(raw.get("link")),
        error="",
        platform=platform,
        local_image_id=_string(raw.get("local_image_id")),
    )
    if (
        _container_identity(container) is None
        or not (container.remote_tag or container.remote_digest)
    ):
        return None
    return container


def _append_pending_observation(
    container: WudApiContainer,
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
    *,
    observed_at: str,
) -> None:
    containers.append(container)
    identity = _container_identity(container)
    if identity is not None:
        pending_observations[identity] = _PendingObservation(
            container=container,
            observed_at=observed_at,
        )


def _retain_previous_observation(
    container: WudApiContainer,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
) -> bool:
    match = _previous_observation_for_container(container, previous)
    if match is None:
        return False
    identity, previous_observation = match

    retained = previous_observation.container
    retained = replace(
        retained,
        display_name=container.display_name,
        status=container.status,
        error=container.error or "WUD update result is unavailable",
        labels=container.labels,
        metadata_status="retained",
    )
    containers.append(retained)
    pending_observations[identity] = _PendingObservation(
        container=retained,
        observed_at=previous_observation.observed_at,
    )
    return True


def _previous_observation_for_container(
    container: WudApiContainer,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
) -> tuple[WudContainerIdentity, _PendingObservation] | None:
    identity = _container_identity(container)
    if identity is None:
        return None
    exact = previous.get(identity)
    if exact is not None:
        return identity, exact
    if container.local_digest or not container.local_image_id:
        return None
    matches = [
        (previous_identity, observation)
        for previous_identity, observation in previous.items()
        if previous_identity[:5] == identity[:5]
        and previous_identity[6] == identity[6]
    ]
    return matches[0] if len(matches) == 1 else None


def _pending_file_recovery_targets(settings: WebSettings) -> tuple[WudTarget, ...]:
    if not settings.legacy_scripts_enabled:
        return ()
    try:
        text = settings.config.wud_out_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    return parse_wud_text(text).targets


def _recover_pending_file_observation(
    container: WudApiContainer,
    targets: Sequence[WudTarget],
) -> WudApiContainer | None:
    for target in targets:
        if not _pending_file_target_is_recoverable(container, target):
            continue

        return cast(
            WudApiContainer,
            replace(
                container,
                remote_tag=target.desired_tag,
                remote_digest=target.digest,
                update_kind="tag" if target.desired_tag else "digest",
                error=(
                    container.error
                    or "WUD update result is unavailable; pending update recovered "
                    "from WUD_OUT_FILE"
                ),
                metadata_status="recovered",
            ),
        )
    return None


def _pending_file_target_is_recoverable(
    container: WudApiContainer,
    target: WudTarget,
) -> bool:
    if not (target.desired_tag or target.digest):
        return False
    if not _recovery_container_matches_target(container, target):
        return False
    if target.platform is not None and target.platform != container.platform:
        return False
    if target.desired_tag:
        return target.desired_tag != container.local_tag
    return target.digest != normalize_digest(container.local_digest)


def _reconcile_degraded_observation(
    observation: _WudContainerObservation,
    settings: WebSettings,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
    recovery_targets: tuple[WudTarget, ...] | None,
) -> tuple[
    tuple[WudTarget, ...] | None,
    int,
    int,
    int,
    int,
    WudApiObservationOutcome,
]:
    container = observation.container
    if _retain_previous_observation(
        container,
        previous,
        containers,
        pending_observations,
    ):
        return recovery_targets, 1, 1, 0, 0, "retained"

    if observation.unsupported:
        return recovery_targets, 0, 0, 0, 1, "unsupported_ignored"
    if recovery_targets is None:
        recovery_targets = _pending_file_recovery_targets(settings)
    recovered = _recover_pending_file_observation(container, recovery_targets)
    if recovered is not None:
        containers.append(recovered)
        return recovery_targets, 1, 0, 1, 0, "recovered"
    return recovery_targets, 1, 0, 0, 0, "unresolved"


def _record_retryable_degraded_container(
    observation: _WudContainerObservation,
    seen_container_ids: set[str],
    container_ids: list[str],
) -> None:
    container_id = observation.container.id
    if (
        not observation.degraded
        or not container_id
        or container_id in seen_container_ids
    ):
        return
    seen_container_ids.add(container_id)
    container_ids.append(container_id)


def _reconcile_container_observations(
    payload: Sequence[object],
    settings: WebSettings,
    *,
    previous: Mapping[WudContainerIdentity, _PendingObservation],
    observed_at: str,
) -> tuple[
    tuple[WudApiContainer, ...],
    tuple[WudApiContainer, ...],
    tuple[WudApiContainer, ...],
    tuple[WudApiContainer, ...],
    tuple[str, ...],
    int,
    int,
    int,
    int,
    tuple[WudApiObservationDiagnostic, ...],
    Mapping[WudContainerIdentity, _PendingObservation],
]:
    containers: list[WudApiContainer] = []
    inventory_containers: list[WudApiContainer] = []
    unresolved_containers: list[WudApiContainer] = []
    hidden_update_candidates: list[WudApiContainer] = []
    retryable_degraded_container_ids: list[str] = []
    seen_retryable_container_ids: set[str] = set()
    pending_observations: dict[WudContainerIdentity, _PendingObservation] = {}
    degraded_container_count = 0
    retained_update_count = 0
    recovered_update_count = 0
    unsupported_container_count = 0
    observation_diagnostics: list[WudApiObservationDiagnostic] = []
    recovery_targets: tuple[WudTarget, ...] | None = None

    for raw in payload:
        observation = _parse_container_observation(raw, settings)
        if observation is None:
            degraded_container_count += 1
            observation_diagnostics.append(
                _malformed_observation_diagnostic(raw, settings)
            )
            continue

        container = observation.container
        inventory_containers.append(
            replace(
                container,
                update_available=(
                    None if observation.degraded or observation.unsupported
                    else observation.update_available
                ),
            )
        )
        _record_retryable_degraded_container(
            observation,
            seen_retryable_container_ids,
            retryable_degraded_container_ids,
        )
        if observation.unsupported or observation.degraded:
            (
                recovery_targets,
                degraded_delta,
                retained_delta,
                recovered_delta,
                unsupported_delta,
                outcome,
            ) = _reconcile_degraded_observation(
                observation,
                settings,
                previous,
                containers,
                pending_observations,
                recovery_targets,
            )
            degraded_container_count += degraded_delta
            retained_update_count += retained_delta
            recovered_update_count += recovered_delta
            unsupported_container_count += unsupported_delta
            observation_diagnostics.append(
                _observation_diagnostic(observation, outcome, settings)
            )
            if outcome == "unresolved":
                unresolved_containers.append(container)
            continue

        _record_healthy_observation(
            raw, observation, containers, pending_observations,
            hidden_update_candidates, observed_at,
        )

    return (
        tuple(containers),
        tuple(inventory_containers),
        tuple(unresolved_containers),
        tuple(hidden_update_candidates),
        tuple(retryable_degraded_container_ids),
        degraded_container_count,
        retained_update_count,
        recovered_update_count,
        unsupported_container_count,
        tuple(observation_diagnostics),
        pending_observations,
    )


def _record_healthy_observation(
    raw: object,
    observation: _WudContainerObservation,
    containers: list[WudApiContainer],
    pending_observations: dict[WudContainerIdentity, _PendingObservation],
    hidden_update_candidates: list[WudApiContainer],
    observed_at: str,
) -> None:
    container = observation.container
    if observation.update_available:
        _append_pending_observation(
            container, containers, pending_observations, observed_at=observed_at,
        )
        return
    update_kind = _object(cast(Mapping[str, object], raw).get("updateKind"))
    if _hidden_update_kind_has_delta(update_kind):
        hidden_update_candidates.append(container)


def _parse_container_observation(
    raw: object,
    settings: WebSettings,
) -> _WudContainerObservation | None:
    if not isinstance(raw, dict):
        return None
    container = _parse_container_payload(raw, settings)
    if container is None:
        return None
    update_available = raw.get("updateAvailable")
    usable_scan_result = _has_usable_scan_result(raw, container)
    unsupported = (
        update_available is False
        and not usable_scan_result
        and container.error.casefold().startswith(_UNSUPPORTED_REGISTRY_ERROR_PREFIX)
    )
    return _WudContainerObservation(
        container=container,
        update_available=(
            update_available if isinstance(update_available, bool) else None
        ),
        usable_scan_result=usable_scan_result,
        unsupported=unsupported,
        degraded=(
            not unsupported
            and (
                not isinstance(update_available, bool)
                or bool(container.error)
                or not usable_scan_result
            )
        ),
    )


def _malformed_observation_diagnostic(
    raw: object,
    settings: WebSettings,
) -> WudApiObservationDiagnostic:
    if not isinstance(raw, dict):
        return WudApiObservationDiagnostic(
            outcome="unresolved",
            reason_code="malformed_observation",
        )

    image = _object(raw.get("image"))
    update_available = raw.get("updateAvailable")
    registry = _registry_host(_path_string(image, "registry", "url"))
    return WudApiObservationDiagnostic(
        outcome="unresolved",
        reason_code="missing_image",
        container_id=_diagnostic_text(settings, _string(raw.get("id"))),
        name=_diagnostic_text(settings, _string(raw.get("name"))),
        registry=_diagnostic_text(settings, registry),
        watcher=_diagnostic_text(settings, _string(raw.get("watcher"))),
        update_available=(
            update_available if isinstance(update_available, bool) else None
        ),
        error=_diagnostic_error_text(settings, _error_message(raw.get("error"))),
    )


def _observation_diagnostic(
    observation: _WudContainerObservation,
    outcome: WudApiObservationOutcome,
    settings: WebSettings,
) -> WudApiObservationDiagnostic:
    container = observation.container
    return WudApiObservationDiagnostic(
        outcome=outcome,
        reason_code=_observation_reason_code(observation),
        container_id=_diagnostic_text(settings, container.id),
        name=_diagnostic_text(settings, container.name),
        image=_diagnostic_text(settings, container.image),
        registry=_diagnostic_text(
            settings,
            _image_registry_key(container.image) or "docker.io",
        ),
        watcher=_diagnostic_text(settings, container.watcher),
        update_available=observation.update_available,
        usable_result=observation.usable_scan_result,
        retryable=observation.degraded and bool(container.id),
        error=_diagnostic_error_text(settings, container.error),
    )


def _observation_reason_code(
    observation: _WudContainerObservation,
) -> WudApiObservationReason:
    if observation.unsupported:
        return "unsupported_registry"
    if observation.update_available is None:
        return "invalid_update_flag"
    if observation.container.error:
        return "reported_error"
    return "missing_scan_result"


def _diagnostic_text(settings: WebSettings, value: str) -> str:
    return _sanitize_detail(settings, value)


def _diagnostic_error_text(settings: WebSettings, value: str) -> str:
    sanitized = _sanitize_detail(settings, value)
    if not sanitized:
        return ""
    if sanitized.casefold().startswith(_UNSUPPORTED_REGISTRY_ERROR_PREFIX):
        return "Unsupported registry"
    status = _HTTP_STATUS_DETAIL_RE.search(sanitized)
    if status is not None:
        if status.group(1) == "429":
            return (
                "The registry rate-limited the last WUD update check "
                "(HTTP 429: too many requests). Wait before rescanning; "
                "a successful check clears this error."
            )
        return (
            "The last WUD update check failed: "
            f"registry request returned HTTP status {status.group(1)}. "
            "Check WUD logs for details."
        )
    return "The last WUD update check failed. Check WUD logs for details."


def _has_usable_scan_result(
    raw: Mapping[str, object],
    container: WudApiContainer,
) -> bool:
    result = raw.get("result")
    if not isinstance(result, dict):
        return False
    if raw.get("updateAvailable") is True:
        return bool(container.remote_tag or container.remote_digest)
    return bool(
        _string(result.get("tag"))
        or _string(result.get("digest"))
        or _string(result.get("created"))
    )


def _hidden_update_kind_has_delta(update_kind: Mapping[str, object]) -> bool:
    if _string(update_kind.get("kind")) not in {"tag", "digest"}:
        return False
    local_value = _string(update_kind.get("localValue"))
    remote_value = _string(update_kind.get("remoteValue"))
    return bool(local_value and remote_value and local_value != remote_value)


def _container_identity(
    container: WudApiContainer,
) -> WudContainerIdentity | None:
    if not container.id or not container.image:
        return None
    platform = container.platform.value if container.platform is not None else ""
    return (
        container.watcher,
        container.id,
        container.name,
        container.image,
        container.local_image_id,
        container.local_digest,
        platform,
    )


def _parse_container_payload(
    raw: Mapping[str, object],
    settings: WebSettings,
) -> WudApiContainer | None:
    image = _object(raw.get("image"))
    result = _object(raw.get("result"))
    update_kind = _object(raw.get("updateKind"))
    image_ref = _image_ref(image)
    if not image_ref:
        return None
    labels = {
        **_string_mapping(image.get("labels")),
        **_string_mapping(raw.get("labels")),
    }
    return WudApiContainer(
        id=_string(raw.get("id")),
        name=_string(raw.get("name")),
        display_name=_string(raw.get("displayName")),
        status=_string(raw.get("status")),
        watcher=_string(raw.get("watcher")),
        image=image_ref,
        local_tag=_path_string(image, "tag", "value"),
        local_digest=_digest_from_value(_path_string(image, "digest", "value")),
        remote_tag=_remote_tag(result, update_kind),
        remote_digest=_remote_digest(result, update_kind),
        update_kind=_string(update_kind.get("kind")),
        semver_diff=_string(update_kind.get("semverDiff")),
        link=_string(result.get("link") or raw.get("link")),
        error=_sanitize_detail(settings, _error_message(raw.get("error"))),
        labels=labels,
        platform=_container_platform(raw, image),
        local_image_id=_string(image.get("id")),
    )


def _container_platform(
    raw: Mapping[str, object],
    image: Mapping[str, object],
) -> ImagePlatform | None:
    for value in (
        _string(image.get("platform")),
        _string(raw.get("platform")),
        _string(raw.get("image_platform")),
        _string(raw.get("imagePlatform")),
    ):
        platform = parse_platform(value) if value else None
        if platform is not None:
            return platform

    for source in (
        _object(image.get("platform")),
        _object(raw.get("platform")),
        image,
        raw,
        _object(raw.get("container_json")),
        _object(raw.get("containerJson")),
    ):
        platform = platform_from_parts(
            _string(source.get("os") or source.get("image_os") or source.get("imageOs")),
            _string(
                source.get("architecture")
                or source.get("arch")
                or source.get("image_architecture")
                or source.get("imageArchitecture")
            ),
            _string(source.get("variant") or source.get("image_variant") or source.get("imageVariant")),
        )
        if platform is not None:
            return platform
    return None


def _match_container(
    target: WudTarget,
    containers: Sequence[WudApiContainer],
) -> WudApiContainer | None:
    for container in containers:
        if _container_matches_target(container, target):
            return container
    return None


def _container_matches_target(container: WudApiContainer, target: WudTarget) -> bool:
    if target.first in {container.name, container.display_name, container.id}:
        return True
    if not container.image:
        return False
    allow_repo = target.allow_repo or not image_has_tag(target.first)
    return image_matches_resolved_target(container.image, target.first, allow_repo)


def _recovery_container_matches_target(
    container: WudApiContainer,
    target: WudTarget,
) -> bool:
    if target.first in {container.name, container.display_name, container.id}:
        return True
    if not container.image:
        return False
    if _image_registry_key(container.image) != _image_registry_key(target.first):
        return False
    allow_repo = target.allow_repo or not image_has_tag(target.first)
    return image_matches_resolved_target(container.image, target.first, allow_repo)


def _image_registry_key(image: str) -> str:
    if not _image_has_registry(image):
        return ""
    registry = strip_digest(image).partition("/")[0].lower()
    return "" if registry in DOCKER_HUB_REGISTRIES else registry


def _image_ref(image: Mapping[str, object]) -> str:
    name = _string(image.get("name"))
    tag = _path_string(image, "tag", "value")
    if not name:
        return ""
    registry = _registry_host(_path_string(image, "registry", "url"))
    if registry and not _image_has_registry(name):
        name = f"{registry}/{name}"
    if tag and not image_has_tag(name):
        return f"{name}:{tag}"
    return name


def _registry_host(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value if "://" in value else f"//{value}")
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).split("@")[-1].lower()
    return "" if host in DOCKER_HUB_REGISTRIES else host


def _image_has_registry(image: str) -> bool:
    left, sep, _rest = strip_digest(image).partition("/")
    return bool(sep and ("." in left or ":" in left or left == "localhost"))


def _remote_tag(
    result: Mapping[str, object],
    update_kind: Mapping[str, object],
) -> str:
    result_tag = _string(result.get("tag"))
    if tag_value_valid(result_tag):
        return result_tag
    if _string(update_kind.get("kind")) != "tag":
        return ""
    return _tag_from_remote_value(_string(update_kind.get("remoteValue")))


def _tag_from_remote_value(value: str) -> str:
    if not value:
        return ""
    candidate = value.split("@sha256:", 1)[0]
    if image_has_tag(candidate):
        candidate = image_tag(candidate)
    if tag_value_valid(candidate):
        return candidate
    return ""


def _remote_digest(
    result: Mapping[str, object],
    update_kind: Mapping[str, object],
) -> str:
    result_digest = _digest_from_value(_string(result.get("digest")))
    if result_digest:
        return result_digest
    if _string(update_kind.get("kind")) not in {"digest", "tag"}:
        return ""
    return _digest_from_value(_string(update_kind.get("remoteValue")))


def _digest_from_value(value: str) -> str:
    if not value:
        return ""
    if "@sha256:" in value or value.startswith("sha256:"):
        return normalize_digest(value)
    return ""


def _object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _path_string(value: Mapping[str, object], *parts: str) -> str:
    current: object = value
    for part in parts:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return _string(current)


def _string_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _error_message(value: object) -> str:
    if isinstance(value, dict):
        return _string(value.get("message") or value.get("error"))
    return _string(value)

