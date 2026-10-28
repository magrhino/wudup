"""Docker CLI subprocess layer for the Python updater."""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import request_scope
from .command import CommandError, CommandResult, CommandRunner
from .platforms import ImagePlatform, platform_from_parts

DEFAULT_CONTAINER_FORMAT = "{{.Names}}\t{{.Image}}"
IMAGE_ID_FORMAT = "{{.Id}}"
IMAGE_DIGESTS_FORMAT = "{{range .RepoDigests}}{{println .}}{{end}}"
IMAGE_PLATFORM_FORMAT = "{{.Os}} {{.Architecture}} {{.Variant}}"
CONTAINER_ID_FORMAT = "{{.Id}}"
CONTAINER_IMAGE_ID_FORMAT = "{{.Image}}"
CONTAINER_STATE_ERROR_FORMAT = "{{.State.Error}}"
# Only long-standing inspect fields, so the template parses on older engines;
# newer per-network keys such as DNSNames simply appear inside "Networks".
CONTAINER_DNS_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},'
    '"Service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"Networks":{{json .NetworkSettings.Networks}},'
    '"Ports":{{json .NetworkSettings.Ports}},'
    '"PortBindings":{{json .HostConfig.PortBindings}},'
    '"NetworkMode":{{json .HostConfig.NetworkMode}}}'
)


@dataclass(frozen=True)
class ContainerImage:
    name: str
    image: str


class DockerCli:
    """Thin Docker command wrapper matching the shell updater's call shapes."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        executable: str = "docker",
    ) -> None:
        self.runner = runner or CommandRunner()
        self.executable = executable

    def ps_format(
        self,
        fmt: str = DEFAULT_CONTAINER_FORMAT,
        *,
        all_containers: bool = False,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        args = [self.executable, "ps"]
        if all_containers:
            args.append("--all")
        args.extend(("--format", fmt))
        return self.runner.capture_lines(
            args,
            check=True,
            timeout_seconds=timeout_seconds,
        )

    def container_images(self) -> list[ContainerImage]:
        containers: list[ContainerImage] = []
        for line in self.ps_format(DEFAULT_CONTAINER_FORMAT):
            name, sep, image = line.partition("\t")
            if sep and name and image:
                containers.append(ContainerImage(name=name, image=image))
        return containers

    def try_container_images(self) -> list[ContainerImage]:
        """Return running container images, or an empty list if Docker fails."""

        try:
            return self.container_images()
        except CommandError:
            return []

    def image_inspect(self, image: str, fmt: str) -> list[str]:
        return self.runner.capture_lines(
            [self.executable, "image", "inspect", "--format", fmt, image],
            check=True,
        )

    def image_id(self, image: str) -> str:
        try:
            return _first_nonblank(self.image_inspect(image, IMAGE_ID_FORMAT))
        except CommandError:
            return ""

    def image_platform(self, image: str) -> ImagePlatform | None:
        try:
            parts = _first_nonblank(self.image_inspect(image, IMAGE_PLATFORM_FORMAT)).split()
        except CommandError:
            return None
        if len(parts) not in {2, 3}:
            return None
        return platform_from_parts(*parts)

    def image_repo_digests(self, image: str) -> list[str]:
        try:
            return _nonblank_lines(self.image_inspect(image, IMAGE_DIGESTS_FORMAT))
        except CommandError:
            return []

    def image_digest(self, image: str) -> str:
        return _first_nonblank(self.image_repo_digests(image))

    def try_image_label(self, image: str, label: str) -> tuple[str, CommandError | None]:
        fmt = f'{{{{ index .Config.Labels "{label}" }}}}'
        try:
            value = _first_nonblank(self.image_inspect(image, fmt))
        except CommandError as exc:
            return "", exc
        if value == "<no value>":
            return "", None
        return value, None

    def manifest_inspect(self, image: str) -> CommandResult:
        return self.runner.capture(
            [self.executable, "manifest", "inspect", image],
            check=True,
        )

    def check_manifest_available(self, image: str) -> None:
        """Raise ``CommandError`` when the registry has no manifest for ``image``.

        Inside a WebUI request a recent success may be reused; see
        ``request_scope.reuse_registry_check``.
        """

        environment_key = getattr(self.runner, "environment_key", None)
        request_scope.reuse_registry_check(
            (
                "manifest-available",
                self.executable,
                image,
                environment_key() if callable(environment_key) else None,
            ),
            lambda: self.manifest_inspect(image),
        )

    def manifest_inspect_verbose(self, image: str) -> CommandResult:
        return self.runner.capture(
            [self.executable, "manifest", "inspect", "--verbose", image],
            check=True,
        )

    def pull_image(self, image: str) -> CommandResult:
        # Stream pull progress to the container logs, keeping a bounded stderr
        # tail so a failure can report Docker's error.
        return self.runner.run_streaming([self.executable, "pull", image], check=True)

    def inspect(self, target: str, fmt: str) -> list[str]:
        return self.runner.capture_lines(
            [self.executable, "inspect", "-f", fmt, target],
            check=True,
        )

    def try_inspect(self, target: str, fmt: str) -> list[str]:
        try:
            return self.inspect(target, fmt)
        except CommandError:
            return []

    def container_id(self, container: str) -> str:
        return _first_nonblank(self.inspect(container, CONTAINER_ID_FORMAT))

    def container_image_id(self, container: str) -> str:
        return _first_nonblank(self.inspect(container, CONTAINER_IMAGE_ID_FORMAT))

    def container_state_error(self, container: str) -> str:
        """Return the error from the container's last failed start, if any."""
        return _first_nonblank(self.inspect(container, CONTAINER_STATE_ERROR_FORMAT))

    def container_dns_details(self, container: str) -> dict[str, object]:
        """Return the container's name, Compose service, networks, ports, and network mode.

        Raises ``CommandError`` when Docker fails and ``ValueError`` or ``TypeError``
        when the output is not the expected JSON object.
        """
        details = json.loads(_first_nonblank(self.inspect(container, CONTAINER_DNS_FORMAT)))
        if not isinstance(details, dict):
            raise TypeError("unexpected docker inspect output")
        return details

    def try_container_image_id(self, container: str) -> str:
        return _first_nonblank(
            self.try_inspect(container, CONTAINER_IMAGE_ID_FORMAT)
        )

    def restart_container(
        self,
        container: str,
        *,
        timeout_seconds: int = 10,
        command_timeout_seconds: float | None = None,
    ) -> CommandResult:
        return self.runner.capture(
            [
                self.executable,
                "restart",
                "--time",
                str(timeout_seconds),
                container,
            ],
            check=True,
            timeout_seconds=command_timeout_seconds,
        )


def _nonblank_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line]


def _first_nonblank(lines: list[str]) -> str:
    for line in lines:
        if line:
            return line
    return ""
