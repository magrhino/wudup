"""WebUI listeners and startup summary helpers."""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from .doctor import DEFAULT_CONTAINER_SCRIPTS_DIR, MANAGED_SCRIPTS_MARKER
from .web_auth import _setup_url
from .web_models import WebSettings

SCRIPT_SYNC_STATUS_ENV = "WUD_SCRIPT_SYNC_STATUS"
DOCTOR_COMMAND = "docker compose exec wudup doctor"
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def run_web_server(app: FastAPI, *, host: str, port: int) -> None:
    """Serve the container wildcard on both families; keep specific binds exact."""
    if host != "0.0.0.0":
        uvicorn.run(app, host=host, port=port)
        return

    # Separate sockets preserve native IPv4 client addresses for auth/readiness.
    with ExitStack() as stack:
        ipv4 = stack.enter_context(socket.create_server((host, port)))
        listeners = [ipv4]
        try:
            ipv6 = socket.create_server(
                ("::", ipv4.getsockname()[1]), family=socket.AF_INET6,
            )
        except OSError as exc:
            if exc.errno not in {
                errno.EAFNOSUPPORT,
                errno.EPROTONOSUPPORT,
                errno.EADDRNOTAVAIL,
            }:
                raise
            logging.getLogger(__name__).warning(
                "IPv6 is unavailable; the WebUI will accept IPv4 connections only. "
                "Enable IPv6 on the host/container network to accept IPv6 connections."
            )
        else:
            listeners.append(stack.enter_context(ipv6))
        uvicorn.Server(uvicorn.Config(app, host=host, port=port)).run(
            sockets=listeners,
        )


def print_web_startup_summary(
    settings: WebSettings,
    *,
    host: str,
    port: int,
    setup_claim: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Print the concise web-mode setup summary."""

    env = os.environ if environ is None else environ
    url_label = "Setup link" if setup_claim else "Web URL"
    url = (
        _setup_url(settings, host=host, port=port, claim=setup_claim)
        if setup_claim
        else _web_url(settings, host=host, port=port)
    )
    lines = [
        "WUDup WebUI startup summary",
        f"  {url_label}: {url}",
        f"  Docker base: {settings.config.docker_base}",
        f"  WUD output: {settings.config.wud_out_file}",
        f"  Script sync: {_script_sync_summary(env)}",
        f"  Doctor: {DOCTOR_COMMAND}",
    ]
    print("\n".join(lines), file=sys.stderr)


def _web_url(settings: WebSettings, *, host: str, port: int) -> str:
    origin = settings.public_origin or _fallback_origin(host=host, port=port)
    return f"{origin}/"


def _fallback_origin(*, host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _script_sync_summary(env: Mapping[str, str]) -> str:
    scripts_dir = Path(env.get("WUD_SCRIPTS_DIR") or DEFAULT_CONTAINER_SCRIPTS_DIR)
    status = env.get(SCRIPT_SYNC_STATUS_ENV, "").strip()
    if status:
        return _script_sync_status_message(status, scripts_dir)

    legacy_scripts = env.get("WUDUP_LEGACY_SCRIPTS")
    if legacy_scripts is not None and legacy_scripts.strip().lower() in FALSE_VALUES:
        return "disabled by WUDUP_LEGACY_SCRIPTS"

    configured_sync = env.get("WUD_SYNC_SCRIPTS")
    if configured_sync is not None:
        normalized_sync = configured_sync.strip().lower()
        if normalized_sync in TRUE_VALUES:
            return "forced by WUD_SYNC_SCRIPTS; entrypoint status unavailable"
        if normalized_sync != "auto":
            return "disabled by WUD_SYNC_SCRIPTS"

    default_dir = Path(DEFAULT_CONTAINER_SCRIPTS_DIR)
    if scripts_dir != default_dir:
        return f"entrypoint status unavailable; configured scripts dir is {scripts_dir}"
    if scripts_dir.is_dir() and os.access(scripts_dir, os.W_OK):
        marker_status = (
            "managed marker present"
            if (scripts_dir / MANAGED_SCRIPTS_MARKER).exists()
            else "managed marker absent"
        )
        return f"auto fallback sees writable {scripts_dir} ({marker_status})"
    return f"auto fallback did not detect writable {scripts_dir}"


def _script_sync_status_message(status: str, scripts_dir: Path) -> str:
    if status == "auto-detected":
        return f"auto-detected writable {scripts_dir} and synced packaged scripts"
    if status == "auto-not-detected":
        return f"auto mode did not detect writable {scripts_dir}"
    if status == "forced":
        return "forced by WUD_SYNC_SCRIPTS and synced packaged scripts"
    if status == "disabled":
        return "disabled by WUD_SYNC_SCRIPTS"
    if status == "skipped-doctor":
        return "not run for doctor command"
    if status == "manual-command":
        return "manual sync command"
    return f"entrypoint reported {status}"
