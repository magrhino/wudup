"""Recreate-scope guard that keeps an update from stopping WUDup or its Docker endpoint.

The updater's apply preflight, WebUI plan review, and retag apply all share it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .command import CommandError
from .compose import ComposeStack
from .container_identity import (
    DockerEndpoint,
    container_serves_docker_endpoint,
    docker_endpoint,
    network_mode_container,
    resolve_docker_endpoint,
)
from .self_update import is_self_update_target
from .updater_lifecycle_scope import runtime_services_for_scope
from .updater_models import UpdateScope

_SELF_UPDATE_BLOCKED = "self-update-recreate-blocked"
_DOCKER_ENDPOINT_BLOCKED = "docker-endpoint-recreate-blocked"
_SELF_UPDATE_MESSAGE = (
    "This update would stop or recreate WUDup itself. "
    "Use the self-update action for WUDup, or update this stack from "
    "the host. Remove it from this selection to update other services."
)
# Bound on following network_mode container:<name> links before failing closed.
_NETWORK_MODE_HOP_LIMIT = 5
_SCOPE_LOOKUP_MESSAGE = (
    "Could not verify the update's recreate scope; no update was applied. "
    "Check Docker access and the Compose configuration, then retry."
)
_CONTAINER_LOOKUP_MESSAGE = (
    "Could not verify whether this update includes the WUDup "
    "container; no update was applied. Check Docker access and "
    "the Compose configuration, then retry."
)
_DOCKER_ENDPOINT_LOOKUP_MESSAGE = (
    "Could not check whether this update would stop the Docker endpoint "
    "WUDup uses (DOCKER_HOST); no update was applied. Check Docker access "
    "and the Compose configuration, then retry."
)


@dataclass(frozen=True)
class SelfScopeIdentity:
    """WUDup's own container and Docker endpoint, looked up once per check."""

    protect_self: bool
    protected_id: str
    identity_error: str
    endpoint: DockerEndpoint | None
    resolved_endpoint: Callable[[], DockerEndpoint]


@dataclass(frozen=True)
class SelfScopeBlock:
    """Why one stack's recreate scope would stop WUDup or its Docker endpoint."""

    reason: str
    message: str
    services: frozenset[str]


def self_scope_identity(
    docker: Any,
    protected_container: str | None,
    environ: Mapping[str, str],
) -> SelfScopeIdentity | None:
    """Return what the self-scope guard protects, or None when nothing needs protecting."""

    protect_self = protected_container is not None
    endpoint = docker_endpoint(environ)
    if not protect_self and endpoint is None:
        return None

    @functools.cache
    def resolved_endpoint() -> DockerEndpoint:
        # A slow resolver only delays runs that have a container to check.
        if endpoint is None:
            # Raised inside the guard's lookup handler, so the stack is blocked.
            raise ValueError("DOCKER_HOST is not a network endpoint")
        return resolve_docker_endpoint(endpoint)

    protected_id = ""
    identity_error = ""
    if protected_container:
        try:
            protected_id = docker.container_id(protected_container)
        except CommandError:
            pass
        if not protected_id:
            identity_error = (
                "Could not verify the WUDup container identity; no update was applied. "
                "Check Docker access and WUD_WEB_RESTART_CONTAINER, then retry."
            )
    return SelfScopeIdentity(
        protect_self=protect_self,
        protected_id=protected_id,
        identity_error=identity_error,
        endpoint=endpoint,
        resolved_endpoint=resolved_endpoint,
    )


def self_scope_block(
    identity: SelfScopeIdentity,
    docker: Any,
    compose: Any,
    stack: ComposeStack,
    update_scope: Callable[[], UpdateScope],
) -> tuple[UpdateScope | None, SelfScopeBlock | None]:
    """Check one stack's strict recreate scope; shared by apply preflight and plan review."""

    if identity.identity_error:
        return None, SelfScopeBlock(
            _SELF_UPDATE_BLOCKED, identity.identity_error, frozenset(),
        )
    try:
        scope = update_scope()
    except (CommandError, ValueError):
        reason = _SELF_UPDATE_BLOCKED if identity.protect_self else _DOCKER_ENDPOINT_BLOCKED
        return None, SelfScopeBlock(reason, _SCOPE_LOOKUP_MESSAGE, frozenset())
    return scope, _scoped_block(identity, docker, compose, stack, scope)


def _scoped_block(
    identity: SelfScopeIdentity,
    docker: Any,
    compose: Any,
    stack: ComposeStack,
    scope: UpdateScope,
) -> SelfScopeBlock | None:
    services = set(runtime_services_for_scope(scope))
    protected_services = {
        item.service for item in stack.service_images
        if identity.protect_self
        and item.service in services
        and is_self_update_target(item.image)
    }
    container_ids, lookup_block = _scope_container_ids(identity, compose, stack, services)
    if lookup_block is not None:
        return replace(lookup_block, services=frozenset(protected_services))
    if identity.protected_id and identity.protected_id in container_ids:
        protected_services.update(services)
    if protected_services:
        return SelfScopeBlock(
            _SELF_UPDATE_BLOCKED, _SELF_UPDATE_MESSAGE, frozenset(protected_services),
        )
    if identity.endpoint is None:
        return None
    return _endpoint_block(identity, docker, container_ids)


def _scope_container_ids(
    identity: SelfScopeIdentity,
    compose: Any,
    stack: ComposeStack,
    services: set[str],
) -> tuple[list[str], SelfScopeBlock | None]:
    """Return the scope's container IDs, or a fail-closed block when they cannot be read."""

    if not identity.protected_id and identity.endpoint is None:
        return [], None
    try:
        container_ids = compose.ps_quiet_checked(
            stack.directory, stack.file, tuple(sorted(services)),
            project_directory=stack.project_directory,
        )
    except CommandError:
        if identity.protected_id:
            return [], SelfScopeBlock(
                _SELF_UPDATE_BLOCKED, _CONTAINER_LOOKUP_MESSAGE, frozenset(),
            )
        return [], SelfScopeBlock(
            _DOCKER_ENDPOINT_BLOCKED, _DOCKER_ENDPOINT_LOOKUP_MESSAGE, frozenset(),
        )
    return container_ids, None


def _endpoint_block(
    identity: SelfScopeIdentity,
    docker: Any,
    container_ids: Sequence[str],
) -> SelfScopeBlock | None:
    endpoint = identity.endpoint
    if endpoint is None:
        return None
    try:
        endpoint_services = _docker_endpoint_services(
            docker, container_ids, identity.resolved_endpoint,
        )
    except (CommandError, TypeError, ValueError):
        return SelfScopeBlock(
            _DOCKER_ENDPOINT_BLOCKED, _DOCKER_ENDPOINT_LOOKUP_MESSAGE, frozenset(),
        )
    if not endpoint_services:
        return None
    return SelfScopeBlock(
        _DOCKER_ENDPOINT_BLOCKED,
        _docker_endpoint_message(endpoint.host, endpoint_services),
        frozenset(endpoint_services),
    )


def _docker_endpoint_services(
    docker: Any,
    container_ids: Sequence[str],
    endpoint: Callable[[], DockerEndpoint],
) -> set[str]:
    """Return services of scoped containers that may serve ``endpoint()``.

    A container that shares another container's network (``network_mode``) is
    reached through that container's names, addresses, and published ports.
    """

    details_by_container: dict[str, dict[str, object]] = {}

    def inspect(container: str) -> dict[str, object]:
        if container not in details_by_container:
            details_by_container[container] = docker.container_dns_details(container)
        return details_by_container[container]

    services: set[str] = set()
    for container_id in container_ids:
        details = inspect(container_id)
        network_details = _network_owner_details(details, inspect)
        if container_serves_docker_endpoint(network_details, endpoint()):
            service = details.get("Service")
            services.add(service if isinstance(service, str) and service else container_id)
    return services


def _network_owner_details(
    details: dict[str, object],
    inspect: Callable[[str], dict[str, object]],
) -> dict[str, object]:
    """Follow ``network_mode: container:<name>`` to the container owning the network."""

    seen: set[str] = set()
    while provider := network_mode_container(details):
        if provider in seen or len(seen) >= _NETWORK_MODE_HOP_LIMIT:
            raise ValueError("network_mode container chain does not end")
        seen.add(provider)
        details = inspect(provider)
    return details


def _docker_endpoint_message(endpoint_host: str, services: Iterable[str]) -> str:
    return (
        f"This update would stop or recreate {', '.join(sorted(services))}, which serves "
        f"the Docker endpoint WUDup uses (DOCKER_HOST host {endpoint_host}). WUDup would "
        "lose Docker access and could not finish or roll back the update. Update that "
        "service from the host instead, and remove it from this selection to update "
        "other services."
    )
