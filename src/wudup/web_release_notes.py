"""WebUI release-note route handlers."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException, Request

from . import web_pending_sources, web_release_notification_state, web_wud_api
from .command import CommandError, CommandRunner
from .db import DatabaseError, init_db, open_db
from .docker_cli import ContainerImage, DockerCli
from .images import (
    image_has_tag,
    image_matches_resolved_target,
    image_with_tag,
    strip_digest,
    tag_value_valid,
)
from .release_notes import (
    OCI_SOURCE_LABEL,
    ReleaseNoteSourceResolver,
    ReleaseNoteTargetTagResolver,
    cached_release_notes,
    github_repo_from_ghcr_image,
    github_repo_from_source,
    refresh_release_notes,
    release_note_placeholders,
)
from .web_auth import (
    _redact_sensitive_text,
    _redact_unknown_absolute_paths,
    _safe_exception_detail,
    _settings,
)
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_models import (
    PendingSourceInfo,
    ReleaseNoteInfo,
    ReleaseNotesResponse,
    WebSettings,
    WudApiStatus,
)
from .web_settings import (
    effective_release_notes_enabled,
    effective_release_notification_config,
)
from .wud_file import WudTarget

LOGGER = logging.getLogger(__name__)
DOCKER_STDERR_LOG_LIMIT = 500
RELEASE_NOTES_DISABLED_DETAIL = "Release-note notifications are disabled."
SourceLabelReader = Callable[[str], tuple[str, CommandError | None]]


@dataclass(frozen=True)
class _ReleaseNotesRequestContext:
    targets: tuple[WudTarget, ...]
    warnings: tuple[str, ...]
    wud_api: Any
    source: web_pending_sources.PendingSourceResult
    source_resolver: ReleaseNoteSourceResolver
    target_tag_resolver: ReleaseNoteTargetTagResolver


@dataclass(frozen=True)
class ReleaseNotesDisabledState:
    reason: str
    source: PendingSourceInfo
    wud_api: WudApiStatus


def api_release_notes(request: Request) -> ReleaseNotesResponse:
    settings = _settings(request)
    context = _release_notes_request_context(settings)
    if isinstance(context, ReleaseNotesResponse):
        return context
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            items = cached_release_notes(
                conn,
                context.targets,
                settings.command_env or {},
                source_resolver=context.source_resolver,
                target_tag_resolver=context.target_tag_resolver,
            )
    except ReadOnlyDatabaseMissing:
        items = release_note_placeholders(
            context.targets,
            settings.command_env or {},
            source_resolver=context.source_resolver,
            target_tag_resolver=context.target_tag_resolver,
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release-note cache",
                exc,
            ),
        ) from exc
    return release_notes_response(
        settings,
        items,
        context.warnings,
        wud_api=context.wud_api,
        source=context.source,
    )


def api_refresh_release_notes(request: Request) -> ReleaseNotesResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    context = _release_notes_request_context(settings)
    if isinstance(context, ReleaseNotesResponse):
        return context
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            items = refresh_release_notes(
                conn,
                context.targets,
                settings.command_env or {},
                source_resolver=context.source_resolver,
                target_tag_resolver=context.target_tag_resolver,
                redact_error=lambda value: _redact_sensitive_text(settings, value),
                force=request.query_params.get("force") == "true",
            )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not refresh release-note metadata",
                exc,
            ),
        ) from exc
    return release_notes_response(
        settings,
        items,
        context.warnings,
        wud_api=context.wud_api,
        source=context.source,
    )


def _release_notes_request_context(
    settings: WebSettings,
) -> _ReleaseNotesRequestContext | ReleaseNotesResponse:
    try:
        source = web_pending_sources.resolve_pending_source(
            settings,
            include_wud_metadata=True,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read pending source",
                exc,
            ),
        ) from exc
    parsed = source.parsed
    if not parsed.targets:
        notifications_enabled = effective_release_notes_enabled(settings)
        return ReleaseNotesResponse(
            source_file=source.source_file,
            source=source.response_source(),
            count=0,
            items=[],
            notifications_enabled=notifications_enabled,
            notifications_disabled_reason=(
                "" if notifications_enabled else RELEASE_NOTES_DISABLED_DETAIL
            ),
            wud_api=_wud_api_status(source),
            warnings=list(source.warnings),
        )
    wud_metadata = dict(source.metadata_by_line or {})
    return _ReleaseNotesRequestContext(
        targets=parsed.targets,
        warnings=source.warnings,
        wud_api=_wud_api_status(source),
        source=source,
        source_resolver=release_note_source_resolver(
            settings,
            wud_metadata=wud_metadata,
        ),
        target_tag_resolver=web_wud_api.target_tag_resolver_from_metadata(
            wud_metadata,
        ),
    )


def release_notes_response(
    settings: WebSettings,
    items: list[Any],
    warnings: Iterable[str],
    *,
    wud_api: Any,
    source: web_pending_sources.PendingSourceResult,
) -> ReleaseNotesResponse:
    redacted_items: list[ReleaseNoteInfo] = []
    for item in items:
        data = asdict(item)
        data["error"] = _redact_sensitive_text(settings, str(data.get("error", "")))
        redacted_items.append(ReleaseNoteInfo.model_validate(data))
    redacted_items = _annotate_notification_state(settings, redacted_items, source)
    notifications_enabled = effective_release_notes_enabled(settings)
    return ReleaseNotesResponse(
        source_file=source.source_file,
        source=source.response_source(),
        count=len(items),
        items=redacted_items,
        notifications_enabled=notifications_enabled,
        notifications_disabled_reason=(
            "" if notifications_enabled else RELEASE_NOTES_DISABLED_DETAIL
        ),
        wud_api=wud_api,
        warnings=[_redact_sensitive_text(settings, warning) for warning in warnings],
    )


def _annotate_notification_state(
    settings: WebSettings,
    items: list[ReleaseNoteInfo],
    source: web_pending_sources.PendingSourceResult,
) -> list[ReleaseNoteInfo]:
    targets_by_line = {target.line_no: target for target in source.parsed.targets}
    identities: dict[int, web_release_notification_state.NotificationIdentity] = {}
    for item in items:
        target = targets_by_line.get(item.line_no)
        if target is None:
            continue
        identities[item.line_no] = web_release_notification_state.notification_identity(
            target,
            item,
            (source.metadata_by_line or {}).get(item.line_no),
        )
    if not identities:
        return items
    config = effective_release_notification_config(settings)
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            annotations = web_release_notification_state.notification_annotations(
                conn,
                config,
                identities,
                resend=False,
            )
    except ReadOnlyDatabaseMissing:
        annotations = web_release_notification_state.notification_annotations_from_history(
            config,
            identities,
            {},
            resend=False,
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read release notification history",
                exc,
            ),
        ) from exc

    annotated: list[ReleaseNoteInfo] = []
    for item in items:
        identity = identities.get(item.line_no)
        if identity is None:
            annotated.append(item)
            continue
        annotation = annotations[item.line_no]
        annotated.append(
            item.model_copy(
                update={
                    "notification_key": annotation.notification_key,
                    "notification_status": annotation.status,
                    "notification_last_sent_at": annotation.last_sent_at,
                    "notification_send_count": annotation.send_count,
                    "notification_skipped_reason": annotation.skipped_reason,
                }
            )
        )
    return annotated


def _wud_api_status(source: web_pending_sources.PendingSourceResult) -> WudApiStatus:
    if source.wud_snapshot is not None:
        return source.wud_snapshot.status
    return WudApiStatus(
        state="unavailable",
        available=False,
        metadata_available=False,
        last_checked_at="",
    )


def release_notes_disabled_response(settings: WebSettings) -> ReleaseNotesResponse:
    disabled = release_notes_disabled_state(settings)
    return ReleaseNotesResponse(
        source_file=str(settings.config.wud_out_file),
        source=disabled.source,
        count=0,
        items=[],
        enabled=False,
        disabled_reason=disabled.reason,
        notifications_enabled=False,
        notifications_disabled_reason=disabled.reason,
        wud_api=disabled.wud_api,
        warnings=[],
    )


def release_notes_disabled_state(settings: WebSettings) -> ReleaseNotesDisabledState:
    return ReleaseNotesDisabledState(
        reason=RELEASE_NOTES_DISABLED_DETAIL,
        source=PendingSourceInfo(
            configured=settings.pending_source,
            active="file",
            label="Release notes disabled",
            fresh=True,
            degraded=False,
            detail=RELEASE_NOTES_DISABLED_DETAIL,
        ),
        wud_api=WudApiStatus(
            state="unavailable",
            available=False,
            metadata_available=False,
            last_checked_at="",
            detail=RELEASE_NOTES_DISABLED_DETAIL,
        ),
    )


def release_note_source_resolver(
    settings: WebSettings,
    *,
    wud_metadata: dict[int, web_wud_api.WudApiContainer] | None = None,
) -> ReleaseNoteSourceResolver:
    docker = DockerCli(runner=CommandRunner(env=settings.command_env))
    label_cache: dict[str, tuple[str, CommandError | None]] = {}
    container_images: list[ContainerImage] | None = None
    wud_source_resolver = web_wud_api.source_resolver_from_metadata(wud_metadata or {})

    def source_label(image: str) -> tuple[str, CommandError | None]:
        if image not in label_cache:
            value, error = docker.try_image_label(image, OCI_SOURCE_LABEL)
            label_cache[image] = (value, error)
        return label_cache[image]

    def running_images() -> list[ContainerImage]:
        nonlocal container_images
        if container_images is None:
            container_images = docker.try_container_images()
        return container_images

    def resolve(target: WudTarget) -> str:
        wud_source = wud_source_resolver(target)
        if github_repo_from_source(wud_source):
            return wud_source

        value, error = source_label_for_target(target, source_label)
        if github_repo_from_source(value):
            return value

        image_source = ghcr_release_source(target.first)
        if image_source:
            return image_source

        running_source, running_error = running_container_release_source(
            target,
            running_images(),
            source_label,
        )
        if running_source:
            return running_source

        if error is not None:
            log_source_label_error(settings, target, error)
        elif running_error is not None:
            log_source_label_error(settings, target, running_error)
        return value

    return resolve


def source_label_for_target(
    target: WudTarget,
    source_label: SourceLabelReader,
) -> tuple[str, CommandError | None]:
    return source_label_from_candidates(
        source_label_candidates_for_target(target),
        source_label,
    )


def source_label_candidates_for_target(target: WudTarget) -> tuple[str, ...]:
    return source_label_candidates(
        target.first,
        tag_token=target.tag_token or raw_tag_hint(target.raw),
    )


def source_label_from_candidates(
    candidates: tuple[str, ...],
    source_label: SourceLabelReader,
) -> tuple[str, CommandError | None]:
    fallback = ""
    first_error: CommandError | None = None
    for image in candidates:
        value, error = source_label(image)
        if github_repo_from_source(value):
            return value, None
        if value and not fallback:
            fallback = value
        if error is not None and first_error is None:
            first_error = error
    return fallback, first_error


def source_label_candidates(image: str, *, tag_token: str = "") -> tuple[str, ...]:
    candidates: list[str] = []
    if "@sha256:" in image:
        if image_has_tag(image):
            candidates.append(strip_digest(image))
        elif tag_token:
            candidates.append(image_with_tag(image, tag_token))
    candidates.append(image)
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def raw_tag_hint(raw: str) -> str:
    tag = ""
    for token in raw.split()[1:]:
        if token.startswith("tag="):
            tag = token.removeprefix("tag=")
    return tag if tag_value_valid(tag) else ""


def running_container_release_source(
    target: WudTarget,
    containers: list[ContainerImage],
    source_label: SourceLabelReader,
) -> tuple[str, CommandError | None]:
    fallback = ""
    first_error: CommandError | None = None
    for container in containers:
        if container.name != target.first and not image_matches_resolved_target(
            container.image,
            target.first,
            target.allow_repo,
        ):
            continue
        value, error = source_label_from_candidates(
            source_label_candidates(container.image),
            source_label,
        )
        if github_repo_from_source(value):
            return value, None
        if value and not fallback:
            fallback = value
        if error is not None and first_error is None:
            first_error = error
        source = ghcr_release_source(container.image)
        if source:
            return source, None
    return fallback, first_error


def ghcr_release_source(image: str) -> str:
    repo = github_repo_from_ghcr_image(image)
    if not repo:
        return ""
    return f"https://github.com/{repo}"


def log_source_label_error(
    settings: WebSettings,
    target: WudTarget,
    error: CommandError,
) -> None:
    LOGGER.error(
        "WebUI release-note fallback: Docker inspect failed for %s; "
        "cannot read %s, so GitHub release links may be unavailable. "
        "Command: %s. stderr: %s",
        target.first,
        OCI_SOURCE_LABEL,
        error.result.display,
        sanitize_stderr(
            settings,
            error.result.stderr.strip() or "<empty>",
        ),
    )


def sanitize_stderr(settings: WebSettings, value: str) -> str:
    sanitized = _redact_unknown_absolute_paths(_redact_sensitive_text(settings, value))
    if len(sanitized) <= DOCKER_STDERR_LOG_LIMIT:
        return sanitized
    return f"{sanitized[:DOCKER_STDERR_LOG_LIMIT].rstrip()}... [truncated]"


# Compatibility aliases for callers that imported private helpers from web.py.
_release_notes_response = release_notes_response
_release_note_source_resolver = release_note_source_resolver
