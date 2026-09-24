"""Read-only FastAPI WebUI foundation for WUDup."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from . import (
    __version__,
    web_auth,
    web_database,
    web_diagnostics,
    web_health,
    web_jobs,
    web_models,
    web_onboarding,
    web_pending,
    web_pending_rescan,
    web_pending_sources,
    web_plans,
    web_release_notes,
    web_release_notifications,
    web_request_context,
    web_retags,
    web_rollback,
    web_runs,
    web_scheduler,
    web_security,
    web_self_update,
    web_settings,
    web_startup,
    web_state,
    web_static,
    web_tracking,
    web_wud_api,
)
from .config import (
    ConfigError,
    UpdaterConfig,
    load_config,
    parse_bool_env,
)

DEFAULT_WEB_PORT = 7417
LOGGER = logging.getLogger(__name__)


def create_app(
    settings: web_models.WebSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Create the read-only WebUI ASGI app."""

    active_settings = settings or load_web_settings(environ)
    app = FastAPI(
        title="WUDup WebUI",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
    )
    app.state.web_settings = active_settings
    app.state.web_setup_claim = ""
    web_jobs.initialize_apply_job_state(app.state)
    web_security.initialize_security_scan_state(app.state, active_settings)
    web_retags.initialize_retag_preview_state(app.state)
    web_release_notifications.initialize_release_notification_scheduler_state(app.state)
    app.state.web_login_throttle_lock = Lock()
    app.state.web_login_throttle = {}
    app.state.web_login_client_throttle = {}
    web_scheduler.initialize_auto_update_scheduler_state(app.state)
    web_wud_api.initialize_pending_observation_cache(active_settings)
    web_wud_api.startup_probe(active_settings)
    if not active_settings.dev_no_auth:
        app.state.web_setup_claim = web_auth._prepare_web_auth_state(
            active_settings
        )
    app.add_exception_handler(
        RequestValidationError,
        web_auth._validation_exception_handler,
    )

    if active_settings.mutations_enabled:
        app.state.web_auto_update_thread = web_scheduler.start_auto_update_scheduler(
            app,
            active_settings,
            effective_config_loader=web_settings._effective_config,
        )
        app.state.web_release_notification_thread = (
            web_release_notifications.start_release_notification_scheduler(
                app,
                active_settings,
            )
        )

    def shutdown_apply_executor() -> None:
        web_release_notifications.shutdown_release_notification_scheduler_state(app.state)
        web_scheduler.shutdown_auto_update_scheduler_state(app.state)
        web_retags.shutdown_retag_preview_state(app.state)
        web_security.shutdown_security_scan_state(app.state)
        web_jobs.shutdown_apply_job_state(app.state)
        try:
            web_wud_api.checkpoint_pending_observation_cache(active_settings)
        except Exception:  # noqa: BLE001 - persistence must not fail app shutdown.
            LOGGER.error("WUD API pending observation checkpoint failed")

    router_shutdown = getattr(getattr(app, "router", None), "on_shutdown", None)
    if isinstance(router_shutdown, list):
        router_shutdown.append(shutdown_apply_executor)

    @app.middleware("http")
    async def web_request_safety(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        return await web_auth.request_safety_middleware(
            request,
            call_next,
            active_settings,
        )

    web_health.configure(
        effective_config_loader=web_settings._effective_config,
        static_spa_available_checker=web_static.static_spa_available,
    )
    web_pending.configure(effective_config_loader=web_settings._effective_config)
    web_plans.configure(effective_config_loader=web_settings._effective_config)
    web_retags.configure(
        effective_config_loader=web_settings._effective_config,
        retag_digest_pins_loader=web_settings._effective_retag_digest_pins,
    )

    app.add_api_route(
        "/healthz",
        web_health.api_healthz,
        methods=["GET"],
        response_model=web_models.HealthResponse,
    )
    app.add_api_route(
        "/readyz",
        web_health.api_readyz,
        methods=["GET"],
        response_model=web_models.ReadyResponse,
    )

    setup_router = APIRouter(prefix="/api/v1/setup")
    setup_router.add_api_route(
        "/status",
        web_auth.api_setup_status,
        methods=["GET"],
        response_model=web_models.SetupStatusResponse,
    )
    setup_router.add_api_route(
        "/claim",
        web_auth.api_setup_claim,
        methods=["POST"],
        response_model=web_models.AuthSessionResponse,
    )
    app.include_router(setup_router)

    auth_router = APIRouter(prefix="/api/v1/auth")
    auth_router.add_api_route(
        "/csrf",
        web_auth.api_auth_csrf,
        methods=["GET"],
        response_model=web_models.CsrfResponse,
    )
    auth_router.add_api_route(
        "/login",
        web_auth.api_auth_login,
        methods=["POST"],
        response_model=web_models.AuthSessionResponse,
    )
    auth_router.add_api_route(
        "/reset-admin/claim",
        web_auth.api_auth_reset_admin_claim,
        methods=["POST"],
        response_model=web_models.AuthSessionResponse,
    )
    auth_router.add_api_route(
        "/logout",
        web_auth.api_auth_logout,
        methods=["POST"],
        response_model=web_models.AuthSessionResponse,
    )
    auth_router.add_api_route(
        "/session",
        web_auth.api_auth_session,
        methods=["GET"],
        response_model=web_models.AuthSessionResponse,
    )
    app.include_router(auth_router)

    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(web_auth.require_auth)],
    )
    web_self_update.configure(
        effective_config_loader=web_settings._effective_config,
        plan_response_builder=web_plans.plan_response,
    )
    router.add_api_route(
        "/status",
        api_status,
        methods=["GET"],
        response_model=web_models.StatusResponse,
    )
    router.add_api_route(
        "/settings",
        web_settings.api_settings,
        methods=["GET"],
        response_model=web_models.SettingsResponse,
    )
    router.add_api_route(
        "/settings/managed",
        web_settings.api_update_managed_settings,
        methods=["POST"],
        response_model=web_models.ManagedSettingsUpdateResponse,
    )
    router.add_api_route(
        "/settings/managed",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/doctor",
        web_health.api_doctor,
        methods=["POST"],
        response_model=web_models.DoctorResponse,
    )
    router.add_api_route(
        "/doctor",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/ready",
        web_health.api_ready,
        methods=["GET"],
        response_model=web_models.ReadyResponse,
    )
    router.add_api_route(
        "/onboarding/checklist",
        web_onboarding.api_onboarding_checklist,
        methods=["POST"],
        response_model=web_models.OnboardingChecklistResponse,
    )
    router.add_api_route(
        "/onboarding/checklist",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/onboarding/dismiss",
        web_onboarding.api_onboarding_dismiss,
        methods=["POST"],
        response_model=web_models.OnboardingDismissResponse,
    )
    router.add_api_route(
        "/onboarding/dismiss",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/onboarding/core-update-tour",
        web_onboarding.api_core_update_tour,
        methods=["GET"],
        response_model=web_models.CoreUpdateTourResponse,
    )
    router.add_api_route(
        "/onboarding/core-update-tour",
        web_onboarding.api_update_core_update_tour,
        methods=["POST"],
        response_model=web_models.CoreUpdateTourResponse,
    )
    router.add_api_route(
        "/pending",
        web_pending.api_pending,
        methods=["GET"],
        response_model=web_models.PendingResponse,
    )
    router.add_api_route(
        "/pending/metadata",
        web_pending.api_pending_metadata,
        methods=["POST"],
        response_model=web_models.PendingMetadataRefreshResponse,
    )
    router.add_api_route(
        "/pending/metadata",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/update-targets",
        web_pending.api_update_targets,
        methods=["GET"],
        response_model=web_models.UpdateTargetsResponse,
    )
    router.add_api_route(
        "/retag-targets",
        web_retags.api_retag_targets,
        methods=["GET"],
        response_model=web_models.RetagTargetsResponse,
    )
    router.add_api_route(
        "/tracked-containers",
        web_tracking.api_tracked_containers,
        methods=["GET"],
        response_model=web_models.TrackedContainersResponse,
    )
    router.add_api_route(
        "/tracking-repairs",
        web_tracking.api_tracking_repair_plan,
        methods=["POST"],
        response_model=web_models.TrackingRepairPlan,
    )
    router.add_api_route(
        "/tracking-repairs/apply",
        web_tracking.api_apply_tracking_repair,
        methods=["POST"],
        response_model=web_models.ApplyJobResponse,
        status_code=202,
    )
    router.add_api_route(
        "/retag-targets/github-latest/refresh",
        web_retags.api_refresh_retag_github_latest,
        methods=["POST"],
        response_model=web_models.RetagTargetsResponse,
    )
    router.add_api_route(
        "/retag-plans",
        web_retags.api_create_retag_plan,
        methods=["POST"],
        response_model=web_models.RetagPlanResponse,
    )
    router.add_api_route(
        "/retag-plans/preview",
        web_retags.api_start_retag_plan_preview,
        methods=["POST"],
        response_model=web_models.RetagPreviewJobResponse,
        status_code=202,
    )
    router.add_api_route(
        "/retag-plans/preview/{preview_job_id}",
        web_retags.api_retag_plan_preview_job,
        methods=["GET"],
        response_model=web_models.RetagPreviewJobResponse,
    )
    router.add_api_route(
        "/retag-plans/apply",
        web_retags.api_apply_retag_plan,
        methods=["POST"],
        response_model=web_models.ApplyJobResponse,
        status_code=202,
    )
    router.add_api_route(
        "/pending/cleanup",
        web_pending.api_pending_cleanup,
        methods=["POST"],
        response_model=web_models.PendingCleanupResponse,
    )
    router.add_api_route(
        "/pending/rescan",
        web_pending_rescan.api_pending_rescan,
        methods=["POST"],
        response_model=web_models.PendingRescanResponse,
    )
    router.add_api_route(
        "/pending/rescan",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/pending/removal-plan",
        web_pending.api_pending_removal_plan,
        methods=["POST"],
        response_model=web_models.PendingRemovalPlanResponse,
    )
    router.add_api_route(
        "/pending/removal",
        web_pending.api_pending_removal,
        methods=["POST"],
        response_model=web_models.PendingCleanupResponse,
    )
    router.add_api_route(
        "/release-notes",
        web_release_notes.api_release_notes,
        methods=["GET"],
        response_model=web_models.ReleaseNotesResponse,
    )
    router.add_api_route(
        "/release-notes/refresh",
        web_release_notes.api_refresh_release_notes,
        methods=["POST"],
        response_model=web_models.ReleaseNotesResponse,
    )
    router.add_api_route(
        "/release-notifications/preview",
        web_release_notifications.api_preview_release_notifications,
        methods=["POST"],
        response_model=web_models.ReleaseNotificationResponse,
    )
    router.add_api_route(
        "/release-notifications/preview",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/release-notifications/send",
        web_release_notifications.api_send_release_notifications,
        methods=["POST"],
        response_model=web_models.ReleaseNotificationResponse,
    )
    router.add_api_route(
        "/release-notifications/send",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/release-notifications/test",
        web_release_notifications.api_test_release_notification_webhook,
        methods=["POST"],
        response_model=web_models.ReleaseNotificationTestResponse,
    )
    router.add_api_route(
        "/release-notifications/test",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    web_security.register_security_scan_routes(router, api_post_only_method_not_allowed)
    router.add_api_route(
        "/service-policies",
        web_state.api_service_policies,
        methods=["GET"],
        response_model=list[web_models.ServicePolicyRecord],
    )
    router.add_api_route(
        "/snoozes",
        web_state.api_snoozes,
        methods=["GET"],
        response_model=list[web_models.SnoozeRecord],
    )
    router.add_api_route(
        "/diagnostics/support-bundle",
        web_diagnostics.api_diagnostics_support_bundle,
        methods=["GET"],
        response_model=web_models.DiagnosticsSupportBundleResponse,
    )
    router.add_api_route(
        "/tag-exclusions",
        web_state.api_tag_exclusions,
        methods=["GET"],
        response_model=list[web_models.TagExclusionRuleRecord],
    )
    router.add_api_route(
        "/state/operations",
        web_state.api_state_operation,
        methods=["POST"],
        response_model=web_models.StateOperationResponse,
    )
    router.add_api_route(
        "/self-update",
        web_self_update.api_self_update,
        methods=["GET"],
        response_model=web_models.SelfUpdateResponse,
    )
    router.add_api_route(
        "/self-update/plan",
        web_self_update.api_plan_self_update,
        methods=["POST"],
        response_model=web_models.SelfUpdatePlanResponse,
    )
    router.add_api_route(
        "/self-update/prepare",
        web_self_update.api_prepare_self_update,
        methods=["POST"],
        response_model=web_models.SelfUpdatePrepareResponse,
    )
    router.add_api_route(
        "/self-update",
        web_self_update.api_apply_self_update,
        methods=["POST"],
        response_model=web_models.SelfUpdateApplyResponse,
    )
    router.add_api_route(
        "/container/restart",
        web_self_update.api_restart_container,
        methods=["POST"],
        response_model=web_models.ContainerRestartResponse,
        status_code=202,
    )
    router.add_api_route(
        "/container/restart",
        api_post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/plans",
        web_plans.api_create_plan,
        methods=["POST"],
        response_model=web_models.PlanResponse,
    )
    router.add_api_route(
        "/jobs",
        web_plans.api_create_job,
        methods=["POST"],
        response_model=web_models.ApplyJobResponse,
        status_code=202,
    )
    router.add_api_route(
        "/jobs/{job_id}",
        api_job,
        methods=["GET"],
        response_model=web_models.ApplyJobResponse,
    )
    router.add_api_route(
        "/jobs/{job_id}/stream",
        api_job_stream,
        methods=["GET"],
    )
    router.add_api_route(
        "/plans/apply",
        web_plans.api_apply_plan,
        methods=["POST"],
        response_model=web_models.ApplyJobResponse,
        status_code=202,
    )
    router.add_api_route(
        "/apply-jobs/{job_id}",
        api_apply_job,
        methods=["GET"],
        response_model=web_models.ApplyJobResponse,
    )
    router.add_api_route(
        "/runs",
        web_runs.api_runs,
        methods=["GET"],
        response_model=list[web_models.RunHistorySummary],
    )
    router.add_api_route(
        "/runs/{run_id}",
        web_runs.api_run_detail,
        methods=["GET"],
        response_model=web_models.RunDetail,
    )
    router.add_api_route(
        "/runs/{run_id}/rollback-plan",
        web_rollback.api_rollback_plan,
        methods=["GET"],
        response_model=web_models.RollbackPlanResponse,
    )
    router.add_api_route(
        "/runs/{run_id}/log",
        web_runs.api_run_log,
        methods=["GET"],
        response_model=web_models.RunLogResponse,
    )
    app.include_router(router)
    web_static.mount_static_spa_if_present(app, active_settings)
    return app


def load_web_settings(
    environ: Mapping[str, str] | None = None,
    *,
    static_dir: str | Path | None = None,
) -> web_models.WebSettings:
    env = os.environ if environ is None else environ
    config = load_config(env)
    configured_static = static_dir or env.get("WUD_WEB_STATIC_DIR") or None
    public_origin = web_auth._parse_public_origin(
        env.get("WUD_WEB_PUBLIC_ORIGIN", "")
    )
    host_docker_base = _parse_host_docker_base(env, config)
    legacy_scripts_enabled = parse_bool_env(
        web_settings.LEGACY_SCRIPTS_ENV,
        env.get(web_settings.LEGACY_SCRIPTS_ENV),
        default=True,
    )
    return web_models.WebSettings(
        config=config,
        auth_token=env.get("WUD_WEB_TOKEN", ""),
        dev_no_auth=web_auth._parse_bool(
            env.get("WUD_WEB_DEV_NO_AUTH"),
            default=False,
        ),
        allowed_origins=web_auth._parse_origins(
            env.get("WUD_WEB_ALLOWED_ORIGINS", "")
        ),
        public_origin=public_origin,
        allowed_hosts=web_auth._parse_allowed_hosts(
            env.get("WUD_WEB_ALLOWED_HOSTS", ""),
            public_origin=public_origin,
            bind_host=env.get("WUD_WEB_HOST", web_settings.DEFAULT_WEB_HOST),
        ),
        trusted_proxies=web_auth._parse_trusted_proxies(
            env.get("WUD_WEB_TRUSTED_PROXIES", "")
        ),
        secure_cookies=web_auth._parse_secure_cookie_mode(
            env.get("WUD_WEB_SECURE_COOKIES", "auto")
        ),
        mutations_enabled=web_auth._parse_bool(
            env.get("WUD_WEB_MUTATIONS_ENABLED"),
            default=False,
        ),
        static_dir=web_static.resolve_static_dir(configured_static),
        host_docker_base=host_docker_base,
        restart_container=_resolve_restart_container(env),
        wud_api_base_url=web_wud_api.configured_base_url(env),
        wud_api_startup_wait_seconds=web_wud_api.configured_startup_wait_seconds(env),
        wud_api_client=web_wud_api.configured_client_config(env),
        pending_source=(
            "api"
            if not legacy_scripts_enabled
            else web_pending_sources.configured_pending_source(env)
        ),
        legacy_scripts_enabled=legacy_scripts_enabled,
        release_notes_enabled_env=(
            parse_bool_env(
                web_settings.RELEASE_NOTES_ENABLED_ENV,
                env.get(web_settings.RELEASE_NOTES_ENABLED_ENV),
            )
            if web_settings.RELEASE_NOTES_ENABLED_ENV in env
            else None
        ),
        security_scan=web_security.configured_security_scan_config(env),
        command_env=dict(env),
    )


def run_web_from_namespace(args: object) -> int:
    if getattr(args, "web_command", None) == "reset-admin":
        return run_web_reset_admin_from_namespace(args)
    if getattr(args, "user", None):
        print("--user is only valid with web reset-admin", file=sys.stderr)
        return 1

    env = _environment_with_cli_overrides(args, os.environ)
    try:
        settings = load_web_settings(
            env,
            static_dir=getattr(args, "static_dir", None),
        )
        web_auth._validate_startup_auth(settings)
        host = str(
            getattr(args, "host", None)
            or env.get("WUD_WEB_HOST")
            or web_settings.DEFAULT_WEB_HOST
        )
        web_auth._validate_bind_host_allowed(settings, host)
        port = _parse_port(getattr(args, "port", None) or env.get("WUD_WEB_PORT"))
    except (ConfigError, web_auth.WebConfigError) as exc:
        print(exc, file=sys.stderr)
        return 1

    app = create_app(settings)
    setup_claim = str(getattr(app.state, "web_setup_claim", ""))
    web_startup.print_web_startup_summary(
        settings,
        host=host,
        port=port,
        setup_claim=setup_claim,
        environ=env,
    )
    try:
        web_startup.run_web_server(app, host=host, port=port)
    except OSError as exc:
        print(
            f"The WebUI could not open its listening port: {exc.strerror}. "
            "Check that the bind address is available and the port is not in use.",
            file=sys.stderr,
        )
        return 1
    return 0


def run_web_reset_admin_from_namespace(args: object) -> int:
    username = web_auth._normalize_username(str(getattr(args, "user", "") or ""))
    if not username:
        print("web reset-admin requires --user USERNAME", file=sys.stderr)
        return 1

    env = _environment_with_cli_overrides(args, os.environ)
    try:
        settings = load_web_settings(env)
        web_auth._validate_startup_auth(settings)
        host = str(
            getattr(args, "host", None)
            or env.get("WUD_WEB_HOST")
            or web_settings.DEFAULT_WEB_HOST
        )
        web_auth._validate_bind_host_allowed(settings, host)
        port = _parse_port(getattr(args, "port", None) or env.get("WUD_WEB_PORT"))
        recovery = web_auth.issue_admin_recovery_claim(settings, username)
    except (
        ConfigError,
        web_auth.WebConfigError,
        web_auth.WebAdminResetError,
    ) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(
        web_auth._reset_admin_url(
            settings,
            host=host,
            port=port,
            claim=recovery.claim,
            username=recovery.username,
        )
    )
    return 0


def api_status(request: Request) -> web_models.StatusResponse:
    settings = web_request_context.request_settings(request)
    pending = web_pending.pending_response(
        settings,
        include_grouping=False,
        include_wud_metadata=False,
    )
    db_ready, db_warning = web_database.database_ready(settings)
    wud_api = web_wud_api.get_snapshot(settings, include_containers=True)
    warnings = list(pending.warnings)
    if db_warning:
        warnings.append(db_warning)
    return web_models.StatusResponse(
        ok=db_ready,
        version=__version__,
        wud_file=str(settings.config.wud_out_file),
        wud_file_exists=settings.config.wud_out_file.is_file(),
        pending_count=pending.count,
        pending_source=pending.source,
        source_hash=pending.source_hash,
        db_path=str(settings.config.db_path),
        db_ready=db_ready,
        auth_required=settings.auth_required,
        dev_auth_bypass=settings.dev_no_auth,
        setup_required=web_auth._setup_required(settings),
        mutations_enabled=settings.mutations_enabled,
        timezone=settings.config.timezone_name,
        auto_update_scheduler_enabled=settings.mutations_enabled,
        static_spa_available=web_static.static_spa_available(settings),
        wud_api=wud_api.status,
        warnings=warnings,
    )


def api_post_only_method_not_allowed() -> JSONResponse:
    return JSONResponse(
        {"detail": "method not allowed"},
        status_code=405,
        headers={"Allow": "POST"},
    )


def api_job(job_id: str, request: Request) -> web_models.ApplyJobResponse:
    return web_jobs._apply_job_response_for_request(job_id, request)


def api_apply_job(job_id: str, request: Request) -> web_models.ApplyJobResponse:
    return api_job(job_id, request)


def api_job_stream(
    job_id: str,
    request: Request,
    log_tail_bytes: int = Query(
        default=web_jobs.DEFAULT_JOB_LOG_TAIL_BYTES,
        ge=1,
    ),
) -> StreamingResponse:
    settings = web_request_context.request_settings(request)
    web_jobs._require_apply_job(job_id, request)
    return StreamingResponse(
        web_jobs._apply_job_stream(
            request.app.state,
            settings,
            job_id,
            log_tail_bytes=min(log_tail_bytes, web_runs.MAX_LOG_TAIL_BYTES),
            safe_log_path=web_runs._safe_log_path,
            read_log_tail=web_runs._read_log_tail,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _auto_update_tick(
    app: FastAPI,
    settings: web_models.WebSettings,
    *,
    now: datetime | None = None,
) -> web_models.ApplyJobResponse | None:
    return web_scheduler._auto_update_tick(
        app,
        settings,
        effective_config_loader=web_settings._effective_config,
        now=now,
    )


def _start_auto_update_scheduler(
    app: FastAPI,
    settings: web_models.WebSettings,
) -> Any:
    return web_scheduler.start_auto_update_scheduler(
        app,
        settings,
        effective_config_loader=web_settings._effective_config,
    )


def _resolve_restart_container(env: Mapping[str, str]) -> str:
    configured = env.get("WUD_WEB_RESTART_CONTAINER")
    if configured is not None:
        return web_self_update._validate_restart_container_target(configured.strip())
    if not _running_in_container():
        return ""
    return web_self_update._validate_restart_container_target(
        env.get("HOSTNAME", "").strip()
    )


def _running_in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        marker in cgroup
        for marker in ("/docker/", "/kubepods/", "/containerd/")
    )


def _parse_host_docker_base(
    env: Mapping[str, str],
    config: UpdaterConfig,
) -> Path | None:
    value = env.get("HOST_DOCKER_BASE") or ""
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise web_auth.WebConfigError("HOST_DOCKER_BASE must be an absolute path")
    if not config.docker_base.is_absolute():
        raise web_auth.WebConfigError(
            "DOCKER_BASE must be an absolute path when HOST_DOCKER_BASE is set"
        )
    return path


def _environment_with_cli_overrides(
    args: object,
    environ: Mapping[str, str],
) -> dict[str, str]:
    env = dict(environ)
    for attr, name in (
        ("base", "DOCKER_BASE"),
        ("file", "WUD_OUT_FILE"),
        ("log_dir", "WUD_LOG_DIR"),
        ("db_path", "WUD_DB_PATH"),
    ):
        value = getattr(args, attr, None)
        if value:
            env[name] = str(value)
    return env


def _parse_port(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_WEB_PORT
    try:
        port = int(str(value), 10)
    except ValueError as exc:
        raise web_auth.WebConfigError("WUD_WEB_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise web_auth.WebConfigError("WUD_WEB_PORT must be between 1 and 65535")
    return port
