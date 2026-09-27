"""Environment-to-``WebSettings`` loader for the WUDup WebUI."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import (
    web_auth,
    web_models,
    web_pending_sources,
    web_security,
    web_self_update,
    web_settings,
    web_static,
    web_wud_api,
)
from .config import (
    UpdaterConfig,
    load_config,
    parse_bool_env,
)


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
