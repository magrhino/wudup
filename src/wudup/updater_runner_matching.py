"""Runner parsing, matching, and progress helpers for ``update-from-wud``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from . import updater_audit
from .compose import ComposeStack
from .digest_provenance import digest_from_image
from .images import image_has_tag, image_tag, normalize_digest, repo_key
from .line_specs import parse_line_spec
from .plan_matching import (
    filter_matches_for_selections,
    partially_selected_line_numbers,
)
from .plan_models import PlanInputError
from .updater_matching import _services_for_target_match
from .updater_models import Match, UpdaterError, UpdaterProgressEvent
from .wud_file import (
    ParsedWudFile,
    WudTarget,
    is_digest_target_line,
    parse_wud_file,
)


class _RunnerMatchingMixin:
    def _parse_wud_files(self) -> tuple[ParsedWudFile, ParsedWudFile]:
        opts = self.options
        full_parse = parse_wud_file(opts.wud_file)
        only_lines = parse_line_spec(opts.only_lines, len(full_parse.lines), "--only-lines")
        exclude_lines = parse_line_spec(
            opts.exclude_tag_lines,
            len(full_parse.lines),
            "--exclude-tag-lines",
        )
        parse_line_spec(
            opts.remove_lines_before_run,
            len(full_parse.lines),
            "--remove-lines-before-run",
        )
        update_lines: set[int] | None = set(only_lines) if only_lines else None
        if exclude_lines:
            if update_lines is None:
                update_lines = {
                    line.line_no for line in full_parse.lines if line.actionable
                }
            update_lines -= set(exclude_lines)
        parsed = parse_wud_file(
            opts.wud_file,
            selected_lines=sorted(update_lines) if update_lines is not None else None,
        )
        excluded = parse_wud_file(opts.wud_file, selected_lines=exclude_lines)
        for warning in parsed.warnings:
            self.log.warning(warning)
        for warning in excluded.warnings:
            self.log.warning(warning)
        return self._apply_tag_overrides(parsed), excluded

    def _apply_tag_overrides(
        self,
        parsed: ParsedWudFile,
        *,
        log: bool = True,
    ) -> ParsedWudFile:
        overrides = {item.line_no: item.tag for item in self.options.tag_overrides}
        if not overrides:
            return parsed

        targets_by_line = {target.line_no: target for target in parsed.targets}
        missing = sorted(set(overrides) - set(targets_by_line))
        if missing:
            line_list = ", ".join(str(line_no) for line_no in missing)
            raise UpdaterError(
                f"--tag-override line(s) did not match selected WUD entries: {line_list}"
            )

        updated_targets: list[WudTarget] = []
        for target in parsed.targets:
            override = overrides.get(target.line_no)
            if override is None:
                updated_targets.append(target)
                continue
            if not target.desired_tag:
                raise UpdaterError(
                    f"--tag-override line {target.line_no} does not target a tag update"
                )
            if log:
                self.log.info(
                    "Tag override: "
                    f"line {target.line_no} uses tag {override} "
                    f"instead of {target.desired_tag}"
                )
            updated_targets.append(replace(target, desired_tag=override))

        return ParsedWudFile(
            lines=parsed.lines,
            targets=tuple(updated_targets),
            warnings=parsed.warnings,
        )

    def _audit_parsed_file(
        self,
        parsed: ParsedWudFile,
        audit_lines: Sequence[int],
    ) -> ParsedWudFile:
        return updater_audit.audit_parsed_file(self, parsed, audit_lines)

    def _build_matches(
        self,
        parsed: ParsedWudFile,
        stacks: Sequence[ComposeStack],
    ) -> tuple[list[Match], list[WudTarget]]:
        container_images = {item.name: item.image for item in self.docker.try_container_images()}
        matches: list[Match] = []
        seen: set[tuple[int, int, str, str, str]] = set()
        targets, skipped_tags = self._prefilter_targets(parsed.targets)

        for target in targets:
            for stack in stacks:
                for resolved, image, service in self._match_target_in_stack(
                    target,
                    stack,
                    container_images,
                ):
                    self._append_match_if_new(
                        matches,
                        seen,
                        stack,
                        target,
                        resolved,
                        image,
                        service,
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
        all_matches = matches
        try:
            matches, _normalized = filter_matches_for_selections(
                all_matches,
                self.options.update_selections,
            )
        except PlanInputError as exc:
            raise UpdaterError(str(exc)) from exc
        self.partially_selected_line_numbers = partially_selected_line_numbers(
            all_matches,
            matches,
        )
        return self._apply_tag_stream_updates(matches), skipped_tags

    def _apply_tag_stream_updates(self, matches: Sequence[Match]) -> list[Match]:
        updates_by_target = {
            (
                update.line_no,
                update.stack,
                update.stack_directory,
                update.compose_file,
                update.service,
                update.current_tag,
                update.reported_tag,
            ): update
            for update in self.options.tag_stream_updates
        }
        self.matched_tag_stream_updates = set()
        if not updates_by_target:
            return list(matches)

        adjusted: list[Match] = []
        for match in matches:
            update = updates_by_target.get(
                (
                    match.target.line_no,
                    match.stack.name,
                    str(match.stack.directory.resolve(strict=False)),
                    match.stack.file,
                    match.service,
                    image_tag(match.compose_image),
                    match.target.desired_tag,
                )
            )
            if update is None:
                adjusted.append(match)
                continue
            self.matched_tag_stream_updates.add(update)
            adjusted.append(
                replace(
                    match,
                    target=replace(match.target, desired_tag=update.selected_tag),
                )
            )
        return adjusted

    def _prefilter_targets(
        self,
        targets: Sequence[WudTarget],
    ) -> tuple[list[WudTarget], list[WudTarget]]:
        filtered: list[WudTarget] = []
        skipped_tags: list[WudTarget] = []
        for target in targets:
            if target.desired_tag and not self.options.allow_tag_updates:
                skipped_tags.append(target)
                continue
            filtered.append(target)
        return filtered, skipped_tags

    def _match_target_in_stack(
        self,
        target: WudTarget,
        stack: ComposeStack,
        container_images: Mapping[str, str],
    ) -> list[tuple[str, str, str]]:
        resolved = container_images.get(target.first, target.first)
        allow_repo = (
            target.allow_repo or resolved != target.first or not image_has_tag(resolved)
        )
        candidates: list[tuple[str, str, str]] = []

        for image in stack.images:
            services = _services_for_target_match(
                stack.service_images,
                image,
                target,
                resolved,
                allow_repo,
                allow_digest_pin_rematch=self.options.digest_pin_updates,
            )
            if services is None:
                continue
            if services:
                candidates.extend((resolved, image, service) for service in services)
            else:
                candidates.append((resolved, image, ""))

        if not candidates:
            candidates.extend(self._digest_unpin_plan_candidates(target, stack))

        return candidates

    def _digest_unpin_plan_candidates(
        self,
        target: WudTarget,
        stack: ComposeStack,
    ) -> list[tuple[str, str, str]]:
        if not is_digest_target_line(target):
            return []
        target_tag = image_tag(target.first)
        target_digest = normalize_digest(target.digest)
        candidates: list[tuple[str, str, str]] = []
        for update in self.options.digest_unpin_plan:
            if update.resolved_tag != target_tag:
                continue
            if update.target_digest != target_digest:
                continue
            if repo_key(update.old_image) != target.repo:
                continue
            if not digest_from_image(update.old_image):
                continue
            for service in update.services:
                if any(
                    item.service == service and item.image == update.old_image
                    for item in stack.service_images
                ):
                    candidates.append((update.tag_image, update.old_image, service))
        return candidates

    def _append_match_if_new(
        self,
        matches: list[Match],
        seen: set[tuple[int, int, str, str, str]],
        stack: ComposeStack,
        target: WudTarget,
        resolved: str,
        image: str,
        service: str,
    ) -> None:
        key = (stack.index, target.line_no, resolved, image, service)
        if key in seen:
            return
        matches.append(Match(stack, target, resolved, image, service))
        seen.add(key)

    def _progress(
        self,
        phase: str,
        status: str,
        message: str,
        *,
        stack: str = "",
        services: Sequence[str] | None = (),
        matches: Sequence[Match] = (),
    ) -> None:
        if self.progress_callback is None:
            return
        line_numbers = tuple(sorted({match.target.line_no for match in matches}))
        self.progress_callback(
            UpdaterProgressEvent(
                phase=phase,
                status=status,
                message=message,
                stack=stack,
                services=tuple(services or ()),
                line_numbers=line_numbers,
            )
        )
