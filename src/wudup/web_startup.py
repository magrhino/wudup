"""WebUI listeners and startup summary helpers."""

from __future__ import annotations

import errno
import logging
import socket
import sys
from contextlib import ExitStack

import uvicorn
from fastapi import FastAPI

from .web_auth import _setup_url, weak_auth_token_warning
from .web_models import WebSettings

DOCTOR_COMMAND = "docker compose exec wudup doctor"


def run_web_server(app: FastAPI, *, host: str, port: int) -> None:
    """Serve the container wildcard on both families; keep specific binds exact."""
    if host == "0.0.0.0":
        addresses = [(socket.AF_INET, (host, port)), (socket.AF_INET6, ("::", port))]
    else:
        addresses = list(dict.fromkeys(
            (family, address)
            for family, _kind, _protocol, _name, address in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE,
            )
        ))

    # Separate sockets preserve native IPv4 client addresses for auth/readiness.
    with ExitStack() as stack:
        listeners = []
        for family, address in addresses:
            if listeners:
                address = (address[0], listeners[0].getsockname()[1], *address[2:])
            try:
                listener = socket.create_server(address, family=family)
            except OSError as exc:
                if (
                    (host == "0.0.0.0" and family == socket.AF_INET)
                    or len(addresses) == 1
                    or exc.errno not in {
                        errno.EAFNOSUPPORT,
                        errno.EPROTONOSUPPORT,
                        errno.EADDRNOTAVAIL,
                    }
                ):
                    raise
                logging.getLogger(__name__).warning(
                    "A WebUI listening address is unavailable; continuing with other "
                    "available addresses. Check the host/container network if both "
                    "IPv4 and IPv6 connections are needed."
                )
            else:
                listeners.append(stack.enter_context(listener))
        if not listeners:
            raise OSError(errno.EADDRNOTAVAIL, "No WebUI bind address is available")
        uvicorn.Server(uvicorn.Config(app, host=host, port=port)).run(
            sockets=listeners,
        )


def print_web_startup_summary(
    settings: WebSettings,
    *,
    host: str,
    port: int,
    setup_claim: str,
) -> None:
    """Print the concise web-mode setup summary."""

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
        f"  Doctor: {DOCTOR_COMMAND}",
    ]
    token_warning = weak_auth_token_warning(settings)
    if token_warning:
        lines.append(f"  Warning: {token_warning}")
    print("\n".join(lines), file=sys.stderr)


def _web_url(settings: WebSettings, *, host: str, port: int) -> str:
    origin = settings.public_origin or _fallback_origin(host=host, port=port)
    return f"{origin}/"


def _fallback_origin(*, host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"
