"""Stack lifecycle execution for ``update-from-wud``."""

# Compatibility imports deliberately re-export these names unchanged.

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .command import CommandError
from .compose import (
    COMPOSE_RUNTIME_STATE_FORMAT,
    ComposeRuntimeServiceState,
    ComposeStack,
    compose_runtime_service_key,
    compose_runtime_service_key_matches,
    compose_runtime_service_states,
)
from .updater_digest_pin import _digest_pin_tag_materialization_updates
from .updater_lifecycle_digest import _LifecycleDigestMixin
from .updater_lifecycle_health import (
    CONTAINER_SUMMARY_FORMAT as CONTAINER_SUMMARY_FORMAT,
)
from .updater_lifecycle_health import (
    HEALTH_LOG_FORMAT as HEALTH_LOG_FORMAT,
)
from .updater_lifecycle_health import (
    _cid_is_ok as _cid_is_ok,
)
from .updater_lifecycle_health import (
    _LifecycleHealthMixin,
)
from .updater_lifecycle_health import (
    _split_summary as _split_summary,
)
from .updater_lifecycle_health import (
    _updated_images as _updated_images,
)
from .updater_lifecycle_recreate import _LifecycleRecreateMixin
from .updater_lifecycle_rewrite import (
    _LifecycleRewriteMixin,
)
from .updater_lifecycle_rewrite import (
    _tag_update_failure_progress_message as _tag_update_failure_progress_message,
)
from .updater_lifecycle_rewrite import (
    _tag_update_failure_progress_phase as _tag_update_failure_progress_phase,
)
from .updater_lifecycle_scope import (
    _stack_level_scope_message as _stack_level_scope_message,
)
from .updater_lifecycle_scope import (
    _UpdateScopeMixin,
    runtime_services_for_scope,
)
from .updater_lifecycle_state import _StackUpdateState
from .updater_models import (
    STALE_PENDING_DIGEST_REASON,
    Match,
    StackStatus,
    UpdaterError,
    UpdateScope,
)

INACTIVE_CONTAINER_STATES = frozenset({"created", "dead", "exited"})


class StackLifecycleExecutor(
    _LifecycleRewriteMixin,
    _LifecycleRecreateMixin,
    _LifecycleHealthMixin,
    _UpdateScopeMixin,
    _LifecycleDigestMixin,
):
    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runner, name)

    def _update_stack(self, stack: ComposeStack, matches: Sequence[Match]) -> StackStatus:
        state_or_status = self._build_stack_update_state(stack, matches)
        if isinstance(state_or_status, StackStatus):
            return state_or_status

        state = state_or_status
        for step in (
            self._apply_compose_tag_updates,
            self._apply_compose_digest_unpin_updates,
            self._pull_and_verify_images,
            self._apply_compose_digest_pin_updates,
            self._finish_pull_phase,
        ):
            status = step(state)
            if status is not None:
                return status

        return self._recreate_and_verify_stack(state)

    def _preflight_stack_expected_digests(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
    ) -> StackStatus | None:
        if self._preflight_expected_digests(stack, matches):
            return None
        for outcome in ("stale", "failed"):
            failed_matches = tuple(
                match
                for match in matches
                if self._preflight_expected_digest_outcome(match) == outcome
            )
            if not failed_matches:
                continue
            integrity_failed = outcome == "failed"
            reason = (
                "manifest-integrity-mismatch" if integrity_failed
                else STALE_PENDING_DIGEST_REASON
            )
            note = (
                "Registry manifest integrity check failed before any Compose or "
                "Docker update; check the registry before retrying."
                if integrity_failed else
                "Stale pending digest detected before any Compose or Docker "
                "update; refresh or replace it before retrying."
            )
            scope = self._update_scope(stack, failed_matches)
            self._record_failure(
                stack,
                failed_matches,
                phase="preflight",
                reason=reason,
                services=scope.pull_services,
                note=note,
            )
            self._progress(
                "preflight",
                "failure",
                f"[{stack.name}] {note}" if integrity_failed else
                f"[{stack.name}] Pending WUD digest is stale; no update was applied.",
                stack=stack.name,
                services=scope.pull_services,
                matches=failed_matches,
            )
        return StackStatus(
            "failure",
            "manifest-integrity-mismatch"
            if self.preflight_digest_outcomes.failed_in_stack(stack.index)
            else STALE_PENDING_DIGEST_REASON,
        )

    def _build_stack_update_state(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
    ) -> _StackUpdateState | StackStatus:
        scope = self._update_scope(stack, matches)
        self._log_stack_scope(stack, scope)

        runtime_state = self._initial_service_runtime(stack, scope, matches)
        if isinstance(runtime_state, StackStatus):
            return runtime_state
        running_services, stopped_services = runtime_state

        images = tuple(stack.images)
        before = self._image_state(images)
        tag_updates = self._tag_updates(matches)
        stack_directory = str(stack.directory.resolve(strict=False))
        tag_stream_updates = tuple(
            update
            for update in self.options.tag_stream_updates
            if update.stack_directory == stack_directory
            and update.compose_file == stack.file
        )
        selected_lines = {match.target.line_no for match in matches}
        unexpected_stream_lines = sorted(
            {
                update.line_no
                for update in tag_stream_updates
                if update.line_no not in selected_lines
            }
        )
        if unexpected_stream_lines:
            line_list = ", ".join(str(line_no) for line_no in unexpected_stream_lines)
            self.log.error(
                f"[{stack.name}] Tag stream update line(s) are not selected: {line_list}"
            )
            self._record_failure(
                stack,
                matches,
                phase="compose-tag-rewrite",
                reason="compose-tag-stream-plan-stale",
                services=scope.pull_services,
                note=f"Unselected tag stream line(s): {line_list}",
            )
            return StackStatus("failure", "compose-tag-stream-plan-stale")
        try:
            digest_pin_updates = self._digest_pin_updates(matches)
            digest_unpin_updates = self._digest_unpin_updates(matches)
        except UpdaterError as exc:
            self.log.error(f"[{stack.name}] {exc}")
            self._record_failure(
                stack,
                matches,
                phase="compose-digest-pin",
                reason="compose-digest-pin-plan-failed",
                services=scope.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-digest-pin-plan-failed")

        digest_pin_tag_updates = _digest_pin_tag_materialization_updates(
            digest_pin_updates
        )
        compose_tag_updates = (*tag_updates, *digest_pin_tag_updates)
        return _StackUpdateState(
            stack=stack,
            matches=matches,
            scope=scope,
            current_stack=stack,
            images=images,
            before=before,
            after=dict(before),
            digest_pin_updates=digest_pin_updates,
            digest_unpin_updates=digest_unpin_updates,
            compose_tag_updates=compose_tag_updates,
            tag_stream_updates=tag_stream_updates,
            running_services=running_services,
            stopped_services=stopped_services,
        )

    def _initial_service_runtime(
        self,
        stack: ComposeStack,
        scope: UpdateScope,
        matches: Sequence[Match],
    ) -> tuple[tuple[str, ...], tuple[str, ...]] | StackStatus:
        services = runtime_services_for_scope(scope)
        if not services:
            self.log.error(
                f"[{stack.name}] Could not determine which Compose services are "
                "selected; the update was not applied."
            )
            self._record_failure(
                stack,
                matches,
                phase="preflight",
                reason="runtime-state-unavailable",
                services=services,
                note="Compose service discovery returned no services.",
            )
            self._progress(
                "preflight",
                "failure",
                f"[{stack.name}] Selected Compose services could not be determined.",
                stack=stack.name,
                services=services,
                matches=matches,
            )
            return StackStatus("failure", "runtime-state-unavailable")
        running: list[str] = []
        stopped: list[str] = []
        try:
            if not stack.project_name:
                raise ValueError("Compose project identity is unavailable.")
            runtime_states = compose_runtime_service_states(
                self.docker.ps_format(
                    COMPOSE_RUNTIME_STATE_FORMAT,
                    all_containers=True,
                )
            )
            for service in services:
                service_states = self._compose_service_runtime_states(
                    stack,
                    service,
                    runtime_states,
                )
                state_values = set(service_states)
                if len(service_states) > 1 or (
                    state_values
                    and state_values != {"running"}
                    and not state_values <= INACTIVE_CONTAINER_STATES
                ):
                    self.log.error(
                        f"[{stack.name}] Compose service {service} has scaled or "
                        "unverified container state; the update was not applied."
                    )
                    self._record_failure(
                        stack,
                        matches,
                        phase="preflight",
                        reason="runtime-state-unavailable",
                        services=(service,),
                        note=(
                            f"Compose service {service} observed "
                            f"{len(service_states)} replica(s); runtime states: "
                            f"{', '.join(sorted(state_values))}."
                        ),
                    )
                    self._progress(
                        "preflight",
                        "failure",
                        f"[{stack.name}] Service {service} replica state cannot be "
                        "safely preserved.",
                        stack=stack.name,
                        services=(service,),
                        matches=matches,
                    )
                    return StackStatus("failure", "runtime-state-unavailable")
                (running if state_values == {"running"} else stopped).append(service)
        except (CommandError, ValueError) as exc:
            self.log.error(
                f"[{stack.name}] Could not verify whether selected services are running; "
                "the update was not applied."
            )
            self._record_failure(
                stack,
                matches,
                phase="preflight",
                reason="runtime-state-unavailable",
                services=services,
                command_error=exc if isinstance(exc, CommandError) else None,
                note=str(exc) if isinstance(exc, ValueError) else "",
            )
            self._progress(
                "preflight",
                "failure",
                f"[{stack.name}] Selected service runtime state could not be verified.",
                stack=stack.name,
                services=services,
                matches=matches,
            )
            return StackStatus("failure", "runtime-state-unavailable")

        runtime_state = tuple(running), tuple(stopped)
        self.runner.stack_runtime_states[stack.index] = runtime_state
        self.runner.stack_runtime_states_after[stack.index] = runtime_state
        if stopped:
            self.log.warning(
                f"[{stack.name}] Selected service(s) already stopped and will remain "
                f"stopped: {' '.join(stopped)}"
            )
        return runtime_state

    @staticmethod
    def _compose_service_runtime_states(
        stack: ComposeStack,
        service: str,
        runtime_states: Sequence[ComposeRuntimeServiceState],
    ) -> tuple[str, ...]:
        expected = compose_runtime_service_key(
            stack.project_directory or stack.directory,
            stack.file,
            stack.project_name,
            service,
        )
        return tuple(
            state
            for key, state in runtime_states
            if compose_runtime_service_key_matches(expected, key)
        )

    def _log_stack_scope(self, stack: ComposeStack, scope: UpdateScope) -> None:
        opts = self.options
        services = scope.services
        pull_services = scope.pull_services
        service_scoped = services is not None
        services_label = " ".join(services or ())
        pull_services_label = " ".join(pull_services or ())

        if self.log.rich_enabled():
            self.log.plain("INFO", f"[{stack.name}] Checking for updates (mode={opts.mode})")
            panel_lines = [(f"Checking for updates (mode={opts.mode})", "info")]
            if service_scoped:
                message = f"Matched compose service(s): {services_label}"
                self.log.plain("INFO", f"[{stack.name}] {message}")
                panel_lines.append((message, "info"))
            else:
                message = _stack_level_scope_message(scope)
                self.log.plain("WARN", f"[{stack.name}] {message}")
                panel_lines.append((message, "warning"))
                if pull_services is not None:
                    pull_message = f"Pulling matched compose service(s): {pull_services_label}"
                    self.log.plain("INFO", f"[{stack.name}] {pull_message}")
                    panel_lines.append((pull_message, "info"))
            self.log.renderer.stack_summary(stack.name, panel_lines)
        else:
            self.log.info(f"[{stack.name}] Checking for updates (mode={opts.mode})")
            if service_scoped:
                self.log.info(f"[{stack.name}] Matched compose service(s): {services_label}")
            else:
                self.log.warning(f"[{stack.name}] {_stack_level_scope_message(scope)}")
                if pull_services is not None:
                    self.log.info(
                        f"[{stack.name}] Pulling matched compose service(s): {pull_services_label}"
                    )

    def _pull_and_verify_images(
        self,
        state: _StackUpdateState,
    ) -> StackStatus | None:
        stack = state.stack
        matches = state.matches

        self._progress(
            "pull",
            "running",
            f"[{stack.name}] Pulling selected image updates.",
            stack=stack.name,
            services=state.pull_services,
            matches=state.matches,
        )
        try:
            self.compose.pull(
                stack.directory,
                stack.file,
                state.pull_services,
                project_directory=stack.project_directory,
            )
        except CommandError as exc:
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "pull-failed",
                    phase="pull",
                    command_error=exc,
                )
            self._record_failure(
                stack,
                state.matches,
                phase="pull",
                reason="pull-failed",
                services=state.pull_services,
                command_error=exc,
                health_details=self._capture_health_details(stack, state.pull_services),
            )
            self._progress(
                "pull",
                "failure",
                f"[{stack.name}] Pull failed.",
                stack=stack.name,
                services=state.pull_services,
                matches=state.matches,
            )
            return StackStatus("failure", "pull-failed")

        state.after = self._image_state(state.images)
        self.runner.stack_image_states[state.stack.index] = (
            dict(state.before),
            dict(state.after),
        )
        if not self._verify_expected_digests(stack, matches):
            reason = self._expected_digest_failure_reason(matches)
            failed_matches = tuple(
                match
                for match in matches
                if self._expected_digest_outcome(match) in {"failed", "stale"}
            )
            failure_scope = self._update_scope(stack, failed_matches)
            note = (
                "Stale pending digest detected; refresh or replace it before "
                "retrying."
                if reason == STALE_PENDING_DIGEST_REASON
                else ""
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    reason,
                    phase="digest",
                    failure_matches=failed_matches,
                )
            self._record_failure(
                stack,
                failed_matches,
                phase="digest",
                reason=reason,
                services=failure_scope.pull_services,
                health_details=self._capture_health_details(
                    stack,
                    failure_scope.pull_services,
                ),
                note=note,
            )
            self._progress(
                "pull",
                "failure",
                f"[{stack.name}] Pulled images did not reach the expected digest.",
                stack=stack.name,
                services=failure_scope.pull_services,
                matches=failed_matches,
            )
            return StackStatus("failure", reason)

        if state.digest_pin_updates and not self._verify_digest_pin_updates(
            stack,
            state.digest_pin_updates,
            state.images,
        ):
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "digest-pin-verification-failed",
                    phase="digest",
                )
            self._progress(
                "pull",
                "failure",
                f"[{stack.name}] Pulled images failed digest pin verification.",
                stack=stack.name,
                services=state.pull_services,
                matches=state.matches,
            )
            self._record_failure(
                stack,
                state.matches,
                phase="digest",
                reason="digest-pin-verification-failed",
                services=state.pull_services,
                health_details=self._capture_health_details(stack, state.pull_services),
            )
            return StackStatus("failure", "digest-pin-verification-failed")

        return None

    def _finish_pull_phase(self, state: _StackUpdateState) -> StackStatus | None:
        stack = state.stack
        self.runner.stack_image_states[state.stack.index] = (
            dict(state.before),
            dict(state.after),
        )
        self._progress(
            "pull",
            "success",
            f"[{stack.name}] Images pulled and verified.",
            stack=stack.name,
            services=state.pull_services,
            matches=state.matches,
        )

        changes = _updated_images(state.before, state.after)
        update_needed = bool(
            state.applied_tags
            or state.applied_digest_pins
            or state.applied_digest_unpins
            or changes
        )
        for image, image_state in changes:
            target = image_state.digest if image_state.digest else image_state.image_id
            self.log.info(f"[{stack.name}] Image updated: {image} -> {target}")

        if not update_needed:
            self.log.info(f"[{stack.name}] All images up to date, skipping restart")
            self._progress(
                "recreate",
                "skipped",
                f"[{stack.name}] Images are already current; recreate was skipped.",
                stack=stack.name,
                services=state.services,
                matches=state.matches,
            )
            self._progress(
                "health",
                "skipped",
                f"[{stack.name}] Health wait was skipped because no containers changed.",
                stack=stack.name,
                services=state.services,
                matches=state.matches,
            )
            return StackStatus("success", "already-current")

        return None
