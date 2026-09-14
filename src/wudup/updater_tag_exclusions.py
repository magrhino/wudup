"""Tag-exclusion planning and application helpers for the updater."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import compose_rewrite, updater_audit
from .compose import ComposeStack
from .images import (
    image_has_tag,
    image_matches_resolved_target,
    image_with_tag,
    repo_key,
)
from .updater_matching import (
    _expand_network_mode_services,
    _first_match_by_line,
    _network_mode_providers,
    _ordered_unique,
    _services_for_image,
)
from .updater_models import (
    ComposeTagRewriteError,
    Match,
    StackStatus,
    TagExclusionUpdate,
)
from .updater_planning import (
    _tag_exclusion_updates_by_stack,
    _unique_tag_exclusion_updates,
)
from .wud_file import ParsedWudFile, WudTarget


def print_tag_exclusions(runner: Any, parsed: ParsedWudFile) -> None:
    if not parsed.targets:
        return
    runner.log.info("Tag exclusions requested:")
    for target in parsed.targets:
        if target.desired_tag:
            desired_image = image_with_tag(target.first, target.desired_tag)
            runner.log.info(
                f"  line {target.line_no}: exclude {target.desired_tag} "
                f"for {desired_image}"
            )
        else:
            runner.log.info(f"  line {target.line_no}: cannot exclude {target.first}")


def print_tag_exclusion_plan(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
    failures: Sequence[tuple[WudTarget, str]],
) -> None:
    if updates:
        runner.log.info("Tag exclusions to write:")
        seen: set[tuple[str, str, str, str]] = set()
        for update in updates:
            key = (update.stack.name, update.service, update.image_repo, update.tag)
            if key in seen:
                continue
            seen.add(key)
            runner.log.info(
                f"  [{update.stack.name}] {update.service}: "
                f"exclude {update.tag} for {update.image_repo} "
                f"({update.scope})"
            )
    for target, reason in failures:
        runner.log.warning(
            f"Tag exclusion for line {target.line_no} could not be planned: "
            f"{reason}"
        )


def build_tag_exclusion_matches(
    runner: Any,
    parsed: ParsedWudFile,
    stacks: Sequence[ComposeStack],
) -> tuple[list[Match], list[tuple[WudTarget, str]]]:
    container_images = _container_images_by_name(runner)
    matches: list[Match] = []
    failures: list[tuple[WudTarget, str]] = []
    seen: set[tuple[int, int, str, str, str]] = set()

    for target in parsed.targets:
        if not target.desired_tag:
            failures.append((target, "not-a-tag-update"))
            continue

        resolved, allow_repo = _resolve_tag_exclusion_target(target, container_images)
        target_matches = _tag_exclusion_matches_for_target(
            target,
            resolved,
            allow_repo,
            stacks,
        )
        _append_unique_matches(matches, target_matches, seen)

        if not target_matches:
            failures.append((target, "unmatched"))

    matches.sort(key=_match_sort_key)
    return matches, failures


def _container_images_by_name(runner: Any) -> dict[str, str]:
    return {item.name: item.image for item in runner.docker.try_container_images()}


def _resolve_tag_exclusion_target(
    target: WudTarget,
    container_images: Mapping[str, str],
) -> tuple[str, bool]:
    resolved = container_images.get(target.first, target.first)
    allow_repo = target.allow_repo or resolved != target.first or not image_has_tag(resolved)
    return resolved, allow_repo


def _tag_exclusion_matches_for_target(
    target: WudTarget,
    resolved: str,
    allow_repo: bool,
    stacks: Sequence[ComposeStack],
) -> list[Match]:
    matches: list[Match] = []
    for stack in stacks:
        for image in stack.images:
            matches.extend(
                Match(stack, target, resolved, image, service)
                for service in _tag_exclusion_services_for_image(
                    stack,
                    image,
                    resolved,
                    allow_repo,
                )
            )
    return matches


def _tag_exclusion_services_for_image(
    stack: ComposeStack,
    image: str,
    resolved: str,
    allow_repo: bool,
) -> tuple[str, ...]:
    if not image_matches_resolved_target(image, resolved, allow_repo):
        return ()
    return _services_for_image(stack.service_images, image)


def _append_unique_matches(
    matches: list[Match],
    candidates: Sequence[Match],
    seen: set[tuple[int, int, str, str, str]],
) -> None:
    for match in candidates:
        key = _match_dedupe_key(match)
        if key in seen:
            continue
        matches.append(match)
        seen.add(key)


def _match_dedupe_key(match: Match) -> tuple[int, int, str, str, str]:
    return (
        match.stack.index,
        match.target.line_no,
        match.resolved,
        match.compose_image,
        match.service,
    )


def _match_sort_key(match: Match) -> tuple[int, int, str, str, str, str]:
    return (
        match.stack.index,
        match.target.line_no,
        match.target.first,
        match.resolved,
        match.compose_image,
        match.service,
    )


def plan_tag_exclusions(
    runner: Any,
    matches: Sequence[Match],
    stacks: Sequence[ComposeStack],
) -> tuple[list[TagExclusionUpdate], list[tuple[WudTarget, str]]]:
    updates: list[TagExclusionUpdate] = []
    failures: list[tuple[WudTarget, str]] = []

    by_line = _first_match_by_line(matches)
    for line_no in sorted(by_line):
        first_match = by_line[line_no]
        line_matches = [match for match in matches if match.target.line_no == line_no]
        if not all(match.service for match in line_matches):
            failures.append((first_match.target, "service-unmapped"))
            continue

        image_repo = repo_key(first_match.compose_image)
        repo_updates = runner._tag_exclusion_repo_updates(
            stacks,
            image_repo=image_repo,
            tag=first_match.target.desired_tag,
            source_line=line_no,
        )
        if repo_updates and runner._can_apply_tag_exclusions(repo_updates):
            updates.extend(repo_updates)
            continue

        service_updates = [
            TagExclusionUpdate(
                stack=match.stack,
                service=match.service,
                image=match.compose_image,
                image_repo=repo_key(match.compose_image),
                tag=match.target.desired_tag,
                source_line=line_no,
                scope="service",
            )
            for match in line_matches
            if match.service
        ]
        if service_updates and runner._can_apply_tag_exclusions(service_updates):
            updates.extend(service_updates)
        else:
            failures.append((first_match.target, "compose-label-unsupported"))

    return _unique_tag_exclusion_updates(updates), failures


def tag_exclusion_repo_updates(
    stacks: Sequence[ComposeStack],
    *,
    image_repo: str,
    tag: str,
    source_line: int,
) -> list[TagExclusionUpdate]:
    updates: list[TagExclusionUpdate] = []
    for stack in stacks:
        for service_image in stack.service_images:
            if repo_key(service_image.image) != image_repo:
                continue
            updates.append(
                TagExclusionUpdate(
                    stack=stack,
                    service=service_image.service,
                    image=service_image.image,
                    image_repo=image_repo,
                    tag=tag,
                    source_line=source_line,
                    scope="image_repo",
                )
            )
    return updates


def can_apply_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> bool:
    try:
        for stack, stack_updates in _tag_exclusion_updates_by_stack(updates).items():
            existing_exact_tags = runner._existing_exact_tag_exclusions(stack_updates)
            compose_rewrite.render_compose_tag_exclusions(
                stack.directory / stack.file,
                stack_updates,
                existing_exact_tags=existing_exact_tags,
            )
    except ComposeTagRewriteError:
        return False
    return True


def apply_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> dict[tuple[int, int], StackStatus]:
    statuses = {
        (update.stack.index, update.source_line): StackStatus("success", "tag-excluded")
        for update in updates
    }
    if not updates:
        return statuses

    successful_updates: list[TagExclusionUpdate] = []
    for stack, stack_updates in _tag_exclusion_updates_by_stack(updates).items():
        existing_exact_tags = runner._existing_exact_tag_exclusions(stack_updates)
        try:
            applied = compose_rewrite.apply_compose_tag_exclusions(
                stack.directory / stack.file,
                stack_updates,
                existing_exact_tags=existing_exact_tags,
            )
        except ComposeTagRewriteError as exc:
            runner.log.error(
                f"[{stack.name}] Could not safely write wud.tag.exclude: {exc}"
            )
            for update in stack_updates:
                statuses[(update.stack.index, update.source_line)] = StackStatus(
                    "failure",
                    "tag-exclusion-label-failed",
                )
            continue
        except OSError as exc:
            runner.log.error(f"[{stack.name}] Could not write wud.tag.exclude: {exc}")
            for update in stack_updates:
                statuses[(update.stack.index, update.source_line)] = StackStatus(
                    "failure",
                    "tag-exclusion-label-failed",
                )
            continue

        for item in applied:
            runner.log.info(
                f"[{stack.name}] Updated wud.tag.exclude for service "
                f"{item.service}: {', '.join(item.tags)}"
            )
        runner._record_tag_exclusion_rules(stack_updates)
        successful_updates.extend(
            _applied_tag_exclusion_updates(stack_updates, applied)
        )

    if runner.options.recreate_excluded_services:
        runner._recreate_tag_exclusion_services(successful_updates, statuses)
    return statuses


def _applied_tag_exclusion_updates(
    stack_updates: Sequence[TagExclusionUpdate],
    applied: Sequence[Any],
) -> list[TagExclusionUpdate]:
    applied_services = {item.service for item in applied}
    return [update for update in stack_updates if update.service in applied_services]


def existing_exact_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> dict[str, set[str]]:
    return updater_audit.existing_exact_tag_exclusions(runner, updates)


def record_tag_exclusion_rules(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> None:
    updater_audit.record_tag_exclusion_rules(runner, updates)


def recreate_tag_exclusion_services(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
    statuses: dict[tuple[int, int], StackStatus],
) -> None:
    for stack, stack_updates in _tag_exclusion_updates_by_stack(updates).items():
        services = tuple(sorted({update.service for update in stack_updates}))
        network_providers = _network_mode_providers(stack.service_images)
        up_services, uses_network_provider = _expand_network_mode_services(
            services,
            network_providers,
        )
        missing_providers = runner._missing_network_mode_providers(
            stack,
            services,
            network_providers,
        )
        if missing_providers:
            up_services = _ordered_unique((*missing_providers, *up_services))
            uses_network_provider = True
        result = runner._run_compose_up(
            stack,
            up_services,
            no_deps=not uses_network_provider,
        )
        if result.ok and (
            result.wait_handled or runner._wait_for_health(stack, up_services)
        ):
            continue
        for update in stack_updates:
            statuses[(update.stack.index, update.source_line)] = StackStatus(
                "failure",
                "tag-exclusion-recreate-failed",
            )


def mark_tag_exclusions_pending(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> None:
    updater_audit.mark_tag_exclusions_pending(runner, updates)


def mark_successful_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
    statuses: Mapping[tuple[int, int], StackStatus],
) -> None:
    updater_audit.mark_successful_tag_exclusions(runner, updates, statuses)


def mark_tag_exclusion_failures(
    runner: Any,
    failures: Sequence[tuple[WudTarget, str]],
) -> None:
    updater_audit.mark_tag_exclusion_failures(runner, failures)
