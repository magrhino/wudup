"""Retag recreate-scope guard for WUDup itself and its Docker endpoint."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from .command import CommandError
from .compose import ComposeCli, ComposeStack
from .container_identity import DockerEndpoint, docker_endpoint
from .docker_cli import DockerCli
from .self_update import is_self_update_target
from .updater_preflight import (
    _DOCKER_ENDPOINT_LOOKUP_MESSAGE,
    _SELF_UPDATE_MESSAGE,
    _docker_endpoint_message,
    _docker_endpoint_services,
)
from .web_models import WebSettings


class RetagScopeBlocked(RuntimeError):
    """Raised when a retag would stop or recreate WUDup or its Docker endpoint."""

    def __init__(
        self,
        message: str,
        *,
        stack: str = "",
        services: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.stack = stack
        self.services = tuple(sorted(services))


def validate_retag_recreate_scope(
    settings: WebSettings,
    compose: ComposeCli,
    docker: DockerCli,
    stacks: Sequence[tuple[ComposeStack, Sequence[str]]],
) -> None:
    """Fail closed before any retag mutation that would stop WUDup or its Docker endpoint.

    Retag recreates only the selected services (``--no-deps``) and never passes
    ``--remove-orphans``, so each stack's scope is exactly ``services``. Mirrors the pending-update preflight in
    ``updater_preflight.validate_self_update_scope``.
    """
    environ = settings.command_env if settings.command_env is not None else os.environ
    endpoint = docker_endpoint(environ)
    protected_id = _protected_container_id(settings, docker)
    for stack, services in stacks:
        scoped = set(services)
        _reject_self_images(stack, scoped)
        if protected_id or endpoint is not None:
            container_ids = _scoped_container_ids(
                compose, stack, scoped, protected_id=protected_id
            )
            if protected_id and protected_id in container_ids:
                raise RetagScopeBlocked(
                    f"[{stack.name}] {_SELF_UPDATE_MESSAGE}",
                    stack=stack.name,
                    services=scoped,
                )
            if endpoint is not None:
                _reject_docker_endpoint(docker, stack, container_ids, endpoint)


def _protected_container_id(settings: WebSettings, docker: DockerCli) -> str:
    """Return the WUDup container ID, or "" when no restart container is configured."""
    if not settings.restart_container:
        return ""
    protected_id = ""
    try:
        protected_id = docker.container_id(settings.restart_container)
    except CommandError:
        pass
    if not protected_id:
        raise RetagScopeBlocked(
            "Could not verify the WUDup container identity; no retag was applied. "
            "Check Docker access and WUD_WEB_RESTART_CONTAINER, then retry."
        )
    return protected_id


def _reject_self_images(stack: ComposeStack, scoped: set[str]) -> None:
    self_services = {
        item.service for item in stack.service_images
        if item.service in scoped and is_self_update_target(item.image)
    }
    if self_services:
        raise RetagScopeBlocked(
            f"[{stack.name}] {_SELF_UPDATE_MESSAGE}",
            stack=stack.name,
            services=self_services,
        )


def _scoped_container_ids(
    compose: ComposeCli,
    stack: ComposeStack,
    scoped: set[str],
    *,
    protected_id: str,
) -> list[str]:
    try:
        return compose.ps_quiet_checked(
            stack.directory, stack.file, tuple(sorted(scoped)),
            project_directory=stack.project_directory,
        )
    except CommandError as exc:
        if protected_id:
            raise RetagScopeBlocked(
                f"[{stack.name}] Could not verify whether this retag includes the "
                "WUDup container; no retag was applied. Check Docker access and "
                "the Compose configuration, then retry.",
                stack=stack.name,
            ) from exc
        raise RetagScopeBlocked(
            f"[{stack.name}] {_DOCKER_ENDPOINT_LOOKUP_MESSAGE}", stack=stack.name
        ) from exc


def _reject_docker_endpoint(
    docker: DockerCli,
    stack: ComposeStack,
    container_ids: Sequence[str],
    endpoint: DockerEndpoint,
) -> None:
    try:
        endpoint_services = _docker_endpoint_services(docker, container_ids, endpoint)
    except (CommandError, TypeError, ValueError) as exc:
        raise RetagScopeBlocked(
            f"[{stack.name}] {_DOCKER_ENDPOINT_LOOKUP_MESSAGE}", stack=stack.name
        ) from exc
    if endpoint_services:
        raise RetagScopeBlocked(
            f"[{stack.name}] " + _docker_endpoint_message(endpoint.host, endpoint_services),
            stack=stack.name,
            services=endpoint_services,
        )
