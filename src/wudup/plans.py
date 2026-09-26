"""Structured dry-run planning for WebUI update previews."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .command import CommandRunner
from .compose import ComposeCli, ComposeDiscoveryError, ComposeStack
from .config import UpdaterConfig
from .digest_provenance import (
    DigestTagProvenance,
    digest_provenance_from_digest_target,
    digest_provenance_from_unpin_update,
    digest_provenance_from_update,
)
from .docker_cli import DockerCli
from .images import (
    image_tag,
    image_with_tag,
    normalize_digest,
)
from .plan_actions import render_plan_actions
from .plan_digest_unpin import recover_digest_unpin_matches
from .plan_identity import _file_sha256, _plan_id
from .plan_issues import (
    digest_pin_plan_issues,
    digest_unpin_plan_issues,
    manifest_issues,
    preflight_issues,
    tag_update_plan_issues,
    unmatched_issues,
)
from .plan_matching import (
    _cleanup_for_skipped,
    _match_targets,
    _unmatched_diagnostics,
    filter_matches_for_completed_update_selections,
    filter_matches_for_selections,
    normalize_update_selections,
    selection_id_for_matches,
)
from .plan_models import (
    DryRunPlan,
    DryRunPlanAction,
    DryRunPlanCleanup,
    DryRunPlanCleanupItem,
    DryRunPlanDigestPinLabelRewrite,
    DryRunPlanDigestPinUpdate,
    DryRunPlanDigestUnpinUpdate,
    DryRunPlanIssue,
    DryRunPlanLine,
    DryRunPlanSkipped,
    DryRunPlanSource,
    DryRunPlanStack,
    DryRunPlanSummary,
    DryRunPlanTagStreamUpdate,
    DryRunPlanTagUpdate,
    DryRunPlanTarget,
    PendingGroupingItem,
    PendingGroupingResult,
    PendingStackGroup,
    PendingTagStream,
    PlanFileMissing,
    PlanInputError,
    UnmatchedDiagnostic,
)
from .tag_streams import pending_tag_stream_hint, plan_tag_stream_changes
from .updater_digest_pin import _digest_pin_match_tag
from .updater_lifecycle_scope import (
    _UpdateScopeMixin,
    runtime_services_for_scope,
)
from .updater_matching import (
    _ordered_unique,
    _scope_plan_label,
    _stacks_to_update,
)
from .updater_models import (
    CompletedUpdateSelection,
    DigestPinLabelRewrite,
    DigestPinLabelRewriteApproval,
    DigestPinUpdate,
    DigestUnpinUpdate,
    Match,
    TagOverride,
    TagStreamDecision,
    TagStreamLabelRewriteApproval,
    TagStreamUpdate,
    UpdateSelection,
)
from .updater_planning import _tag_updates
from .wud_file import (
    ParsedWudFile,
    WudTarget,
    is_digest_target_line,
    parse_wud_file,
)

__all__ = [
    "DryRunPlan",
    "DryRunPlanAction",
    "DryRunPlanCleanup",
    "DryRunPlanCleanupItem",
    "DryRunPlanDigestPinLabelRewrite",
    "DryRunPlanDigestPinUpdate",
    "DryRunPlanDigestUnpinUpdate",
    "DryRunPlanIssue",
    "DryRunPlanLine",
    "DryRunPlanSkipped",
    "DryRunPlanSource",
    "DryRunPlanStack",
    "DryRunPlanSummary",
    "DryRunPlanTagUpdate",
    "DryRunPlanTarget",
    "PendingGroupingItem",
    "PendingGroupingResult",
    "PendingStackGroup",
    "PlanFileMissing",
    "PlanInputError",
    "UnmatchedDiagnostic",
    "build_dry_run_plan",
    "build_dry_run_plan_from_pending_source",
    "build_unmatched_cleanup",
    "resolve_pending_groups",
]

_DigestProvenanceByService = Mapping[str, DigestTagProvenance]
_DigestUnpinUpdatesByStack = Mapping[int, Sequence[DigestUnpinUpdate]]


@dataclass(frozen=True)
class _PlanSelectionScope:
    update_selections: Sequence[UpdateSelection] = ()
    completed_update_selections: Sequence[CompletedUpdateSelection] = ()


@dataclass(frozen=True)
class _TagStreamPlanInputs:
    tag_overrides: Sequence[TagOverride] = ()
    decisions: Sequence[TagStreamDecision] = ()
    label_rewrite_approvals: Sequence[TagStreamLabelRewriteApproval] = ()


@dataclass
class _PlanBuilder(_UpdateScopeMixin):
    config: UpdaterConfig
    line_numbers: Sequence[int]
    update_selections: Sequence[UpdateSelection] = ()
    completed_update_selections: Sequence[CompletedUpdateSelection] = ()
    allow_tag_updates: bool = False
    tag_overrides: Sequence[TagOverride] = ()
    tag_stream_decisions: Sequence[TagStreamDecision] = ()
    tag_stream_label_rewrite_approvals: Sequence[
        TagStreamLabelRewriteApproval
    ] = ()
    digest_pin_label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval] = ()
    host_docker_base: Path | None = None
    command_runner: CommandRunner | None = None
    source_parsed: ParsedWudFile | None = None
    source_file: str | None = None
    source_hash: str | None = None
    source: DryRunPlanSource | None = None
    known_digest_provenance_by_service: _DigestProvenanceByService = field(
        default_factory=dict,
    )
    docker: DockerCli = field(init=False)
    compose: ComposeCli = field(init=False)
    digest_pin_updates_by_stack: dict[int, tuple[DigestPinUpdate, ...]] = field(
        init=False,
        default_factory=dict,
    )
    digest_pin_label_rewrites_by_stack: dict[
        int,
        dict[tuple[str, str], tuple[DigestPinLabelRewrite, ...]],
    ] = field(init=False, default_factory=dict)
    digest_unpin_updates_by_stack: dict[int, tuple[DigestUnpinUpdate, ...]] = field(
        init=False,
        default_factory=dict,
    )
    tag_stream_updates_by_stack: dict[int, tuple[TagStreamUpdate, ...]] = field(
        init=False,
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        runner = self.command_runner or CommandRunner()
        self.docker = DockerCli(runner=runner)
        self.compose = ComposeCli(runner=runner)

    def _source_input(
        self,
    ) -> tuple[ParsedWudFile, str, str, DryRunPlanSource]:
        if self.source_parsed is None:
            full_parse = _read_wud_file(self.config.wud_out_file)
            source_hash = _file_sha256(self.config.wud_out_file)
            return (
                full_parse,
                str(self.config.wud_out_file),
                source_hash,
                DryRunPlanSource(source_hash=source_hash),
            )
        source_hash = self.source_hash or ""
        plan_source = self.source or DryRunPlanSource(source_hash=source_hash)
        if plan_source.source_hash != source_hash:
            plan_source = replace(plan_source, source_hash=source_hash)
        return (
            self.source_parsed,
            self.source_file or str(self.config.wud_out_file),
            source_hash,
            plan_source,
        )

    def build(self) -> DryRunPlan:
        normalized_selections = normalize_update_selections(self.update_selections)
        if self.line_numbers and normalized_selections:
            raise PlanInputError(
                "provide either line_numbers or selections, not both"
            )
        selected = _selected_line_numbers(
            self.line_numbers
            or tuple(item.line_no for item in normalized_selections)
        )
        full_parse, source_file, source_hash, plan_source = self._source_input()
        _validate_selected_targets(full_parse, selected)
        parsed = (
            parse_wud_file(self.config.wud_out_file, selected_lines=selected)
            if self.source_parsed is None
            else _parsed_for_selected_lines(full_parse, selected)
        )
        issues = [
            DryRunPlanIssue(
                severity="warning",
                code="wud-parse-warning",
                message=warning,
            )
            for warning in parsed.warnings
        ]
        skipped: list[DryRunPlanSkipped] = []
        matches: list[Match] = []
        stacks: tuple[ComposeStack, ...] = ()
        cleanup = DryRunPlanCleanup()

        try:
            stacks = self.compose.discover_stacks(
                self.config.docker_base,
                project_base=self.host_docker_base,
                ignore_paths=self.config.compose_ignore_paths,
            )
        except ComposeDiscoveryError as exc:
            issues.append(
                DryRunPlanIssue(
                    severity="error",
                    code="compose-discovery-failed",
                    message=str(exc),
                )
            )
        else:
            matches, skipped, digest_unpin_issues = self._build_matches(parsed, stacks)
            matches = filter_matches_for_completed_update_selections(
                matches,
                self.completed_update_selections,
            )
            matches, normalized_selections = filter_matches_for_selections(
                matches,
                normalized_selections,
            )
            tag_stream_result = plan_tag_stream_changes(
                self.docker,
                matches,
                decisions=self.tag_stream_decisions,
                label_rewrite_approvals=self.tag_stream_label_rewrite_approvals,
                tag_overrides=self.tag_overrides,
            )
            matches = list(tag_stream_result.matches)
            issues.extend(tag_stream_result.issues)
            self.tag_stream_updates_by_stack = dict(
                tag_stream_result.updates_by_stack
            )
            parsed = _apply_selected_tags(
                parsed,
                tag_stream_result.selected_tags_by_line,
            )
            parsed = self._apply_tag_overrides(parsed)
            matches = _replace_match_targets(
                matches,
                parsed,
                preserve_desired_tag_lines=tag_stream_result.selected_tags_by_line,
            )
            self._filter_digest_unpin_updates(matches)
            if any(item.selection_id for item in normalized_selections):
                digest_unpin_issues = tuple(
                    issue
                    for issue in digest_unpin_issues
                    if _issue_applies_to_matches(issue, matches)
                )
            diagnostics = _unmatched_diagnostics(
                self.config,
                parsed.targets,
                skipped,
                self.docker,
                host_docker_base=self.host_docker_base,
            )
            issues.extend(unmatched_issues(parsed.targets, matches, skipped, diagnostics))
            issues.extend(digest_unpin_issues)
            cleanup_skipped = skipped
            cleanup_diagnostics = diagnostics
            if not self.allow_tag_updates and any(
                target.desired_tag for target in parsed.targets
            ):
                _cleanup_matches, cleanup_skipped = _match_targets(
                    parsed,
                    stacks,
                    self.docker,
                    allow_tag_updates=True,
                    allow_digest_pin_rematch=self.config.digest_pin_updates,
                )
                cleanup_diagnostics = _unmatched_diagnostics(
                    self.config,
                    parsed.targets,
                    cleanup_skipped,
                    self.docker,
                    host_docker_base=self.host_docker_base,
                )
            cleanup = _cleanup_for_skipped(
                self.config,
                parsed.targets,
                cleanup_skipped,
                cleanup_diagnostics,
                host_docker_base=self.host_docker_base,
            )
            issues.extend(tag_update_plan_issues(matches))
            issues.extend(manifest_issues(self.docker, matches))
            digest_pin_result = digest_pin_plan_issues(
                self.config,
                self.docker,
                matches,
                self.digest_pin_label_rewrite_approvals,
                self.tag_stream_updates_by_stack,
            )
            self.digest_pin_updates_by_stack = dict(digest_pin_result.updates_by_stack)
            self.digest_pin_label_rewrites_by_stack = {
                stack_index: dict(rewrites_by_update)
                for stack_index, rewrites_by_update in (
                    digest_pin_result.label_rewrites_by_stack.items()
                )
            }
            issues.extend(digest_pin_result.issues)
            issues.extend(
                digest_unpin_plan_issues(matches, self.digest_unpin_updates_by_stack)
            )
            issues.extend(
                preflight_issues(self.config, self.compose, matches, self._update_scope)
            )

        targets_by_line = {target.line_no: target for target in parsed.targets}
        plan_stacks = self._plan_stacks(matches)
        targets = self._plan_targets(targets_by_line, matches, skipped)
        status = _plan_status(matches, skipped, issues)
        summary = DryRunPlanSummary(
            target_count=len(parsed.targets),
            matched_target_count=len({match.target.line_no for match in matches}),
            stack_count=len(plan_stacks),
            service_count=_service_count(plan_stacks),
            skipped_count=len(skipped),
            issue_count=len(issues),
        )
        plan = DryRunPlan(
            plan_id="",
            dry_run=True,
            can_apply=False,
            status=status,
            source_file=source_file,
            mode=self.config.update_mode,
            max_wait=self.config.max_wait,
            digest_pin_updates=self.config.digest_pin_updates,
            selected_line_numbers=selected,
            summary=summary,
            selected_selections=normalized_selections,
            completed_update_selections=tuple(self.completed_update_selections),
            source=plan_source,
            targets=targets,
            stacks=plan_stacks,
            skipped=tuple(skipped),
            issues=tuple(issues),
            cleanup=cleanup,
        )
        return replace(
            plan,
            plan_id=_plan_id(
                plan,
                config=self.config,
                allow_tag_updates=self.allow_tag_updates,
                tag_overrides=self.tag_overrides,
                tag_stream_decisions=self.tag_stream_decisions,
                tag_stream_label_rewrite_approvals=(
                    self.tag_stream_label_rewrite_approvals
                ),
                digest_pin_label_rewrite_approvals=(
                    self.digest_pin_label_rewrite_approvals
                ),
                host_docker_base=self.host_docker_base,
                wud_file_hash=source_hash,
                source_file=source_file,
            ),
        )

    def _filter_digest_unpin_updates(self, matches: Sequence[Match]) -> None:
        selected_by_stack = {
            stack.index: [
                match for match in matches if match.stack.index == stack.index
            ]
            for stack in _stacks_to_update(matches)
        }
        filtered: dict[int, tuple[DigestUnpinUpdate, ...]] = {}
        for stack_index, updates in self.digest_unpin_updates_by_stack.items():
            selected_matches = selected_by_stack.get(stack_index, ())
            selected_updates = tuple(
                update
                for update in updates
                if any(
                    self._match_digest_unpin(match) is update
                    for match in selected_matches
                )
            )
            if selected_updates:
                filtered[stack_index] = selected_updates
        self.digest_unpin_updates_by_stack = filtered

    def _apply_tag_overrides(self, parsed: ParsedWudFile) -> ParsedWudFile:
        overrides = {item.line_no: item.tag for item in self.tag_overrides}
        if not overrides:
            return parsed
        if not self.allow_tag_updates:
            raise PlanInputError("tag_overrides require allow_tag_updates=true")

        targets_by_line = {target.line_no: target for target in parsed.targets}
        missing = sorted(set(overrides) - set(targets_by_line))
        if missing:
            values = ", ".join(str(line_no) for line_no in missing)
            raise PlanInputError(
                "tag_overrides must reference selected WUD tag update lines: "
                + values
            )

        updated_targets: list[WudTarget] = []
        for target in parsed.targets:
            override = overrides.get(target.line_no)
            if override is None:
                updated_targets.append(target)
                continue
            if not target.desired_tag:
                raise PlanInputError(
                    f"tag_overrides line {target.line_no} does not target a tag update"
                )
            updated_targets.append(replace(target, desired_tag=override))

        return ParsedWudFile(
            lines=parsed.lines,
            targets=tuple(updated_targets),
            warnings=parsed.warnings,
        )

    def _build_matches(
        self,
        parsed: ParsedWudFile,
        stacks: Sequence[ComposeStack],
    ) -> tuple[list[Match], list[DryRunPlanSkipped], tuple[DryRunPlanIssue, ...]]:
        self.digest_unpin_updates_by_stack = {}
        matches, skipped = _match_targets(
            parsed,
            stacks,
            self.docker,
            allow_tag_updates=self.allow_tag_updates,
            allow_digest_pin_rematch=self.config.digest_pin_updates,
        )
        if self.config.digest_pin_updates:
            return matches, skipped, ()

        result = recover_digest_unpin_matches(
            parsed,
            stacks,
            matches,
            skipped,
            self.known_digest_provenance_by_service,
        )
        self.digest_unpin_updates_by_stack = dict(result.updates_by_stack)
        return list(result.matches), list(result.skipped), result.issues

    def _plan_stacks(self, matches: Sequence[Match]) -> tuple[DryRunPlanStack, ...]:
        stacks: list[DryRunPlanStack] = []
        for stack in _stacks_to_update(matches):
            stack_matches = [
                match for match in matches if match.stack.index == stack.index
            ]
            scope = self._update_scope(stack, stack_matches)
            tag_updates = _tag_updates(stack_matches)
            plan_tag_updates = tuple(
                DryRunPlanTagUpdate(
                    old_image=update.old_image,
                    desired_tag=update.desired_tag,
                    new_image=update.new_image,
                    services=update.services,
                )
                for update in tag_updates
            )
            plan_tag_stream_updates = tuple(
                DryRunPlanTagStreamUpdate(
                    line_no=update.line_no,
                    service=update.service,
                    current_tag=update.current_tag,
                    reported_tag=update.reported_tag,
                    selected_tag=update.selected_tag,
                    decision=update.decision,
                    label_key=update.label_key,
                    current_label_value=update.current_label_value,
                    proposed_label_value=update.proposed_label_value,
                    proposed_label_regex=update.proposed_label_regex,
                    approved=update.approved,
                    reason=update.reason,
                )
                for update in self.tag_stream_updates_by_stack.get(stack.index, ())
            )
            digest_pin_updates = self.digest_pin_updates_by_stack.get(stack.index, ())
            digest_unpin_updates = self.digest_unpin_updates_by_stack.get(
                stack.index,
                (),
            )
            label_rewrites_by_update = self.digest_pin_label_rewrites_by_stack.get(
                stack.index,
                {},
            )
            plan_digest_pin_updates = tuple(
                DryRunPlanDigestPinUpdate(
                    source_image=update.old_image,
                    resolved_tag=update.resolved_tag,
                    planned_digest=update.planned_digest,
                    final_image=update.final_image,
                    watch_tag=update.watch_tag,
                    marker=update.marker,
                    label_key=update.label_key,
                    label_value=update.label_value,
                    services=update.services,
                    digest_provenance=digest_provenance_from_update(
                        update,
                        provenance_source="plan",
                        provenance_confidence="verified",
                    ),
                    label_rewrites=tuple(
                        DryRunPlanDigestPinLabelRewrite(
                            service=item.service,
                            label_key=item.label_key,
                            current_label_value=item.current_label_value,
                            planned_tag=item.planned_tag,
                            proposed_label_value=item.proposed_label_value,
                            proposed_label_regex=item.proposed_label_regex,
                            approved=item.approved,
                            reason=item.reason,
                        )
                        for item in label_rewrites_by_update.get(
                            (update.old_image, update.resolved_tag),
                            (),
                        )
                    ),
                )
                for update in digest_pin_updates
            )
            plan_digest_unpin_updates = tuple(
                DryRunPlanDigestUnpinUpdate(
                    source_image=update.old_image,
                    resolved_tag=update.resolved_tag,
                    tag_image=update.tag_image,
                    current_digest=update.current_digest,
                    target_digest=update.target_digest,
                    watch_tag=update.watch_tag,
                    marker=update.marker,
                    label_key=update.label_key,
                    label_value=update.label_value,
                    services=update.services,
                    digest_provenance=digest_provenance_from_unpin_update(
                        update,
                        provenance_source="plan",
                        provenance_confidence="recovered",
                    ),
                )
                for update in digest_unpin_updates
            )
            stacks.append(
                DryRunPlanStack(
                    name=stack.name,
                    directory=str(stack.directory),
                    compose_file=stack.file,
                    project_directory=(
                        "" if stack.project_directory is None else str(stack.project_directory)
                    ),
                    services_label=_scope_plan_label(scope),
                    services=tuple(scope.services or ()),
                    pull_services=tuple(scope.pull_services or ()),
                    stop_services=tuple(scope.stop_services or ()),
                    force_recreate=scope.force_recreate,
                    up_no_deps=scope.up_no_deps,
                    tag_updates=plan_tag_updates,
                    tag_stream_updates=plan_tag_stream_updates,
                    digest_pin_updates=plan_digest_pin_updates,
                    digest_unpin_updates=plan_digest_unpin_updates,
                    actions=render_plan_actions(
                        self.config,
                        self.compose,
                        stack,
                        scope,
                        plan_tag_updates,
                        plan_tag_stream_updates,
                        plan_digest_pin_updates,
                        plan_digest_unpin_updates,
                    ),
                    lines=self._plan_lines(
                        stack_matches,
                        digest_pin_updates,
                        digest_unpin_updates,
                    ),
                )
            )
        return tuple(stacks)

    def _plan_targets(
        self,
        targets_by_line: Mapping[int, WudTarget],
        matches: Sequence[Match],
        skipped: Sequence[DryRunPlanSkipped],
    ) -> tuple[DryRunPlanTarget, ...]:
        matches_by_line: dict[int, list[Match]] = {}
        for match in matches:
            matches_by_line.setdefault(match.target.line_no, []).append(match)
        skipped_by_line = {item.line_no: item for item in skipped}
        targets: list[DryRunPlanTarget] = []
        for line_no in sorted(targets_by_line):
            target = targets_by_line[line_no]
            line_matches = matches_by_line.get(line_no, [])
            resolved = (
                self._normalized_resolved_image(line_matches[0])
                if line_matches
                else target.first
            )
            action = (
                "digest-unpin"
                if any(self._match_digest_unpin(match) for match in line_matches)
                else _target_action(target, bool(line_matches), skipped_by_line.get(line_no))
            )
            targets.append(
                DryRunPlanTarget(
                    line_no=target.line_no,
                    raw=target.raw,
                    image=target.first,
                    resolved_image=resolved,
                    digest=target.digest,
                    desired_tag=target.desired_tag,
                    matched=bool(line_matches),
                    action=action,
                )
            )
        return tuple(targets)

    def _plan_lines(
        self,
        matches: Sequence[Match],
        digest_pin_updates: Sequence[DigestPinUpdate] = (),
        digest_unpin_updates: Sequence[DigestUnpinUpdate] = (),
    ) -> tuple[DryRunPlanLine, ...]:
        seen: set[tuple[int, str, str, str]] = set()
        lines: list[DryRunPlanLine] = []
        digest_pins = {
            (update.old_image, update.resolved_tag): update
            for update in digest_pin_updates
        }
        digest_unpins = {
            (update.old_image, update.resolved_tag, update.target_digest): update
            for update in digest_unpin_updates
        }
        for match in matches:
            key = (
                match.target.line_no,
                match.resolved,
                match.compose_image,
                match.service,
            )
            if key in seen:
                continue
            seen.add(key)
            digest_pin_tag = _digest_pin_match_tag(match)
            digest_pin = digest_pins.get((match.compose_image, digest_pin_tag))
            digest_unpin = digest_unpins.get(
                (
                    match.compose_image,
                    image_tag(match.target.first),
                    normalize_digest(match.target.digest),
                )
            )
            if digest_pin is not None:
                target_image, resolved_image, digest_provenance = (
                    _digest_pin_line_image_details(digest_pin)
                )
            elif digest_unpin is not None:
                target_image, resolved_image, digest_provenance = (
                    _digest_unpin_line_image_details(digest_unpin)
                )
            else:
                target_image = self._target_image_for_match(match)
                resolved_image = target_image
                digest_provenance = None
            lines.append(
                DryRunPlanLine(
                    line_no=match.target.line_no,
                    raw=match.target.raw,
                    image=match.target.first,
                    resolved_image=resolved_image,
                    compose_image=match.compose_image,
                    target_image=target_image,
                    service=match.service,
                    digest=match.target.digest,
                    desired_tag=match.target.desired_tag,
                    action=_plan_line_action(match, digest_pin, digest_unpin),
                    digest_provenance=digest_provenance,
                )
            )
        return tuple(lines)

    def _target_image_for_match(self, match: Match) -> str:
        if match.target.desired_tag:
            return image_with_tag(match.compose_image, match.target.desired_tag)
        return self._normalized_resolved_image(match)

    def _normalized_resolved_image(self, match: Match) -> str:
        return _pending_normalized_resolved(
            match,
            digest_pin_updates_enabled=self.config.digest_pin_updates,
        )

    def _match_digest_unpin(self, match: Match) -> DigestUnpinUpdate | None:
        return _pending_digest_unpin_for_matches(
            (match,),
            self.digest_unpin_updates_by_stack.get(match.stack.index, ()),
        )


def _digest_pin_line_image_details(
    update: DigestPinUpdate,
) -> tuple[str, str, DigestTagProvenance]:
    return (
        update.final_image,
        update.resolved_image,
        digest_provenance_from_update(
            update,
            provenance_source="plan",
            provenance_confidence="verified",
        ),
    )


def _digest_unpin_line_image_details(
    update: DigestUnpinUpdate,
) -> tuple[str, str, DigestTagProvenance]:
    return (
        update.tag_image,
        update.tag_image,
        digest_provenance_from_unpin_update(
            update,
            provenance_source="plan",
            provenance_confidence="recovered",
        ),
    )


def _plan_line_action(
    match: Match,
    digest_pin: DigestPinUpdate | None,
    digest_unpin: DigestUnpinUpdate | None,
) -> str:
    if digest_pin is not None:
        return "digest-pin"
    if digest_unpin is not None:
        return "digest-unpin"
    if match.target.desired_tag:
        return "tag-update"
    return "update"


def build_dry_run_plan(
    config: UpdaterConfig,
    *,
    line_numbers: Sequence[int],
    update_selections: Sequence[UpdateSelection] = (),
    completed_update_selections: Sequence[CompletedUpdateSelection] = (),
    allow_tag_updates: bool = False,
    tag_overrides: Sequence[TagOverride] = (),
    tag_stream_decisions: Sequence[TagStreamDecision] = (),
    tag_stream_label_rewrite_approvals: Sequence[
        TagStreamLabelRewriteApproval
    ] = (),
    digest_pin_label_rewrite_approvals: Sequence[
        DigestPinLabelRewriteApproval
    ] = (),
    host_docker_base: Path | None = None,
    environ: Mapping[str, str] | None = None,
    known_digest_provenance_by_service: _DigestProvenanceByService | None = None,
) -> DryRunPlan:
    runner = CommandRunner(env=environ) if environ is not None else CommandRunner()
    return _PlanBuilder(
        config=config,
        line_numbers=line_numbers,
        update_selections=update_selections,
        completed_update_selections=completed_update_selections,
        allow_tag_updates=allow_tag_updates,
        tag_overrides=tag_overrides,
        tag_stream_decisions=tag_stream_decisions,
        tag_stream_label_rewrite_approvals=tag_stream_label_rewrite_approvals,
        digest_pin_label_rewrite_approvals=digest_pin_label_rewrite_approvals,
        host_docker_base=host_docker_base,
        command_runner=runner,
        known_digest_provenance_by_service=known_digest_provenance_by_service or {},
    ).build()


def build_dry_run_plan_from_pending_source(
    config: UpdaterConfig,  # NOSONAR - compatibility facade
    parsed: ParsedWudFile,
    *,
    source_file: str,
    source_hash: str,
    source: DryRunPlanSource,
    line_numbers: Sequence[int],
    selection_scope: _PlanSelectionScope | None = None,
    allow_tag_updates: bool = False,
    tag_overrides: Sequence[TagOverride] = (),
    tag_stream_decisions: Sequence[TagStreamDecision] = (),
    tag_stream_label_rewrite_approvals: Sequence[
        TagStreamLabelRewriteApproval
    ] = (),
    digest_pin_label_rewrite_approvals: Sequence[
        DigestPinLabelRewriteApproval
    ] = (),
    host_docker_base: Path | None = None,
    environ: Mapping[str, str] | None = None,
    known_digest_provenance_by_service: _DigestProvenanceByService | None = None,
) -> DryRunPlan:
    return _build_dry_run_plan_from_pending_source(
        config,
        parsed,
        source_file=source_file,
        source_hash=source_hash,
        source=source,
        line_numbers=line_numbers,
        selection_scope=selection_scope,
        allow_tag_updates=allow_tag_updates,
        tag_stream_inputs=_TagStreamPlanInputs(
            tag_overrides=tag_overrides,
            decisions=tag_stream_decisions,
            label_rewrite_approvals=tag_stream_label_rewrite_approvals,
        ),
        digest_pin_label_rewrite_approvals=digest_pin_label_rewrite_approvals,
        host_docker_base=host_docker_base,
        environ=environ,
        known_digest_provenance_by_service=known_digest_provenance_by_service,
    )


def _build_dry_run_plan_from_pending_source(
    config: UpdaterConfig,
    parsed: ParsedWudFile,
    *,
    source_file: str,
    source_hash: str,
    source: DryRunPlanSource,
    line_numbers: Sequence[int],
    selection_scope: _PlanSelectionScope | None = None,
    allow_tag_updates: bool = False,
    tag_stream_inputs: _TagStreamPlanInputs | None = None,
    digest_pin_label_rewrite_approvals: Sequence[
        DigestPinLabelRewriteApproval
    ] = (),
    host_docker_base: Path | None = None,
    environ: Mapping[str, str] | None = None,
    known_digest_provenance_by_service: _DigestProvenanceByService | None = None,
) -> DryRunPlan:
    runner = CommandRunner(env=environ) if environ is not None else CommandRunner()
    selections = selection_scope or _PlanSelectionScope()
    stream_inputs = tag_stream_inputs or _TagStreamPlanInputs()
    return _PlanBuilder(
        config=config,
        line_numbers=line_numbers,
        update_selections=selections.update_selections,
        completed_update_selections=selections.completed_update_selections,
        allow_tag_updates=allow_tag_updates,
        tag_overrides=stream_inputs.tag_overrides,
        tag_stream_decisions=stream_inputs.decisions,
        tag_stream_label_rewrite_approvals=stream_inputs.label_rewrite_approvals,
        digest_pin_label_rewrite_approvals=digest_pin_label_rewrite_approvals,
        host_docker_base=host_docker_base,
        command_runner=runner,
        source_parsed=parsed,
        source_file=source_file,
        source_hash=source_hash,
        source=source,
        known_digest_provenance_by_service=known_digest_provenance_by_service or {},
    ).build()


def build_unmatched_cleanup(
    config: UpdaterConfig,
    *,
    line_numbers: Sequence[int],
    parsed: ParsedWudFile | None = None,
    host_docker_base: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> DryRunPlanCleanup:
    selected = _selected_line_numbers(line_numbers)
    full_parse = parsed or _read_wud_file(config.wud_out_file)
    _validate_selected_targets(full_parse, selected)
    selected_parse = _parsed_for_selected_lines(full_parse, selected)
    runner = CommandRunner(env=environ) if environ is not None else CommandRunner()
    docker = DockerCli(runner=runner)
    compose = ComposeCli(runner=runner)

    try:
        stacks = compose.discover_stacks(
            config.docker_base,
            project_base=host_docker_base,
            ignore_paths=config.compose_ignore_paths,
        )
    except ComposeDiscoveryError:
        return DryRunPlanCleanup()

    _matches, skipped = _match_targets(
        selected_parse,
        stacks,
        docker,
        allow_tag_updates=True,
        allow_digest_pin_rematch=config.digest_pin_updates,
    )
    diagnostics = _unmatched_diagnostics(
        config,
        selected_parse.targets,
        skipped,
        docker,
        host_docker_base=host_docker_base,
    )
    return _cleanup_for_skipped(
        config,
        selected_parse.targets,
        skipped,
        diagnostics,
        host_docker_base=host_docker_base,
    )


def resolve_pending_groups(
    config: UpdaterConfig,
    parsed: ParsedWudFile,
    *,
    host_docker_base: Path | None = None,
    environ: Mapping[str, str] | None = None,
    known_digest_provenance_by_service: _DigestProvenanceByService | None = None,
    completed_update_selections: Sequence[CompletedUpdateSelection] = (),
) -> PendingGroupingResult:
    runner = CommandRunner(env=environ) if environ is not None else CommandRunner()
    docker = DockerCli(runner=runner)
    compose = ComposeCli(runner=runner)

    try:
        stacks = compose.discover_stacks(
            config.docker_base,
            project_base=host_docker_base,
            ignore_paths=config.compose_ignore_paths,
        )
    except ComposeDiscoveryError as exc:
        return PendingGroupingResult(
            status="unavailable",
            unmatched=tuple(
                _pending_grouping_item(
                    target,
                    action=_target_action_name(target),
                    digest_provenance=_pending_digest_provenance(target),
                )
                for target in parsed.targets
            ),
            warnings=(str(exc),),
        )

    scope_builder = _PlanBuilder(
        config,
        (),
        allow_tag_updates=True,
        host_docker_base=host_docker_base,
        command_runner=runner,
        known_digest_provenance_by_service=known_digest_provenance_by_service or {},
    )
    matches, skipped, _digest_unpin_issues = scope_builder._build_matches(parsed, stacks)
    matches = filter_matches_for_completed_update_selections(
        matches,
        completed_update_selections,
    )
    scope_builder._filter_digest_unpin_updates(matches)
    diagnostics = _unmatched_diagnostics(
        config,
        parsed.targets,
        skipped,
        docker,
        host_docker_base=host_docker_base,
    )
    targets_by_line = {target.line_no: target for target in parsed.targets}
    return PendingGroupingResult(
        status="ready",
        groups=_pending_stack_groups(
            matches,
            scope_builder=scope_builder,
            digest_unpin_updates_by_stack=scope_builder.digest_unpin_updates_by_stack,
            digest_pin_updates_enabled=config.digest_pin_updates,
        ),
        unmatched=tuple(
            _pending_grouping_item(
                targets_by_line[item.line_no],
                action=item.reason,
                diagnostic=diagnostics.get(item.line_no),
                digest_provenance=_pending_digest_provenance(
                    targets_by_line[item.line_no]
                ),
            )
            for item in skipped
            if item.line_no in targets_by_line
        ),
    )


def _parsed_for_selected_lines(
    parsed: ParsedWudFile,
    selected: Sequence[int],
) -> ParsedWudFile:
    selected_set = set(selected)
    return ParsedWudFile(
        lines=parsed.lines,
        targets=tuple(
            target for target in parsed.targets if target.line_no in selected_set
        ),
        warnings=parsed.warnings,
    )


def _apply_selected_tags(
    parsed: ParsedWudFile,
    selected_tags_by_line: Mapping[int, str],
) -> ParsedWudFile:
    if not selected_tags_by_line:
        return parsed
    return ParsedWudFile(
        lines=parsed.lines,
        targets=tuple(
            replace(
                target,
                desired_tag=selected_tags_by_line.get(
                    target.line_no,
                    target.desired_tag,
                ),
            )
            for target in parsed.targets
        ),
        warnings=parsed.warnings,
    )


def _replace_match_targets(
    matches: Sequence[Match],
    parsed: ParsedWudFile,
    *,
    preserve_desired_tag_lines: Mapping[int, str] | None = None,
) -> list[Match]:
    targets_by_line = {target.line_no: target for target in parsed.targets}
    preserved_lines = preserve_desired_tag_lines or {}
    return [
        replace(
            match,
            target=(
                replace(
                    targets_by_line.get(match.target.line_no, match.target),
                    desired_tag=match.target.desired_tag,
                )
                if match.target.line_no in preserved_lines
                else targets_by_line.get(match.target.line_no, match.target)
            ),
        )
        for match in matches
    ]


def _digest_unpin_updates_for_stack(
    stack_index: int,
    updates_by_stack: _DigestUnpinUpdatesByStack | None,
) -> Sequence[DigestUnpinUpdate]:
    if updates_by_stack is None:
        return ()
    return updates_by_stack.get(stack_index, ())


def _project_directory_text(project_directory: Path | None) -> str:
    if project_directory is None:
        return ""
    return str(project_directory)


def _pending_stack_groups(
    matches: Sequence[Match],
    *,
    scope_builder: _PlanBuilder | None = None,
    digest_unpin_updates_by_stack: _DigestUnpinUpdatesByStack | None = None,
    digest_pin_updates_enabled: bool = True,
) -> tuple[PendingStackGroup, ...]:
    groups: list[PendingStackGroup] = []
    for stack in _stacks_to_update(matches):
        stack_matches = [match for match in matches if match.stack.index == stack.index]
        services = _ordered_unique(
            match.service for match in stack_matches if match.service
        )
        stack_action = ""
        if scope_builder is not None:
            scope = scope_builder._update_scope(stack, stack_matches)
            if scope.services is None:
                stack_action = "recreate_stack"
        items = _pending_grouping_items(
            stack_matches,
            stack=stack,
            scope_builder=scope_builder,
            stack_action=stack_action,
            digest_unpin_updates=_digest_unpin_updates_for_stack(
                stack.index,
                digest_unpin_updates_by_stack,
            ),
            digest_pin_updates_enabled=digest_pin_updates_enabled,
        )
        groups.append(
            PendingStackGroup(
                name=stack.name,
                directory=str(stack.directory),
                compose_file=stack.file,
                project_directory=_project_directory_text(stack.project_directory),
                project_name=stack.project_name,
                services_label=_pending_services_label(services),
                services=services,
                line_numbers=tuple(item.line_no for item in items),
                items=items,
            )
        )
    return tuple(groups)


def _pending_grouping_items(
    matches: Sequence[Match],
    *,
    stack: ComposeStack,
    scope_builder: _PlanBuilder | None = None,
    stack_action: str = "",
    digest_unpin_updates: Sequence[DigestUnpinUpdate] = (),
    digest_pin_updates_enabled: bool = True,
) -> tuple[PendingGroupingItem, ...]:
    items: list[PendingGroupingItem] = []
    for line_no in sorted({match.target.line_no for match in matches}):
        line_matches = [match for match in matches if match.target.line_no == line_no]
        target = line_matches[0].target
        resolved = _pending_normalized_resolved(
            line_matches[0],
            digest_pin_updates_enabled=digest_pin_updates_enabled,
        )
        compose_images = _ordered_unique(match.compose_image for match in line_matches)
        services = _ordered_unique(
            match.service for match in line_matches if match.service
        )
        runtime_services = services
        if scope_builder is not None:
            runtime_services = runtime_services_for_scope(
                scope_builder._update_scope(stack, line_matches),
            )
        digest_unpin = _pending_digest_unpin_for_matches(
            line_matches,
            digest_unpin_updates,
        )
        action = (
            "digest-unpin"
            if digest_unpin is not None
            else _target_action_name(target, services, stack_action=stack_action)
        )
        digest_provenance = _pending_grouping_digest_provenance(target, digest_unpin)
        items.append(
            _pending_grouping_item(
                target,
                resolved_image=resolved,
                compose_images=compose_images,
                services=services,
                runtime_services=runtime_services,
                action=action,
                digest_provenance=digest_provenance,
                target_image=_pending_digest_unpin_target_image(digest_unpin),
                digest_pin_updates_enabled=digest_pin_updates_enabled,
                selection_id=selection_id_for_matches(line_matches),
            )
        )
    return tuple(items)


def _pending_grouping_digest_provenance(
    target: WudTarget,
    digest_unpin: DigestUnpinUpdate | None,
) -> DigestTagProvenance | None:
    if digest_unpin is None:
        return _pending_digest_provenance(target)
    return digest_provenance_from_unpin_update(
        digest_unpin,
        provenance_source="plan",
        provenance_confidence="recovered",
    )


def _pending_digest_unpin_target_image(
    digest_unpin: DigestUnpinUpdate | None,
) -> str:
    if digest_unpin is None:
        return ""
    return digest_unpin.tag_image


def _pending_grouping_item(
    target: WudTarget,
    *,
    action: str,
    resolved_image: str = "",
    compose_images: Sequence[str] = (),
    services: Sequence[str] = (),
    runtime_services: Sequence[str] = (),
    diagnostic: UnmatchedDiagnostic | None = None,
    digest_provenance: DigestTagProvenance | None = None,
    target_image: str = "",
    digest_pin_updates_enabled: bool = True,
    selection_id: str = "",
) -> PendingGroupingItem:
    resolved = resolved_image or target.first
    resolved_target_image = _pending_grouping_target_image(
        target_image,
        target,
        resolved,
        compose_images,
        digest_pin_updates_enabled=digest_pin_updates_enabled,
    )
    tag_stream_hint = pending_tag_stream_hint(
        image_repo=target.repo,
        current_tag=image_tag(target.first),
        reported_tag=target.desired_tag,
    )
    return PendingGroupingItem(
        line_no=target.line_no,
        raw=target.raw,
        image=target.first,
        key=target.key,
        repo=target.repo,
        has_tag=target.has_tag,
        allow_repo=target.allow_repo,
        digest=target.digest,
        desired_tag=target.desired_tag,
        resolved_image=resolved,
        target_image=resolved_target_image,
        compose_images=tuple(compose_images),
        services=tuple(services),
        runtime_services=tuple(runtime_services),
        action=action,
        selection_id=selection_id,
        platform=target.platform_value,
        platform_os=target.platform.os if target.platform is not None else "",
        platform_architecture=(
            target.platform.architecture if target.platform is not None else ""
        ),
        platform_variant=target.platform.variant if target.platform is not None else "",
        diagnostic=diagnostic,
        digest_provenance=digest_provenance,
        tag_stream=(
            None
            if tag_stream_hint is None
            else PendingTagStream(
                current_stream=tag_stream_hint.current_stream,
                reported_stream=tag_stream_hint.reported_stream,
            )
        ),
    )


def _issue_applies_to_matches(
    issue: DryRunPlanIssue,
    matches: Sequence[Match],
) -> bool:
    if not issue.stack and not issue.service and issue.line_no is None:
        return True
    return any(
        (issue.line_no is None or issue.line_no == match.target.line_no)
        and (not issue.stack or issue.stack == match.stack.name)
        and (not issue.service or issue.service == match.service)
        for match in matches
    )


def _pending_grouping_target_image(
    target_image: str,
    target: WudTarget,
    resolved_image: str,
    compose_images: Sequence[str],
    *,
    digest_pin_updates_enabled: bool,
) -> str:
    if target_image:
        return target_image
    return _pending_target_image(
        target,
        resolved_image,
        compose_images,
        digest_pin_updates_enabled=digest_pin_updates_enabled,
    )


def _pending_digest_provenance(target: WudTarget) -> DigestTagProvenance | None:
    if not is_digest_target_line(target):
        return None
    return digest_provenance_from_digest_target(
        target.first,
        target.digest,
        provenance_source="compose",
        provenance_confidence="recovered",
    )


def _pending_digest_unpin_for_matches(
    matches: Sequence[Match],
    updates: Sequence[DigestUnpinUpdate],
) -> DigestUnpinUpdate | None:
    for match in matches:
        key = (
            match.compose_image,
            image_tag(match.target.first),
            normalize_digest(match.target.digest),
        )
        for update in updates:
            if (update.old_image, update.resolved_tag, update.target_digest) != key:
                continue
            if match.service and match.service not in update.services:
                continue
            return update
    return None


def _pending_normalized_resolved(
    match: Match,
    *,
    digest_pin_updates_enabled: bool,
) -> str:
    if not digest_pin_updates_enabled and is_digest_target_line(match.target):
        return image_with_tag(match.compose_image, image_tag(match.target.first))
    return match.resolved


def _pending_target_image(
    target: WudTarget,
    resolved_image: str,
    compose_images: Sequence[str],
    *,
    digest_pin_updates_enabled: bool = True,
) -> str:
    base_image = compose_images[0] if compose_images else resolved_image
    if target.desired_tag:
        return image_with_tag(base_image, target.desired_tag)
    if _preserve_digest_tag(target, digest_pin_updates_enabled):
        return image_with_tag(base_image, image_tag(target.first))
    return resolved_image


def _preserve_digest_tag(
    target: WudTarget,
    digest_pin_updates_enabled: bool,
) -> bool:
    return not digest_pin_updates_enabled and is_digest_target_line(target)


def _target_action_name(
    target: WudTarget,
    services: Sequence[str] = (),
    *,
    stack_action: str = "",
) -> str:
    if target.desired_tag:
        return "tag-update"
    if stack_action:
        return stack_action
    return "recreate_service" if services else "recreate_stack"


def _pending_services_label(services: Sequence[str]) -> str:
    return ", ".join(services) if services else "stack-level"


def _read_wud_file(path: Path) -> ParsedWudFile:
    try:
        return parse_wud_file(path)
    except FileNotFoundError as exc:
        raise PlanFileMissing(f"WUD file not found: {path}") from exc


def _selected_line_numbers(line_numbers: Sequence[int]) -> tuple[int, ...]:
    selected = tuple(sorted(set(line_numbers)))
    if not selected:
        raise PlanInputError("line_numbers must not be empty")
    invalid = [line_no for line_no in selected if line_no < 1]
    if invalid:
        values = ", ".join(str(line_no) for line_no in invalid)
        raise PlanInputError(f"line_numbers must be positive integers: {values}")
    return selected


def _validate_selected_targets(
    parsed: ParsedWudFile,
    selected: Sequence[int],
) -> None:
    target_lines = {target.line_no for target in parsed.targets}
    missing = [line_no for line_no in selected if line_no not in target_lines]
    if missing:
        values = ", ".join(str(line_no) for line_no in missing)
        raise PlanInputError(
            "line_numbers must reference actionable WUD target lines: " + values
        )


def _target_action(
    target: WudTarget,
    matched: bool,
    skipped: DryRunPlanSkipped | None,
) -> str:
    if skipped is not None:
        return skipped.reason
    if target.desired_tag and matched:
        return "tag-update"
    if matched:
        return "update"
    return "unmatched"


def _plan_status(
    matches: Sequence[Match],
    skipped: Sequence[DryRunPlanSkipped],
    issues: Sequence[DryRunPlanIssue],
) -> str:
    if any(issue.severity == "error" for issue in issues):
        return "blocked"
    if not matches:
        return "empty"
    if skipped:
        return "blocked"
    return "ready"


def _service_count(stacks: Sequence[DryRunPlanStack]) -> int:
    services: set[tuple[str, str]] = set()
    for stack in stacks:
        for service in stack.services or stack.pull_services:
            services.add((stack.name, service))
    return len(services)
