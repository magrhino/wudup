"""Preflight validation helpers for ``update-from-wud``."""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import compose_rewrite, updater_audit, updater_logging
from .command import CommandError
from .compose import ComposeBindMount, ComposeRuntimePortIssue, ComposeStack
from .container_identity import (
    DockerEndpoint,
    container_serves_docker_endpoint,
    docker_endpoint,
    network_mode_container,
    resolve_docker_endpoint,
)
from .images import image_tag
from .self_update import is_self_update_target
from .updater_digest_pin import _digest_pin_match_tag
from .updater_lifecycle_scope import runtime_services_for_scope
from .updater_matching import (
    _preflight_status_reason,
    _stacks_to_update,
    _update_services,
)
from .updater_models import (
    ComposeTagRewriteError,
    Match,
    StackStatus,
    UpdaterError,
)
from .updater_planning import _container_bind_mount_path_issue
from .wud_file import ParsedWudFile, WudTarget


@dataclass
class _PreflightIssueRecords:
    messages_by_stack: dict[int, list[str]] = field(default_factory=dict)
    services_by_stack: dict[int, set[str]] = field(default_factory=dict)

    def add(
        self,
        stack: ComposeStack,
        service: str,
        messages: Sequence[str],
    ) -> None:
        self.messages_by_stack.setdefault(stack.index, []).extend(messages)
        self.services_by_stack.setdefault(stack.index, set()).add(service)

    def messages_for(self, stack: ComposeStack) -> list[str] | None:
        return self.messages_by_stack.get(stack.index)

    def services_for(self, stack: ComposeStack) -> tuple[str, ...] | None:
        return tuple(sorted(self.services_by_stack.get(stack.index, ()))) or None


_ComposePreflightValidator = Callable[
    [Any, ComposeStack, Sequence[Match], _PreflightIssueRecords],
    bool,
]


_SELF_UPDATE_BLOCKED = "self-update-recreate-blocked"
_DOCKER_ENDPOINT_BLOCKED = "docker-endpoint-recreate-blocked"
_SELF_UPDATE_MESSAGE = (
    "This update would stop or recreate WUDup itself. "
    "Use the self-update action for WUDup, or update this stack from "
    "the host. Remove it from this selection to update other services."
)
# Bound on following network_mode container:<name> links before failing closed.
_NETWORK_MODE_HOP_LIMIT = 5
_DOCKER_ENDPOINT_LOOKUP_MESSAGE = (
    "Could not check whether this update would stop the Docker endpoint "
    "WUDup uses (DOCKER_HOST); no update was applied. Check Docker access "
    "and the Compose configuration, then retry."
)


def validate_self_update_scope(runner: Any, matches: Sequence[Match]) -> bool:
    """Block stacks whose recreate scope includes WUDup or its Docker endpoint."""

    protect_self = runner.options.protected_container is not None
    endpoint = docker_endpoint(runner.environ)
    if not protect_self and endpoint is None:
        return True

    @functools.cache
    def resolved_endpoint() -> DockerEndpoint:
        # A slow resolver only delays runs that have a container to check.
        if endpoint is None:
            # Raised inside the guard's lookup handler, so the stack is blocked.
            raise ValueError("DOCKER_HOST is not a network endpoint")
        return resolve_docker_endpoint(endpoint)

    protected_id = ""
    identity_error = ""
    if runner.options.protected_container:
        try:
            protected_id = runner.docker.container_id(runner.options.protected_container)
        except CommandError:
            pass
        if not protected_id:
            identity_error = (
                "Could not verify the WUDup container identity; no update was applied. "
                "Check Docker access and WUD_WEB_RESTART_CONTAINER, then retry."
            )
    records_by_reason = {
        _SELF_UPDATE_BLOCKED: _PreflightIssueRecords(),
        _DOCKER_ENDPOINT_BLOCKED: _PreflightIssueRecords(),
    }
    stacks = _stacks_to_update(matches)
    for stack in stacks:
        stack_matches = _matches_for_stack(matches, stack)
        message = identity_error
        reason = _SELF_UPDATE_BLOCKED
        protected_services: set[str] = set()
        scope = None
        if not message:
            try:
                scope = runner.lifecycle._update_scope(stack, stack_matches, strict=True)
            except (CommandError, ValueError):
                message = (
                    "Could not verify the update's recreate scope; no update was applied. "
                    "Check Docker access and the Compose configuration, then retry."
                )
                if not protect_self:
                    reason = _DOCKER_ENDPOINT_BLOCKED
        if scope is not None:
            services = set(runtime_services_for_scope(scope))
            if protect_self:
                protected_services = {
                    item.service for item in stack.service_images
                    if item.service in services and is_self_update_target(item.image)
                }
            container_ids: list[str] = []
            if protected_id or endpoint is not None:
                try:
                    container_ids = runner.compose.ps_quiet_checked(
                        stack.directory, stack.file, tuple(sorted(services)),
                        project_directory=stack.project_directory,
                    )
                except CommandError:
                    if protected_id:
                        message = (
                            "Could not verify whether this update includes the WUDup "
                            "container; no update was applied. Check Docker access and "
                            "the Compose configuration, then retry."
                        )
                    else:
                        message = _DOCKER_ENDPOINT_LOOKUP_MESSAGE
                        reason = _DOCKER_ENDPOINT_BLOCKED
            if protected_id and protected_id in container_ids:
                protected_services.update(services)
            if endpoint is not None and not message and not protected_services:
                try:
                    endpoint_services = _docker_endpoint_services(
                        runner.docker, container_ids, resolved_endpoint,
                    )
                except (CommandError, TypeError, ValueError):
                    message = _DOCKER_ENDPOINT_LOOKUP_MESSAGE
                    reason = _DOCKER_ENDPOINT_BLOCKED
                else:
                    if endpoint_services:
                        protected_services = endpoint_services
                        reason = _DOCKER_ENDPOINT_BLOCKED
                        message = _docker_endpoint_message(endpoint.host, endpoint_services)
        if not message and not protected_services:
            runner.lifecycle.verified_update_scopes[(stack.index, _update_services(stack_matches))] = scope
            continue
        message = f"[{stack.name}] " + (message or _SELF_UPDATE_MESSAGE)
        runner.log.error(message)
        runner._progress(
            "preflight", "failure", message, stack=stack.name,
            services=tuple(sorted(protected_services)), matches=stack_matches,
        )
        records = records_by_reason[reason]
        records.messages_by_stack[stack.index] = [message]
        records.services_by_stack[stack.index] = protected_services
    for stack in stacks:
        for reason, records in records_by_reason.items():
            _record_compose_preflight_failures(runner, matches, (stack,), records, reason=reason)
    return not any(records.messages_by_stack for records in records_by_reason.values())


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


def validate_tag_manifests(runner: Any, matches: Sequence[Match]) -> bool:
    ok = True
    for update in runner._tag_updates(matches):
        try:
            runner.docker.manifest_inspect(update.new_image)
        except CommandError as exc:
            ok = False
            runner.log.error(
                "Invalid or unavailable remote tag: "
                f"{update.old_image} -> {update.new_image}"
            )
            for line in exc.result.stderr_lines:
                runner.log.error(
                    f"manifest stderr: {updater_logging.sanitize_stream(line)}"
                )
            runner._log_command_result(exc.result)
        else:
            runner.log.info(
                "Validated remote tag: "
                f"{update.old_image} -> {update.new_image}"
            )
    return ok


def validate_tag_update_plan(runner: Any, matches: Sequence[Match]) -> bool:
    ok = True
    desired_by_service: dict[tuple[int, str, str], set[str]] = {}
    for match in matches:
        if not match.target.desired_tag:
            continue
        if not match.service:
            ok = False
            runner.log.error(
                f"[{match.stack.name}] Tag update for {match.compose_image} "
                "cannot be safely rewritten because the compose service image "
                "could not be mapped."
            )
            continue
        key = (match.stack.index, match.service, match.compose_image)
        desired_by_service.setdefault(key, set()).add(match.target.desired_tag)

    for stack_index, service, image in sorted(desired_by_service):
        desired = desired_by_service[(stack_index, service, image)]
        if len(desired) <= 1:
            continue
        ok = False
        stack_name = next(
            (
                match.stack.name
                for match in matches
                if match.stack.index == stack_index
            ),
            str(stack_index),
        )
        runner.log.error(
            f"[{stack_name}] Conflicting tag updates for service {service} "
            f"image {image}: {', '.join(sorted(desired))}"
        )

    available_stream_targets = {
        (
            match.target.line_no,
            match.stack.name,
            str(match.stack.directory.resolve(strict=False)),
            match.stack.file,
            match.service,
            image_tag(match.compose_image),
            match.target.desired_tag,
        )
        for match in matches
    }
    for update in runner.options.tag_stream_updates:
        if update not in runner.matched_tag_stream_updates:
            ok = False
            runner.log.error(
                f"[{update.stack}] Tag stream plan for {update.compose_file} "
                f"service {update.service} line {update.line_no} is stale."
            )
            continue
        target = (
            update.line_no,
            update.stack,
            update.stack_directory,
            update.compose_file,
            update.service,
            update.current_tag,
            update.selected_tag,
        )
        if target in available_stream_targets:
            continue
        ok = False
        runner.log.error(
            f"[{update.stack}] Tag stream plan for {update.compose_file} "
            f"service {update.service} line {update.line_no} is stale."
        )
    return ok


def validate_compose_bind_mount_paths(
    runner: Any,
    matches: Sequence[Match],
) -> bool:
    return _validate_compose_preflight(
        runner,
        matches,
        reason="bind-mount-path-invalid",
        validate_stack=_validate_stack_bind_mount_paths,
    )


def _validate_compose_preflight(
    runner: Any,
    matches: Sequence[Match],
    *,
    reason: str,
    validate_stack: _ComposePreflightValidator,
) -> bool:
    ok = True
    stacks = _stacks_to_update(matches)
    records = _PreflightIssueRecords()
    for stack in stacks:
        stack_matches = _matches_for_stack(matches, stack)
        if validate_stack(runner, stack, stack_matches, records):
            continue
        ok = False
    _record_compose_preflight_failures(
        runner,
        matches,
        stacks,
        records,
        reason=reason,
    )
    return ok


def _validate_stack_bind_mount_paths(
    runner: Any,
    stack: ComposeStack,
    stack_matches: Sequence[Match],
    records: _PreflightIssueRecords,
) -> bool:
    stack_ok = True
    mounts = runner.compose.try_service_bind_mounts(
        stack.directory,
        stack.file,
        project_directory=stack.project_directory,
    )
    if not mounts:
        return stack_ok

    scoped_services = _scoped_preflight_services(
        runner,
        stack,
        stack_matches,
        (mount.service for mount in mounts),
    )
    for mount in mounts:
        if mount.service not in scoped_services:
            continue
        issue = _container_bind_mount_path_issue(
            mount,
            docker_base=runner.options.docker_base,
        )
        if not issue:
            continue
        stack_ok = False
        messages = runner._bind_mount_path_issue_messages(stack, mount, issue)
        runner._log_bind_mount_path_issue(messages)
        if runner.options.dry_run:
            continue
        records.add(stack, mount.service, messages)
    return stack_ok


def _record_compose_preflight_failures(
    runner: Any,
    matches: Sequence[Match],
    stacks: Sequence[ComposeStack],
    records: _PreflightIssueRecords,
    *,
    reason: str,
) -> None:
    if runner.options.dry_run:
        return
    for stack in stacks:
        messages = records.messages_for(stack)
        if not messages:
            continue
        runner._record_failure(
            stack,
            _matches_for_stack(matches, stack),
            phase="preflight",
            reason=reason,
            services=records.services_for(stack),
            health_details="\n".join(messages),
        )


def _matches_for_stack(
    matches: Sequence[Match],
    stack: ComposeStack,
) -> list[Match]:
    return [match for match in matches if match.stack.index == stack.index]


def _scoped_preflight_services(
    runner: Any,
    stack: ComposeStack,
    stack_matches: Sequence[Match],
    service_names: Iterable[str],
) -> set[str]:
    scope = runner._update_scope(stack, stack_matches)
    if scope.services is None:
        return set(service_names)
    return set(scope.services)


def validate_compose_runtime_ports(
    runner: Any,
    matches: Sequence[Match],
) -> bool:
    return _validate_compose_preflight(
        runner,
        matches,
        reason="compose-port-invalid",
        validate_stack=_validate_stack_runtime_ports,
    )


def _validate_stack_runtime_ports(
    runner: Any,
    stack: ComposeStack,
    stack_matches: Sequence[Match],
    records: _PreflightIssueRecords,
) -> bool:
    stack_ok = True
    issues = runner.compose.try_service_runtime_port_issues(
        stack.directory,
        stack.file,
        project_directory=stack.project_directory,
    )
    if not issues:
        return stack_ok

    scoped_services = _scoped_preflight_services(
        runner,
        stack,
        stack_matches,
        (issue.service for issue in issues),
    )
    for issue in issues:
        if issue.service not in scoped_services:
            continue
        stack_ok = False
        message = runner._compose_runtime_port_issue_message(stack, issue)
        runner._log_preflight_issue(message)
        if runner.options.dry_run:
            continue
        records.add(stack, issue.service, (message,))
    return stack_ok


def compose_runtime_port_issue_message(
    stack: ComposeStack,
    issue: ComposeRuntimePortIssue,
) -> str:
    return (
        f"[{stack.name}] Compose service {issue.service} has invalid "
        f"{issue.field} value {issue.value!r}: {issue.reason}."
    )


def bind_mount_path_issue_messages(
    runner: Any,
    stack: ComposeStack,
    mount: ComposeBindMount,
    issue: str,
) -> list[str]:
    target = f" -> {mount.target}" if mount.target else ""
    messages = [
        (
            f"[{stack.name}] Compose bind mount for service {mount.service} "
            f"resolves to {mount.source}{target}; {issue}."
        )
    ]
    if runner.options.host_docker_base is not None:
        messages.append(
            f"[{stack.name}] HOST_DOCKER_BASE is set to "
            f"{runner.options.host_docker_base}; verify it is the Docker "
            f"daemon-visible host root that corresponds to "
            f"DOCKER_BASE={runner.options.docker_base}."
        )
        return messages
    messages.append(
        f"[{stack.name}] Mount the Compose root at the same absolute path "
        "the Docker daemon uses, then set DOCKER_BASE to that path "
        "(for example DOCKER_BASE=/srv/docker with /srv/docker:/srv/docker), "
        "or keep the helper path and set HOST_DOCKER_BASE=/srv/docker "
        "to the matching daemon-visible host root."
    )
    return messages


def log_bind_mount_path_issue(runner: Any, messages: Sequence[str]) -> None:
    for message in messages:
        log_preflight_issue(runner, message)


def log_preflight_issue(runner: Any, message: str) -> None:
    log = runner.log.warn if runner.options.dry_run else runner.log.error
    log(message)


def validate_digest_pin_plan(runner: Any, matches: Sequence[Match]) -> bool:
    if not runner.options.digest_pin_updates:
        return True
    ok = True
    for match in matches:
        if _digest_pin_match_tag(match):
            continue
        ok = False
        runner.log.error(
            f"[{match.stack.name}] Digest-pin updates require a safe resolved "
            f"tag for line {match.target.line_no} ({match.target.first})."
        )
    try:
        for stack in _stacks_to_update(matches):
            stack_matches = [
                match for match in matches if match.stack.index == stack.index
            ]
            stack_updates = runner._digest_pin_updates(stack_matches)
            stack_directory = str(stack.directory.resolve(strict=False))
            tag_stream_updates = tuple(
                update
                for update in runner.options.tag_stream_updates
                if update.stack_directory == stack_directory
                and update.compose_file == stack.file
            )
            selected_lines = {match.target.line_no for match in stack_matches}
            if any(
                update.line_no not in selected_lines
                for update in tag_stream_updates
            ):
                raise ComposeTagRewriteError(
                    f"{stack.name} tag stream plan references an unselected line."
                )
            compose_rewrite.render_compose_digest_pins(
                stack.directory / stack.file,
                stack_updates,
                label_rewrite_approvals=(
                    runner.options.digest_pin_label_rewrite_approvals
                ),
                tag_stream_updates=tag_stream_updates,
                stack_name=stack.name,
            )
    except (ComposeTagRewriteError, UpdaterError) as exc:
        ok = False
        runner.log.error(f"Digest-pin plan is not safe to apply: {exc}")
    return ok


def validate_digest_unpin_plan(runner: Any, matches: Sequence[Match]) -> bool:
    if not runner.options.digest_unpin_plan:
        return True
    ok = True
    try:
        for stack in _stacks_to_update(matches):
            stack_matches = [
                match for match in matches if match.stack.index == stack.index
            ]
            stack_updates = runner._digest_unpin_updates(stack_matches)
            compose_rewrite.render_compose_digest_unpins(
                stack.directory / stack.file,
                stack_updates,
                stack_name=stack.name,
                config_transforms=compose_rewrite.compose_config_wud_tag_transforms(
                    stack.service_images
                ),
            )
    except (ComposeTagRewriteError, UpdaterError) as exc:
        ok = False
        runner.log.error(f"Digest-unpin plan is not safe to apply: {exc}")
    return ok


def finish_preflight_failure(
    runner: Any,
    parsed: ParsedWudFile,
    matches: Sequence[Match],
    skipped_tags: Sequence[WudTarget],
) -> int:
    preflight_failures = [
        failure
        for failure in runner.failures
        if failure.phase == "preflight"
    ]
    failed_stack_indices = {failure.stack.index for failure in preflight_failures}
    failed_matches = [
        match for match in matches if match.stack.index in failed_stack_indices
    ]
    skipped_matches = [
        match for match in matches if match.stack.index not in failed_stack_indices
    ]
    failed_lines = sorted({match.target.line_no for match in failed_matches})
    stack_statuses = {
        stack_index: StackStatus(
            "failure",
            _preflight_status_reason(stack_index, preflight_failures),
        )
        for stack_index in failed_stack_indices
    }

    updater_audit.start_audit(runner, parsed)
    updater_audit.mark_unmatched_pending(runner, parsed, matches, skipped_tags)
    updater_audit.mark_matched_pending(
        runner,
        skipped_matches,
        status="pending",
        status_reason="preflight-skipped",
    )
    updater_audit.mark_failed_pending(
        runner,
        failed_matches,
        stack_statuses,
        failed_lines,
    )
    runner._mark_failed_lines_restored(())
    updater_audit.finish_audit_run(runner, "failure")

    error_report = runner._write_error_report()
    if error_report is not None:
        runner.log.error(
            "Completed with preflight failure(s). "
            f"See log: {runner.log_file}; error report: {error_report}"
        )
    else:
        runner.log.error(
            f"Completed with preflight failure(s). See log: {runner.log_file}"
        )
    return 1
