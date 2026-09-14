"""Runner output and report helpers for ``update-from-wud``."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import updater_logging, updater_tag_exclusions
from .images import image_with_tag
from .updater_digest_pin import _digest_pin_match_tag
from .updater_matching import (
    _failure_target_lines,
    _plan_line,
    _scope_plan_label,
    _stacks_to_update,
)
from .updater_models import (
    STALE_PENDING_DIGEST_REASON,
    DigestPinUpdate,
    FailureRecord,
    Match,
    StackStatus,
    TagExclusionUpdate,
    UpdaterError,
)
from .wud_file import ParsedWudFile, WudTarget


class _RunnerOutputMixin:
    def _failure_summary(
        self,
        fail_count: int,
        exclusion_updates: Sequence[TagExclusionUpdate],
        exclusion_statuses: Mapping[tuple[int, int], StackStatus],
    ) -> str:
        failed_services = sorted({
            f"{failure.stack.name}/{service}"
            for failure in self.failures
            for service in (failure.services or ("stack",))
        } | {
            update.service_key for update in exclusion_updates
            if (status := exclusion_statuses[(update.stack.index, update.source_line)]).status
            != "success" and status.reason != "preflight-skipped"
        })
        skipped_services = sorted({
            update.service_key for update in exclusion_updates
            if exclusion_statuses[(update.stack.index, update.source_line)].reason
            == "preflight-skipped"
        })
        refresh_services = sorted({
            f"{failure.stack.name}/{service}"
            for failure in self.failures
            if failure.reason == STALE_PENDING_DIGEST_REASON
            for service in (failure.services or ("stack",))
        })
        summary = f"Completed with {fail_count} failure(s)."
        if failed_services:
            summary += f" Failed: {', '.join(failed_services)}."
        if refresh_services:
            summary += (
                f" Refresh pending updates for: {', '.join(refresh_services)};"
                " then retry."
            )
        if skipped_services:
            summary += (
                f" Skipped exclusions: {', '.join(skipped_services)}"
                " (stack preflight failed)."
            )
        return summary

    def _write_error_report(self) -> Path | None:
        if not self.failures:
            return None
        report_path = self._error_report_path()
        content = self._render_error_report(report_path)
        try:
            return updater_logging._create_unique_text_file_exclusive(
                report_path,
                content,
                owner=self.owner,
            )
        except OSError as exc:
            self.log.error(f"Could not write error report: {exc}")
            return None

    def _error_report_path(self) -> Path:
        name = self.log_file.name
        if name.endswith(".log"):
            name = f"{name[:-4]}.errors.log"
        else:
            name = f"{name}.errors.log"
        return self.log_file.with_name(name)

    def _render_error_report(self, report_path: Path) -> str:
        content = [
            "WUDup error report\n",
            f"timestamp={updater_logging.timestamp()}\n",
            f"central_log={self.log_file}\n",
            f"error_report={report_path}\n",
            f"failures={len(self.failures)}\n",
        ]
        for index, failure in enumerate(self.failures, start=1):
            content.extend(self._render_failure(index, failure))
        return "".join(content)

    def _render_failure(self, index: int, failure: FailureRecord) -> list[str]:
        services = " ".join(failure.services or ()) or "stack-level"
        restored = updater_logging._restored_text(failure.wud_restored)
        content = [
            f"\n[Failure {index}]\n",
            f"stack={failure.stack.name}\n",
            f"stack_dir={failure.stack.directory}\n",
            f"compose_file={failure.stack.file}\n",
            f"services={services}\n",
            f"phase={failure.phase}\n",
            f"reason={failure.reason}\n",
            f"wud_entries_restored={restored}\n",
            "targets:\n",
        ]
        for line in _failure_target_lines(failure.matches):
            content.append(f"  {line}\n")
        if failure.note:
            content.append(f"note={updater_logging.sanitize_stream(failure.note)}\n")
        result = failure.command_result
        if result is not None:
            content.extend(updater_logging._render_command_result(result))
        details_label = "details" if failure.phase == "preflight" else "health"
        content.append(f"{details_label}:\n")
        content.extend(updater_logging._indented_block(failure.health_details, "  "))
        return content

    def _print_header(self) -> None:
        opts = self.options
        self.log.info("docker-update-from-wud-v2")
        self.log.info(f"Base    : {opts.docker_base_label or str(opts.docker_base)}")
        if opts.host_docker_base is not None:
            self.log.info(
                f"HostBase: {opts.host_docker_base_label or str(opts.host_docker_base)}"
            )
        self.log.info(f"WUD file: {opts.wud_file_label or str(opts.wud_file)}")
        self.log.info(f"Log file: {self.log_file}")
        self.log.info(f"Mode    : {opts.mode}")
        self.log.info(f"Dry-run : {updater_logging._bool_text(opts.dry_run)}")
        self.log.info(f"Confirm : {updater_logging._bool_text(opts.assume_yes)}")
        self.log.info(
            f"TagEdit : {updater_logging._bool_text(opts.allow_tag_updates)}"
        )
        self.log.info(
            f"DigestP : {updater_logging._bool_text(opts.digest_pin_updates)}"
        )
        self.log.info(f"MaxWait : {opts.max_wait}s")
        if opts.only_lines:
            self.log.info(f"Only    : {opts.only_lines}")
        if opts.remove_lines_before_run:
            self.log.info(f"Remove  : {opts.remove_lines_before_run}")
        if opts.exclude_tag_lines:
            self.log.info(f"Exclude : {opts.exclude_tag_lines}")
            self.log.info(
                f"Recreate: {updater_logging._bool_text(opts.recreate_excluded_services)}"
            )
        for override in opts.tag_overrides:
            self.log.info(f"Override: line {override.line_no} tag={override.tag}")
        if self.owner.configured:
            self.log.info(f"Owner   : {self.owner.uid}:{self.owner.gid}")
        self.log.info("PTY     : python subprocess")

    def _print_targets(self, parsed: ParsedWudFile) -> None:
        if self.log.rich_enabled():
            self.log.plain("INFO", "Targets:")
            rows: list[tuple[int, str, str, str]] = []
            for target in parsed.targets:
                suffix = f" sha256={target.digest}" if target.digest else ""
                suffix += f" tag={target.desired_tag}" if target.desired_tag else ""
                self.log.plain(
                    "INFO",
                    f"  line {target.line_no}: {target.first}{suffix}",
                )
                rows.append(
                    (
                        target.line_no,
                        target.first,
                        target.digest,
                        target.desired_tag,
                    )
                )
            self.log.renderer.updater_targets(rows)
            return

        self.log.info("Targets:")
        for target in parsed.targets:
            suffix = f" sha256={target.digest}" if target.digest else ""
            suffix += f" tag={target.desired_tag}" if target.desired_tag else ""
            self.log.info(f"  line {target.line_no}: {target.first}{suffix}")

    def _print_tag_exclusions(self, parsed: ParsedWudFile) -> None:
        updater_tag_exclusions.print_tag_exclusions(self, parsed)

    def _print_skipped_tag_updates(self, skipped_tags: Sequence[WudTarget]) -> None:
        if not skipped_tags:
            return
        self.log.warning("Tag update entries require --allow-tag-updates and were left pending:")
        for target in skipped_tags:
            desired_image = image_with_tag(target.first, target.desired_tag)
            self.log.info(f"  line {target.line_no}: {target.first} -> {desired_image}")

    def _print_tag_exclusion_plan(
        self,
        updates: Sequence[TagExclusionUpdate],
        failures: Sequence[tuple[WudTarget, str]],
    ) -> None:
        updater_tag_exclusions.print_tag_exclusion_plan(self, updates, failures)

    def _print_plan(self, matches: Sequence[Match]) -> None:
        if self.log.rich_enabled():
            self.log.plain("INFO", "Stacks to update:")
            rows: list[tuple[str, str, list[str]]] = []
            for stack in _stacks_to_update(matches):
                self.log.plain("INFO", f"  - {stack.name} ({stack.directory})")
                stack_matches = [
                    match for match in matches if match.stack.index == stack.index
                ]
                scope = self._update_scope(stack, stack_matches)
                services_label = _scope_plan_label(scope)
                self.log.plain("INFO", f"      services: {services_label}")
                digest_pins = self._digest_pin_plan_by_line(stack_matches)

                plan_lines: list[str] = []
                lines = {
                    (
                        match.target.line_no,
                        match.target.first,
                        match.resolved,
                        match.target.desired_tag,
                    )
                    for match in stack_matches
                }
                for line_no, target, resolved, desired_tag in sorted(lines):
                    line = _plan_line(
                        line_no,
                        target,
                        resolved,
                        desired_tag,
                        digest_pins.get(line_no),
                    )
                    self.log.plain("INFO", f"      {line}")
                    plan_lines.append(line)
                rows.append((stack.name, services_label, plan_lines))
            self.log.renderer.updater_stack_plan(rows)
            return

        self.log.info("Stacks to update:")
        for stack in _stacks_to_update(matches):
            self.log.info(f"  - {stack.name} ({stack.directory})")
            stack_matches = [match for match in matches if match.stack.index == stack.index]
            scope = self._update_scope(stack, stack_matches)
            self.log.info(f"      services: {_scope_plan_label(scope)}")
            digest_pins = self._digest_pin_plan_by_line(stack_matches)
            lines = {
                (match.target.line_no, match.target.first, match.resolved, match.target.desired_tag)
                for match in stack_matches
            }
            for line_no, target, resolved, desired_tag in sorted(lines):
                self.log.info(
                    f"      {_plan_line(line_no, target, resolved, desired_tag, digest_pins.get(line_no))}"
                )

    def _digest_pin_plan_by_line(
        self,
        matches: Sequence[Match],
    ) -> dict[int, DigestPinUpdate]:
        if not self.options.digest_pin_updates:
            return {}
        try:
            updates = self._digest_pin_updates(matches)
        except UpdaterError:
            return {}
        by_key = {
            (update.old_image, update.resolved_tag): update
            for update in updates
        }
        by_line: dict[int, DigestPinUpdate] = {}
        for match in matches:
            resolved_tag = _digest_pin_match_tag(match)
            if not resolved_tag:
                continue
            update = by_key.get((match.compose_image, resolved_tag))
            if update is not None:
                by_line.setdefault(match.target.line_no, update)
        return by_line

    def _confirm_before_mutation(self) -> None:
        if self.options.assume_yes:
            return
        if not sys.stdin.isatty():
            raise UpdaterError("Refusing to mutate without --yes because stdin is not a TTY.")
        print("Proceed with docker compose pull/restart and WUD cleanup? [y/N] ", end="")
        answer = sys.stdin.readline().strip()
        if answer not in {"y", "Y", "yes", "YES"}:
            raise UpdaterError("Aborted before mutation.")
