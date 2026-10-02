"""Container health and image-state helpers for updater lifecycle execution."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from . import updater_logging
from .command import CommandError, CommandResult
from .compose import ComposeStack
from .images import image_repo_ref
from .updater_models import ImageState, Match, UpResult

CONTAINER_SUMMARY_FORMAT = "{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.ExitCode}}"
HEALTH_LOG_FORMAT = "{{if .State.Health}}{{range .State.Health.Log}}{{println .Output}}{{end}}{{end}}"


@dataclass(frozen=True)
class _ContainerImageCheck:
    """Result of comparing service containers with the pulled images."""

    behind: dict[str, tuple[str, str]]
    """Service -> (Compose image, image ID the container still uses)."""
    failed_start: tuple[str, ...]
    """Stopped services whose last container start attempt failed."""
    unverified: tuple[str, ...]
    error: CommandError | None = None


class _LifecycleHealthMixin:
    def _run_compose_up_no_start(
        self,
        stack: ComposeStack,
        services: Sequence[str],
        *,
        force_recreate: bool = False,
    ) -> UpResult:
        try:
            self.compose.up(
                stack.directory,
                stack.file,
                services,
                force_recreate=force_recreate,
                no_deps=True,
                no_start=True,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            self.log.error(
                f"[{stack.name}] Could not recreate stopped service(s) without "
                "starting them"
            )
            return UpResult(False, False, exc)

        return self._verify_services_stopped(stack, services)

    def _verify_services_stopped(
        self,
        stack: ComposeStack,
        services: Sequence[str],
    ) -> UpResult:
        try:
            running_cids = self.compose.ps_quiet_checked(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            self.log.error(
                f"[{stack.name}] Could not verify that recreated service(s) remained stopped"
            )
            self._restore_services_stopped(stack, services)
            return UpResult(False, False, exc)
        if running_cids:
            self.log.error(
                f"[{stack.name}] Recreated service(s) unexpectedly started: "
                f"{' '.join(services)}"
            )
            self._restore_services_stopped(stack, services)
            return UpResult(False, False)
        return UpResult(True, False)

    def _restore_services_stopped(
        self,
        stack: ComposeStack,
        services: Sequence[str],
    ) -> None:
        try:
            self.compose.stop(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            self.log.error(
                f"[{stack.name}] Could not restore unexpectedly started service(s) "
                f"to stopped: {' '.join(services)} ({exc})"
            )

    def _run_compose_up(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        *,
        force_recreate: bool = False,
        no_deps: bool = True,
    ) -> UpResult:
        if self.options.mode != "pause" and self.compose.up_wait_supported(
            stack.directory,
            stack.file,
            project_directory=stack.project_directory,
        ):
            self.log.info(
                f"[{stack.name}] docker compose up --wait is supported; using native wait"
            )
            try:
                self.compose.up(
                    stack.directory,
                    stack.file,
                    services,
                    wait=True,
                    wait_timeout=self.options.max_wait,
                    force_recreate=force_recreate,
                    no_deps=no_deps,
                    project_directory=stack.project_directory,
                )
                return UpResult(True, True)
            except CommandError as exc:
                self.log.error(f"[{stack.name}] docker compose up --wait failed")
                health_details = self._capture_health_details(stack, services)
                self._log_health_details(stack, services, health_details)
                return UpResult(False, True, exc, health_details)

        try:
            self.compose.up(
                stack.directory,
                stack.file,
                services,
                force_recreate=force_recreate,
                no_deps=no_deps,
                project_directory=stack.project_directory,
            )
            return UpResult(True, False)
        except CommandError as exc:
            self.log.error(f"[{stack.name}] docker compose up failed")
            health_details = self._capture_health_details(stack, services)
            self._log_health_details(stack, services, health_details)
            return UpResult(False, False, exc, health_details)

    def _wait_for_health(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        matches: Sequence[Match] = (),
    ) -> bool:
        start = time.monotonic()
        self._progress(
            "health",
            "running",
            f"[{stack.name}] Waiting up to {self.options.max_wait}s for health.",
            stack=stack.name,
            services=services,
            matches=matches,
        )
        if self.options.max_wait > 0:
            time.sleep(2)

        while True:
            cids = self.compose.ps_quiet(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
            ok = bool(cids)
            for cid in cids:
                summary = self._cid_summary(cid)
                if not summary or not _cid_is_ok(summary):
                    ok = False

            elapsed = int(time.monotonic() - start)
            if ok:
                self.log.plain("INFO", f"[{stack.name}] Health wait succeeded in {elapsed}s")
                self._progress(
                    "health",
                    "success",
                    f"[{stack.name}] Health wait succeeded in {elapsed}s.",
                    stack=stack.name,
                    services=services,
                    matches=matches,
                )
                return True
            if elapsed >= self.options.max_wait:
                self.log.error(f"[{stack.name}] Failed health gate after {elapsed}s")
                if not cids:
                    self.log.plain(
                        "ERROR",
                        f"[{stack.name}] Health blocker: docker compose ps -q returned no containers",
                    )
                self._log_health_details(stack, services)
                self._progress(
                    "health",
                    "failure",
                    f"[{stack.name}] Failed health gate after {elapsed}s.",
                    stack=stack.name,
                    services=services,
                    matches=matches,
                )
                return False
            time.sleep(2)

    def _capture_health_details(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
    ) -> str:
        cids = self.compose.ps_quiet(
            stack.directory,
            stack.file,
            services,
            project_directory=stack.project_directory,
        )
        if not cids:
            return "health: docker compose ps -q returned no containers\n"

        lines: list[str] = []
        for cid in cids:
            summary = self._cid_summary(cid)
            if not summary:
                lines.append(f"health: container={cid} inspect returned no state")
                continue
            name, status, health, restarts, exit_code = _split_summary(summary)
            lines.append(
                f"health: container={name.lstrip('/')} status={status} "
                f"health={health} restarts={restarts} exit_code={exit_code}"
            )
            for output in self.docker.try_inspect(cid, HEALTH_LOG_FORMAT):
                output = updater_logging.sanitize_stream(output)
                if output:
                    lines.append(f"health_output[{name.lstrip('/')}]: {output}")
        return "\n".join(lines) + "\n"

    def _log_health_details(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        health_details: str | None = None,
    ) -> None:
        details = health_details
        if details is None:
            details = self._capture_health_details(stack, services)
        for line in details.splitlines():
            self.log.plain("ERROR", f"[{stack.name}] {line}")

    def _log_command_result(self, result: CommandResult) -> None:
        for line in updater_logging._render_command_result(result):
            self.log.plain("ERROR", line.rstrip("\n"))

    def _cid_summary(self, cid: str) -> str:
        lines = self.docker.try_inspect(cid, CONTAINER_SUMMARY_FORMAT)
        return lines[0] if lines else ""

    def _image_state(self, images: Iterable[str]) -> dict[str, ImageState]:
        return {
            image: ImageState(
                image_id=self.docker.image_id(image),
                digest=self.docker.image_digest(image),
            )
            for image in images
            if image
        }

    def _check_container_images(
        self,
        stack: ComposeStack,
        services: Sequence[str],
        stopped_services: Sequence[str],
        after: Mapping[str, ImageState],
    ) -> _ContainerImageCheck:
        """Compare each service's containers with the image just pulled.

        An earlier run can pull a same-tag image and then fail to recreate or
        start the container, so the local tag alone cannot show that the
        update applied. Anything that cannot be read is reported as unverified
        instead of being treated as current.
        """
        pulled_ids = {
            item.service: (item.image, after[item.image].image_id)
            for item in stack.service_images
            if item.image in after and after[item.image].image_id
        }
        stopped = set(stopped_services)
        behind: dict[str, tuple[str, str]] = {}
        failed_start: list[str] = []
        unverified: list[str] = []
        error: CommandError | None = None
        for service in services:
            image, pulled_id = pulled_ids.get(service, ("", ""))
            if not pulled_id:
                continue
            try:
                container_ids = self.compose.ps_quiet_checked(
                    stack.directory,
                    stack.file,
                    (service,),
                    project_directory=stack.project_directory,
                    all_containers=True,
                )
                for container_id in container_ids:
                    container_image_id = self.docker.container_image_id(container_id)
                    if not container_image_id:
                        unverified.append(service)
                        break
                    if container_image_id != pulled_id:
                        behind.setdefault(service, (image, container_image_id))
                    if (
                        service in stopped
                        and service not in failed_start
                        and self.docker.container_state_error(container_id)
                    ):
                        failed_start.append(service)
            except CommandError as exc:
                unverified.append(service)
                error = error or exc
        return _ContainerImageCheck(
            behind=behind,
            failed_start=tuple(failed_start),
            unverified=tuple(unverified),
            error=error,
        )


def _updated_images(
    before: Mapping[str, ImageState],
    after: Mapping[str, ImageState],
) -> list[tuple[str, ImageState]]:
    changes: list[tuple[str, ImageState]] = []
    after_by_image_id: dict[str, tuple[str, ImageState] | None] = {}
    after_by_repository: dict[str, tuple[str, ImageState] | None] = {}
    for image, state in after.items():
        if state.image_id:
            after_by_image_id[state.image_id] = (
                None
                if state.image_id in after_by_image_id
                else (image, state)
            )
        repository = image_repo_ref(image)
        after_by_repository[repository] = (
            None
            if repository in after_by_repository
            else (image, state)
        )

    for image, old in before.items():
        new_image = image
        new = after.get(image)
        if new is None and old.image_id:
            image_id_match = after_by_image_id.get(old.image_id)
            if image_id_match is not None:
                new_image, new = image_id_match
        if new is None:
            repository_match = after_by_repository.get(image_repo_ref(image))
            if repository_match is not None:
                new_image, new = repository_match
        if (
            new is not None
            and new.image_id
            and (old.image_id != new.image_id or image != new_image)
        ):
            changes.append((image, new))
    return changes


def _cid_is_ok(summary: str) -> bool:
    _name, status, health, _restarts, _exit_code = _split_summary(summary)
    if health != "none":
        return health == "healthy"
    return status == "running"


def _split_summary(summary: str) -> tuple[str, str, str, str, str]:
    parts = summary.split("|", 4)
    while len(parts) < 5:
        parts.append("")
    return tuple(parts[:5])  # type: ignore[return-value]
