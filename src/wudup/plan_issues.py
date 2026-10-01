"""Issue generation and preflight checks for WebUI dry-run plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .command import CommandError
from .compose import (
    ComposeBindMount,
    ComposeCli,
    ComposeRuntimePortIssue,
    ComposeStack,
)
from .compose_rewrite import (
    compose_config_wud_tag_transforms,
    render_compose_digest_pins,
    render_compose_digest_unpins,
)
from .config import UpdaterConfig
from .digest_verifier import DigestVerifier, DockerManifestResolver
from .docker_cli import DockerCli
from .plan_matching import GENERIC_UNMATCHED_MESSAGE
from .plan_models import DryRunPlanIssue, DryRunPlanSkipped, UnmatchedDiagnostic
from .updater_digest_pin import (
    _digest_pin_candidates,
    _digest_pin_match_tag,
    _digest_pin_resolve_error,
    _resolve_digest_pin_candidate,
    digest_pin_update_from_values,
)
from .updater_matching import _stacks_to_update
from .updater_models import (
    ComposeTagRewriteError,
    DigestPinLabelRewrite,
    DigestPinLabelRewriteApproval,
    DigestPinLabelRewriteApprovalRequired,
    DigestPinUpdate,
    DigestUnpinUpdate,
    Match,
    TagStreamUpdate,
    UpdaterError,
    UpdateScope,
)
from .updater_planning import _container_bind_mount_path_issue, _tag_updates
from .wud_file import WudTarget


@dataclass(frozen=True)
class DigestPinPlanIssueResult:
    issues: tuple[DryRunPlanIssue, ...]
    updates_by_stack: Mapping[int, tuple[DigestPinUpdate, ...]]
    label_rewrites_by_stack: Mapping[
        int,
        Mapping[tuple[str, str], tuple[DigestPinLabelRewrite, ...]],
    ]


def unmatched_issues(
    targets: Sequence[WudTarget],
    matches: Sequence[Match],
    skipped: Sequence[DryRunPlanSkipped],
    diagnostics: Mapping[int, UnmatchedDiagnostic],
) -> list[DryRunPlanIssue]:
    matched_lines = {match.target.line_no for match in matches}
    skipped_reasons = {item.line_no: item.reason for item in skipped}
    issues: list[DryRunPlanIssue] = []
    for target in targets:
        if target.line_no in matched_lines:
            continue
        reason = skipped_reasons.get(target.line_no, "unmatched")
        severity = "warning" if reason == "tag-updates-disabled" else "error"
        diagnostic = diagnostics.get(target.line_no)
        issues.append(
            DryRunPlanIssue(
                severity=severity,
                code=diagnostic.code if diagnostic is not None else reason,
                message=_unmatched_issue_message(reason, diagnostic),
                line_no=target.line_no,
                stack="" if diagnostic is None else diagnostic.stack,
                service="" if diagnostic is None else diagnostic.service,
                hint="" if diagnostic is None else diagnostic.hint,
                details={} if diagnostic is None else diagnostic.details,
            )
        )
    return issues


def _unmatched_issue_message(
    reason: str,
    diagnostic: UnmatchedDiagnostic | None,
) -> str:
    if reason == "tag-updates-disabled":
        return "Tag update entries require allow_tag_updates=true."
    if diagnostic is not None:
        return diagnostic.message
    return GENERIC_UNMATCHED_MESSAGE


def tag_update_plan_issues(
    matches: Sequence[Match],
) -> list[DryRunPlanIssue]:
    desired_by_service: dict[tuple[int, str, str], set[str]] = {}
    issues: list[DryRunPlanIssue] = []
    for match in matches:
        if not match.target.desired_tag:
            continue
        if not match.service:
            issues.append(
                DryRunPlanIssue(
                    severity="error",
                    code="tag-update-service-unmapped",
                    message=(
                        "Tag update cannot be safely rewritten because the "
                        "Compose service image could not be mapped."
                    ),
                    line_no=match.target.line_no,
                    stack=match.stack.name,
                )
            )
            continue
        key = (match.stack.index, match.service, match.compose_image)
        desired_by_service.setdefault(key, set()).add(match.target.desired_tag)

    for stack_index, service, image in sorted(desired_by_service):
        desired = desired_by_service[(stack_index, service, image)]
        if len(desired) <= 1:
            continue
        stack_name = next(
            (
                match.stack.name
                for match in matches
                if match.stack.index == stack_index
            ),
            str(stack_index),
        )
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="conflicting-tag-updates",
                message=(
                    f"Conflicting tag updates for service {service} image "
                    f"{image}: {', '.join(sorted(desired))}."
                ),
                stack=stack_name,
                service=service,
            )
        )
    return issues


def manifest_issues(
    docker: DockerCli,
    matches: Sequence[Match],
) -> list[DryRunPlanIssue]:
    issues: list[DryRunPlanIssue] = []
    for update in _tag_updates(matches):
        try:
            docker.manifest_inspect(update.new_image)
        except CommandError as exc:
            detail = _first_error_line(exc)
            suffix = f": {detail}" if detail else ""
            issues.append(
                DryRunPlanIssue(
                    severity="error",
                    code="tag-manifest-unavailable",
                    message=(
                        f"Invalid or unavailable remote tag "
                        f"{update.old_image} -> {update.new_image}{suffix}"
                    ),
                )
            )
    return issues


def digest_pin_plan_issues(
    config: UpdaterConfig,
    docker: DockerCli,
    matches: Sequence[Match],
    digest_pin_label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval],
    tag_stream_updates_by_stack: Mapping[
        int,
        Sequence[TagStreamUpdate],
    ] | None = None,
) -> DigestPinPlanIssueResult:
    updates_by_stack: dict[int, tuple[DigestPinUpdate, ...]] = {}
    label_rewrites_by_stack: dict[
        int,
        dict[tuple[str, str], tuple[DigestPinLabelRewrite, ...]],
    ] = {}
    if not config.digest_pin_updates:
        return DigestPinPlanIssueResult((), updates_by_stack, label_rewrites_by_stack)

    issues: list[DryRunPlanIssue] = []
    stream_updates_by_stack = tag_stream_updates_by_stack or {}
    for stack in _stacks_to_update(matches):
        stack_matches = [
            match for match in matches if match.stack.index == stack.index
        ]
        issues.extend(_digest_pin_tag_issues(stack_matches))
        stack_updates, update_issues = _digest_pin_stack_updates(
            docker,
            stack,
            stack_matches,
        )
        issues.extend(update_issues)

        if stack_updates:
            label_rewrites, render_issues = _digest_pin_render_issues(
                stack,
                stack_updates,
                digest_pin_label_rewrite_approvals,
                stream_updates_by_stack.get(stack.index, ()),
            )
            issues.extend(render_issues)
            if label_rewrites:
                label_rewrites_by_stack[stack.index] = label_rewrites
        label_rewrites_by_stack.setdefault(stack.index, {})
        updates_by_stack[stack.index] = tuple(stack_updates)
    return DigestPinPlanIssueResult(
        tuple(issues),
        updates_by_stack,
        label_rewrites_by_stack,
    )


def _digest_pin_tag_issues(
    matches: Sequence[Match],
) -> list[DryRunPlanIssue]:
    issues: list[DryRunPlanIssue] = []
    for match in matches:
        if _digest_pin_match_tag(match):
            continue
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="digest-pin-tag-required",
                message=(
                    "Digest-pin updates require a safe resolved tag. "
                    "This line cannot be digest-pinned automatically."
                ),
                line_no=match.target.line_no,
                stack=match.stack.name,
                service=match.service,
            )
        )
    return issues


def _digest_pin_stack_updates(
    docker: DockerCli,
    stack: ComposeStack,
    matches: Sequence[Match],
) -> tuple[list[DigestPinUpdate], list[DryRunPlanIssue]]:
    issues: list[DryRunPlanIssue] = []
    updates: list[DigestPinUpdate] = []
    resolver = DockerManifestResolver(docker, verbose=True)
    verifier = DigestVerifier(
        docker,
        primary_resolver=resolver,
        fallback_resolver=resolver,
    )
    try:
        candidates = _digest_pin_candidates(matches)
    except UpdaterError as exc:
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="digest-pin-conflict",
                message=f"Digest-pin plan is not safe to apply: {exc}",
                stack=stack.name,
            )
        )
        return updates, issues

    for candidate in candidates:
        result = _resolve_digest_pin_candidate(verifier, candidate)
        if not result.ok:
            code = (
                "digest-pin-digest-stale"
                if result.reason == "stale-digest"
                else "digest-pin-digest-unavailable"
            )
            issues.append(
                DryRunPlanIssue(
                    severity="error",
                    code=code,
                    message=(
                        _digest_pin_resolve_error(
                            candidate.resolved_image,
                            result,
                        )
                        + (f" ({result.error})" if result.error else "")
                    ),
                    stack=stack.name,
                )
            )
            continue
        updates.append(
            digest_pin_update_from_values(
                old_image=candidate.old_image,
                resolved_tag=candidate.resolved_tag,
                planned_digest=result.digest,
                services=candidate.services,
            )
        )
    return updates, issues


def _digest_pin_render_issues(
    stack: ComposeStack,
    stack_updates: Sequence[DigestPinUpdate],
    digest_pin_label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval],
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
) -> tuple[
    dict[tuple[str, str], tuple[DigestPinLabelRewrite, ...]],
    list[DryRunPlanIssue],
]:
    issues: list[DryRunPlanIssue] = []
    if any(not update.approved for update in tag_stream_updates):
        return {}, [
            DryRunPlanIssue(
                severity="error",
                code="compose-digest-pin-validation-deferred",
                message=(
                    "Compose digest-pin validation is deferred until every tag "
                    "stream label rewrite in this stack is approved."
                ),
                stack=stack.name,
            )
        ]
    try:
        _rendered, applied = render_compose_digest_pins(
            stack.directory / stack.file,
            stack_updates,
            label_rewrite_approvals=digest_pin_label_rewrite_approvals,
            tag_stream_updates=tag_stream_updates,
            stack_name=stack.name,
        )
        return (
            {
                (item.old_image, item.resolved_tag): item.label_rewrites
                for item in applied
            },
            issues,
        )
    except DigestPinLabelRewriteApprovalRequired as exc:
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="compose-digest-pin-label-rewrite-unapproved",
                message=(
                    f'{stack.name} {exc.label_key} is '
                    f'"{exc.current_label_value}"; approve replacing it '
                    f'with "{exc.proposed_label_regex}" before pinning '
                    "the digest."
                ),
                stack=stack.name,
                service=exc.service,
                hint=(
                    "Approve the label rewrite to replace the current "
                    "include rule with the exact planned tag, or edit "
                    "the Compose label manually."
                ),
                details={
                    "stack": stack.name,
                    "service": exc.service,
                    "compose_file": stack.file,
                    "label_key": exc.label_key,
                    "current_label_value": exc.current_label_value,
                    "planned_tag": exc.planned_tag,
                    "proposed_label_value": exc.proposed_label_value,
                    "proposed_label_regex": exc.proposed_label_regex,
                    "explanation": (
                        "WUDup can only overwrite this include "
                        "rule after explicit approval because it is not "
                        "an empty label, an exact tag regex, or a plain "
                        "tag matching the current/planned update."
                    ),
                },
            )
        )
    except ComposeTagRewriteError as exc:
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="compose-digest-pin-unsupported",
                message=f"Compose digest-pin rewrite is not safe: {exc}",
                stack=stack.name,
            )
        )
    return {}, issues


def digest_unpin_plan_issues(
    matches: Sequence[Match],
    digest_unpin_updates_by_stack: Mapping[int, Sequence[DigestUnpinUpdate]],
) -> tuple[
    list[DryRunPlanIssue],
    dict[int, dict[tuple[str, str], tuple[tuple[str, str], ...]]],
]:
    """Return render issues and the managed transforms each unpin would add."""

    issues: list[DryRunPlanIssue] = []
    transforms: dict[int, dict[tuple[str, str], tuple[tuple[str, str], ...]]] = {}
    stack_by_index = {stack.index: stack for stack in _stacks_to_update(matches)}
    for stack_index, updates in sorted(digest_unpin_updates_by_stack.items()):
        if not updates:
            continue
        stack = stack_by_index.get(stack_index)
        if stack is None:
            continue
        try:
            _rendered, applied = render_compose_digest_unpins(
                stack.directory / stack.file,
                updates,
                stack_name=stack.name,
                config_transforms=compose_config_wud_tag_transforms(stack.service_images),
            )
            transforms[stack_index] = {
                (item.old_image, item.resolved_tag): item.added_transforms
                for item in applied
                if item.added_transforms
            }
        except ComposeTagRewriteError as exc:
            issues.append(
                DryRunPlanIssue(
                    severity="error",
                    code="compose-digest-unpin-unsupported",
                    message=f"Compose digest-unpin rewrite is not safe: {exc}",
                    stack=stack.name,
                )
            )
    return issues, transforms


def preflight_issues(
    config: UpdaterConfig,
    compose: ComposeCli,
    matches: Sequence[Match],
    update_scope: Callable[[ComposeStack, Sequence[Match]], UpdateScope],
) -> list[DryRunPlanIssue]:
    issues: list[DryRunPlanIssue] = []
    for stack in _stacks_to_update(matches):
        stack_matches = [
            match for match in matches if match.stack.index == stack.index
        ]
        scope = update_scope(stack, stack_matches)
        scoped_services = set(scope.services or ())

        runtime_issues = compose.try_service_runtime_port_issues(
            stack.directory,
            stack.file,
            project_directory=stack.project_directory,
        )
        issues.extend(
            _runtime_port_preflight_issues(
                stack,
                scoped_services,
                runtime_issues,
            )
        )

        mounts = compose.try_service_bind_mounts(
            stack.directory,
            stack.file,
            project_directory=stack.project_directory,
        )
        issues.extend(
            _bind_mount_preflight_issues(
                config,
                stack,
                scoped_services,
                mounts,
            )
        )
    return issues


def _runtime_port_preflight_issues(
    stack: ComposeStack,
    scoped_services: set[str],
    runtime_issues: Sequence[ComposeRuntimePortIssue],
) -> list[DryRunPlanIssue]:
    issues: list[DryRunPlanIssue] = []
    runtime_scope = scoped_services or {issue.service for issue in runtime_issues}
    for issue in runtime_issues:
        if issue.service not in runtime_scope:
            continue
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="compose-port-invalid",
                message=(
                    f"Compose service {issue.service} has invalid "
                    f"{issue.field} value {issue.value!r}: {issue.reason}."
                ),
                stack=stack.name,
                service=issue.service,
            )
        )
    return issues


def _bind_mount_preflight_issues(
    config: UpdaterConfig,
    stack: ComposeStack,
    scoped_services: set[str],
    mounts: Sequence[ComposeBindMount],
) -> list[DryRunPlanIssue]:
    issues: list[DryRunPlanIssue] = []
    mount_scope = scoped_services or {mount.service for mount in mounts}
    for mount in mounts:
        if mount.service not in mount_scope:
            continue
        issue = _container_bind_mount_path_issue(
            mount,
            docker_base=config.docker_base,
        )
        if not issue:
            continue
        target = f" -> {mount.target}" if mount.target else ""
        issues.append(
            DryRunPlanIssue(
                severity="error",
                code="bind-mount-path-invalid",
                message=(
                    f"Compose bind mount resolves to {mount.source}"
                    f"{target}; {issue}."
                ),
                stack=stack.name,
                service=mount.service,
            )
        )
    return issues


def _first_error_line(exc: CommandError) -> str:
    for line in (*exc.result.stderr_lines, *exc.result.stdout_lines):
        if line.strip():
            return line.strip()
    return ""
