"""Docker CLI subprocess layer for the Python updater."""

from __future__ import annotations

from dataclasses import dataclass

from .command import CommandError, CommandResult, CommandRunner

DEFAULT_CONTAINER_FORMAT = "{{.Names}}\t{{.Image}}"
IMAGE_ID_FORMAT = "{{.Id}}"
IMAGE_DIGESTS_FORMAT = "{{range .RepoDigests}}{{println .}}{{end}}"
CONTAINER_ID_FORMAT = "{{.Id}}"
CONTAINER_IMAGE_ID_FORMAT = "{{.Image}}"


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

    def manifest_inspect_verbose(self, image: str) -> CommandResult:
        return self.runner.capture(
            [self.executable, "manifest", "inspect", "--verbose", image],
            check=True,
        )

    def pull_image(self, image: str) -> CommandResult:
        return self.runner.run([self.executable, "pull", image], check=True)

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

    def try_container_image_id(self, container: str) -> str:
        return _first_nonblank(
            self.try_inspect(container, CONTAINER_IMAGE_ID_FORMAT)
        )

    def restart_container(
        self,
        container: str,
        *,
        timeout_seconds: int = 10,
    ) -> CommandResult:
        return self.runner.run(
            [
                self.executable,
                "restart",
                "--time",
                str(timeout_seconds),
                container,
            ],
            check=True,
        )


def _nonblank_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line]


def _first_nonblank(lines: list[str]) -> str:
    for line in lines:
        if line:
            return line
    return ""
