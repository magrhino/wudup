"""WebUI self-update and container restart endpoints."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any, Literal, cast

from fastapi import BackgroundTasks, HTTPException, Request

from . import __version__, web_jobs
from .banner import (
    current_tag,
    fetch_latest_release_tag,
    release_check_enabled,
    release_update_available,
)
from .command import CommandError, CommandRunner
from .compose import ComposeCli
from .compose_rewrite import (
    _backup_compose,
    apply_compose_digest_pins,
    apply_compose_tag_updates,
)
from .config import UpdaterConfig
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .digest_verifier import DigestVerifier, DockerManifestResolver
from .docker_cli import DockerCli
from .images import (
    image_has_tag,
    image_with_tag,
    normalize_digest,
    tag_value_valid,
)
from .plans import DryRunPlan, PlanFileMissing, PlanInputError, build_dry_run_plan
from .release_notes import detect_breaking
from .self_update import (
    DEFAULT_SELF_UPDATE_IMAGE,
    current_container_image,
    release_self_update_target,
    self_update_image_variant_known,
)
from .updater_digest_pin import digest_pin_update_from_values
from .updater_models import ComposeTagRewriteError, DigestPinUpdate, TagUpdate
from .web_auth import (
    WebConfigError,
    _immediate_transaction,
    _parse_bool,
    _redact_sensitive_text,
    _request_actor_type,
    _safe_exception_detail,
    _settings,
)
from .web_metadata import json_object as _json_object
from .web_metadata import json_object_or_empty
from .web_models import (
    SELF_UPDATE_RELEASE_NOTES_CAP,
    ContainerRestartRequest,
    ContainerRestartResponse,
    PlanResponse,
    SelfUpdateApplyResponse,
    SelfUpdateAuditStatus,
    SelfUpdatePlanResponse,
    SelfUpdatePrepareRequest,
    SelfUpdatePrepareResponse,
    SelfUpdateReleaseNote,
    SelfUpdateRequest,
    SelfUpdateResponse,
    SelfUpdateStrategy,
    WebSelfUpdatePlan,
    WebSettings,
)

LOGGER = logging.getLogger(__name__)

SELF_UPDATE_RELEASES_URL = "https://api.github.com/repos/magrhino/wudup/releases"
SELF_UPDATE_PLAN_TTL_SECONDS = 30 * 60
CONTAINER_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

EffectiveConfigLoader = Callable[[WebSettings], UpdaterConfig]
PlanResponseBuilder = Callable[[DryRunPlan, WebSettings, Request], PlanResponse]

_effective_config_loader: EffectiveConfigLoader | None = None
_plan_response_builder: PlanResponseBuilder | None = None


def configure(
    *,
    effective_config_loader: EffectiveConfigLoader,
    plan_response_builder: PlanResponseBuilder,
) -> None:
    """Configure callbacks supplied by the main WebUI module."""

    global _effective_config_loader, _plan_response_builder
    _effective_config_loader = effective_config_loader
    _plan_response_builder = plan_response_builder


def api_self_update(request: Request) -> SelfUpdateResponse:
    return _self_update_response(_settings(request))


def api_plan_self_update(request: Request) -> SelfUpdatePlanResponse:
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)

    status = _self_update_response(settings)
    if not status.can_update:
        detail = status.disabled_reason or "self-update is not available"
        raise HTTPException(status_code=409, detail=detail)
    if status.strategy != "prepare_tag_update":
        raise HTTPException(
            status_code=409,
            detail="self-update target does not require tag update preparation",
        )

    try:
        plan, cached = _build_self_update_tag_plan(settings, status)
    except PlanInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not create self-update tag plan",
                exc,
            ),
        ) from exc

    plan_response = _plan_response(plan, settings, request)
    try:
        _validate_self_update_prepare_plan(plan_response)
    except HTTPException:
        _delete_self_update_plan_file(cached)
        raise
    _cache_self_update_plan(request.app.state, cached)
    return SelfUpdatePlanResponse(
        strategy="prepare_tag_update",
        plan=plan_response,
        current_tag=status.current_tag,
        latest_tag=status.latest_tag,
        current_image=status.current_image,
        target_image=status.target_image,
        restart_container=status.restart_container,
    )


def api_apply_self_update(
    payload: SelfUpdateRequest,
    request: Request,
) -> SelfUpdateApplyResponse:
    _ = payload.confirmation
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    reservation_error = web_jobs._reserve_self_update(request.app.state)
    if reservation_error:
        raise HTTPException(status_code=409, detail=reservation_error)

    try:
        status = _self_update_response(settings)
        if not status.can_update:
            detail = status.disabled_reason or "self-update is not available"
            raise HTTPException(status_code=409, detail=detail)
        if status.strategy != "pull_image":
            raise HTTPException(
                status_code=409,
                detail="self-update target requires tag update preparation",
            )
        if _self_update_confirmation_stale(payload, status):
            raise HTTPException(status_code=409, detail="self-update target is stale")
        (
            audit_run_id,
            verified_running_image_id,
            prepared_image_id,
            running_image_verified,
        ) = _apply_self_update_pull(
            settings,
            request,
            status,
        )
    finally:
        web_jobs._release_self_update(request.app.state)

    return SelfUpdateApplyResponse(
        status=(
            "running_image_verified" if running_image_verified else "prepared_only"
        ),
        audit_run_id=audit_run_id,
        current_tag=status.current_tag,
        latest_tag=status.latest_tag,
        target_image=status.target_image,
        container=status.restart_container,
        running_image_id=verified_running_image_id,
        prepared_image_id=prepared_image_id,
        external_recreate_required=not running_image_verified,
    )


def _apply_self_update_pull(
    settings: WebSettings,
    request: Request,
    status: SelfUpdateResponse,
) -> tuple[int, str, str, bool]:
    docker = DockerCli(runner=CommandRunner(env=settings.command_env))
    try:
        container_id = docker.container_id(status.restart_container)
    except CommandError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not inspect restart container",
                exc,
            ),
        ) from exc
    if not container_id:
        raise HTTPException(
            status_code=500,
            detail="could not inspect restart container",
        )
    running_image_id = docker.try_container_image_id(status.restart_container)
    if not running_image_id:
        raise HTTPException(
            status_code=500,
            detail="could not inspect running container image identity",
        )

    audit_run_id = _record_self_update_audit(settings, request, status)
    try:
        docker.pull_image(status.target_image)
    except CommandError as exc:
        detail = exc.result.stderr.strip() or str(exc)
        LOGGER.error(
            "WebUI self-update image pull failed for %s -> %s: %s",
            status.target_image,
            status.restart_container,
            _redact_sensitive_text(settings, detail),
        )
        _safe_update_self_update_audit(
            settings,
            audit_run_id,
            status="failure",
            error=_redact_sensitive_text(settings, detail),
        )
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not pull self-update image",
                exc,
            ),
        ) from exc
    prepared_image_id = docker.image_id(status.target_image)
    verified_running_image_id = docker.try_container_image_id(status.restart_container)
    if not prepared_image_id or not verified_running_image_id:
        _safe_update_self_update_audit(
            settings,
            audit_run_id,
            status="failure",
            error="could not verify self-update image identities after pull",
        )
        raise HTTPException(
            status_code=500,
            detail="could not verify self-update image identities after pull",
        )
    running_image_verified = verified_running_image_id == prepared_image_id
    audit_status: SelfUpdateAuditStatus = (
        "running_image_verified" if running_image_verified else "image_prepared"
    )
    _safe_update_self_update_audit(
        settings,
        audit_run_id,
        status=audit_status,
        metadata_extra={
            "running_image_id_before": running_image_id,
            "running_image_id_after": verified_running_image_id,
            "prepared_image_id": prepared_image_id,
            "external_recreate_required": not running_image_verified,
        },
    )
    return (
        audit_run_id,
        verified_running_image_id,
        prepared_image_id,
        running_image_verified,
    )


def _record_self_update_audit(
    settings: WebSettings,
    request: Request,
    status: SelfUpdateResponse,
) -> int:
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                return _insert_self_update_audit(
                    conn,
                    settings,
                    request,
                    status=status,
                )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not record self-update audit",
                exc,
            ),
        ) from exc


def api_prepare_self_update(
    payload: SelfUpdatePrepareRequest,
    request: Request,
) -> SelfUpdatePrepareResponse:
    _ = payload.confirmation
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    reservation_error = web_jobs._reserve_self_update(request.app.state)
    if reservation_error:
        raise HTTPException(status_code=409, detail=reservation_error)

    audit_run_id: int | None = None
    status: SelfUpdateResponse | None = None
    try:
        status = _self_update_response(settings)
        if not status.can_update:
            detail = status.disabled_reason or "self-update is not available"
            raise HTTPException(status_code=409, detail=detail)
        if status.strategy != "prepare_tag_update":
            raise HTTPException(
                status_code=409,
                detail="self-update target does not require tag update preparation",
            )
        if _self_update_confirmation_stale(payload, status):
            raise HTTPException(status_code=409, detail="self-update target is stale")

        cached = _require_self_update_cached_plan(
            request.app.state,
            payload.plan_id,
        )
        if _self_update_cached_plan_stale(cached, status):
            raise HTTPException(status_code=409, detail="self-update plan is stale")
        try:
            plan = _rebuild_self_update_cached_plan(settings, cached)
        except (PlanInputError, PlanFileMissing, OSError) as exc:
            raise HTTPException(
                status_code=409,
                detail="self-update plan is stale",
            ) from exc
        if plan.plan_id != payload.plan_id:
            raise HTTPException(status_code=409, detail="self-update plan is stale")
        plan_response = _plan_response(plan, settings, request)
        _validate_self_update_prepare_plan(plan_response)

        docker = DockerCli(runner=CommandRunner(env=settings.command_env))
        try:
            container_id = docker.container_id(status.restart_container)
        except CommandError as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not inspect restart container",
                    exc,
                ),
            ) from exc
        if not container_id:
            raise HTTPException(
                status_code=500,
                detail="could not inspect restart container",
            )

        try:
            with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
                init_db(conn)
                with _immediate_transaction(conn):
                    audit_run_id = _insert_self_update_audit(
                        conn,
                        settings,
                        request,
                        status=status,
                    )
        except (OSError, sqlite3.Error, DatabaseError) as exc:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not record self-update audit",
                    exc,
                ),
            ) from exc

        try:
            metadata = _prepare_self_update_tag_update(settings, plan_response)
        except (CommandError, ComposeTagRewriteError, OSError, RuntimeError) as exc:
            detail = _redact_sensitive_text(settings, str(exc))
            LOGGER.error(
                "WebUI self-update tag prepare failed for %s -> %s: %s",
                status.current_image,
                status.target_image,
                detail,
            )
            _safe_update_self_update_audit(
                settings,
                audit_run_id,
                status="failure",
                error=detail,
            )
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not prepare self-update tag update",
                    exc,
                ),
            ) from exc
        _safe_update_self_update_audit(
            settings,
            audit_run_id,
            status="tag_prepared",
            metadata_extra=metadata,
        )
    finally:
        web_jobs._release_self_update(request.app.state)
        _remove_self_update_cached_plan(request.app.state, payload.plan_id)

    return SelfUpdatePrepareResponse(
        status="tag_prepared",
        audit_run_id=audit_run_id,
        current_tag=status.current_tag,
        latest_tag=status.latest_tag,
        target_image=status.target_image,
        container=status.restart_container,
        external_recreate_required=True,
    )


def api_restart_container(
    payload: ContainerRestartRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> ContainerRestartResponse:
    _ = payload.confirmation
    settings = _settings(request)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    active_error = web_jobs._active_mutation_error(request)
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    container = settings.restart_container.strip()
    if not container:
        raise HTTPException(
            status_code=409,
            detail="container restart target is not configured",
        )
    try:
        _validate_restart_container_target(container)
    except WebConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    docker = DockerCli(runner=CommandRunner(env=settings.command_env))
    try:
        container_id = docker.container_id(container)
    except CommandError as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not inspect restart container",
                exc,
            ),
        ) from exc
    if not container_id:
        raise HTTPException(
            status_code=500,
            detail="could not inspect restart container",
        )

    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                audit_run_id = _insert_container_restart_audit(
                    conn,
                    settings,
                    request,
                    container=container,
                )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not record container restart audit",
                exc,
            ),
        ) from exc

    background_tasks.add_task(
        _restart_container_task,
        settings,
        container,
        audit_run_id,
    )
    return ContainerRestartResponse(
        status="scheduled",
        audit_run_id=audit_run_id,
        container=container,
    )


def _effective_config(settings: WebSettings) -> UpdaterConfig:
    if _effective_config_loader is None:
        raise RuntimeError("WebUI self-update effective config callback is not set")
    return _effective_config_loader(settings)


def _plan_response(
    plan: DryRunPlan,
    settings: WebSettings,
    request: Request,
) -> PlanResponse:
    if _plan_response_builder is None:
        raise RuntimeError("WebUI self-update plan response callback is not set")
    return _plan_response_builder(plan, settings, request)


def _build_self_update_tag_plan(
    settings: WebSettings,
    status: SelfUpdateResponse,
) -> tuple[DryRunPlan, WebSelfUpdatePlan]:
    target_spec = release_self_update_target(
        status.current_image,
        status.current_tag,
        status.latest_tag,
    )
    if not _self_update_requires_tag_rewrite(target_spec):
        raise PlanInputError("self-update target does not require a tag update")
    wud_file = _write_self_update_tag_plan_file(settings, target_spec)
    try:
        plan = _build_self_update_plan_from_file(settings, wud_file)
    except Exception:
        _delete_self_update_plan_file_path(wud_file)
        raise
    cached = WebSelfUpdatePlan(
        plan_id=plan.plan_id,
        created_at=time.monotonic(),
        wud_file=wud_file,
        current_tag=status.current_tag,
        latest_tag=status.latest_tag,
        current_image=status.current_image,
        target_spec=target_spec,
        target_image=status.target_image,
        restart_container=status.restart_container,
    )
    return plan, cached


def _rebuild_self_update_cached_plan(
    settings: WebSettings,
    cached: WebSelfUpdatePlan,
) -> DryRunPlan:
    if not cached.wud_file.is_file():
        raise PlanFileMissing(str(cached.wud_file))
    return _build_self_update_plan_from_file(settings, cached.wud_file)


def _build_self_update_plan_from_file(
    settings: WebSettings,
    wud_file: Path,
) -> DryRunPlan:
    config = cast(
        UpdaterConfig,
        replace(
            _effective_config(settings),
            wud_out_file=wud_file,
        ),
    )
    return build_dry_run_plan(
        config,
        line_numbers=(1,),
        allow_tag_updates=True,
        tag_overrides=(),
        host_docker_base=settings.host_docker_base,
        environ=settings.command_env,
    )


def _write_self_update_tag_plan_file(settings: WebSettings, target_spec: str) -> Path:
    parent = settings.config.wud_out_file.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".self-update-plan.",
        suffix=".todo",
        dir=str(parent),
    )
    path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(f"{target_spec}\n")
    except Exception:
        _delete_self_update_plan_file_path(path)
        raise
    return path


def _validate_self_update_prepare_plan(plan: PlanResponse) -> None:
    if plan.status != "ready" or not plan.can_apply:
        detail = "self-update tag update plan is not ready"
        for issue in plan.issues:
            if issue.severity == "error":
                detail = issue.message
                break
        else:
            if plan.skipped:
                detail = plan.skipped[0].reason
        raise HTTPException(status_code=409, detail=detail)
    if len(plan.stacks) != 1:
        raise HTTPException(
            status_code=409,
            detail="self-update tag update must match exactly one Compose stack",
        )
    stack = plan.stacks[0]
    if not stack.tag_updates:
        raise HTTPException(
            status_code=409,
            detail="self-update tag update plan has no Compose tag update",
        )
    if not stack.services:
        raise HTTPException(
            status_code=409,
            detail="self-update tag update must match at least one Compose service",
        )


def _verify_self_update_digest_pin_updates(
    settings: WebSettings,
    updates: Sequence[DigestPinUpdate],
) -> None:
    if not updates:
        return

    command_runner = CommandRunner(env=settings.command_env)
    docker = DockerCli(runner=command_runner)
    resolver = DockerManifestResolver(docker, verbose=True)
    verifier = DigestVerifier(
        docker,
        primary_resolver=resolver,
        fallback_resolver=resolver,
    )
    for update in updates:
        current = verifier.resolve_tag_digest(update.resolved_image)
        if not current.ok:
            raise RuntimeError(
                "could not re-resolve digest-pin target "
                f"{update.resolved_image}: {current.reason}"
                + (f" ({current.error})" if current.error else "")
            )
        current_digest = normalize_digest(current.digest)
        if current_digest != update.planned_digest:
            raise RuntimeError(
                "digest-pin target moved for "
                f"{update.resolved_image}: planned {update.planned_digest}, "
                f"current {current_digest}"
            )

        digest_result = verifier.verify(update.resolved_image, update.planned_digest)
        if not digest_result.ok:
            detail = digest_result.reason
            if digest_result.error:
                detail = f"{detail} ({digest_result.error})"
            raise RuntimeError(
                "digest-pin target did not verify for "
                f"{update.resolved_image}: wanted {update.planned_digest}; {detail}"
            )


def _prepare_self_update_tag_update(
    settings: WebSettings,
    plan: PlanResponse,
) -> dict[str, Any]:
    _validate_self_update_prepare_plan(plan)
    stack = plan.stacks[0]
    compose_path = Path(stack.directory) / stack.compose_file
    updates = tuple(
        TagUpdate(
            old_image=item.old_image,
            desired_tag=item.desired_tag,
            new_image=item.new_image,
            services=tuple(item.services),
        )
        for item in stack.tag_updates
    )
    if not updates:
        raise RuntimeError("self-update tag update plan has no Compose tag update")
    digest_pin_updates = tuple(
        digest_pin_update_from_values(
            old_image=item.source_image,
            resolved_tag=item.resolved_tag,
            planned_digest=item.planned_digest,
            services=tuple(item.services),
        )
        for item in stack.digest_pin_updates
    )

    backup = _backup_compose(compose_path)
    restore_error = ""
    restore_succeeded = True
    applied_digest_pins = ()
    try:
        applied = apply_compose_tag_updates(compose_path, updates)
        if not applied:
            raise RuntimeError("no Compose image lines were rewritten")
        compose = ComposeCli(runner=CommandRunner(env=settings.command_env))
        pull_services = tuple(stack.pull_services) or tuple(stack.services) or None
        compose.pull(
            stack.directory,
            stack.compose_file,
            pull_services,
            project_directory=stack.project_directory or None,
        )
        if digest_pin_updates:
            _verify_self_update_digest_pin_updates(settings, digest_pin_updates)
            applied_digest_pins = apply_compose_digest_pins(
                compose_path,
                digest_pin_updates,
            )
            if not applied_digest_pins:
                raise RuntimeError("no Compose image lines were digest-pinned")
    except Exception as exc:
        restore_succeeded = False
        try:
            shutil.copy2(backup, compose_path)
            restore_succeeded = True
        except Exception as restore_exc:  # noqa: BLE001 - preserve the original apply error.
            restore_error = f"; compose rollback failed: {restore_exc}"
        raise RuntimeError(f"{exc}{restore_error}") from exc
    finally:
        if restore_succeeded:
            _delete_self_update_plan_file_path(backup)

    return {
        "strategy": "prepare_tag_update",
        "external_recreate_required": True,
        "stack": stack.name,
        "compose_file": stack.compose_file,
        "services": list(stack.services),
        "pull_services": list(stack.pull_services),
        "tag_updates": [
            {
                "old_image": item.old_image,
                "desired_tag": item.desired_tag,
                "new_image": item.new_image,
                "services": list(item.services),
                "replacements": item.replacements,
            }
            for item in applied
        ],
        "digest_pin_updates": [
            {
                "source_image": item.old_image,
                "resolved_tag": item.resolved_tag,
                "planned_digest": item.planned_digest,
                "final_image": item.final_image,
                "watch_tag": item.watch_tag,
                "marker": item.marker,
                "label_key": item.label_key,
                "label_value": item.label_value,
                "services": list(item.services),
                "replacements": item.replacements,
            }
            for item in applied_digest_pins
        ],
    }


def _cache_self_update_plan(state: Any, cached: WebSelfUpdatePlan) -> None:
    apply_lock: Lock = state.web_apply_lock
    with apply_lock:
        _prune_self_update_plan_cache_unlocked(state)
        plans: dict[str, WebSelfUpdatePlan] = state.web_self_update_plans
        plans[cached.plan_id] = cached


def _require_self_update_cached_plan(
    state: Any,
    plan_id: str,
) -> WebSelfUpdatePlan:
    apply_lock: Lock = state.web_apply_lock
    with apply_lock:
        _prune_self_update_plan_cache_unlocked(state)
        plans: dict[str, WebSelfUpdatePlan] = state.web_self_update_plans
        cached = plans.get(plan_id)
    if cached is None:
        raise HTTPException(status_code=409, detail="self-update plan is stale")
    return cached


def _remove_self_update_cached_plan(state: Any, plan_id: str) -> None:
    apply_lock: Lock = state.web_apply_lock
    with apply_lock:
        plans: dict[str, WebSelfUpdatePlan] = state.web_self_update_plans
        cached = plans.pop(plan_id, None)
    if cached is not None:
        _delete_self_update_plan_file(cached)


def _prune_self_update_plan_cache_unlocked(state: Any) -> None:
    now = time.monotonic()
    plans: dict[str, WebSelfUpdatePlan] = state.web_self_update_plans
    expired = [
        plan_id
        for plan_id, cached in plans.items()
        if now - cached.created_at > SELF_UPDATE_PLAN_TTL_SECONDS
    ]
    for plan_id in expired:
        cached = plans.pop(plan_id)
        _delete_self_update_plan_file(cached)


def _delete_self_update_plan_file(cached: WebSelfUpdatePlan) -> None:
    _delete_self_update_plan_file_path(cached.wud_file)


def _delete_self_update_plan_file_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.warning("failed to remove self-update temporary plan file: %s", path)


def _self_update_cached_plan_stale(
    cached: WebSelfUpdatePlan,
    status: SelfUpdateResponse,
) -> bool:
    return (
        cached.current_tag != status.current_tag
        or cached.latest_tag != status.latest_tag
        or cached.current_image != status.current_image
        or cached.target_image != status.target_image
        or cached.restart_container != status.restart_container
    )


def _self_update_response(settings: WebSettings) -> SelfUpdateResponse:
    env = settings.command_env or {}
    local_tag = current_tag()
    container = settings.restart_container.strip()
    current_image = current_container_image(env)
    warnings: list[str] = []

    if _parse_bool(env.get("WUD_WEB_DEMO_SELF_UPDATE"), default=False):
        return _demo_self_update_response(
            settings,
            current_image=current_image,
            restart_container=container,
        )

    if not release_check_enabled(env):
        return SelfUpdateResponse(
            status="disabled",
            strategy="pull_image",
            current_tag=local_tag,
            latest_tag="",
            current_image=current_image,
            target_image="",
            restart_container=container,
            disabled_reason="release checks are disabled",
        )

    latest_tag = fetch_latest_release_tag()
    if latest_tag is None:
        return SelfUpdateResponse(
            status="unavailable",
            strategy="pull_image",
            current_tag=local_tag,
            latest_tag="",
            current_image=current_image,
            target_image="",
            restart_container=container,
            disabled_reason="latest release could not be checked",
            warnings=["latest WUDup release could not be checked"],
        )

    if not release_update_available(local_tag, latest_tag):
        return SelfUpdateResponse(
            status="up_to_date",
            strategy="pull_image",
            current_tag=local_tag,
            latest_tag=latest_tag,
            current_image=current_image,
            target_image="",
            restart_container=container,
        )

    if not self_update_image_variant_known(current_image):
        return SelfUpdateResponse(
            status="unavailable",
            strategy="pull_image",
            current_tag=local_tag,
            latest_tag=latest_tag,
            current_image=current_image,
            target_image="",
            restart_container=container,
            disabled_reason=(
                "current container image could not be inspected; self-update "
                "cannot preserve the image variant"
            ),
            warnings=["current WUDup container image identity is unavailable"],
        )

    target_spec = release_self_update_target(current_image, local_tag, latest_tag)
    target_image = _self_update_pull_image(target_spec)
    strategy: SelfUpdateStrategy = (
        "prepare_tag_update"
        if _self_update_requires_tag_rewrite(target_spec)
        else "pull_image"
    )
    release_notes, truncated, note_warnings = _fetch_self_update_release_notes(
        local_tag,
        latest_tag,
        env,
        cap=SELF_UPDATE_RELEASE_NOTES_CAP,
    )
    warnings.extend(note_warnings)
    disabled_reason = _self_update_disabled_reason(
        settings,
        target_spec=target_spec,
        target_image=target_image,
        restart_container=container,
    )
    return SelfUpdateResponse(
        status="available",
        strategy=strategy,
        current_tag=local_tag,
        latest_tag=latest_tag,
        current_image=current_image,
        target_image=target_image,
        restart_container=container,
        release_notes=release_notes,
        release_notes_truncated=truncated,
        release_notes_cap=SELF_UPDATE_RELEASE_NOTES_CAP,
        can_update=disabled_reason == "",
        disabled_reason=disabled_reason,
        external_recreate_required=True,
        warnings=warnings,
    )


def _demo_self_update_response(
    settings: WebSettings,
    *,
    current_image: str,
    restart_container: str,
) -> SelfUpdateResponse:
    demo_current_tag = "v0.25.0"
    latest_tag = "v0.26.0"
    current_image = current_image or DEFAULT_SELF_UPDATE_IMAGE
    target_image = DEFAULT_SELF_UPDATE_IMAGE
    disabled_reason = _self_update_disabled_reason(
        settings,
        target_spec=DEFAULT_SELF_UPDATE_IMAGE,
        target_image=target_image,
        restart_container=restart_container,
    )
    notes = [
        SelfUpdateReleaseNote(
            tag=f"v0.{minor}.0",
            title=f"v0.{minor}.0 demo release",
            published_at=f"2026-05-{day:02d}T12:00:00Z",
            url=f"https://github.com/magrhino/wudup/releases/tag/v0.{minor}.0",
            body=(
                "Adds the WebUI self-update banner, release-note review, "
                "and image pull flow."
                if minor == 26
                else "Demo release note for the capped self-update history list."
            ),
            breaking=minor == 26,
            breaking_reasons=(
                ["Review external container recreate steps."] if minor == 26 else []
            ),
        )
        for minor, day in zip(range(26, 16, -1), range(28, 18, -1), strict=True)
    ]
    return SelfUpdateResponse(
        status="available",
        strategy="pull_image",
        current_tag=demo_current_tag,
        latest_tag=latest_tag,
        current_image=current_image,
        target_image=target_image,
        restart_container=restart_container,
        release_notes=notes,
        release_notes_truncated=True,
        release_notes_cap=SELF_UPDATE_RELEASE_NOTES_CAP,
        can_update=disabled_reason == "",
        disabled_reason=disabled_reason,
        external_recreate_required=True,
    )


def _self_update_disabled_reason(
    settings: WebSettings,
    *,
    target_spec: str = "",
    target_image: str,
    restart_container: str,
) -> str:
    _ = target_spec
    if not settings.mutations_enabled:
        return (
            "Read-only mode is active. Set WUD_WEB_MUTATIONS_ENABLED=true on "
            "the server to update the WebUI container."
        )
    if not restart_container:
        return "container restart target is not configured"
    if not target_image:
        return "self-update image target could not be determined"
    try:
        _validate_restart_container_target(restart_container)
    except WebConfigError as exc:
        return str(exc)
    return ""


def _self_update_confirmation_stale(
    payload: SelfUpdateRequest | SelfUpdatePrepareRequest,
    status: SelfUpdateResponse,
) -> bool:
    return (
        payload.current_tag != status.current_tag
        or payload.latest_tag != status.latest_tag
        or payload.target_image != status.target_image
        or payload.restart_container != status.restart_container
    )


def _self_update_pull_image(target: str) -> str:
    parts = target.strip().split()
    if not parts:
        return ""
    image = parts[0]
    desired_tag = ""
    for token in parts[1:]:
        if token.startswith("tag="):
            desired_tag = token.removeprefix("tag=")
    if desired_tag and image_has_tag(image) and tag_value_valid(desired_tag):
        return image_with_tag(image, desired_tag)
    return image


def _self_update_requires_tag_rewrite(target: str) -> bool:
    parts = target.strip().split()
    return any(token.startswith("tag=") for token in parts[1:])


def _fetch_self_update_release_notes(
    current: str,
    latest: str,
    environ: Mapping[str, str],
    *,
    cap: int,
) -> tuple[list[SelfUpdateReleaseNote], bool, list[str]]:
    request = urllib.request.Request(
        SELF_UPDATE_RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"wudup-webui/{__version__}",
            **(
                {"Authorization": f"Bearer {environ['GITHUB_TOKEN']}"}
                if environ.get("GITHUB_TOKEN")
                else {}
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=6.0) as response:
            payload = response.read(262_144)
        data = json.loads(payload.decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return [], False, [f"self-update release notes unavailable: {exc}"]
    if not isinstance(data, list):
        return [], False, ["self-update release notes unavailable: invalid response"]

    matched: list[SelfUpdateReleaseNote] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str):
            continue
        normalized_tag = _normalize_self_update_tag(tag)
        if not _self_update_tag_between(normalized_tag, current, latest):
            continue
        body = str(item.get("body") or "")
        body_truncated = len(body) > 6000
        if body_truncated:
            body = body[:6000].rstrip()
        breaking, reasons = detect_breaking(body, current, normalized_tag)
        matched.append(
            SelfUpdateReleaseNote(
                tag=normalized_tag,
                title=str(item.get("name") or normalized_tag),
                published_at=str(item.get("published_at") or ""),
                url=str(item.get("html_url") or ""),
                body=body,
                body_truncated=body_truncated,
                breaking=breaking,
                breaking_reasons=reasons,
            )
        )

    matched.sort(
        key=lambda note: _self_update_semver_key(note.tag) or (0, 0, 0),
        reverse=True,
    )
    return matched[:cap], len(matched) > cap, []


def _self_update_tag_between(tag: str, current: str, latest: str) -> bool:
    tag_key = _self_update_semver_key(tag)
    current_key = _self_update_semver_key(current)
    latest_key = _self_update_semver_key(latest)
    if tag_key is None or current_key is None or latest_key is None:
        return False
    return current_key < tag_key <= latest_key


def _self_update_semver_key(tag: str) -> tuple[int, int, int] | None:
    match = re.match(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+].*)?$", tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _normalize_self_update_tag(tag: str) -> str:
    normalized = tag.strip()
    if not normalized:
        return ""
    return normalized if normalized.startswith("v") else f"v{normalized}"


def _restart_container_task(
    settings: WebSettings,
    container: str,
    audit_run_id: int,
) -> None:
    try:
        DockerCli(runner=CommandRunner(env=settings.command_env)).restart_container(
            container,
            timeout_seconds=10,
        )
    except CommandError as exc:
        detail = exc.result.stderr.strip() or str(exc)
        LOGGER.error(
            "WebUI container restart failed for %s: %s",
            container,
            _redact_sensitive_text(settings, detail),
        )
        _safe_update_container_restart_audit(
            settings,
            audit_run_id,
            status="failure",
            error=_redact_sensitive_text(settings, detail),
        )
        return

    _safe_update_container_restart_audit(
        settings,
        audit_run_id,
        status="success",
    )


def _insert_container_restart_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    *,
    container: str,
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": "webui",
        "operation": "restart_container",
        "actor_type": _request_actor_type(settings, request),
        "resource_type": "container",
        "resource_id": container,
        "target": {"container": container},
        "status": "scheduled",
    }
    cursor = conn.execute(
        """
        INSERT INTO update_runs (
            started_at,
            finished_at,
            status,
            dry_run,
            mode,
            wud_file,
            log_file,
            metadata_json
        )
        VALUES (?, NULL, 'scheduled', 0, 'web-container-restart', ?, '', ?)
        """,
        (
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO update_events (
            run_id,
            created_at,
            service_name,
            stack_name,
            image,
            target_image,
            status,
            metadata_json
        )
        VALUES (?, ?, 'wudup', '', '', ?, 'scheduled', ?)
        """,
        (
            run_id,
            now,
            container,
            _json_object(metadata),
        ),
    )
    return run_id


def _insert_self_update_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    request: Request,
    *,
    status: SelfUpdateResponse,
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": "webui",
        "operation": "self_update",
        "actor_type": _request_actor_type(settings, request),
        "resource_type": "container",
        "resource_id": status.restart_container,
        "current_tag": status.current_tag,
        "latest_tag": status.latest_tag,
        "current_image": status.current_image,
        "target_image": status.target_image,
        "strategy": status.strategy,
        "external_recreate_required": status.external_recreate_required,
        "target": {
            "container": status.restart_container,
            "image": status.target_image,
        },
        "status": "scheduled",
    }
    cursor = conn.execute(
        """
        INSERT INTO update_runs (
            started_at,
            finished_at,
            status,
            dry_run,
            mode,
            wud_file,
            log_file,
            metadata_json
        )
        VALUES (?, NULL, 'scheduled', 0, 'web-self-update', ?, '', ?)
        """,
        (
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
    )
    run_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO update_events (
            run_id,
            created_at,
            service_name,
            stack_name,
            image,
            target_image,
            status,
            metadata_json
        )
        VALUES (?, ?, 'wudup', 'webui', ?, ?, 'scheduled', ?)
        """,
        (
            run_id,
            now,
            status.current_image,
            status.target_image,
            _json_object(metadata),
        ),
    )
    return run_id


def _safe_update_container_restart_audit(
    settings: WebSettings,
    run_id: int,
    *,
    status: Literal["success", "failure"],
    error: str = "",
) -> None:
    try:
        _update_container_restart_audit(
            settings,
            run_id,
            status=status,
            error=error,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to update WebUI container restart audit")


def _safe_update_self_update_audit(
    settings: WebSettings,
    run_id: int,
    *,
    status: SelfUpdateAuditStatus,
    error: str = "",
    metadata_extra: Mapping[str, Any] | None = None,
) -> None:
    try:
        _update_self_update_audit(
            settings,
            run_id,
            status=status,
            error=error,
            metadata_extra=metadata_extra,
        )
    except (OSError, sqlite3.Error, DatabaseError):
        LOGGER.exception("failed to update WebUI self-update audit")


def _update_self_update_audit(
    settings: WebSettings,
    run_id: int,
    *,
    status: SelfUpdateAuditStatus,
    error: str = "",
    metadata_extra: Mapping[str, Any] | None = None,
) -> None:
    now = utc_timestamp()
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            metadata = _self_update_audit_metadata(conn, run_id)
            metadata["status"] = status
            if metadata_extra:
                metadata.update(metadata_extra)
            if error:
                metadata["error"] = error
            else:
                metadata.pop("error", None)
            metadata_json = _json_object(metadata)
            conn.execute(
                """
                UPDATE update_runs
                SET finished_at = ?,
                    status = ?,
                    metadata_json = ?
                WHERE id = ?
                  AND mode = 'web-self-update'
                """,
                (now, status, metadata_json, run_id),
            )
            conn.execute(
                """
                UPDATE update_events
                SET status = ?,
                    metadata_json = ?
                WHERE run_id = ?
                """,
                (status, metadata_json, run_id),
            )


def _self_update_audit_metadata(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM update_runs
        WHERE id = ?
          AND mode = 'web-self-update'
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {}
    return json_object_or_empty(row["metadata_json"])


def _update_container_restart_audit(
    settings: WebSettings,
    run_id: int,
    *,
    status: Literal["success", "failure"],
    error: str = "",
) -> None:
    now = utc_timestamp()
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            metadata = _container_restart_audit_metadata(conn, run_id)
            metadata["status"] = status
            if error:
                metadata["error"] = error
            else:
                metadata.pop("error", None)
            metadata_json = _json_object(metadata)
            conn.execute(
                """
                UPDATE update_runs
                SET finished_at = ?,
                    status = ?,
                    metadata_json = ?
                WHERE id = ?
                  AND mode = 'web-container-restart'
                """,
                (now, status, metadata_json, run_id),
            )
            conn.execute(
                """
                UPDATE update_events
                SET status = ?,
                    metadata_json = ?
                WHERE run_id = ?
                """,
                (status, metadata_json, run_id),
            )


def _container_restart_audit_metadata(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM update_runs
        WHERE id = ?
          AND mode = 'web-container-restart'
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {}
    return json_object_or_empty(row["metadata_json"])


def _validate_restart_container_target(value: str) -> str:
    if not value:
        return ""
    if not CONTAINER_REF_RE.fullmatch(value):
        raise WebConfigError(
            "WUD_WEB_RESTART_CONTAINER must be a Docker container name or ID"
        )
    return value
