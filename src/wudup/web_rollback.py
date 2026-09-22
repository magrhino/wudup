"""Read-only rollback planning for recorded updater runs."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing

from fastapi import HTTPException, Request

from .command import CommandError, CommandRunner
from .compose import ComposeCli, ComposeDiscoveryError, ComposeStack, ServiceImage
from .config import VALID_UPDATE_MODES
from .db import DatabaseError
from .docker_cli import DockerCli
from .images import image_with_digest, normalize_digest
from .web_database import ReadOnlyDatabaseMissing, connect_readonly_db
from .web_models import (
    RollbackPlanItem,
    RollbackPlanResponse,
    RunEventRecord,
    RunSummary,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_request_context import request_settings as _settings
from .web_runs import _event_from_row, _run_summary_from_row
from .web_settings import _effective_config

_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def api_rollback_plan(run_id: int, request: Request) -> RollbackPlanResponse:
    settings = _settings(request)
    try:
        with closing(connect_readonly_db(settings)) as conn:
            run_row = conn.execute(
                "SELECT * FROM update_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise HTTPException(status_code=404, detail="run not found")
            event_rows = conn.execute(
                """
                SELECT *
                FROM update_events
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
            superseding_runs = _superseding_runs(conn, run_id)
    except ReadOnlyDatabaseMissing as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read database", exc),
        ) from exc

    run = _run_summary_from_row(run_row)
    events = [_event_from_row(row) for row in event_rows]
    return build_rollback_plan(settings, run, events, superseding_runs)


def build_rollback_plan(
    settings: WebSettings,
    run: RunSummary,
    events: Sequence[RunEventRecord],
    superseding_runs: Mapping[tuple[str, str], int] | None = None,
) -> RollbackPlanResponse:
    not_applicable = _not_applicable_reason(run, events)
    if not_applicable:
        return RollbackPlanResponse(
            run_id=run.id,
            status="not_applicable",
            detail=not_applicable,
        )
    superseding_runs = superseding_runs or {}
    recorded_items = [
        _recorded_rollback_item(
            event,
            superseding_runs.get((event.stack_name, event.service_name)),
        )
        for event in events
    ]
    pending_events = [
        event
        for event, item in zip(events, recorded_items, strict=True)
        if item is None
    ]
    if not pending_events:
        return _rollback_response(
            run.id,
            [item for item in recorded_items if item is not None],
        )

    runner = CommandRunner(env=settings.command_env)
    compose = ComposeCli(runner=runner)
    docker = DockerCli(runner=runner)
    try:
        config = _effective_config(settings)
        stacks = compose.discover_stacks(
            config.docker_base,
            project_base=settings.host_docker_base,
            ignore_paths=config.compose_ignore_paths,
            required_stack_names={event.stack_name for event in pending_events},
        )
    except ComposeDiscoveryError as exc:
        unavailable_items = iter(
            _blocked_item(
                _item_values(event),
                "The current Compose state could not be verified.",
            )
            for event in pending_events
        )
        items = [
            item if item is not None else next(unavailable_items)
            for item in recorded_items
        ]
        return RollbackPlanResponse(
            run_id=run.id,
            status="unavailable",
            detail=_safe_exception_detail(
                settings,
                "could not verify current Compose state",
                exc,
            ),
            blocked_count=sum(item.status == "blocked" for item in items),
            not_needed_count=sum(item.status == "not_needed" for item in items),
            items=items,
        )

    live_items = iter(
        _live_rollback_item(event, stacks, compose, docker)
        for event in pending_events
    )
    items = [
        item if item is not None else next(live_items) for item in recorded_items
    ]
    return _rollback_response(run.id, items)


def _not_applicable_reason(
    run: RunSummary,
    events: Sequence[RunEventRecord],
) -> str:
    if run.dry_run:
        return "Dry runs do not change services, so there is nothing to roll back."
    if run.mode not in VALID_UPDATE_MODES:
        return "This run is not an updater apply run."
    if not events:
        return "This run has no recorded update events."
    return ""


def _superseding_runs(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[tuple[str, str], int]:
    modes = tuple(sorted(VALID_UPDATE_MODES))
    placeholders = ", ".join("?" for _ in modes)
    rows = conn.execute(
        f"""
        SELECT e.stack_name, e.service_name, MAX(e.run_id) AS latest_run_id
        FROM update_events AS e
        JOIN update_runs AS r ON r.id = e.run_id
        WHERE e.run_id > ?
          AND e.status = 'success'
          AND r.mode IN ({placeholders})
        GROUP BY e.stack_name, e.service_name
        """,
        (run_id, *modes),
    ).fetchall()
    return {
        (str(row["stack_name"]), str(row["service_name"])): int(
            row["latest_run_id"]
        )
        for row in rows
    }


def _recorded_rollback_item(
    event: RunEventRecord,
    superseding_run_id: int | None,
) -> RollbackPlanItem | None:
    base = _item_values(event)
    if event.status != "success":
        return _blocked_item(
            base, "The recorded update event did not complete successfully."
        )
    if _recorded_no_change(event):
        return RollbackPlanItem(
            **base,
            status="not_needed",
            reason="The image reference and image ID did not change.",
        )
    if not all(
        (
            event.stack_name,
            event.service_name,
            event.image,
            event.target_image,
            event.old_image_id,
            event.new_image_id,
        )
    ):
        return _blocked_item(
            base, "The event is missing stack, service, image, or image ID evidence."
        )
    if superseding_run_id is not None:
        return _blocked_item(
            base,
            f"A later successful updater run #{superseding_run_id} changed this service.",
        )
    if not base["rollback_image"]:
        return _blocked_item(
            base, "The event does not contain a valid exact previous sha256 digest."
        )
    return None


def _live_rollback_item(
    event: RunEventRecord,
    stacks: Sequence[ComposeStack],
    compose: ComposeCli,
    docker: DockerCli,
) -> RollbackPlanItem:
    base = _item_values(event)

    matches = _stack_service_matches(stacks, event.stack_name, event.service_name)
    if len(matches) != 1:
        return _blocked_item(
            base,
            (
                "The current Compose service could not be found uniquely."
                if not matches
                else "More than one current Compose service matches this event."
            ),
        )
    stack, service = matches[0]
    base["current_compose_image"] = service.image
    if service.image != event.target_image:
        return _blocked_item(
            base,
            "The current Compose image no longer matches the recorded target image.",
        )

    try:
        container_ids = compose.ps_quiet(
            stack.directory,
            stack.file,
            [event.service_name],
            project_directory=stack.project_directory,
        )
    except CommandError:
        return _blocked_item(
            base, "The running containers for this service could not be inspected."
        )
    if not container_ids:
        return _blocked_item(base, "No running container was found for this service.")

    image_ids = [docker.try_container_image_id(container) for container in container_ids]
    base["current_container_image_ids"] = sorted(set(filter(None, image_ids)))
    if any(not image_id for image_id in image_ids):
        return _blocked_item(
            base, "At least one running container image ID could not be inspected."
        )
    if any(image_id != event.new_image_id for image_id in image_ids):
        return _blocked_item(
            base,
            "At least one running container no longer uses the recorded new image ID.",
        )

    if docker.image_id(str(base["rollback_image"])) != event.old_image_id:
        return _blocked_item(
            base, "The exact previous image is no longer available locally."
        )
    return RollbackPlanItem(
        **base,
        status="ready",
        reason="Current and previous image state was verified from Docker and Compose.",
    )


def _item_values(event: RunEventRecord) -> dict[str, object]:
    service_key = "/".join(filter(None, (event.stack_name, event.service_name)))
    previous_digest = normalize_digest(event.old_digest)
    rollback_image = (
        image_with_digest(event.image, previous_digest)
        if event.image and _SHA256_DIGEST_RE.fullmatch(previous_digest)
        else ""
    )
    return {
        "event_id": event.id,
        "service_key": service_key,
        "stack_name": event.stack_name,
        "service_name": event.service_name,
        "recorded_previous_image": event.image,
        "recorded_target_image": event.target_image,
        "rollback_image": rollback_image,
        "previous_image_id": event.old_image_id,
        "previous_digest": event.old_digest,
    }


def _blocked_item(base: Mapping[str, object], reason: str) -> RollbackPlanItem:
    return RollbackPlanItem(**base, status="blocked", reason=reason)


def _recorded_no_change(event: RunEventRecord) -> bool:
    return (
        event.status == "success"
        and bool(event.image)
        and event.image == event.target_image
        and bool(event.old_image_id)
        and event.old_image_id == event.new_image_id
    )


def _stack_service_matches(
    stacks: Sequence[ComposeStack],
    stack_name: str,
    service_name: str,
) -> list[tuple[ComposeStack, ServiceImage]]:
    return [
        (stack, service)
        for stack in stacks
        if stack.name == stack_name
        for service in stack.service_images
        if service.service == service_name
    ]


def _rollback_response(
    run_id: int,
    items: Sequence[RollbackPlanItem],
) -> RollbackPlanResponse:
    ready = sum(item.status == "ready" for item in items)
    blocked = sum(item.status == "blocked" for item in items)
    not_needed = sum(item.status == "not_needed" for item in items)
    if ready and blocked:
        status = "partial"
        detail = "Some services have verified rollback targets; others are blocked."
    elif blocked:
        status = "blocked"
        detail = "No service has a verified rollback target; review each blocker."
    elif ready:
        status = "ready"
        detail = "The listed services have verified local rollback targets."
    else:
        status = "not_needed"
        detail = "The recorded events do not contain an image change to roll back."
    return RollbackPlanResponse(
        run_id=run_id,
        status=status,
        detail=detail,
        ready_count=ready,
        blocked_count=blocked,
        not_needed_count=not_needed,
        items=list(items),
    )
