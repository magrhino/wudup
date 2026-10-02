"""In-process Compose/Docker fakes for health-gate container lookups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from wudup.command import CommandError, CommandResult
from wudup.compose import ComposeStack
from wudup.updater_lifecycle_health import CONTAINER_SUMMARY_FORMAT


class FakeHealthCompose:
    """`docker compose ps -q` that lists only running containers.

    ``running`` maps each service to its running container IDs. A lookup that
    names a service in ``failing`` raises, and with ``fail_all`` every lookup
    raises, as when the Compose file is invalid or Docker is unreachable.
    """

    def __init__(
        self,
        running: Mapping[str, Sequence[str]],
        *,
        failing: Sequence[str] = (),
        fail_all: bool = False,
    ) -> None:
        self.running = running
        self.failing = set(failing)
        self.fail_all = fail_all

    def ps_quiet(self, directory, file, services=None, *, project_directory=None):
        try:
            return self.ps_quiet_checked(directory, file, services)
        except CommandError:
            return []

    def ps_quiet_checked(
        self, directory, file, services=None, *, project_directory=None
    ):
        selected = list(services or self.running)
        if self.fail_all or self.failing.intersection(selected):
            raise CommandError(CommandResult(("docker", "compose", "ps"), None, 1))
        return [cid for service in selected for cid in self.running[service]]


class FakeHealthDocker:
    """`docker inspect` returning a fixed summary and health log per container."""

    def __init__(
        self,
        summaries: Mapping[str, str] | None = None,
        health_logs: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.summaries = summaries or {}
        self.health_logs = health_logs or {}

    def try_inspect(self, cid: str, fmt: str) -> list[str]:
        if fmt == CONTAINER_SUMMARY_FORMAT:
            summary = self.summaries.get(cid, f"/{cid}|running|healthy|0|0")
            return [summary] if summary else []
        return list(self.health_logs.get(cid, ()))


def health_stack(directory: Path) -> ComposeStack:
    return ComposeStack(
        index=0,
        directory=directory,
        file="docker-compose.yml",
        name="stack",
        images=(),
        service_images=(),
    )
