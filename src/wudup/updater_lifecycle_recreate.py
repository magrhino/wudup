"""Recreate and health-finish helpers for updater lifecycle execution."""

from __future__ import annotations

from .command import CommandError
from .updater_lifecycle_state import _StackUpdateState, _StopResult
from .updater_models import StackStatus, UpResult


class _LifecycleRecreateMixin:
    def _recreate_and_verify_stack(self, state: _StackUpdateState) -> StackStatus:
        stack = state.stack
        self.runner.stack_runtime_states_after.pop(stack.index, None)
        self._progress(
            "recreate",
            "running",
            f"[{stack.name}] Recreating selected containers.",
            stack=stack.name,
            services=state.services,
            matches=state.matches,
        )

        self._log_compose_up_scope(state)
        if state.stopped_services:
            stopped_result = self._run_compose_up_no_start(
                stack,
                state.stopped_services,
                force_recreate=state.scope.force_recreate,
            )
            if not stopped_result.ok:
                return self._handle_compose_up_failure(
                    state,
                    _StopResult(),
                    stopped_result,
                    prepared=False,
                )

        stop_result = self._prepare_for_recreate(state)
        up_result = UpResult(True, False)
        if state.running_services:
            up_result = self._run_compose_up(
                stack,
                state.running_services,
                force_recreate=state.scope.force_recreate,
                no_deps=state.scope.up_no_deps or bool(state.stopped_services),
            )
            if not up_result.ok:
                return self._handle_compose_up_failure(state, stop_result, up_result)

        if state.stopped_services:
            preserved = self._verify_services_stopped(
                stack,
                state.stopped_services,
            )
            if not preserved.ok:
                return self._handle_compose_up_failure(
                    state,
                    stop_result,
                    preserved,
                    restart_stopped=False,
                )

        self._progress(
            "recreate",
            "success",
            f"[{stack.name}] Containers were recreated with their prior running state.",
            stack=stack.name,
            services=state.services,
            matches=state.matches,
        )

        unpaused = self._unpause_after_recreate(state, up_result)
        if isinstance(unpaused, StackStatus):
            return unpaused
        return self._finish_health_after_recreate(state, stop_result, unpaused)

    def _prepare_for_recreate(self, state: _StackUpdateState) -> _StopResult:
        stack = state.stack
        stop_services = state.running_stop_services or state.running_services
        if not stop_services:
            return _StopResult()
        if self.options.mode == "pause":
            self.log.warning(
                f"[{stack.name}] Mode pause is deprecated; pausing before recreate and unpausing before health check"
            )
            try:
                self.compose.pause(
                    stack.directory,
                    stack.file,
                    stop_services,
                    project_directory=stack.project_directory,
                )
            except CommandError:
                self.log.warning(f"[{stack.name}] Pause failed; continuing with live recreate")
        elif self.options.mode == "stop":
            try:
                if state.service_scoped:
                    self.log.warning(
                        f"[{stack.name}] Stopping affected running service(s): "
                        f"{' '.join(stop_services)}"
                    )
                else:
                    self.log.warning(
                        f"[{stack.name}] Stopping running stack service(s): "
                        f"{' '.join(stop_services)}"
                    )
                self.compose.stop(
                    stack.directory,
                    stack.file,
                    stop_services,
                    project_directory=stack.project_directory,
                )
            except CommandError as exc:
                self.log.warning(
                    f"[{stack.name}] Stop failed; attempting up for recovery, but this stack will not be marked successful"
                )
                return _StopResult(True, exc)
        return _StopResult()

    def _log_compose_up_scope(self, state: _StackUpdateState) -> None:
        if state.stopped_services:
            self.log.info(
                f"[{state.stack.name}] Recreating stopped service(s) without "
                f"starting them: {' '.join(state.stopped_services)}"
            )
        if state.running_services and state.service_scoped:
            self.log.info(
                f"[{state.stack.name}] Bringing affected running service(s) up: "
                f"{' '.join(state.running_services)}"
            )
        elif state.running_services:
            self.log.info(
                f"[{state.stack.name}] Bringing running stack service(s) up: "
                f"{' '.join(state.running_services)}"
            )

    def _handle_compose_up_failure(
        self,
        state: _StackUpdateState,
        stop_result: _StopResult,
        up_result: UpResult,
        *,
        prepared: bool = True,
        restart_stopped: bool = True,
    ) -> StackStatus:
        stack = state.stack
        unpaused = (
            self._unpause_after_recreate(state, up_result) if prepared else up_result
        )
        if isinstance(unpaused, StackStatus):
            return unpaused
        up_result = unpaused

        if state.compose_rewrite_applied and state.compose_backup is not None:
            failure_phase = "up"
            failure_reason = "up-or-health-failed"
            failure_error = up_result.command_error
            if stop_result.failed and stop_result.error is not None:
                failure_phase = stop_result.phase
                failure_reason = "down-failed"
                failure_error = stop_result.error
            return self._handle_compose_rewrite_failure(
                state,
                failure_reason,
                phase=failure_phase,
                command_error=failure_error,
                failure_health=up_result.health_details,
            )

        restart_note = (
            self._restart_services_stopped_for_update(state)
            if prepared and restart_stopped
            else ""
        )
        if stop_result.failed and stop_result.error is not None:
            self._record_failure(
                stack,
                state.matches,
                phase=stop_result.phase,
                reason="down-failed",
                services=state.services,
                command_error=stop_result.error,
                health_details=self._capture_health_details(stack, state.services),
                note="Update recovery also failed during compose up.",
            )

        self._record_failure(
            stack,
            state.matches,
            phase="up",
            reason="up-or-health-failed",
            services=state.services,
            command_error=up_result.command_error,
            health_details=up_result.health_details,
            note=restart_note,
        )
        self._progress(
            "recreate",
            "failure",
            f"[{stack.name}] Compose up failed.",
            stack=stack.name,
            services=state.services,
            matches=state.matches,
        )
        return StackStatus("failure", "up-or-health-failed")

    def _restart_services_stopped_for_update(self, state: _StackUpdateState) -> str:
        """Start services that stop mode stopped before a failed recreate.

        This is the only recovery attempt: leaving them stopped would make the
        next run treat them as intentionally stopped and never start them
        again. If it fails, the run stays failed, the pending entry is kept,
        and the operator gets the exact command to start them by hand.
        """
        if self.options.mode != "stop":
            return ""
        stack = state.stack
        services = state.running_stop_services or state.running_services
        if not services:
            return ""
        label = " ".join(services)
        try:
            self.compose.start(
                stack.directory,
                stack.file,
                services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            command = self.compose.display_command(
                stack.directory,
                stack.file,
                services,
                "start",
                project_directory=stack.project_directory,
            )
            message = (
                f"Service(s) {label} were stopped for the update, and starting "
                "them again after the failed update also failed, so they are "
                "still stopped and not running the new image. Fix the error "
                f"above, then start them with: {command}. The pending update "
                "was kept, so rerunning the update after that is safe and "
                "retries it."
            )
            self.log.error(f"[{stack.name}] {message} ({exc})")
            return message
        message = (
            f"Service(s) {label} were stopped for the update and were started "
            "again after it failed."
        )
        self.log.warning(f"[{stack.name}] {message}")
        return message

    def _unpause_after_recreate(
        self,
        state: _StackUpdateState,
        up_result: UpResult,
    ) -> UpResult | StackStatus:
        stack = state.stack
        if self.options.mode != "pause" or not state.running_services:
            return up_result

        self.log.warning(f"[{stack.name}] Unpausing before health check")
        try:
            self.compose.unpause(
                stack.directory,
                stack.file,
                state.running_services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "unpause-failed",
                    phase="unpause",
                    command_error=exc,
                )
            self._record_failure(
                stack,
                state.matches,
                phase="unpause",
                reason="unpause-failed",
                services=state.services,
                command_error=exc,
                health_details=self._capture_health_details(stack, state.services),
            )
            return StackStatus("failure", "unpause-failed")
        return UpResult(
            up_result.ok,
            False,
            up_result.command_error,
            up_result.health_details,
        )

    def _finish_health_after_recreate(
        self,
        state: _StackUpdateState,
        stop_result: _StopResult,
        up_result: UpResult,
    ) -> StackStatus:
        stack = state.stack
        if not state.running_services:
            self._progress(
                "health",
                "skipped",
                f"[{stack.name}] Stopped services remained stopped; health wait was skipped.",
                stack=stack.name,
                services=state.stopped_services,
                matches=state.matches,
            )
            self.log.info(f"[{stack.name}] Updated without starting stopped services")
            self._remember_applied_digest_changes(state)
            return StackStatus("success", "updated")

        if up_result.wait_handled:
            self._progress(
                "health",
                "success",
                f"[{stack.name}] Compose reported healthy containers.",
                stack=stack.name,
                services=state.running_services,
                matches=state.matches,
            )

        if up_result.wait_handled or self._wait_for_health(
            stack,
            state.running_services,
            state.matches,
        ):
            self.log.info(f"[{stack.name}] Healthy")
            if stop_result.failed:
                if state.compose_rewrite_applied and state.compose_backup is not None:
                    return self._handle_compose_rewrite_failure(
                        state,
                        "down-failed",
                        phase=stop_result.phase,
                        command_error=stop_result.error,
                    )
                self._record_failure(
                    stack,
                    state.matches,
                    phase=stop_result.phase,
                    reason="down-failed",
                    services=state.services,
                    command_error=stop_result.error,
                    health_details=self._capture_health_details(stack, state.services),
                    note="Compose up recovery succeeded, but the earlier stop command failed.",
                )
                self._progress(
                    "recreate",
                    "failure",
                    f"[{stack.name}] Stop failed before recreate.",
                    stack=stack.name,
                    services=state.services,
                    matches=state.matches,
                )
                return StackStatus("failure", "down-failed")

            # Record the verified state so history shows services whose
            # earlier start failed as running after this update.
            self.runner.stack_runtime_states_after[stack.index] = (
                state.running_services,
                state.stopped_services,
            )
            self._remember_applied_digest_changes(state)
            return StackStatus("success", "updated")

        health_details = self._capture_health_details(stack, state.running_services)
        if state.compose_rewrite_applied and state.compose_backup is not None:
            return self._handle_compose_rewrite_failure(
                state,
                "health-failed",
                phase="health",
                failure_health=health_details,
            )
        self._record_failure(
            stack,
            state.matches,
            phase="health",
            reason="health-failed",
            services=state.services,
            health_details=health_details,
        )
        self._progress(
            "health",
            "failure",
            f"[{stack.name}] Health wait failed.",
            stack=stack.name,
            services=state.services,
            matches=state.matches,
        )
        return StackStatus("failure", "health-failed")

    def _remember_applied_digest_changes(self, state: _StackUpdateState) -> None:
        if state.digest_pin_updates:
            self._remember_applied_digest_pins(
                state.stack,
                state.matches,
                state.digest_pin_updates,
            )
        if state.digest_unpin_updates:
            self._remember_applied_digest_unpins(
                state.stack,
                state.matches,
                state.digest_unpin_updates,
            )
