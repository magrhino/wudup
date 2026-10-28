"""Preflight validation helpers for ``update-from-wud``."""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import compose_rewrite, updater_audit, updater_logging
from .command import CommandError
from .compose import ComposeBindMount, ComposeRuntimePortIssue, ComposeStack
from .container_identity import DockerEndpoint
from .images import image_tag
from .updater_digest_pin import _digest_pin_match_tag
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
from .updater_self_scope import (
    _DOCKER_ENDPOINT_BLOCKED,
    _SELF_UPDATE_BLOCKED,
    _docker_endpoint_services,
    self_scope_block,
    self_scope_identity,
)
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


_COMPOSE_PROJECT_CONTAINER_FORMAT = (
    '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t'
    '{{.Label "com.docker.compose.service"}}'
)


def validate_self_update_scope(runner: Any, matches: Sequence[Match]) -> bool:
    """Block stacks whose recreate scope includes WUDup or its Docker endpoint."""

    identity = self_scope_identity(
        runner.docker, runner.options.protected_container, runner.environ,
    )
    if identity is None:
        return True

    @functools.cache
    def project_containers() -> tuple[tuple[str, str, str], ...]:
        rows = runner.docker.ps_format(_COMPOSE_PROJECT_CONTAINER_FORMAT, all_containers=True)
        containers = []
        for row in rows:
            if not row.strip():
                continue
            fields = row.split("\t")
            if len(fields) != 3 or not fields[0]:
                raise ValueError("unexpected docker ps output")
            containers.append((fields[0], fields[1], fields[2]))
        return tuple(containers)

    records_by_reason = {
        _SELF_UPDATE_BLOCKED: _PreflightIssueRecords(),
        _DOCKER_ENDPOINT_BLOCKED: _PreflightIssueRecords(),
    }
    stacks = _stacks_to_update(matches)
    for stack in stacks:
        stack_matches = _matches_for_stack(matches, stack)
        scope, block = self_scope_block(
            identity, runner.docker, runner.compose, stack,
            functools.partial(runner.lifecycle._update_scope, stack, stack_matches, strict=True),
        )
        if block is None:
            runner.lifecycle.verified_update_scopes[(stack.index, _update_services(stack_matches))] = scope
            if identity.protected_id or identity.endpoint is not None:
                _keep_protected_orphans(
                    runner, stack, identity.protected_id, identity.endpoint,
                    identity.resolved_endpoint, project_containers,
                )
            continue
        message = f"[{stack.name}] {block.message}"
        runner.log.error(message)
        runner._progress(
            "preflight", "failure", message, stack=stack.name,
            services=tuple(sorted(block.services)), matches=stack_matches,
        )
        records = records_by_reason[block.reason]
        records.messages_by_stack[stack.index] = [message]
        records.services_by_stack[stack.index] = set(block.services)
    for stack in stacks:
        for reason, records in records_by_reason.items():
            _record_compose_preflight_failures(runner, matches, (stack,), records, reason=reason)
    return not any(records.messages_by_stack for records in records_by_reason.values())


def _keep_protected_orphans(
    runner: Any,
    stack: ComposeStack,
    protected_id: str,
    endpoint: DockerEndpoint | None,
    resolved_endpoint: Callable[[], DockerEndpoint],
    project_containers: Callable[[], tuple[tuple[str, str, str], ...]],
) -> None:
    """Keep orphans when ``--remove-orphans`` would remove WUDup or its Docker endpoint.

    Compose removes every project container whose service is no longer in the
    Compose file, whatever services ``up`` names. When that would remove WUDup
    or the container serving ``DOCKER_HOST``, or that cannot be checked, the
    stack is recreated without ``--remove-orphans`` instead.
    """

    if stack.index in runner.stacks_keeping_orphans:
        return
    defined = set(stack.service_names)
    try:
        if not stack.project_name:
            raise ValueError("Compose project identity is unavailable.")
        orphans = {
            container_id: service or container_id
            for container_id, project, service in project_containers()
            if project == stack.project_name and service not in defined
        }
        # ``docker ps`` prints short IDs; the inspected WUDup ID is the full one.
        self_ids = {
            container_id for container_id in orphans
            if protected_id and protected_id.startswith(container_id)
        }
        protected = {orphans[container_id] for container_id in self_ids}
        if endpoint is not None and len(self_ids) < len(orphans):
            protected |= _docker_endpoint_services(
                runner.docker,
                [container_id for container_id in orphans if container_id not in self_ids],
                resolved_endpoint,
            )
    except (CommandError, TypeError, ValueError):
        runner.stacks_keeping_orphans.add(stack.index)
        runner.log.warning(
            f"[{stack.name}] Could not check whether leftover containers in this "
            "Compose project include WUDup or the Docker endpoint it uses "
            "(DOCKER_HOST); recreating without --remove-orphans so no leftover "
            "container is removed."
        )
        return
    if not protected:
        return
    runner.stacks_keeping_orphans.add(stack.index)
    runner.log.warning(
        f"[{stack.name}] {', '.join(sorted(protected))} still runs in this Compose "
        f"project but is no longer defined in {stack.file}, and it is WUDup or the "
        "Docker endpoint WUDup uses (DOCKER_HOST). Recreating without "
        "--remove-orphans so WUDup keeps Docker access; other leftover containers "
        f"in the project are kept too. To clear this warning, add that service back "
        f"to {stack.file} or recreate it from the Compose file that now defines it."
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
