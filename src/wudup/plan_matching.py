"""Target matching and unmatched cleanup helpers for WebUI plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .compose import COMPOSE_FILENAMES, ComposeStack
from .config import UpdaterConfig
from .docker_cli import DockerCli
from .images import image_has_tag, image_matches_resolved_target
from .plan_models import (
    DryRunPlanCleanup,
    DryRunPlanCleanupItem,
    DryRunPlanSkipped,
    PlanInputError,
    UnmatchedDiagnostic,
)
from .updater_matching import _services_for_target_match
from .updater_models import Match, UpdateSelection
from .wud_file import ParsedWudFile, WudTarget

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
SELECTION_ID_PREFIX = "sel-v1-"
UNMATCHED_HINT = (
    "Preflight found a matching running container, but its Docker Compose "
    "labels do not point to an active supported Compose file. Restore or "
    "rename the active Compose file, update discovery settings if the stack "
    "moved, or remove the stale WUD line."
)
GENERIC_UNMATCHED_MESSAGE = (
    "This pending update no longer matches any discovered Compose service."
)
GENERIC_UNMATCHED_HINT = (
    "Preflight did not find a matching Compose service or running Docker "
    "container. Likely causes are service removal, image rename, or a tag "
    "that was already applied."
)
GENERIC_UNMATCHED_FINDINGS = (
    "No discovered Compose service matched this pending line.",
    "No running Docker container matched this pending line.",
)
GENERIC_UNMATCHED_POSSIBLE_REASONS = (
    "The Compose service was removed or renamed.",
    "The Compose image name changed.",
    "The update tag was already applied and WUD left the old pending line behind.",
)
GENERIC_UNMATCHED_RECOMMENDED_ACTIONS = (
    "Remove the stale WUD line when the service is intentionally gone or already updated.",
    "If the service should still be managed, update the WUD line or stack image to the current service/image name.",
)
COMPOSE_LABEL_UNDISCOVERED_HINT = (
    "Preflight found a matching running container and active Compose labels, "
    "but Compose discovery did not include that stack. Check Docker base and "
    "ignored paths before removing the WUD line."
)
COMPOSE_LABEL_UNDISCOVERED_POSSIBLE_REASONS = (
    "The stack moved outside the configured Docker base.",
    "The stack is excluded by Compose ignore paths.",
    "Compose discovery is pointed at a different project directory.",
)
COMPOSE_LABEL_UNDISCOVERED_RECOMMENDED_ACTIONS = (
    "Update Docker base or ignore paths so discovery includes the stack.",
    "Move the stack back under the discovered Docker base if it should be managed.",
    "Remove the stale WUD line if the stack is intentionally unmanaged.",
)
MATCHING_CONTAINER_UNLABELED_HINT = (
    "Preflight found a matching running container, but Docker did not report "
    "Compose config labels for it. The line cannot be tied to a discovered stack."
)
MATCHING_CONTAINER_UNLABELED_POSSIBLE_REASONS = (
    "The container is not managed by Docker Compose.",
    "Compose labels are missing or unavailable on the running container.",
)
MATCHING_CONTAINER_UNLABELED_RECOMMENDED_ACTIONS = (
    "Inspect the container source before removing the line.",
    "Remove the stale WUD line if this container should not be managed by WUDup.",
)


def _match_targets(
    parsed: ParsedWudFile,
    stacks: Sequence[ComposeStack],
    docker: DockerCli,
    *,
    allow_tag_updates: bool,
    allow_digest_pin_rematch: bool,
) -> tuple[list[Match], list[DryRunPlanSkipped]]:
    container_images = {item.name: item.image for item in docker.try_container_images()}
    matches: list[Match] = []
    skipped: list[DryRunPlanSkipped] = []
    seen: set[tuple[int, int, str, str, str]] = set()

    for target in parsed.targets:
        if target.desired_tag and not allow_tag_updates:
            skipped.append(_skipped(target, "tag-updates-disabled"))
            continue
        matches.extend(
            _matches_for_target(
                target,
                stacks,
                container_images,
                allow_digest_pin_rematch=allow_digest_pin_rematch,
                seen=seen,
            )
        )

    matches.sort(
        key=lambda item: (
            item.stack.index,
            item.target.line_no,
            item.target.first,
            item.resolved,
            item.compose_image,
            item.service,
        )
    )
    matched_lines = {match.target.line_no for match in matches}
    skipped_lines = {item.line_no for item in skipped}
    for target in parsed.targets:
        if target.line_no not in matched_lines and target.line_no not in skipped_lines:
            skipped.append(_skipped(target, "unmatched"))
    return matches, skipped


def normalize_update_selections(
    selections: Sequence[UpdateSelection],
) -> tuple[UpdateSelection, ...]:
    normalized: list[UpdateSelection] = []
    seen: set[tuple[int, str]] = set()
    modes_by_line: dict[int, set[bool]] = {}
    for item in selections:
        if item.line_no < 1:
            raise PlanInputError(
                f"selections line numbers must be positive integers: {item.line_no}"
            )
        key = (item.line_no, item.selection_id)
        if key in seen:
            raise PlanInputError(
                f"selection for line {item.line_no} was provided more than once"
            )
        seen.add(key)
        modes_by_line.setdefault(item.line_no, set()).add(bool(item.selection_id))
        normalized.append(item)
    mixed = sorted(
        line_no for line_no, modes in modes_by_line.items() if len(modes) > 1
    )
    if mixed:
        values = ", ".join(str(line_no) for line_no in mixed)
        raise PlanInputError(
            "selections cannot mix line-wide and stack-scoped entries for line(s): "
            + values
        )
    return tuple(
        sorted(normalized, key=lambda item: (item.line_no, item.selection_id))
    )


def selection_id_for_matches(matches: Sequence[Match]) -> str:
    payload = _selection_identity_payload(matches)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{SELECTION_ID_PREFIX}{digest}"


def pending_target_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _selection_identity_payload(matches: Sequence[Match]) -> dict[str, object]:
    if not matches:
        raise ValueError("selection identity requires at least one match")
    first = matches[0]
    if any(
        item.stack.index != first.stack.index
        or item.target.line_no != first.target.line_no
        for item in matches
    ):
        raise ValueError("selection identity matches must share a stack and line")
    stack_directory = first.stack.directory.resolve(strict=False)
    return {
        "compose_file": str(
            (stack_directory / first.stack.file).resolve(strict=False)
        ),
        "compose_images": sorted({item.compose_image for item in matches}),
        "directory": str(stack_directory),
        "line_no": first.target.line_no,
        "project_directory": (
            "" if first.stack.project_directory is None
            else str(first.stack.project_directory.resolve(strict=False))
        ),
        "project_name": first.stack.project_name,
        "services": sorted({item.service for item in matches}),
        "target": first.target.raw,
    }


def filter_matches_for_selections(
    matches: Sequence[Match],
    selections: Sequence[UpdateSelection],
) -> tuple[list[Match], tuple[UpdateSelection, ...]]:
    normalized = normalize_update_selections(selections)
    if not normalized:
        return list(matches), ()

    matches_by_line: dict[int, list[Match]] = {}
    grouped_matches: dict[tuple[int, int], list[Match]] = {}
    for match in matches:
        matches_by_line.setdefault(match.target.line_no, []).append(match)
        grouped_matches.setdefault(
            (match.stack.index, match.target.line_no),
            [],
        ).append(match)
    matches_by_selection_id = {
        selection_id_for_matches(group): group
        for group in grouped_matches.values()
    }

    selected: list[Match] = []
    for item in normalized:
        if not item.selection_id:
            selected.extend(matches_by_line.get(item.line_no, ()))
            continue
        scoped = matches_by_selection_id.get(item.selection_id)
        if scoped is None or scoped[0].target.line_no != item.line_no:
            raise PlanInputError(
                f"selection for line {item.line_no} is stale or no longer available"
            )
        selected.extend(scoped)

    selected_keys = {
        (
            item.stack.index,
            item.target.line_no,
            item.resolved,
            item.compose_image,
            item.service,
        )
        for item in selected
    }
    return (
        [
            item
            for item in matches
            if (
                item.stack.index,
                item.target.line_no,
                item.resolved,
                item.compose_image,
                item.service,
            )
            in selected_keys
        ],
        normalized,
    )


def partially_selected_line_numbers(
    all_matches: Sequence[Match],
    selected_matches: Sequence[Match],
) -> tuple[int, ...]:
    all_by_line: dict[int, set[tuple[int, str, str]]] = {}
    selected_by_line: dict[int, set[tuple[int, str, str]]] = {}
    for match in all_matches:
        all_by_line.setdefault(match.target.line_no, set()).add(
            (match.stack.index, match.compose_image, match.service)
        )
    for match in selected_matches:
        selected_by_line.setdefault(match.target.line_no, set()).add(
            (match.stack.index, match.compose_image, match.service)
        )
    return tuple(
        line_no
        for line_no in sorted(selected_by_line)
        if selected_by_line[line_no] != all_by_line.get(line_no, set())
    )


def _matches_for_target(
    target: WudTarget,
    stacks: Sequence[ComposeStack],
    container_images: Mapping[str, str],
    *,
    allow_digest_pin_rematch: bool,
    seen: set[tuple[int, int, str, str, str]],
) -> list[Match]:
    resolved = container_images.get(target.first, target.first)
    allow_repo = (
        target.allow_repo or resolved != target.first or not image_has_tag(resolved)
    )
    matches: list[Match] = []

    for stack in stacks:
        for image in stack.images:
            services = _services_for_target_match(
                stack.service_images,
                image,
                target,
                resolved,
                allow_repo,
                allow_digest_pin_rematch=allow_digest_pin_rematch,
            )
            if services is None:
                continue
            matches.extend(
                _deduped_matches_for_services(
                    stack,
                    target,
                    resolved,
                    image,
                    services or ("",),
                    seen,
                )
            )
    return matches


def _deduped_matches_for_services(
    stack: ComposeStack,
    target: WudTarget,
    resolved: str,
    image: str,
    services: Sequence[str],
    seen: set[tuple[int, int, str, str, str]],
) -> list[Match]:
    matches: list[Match] = []
    for service in services:
        key = (stack.index, target.line_no, resolved, image, service)
        if key in seen:
            continue
        matches.append(Match(stack, target, resolved, image, service))
        seen.add(key)
    return matches


def _unmatched_diagnostics(
    config: UpdaterConfig,
    targets: Sequence[WudTarget],
    skipped: Sequence[DryRunPlanSkipped],
    docker: DockerCli,
    *,
    host_docker_base: Path | None,
) -> dict[int, UnmatchedDiagnostic]:
    skipped_reasons = {item.line_no: item.reason for item in skipped}
    unmatched_targets = [
        target for target in targets if skipped_reasons.get(target.line_no) == "unmatched"
    ]
    if not unmatched_targets:
        return {}

    containers = docker.try_container_images()
    diagnostics: dict[int, UnmatchedDiagnostic] = {}
    for target in unmatched_targets:
        diagnostics[target.line_no] = _generic_unmatched_diagnostic()
        for container in containers:
            if not _container_matches_target(container.name, container.image, target):
                continue
            diagnostic = _compose_label_diagnostic(
                config,
                docker,
                container.name,
                container.image,
                host_docker_base=host_docker_base,
            )
            diagnostics[target.line_no] = diagnostic
            break
    return diagnostics


def _container_matches_target(
    container_name: str,
    container_image: str,
    target: WudTarget,
) -> bool:
    if container_name == target.first:
        return True
    allow_repo = target.allow_repo or not image_has_tag(target.first)
    return image_matches_resolved_target(container_image, target.first, allow_repo)


def _compose_label_diagnostic(
    config: UpdaterConfig,
    docker: DockerCli,
    container_name: str,
    container_image: str,
    *,
    host_docker_base: Path | None,
) -> UnmatchedDiagnostic:
    working_dir = _container_label(docker, container_name, COMPOSE_WORKING_DIR_LABEL)
    config_files = _split_compose_config_files(
        _container_label(docker, container_name, COMPOSE_CONFIG_FILES_LABEL)
    )
    if not config_files:
        return _matching_container_unlabeled_diagnostic(container_name, container_image)

    project = _container_label(docker, container_name, COMPOSE_PROJECT_LABEL)
    service = _container_label(docker, container_name, COMPOSE_SERVICE_LABEL)
    stack = project or _stack_name_from_label_path(working_dir) or _stack_name_from_label_path(
        config_files[0]
    )
    references = tuple(
        _display_label_path(path, config, host_docker_base=host_docker_base)
        for path in config_files
    )
    local_paths = tuple(
        _local_label_path(
            path,
            working_dir,
            config,
            host_docker_base=host_docker_base,
        )
        for path in config_files
    )
    if any(path is not None and path.is_file() for path in local_paths):
        return _compose_label_undiscovered_diagnostic(
            container_name,
            container_image,
            references,
            stack=stack,
            service=service,
        )

    found_files = _nonstandard_compose_files(
        local_paths,
        config,
        host_docker_base=host_docker_base,
    )
    reference_label = _join_display_values(references)
    if found_files:
        message = (
            "No active Compose file matched this WUD entry. Docker labels "
            f"reference {reference_label}, but only archived/nonstandard "
            f"compose files were found: {_join_display_values(found_files)}."
        )
    else:
        message = (
            "No active Compose file matched this WUD entry. Docker labels "
            f"reference {reference_label}, but the active compose file was not found."
        )
    findings = (
        f"Running container {container_name} still matches this pending line.",
        f"Docker labels reference {_join_display_values(references)}.",
        (
            "The referenced Compose file was not found, but archived/nonstandard "
            f"file(s) were found: {_join_display_values(found_files)}."
            if found_files
            else "The referenced Compose file was not found."
        ),
    )
    possible_reasons = (
        (
            "The active Compose file was renamed to an archived or nonstandard filename.",
            "The stack was moved or the Compose file path changed after the container was created.",
        )
        if found_files
        else (
            "The referenced Compose file was deleted or moved.",
            "The stack path is no longer mounted or reachable from WUDup.",
            "The stack moved outside the configured Docker base.",
        )
    )
    recommended_actions = (
        "Restore or rename the active Compose file to a supported Compose filename.",
        "Update Docker base or ignore paths if the stack moved.",
        "Remove the stale WUD line if the stack is intentionally gone.",
    )
    details = _stale_pending_assistant_details(
        preflight_findings=findings,
        possible_reasons=possible_reasons,
        recommended_actions=recommended_actions,
        referenced_compose_files=references,
        found_compose_files=found_files,
    )
    return UnmatchedDiagnostic(
        code="compose-label-active-file-missing",
        message=message,
        hint=UNMATCHED_HINT,
        stack=stack,
        service=service,
        compose_file=references[0] if references else "",
        found_files=found_files,
        details=details,
    )


def _compose_label_undiscovered_diagnostic(
    container_name: str,
    container_image: str,
    references: Sequence[str],
    *,
    stack: str,
    service: str,
) -> UnmatchedDiagnostic:
    reference_label = _join_display_values(references)
    findings = (
        f"Running container {container_name} still matches this pending line.",
        f"Docker labels reference active Compose file {reference_label}.",
        "Compose discovery did not include that stack.",
    )
    return UnmatchedDiagnostic(
        code="compose-label-undiscovered-active-file",
        message=(
            "A running container still matches this WUD entry and its Compose "
            f"file exists, but Compose discovery did not include {reference_label}."
        ),
        hint=COMPOSE_LABEL_UNDISCOVERED_HINT,
        stack=stack,
        service=service,
        compose_file=references[0] if references else "",
        details=_stale_pending_assistant_details(
            preflight_findings=findings,
            possible_reasons=COMPOSE_LABEL_UNDISCOVERED_POSSIBLE_REASONS,
            recommended_actions=COMPOSE_LABEL_UNDISCOVERED_RECOMMENDED_ACTIONS,
            running_container=container_name,
            running_image=container_image,
            referenced_compose_files=references,
        ),
    )


def _matching_container_unlabeled_diagnostic(
    container_name: str,
    container_image: str,
) -> UnmatchedDiagnostic:
    findings = (
        f"Running container {container_name} still matches this pending line.",
        "Docker did not report Compose config labels for that container.",
    )
    return UnmatchedDiagnostic(
        code="matching-container-without-compose-labels",
        message=(
            "A running container still matches this WUD entry, but Docker did "
            "not report Compose labels that tie it to a discovered stack."
        ),
        hint=MATCHING_CONTAINER_UNLABELED_HINT,
        details=_stale_pending_assistant_details(
            preflight_findings=findings,
            possible_reasons=MATCHING_CONTAINER_UNLABELED_POSSIBLE_REASONS,
            recommended_actions=MATCHING_CONTAINER_UNLABELED_RECOMMENDED_ACTIONS,
            running_container=container_name,
            running_image=container_image,
        ),
    )


def _generic_unmatched_diagnostic() -> UnmatchedDiagnostic:
    return UnmatchedDiagnostic(
        code="unmatched",
        message=GENERIC_UNMATCHED_MESSAGE,
        hint=GENERIC_UNMATCHED_HINT,
        details=_stale_pending_assistant_details(
            preflight_findings=GENERIC_UNMATCHED_FINDINGS,
            possible_reasons=GENERIC_UNMATCHED_POSSIBLE_REASONS,
            recommended_actions=GENERIC_UNMATCHED_RECOMMENDED_ACTIONS,
        ),
    )


def _stale_pending_assistant_details(
    *,
    preflight_findings: Sequence[str],
    possible_reasons: Sequence[str],
    recommended_actions: Sequence[str],
    **extra: object,
) -> Mapping[str, object]:
    return {
        "preflight_findings": tuple(preflight_findings),
        "possible_reasons": tuple(possible_reasons),
        "recommended_actions": tuple(recommended_actions),
        **extra,
    }


def _container_label(docker: DockerCli, container_name: str, label: str) -> str:
    fmt = f'{{{{ index .Config.Labels "{label}" }}}}'
    for value in docker.try_inspect(container_name, fmt):
        cleaned = value.strip()
        if cleaned and cleaned != "<no value>":
            return cleaned
    return ""


def _split_compose_config_files(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _local_label_path(
    value: str,
    working_dir: str,
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> Path | None:
    raw = Path(value)
    if raw.is_absolute():
        return _map_absolute_label_path(
            raw,
            config,
            host_docker_base=host_docker_base,
        )
    local_working_dir = _local_working_dir(
        working_dir,
        config,
        host_docker_base=host_docker_base,
    )
    if local_working_dir is None:
        return None
    return local_working_dir / value


def _local_working_dir(
    value: str,
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> Path | None:
    if not value:
        return None
    raw = Path(value)
    if raw.is_absolute():
        return _map_absolute_label_path(
            raw,
            config,
            host_docker_base=host_docker_base,
        )
    return None


def _map_absolute_label_path(
    path: Path,
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> Path | None:
    if host_docker_base is not None:
        try:
            return config.docker_base / path.relative_to(host_docker_base)
        except ValueError:
            pass
    try:
        path.relative_to(config.docker_base)
    except ValueError:
        return None
    return path


def _display_label_path(
    value: str,
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> str:
    raw = Path(value)
    if raw.is_absolute():
        if host_docker_base is not None:
            try:
                return path_display(host_docker_base, raw)
            except ValueError:
                pass
        try:
            return path_display(config.docker_base, raw)
        except ValueError:
            return raw.name
    cleaned = value.strip().lstrip("./")
    if cleaned.startswith("../"):
        return raw.name
    return cleaned or raw.name


def path_display(base: Path, path: Path) -> str:
    return path.relative_to(base).as_posix()


def _stack_name_from_label_path(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.name:
        if path.suffix in {".yml", ".yaml"}:
            return path.parent.name
        return path.name
    return ""


def _nonstandard_compose_files(
    local_paths: Sequence[Path | None],
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for local_path in local_paths:
        if local_path is None:
            continue
        directory = local_path.parent
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            if not _nonstandard_compose_filename(entry.name):
                continue
            display = _display_local_path(
                entry,
                config,
                host_docker_base=host_docker_base,
            )
            if display in seen:
                continue
            found.append(display)
            seen.add(display)
    return tuple(found)


def _nonstandard_compose_filename(name: str) -> bool:
    lowered = name.lower()
    return (
        "compose" in lowered
        and lowered.endswith((".yml", ".yaml"))
        and lowered not in COMPOSE_FILENAMES
    )


def _display_local_path(
    path: Path,
    config: UpdaterConfig,
    *,
    host_docker_base: Path | None,
) -> str:
    try:
        return path_display(config.docker_base, path)
    except ValueError:
        pass
    if host_docker_base is not None:
        try:
            return path_display(host_docker_base, path)
        except ValueError:
            pass
    return path.name


def _join_display_values(values: Sequence[str]) -> str:
    if not values:
        return "an unknown compose file"
    if len(values) == 1:
        return values[0]
    return ", ".join(values)


def _cleanup_for_skipped(
    targets: Sequence[WudTarget],
    skipped: Sequence[DryRunPlanSkipped],
    diagnostics: Mapping[int, UnmatchedDiagnostic],
) -> DryRunPlanCleanup:
    skipped_reasons = {item.line_no: item.reason for item in skipped}
    items = tuple(
        DryRunPlanCleanupItem(
            line_no=target.line_no,
            raw=target.raw,
            image=target.first,
            desired_tag=target.desired_tag,
            digest=target.digest,
            reason=skipped_reasons[target.line_no],
            diagnostic=diagnostics.get(target.line_no),
        )
        for target in targets
        if skipped_reasons.get(target.line_no) == "unmatched"
    )
    return DryRunPlanCleanup(items=items)


def _skipped(target: WudTarget, reason: str) -> DryRunPlanSkipped:
    return DryRunPlanSkipped(
        line_no=target.line_no,
        raw=target.raw,
        image=target.first,
        desired_tag=target.desired_tag,
        reason=reason,
    )
