"""Compose runtime identities shared by retag discovery and apply revalidation."""

from __future__ import annotations

from pathlib import Path

from .command import CommandError, CommandRunner
from .compose import (
    COMPOSE_RUNTIME_FORMAT,
    ComposeStack,
    compose_runtime_extra_config_files,
    compose_runtime_project_shared,
    compose_runtime_service_key,
    compose_runtime_service_keys,
)
from .docker_cli import DockerCli
from .web_models import WebSettings


def _command_runner(settings: WebSettings) -> CommandRunner:
    if settings.command_env is not None:
        return CommandRunner(env=settings.command_env)
    return CommandRunner()


def _running_retag_compose_service_keys(
    settings: WebSettings,
) -> set[tuple[frozenset[Path], str, str]] | None:
    docker = DockerCli(runner=_command_runner(settings))
    try:
        rows = docker.ps_format(COMPOSE_RUNTIME_FORMAT)
    except CommandError:
        return None
    return compose_runtime_service_keys(rows)


def _retag_project_config_files(
    settings: WebSettings,
    stack: ComposeStack,
    project_name: str,
) -> tuple[tuple[Path, ...], bool] | None:
    """Return extra Compose files the project loaded besides ``stack.file``,
    and whether another Compose file set shares the project name."""
    docker = DockerCli(runner=_command_runner(settings))
    try:
        rows = docker.ps_format(COMPOSE_RUNTIME_FORMAT, all_containers=True)
    except CommandError:
        return None
    runtime_keys = compose_runtime_service_keys(rows)
    project_directory = stack.project_directory or stack.directory
    return (
        compose_runtime_extra_config_files(
            project_directory, stack.file, project_name, runtime_keys
        ),
        compose_runtime_project_shared(
            project_directory, stack.file, project_name, runtime_keys
        ),
    )


def _retag_compose_service_key(
    stack: ComposeStack,
    service: str,
    *,
    project_name: str | None = None,
) -> tuple[frozenset[Path], str, str]:
    project_directory = stack.project_directory or stack.directory
    return compose_runtime_service_key(
        project_directory,
        stack.file,
        stack.project_name if project_name is None else project_name,
        service,
    )
