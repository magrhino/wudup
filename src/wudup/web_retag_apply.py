"""Retag apply jobs, runtime revalidation, and Compose execution/recovery."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Condition

from fastapi import HTTPException
from starlette.datastructures import State

from . import web_job_registry, web_retag_audit
from .command import CommandError, CommandRunner
from .compose import ComposeCli, ComposeStack
from .compose_rewrite import (
    _backup_compose,
    _compose_source_hash,
    apply_compose_digest_pins,
    apply_compose_retag_updates,
    restore_compose_backup,
)
from .config import UpdaterConfig
from .db import utc_timestamp
from .docker_cli import DockerCli
from .updater_lifecycle_health import (
    CONTAINER_SUMMARY_FORMAT,
    HEALTH_LOG_FORMAT,
    health_gate_passed,
    running_service_containers,
)
from .updater_models import UpdaterProgressEvent
from .web_models import (
    ApplyJobResponse,
    RetagApplyRequest,
    RetagPlanRequest,
    WebApplyJob,
    WebSettings,
)
from .web_redaction import safe_exception_detail as _safe_exception_detail
from .web_retag_plans import (
    RetagPlanBuild as _RetagPlanBuild,
)
from .web_retag_plans import (
    RetagPlanUpdate as _RetagPlanUpdate,
)
from .web_retag_plans import (
    ordered_retag_stacks as _ordered_retag_stacks,
)
from .web_retag_plans import retag_config_transforms
from .web_retag_plans import (
    retag_update_service as _retag_update_service,
)
from .web_retag_runtime import (
    _retag_compose_service_key,
    _running_retag_compose_service_keys,
)

LOGGER = logging.getLogger(__name__)


class _RetagApplyFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        successful_updates: Sequence[_RetagPlanUpdate],
        retained_known_image_updates: Sequence[_RetagPlanUpdate] = (),
    ) -> None:
        super().__init__(message)
        self.successful_updates = tuple(successful_updates)
        self.retained_known_image_updates = tuple(retained_known_image_updates)


def submit_retag_apply_job(
    state: State,
    settings: WebSettings,
    payload: RetagApplyRequest,
    *,
    build_plan: Callable[[WebSettings, RetagPlanRequest], _RetagPlanBuild],
    effective_config: Callable[[WebSettings], UpdaterConfig],
) -> ApplyJobResponse:
    apply_condition: Condition = state.web_apply_condition
    jobs: dict[str, WebApplyJob] = state.web_apply_jobs
    executor = state.web_apply_executor
    with apply_condition:
        active_error = web_job_registry._active_mutation_error_unlocked(state)
        if active_error:
            raise HTTPException(status_code=409, detail=active_error)
        job = WebApplyJob(
            id=secrets.token_urlsafe(18),
            status="queued",
            selected_line_numbers=(),
        )
        web_job_registry._register_apply_job_unlocked(jobs, job)
        response = web_job_registry._apply_job_response(job)
        apply_condition.notify_all()
        try:
            executor.submit(
                _run_retag_apply_job,
                settings,
                payload,
                jobs,
                apply_condition,
                job.id,
                build_plan=build_plan,
                effective_config=effective_config,
            )
        except Exception:
            del jobs[job.id]
            apply_condition.notify_all()
            raise
        return response


def _run_retag_apply_job(
    settings: WebSettings,
    payload: RetagApplyRequest,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    *,
    build_plan: Callable[[WebSettings, RetagPlanRequest], _RetagPlanBuild],
    effective_config: Callable[[WebSettings], UpdaterConfig],
) -> None:
    web_job_registry._update_apply_job(
        jobs,
        apply_condition,
        job_id,
        status="running",
        started_at=utc_timestamp(),
    )
    run_id: int | None = None
    build: _RetagPlanBuild | None = None
    preflight = True
    successful_updates: tuple[_RetagPlanUpdate, ...] = ()
    retained_known_image_updates: tuple[_RetagPlanUpdate, ...] = ()
    web_job_registry._append_apply_job_progress(
        jobs,
        apply_condition,
        job_id,
        UpdaterProgressEvent(
            phase="preflight",
            status="running",
            message="Revalidating the selected retag plan.",
        ),
    )
    try:
        build = build_plan(
            settings,
            RetagPlanRequest(
                choices=payload.choices,
                github_latest_fallback=payload.github_latest_fallback,
            ),
        )
        plan = build.response
        if not secrets.compare_digest(plan.plan_id, payload.plan_id):
            raise RuntimeError("retag plan is stale")
        if not plan.can_apply or not build.updates:
            raise RuntimeError("retag plan is not ready to apply")
        web_job_registry._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="preflight",
                status="success",
                message="Selected retag plan revalidated.",
            ),
        )
        preflight = False
        run_id = web_retag_audit._insert_retag_audit_run(settings, build, status="running")
        successful_updates = _apply_retag_updates(
            settings,
            build,
            jobs,
            apply_condition,
            job_id,
            effective_config=effective_config,
        )
        web_retag_audit._finish_retag_audit_run(settings, run_id, build, status="success")
        web_job_registry._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="completion",
                status="success",
                message="Retag changes applied.",
            ),
        )
        web_job_registry._update_apply_job(
            jobs,
            apply_condition,
            job_id,
            status="success",
            run_id=run_id,
            finished_at=utc_timestamp(),
        )
    except Exception as exc:  # noqa: BLE001 - the apply job records all failures.
        if isinstance(exc, _RetagApplyFailed):
            successful_updates = exc.successful_updates
            retained_known_image_updates = exc.retained_known_image_updates
        safe_error = _safe_retag_apply_error(settings, exc)
        web_job_registry._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="preflight" if preflight else "completion",
                status="failure",
                message=safe_error,
            ),
        )
        web_job_registry._update_apply_job(
            jobs,
            apply_condition,
            job_id,
            status="failure",
            run_id=run_id,
            finished_at=utc_timestamp(),
            error=safe_error,
        )
        if run_id is not None and build is not None:
            web_retag_audit._finish_retag_audit_run(
                settings,
                run_id,
                build,
                status="failure",
                error=safe_error,
                successful_updates=successful_updates,
                retained_known_image_updates=retained_known_image_updates,
            )


def _apply_retag_updates(
    settings: WebSettings,
    build: _RetagPlanBuild,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    *,
    effective_config: Callable[[WebSettings], UpdaterConfig],
) -> tuple[_RetagPlanUpdate, ...]:
    env = settings.command_env
    runner = CommandRunner(env=env) if env is not None else CommandRunner()
    compose = ComposeCli(runner=runner)
    docker = DockerCli(runner=runner)
    config = effective_config(settings)
    successful_updates: list[_RetagPlanUpdate] = []
    for stack in _ordered_retag_stacks(build.updates):
        stack_updates = [item for item in build.updates if item.stack.index == stack.index]
        _apply_retag_stack(
            settings,
            compose,
            docker,
            config,
            stack,
            stack_updates,
            jobs,
            apply_condition,
            job_id,
            successful_updates,
            approved_source_hash=build.compose_hashes.get(
                str(stack.directory / stack.file), ""
            ),
        )
    return tuple(successful_updates)


def _apply_retag_stack(
    settings: WebSettings,
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    stack_updates: list[_RetagPlanUpdate],
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    successful_updates: list[_RetagPlanUpdate],
    *,
    approved_source_hash: str,
) -> None:
    """Apply one stack, recording success only after persistence and cleanup."""
    services = tuple(
        sorted(
            {
                service
                for item in stack_updates
                for service in item.update.services
            }
        )
    )
    compose_path = stack.directory / stack.file
    backup: Path | None = None
    backup_hash = ""
    written_hashes: list[str] = []
    known_image_changes: tuple[web_retag_audit.RetagKnownImageChange, ...] = ()
    stopped_services: tuple[str, ...] = ()
    services_recreated = False
    try:
        stopped_services = _revalidate_retag_runtime_before_apply(
            settings, compose, stack_updates
        )
        _progress(
            jobs,
            apply_condition,
            job_id,
            "compose-retag",
            "running",
            f"[{stack.name}] Writing selected retag to Compose.",
            stack=stack.name,
            services=services,
        )
        backup = _backup_compose(compose_path)
        backup_hash = _compose_source_hash(backup)
        if not approved_source_hash:
            raise RuntimeError(
                f"retag plan is stale: WUDup could not read the Compose file for "
                f"{stack.name} when the plan was made, so it was not rewritten. "
                "Check that the file is readable, preview the retag again and "
                "apply the new plan"
            )
        if backup_hash != approved_source_hash:
            raise RuntimeError(
                f"retag plan is stale: the Compose file for {stack.name} changed "
                "after the plan was approved, so it was not rewritten. Preview "
                "the retag again and apply the new plan"
            )
        if stack_updates[0].digest_pin:
            applied = apply_compose_digest_pins(
                compose_path,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
                written_hashes=written_hashes,
                expected_source_hash=approved_source_hash,
            )
        else:
            applied = apply_compose_retag_updates(
                compose_path,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
                written_hashes=written_hashes,
                expected_source_hash=approved_source_hash,
                config_transforms=retag_config_transforms(stack),
            )
        if not applied:
            raise RuntimeError("no Compose image lines were retagged")
        _progress(
            jobs,
            apply_condition,
            job_id,
            "compose-retag",
            "success",
            f"[{stack.name}] Selected retag was written to Compose.",
            stack=stack.name,
            services=services,
        )

        _progress(
            jobs,
            apply_condition,
            job_id,
            "pull",
            "running",
            f"[{stack.name}] Pulling retagged service image(s).",
            stack=stack.name,
            services=services,
        )
        compose.pull(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
        _progress(
            jobs,
            apply_condition,
            job_id,
            "pull",
            "success",
            f"[{stack.name}] Retagged service image(s) pulled.",
            stack=stack.name,
            services=services,
        )

        services_recreated = True
        _recreate_retag_services(
            compose,
            docker,
            config,
            stack,
            services,
            jobs,
            apply_condition,
            job_id,
        )
        known_image_changes = web_retag_audit._record_successful_retag_known_images(
            settings, stack_updates
        )
        if backup is not None:
            _delete_path(backup)
            backup = None
        successful_updates.extend(stack_updates)
    except Exception as exc:
        rollback_summary = ""
        if backup is not None:
            if written_hashes:
                try:
                    rollback_summary = _restore_retag_compose(
                        compose,
                        docker,
                        config,
                        stack,
                        services if services_recreated else (),
                        stopped_services,
                        backup,
                        jobs,
                        apply_condition,
                        job_id,
                        original_error=str(exc),
                        expected_source_hash=written_hashes[-1],
                    )
                except Exception as restore_exc:
                    raise _retag_stack_failure(
                        settings,
                        restore_exc,
                        successful_updates,
                        compose_path,
                        backup_hash,
                        written_hashes,
                        stack_updates,
                        known_image_changes,
                    ) from restore_exc
            else:
                try:
                    _delete_path(backup)
                except Exception as cleanup_exc:
                    raise _RetagApplyFailed(
                        f"{exc}; the unused Compose backup could not be "
                        f"removed and was retained at {backup}: {cleanup_exc}",
                        successful_updates,
                    ) from cleanup_exc
            backup = None
        raise _retag_stack_failure(
            settings,
            exc,
            successful_updates,
            compose_path,
            backup_hash,
            written_hashes,
            stack_updates,
            known_image_changes,
            rollback_summary=rollback_summary,
        ) from exc


def _retag_stack_failure(
    settings: WebSettings,
    failure: Exception,
    successful_updates: Sequence[_RetagPlanUpdate],
    compose_path: Path,
    original_source_hash: str,
    written_hashes: Sequence[str],
    stack_updates: Sequence[_RetagPlanUpdate],
    known_image_changes: Sequence[web_retag_audit.RetagKnownImageChange],
    *,
    rollback_summary: str = "",
) -> _RetagApplyFailed:
    """Describe a failed stack after its known-image records match its Compose file."""
    retained: tuple[_RetagPlanUpdate, ...] = ()
    known_image_error = ""
    if known_image_changes:
        retained, known_image_error = _reconcile_retag_known_images(
            settings,
            compose_path,
            original_source_hash,
            written_hashes[-1] if written_hashes else "",
            stack_updates,
            known_image_changes,
        )
    return _RetagApplyFailed(
        str(failure)
        + (f"; {rollback_summary}" if rollback_summary else "")
        + known_image_error,
        successful_updates,
        retained,
    )


def _reconcile_retag_known_images(
    settings: WebSettings,
    compose_path: Path,
    original_source_hash: str,
    written_source_hash: str,
    stack_updates: Sequence[_RetagPlanUpdate],
    changes: Sequence[web_retag_audit.RetagKnownImageChange],
) -> tuple[tuple[_RetagPlanUpdate, ...], str]:
    """Match known-image records to the Compose file left after a failed stack.

    Returns the updates whose records remain saved, plus any error text to
    append to the failure message.
    """
    recorded_keys = {change.service_key for change in changes}
    recorded = tuple(
        item
        for item in stack_updates
        if item.service_key in recorded_keys
        and not item.known_image_service_key_ambiguous
    )
    try:
        current_source_hash = _compose_source_hash(compose_path)
    except Exception as exc:  # noqa: BLE001 - reported with the stack failure.
        return recorded, (
            "; the new image records were kept because the Compose file "
            f"could not be read to confirm the rollback: {exc}"
        )
    if current_source_hash == written_source_hash:
        # Compose still declares the retagged images, so the records stay accurate.
        return recorded, ""
    if current_source_hash != original_source_hash:
        return recorded, (
            "; the new image records were kept because the Compose file was "
            "changed by something else during the rollback. Check the Compose "
            "file and preview the retag again before retrying"
        )
    try:
        web_retag_audit._restore_retag_known_images(settings, changes)
    except Exception as exc:  # noqa: BLE001 - reported with the stack failure.
        return recorded, (
            "; the Compose file was restored but the saved image records could "
            f"not be reset to their previous values: {exc}"
        )
    return (), ""


def _revalidate_retag_runtime_before_apply(
    settings: WebSettings,
    compose: ComposeCli,
    updates: Sequence[_RetagPlanUpdate],
) -> tuple[str, ...]:
    """Check runtime consent and return the approved services that are stopped."""
    if not updates:
        return ()
    stack = updates[0].stack
    project_name = compose.try_config_project_name(
        stack.directory,
        stack.file,
        project_directory=stack.project_directory,
    )
    if not project_name:
        raise RuntimeError("retag Compose project could not be revalidated")
    if project_name != stack.project_name:
        raise RuntimeError("retag Compose project changed before apply")
    running_service_keys = _running_retag_compose_service_keys(settings)
    if running_service_keys is None:
        raise RuntimeError("retag runtime state could not be revalidated")
    stopped_services: set[str] = set()
    for item in updates:
        service = _retag_update_service(item)
        if (
            _retag_compose_service_key(
                item.stack,
                service,
                project_name=project_name,
            )
            in running_service_keys
        ):
            continue
        if not item.allow_start:
            raise RuntimeError(
                f"{item.service_key} is no longer running in the expected Compose project"
            )
        stopped_services.add(service)
    return tuple(sorted(stopped_services))


def _recreate_retag_services(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    services: Sequence[str],
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> None:
    _progress(
        jobs,
        apply_condition,
        job_id,
        "recreate",
        "running",
        f"[{stack.name}] Recreating retagged service container(s).",
        stack=stack.name,
        services=services,
    )
    pre_up_error: CommandError | None = None
    if config.update_mode == "pause":
        try:
            compose.pause(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError:
            pass
    elif config.update_mode == "stop":
        try:
            compose.stop(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            pre_up_error = exc

    if config.update_mode == "pause":
        try:
            wait_handled = _compose_up_retag_services(compose, stack, services, config)
        except Exception:
            compose.unpause(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
            raise
        compose.unpause(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
    else:
        wait_handled = _compose_up_retag_services(compose, stack, services, config)
    if pre_up_error is not None:
        raise pre_up_error

    _progress(
        jobs,
        apply_condition,
        job_id,
        "recreate",
        "success",
        f"[{stack.name}] Retagged service container(s) recreated.",
        stack=stack.name,
        services=services,
    )
    if wait_handled:
        _progress(
            jobs,
            apply_condition,
            job_id,
            "health",
            "success",
            f"[{stack.name}] Compose reported healthy retagged service container(s).",
            stack=stack.name,
            services=services,
        )
        return
    _wait_for_retag_health(
        compose,
        docker,
        config,
        stack,
        services,
        jobs,
        apply_condition,
        job_id,
    )


def _compose_up_retag_services(
    compose: ComposeCli,
    stack: ComposeStack,
    services: Sequence[str],
    config: UpdaterConfig,
) -> bool:
    wait = (
        config.update_mode != "pause"
        and compose.up_wait_supported(
            stack.directory,
            stack.file,
            project_directory=stack.project_directory,
        )
    )
    compose.up(
        stack.directory,
        stack.file,
        services,
        wait=wait,
        wait_timeout=config.max_wait if wait else None,
        force_recreate=True,
        no_deps=True,
        project_directory=stack.project_directory,
    )
    return wait


def _wait_for_retag_health(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    services: Sequence[str],
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
) -> None:
    _progress(
        jobs,
        apply_condition,
        job_id,
        "health",
        "running",
        f"[{stack.name}] Waiting up to {config.max_wait}s for retagged service health.",
        stack=stack.name,
        services=services,
    )
    start = time.monotonic()
    if config.max_wait > 0:
        time.sleep(2)
    while True:
        cids, missing, failed = running_service_containers(compose, stack, services)
        ok = health_gate_passed(
            cids,
            missing,
            failed,
            lambda cid: _first_nonblank(
                docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT)
            ),
        )
        elapsed = int(time.monotonic() - start)
        if ok:
            _progress(
                jobs,
                apply_condition,
                job_id,
                "health",
                "success",
                f"[{stack.name}] Retagged service health wait succeeded in {elapsed}s.",
                stack=stack.name,
                services=services,
            )
            return
        if elapsed >= config.max_wait:
            detail = _retag_health_details(compose, docker, stack, services)
            raise RuntimeError(
                f"[{stack.name}] retagged service health wait failed after {elapsed}s"
                + (f": {detail}" if detail else "")
            )
        time.sleep(2)


def _restore_retag_compose(
    compose: ComposeCli,
    docker: DockerCli,
    config: UpdaterConfig,
    stack: ComposeStack,
    recreated_services: Sequence[str],
    stopped_services: Sequence[str],
    backup: Path,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    *,
    original_error: str,
    expected_source_hash: str,
) -> str:
    """Restore Compose and return services to their state before the apply.

    Only services this apply already recreated are recreated again, and
    services that were stopped before the apply are recreated without starting.
    Returns a plain-language summary of what rollback did to the services.
    """
    try:
        restored_on_disk = restore_compose_backup(
            backup, stack.directory / stack.file,
            expected_source_hash=expected_source_hash,
        )
    except Exception as rollback_exc:
        raise RuntimeError(
            f"{original_error}; compose rollback failed: {rollback_exc}; "
            f"backup retained at {backup}"
        ) from rollback_exc
    stopped = tuple(
        service for service in recreated_services if service in stopped_services
    )
    running = tuple(
        service for service in recreated_services if service not in stopped_services
    )
    # Attempt every service group even when an earlier one fails, so one
    # failure never leaves the other services on the new image.
    failures: list[str] = []
    first_exc: Exception | None = None
    if stopped:
        try:
            _recreate_retag_services_stopped(compose, stack, stopped)
        except Exception as exc:  # noqa: BLE001 - reported with the other groups.
            first_exc = exc
            failures.append(
                f"{', '.join(stopped)} (stopped before the apply) could not be "
                f"rolled back: {exc}"
            )
    if running:
        try:
            wait_handled = _compose_up_retag_services(compose, stack, running, config)
            if not wait_handled:
                _wait_for_retag_health(
                    compose,
                    docker,
                    config,
                    stack,
                    running,
                    jobs,
                    apply_condition,
                    job_id,
                )
        except Exception as exc:  # noqa: BLE001 - reported with the other groups.
            first_exc = first_exc or exc
            failures.append(
                f"{', '.join(running)} could not be rolled back to the previous "
                f"image: {exc}"
            )
    if not failures and not restored_on_disk:
        LOGGER.warning(
            "[%s] Kept the previous Compose file at %s because the restored "
            "%s may not survive a crash; delete the backup once the storage "
            "is healthy.",
            stack.name, backup, stack.file,
        )
    elif not failures:
        try:
            _delete_path(backup)
        except Exception as exc:  # noqa: BLE001 - reported as a rollback failure.
            first_exc = exc
            failures.append(str(exc))
    if failures:
        raise RuntimeError(
            f"{original_error}; compose rollback failed after the Compose file "
            f"was restored: {'; '.join(failures)}; backup retained at {backup}"
        ) from first_exc
    return _retag_rollback_summary(stack, running, stopped)


def _retag_rollback_summary(
    stack: ComposeStack,
    running: Sequence[str],
    stopped: Sequence[str],
) -> str:
    """Describe in plain language what a successful rollback did to the services."""
    summary = [f"rollback restored the Compose file for {stack.name}"]
    if running:
        summary.append(
            f"recreated and started {', '.join(running)} on the previous image"
        )
    if stopped:
        summary.append(
            f"recreated {', '.join(stopped)} on the previous image without "
            "starting it, because it was stopped before the apply"
        )
    if not running and not stopped:
        summary.append("no services were recreated or started")
    return "; ".join(summary)


def _recreate_retag_services_stopped(
    compose: ComposeCli,
    stack: ComposeStack,
    services: Sequence[str],
) -> None:
    """Recreate services from the restored Compose file and confirm they stay stopped."""
    names = ", ".join(services)
    try:
        compose.up(
            stack.directory,
            stack.file,
            services,
            force_recreate=True,
            no_deps=True,
            no_start=True,
            project_directory=stack.project_directory,
        )
    except CommandError as exc:
        # The apply may already have started it on the new image.
        raise RuntimeError(
            f"WUDup could not recreate {names} on the previous image without "
            f"starting it, so {_stop_retag_services_after_rollback(compose, stack, services)}; "
            f"check that it is stopped ({exc})"
        ) from exc
    try:
        running = compose.ps_quiet_checked(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
    except CommandError as exc:
        raise RuntimeError(
            f"WUDup could not check that {names} stayed stopped after rollback, "
            f"so {_stop_retag_services_after_rollback(compose, stack, services)}; "
            f"check that it is stopped ({exc})"
        ) from exc
    if not running:
        return
    raise RuntimeError(
        f"{names} started during rollback although it was stopped before the "
        f"apply, so {_stop_retag_services_after_rollback(compose, stack, services)}; "
        "check that it is stopped"
    )


def _stop_retag_services_after_rollback(
    compose: ComposeCli,
    stack: ComposeStack,
    services: Sequence[str],
) -> str:
    """Stop services that must stay stopped and describe what happened."""
    try:
        compose.stop(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
    except CommandError as exc:
        return f"WUDup tried to stop it again but that failed: {exc}"
    return "WUDup stopped it again"


def _safe_retag_apply_error(settings: WebSettings, exc: BaseException) -> str:
    return _safe_exception_detail(settings, "retag apply failed", exc)


def _progress(
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    phase: str,
    status: str,
    message: str,
    *,
    stack: str = "",
    services: Sequence[str] = (),
) -> None:
    web_job_registry._append_apply_job_progress(
        jobs,
        apply_condition,
        job_id,
        UpdaterProgressEvent(
            phase=phase,
            status=status,
            message=message,
            stack=stack,
            services=tuple(services),
        ),
    )


def _first_nonblank(values: Sequence[str]) -> str:
    for value in values:
        if value.strip():
            return value.strip()
    return ""


def _retag_health_details(
    compose: ComposeCli,
    docker: DockerCli,
    stack: ComposeStack,
    services: Sequence[str],
) -> str:
    cids, missing, failed = running_service_containers(compose, stack, services)
    details: list[str] = []
    if not cids and not failed and not missing:
        details.append("docker compose ps -q returned no containers")
    if failed:
        details.append(
            f"could not list containers for service(s): {', '.join(failed)} "
            "because docker compose ps failed (check that the Compose file is "
            "valid and Docker is reachable)"
        )
    if missing:
        details.append(
            f"no running container for service(s): {', '.join(missing)} "
            "(the container exited or never started)"
        )
    for cid in cids:
        summary = _first_nonblank(docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT))
        if summary:
            details.append(summary)
        for output in docker.try_inspect(cid, HEALTH_LOG_FORMAT):
            if output.strip():
                details.append(output.strip())
    return "; ".join(details)


def _delete_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
