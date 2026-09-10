"""Pending-update source selection for WebUI file and WUD API modes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import web_wud_api
from .images import (
    image_has_tag,
    image_matches_resolved_target,
    normalize_digest,
    strip_digest,
    tag_value_valid,
)
from .plan_models import DryRunPlanSource
from .web_auth import WebConfigError
from .web_models import (
    PendingMetadataStatus,
    PendingSourceActive,
    PendingSourceInfo,
    PendingSourceMode,
)
from .wud_file import ParsedWudFile, parse_wud_text

if TYPE_CHECKING:
    from .web_models import WebSettings


PENDING_SOURCE_ENV = "WUD_PENDING_SOURCE"
DEFAULT_PENDING_SOURCE = "api"
VALID_PENDING_SOURCES: frozenset[PendingSourceMode] = frozenset(
    {"file", "api", "auto"}
)
API_SOURCE_FILE_LABEL = "WUD API"


@dataclass(frozen=True)
class PendingSourceResult:
    configured: PendingSourceMode
    active: PendingSourceActive
    label: str
    source_file: str
    exists: bool
    parsed: ParsedWudFile
    text: str
    source_hash: str
    fresh: bool = True
    degraded: bool = False
    fallback_reason: str = ""
    detail: str = ""
    warnings: tuple[str, ...] = ()
    wud_snapshot: web_wud_api.WudApiSnapshot | None = None
    metadata_by_line: Mapping[int, web_wud_api.WudApiContainer] | None = None
    container_ids_by_line: Mapping[int, tuple[str, ...]] | None = None
    source_ids_by_line: Mapping[int, str] | None = None
    metadata_status_by_line: Mapping[int, PendingMetadataStatus] | None = None

    def response_source(self) -> PendingSourceInfo:
        return PendingSourceInfo(
            configured=self.configured,
            active=self.active,
            label=self.label,
            fresh=self.fresh,
            degraded=self.degraded,
            fallback_reason=self.fallback_reason,
            detail=self.detail,
        )

    def plan_source(self) -> DryRunPlanSource:
        return DryRunPlanSource(
            configured=self.configured,
            active=self.active,
            label=self.label,
            fresh=self.fresh,
            degraded=self.degraded,
            fallback_reason=self.fallback_reason,
            detail=self.detail,
            source_hash=self.source_hash,
            source_ids_by_line=self.source_ids_by_line or {},
            metadata_status_by_line=self.metadata_status_by_line or {},
        )


def container_ids_for_lines(
    source: PendingSourceResult,
    line_numbers: Sequence[int],
) -> tuple[str, ...]:
    by_line = source.container_ids_by_line or {}
    return tuple(
        dict.fromkeys(
            container_id
            for line_no in line_numbers
            for container_id in by_line.get(line_no, ())
            if container_id
        )
    )


def configured_pending_source(environ: Mapping[str, str]) -> PendingSourceMode:
    raw_value = environ.get(PENDING_SOURCE_ENV, "").strip().lower()
    value = raw_value or DEFAULT_PENDING_SOURCE
    if value not in VALID_PENDING_SOURCES:
        allowed = ", ".join(sorted(VALID_PENDING_SOURCES))
        raise WebConfigError(f"{PENDING_SOURCE_ENV} must be one of: {allowed}")
    return cast(PendingSourceMode, value)


def resolve_pending_source(
    settings: WebSettings,
    *,
    include_wud_metadata: bool = False,
    force_api: bool = False,
) -> PendingSourceResult:
    mode = settings.pending_source
    if mode == "file":
        return _file_source(settings, configured=mode, include_wud_metadata=include_wud_metadata)

    api_result = _api_source(settings, configured=mode, force=force_api)
    api_metadata_available = bool(
        api_result.wud_snapshot
        and api_result.wud_snapshot.status.metadata_available
    )
    if mode == "api" or api_metadata_available:
        return api_result

    return _file_source(
        settings,
        configured=mode,
        include_wud_metadata=include_wud_metadata,
        degraded=True,
        fallback_reason=api_result.detail or "WUD API pending source is unavailable",
        detail=api_result.detail,
        wud_snapshot=api_result.wud_snapshot,
    )


def _file_source(
    settings: WebSettings,
    *,
    configured: PendingSourceMode,
    include_wud_metadata: bool,
    degraded: bool = False,
    fallback_reason: str = "",
    detail: str = "",
    wud_snapshot: web_wud_api.WudApiSnapshot | None = None,
) -> PendingSourceResult:
    path = settings.config.wud_out_file
    exists, text = _read_pending_file(path)
    parsed, source_hash = _parse_pending_source_text(text)
    snapshot = wud_snapshot
    metadata_by_line: dict[int, web_wud_api.WudApiContainer] = {}
    if include_wud_metadata:
        snapshot = snapshot or web_wud_api.get_snapshot(settings, include_containers=True)
        if parsed.targets:
            metadata_by_line = web_wud_api.metadata_by_target(
                settings,
                parsed.targets,
                snapshot=snapshot,
            )
    warnings = parsed.warnings
    if degraded and fallback_reason:
        warnings = (
            f"WUD API pending source degraded; using WUD_OUT_FILE: {fallback_reason}",
            *warnings,
        )
    return PendingSourceResult(
        configured=configured,
        active="file",
        label="Pending file",
        source_file=str(path),
        exists=exists,
        parsed=parsed,
        text=text,
        source_hash=source_hash,
        fresh=not degraded,
        degraded=degraded,
        fallback_reason=fallback_reason,
        detail=detail,
        warnings=warnings,
        wud_snapshot=snapshot,
        metadata_by_line=metadata_by_line,
        container_ids_by_line={
            line_no: (container.id,)
            for line_no, container in metadata_by_line.items()
            if container.id
        },
        source_ids_by_line={
            target.line_no: f"file:{target.line_no}" for target in parsed.targets
        },
        metadata_status_by_line={
            target.line_no: "recovered" if degraded else "fresh"
            for target in parsed.targets
        },
    )


def _api_source(
    settings: WebSettings,
    *,
    configured: PendingSourceMode,
    force: bool,
) -> PendingSourceResult:
    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=force,
    )
    if not snapshot.status.metadata_available:
        detail = snapshot.status.detail or "WUD API container metadata is unavailable"
        return _empty_api_source(
            configured=configured,
            snapshot=snapshot,
            degraded=True,
            detail=detail,
            warnings=(f"WUD API pending source degraded: {detail}",),
        )

    lines = api_pending_lines(
        snapshot.containers,
        unresolved_containers=snapshot.unresolved_containers,
    )
    text = _pending_text(line.raw for line in lines)
    parsed, source_hash = _parse_pending_source_text(text)
    metadata_by_line = {
        line_no: line.container for line_no, line in enumerate(lines, start=1)
    }
    container_ids_by_line = {
        line_no: line.container_ids for line_no, line in enumerate(lines, start=1)
    }
    source_ids_by_line = {
        line_no: ",".join(line.source_ids) for line_no, line in enumerate(lines, start=1)
    }
    metadata_status_by_line = {
        line_no: line.container.metadata_status
        for line_no, line in enumerate(lines, start=1)
    }
    degraded = snapshot.degraded_container_count > 0
    detail = snapshot.status.detail if degraded else ""
    warnings = parsed.warnings
    if degraded:
        warnings = (detail, *warnings)
    return PendingSourceResult(
        configured=configured,
        active="api",
        label=API_SOURCE_FILE_LABEL,
        source_file=API_SOURCE_FILE_LABEL,
        exists=True,
        parsed=parsed,
        text=text,
        source_hash=source_hash,
        fresh=not degraded,
        degraded=degraded,
        detail=detail,
        warnings=warnings,
        wud_snapshot=snapshot,
        metadata_by_line=metadata_by_line,
        container_ids_by_line=container_ids_by_line,
        source_ids_by_line=source_ids_by_line,
        metadata_status_by_line=metadata_status_by_line,
    )


def _empty_api_source(
    *,
    configured: PendingSourceMode,
    snapshot: web_wud_api.WudApiSnapshot | None,
    degraded: bool,
    detail: str,
    warnings: tuple[str, ...],
) -> PendingSourceResult:
    text = ""
    parsed, source_hash = _parse_pending_source_text(text)
    return PendingSourceResult(
        configured=configured,
        active="api",
        label=API_SOURCE_FILE_LABEL,
        source_file=API_SOURCE_FILE_LABEL,
        exists=False,
        parsed=parsed,
        text=text,
        source_hash=source_hash,
        fresh=not degraded,
        degraded=degraded,
        fallback_reason="" if configured == "api" else detail,
        detail=detail,
        warnings=warnings,
        wud_snapshot=snapshot,
        metadata_by_line={},
        container_ids_by_line={},
        source_ids_by_line={},
        metadata_status_by_line={},
    )


@dataclass(frozen=True)
class ApiPendingLine:
    raw: str
    container: web_wud_api.WudApiContainer
    container_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


def api_pending_lines(
    containers: tuple[web_wud_api.WudApiContainer, ...],
    *,
    unresolved_containers: tuple[web_wud_api.WudApiContainer, ...] = (),
) -> tuple[ApiPendingLine, ...]:
    by_raw: dict[str, ApiPendingLine] = {}
    for container in sorted(containers, key=_container_sort_key):
        raw = _container_pending_line(container)
        if not raw:
            continue
        source_id = _container_source_id(container)
        existing = by_raw.get(raw)
        if existing is None:
            by_raw[raw] = ApiPendingLine(
                raw=raw,
                container=container,
                container_ids=_container_ids(container),
                source_ids=(source_id,),
            )
            continue
        by_raw[raw] = _merge_api_pending_line(existing, container)
    related_containers = (
        *containers,
        *(
            replace(container, metadata_status="retained")
            for container in unresolved_containers
        ),
    )
    for raw in sorted(by_raw):
        existing = by_raw[raw]
        for container in sorted(related_containers, key=_container_sort_key):
            if not _container_can_share_apply_scope(container, existing.container):
                continue
            existing = _merge_related_api_pending_line(existing, container)
            by_raw[raw] = existing
    return tuple(by_raw[raw] for raw in sorted(by_raw))


def _merge_api_pending_line(
    existing: ApiPendingLine,
    container: web_wud_api.WudApiContainer,
) -> ApiPendingLine:
    source_ids = existing.source_ids
    source_id = _container_source_id(container)
    if source_id not in source_ids:
        source_ids = (*source_ids, source_id)
    container_ids = existing.container_ids
    for container_id in _container_ids(container):
        if container_id not in container_ids:
            container_ids = (*container_ids, container_id)
    metadata_status = _least_fresh_metadata_status(
        existing.container.metadata_status,
        container.metadata_status,
    )
    if (
        source_ids == existing.source_ids
        and container_ids == existing.container_ids
        and metadata_status == existing.container.metadata_status
    ):
        return existing
    return replace(
        existing,
        container=replace(existing.container, metadata_status=metadata_status),
        container_ids=container_ids,
        source_ids=source_ids,
    )


def _merge_related_api_pending_line(
    existing: ApiPendingLine,
    container: web_wud_api.WudApiContainer,
) -> ApiPendingLine:
    source_ids = tuple(
        sorted(
            dict.fromkeys((*existing.source_ids, _container_source_id(container)))
        )
    )
    container_ids = tuple(
        sorted(dict.fromkeys((*existing.container_ids, *_container_ids(container))))
    )
    return replace(
        existing,
        container=replace(
            existing.container,
            metadata_status=_least_fresh_metadata_status(
                existing.container.metadata_status,
                container.metadata_status,
            ),
        ),
        container_ids=container_ids,
        source_ids=source_ids,
    )


def _container_can_share_apply_scope(
    unresolved: web_wud_api.WudApiContainer,
    pending: web_wud_api.WudApiContainer,
) -> bool:
    if not unresolved.image or not pending.image:
        return False
    return image_matches_resolved_target(
        unresolved.image,
        pending.image,
        allow_repo=not image_has_tag(pending.image),
    )


def _least_fresh_metadata_status(
    left: PendingMetadataStatus,
    right: PendingMetadataStatus,
) -> PendingMetadataStatus:
    priority = {"fresh": 0, "retained": 1, "recovered": 2}
    return left if priority[left] >= priority[right] else right


def _container_pending_line(container: web_wud_api.WudApiContainer) -> str:
    image = container.image.strip()
    if not image:
        return ""
    platform_suffix = (
        f" platform={container.platform.value}" if container.platform is not None else ""
    )
    if container.update_kind == "tag" and tag_value_valid(container.remote_tag):
        return (
            f"{image} tag={container.remote_tag}{platform_suffix}"
            f"{_digest_metadata_suffix(container.remote_digest)}"
        )
    if container.remote_digest:
        return (
            f"{_pending_image_with_digest(image, container.remote_digest)}"
            f"{platform_suffix}"
        )
    if tag_value_valid(container.remote_tag):
        return f"{image} tag={container.remote_tag}{platform_suffix}"
    return f"{image}{platform_suffix}"


def _pending_image_with_digest(image: str, digest: str) -> str:
    return f"{strip_digest(image)}@{normalize_digest(digest)}"


def _digest_metadata_suffix(digest: str) -> str:
    return f" sha256={normalize_digest(digest)}" if digest else ""


def _container_source_id(container: web_wud_api.WudApiContainer) -> str:
    return container.id or container.name or container.display_name or container.image


def _container_ids(container: web_wud_api.WudApiContainer) -> tuple[str, ...]:
    return (container.id,) if container.id else ()


def _container_sort_key(
    container: web_wud_api.WudApiContainer,
) -> tuple[str, str, str, str]:
    return (
        _container_pending_line(container),
        container.id,
        container.name,
        container.image,
    )


def _read_pending_file(path: Path) -> tuple[bool, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, ""
    return True, text


def _parse_pending_source_text(text: str) -> tuple[ParsedWudFile, str]:
    return parse_wud_text(text), _sha256(text)


def _pending_text(lines: Iterable[str]) -> str:
    values = [str(line) for line in lines]
    if not values:
        return ""
    return "\n".join(values) + "\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
