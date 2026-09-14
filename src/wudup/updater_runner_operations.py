"""Runner operation delegates for ``update-from-wud``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from . import updater_preflight, updater_tag_exclusions
from .command import CommandError, CommandResult
from .compose import (
    ComposeBindMount,
    ComposeRuntimePortIssue,
    ComposeStack,
    ServiceImage,
)
from .digest_verifier import DigestCheckResult, DigestResolveResult
from .images import normalize_digest
from .updater_digest_pin import _digest_pin_match_tag
from .updater_lifecycle_state import _StackUpdateState
from .updater_matching import _target_image_for_match, _update_services
from .updater_models import (
    AppliedDigestPinUpdate,
    AppliedDigestUnpinUpdate,
    AppliedTagUpdate,
    DigestPinCandidate,
    DigestPinUpdate,
    DigestRequirementKey,
    DigestUnpinUpdate,
    FailureRecord,
    ImageState,
    Match,
    StackStatus,
    TagExclusionUpdate,
    TagUpdate,
    UpdateScope,
    UpResult,
)
from .wud_file import ParsedWudFile, WudTarget

_SERVICES_UNSET = object()


class _RunnerOperationsMixin:
    def _build_tag_exclusion_matches(
        self,
        parsed: ParsedWudFile,
        stacks: Sequence[ComposeStack],
    ) -> tuple[list[Match], list[tuple[WudTarget, str]]]:
        return updater_tag_exclusions.build_tag_exclusion_matches(
            self,
            parsed,
            stacks,
        )

    def _plan_tag_exclusions(
        self,
        matches: Sequence[Match],
        stacks: Sequence[ComposeStack],
    ) -> tuple[list[TagExclusionUpdate], list[tuple[WudTarget, str]]]:
        return updater_tag_exclusions.plan_tag_exclusions(self, matches, stacks)

    def _tag_exclusion_repo_updates(
        self,
        stacks: Sequence[ComposeStack],
        *,
        image_repo: str,
        tag: str,
        source_line: int,
    ) -> list[TagExclusionUpdate]:
        return updater_tag_exclusions.tag_exclusion_repo_updates(
            stacks,
            image_repo=image_repo,
            tag=tag,
            source_line=source_line,
        )

    def _can_apply_tag_exclusions(
        self,
        updates: Sequence[TagExclusionUpdate],
    ) -> bool:
        return updater_tag_exclusions.can_apply_tag_exclusions(self, updates)

    def _apply_tag_exclusions(
        self,
        updates: Sequence[TagExclusionUpdate],
    ) -> dict[tuple[int, int], StackStatus]:
        return updater_tag_exclusions.apply_tag_exclusions(self, updates)

    def _existing_exact_tag_exclusions(
        self,
        updates: Sequence[TagExclusionUpdate],
    ) -> dict[str, set[str]]:
        return updater_tag_exclusions.existing_exact_tag_exclusions(self, updates)

    def _record_tag_exclusion_rules(
        self,
        updates: Sequence[TagExclusionUpdate],
    ) -> None:
        updater_tag_exclusions.record_tag_exclusion_rules(self, updates)

    def _recreate_tag_exclusion_services(
        self,
        updates: Sequence[TagExclusionUpdate],
        statuses: dict[tuple[int, int], StackStatus],
    ) -> None:
        updater_tag_exclusions.recreate_tag_exclusion_services(
            self,
            updates,
            statuses,
        )

    def _mark_tag_exclusions_pending(
        self,
        updates: Sequence[TagExclusionUpdate],
    ) -> None:
        updater_tag_exclusions.mark_tag_exclusions_pending(self, updates)

    def _mark_successful_tag_exclusions(
        self,
        updates: Sequence[TagExclusionUpdate],
        statuses: Mapping[tuple[int, int], StackStatus],
    ) -> None:
        updater_tag_exclusions.mark_successful_tag_exclusions(self, updates, statuses)

    def _mark_tag_exclusion_failures(
        self,
        failures: Sequence[tuple[WudTarget, str]],
    ) -> None:
        updater_tag_exclusions.mark_tag_exclusion_failures(self, failures)

    def _update_stack(self, stack: ComposeStack, matches: Sequence[Match]) -> StackStatus:
        return self.lifecycle._update_stack(stack, matches)

    def _run_compose_up(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        *,
        force_recreate: bool = False,
        no_deps: bool = True,
    ) -> UpResult:
        return self.lifecycle._run_compose_up(
            stack,
            services,
            force_recreate=force_recreate,
            no_deps=no_deps,
        )

    def _wait_for_health(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        matches: Sequence[Match] = (),
    ) -> bool:
        return self.lifecycle._wait_for_health(stack, services, matches)

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
        return self.lifecycle._handle_tag_update_failure(
            state,
            reason,
            phase=phase,
            command_error=command_error,
            failure_health=failure_health,
            failure_matches=failure_matches,
        )

    def _write_tag_incident_log(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        applied_tags: Sequence[AppliedTagUpdate],
        reason: str,
        rollback_result: str,
        failure_health: str,
    ) -> None:
        self.lifecycle._write_tag_incident_log(
            stack,
            services,
            applied_tags,
            reason,
            rollback_result,
            failure_health,
        )

    def _update_scope(self, stack: ComposeStack, matches: Sequence[Match]) -> UpdateScope:
        return self.lifecycle._update_scope(stack, matches)

    def _missing_network_mode_providers(
        self,
        stack: ComposeStack,
        services: Sequence[str],
        providers: Mapping[str, str],
    ) -> tuple[str, ...]:
        return self.lifecycle._missing_network_mode_providers(stack, services, providers)

    def _stack_stop_services(self, stack: ComposeStack) -> tuple[str, ...] | None:
        return self.lifecycle._stack_stop_services(stack)

    def _stack_recreate_label_cid(
        self,
        stack: ComposeStack,
        services: Sequence[str],
    ) -> str:
        return self.lifecycle._stack_recreate_label_cid(stack, services)

    def _capture_health_details(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
    ) -> str:
        return self.lifecycle._capture_health_details(stack, services)

    def _log_health_details(
        self,
        stack: ComposeStack,
        services: Sequence[str] | None,
        health_details: str | None = None,
    ) -> None:
        self.lifecycle._log_health_details(stack, services, health_details)

    def _log_command_result(self, result: CommandResult) -> None:
        self.lifecycle._log_command_result(result)

    def _cid_summary(self, cid: str) -> str:
        return self.lifecycle._cid_summary(cid)

    def _image_state(self, images: Iterable[str]) -> dict[str, ImageState]:
        return self.lifecycle._image_state(images)

    def _validate_tag_manifests(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_tag_manifests(self, matches)

    def _validate_tag_update_plan(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_tag_update_plan(self, matches)

    def _validate_compose_bind_mount_paths(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_compose_bind_mount_paths(self, matches)

    def _validate_compose_runtime_ports(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_compose_runtime_ports(self, matches)

    def _compose_runtime_port_issue_message(
        self,
        stack: ComposeStack,
        issue: ComposeRuntimePortIssue,
    ) -> str:
        return updater_preflight.compose_runtime_port_issue_message(stack, issue)

    def _bind_mount_path_issue_messages(
        self,
        stack: ComposeStack,
        mount: ComposeBindMount,
        issue: str,
    ) -> list[str]:
        return updater_preflight.bind_mount_path_issue_messages(
            self,
            stack,
            mount,
            issue,
        )

    def _log_bind_mount_path_issue(self, messages: Sequence[str]) -> None:
        updater_preflight.log_bind_mount_path_issue(self, messages)

    def _log_preflight_issue(self, message: str) -> None:
        updater_preflight.log_preflight_issue(self, message)

    def _validate_applied_tag_updates(
        self,
        stack: ComposeStack,
        applied_tags: Sequence[AppliedTagUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        return self.lifecycle._validate_applied_tag_updates(
            stack,
            applied_tags,
            service_images,
        )

    def _validate_applied_digest_pins(
        self,
        stack: ComposeStack,
        applied_pins: Sequence[AppliedDigestPinUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        return self.lifecycle._validate_applied_digest_pins(
            stack,
            applied_pins,
            service_images,
        )

    def _validate_applied_digest_unpins(
        self,
        stack: ComposeStack,
        applied_unpins: Sequence[AppliedDigestUnpinUpdate],
        service_images: Sequence[ServiceImage],
    ) -> bool:
        return self.lifecycle._validate_applied_digest_unpins(
            stack,
            applied_unpins,
            service_images,
        )

    def _verify_expected_digests(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
    ) -> bool:
        return self.lifecycle._verify_expected_digests(stack, matches)

    def _verify_digest_pin_updates(
        self,
        stack: ComposeStack,
        updates: Sequence[DigestPinUpdate],
        images: Sequence[str],
    ) -> bool:
        return self.lifecycle._verify_digest_pin_updates(stack, updates, images)

    def _log_digest_untrusted(
        self,
        stack_name: str,
        result: DigestCheckResult,
    ) -> None:
        self.lifecycle._log_digest_untrusted(stack_name, result)

    def _log_digest_mismatch(
        self,
        stack_name: str,
        result: DigestCheckResult,
    ) -> None:
        self.lifecycle._log_digest_mismatch(stack_name, result)

    def _log_digest_details(
        self,
        level: str,
        stack_name: str,
        result: DigestCheckResult,
    ) -> None:
        self.lifecycle._log_digest_details(level, stack_name, result)

    def _tag_updates(self, matches: Sequence[Match]) -> tuple[TagUpdate, ...]:
        return self.lifecycle._tag_updates(matches)

    def _digest_pin_updates(
        self,
        matches: Sequence[Match],
    ) -> tuple[DigestPinUpdate, ...]:
        return self.lifecycle._digest_pin_updates(matches)

    def _digest_unpin_updates(
        self,
        matches: Sequence[Match],
    ) -> tuple[DigestUnpinUpdate, ...]:
        return self.lifecycle._digest_unpin_updates(matches)

    def _resolve_digest_pin(self, image: str) -> DigestResolveResult:
        return self.lifecycle._resolve_digest_pin(image)

    def _resolve_digest_pin_candidate(
        self,
        candidate: DigestPinCandidate,
    ) -> DigestResolveResult:
        return self.lifecycle._resolve_digest_pin_candidate(candidate)

    def _verify_digest_pin_update_target(
        self,
        update: DigestPinUpdate,
    ) -> DigestResolveResult:
        return self.lifecycle._verify_digest_pin_update_target(update)

    def _validate_digest_pin_plan(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_digest_pin_plan(self, matches)

    def _validate_digest_unpin_plan(self, matches: Sequence[Match]) -> bool:
        return updater_preflight.validate_digest_unpin_plan(self, matches)

    def _refresh_stack_images(self, stack: ComposeStack) -> ComposeStack | None:
        return self.lifecycle._refresh_stack_images(stack)

    def _record_failure(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
        *,
        phase: str,
        reason: str,
        services: Sequence[str] | None | object = _SERVICES_UNSET,
        command_error: CommandError | None = None,
        health_details: str = "",
        note: str = "",
    ) -> None:
        if services is _SERVICES_UNSET:
            failure_services = _update_services(matches)
        elif services is None:
            failure_services = None
        else:
            failure_services = tuple(services)
        self.failures.append(
            FailureRecord(
                stack=stack,
                services=failure_services,
                matches=tuple(matches),
                phase=phase,
                reason=reason,
                command_result=command_error.result if command_error else None,
                health_details=health_details,
                note=note,
            )
        )

    def _finish_preflight_failure(
        self,
        parsed: ParsedWudFile,
        matches: Sequence[Match],
        skipped_tags: Sequence[WudTarget],
    ) -> int:
        return updater_preflight.finish_preflight_failure(
            self,
            parsed,
            matches,
            skipped_tags,
        )

    def _remember_applied_digest_pins(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
        updates: Sequence[DigestPinUpdate],
    ) -> None:
        by_key = {
            (update.old_image, update.resolved_tag): update
            for update in updates
        }
        for match in matches:
            resolved_tag = _digest_pin_match_tag(match)
            if not resolved_tag:
                continue
            update = by_key.get((match.compose_image, resolved_tag))
            if update is None:
                continue
            self.applied_digest_pins[
                (stack.index, match.target.line_no, match.service)
            ] = update

    def _remember_applied_digest_unpins(
        self,
        stack: ComposeStack,
        matches: Sequence[Match],
        updates: Sequence[DigestUnpinUpdate],
    ) -> None:
        by_key = {
            (update.old_image, update.tag_image, update.target_digest): update
            for update in updates
        }
        for match in matches:
            update = by_key.get(
                (
                    match.compose_image,
                    match.resolved,
                    normalize_digest(match.target.digest),
                )
            )
            if update is None:
                continue
            self.applied_digest_unpins[
                (stack.index, match.target.line_no, match.service)
            ] = update

    def _target_image_for_match(self, match: Match) -> str:
        unpin = self.applied_digest_unpins.get(
            (match.stack.index, match.target.line_no, match.service)
        )
        if unpin is not None:
            return unpin.tag_image
        update = self.applied_digest_pins.get(
            (match.stack.index, match.target.line_no, match.service)
        )
        if update is not None:
            return update.final_image
        return _target_image_for_match(match)

    def _stale_pending_digest_line_numbers(
        self,
        matches: Sequence[Match],
        line_numbers: Iterable[int],
    ) -> set[int]:
        candidates = set(line_numbers)
        requirements_by_line: dict[int, set[DigestRequirementKey]] = {}
        for match in matches:
            if not match.target.digest or "@sha256:" not in match.target.first:
                continue
            line_no = match.target.line_no
            requirements_by_line.setdefault(line_no, set()).add(
                (
                    match.stack.index,
                    line_no,
                    self.lifecycle._expected_digest_image(match),
                )
            )

        stale_lines: set[int] = set()
        for line_no in candidates:
            requirements = requirements_by_line.get(line_no, set())
            if (
                requirements
                and requirements.issubset(
                    self.preflight_digest_outcomes.stale
                )
            ) or (
                requirements
                and requirements.issubset(
                    self.expected_digest_outcomes.stale
                )
            ):
                stale_lines.add(line_no)
        return stale_lines

    def _preflight_expected_digest_outcome(self, match: Match) -> str:
        return self.lifecycle._preflight_expected_digest_outcome(match)

    def _expected_digest_outcome(self, match: Match) -> str:
        return self.lifecycle._expected_digest_outcome(match)

    def _expected_digest_failed_in_stack(self, stack: ComposeStack) -> bool:
        return self.lifecycle._expected_digest_failed_in_stack(stack)

    def _mark_failed_lines_restored(self, failed_lines: Iterable[int]) -> None:
        restored = set(failed_lines)
        for failure in self.failures:
            failure_lines = {match.target.line_no for match in failure.matches}
            failure.wud_restored = bool(failure_lines & restored)
