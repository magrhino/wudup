"""Compose rewrite, rollback, and incident helpers for updater lifecycle execution."""

from __future__ import annotations

from collections.abc import Sequence

from . import compose_rewrite, updater_logging
from .command import CommandError
from .compose import ComposeStack, ServiceImage
from .updater_lifecycle_state import _StackUpdateState
from .updater_matching import _update_services
from .updater_models import (
    STALE_PENDING_DIGEST_REASON,
    AppliedDigestPinUpdate,
    AppliedDigestUnpinUpdate,
    AppliedTagUpdate,
    ComposeTagRewriteError,
    Match,
    StackStatus,
    UpResult,
)


class _LifecycleRewriteMixin:
    def _apply_compose_tag_updates(
        self,
        state: _StackUpdateState,
    ) -> StackStatus | None:
        if not state.compose_tag_updates:
            return None

        stack = state.stack
        self.log.info(f"[{stack.name}] Applying compose tag update(s)")
        status = self._ensure_compose_backup(state, "tag update")
        if status is not None:
            return status

        compose_path = stack.directory / stack.file
        try:
            state.applied_tags = compose_rewrite.apply_compose_tag_updates(
                compose_path,
                state.compose_tag_updates,
                tag_stream_updates=state.tag_stream_updates,
                stack_name=stack.name,
                written_hashes=state.compose_written_hashes,
                expected_source_hash=self._expected_compose_hash(state),
            )
        except ComposeTagRewriteError as exc:
            self.log.error(
                f"[{stack.name}] Could not safely rewrite compose image tag(s): {exc}"
            )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-tag-rewrite",
                reason="compose-tag-rewrite-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-tag-rewrite-failed")
        except OSError as exc:
            self.log.error(f"[{stack.name}] Could not rewrite compose image tag(s): {exc}")
            self._record_failure(
                stack,
                state.matches,
                phase="compose-tag-rewrite",
                reason="compose-tag-rewrite-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-tag-rewrite-failed")

        if not state.applied_tags:
            self.log.error(
                f"[{stack.name}] Could not rewrite compose image tag(s); leaving WUD entry pending for manual review."
            )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-tag-rewrite",
                reason="compose-tag-rewrite-failed",
                services=state.pull_services,
                note="No compose image lines were rewritten.",
            )
            return StackStatus("failure", "compose-tag-rewrite-failed")

        for applied in state.applied_tags:
            self.log.info(
                f"[{stack.name}] Compose tag updated: {applied.old_image} -> {applied.new_image}"
            )

        refreshed = self.runner._refresh_stack_images(state.current_stack)
        if refreshed is None:
            return self._handle_compose_rewrite_failure(
                state,
                "compose-refresh-failed",
                phase="compose-refresh",
            )
        if not self._validate_applied_tag_updates(
            stack,
            state.applied_tags,
            refreshed.service_images,
        ):
            return self._handle_compose_rewrite_failure(
                state,
                "compose-tag-validation-failed",
                phase="compose-tag-validation",
            )

        self._set_current_stack(state, refreshed)
        return None

    def _apply_compose_digest_pin_updates(
        self,
        state: _StackUpdateState,
    ) -> StackStatus | None:
        if not state.digest_pin_updates:
            return None

        stack = state.stack
        status = self._ensure_compose_backup(state, "digest-pin rewrite")
        if status is not None:
            return status

        compose_path = stack.directory / stack.file
        try:
            state.applied_digest_pins = compose_rewrite.apply_compose_digest_pins(
                compose_path,
                state.digest_pin_updates,
                label_rewrite_approvals=(
                    self.options.digest_pin_label_rewrite_approvals
                ),
                written_hashes=state.compose_written_hashes,
                tag_stream_updates=state.tag_stream_updates,
                stack_name=stack.name,
                expected_source_hash=self._expected_compose_hash(state),
            )
        except ComposeTagRewriteError as exc:
            self.log.error(
                f"[{stack.name}] Could not safely write digest-pinned compose image(s): {exc}"
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-pin-failed",
                    phase="compose-digest-pin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-pin",
                reason="compose-digest-pin-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-digest-pin-failed")
        except OSError as exc:
            self.log.error(
                f"[{stack.name}] Could not write digest-pinned compose image(s): {exc}"
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-pin-failed",
                    phase="compose-digest-pin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-pin",
                reason="compose-digest-pin-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-digest-pin-failed")

        if not state.applied_digest_pins:
            self.log.error(
                f"[{stack.name}] Could not write digest-pinned compose image(s); leaving WUD entry pending for manual review."
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-pin-failed",
                    phase="compose-digest-pin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-pin",
                reason="compose-digest-pin-failed",
                services=state.pull_services,
                note="No compose image lines were digest pinned.",
            )
            return StackStatus("failure", "compose-digest-pin-failed")

        for applied in state.applied_digest_pins:
            self.log.info(
                f"[{stack.name}] Compose digest pinned: "
                f"{applied.old_image} -> {applied.final_image} "
                f"(resolved-tag={applied.resolved_tag})"
            )

        refreshed = self.runner._refresh_stack_images(state.current_stack)
        if refreshed is None:
            return self._handle_compose_rewrite_failure(
                state,
                "compose-digest-pin-refresh-failed",
                phase="compose-digest-pin",
            )
        if not self._validate_applied_digest_pins(
            stack,
            state.applied_digest_pins,
            refreshed.service_images,
        ):
            return self._handle_compose_rewrite_failure(
                state,
                "compose-digest-pin-validation-failed",
                phase="compose-digest-pin",
            )

        self._set_current_stack(state, refreshed)
        state.after = self._image_state(state.images)
        return None

    def _apply_compose_digest_unpin_updates(
        self,
        state: _StackUpdateState,
    ) -> StackStatus | None:
        if not state.digest_unpin_updates:
            return None

        stack = state.stack
        status = self._ensure_compose_backup(state, "digest-unpin rewrite")
        if status is not None:
            return status

        compose_path = stack.directory / stack.file
        try:
            state.applied_digest_unpins = compose_rewrite.apply_compose_digest_unpins(
                compose_path,
                state.digest_unpin_updates,
                stack_name=stack.name,
                written_hashes=state.compose_written_hashes,
                expected_source_hash=self._expected_compose_hash(state),
            )
        except ComposeTagRewriteError as exc:
            self.log.error(
                f"[{stack.name}] Could not safely unpin digest-pinned compose image(s): {exc}"
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-unpin-failed",
                    phase="compose-digest-unpin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-unpin",
                reason="compose-digest-unpin-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-digest-unpin-failed")
        except OSError as exc:
            self.log.error(
                f"[{stack.name}] Could not unpin digest-pinned compose image(s): {exc}"
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-unpin-failed",
                    phase="compose-digest-unpin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-unpin",
                reason="compose-digest-unpin-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-digest-unpin-failed")

        if not state.applied_digest_unpins:
            self.log.error(
                f"[{stack.name}] Could not unpin digest-pinned compose image(s); "
                "leaving WUD entry pending for manual review."
            )
            if state.compose_rewrite_applied and state.compose_backup is not None:
                return self._handle_compose_rewrite_failure(
                    state,
                    "compose-digest-unpin-failed",
                    phase="compose-digest-unpin",
                )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-digest-unpin",
                reason="compose-digest-unpin-failed",
                services=state.pull_services,
                note="No compose image lines were digest unpinned.",
            )
            return StackStatus("failure", "compose-digest-unpin-failed")

        for applied in state.applied_digest_unpins:
            self.log.info(
                f"[{stack.name}] Compose digest unpinned: "
                f"{applied.old_image} -> {applied.tag_image} "
                f"(resolved-tag={applied.resolved_tag})"
            )

        refreshed = self.runner._refresh_stack_images(state.current_stack)
        if refreshed is None:
            return self._handle_compose_rewrite_failure(
                state,
                "compose-digest-unpin-refresh-failed",
                phase="compose-digest-unpin",
            )
        if not self._validate_applied_digest_unpins(
            stack,
            state.applied_digest_unpins,
            refreshed.service_images,
        ):
            return self._handle_compose_rewrite_failure(
                state,
                "compose-digest-unpin-validation-failed",
                phase="compose-digest-unpin",
            )

        self._set_current_stack(state, refreshed)
        return None

    def _ensure_compose_backup(
        self,
        state: _StackUpdateState,
        action: str,
    ) -> StackStatus | None:
        if state.compose_backup is not None:
            return None

        stack = state.stack
        compose_path = stack.directory / stack.file
        try:
            state.compose_backup = compose_rewrite._backup_compose(compose_path)
        except OSError as exc:
            self.log.error(
                f"[{stack.name}] Could not back up compose file before {action}: {exc}"
            )
            self._record_failure(
                stack,
                state.matches,
                phase="compose-backup",
                reason="compose-backup-failed",
                services=state.pull_services,
                note=str(exc),
            )
            return StackStatus("failure", "compose-backup-failed")
        return None

    @staticmethod
    def _expected_compose_hash(state: _StackUpdateState) -> str:
        if state.compose_written_hashes:
            return state.compose_written_hashes[-1]
        assert state.compose_backup is not None
        return compose_rewrite._compose_source_hash(state.compose_backup)

    def _handle_compose_rewrite_failure(
        self,
        state: _StackUpdateState,
        reason: str,
        *,
        phase: str,
        command_error: CommandError | None = None,
        failure_health: str | None = None,
        failure_matches: Sequence[Match] | None = None,
    ) -> StackStatus:
        if state.compose_backup is None:
            return StackStatus("failure", reason)
        return self._handle_tag_update_failure(
            state,
            reason,
            phase=phase,
            command_error=command_error,
            failure_health=failure_health,
            failure_matches=failure_matches,
        )

    @staticmethod
    def _set_current_stack(
        state: _StackUpdateState,
        stack: ComposeStack,
    ) -> None:
        state.current_stack = stack
        state.images = tuple(stack.images)

    def _handle_tag_update_failure(
        self,
        state: _StackUpdateState,
        reason: str,
        *,
        phase: str,
        command_error: CommandError | None = None,
        failure_health: str | None = None,
        failure_matches: Sequence[Match] | None = None,
    ) -> StackStatus:
        stack = state.stack
        matches = state.matches
        services = state.services
        compose_backup = state.compose_backup
        if compose_backup is None:
            return StackStatus("failure", reason)
        report_matches = matches if failure_matches is None else failure_matches
        report_services = (
            services
            if failure_matches is None
            else _update_services(failure_matches)
        )
        self._progress(
            _tag_update_failure_progress_phase(phase),
            "failure",
            _tag_update_failure_progress_message(stack.name, phase, reason),
            stack=stack.name,
            services=report_services,
            matches=report_matches,
        )
        if failure_health is None:
            failure_health = self._capture_health_details(stack, report_services)
        if state.compose_written_hashes:
            self.log.warning(f"[{stack.name}] Restoring compose file after failed tag update.")
        rollback_result = "rollback-failed-manual-review-required"
        rollback_error: CommandError | None = None
        try:
            if state.compose_written_hashes:
                compose_rewrite.restore_compose_backup(
                    compose_backup, stack.directory / stack.file,
                    expected_source_hash=state.compose_written_hashes[-1],
                )
            self.runner.stack_runtime_states_after.pop(stack.index, None)
            active_services = tuple(state.running_services)
            rollback_ok, rollback_error = self._restore_tag_update_services(
                stack,
                active_services,
                state.stopped_services,
                force_recreate=state.scope.force_recreate,
                no_deps=state.scope.up_no_deps,
            )
            if rollback_ok:
                self.runner.stack_runtime_states_after[stack.index] = (
                    tuple(active_services),
                    tuple(state.stopped_services),
                )
                rollback_result = (
                    "restored-and-healthy"
                    if active_services
                    else "restored-and-stopped"
                )
                if reason == STALE_PENDING_DIGEST_REASON:
                    self.log.warning(
                        f"[{stack.name}] Rolled back to previous tag after a stale "
                        "WUD digest was detected; refresh WUD before retrying."
                    )
                else:
                    self.log.warning(
                        f"[{stack.name}] Rolled back to previous tag; leaving "
                        "WUD entry pending for manual review."
                    )
            else:
                self.log.error(f"[{stack.name}] Rollback failed; manual review required.")
        except (OSError, ComposeTagRewriteError) as exc:
            self.log.error(f"[{stack.name}] Could not safely restore Compose: {exc}")
            self.log.error(f"[{stack.name}] Rollback failed; manual review required.")

        report_error = rollback_error or command_error
        note = f"tag rollback={rollback_result}"
        if reason == STALE_PENDING_DIGEST_REASON:
            note += "; stale pending digest detected"
        self._record_failure(
            stack,
            report_matches,
            phase=phase,
            reason=reason,
            services=report_services,
            command_error=report_error,
            health_details=failure_health,
            note=note,
        )
        self._write_tag_incident_log(
            stack,
            services,
            state.applied_tags,
            reason,
            rollback_result,
            failure_health,
        )
        return StackStatus("failure", reason)

    def _restore_tag_update_services(
        self,
        stack: ComposeStack,
        active_services: Sequence[str],
        stopped_services: Sequence[str],
        *,
        force_recreate: bool,
        no_deps: bool,
    ) -> tuple[bool, CommandError | None]:
        stopped_result = UpResult(True, False)
        if stopped_services:
            stopped_result = self._run_compose_up_no_start(
                stack,
                stopped_services,
                force_recreate=force_recreate,
            )
        rollback_up = UpResult(True, False)
        if active_services:
            rollback_up = self._run_compose_up(
                stack,
                active_services,
                force_recreate=force_recreate,
                no_deps=no_deps or bool(stopped_services),
            )
        if stopped_result.ok and rollback_up.ok and stopped_services:
            stopped_result = self._verify_services_stopped(
                stack,
                stopped_services,
            )
        rollback_ok = stopped_result.ok and rollback_up.ok
        if rollback_ok and active_services:
            rollback_ok = rollback_up.wait_handled or self._wait_for_health(
                stack,
                active_services,
            )
        return rollback_ok, stopped_result.command_error or rollback_up.command_error

    def _write_tag_incident_log(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        applied_tags: Sequence[AppliedTagUpdate],
        reason: str,
        rollback_result: str,
        failure_health: str,
    ) -> None:
        first_tag = applied_tags[0].desired_tag if applied_tags else "tag"
        incident = (
            stack.directory
            / f"error-{updater_logging.safe_component(first_tag)}-{updater_logging.file_timestamp()}.logs"
        )
        services_label = " ".join(services or ()) or "stack-level"
        content = [
            "WUDup tag update incident\n",
            f"timestamp={updater_logging.timestamp()}\n",
            f"stack={stack.name}\n",
            f"compose_file={stack.file}\n",
            f"services={services_label}\n",
            f"reason={reason}\n",
            f"rollback={rollback_result}\n",
            f"central_log={self.log_file}\n",
            "\ntag_updates:\n",
        ]
        for applied in applied_tags:
            content.append(
                f"  {applied.old_image} -> {applied.new_image} "
                f"(tag={applied.desired_tag} replacements={applied.replacements})\n"
            )
        content.append("\nfailure_health:\n")
        content.append(
            failure_health
            if failure_health
            else "health: no failure health details captured\n"
        )
        content.append(
            "\nmanual_review_required="
            + (
                "no\n"
                if rollback_result in {"restored-and-healthy", "restored-and-stopped"}
                else "yes\n"
            )
        )
        try:
            incident = updater_logging._create_unique_text_file_exclusive(
                incident,
                "".join(content),
                owner=self.owner,
            )
        except OSError as exc:
            self.log.warning(
                f"[{stack.name}] Could not create tag update incident log "
                f"{incident} with owner={self.owner}: {exc}"
            )
            return
        self.log.warning(f"[{stack.name}] Wrote tag update incident log: {incident}")

    def _validate_applied_tag_updates(
        self,
        stack: ComposeStack,
        applied_tags: Sequence[AppliedTagUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        ok = True
        image_by_service = {
            (item.service, item.image)
            for item in service_images
        }
        for applied in applied_tags:
            if not applied.services:
                continue
            expected_replacements = len(applied.services)
            if applied.replacements != expected_replacements:
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose tag rewrite touched "
                    f"{applied.replacements} image line(s) for {applied.old_image}, "
                    f"expected {expected_replacements}."
                )
            for service in applied.services:
                if (service, applied.new_image) in image_by_service:
                    continue
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose service {service} did not resolve "
                    f"to rewritten image {applied.new_image} after tag rewrite."
                )
        return ok

    def _validate_applied_digest_pins(
        self,
        stack: ComposeStack,
        applied_pins: Sequence[AppliedDigestPinUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        ok = True
        image_by_service = {(item.service, item.image) for item in service_images}
        for applied in applied_pins:
            if not applied.services:
                continue
            expected_replacements = len(applied.services)
            if applied.replacements != expected_replacements:
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose digest-pin rewrite touched "
                    f"{applied.replacements} image line(s) for {applied.old_image}, "
                    f"expected {expected_replacements}."
                )
            for service in applied.services:
                if (service, applied.final_image) in image_by_service:
                    continue
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose service {service} did not resolve "
                    f"to digest-pinned image {applied.final_image}."
                )
        return ok

    def _validate_applied_digest_unpins(
        self,
        stack: ComposeStack,
        applied_unpins: Sequence[AppliedDigestUnpinUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        ok = True
        image_by_service = {(item.service, item.image) for item in service_images}
        for applied in applied_unpins:
            if not applied.services:
                continue
            expected_replacements = len(applied.services)
            if applied.replacements != expected_replacements:
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose digest-unpin rewrite touched "
                    f"{applied.replacements} image line(s) for {applied.old_image}, "
                    f"expected {expected_replacements}."
                )
            for service in applied.services:
                if (service, applied.tag_image) in image_by_service:
                    continue
                ok = False
                self.log.error(
                    f"[{stack.name}] Compose service {service} did not resolve "
                    f"to unpinned image {applied.tag_image}."
                )
        return ok


def _tag_update_failure_progress_phase(phase: str) -> str:
    if phase in {"pull", "digest", "compose-digest-pin", "compose-digest-unpin"}:
        return "pull"
    if phase in {"up", "stop", "down", "unpause"}:
        return "recreate"
    if phase == "health":
        return "health"
    return "preflight"


def _tag_update_failure_progress_message(stack_name: str, phase: str, reason: str) -> str:
    if phase == "pull":
        return f"[{stack_name}] Pull failed after tag rewrite."
    if phase == "digest":
        return (
            f"[{stack_name}] Pulled image did not reach the expected digest after tag rewrite."
        )
    if phase == "compose-digest-pin":
        return f"[{stack_name}] Compose digest-pin rewrite failed after pull."
    if phase == "compose-digest-unpin":
        return f"[{stack_name}] Compose digest-unpin rewrite failed before pull."
    if phase in {"up", "stop", "down", "unpause"}:
        return f"[{stack_name}] Compose {phase} failed after tag rewrite."
    if phase == "health":
        return f"[{stack_name}] Health wait failed after tag rewrite."
    return f"[{stack_name}] Tag update failed before pull: {reason}."
