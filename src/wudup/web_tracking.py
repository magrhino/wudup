"""Service-centric WUD tracking inventory and plan-first label repair."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Collection, Mapping
from contextlib import closing
from pathlib import Path
from threading import Condition

from fastapi import HTTPException, Request

from . import web_database, web_jobs, web_retags, web_wud_api
from .command import CommandError, CommandRunner
from .compose import (
    ComposeCli,
    ComposeStack,
    _compose_runtime_service_key_from_fields,
    compose_runtime_service_key_matches,
)
from .compose_rewrite import (
    _atomic_replace_compose,
    _backup_compose,
    apply_compose_tracking_label,
    compose_unescape_dollars,
    exact_tags_regex,
    render_compose_tracking_label,
)
from .db import init_db, insert_update_event, insert_update_run, open_db, utc_timestamp
from .docker_cli import DockerCli
from .images import image_tag, tag_value_valid
from .tag_streams import retag_tag_include_regex
from .updater_models import ComposeTagRewriteError, UpdaterProgressEvent
from .web_database import ReadOnlyDatabaseMissing
from .web_metadata import json_object
from .web_models import (
    ApplyJobResponse,
    TrackedContainerItem,
    TrackedContainersResponse,
    TrackingRepairApplyRequest,
    TrackingRepairPlan,
    TrackingRepairRequest,
    WebApplyJob,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_request_context import request_settings as _settings

# A deliberately small JS/Python-common subset. It prevents catastrophic regexes,
# lookarounds, backreferences, and syntax that WUD might interpret differently.
_REGEX_TOKEN = re.compile(r"(?:[A-Za-z0-9_-]|\\\.|\\d\+|\(\?:\\\.\\d\+\)\+)")
_RELEASE_PREFIX = re.compile(r"v?\d+\.\d+", re.ASCII)
_DIGEST_MARKER = "@sha256:"


def api_tracked_containers(request: Request) -> TrackedContainersResponse:
    return tracked_containers(_settings(request))


def tracked_containers(settings: WebSettings) -> TrackedContainersResponse:
    stacks_or_response = web_retags._discover_retag_stacks(settings)
    snapshot = web_wud_api.get_snapshot(settings, include_containers=True)
    if not isinstance(stacks_or_response, tuple):
        return TrackedContainersResponse(
            status="unavailable",
            count=0,
            wud_status=snapshot.status,
            warnings=stacks_or_response.warnings,
        )
    stacks = stacks_or_response
    records = web_retags._retag_target_records_for_stacks(settings, stacks)
    wud_by_key, unkeyed_wud = _wud_containers_by_key(snapshot.inventory_containers)
    discovered_services = [
        (stack, service)
        for stack in stacks
        for service in (
            stack.service_names or tuple(image.service for image in stack.service_images)
        )
    ]
    service_keys = {f"{stack.name}/{service}" for stack, service in discovered_services}
    timestamps = _history_timestamps(settings, service_keys)
    service_key_counts = Counter(
        f"{stack.name}/{service}" for stack, service in discovered_services
    )
    items = [
        _tracked_record_item(record, snapshot, wud_by_key, unkeyed_wud, timestamps)
        for record in records
    ]
    represented = {
        (record.stack.directory, record.stack.file, record.item.service)
        for record in records
    }
    warnings = [] if snapshot.status.metadata_available else [snapshot.status.detail]
    for stack in stacks:
        if not stack.service_names and not stack.service_images:
            warnings.append(
                f"Could not list services in Compose stack {stack.name}; its inventory may be incomplete."
            )
        for service in stack.service_names:
            if (stack.directory, stack.file, service) in represented:
                continue
            items.append(_unresolved_service_item(stack, service, service_key_counts, timestamps))
    items.sort(key=lambda item: (item.stack, item.service, item.target_id))
    return TrackedContainersResponse(
        status="ready",
        count=len(items),
        items=items,
        wud_status=snapshot.status,
        warnings=warnings,
    )


def _wud_containers_by_key(
    containers: Collection[web_wud_api.WudApiContainer],
) -> tuple[dict[tuple[str, str], list[web_wud_api.WudApiContainer]], bool]:
    by_key: dict[tuple[str, str], list[web_wud_api.WudApiContainer]] = {}
    unkeyed = False
    for container in containers:
        key = (
            container.labels.get("com.docker.compose.project", ""),
            container.labels.get("com.docker.compose.service", ""),
        )
        if all(key):
            by_key.setdefault(key, []).append(container)
        else:
            unkeyed = True
    return by_key, unkeyed


def _tracking_health(image: str, tag: str, regex: str, suggested: str) -> tuple[str, str, str]:
    if _DIGEST_MARKER in image:
        return "digest-pinned", "Digest-pinned image; review tracking manually.", ""
    if not regex:
        return "no-filter", "No WUD tag filter is set.", suggested
    if regex == exact_tags_regex((tag,)):
        if suggested:
            return "frozen", "The filter matches only the installed version tag.", suggested
        return "exact-tag", "The filter matches this tag only. Same-tag image changes require WUD digest watching.", suggested
    if regex == suggested:
        return "version-pattern", "The filter follows this version family.", suggested
    return "custom", "Custom tracking filter; verify its future matches.", suggested


def _wud_match_detail(
    snapshot: web_wud_api.WudApiSnapshot,
    candidates: Collection[web_wud_api.WudApiContainer],
    matches: Collection[web_wud_api.WudApiContainer],
    unkeyed: bool,
) -> tuple[str, str]:
    if not snapshot.status.metadata_available:
        return "unknown", " WUD metadata is unavailable."
    if len(matches) > 1:
        return "ambiguous", " Multiple WUD containers match this Compose service."
    if len(matches) == 1:
        return "watching", ""
    if candidates:
        return "unknown", " A same-named WUD container could not be verified against this Compose file and image."
    if unkeyed or snapshot.degraded_container_count:
        return "unknown", ""
    return "untracked", " This service is not in WUD's current inventory."


def _tracked_record_item(
    record: web_retags._RetagTargetRecord,
    snapshot: web_wud_api.WudApiSnapshot,
    wud_by_key: Mapping[tuple[str, str], list[web_wud_api.WudApiContainer]],
    unkeyed_wud: bool,
    timestamps: Mapping[str, tuple[str, str, str, int | None]],
) -> TrackedContainerItem:
    item = record.item
    tag = image_tag(item.image)
    regex = compose_unescape_dollars(item.label_value)
    health, detail, suggested = _tracking_health(item.image, tag, regex, _suggested_regex(tag, item.image))
    candidates = wud_by_key.get((record.stack.project_name, item.service), [])
    matches = [
        container for container in candidates
        if _confirmed_wud_match(container, record.stack, item.service, item.image)
    ]
    match_state, match_detail = _wud_match_detail(snapshot, candidates, matches, unkeyed_wud)
    known_at, action_at, action_status, action_run_id = timestamps.get(
        item.service_key, ("", "", "", None)
    )
    if record.service_key_ambiguous:
        known_at, action_at, action_status, action_run_id = "", "", "", None
    match = matches[0] if len(matches) == 1 else None
    return TrackedContainerItem(
        target_id=item.target_id, service_key=item.service_key, stack=item.stack,
        service=item.service, compose_path=str(record.stack.directory / record.stack.file),
        image=item.image, current_tag=tag,
        runtime_state=item.runtime_state, tracking_regex=regex,
        tracking_health=health, tracking_detail=detail + match_detail,
        suggested_regex=suggested if suggested != regex else "",
        wud=match.response() if match else None, wud_match_state=match_state,
        wud_update_available=match.update_available if match else None,
        last_image_recorded_at=known_at, last_action_at=action_at,
        last_action_status=action_status, last_action_run_id=action_run_id,
        retag_available=item.retag_available,
    )


def _unresolved_service_item(
    stack: ComposeStack, service: str, service_key_counts: Mapping[str, int],
    timestamps: Mapping[str, tuple[str, str, str, int | None]],
) -> TrackedContainerItem:
    key = f"{stack.name}/{service}"
    known_at, action_at, action_status, action_run_id = timestamps.get(key, ("", "", "", None))
    if service_key_counts[key] > 1:
        known_at, action_at, action_status, action_run_id = "", "", "", None
    return TrackedContainerItem(
        target_id=web_retags._retag_target_id_from_values(
            stack.directory, stack.file, stack.project_directory, stack.name, service,
        ),
        service_key=key, stack=stack.name, service=service,
        compose_path=str(stack.directory / stack.file), image="", current_tag="",
        runtime_state="unknown",
        tracking_health="no-image" if stack.inspection_complete else "image-unresolved",
        tracking_detail=(
            "This Compose service has no image; automatic tracking repair is unavailable."
            if stack.inspection_complete else
            "Compose image and tracking label could not be resolved; review this service manually."
        ),
        wud_match_state="unknown", last_image_recorded_at=known_at,
        last_action_at=action_at, last_action_status=action_status,
        last_action_run_id=action_run_id,
    )


def _confirmed_wud_match(
    container: web_wud_api.WudApiContainer,
    stack: ComposeStack,
    service: str,
    image: str,
) -> bool:
    if not stack.project_name or not image or container.image != image:
        return False
    if container.status.casefold() != "running":
        return False
    labels = container.labels
    runtime_key = _compose_runtime_service_key_from_fields((
        labels.get("com.docker.compose.project.working_dir", ""),
        labels.get("com.docker.compose.project.config_files", ""),
        labels.get("com.docker.compose.project", ""),
        labels.get("com.docker.compose.service", ""),
        labels.get("com.docker.compose.oneoff", ""),
    ))
    return runtime_key is not None and compose_runtime_service_key_matches(
        web_retags._retag_compose_service_key(stack, service), runtime_key
    )


def _history_timestamps(
    settings: WebSettings, service_keys: Collection[str]
) -> dict[str, tuple[str, str, str, int | None]]:
    if not service_keys:
        return {}
    try:
        with closing(web_database.connect_readonly_db(settings)) as conn:
            timestamps: dict[str, tuple[str, str, str, int | None]] = {}
            for key in service_keys:
                stack, sep, service = key.partition("/")
                if not sep:
                    continue
                known = conn.execute(
                    "SELECT updated_at FROM known_images WHERE service_key = ?", (key,)
                ).fetchone()
                action = conn.execute(
                    "SELECT created_at, status, run_id FROM update_events "
                    "WHERE stack_name = ? AND service_name = ? ORDER BY id DESC LIMIT 1",
                    (stack, service),
                ).fetchone()
                timestamps[key] = (
                    str(known["updated_at"]) if known is not None else "",
                    str(action["created_at"]) if action is not None else "",
                    str(action["status"]) if action is not None else "",
                    int(action["run_id"]) if action is not None else None,
                )
        return timestamps
    except ReadOnlyDatabaseMissing:
        return {}
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not read container history", exc),
        ) from exc


def _suggested_regex(tag: str, image: str) -> str:
    if _DIGEST_MARKER in image or not tag_value_valid(tag) or not _release_shaped_tag(tag):
        return ""
    try:
        return retag_tag_include_regex(tag)
    except ValueError:
        return ""


def _release_shaped_tag(tag: str) -> bool:
    prefix = _RELEASE_PREFIX.match(tag)
    if prefix is None:
        return False
    suffix = tag[prefix.end():]
    return not suffix or (len(suffix) > 1 and suffix[0] in "-_." and suffix[1].isalnum())


def _validated_regex(regex: str, tag: str) -> None:
    if not regex.startswith("^") or not regex.endswith("$"):
        raise HTTPException(status_code=422, detail="Tracking regex must be anchored with ^ and $.")
    has_numeric_wildcard = _has_numeric_wildcard(regex[1:-1])
    if (not has_numeric_wildcard and regex != exact_tags_regex((tag,))) or re.fullmatch(
        regex, tag, flags=re.ASCII
    ) is None:
        raise HTTPException(
            status_code=422,
            detail="Tracking regex must match the installed tag and either include a numeric wildcard or match that tag exactly.",
        )


def _has_numeric_wildcard(body: str) -> bool:
    offset = 0
    has_numeric_wildcard = False
    unseparated_wildcard = False
    while offset < len(body):
        match = _REGEX_TOKEN.match(body, offset)
        if match is None:
            raise HTTPException(
                status_code=422,
                detail="Tracking regex uses unsupported syntax. Use literals, escaped dots, and \\d+ version parts.",
            )
        token = match.group()
        if token == "\\d+":
            if unseparated_wildcard:
                raise HTTPException(
                    status_code=422,
                    detail="Tracking regex must separate numeric wildcards with a literal non-digit character.",
                )
            has_numeric_wildcard = True
            unseparated_wildcard = True
        elif token == r"(?:\.\d+)+":
            has_numeric_wildcard = True
            unseparated_wildcard = True
        elif token == r"\." or not token.isdigit():
            unseparated_wildcard = False
        offset = match.end()
    return has_numeric_wildcard


def _matching_runtime_image_id(
    settings: WebSettings, stack: ComposeStack, service: str, image: str
) -> str:
    runner = CommandRunner(env=settings.command_env) if settings.command_env is not None else CommandRunner()
    compose = ComposeCli(runner=runner)
    try:
        container_ids = compose.ps_quiet_checked(
            stack.directory, stack.file, [service],
            project_directory=stack.project_directory,
        )
    except CommandError:
        return ""
    if len(container_ids) != 1:
        return ""
    docker = DockerCli(runner=runner)
    running_image_id = docker.try_container_image_id(container_ids[0])
    return running_image_id if running_image_id and running_image_id == docker.image_id(image) else ""


def api_tracking_repair_plan(
    payload: TrackingRepairRequest, request: Request
) -> TrackingRepairPlan:
    return build_tracking_repair_plan(_settings(request), payload)[0]


def build_tracking_repair_plan(
    settings: WebSettings, payload: TrackingRepairRequest
) -> tuple[TrackingRepairPlan, web_retags._RetagTargetRecord]:
    records = web_retags._retag_target_records(settings)
    if not isinstance(records, tuple):
        raise HTTPException(status_code=503, detail="Compose services could not be discovered.")
    matches = [record for record in records if record.item.target_id == payload.target_id]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="Tracking target was not found.")
    record = matches[0]
    item = record.item
    if _DIGEST_MARKER in item.image:
        raise HTTPException(status_code=422, detail="Digest-pinned images need manual tracking review.")
    tag = image_tag(item.image)
    _validated_regex(payload.regex, tag)
    current = compose_unescape_dollars(item.label_value)
    if current == payload.regex:
        raise HTTPException(status_code=422, detail="Tracking regex is already set to this value.")
    compose_path = record.stack.directory / record.stack.file
    try:
        source = compose_path.read_bytes().decode("utf-8")
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        rendered = render_compose_tracking_label(
            compose_path, item.service, item.image, current, payload.regex,
            expected_source_hash=source_hash,
        )
    except (OSError, ComposeTagRewriteError) as exc:
        raise HTTPException(
            status_code=409,
            detail=_safe_exception_detail(settings, "could not preview tracking repair", exc),
        ) from exc
    old_label = f"(set) {current}" if current else "(not set)"
    if current and not current.isprintable():
        old_label = f"(set) {current!r}"
    diff = f"wud.tag.include:\n- {old_label}\n+ (set) {payload.regex}\n"
    issues = []
    if record.service_key_ambiguous:
        issues.append("Duplicate service identity; choose an unambiguous Compose service.")
    if item.runtime_state != "running":
        issues.append("Service must be confirmed running before automatic recreation.")
    runtime_image_id = (
        _matching_runtime_image_id(settings, record.stack, item.service, item.image)
        if item.runtime_state == "running" else ""
    )
    if item.runtime_state == "running" and not runtime_image_id:
        issues.append("Running image differs from the local Compose image or could not be verified; repair tracking only after the image is reconciled.")
    plan_id = hashlib.sha256(
        "\0".join((payload.target_id, payload.regex, item.image, current, source, runtime_image_id)).encode()
    ).hexdigest()
    return TrackingRepairPlan(
        plan_id=plan_id,
        source_hash=source_hash,
        rendered_hash=hashlib.sha256(rendered.encode()).hexdigest(),
        target_id=item.target_id,
        service_key=item.service_key,
        stack=item.stack,
        service=item.service,
        image=item.image,
        current_regex=current,
        proposed_regex=payload.regex,
        compose_diff=diff,
        will_recreate=True,
        can_apply=not issues,
        issues=issues,
    ), record


def api_apply_tracking_repair(
    payload: TrackingRepairApplyRequest, request: Request
) -> ApplyJobResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    plan, _record = build_tracking_repair_plan(settings, payload)
    if not secrets.compare_digest(plan.plan_id, payload.plan_id) or not plan.can_apply:
        raise HTTPException(status_code=409, detail="Tracking repair plan is stale or blocked.")
    state = request.app.state
    condition: Condition = state.web_apply_condition
    jobs: dict[str, WebApplyJob] = state.web_apply_jobs
    with condition:
        active_error = web_jobs._active_mutation_error_unlocked(state)
        if active_error:
            raise HTTPException(status_code=409, detail=active_error)
        job = WebApplyJob(id=secrets.token_urlsafe(18), status="queued", selected_line_numbers=())
        web_jobs._register_apply_job_unlocked(jobs, job)
        condition.notify_all()
        try:
            state.web_apply_executor.submit(
                _run_tracking_repair, settings, payload, jobs, condition, job.id
            )
        except Exception:
            del jobs[job.id]
            condition.notify_all()
            raise
        return web_jobs._apply_job_response(job)


def _revalidate_tracking_repair(
    settings: WebSettings, payload: TrackingRepairApplyRequest,
) -> tuple[TrackingRepairPlan, web_retags._RetagTargetRecord, str, CommandRunner, ComposeCli]:
    plan, record = build_tracking_repair_plan(settings, payload)
    if not secrets.compare_digest(plan.plan_id, payload.plan_id) or not plan.can_apply:
        raise RuntimeError("Tracking repair plan changed; preview it again.")
    stack = record.stack
    item = record.item
    expected_image_id = _matching_runtime_image_id(settings, stack, item.service, item.image)
    if not expected_image_id:
        raise RuntimeError("Running image changed before tracking repair; reconcile the image and preview again.")
    runner = CommandRunner(env=settings.command_env) if settings.command_env is not None else CommandRunner()
    compose = ComposeCli(runner=runner)
    if compose.try_config_project_name(
        stack.directory, stack.file, project_directory=stack.project_directory
    ) != stack.project_name:
        raise RuntimeError("Compose project changed before tracking repair.")
    running = web_retags._running_retag_compose_service_keys(settings)
    if running is None or web_retags._retag_compose_service_key(stack, item.service) not in running:
        raise RuntimeError("Selected service is no longer confirmed running.")
    return plan, record, expected_image_id, runner, compose


def _verify_recreated_service(
    compose: ComposeCli, runner: CommandRunner, record: web_retags._RetagTargetRecord,
    expected_image_id: str, rendered_hash: str,
) -> None:
    stack = record.stack
    item = record.item
    _require_approved_compose_source(stack.directory / stack.file, rendered_hash)
    container_ids = compose.ps_quiet(
        stack.directory, stack.file, [item.service], project_directory=stack.project_directory,
    )
    if len(container_ids) != 1:
        raise RuntimeError("Service did not restart after tracking repair.")
    if DockerCli(runner=runner).try_container_image_id(container_ids[0]) != expected_image_id:
        raise RuntimeError("Service restarted with a different image; inspect it before retrying tracking repair.")


def _rollback_tracking_repair(
    settings: WebSettings, record: web_retags._RetagTargetRecord,
    plan: TrackingRepairPlan, backup: Path, recreate_started: bool,
    expected_image_id: str,
) -> str:
    try:
        _atomic_replace_compose(
            record.stack.directory / record.stack.file,
            backup.read_bytes().decode("utf-8"),
            prefix="tracking-rollback", expected_source_hash=plan.rendered_hash,
        )
        if recreate_started:
            restore_runner = CommandRunner(env=settings.command_env) if settings.command_env is not None else CommandRunner()
            if DockerCli(runner=restore_runner).image_id(record.item.image) != expected_image_id:
                raise RuntimeError("Local image changed; automatic recreation would use a different image")
            ComposeCli(runner=restore_runner).up(
                record.stack.directory, record.stack.file, [record.item.service],
                force_recreate=True, no_deps=True, remove_orphans=False,
                project_directory=record.stack.project_directory,
            )
    except Exception as exc:  # noqa: BLE001 - preserve backup for manual recovery.
        return _safe_exception_detail(settings, "rollback failed", exc)
    return ""


def _run_tracking_repair(
    settings: WebSettings,
    payload: TrackingRepairApplyRequest,
    jobs: dict[str, WebApplyJob],
    condition: Condition,
    job_id: str,
) -> None:
    web_jobs._update_apply_job(
        jobs, condition, job_id, status="running", started_at=utc_timestamp()
    )
    lock = None
    backup: Path | None = None
    run_id: int | None = None
    plan: TrackingRepairPlan | None = None
    record: web_retags._RetagTargetRecord | None = None
    changed = False
    recreate_started = False
    expected_image_id = ""
    retain_backup = False
    try:
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="preflight", status="running", message="Revalidating Compose and runtime state."),
        )
        lock = web_jobs._acquire_apply_wud_lock(settings)
        plan, record, expected_image_id, runner, compose = _revalidate_tracking_repair(
            settings, payload
        )
        stack = record.stack
        item = record.item
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="preflight", status="success", message="Selected service and plan revalidated."),
        )
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            run_id = insert_update_run(
                conn, status="running", mode="web-tracking-repair", dry_run=False,
                wud_file=str(settings.config.wud_out_file),
                metadata_json=json_object({"source": "webui", "operation": "tracking-repair", "target_id": item.target_id, "plan_id": plan.plan_id}),
            )
        path = stack.directory / stack.file
        backup = _backup_compose(path)
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="compose", status="running", message="Writing the tracking label in Compose."),
        )
        apply_compose_tracking_label(
            path, item.service, item.image, plan.current_regex, plan.proposed_regex,
            expected_source_hash=plan.source_hash,
        )
        changed = True
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="compose", status="success", message="Tracking label written; image unchanged."),
        )
        # Recreate only this service; ComposeCli.up explicitly forbids pull and build.
        _require_approved_compose_source(path, plan.rendered_hash)
        if _matching_runtime_image_id(settings, stack, item.service, item.image) != expected_image_id:
            raise RuntimeError("Running image changed before tracking repair; reconcile the image and preview again.")
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="recreate", status="running", message="Recreating only the selected service without a pull."),
        )
        _require_approved_compose_source(path, plan.rendered_hash)
        recreate_started = True
        compose.up(
            stack.directory, stack.file, [item.service],
            force_recreate=True, no_deps=True, remove_orphans=False,
            wait=True, wait_timeout=web_retags._effective_config(settings).max_wait,
            project_directory=stack.project_directory,
        )
        _verify_recreated_service(compose, runner, record, expected_image_id, plan.rendered_hash)
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="recreate", status="success", message="Selected service is running after recreation."),
        )
        _finish_tracking_audit(settings, run_id, plan, "success")
        web_jobs._append_apply_job_progress(
            jobs, condition, job_id,
            UpdaterProgressEvent(phase="completion", status="success", message="Tracking label repaired and service recreated."),
        )
        web_jobs._update_apply_job(
            jobs, condition, job_id, status="success", run_id=run_id,
            finished_at=utc_timestamp(),
        )
    except Exception as exc:  # noqa: BLE001 - job reports and audits every failure.
        retain_backup = _finish_failed_tracking_repair(
            settings, jobs, condition, job_id, exc, run_id, plan, record, backup,
            changed, recreate_started, expected_image_id,
        )
    finally:
        if backup is not None and not retain_backup:
            backup.unlink(missing_ok=True)
        if lock is not None:
            lock.close()


def _finish_failed_tracking_repair(
    settings: WebSettings, jobs: dict[str, WebApplyJob], condition: Condition,
    job_id: str, exc: Exception, run_id: int | None, plan: TrackingRepairPlan | None,
    record: web_retags._RetagTargetRecord | None, backup: Path | None,
    changed: bool, recreate_started: bool, expected_image_id: str,
) -> bool:
    rollback_error = (
        _rollback_tracking_repair(
            settings, record, plan, backup, recreate_started, expected_image_id,
        )
        if changed and backup is not None and record is not None and plan is not None
        else ""
    )
    error = _safe_exception_detail(settings, "tracking repair failed", exc)
    if rollback_error:
        error += f"; {rollback_error}. Compose backup was preserved for manual recovery"
    if run_id is not None and plan is not None:
        try:
            _finish_tracking_audit(settings, run_id, plan, "failure", error)
        except Exception as audit_exc:  # noqa: BLE001 - job must become terminal.
            error += "; " + _safe_exception_detail(
                settings, "audit record could not be finalized", audit_exc
            )
    web_jobs._append_apply_job_progress(
        jobs, condition, job_id,
        UpdaterProgressEvent(phase="completion", status="failure", message=error),
    )
    web_jobs._update_apply_job(
        jobs, condition, job_id, status="failure", run_id=run_id,
        finished_at=utc_timestamp(), error=error,
    )
    return bool(rollback_error)


def _require_approved_compose_source(path: Path, rendered_hash: str) -> None:
    try:
        current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("Compose file could not be verified before completing tracking repair.") from exc
    if not secrets.compare_digest(current_hash, rendered_hash):
        raise RuntimeError(
            "Compose file changed during tracking repair; inspect the selected service before retrying."
        )


def _finish_tracking_audit(
    settings: WebSettings, run_id: int, plan: TrackingRepairPlan, status: str,
    error: str = "",
) -> None:
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        now = utc_timestamp()
        with conn:
            insert_update_event(
                conn, run_id=run_id, created_at=now, service_name=plan.service,
                stack_name=plan.stack, image=plan.image, target_image=plan.image,
                status=status, commit=False,
                metadata_json=json_object({"source": "webui", "operation": "tracking-repair", "target_id": plan.target_id, "old_regex": plan.current_regex, "new_regex": plan.proposed_regex, "error": error}),
            )
            conn.execute(
                "UPDATE update_runs SET finished_at = ?, status = ? WHERE id = ?",
                (now, status, run_id),
            )
