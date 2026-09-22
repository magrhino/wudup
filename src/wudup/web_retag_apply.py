"""Retag apply jobs, runtime revalidation, and Compose execution/recovery."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Condition

from fastapi import HTTPException
from starlette.datastructures import State

from . import web_jobs, web_retag_audit
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
    _cid_is_ok,
)
from .updater_models import UpdaterProgressEvent
from .web_auth import _safe_exception_detail
from .web_models import (
    ApplyJobResponse,
    RetagApplyRequest,
    RetagPlanRequest,
    WebApplyJob,
    WebSettings,
)
from .web_retag_plans import (
    RetagPlanBuild as _RetagPlanBuild,
)
from .web_retag_plans import (
    RetagPlanUpdate as _RetagPlanUpdate,
)
from .web_retag_plans import (
    ordered_retag_stacks as _ordered_retag_stacks,
)
from .web_retag_plans import (
    retag_update_service as _retag_update_service,
)
from .web_retag_runtime import (
    _retag_compose_service_key,
    _running_retag_compose_service_keys,
)


class _RetagApplyFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        successful_updates: Sequence[_RetagPlanUpdate],
    ) -> None:
        super().__init__(message)
        self.successful_updates = tuple(successful_updates)


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
        active_error = web_jobs._active_mutation_error_unlocked(state)
        if active_error:
            raise HTTPException(status_code=409, detail=active_error)
        job = WebApplyJob(
            id=secrets.token_urlsafe(18),
            status="queued",
            selected_line_numbers=(),
        )
        jobs[job.id] = job
        response = web_jobs._apply_job_response(job)
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
    web_jobs._update_apply_job(
        jobs,
        apply_condition,
        job_id,
        status="running",
        started_at=utc_timestamp(),
    )
    run_id: int | None = None
    build: _RetagPlanBuild | None = None
    wud_lock: object | None = None
    preflight = True
    successful_updates: tuple[_RetagPlanUpdate, ...] = ()
    web_jobs._append_apply_job_progress(
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
        wud_lock = web_jobs._acquire_apply_wud_lock(settings)
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
        web_jobs._append_apply_job_progress(
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
        web_jobs._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="completion",
                status="success",
                message="Retag changes applied.",
            ),
        )
        web_jobs._update_apply_job(
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
        safe_error = _safe_retag_apply_error(settings, exc)
        web_jobs._append_apply_job_progress(
            jobs,
            apply_condition,
            job_id,
            UpdaterProgressEvent(
                phase="preflight" if preflight else "completion",
                status="failure",
                message=safe_error,
            ),
        )
        web_jobs._update_apply_job(
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
            )
    finally:
        close = getattr(wud_lock, "close", None) if wud_lock is not None else None
        if close is not None:
            close()


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
        services = tuple(
            sorted(
                {
                    service
                    for item in stack_updates
                    for service in item.update.services
                }
            )
        )
        backup: Path | None = None
        written_hashes: list[str] = []
        try:
            _revalidate_retag_runtime_before_apply(settings, compose, stack_updates)
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
            compose_path = stack.directory / stack.file
            backup = _backup_compose(compose_path)
            backup_hash = _compose_source_hash(backup)
            applier = (
                apply_compose_digest_pins
                if stack_updates[0].digest_pin
                else apply_compose_retag_updates
            )
            applied = applier(
                compose_path,
                tuple(item.update for item in stack_updates),
                stack_name=stack.name,
                written_hashes=written_hashes,
                expected_source_hash=backup_hash,
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
            web_retag_audit._record_successful_retag_known_images(settings, stack_updates)
            if backup is not None:
                _delete_path(backup)
                backup = None
            successful_updates.extend(stack_updates)
        except Exception as exc:
            if backup is not None:
                if written_hashes:
                    try:
                        _restore_retag_compose(
                            compose,
                            docker,
                            config,
                            stack,
                            services,
                            backup,
                            jobs,
                            apply_condition,
                            job_id,
                            original_error=str(exc),
                            expected_source_hash=written_hashes[-1],
                        )
                    except Exception as restore_exc:
                        raise _RetagApplyFailed(
                            str(restore_exc),
                            successful_updates,
                        ) from restore_exc
                else:
                    _delete_path(backup)
                backup = None
            raise _RetagApplyFailed(str(exc), successful_updates) from exc
    return tuple(successful_updates)


def _revalidate_retag_runtime_before_apply(
    settings: WebSettings,
    compose: ComposeCli,
    updates: Sequence[_RetagPlanUpdate],
) -> None:
    if not updates:
        return
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
    for item in updates:
        if item.allow_start:
            continue
        service = _retag_update_service(item)
        if (
            _retag_compose_service_key(
                item.stack,
                service,
                project_name=project_name,
            )
            not in running_service_keys
        ):
            raise RuntimeError(
                f"{item.service_key} is no longer running in the expected Compose project"
            )


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
        cids = compose.ps_quiet(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
        ok = bool(cids)
        for cid in cids:
            summary = _first_nonblank(
                docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT)
            )
            if not summary or not _cid_is_ok(summary):
                ok = False
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
    services: Sequence[str],
    backup: Path,
    jobs: dict[str, WebApplyJob],
    apply_condition: Condition,
    job_id: str,
    *,
    original_error: str,
    expected_source_hash: str,
) -> None:
    try:
        restore_compose_backup(
            backup, stack.directory / stack.file,
            expected_source_hash=expected_source_hash,
        )
        wait_handled = _compose_up_retag_services(compose, stack, services, config)
        if not wait_handled:
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
        _delete_path(backup)
    except Exception as rollback_exc:
        raise RuntimeError(
            f"{original_error}; compose rollback failed: {rollback_exc}; "
            f"backup retained at {backup}"
        ) from rollback_exc


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
    web_jobs._append_apply_job_progress(
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
    cids = compose.ps_quiet(
        stack.directory,
        stack.file,
        services,
        project_directory=stack.project_directory,
    )
    details: list[str] = []
    if not cids:
        details.append("docker compose ps -q returned no containers")
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
