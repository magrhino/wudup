"""Python implementation of ``bin/docker-update-from-wud``."""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import TYPE_CHECKING

from . import db, updater_audit, updater_logging, wud_file
from .command import CommandError, CommandRunner
from .compose import ComposeCli, ComposeDiscoveryError
from .digest_verifier import DigestVerifier
from .docker_cli import DockerCli
from .file_ops import OwnerConfig, OwnerConfigError
from .line_specs import LineSpecError, parse_line_spec
from .locks import DirectoryLock, WudLockError
from .updater_lifecycle import StackLifecycleExecutor
from .updater_matching import (
    _failed_line_numbers,
    _stacks_to_update,
    _tag_exclusion_preflight_matches,
    _unique_matches,
)
from .updater_models import (
    CompletedUpdateSelection,
    DigestRequirementOutcomes,
    StackStatus,
    UpdaterError,
)
from .updater_runner_matching import _RunnerMatchingMixin
from .updater_runner_operations import _RunnerOperationsMixin
from .updater_runner_output import _RunnerOutputMixin

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from .updater_models import (
        DigestPinCandidate,
        DigestPinUpdate,
        DigestUnpinUpdate,
        FailureRecord,
        ImageState,
        Match,
        TagExclusionUpdate,
        TagStreamUpdate,
        UpdaterOptions,
        UpdaterProgressEvent,
    )
    from .wud_file import ParsedWudFile, WudTarget

VALID_MODES = frozenset({"pause", "stop", "live"})
TAG_UPDATE_PLAN_VALIDATION_FAILED = "Tag update plan validation failed."


class UpdateFromWudRunner(
    _RunnerMatchingMixin,
    _RunnerOperationsMixin,
    _RunnerOutputMixin,
):
    def __init__(
        self,
        options: UpdaterOptions,
        *,
        environ: Mapping[str, str] | None = None,
        command_runner: CommandRunner | None = None,
        digest_verifier: DigestVerifier | None = None,
        progress_callback: Callable[[UpdaterProgressEvent], None] | None = None,
    ) -> None:
        self.options = options
        self.environ = os.environ if environ is None else environ
        self.owner = OwnerConfig.from_env(self.environ)
        self.command_runner = command_runner or CommandRunner()
        self.docker = DockerCli(runner=self.command_runner)
        self.digest_verifier = digest_verifier or DigestVerifier(self.docker)
        self.compose = ComposeCli(runner=self.command_runner)
        self.log_file = updater_logging.prepare_log_file(options.log_dir, self.owner)
        self.log = updater_logging.Logger(
            self.log_file,
            no_color=options.no_color,
            environ=self.environ,
        )
        self.failures: list[FailureRecord] = []
        self.preflight_digest_outcomes = DigestRequirementOutcomes()
        self.preflight_skipped_pending_line_numbers: set[int] = set()
        self.expected_digest_outcomes = DigestRequirementOutcomes()
        self.partially_selected_line_numbers: tuple[int, ...] = ()
        self.successful_completed_update_selections: tuple[
            CompletedUpdateSelection, ...
        ] = ()
        self.discovered_completed_update_selections: tuple[
            CompletedUpdateSelection, ...
        ] = ()
        self.matched_tag_stream_updates: set[TagStreamUpdate] = set()
        self.audit_conn: sqlite3.Connection | None = None
        self.audit_run_id: int | None = None
        self.audit_db_path: Path | None = None
        self.applied_digest_pins: dict[tuple[int, int, str], DigestPinUpdate] = {}
        self.applied_digest_unpins: dict[
            tuple[int, int, str],
            DigestUnpinUpdate,
        ] = {}
        self.stack_image_states: dict[
            int,
            tuple[dict[str, ImageState], dict[str, ImageState]],
        ] = {}
        self.stack_runtime_states: dict[
            int,
            tuple[tuple[str, ...], tuple[str, ...]],
        ] = {}
        self.stack_runtime_states_after: dict[
            int,
            tuple[tuple[str, ...], tuple[str, ...]],
        ] = {}
        self.digest_pin_update_cache: dict[
            tuple[DigestPinCandidate, ...],
            tuple[DigestPinUpdate, ...],
        ] = {}
        self.progress_callback = progress_callback
        self.lifecycle = StackLifecycleExecutor(self)

    def _validate_options(self) -> None:
        opts = self.options
        if opts.mode not in VALID_MODES:
            raise UpdaterError("--mode must be pause|stop|live")
        if opts.max_wait < 0:
            raise UpdaterError("--max-wait must be an integer number of seconds")
        if opts.tag_overrides and not opts.allow_tag_updates:
            raise UpdaterError("--tag-override requires --allow-tag-updates")

    def run(self) -> int:
        opts = self.options
        self._validate_options()

        lock = DirectoryLock(
            opts.wud_file,
            timeout_seconds=self.environ.get("WUD_LOCK_TIMEOUT", "30"),
            parent_held=self.environ.get("WUD_LOCK_HELD_BY_PARENT") == "1",
        )

        try:
            self._print_header()
            self._progress(
                "preflight",
                "running",
                "Checking pending entries, Compose stacks, and update safety.",
            )
            if not opts.wud_file.is_file():
                raise UpdaterError(f"List file not found: {opts.wud_file}")

            if lock.parent_held:
                lock.acquire()

            parsed, excluded_tags = self._parse_wud_files()
            if opts.dry_run or not opts.remove_lines_before_run:
                lock.release_parent()

            if not parsed.targets and not excluded_tags.targets:
                if opts.tag_stream_updates and not self._validate_tag_update_plan(()):
                    self._progress(
                        "preflight",
                        "failure",
                        TAG_UPDATE_PLAN_VALIDATION_FAILED,
                    )
                    return 1
                self.log.info("Nothing to do; list is empty.")
                self._progress("preflight", "success", "Pending list is empty.")
                self._progress("completion", "success", "No updates were pending.")
                return 0

            if parsed.targets:
                self._print_targets(parsed)
            self._print_tag_exclusions(excluded_tags)
            stacks = self.compose.discover_stacks(
                opts.docker_base,
                project_base=opts.host_docker_base,
                ignore_paths=opts.compose_ignore_paths,
            )
            exclusion_matches, invalid_exclusions = self._build_tag_exclusion_matches(
                excluded_tags,
                stacks,
            )
            exclusion_updates, exclusion_failures = self._plan_tag_exclusions(
                exclusion_matches,
                stacks,
            )
            exclusion_failures.extend(invalid_exclusions)
            self._print_tag_exclusion_plan(exclusion_updates, exclusion_failures)
            matches, skipped_tags = self._build_matches(parsed, stacks)
            self._print_skipped_tag_updates(skipped_tags)

            if not matches and not exclusion_updates:
                if opts.tag_stream_updates and not self._validate_tag_update_plan(
                    matches
                ):
                    self._progress(
                        "preflight",
                        "failure",
                        TAG_UPDATE_PLAN_VALIDATION_FAILED,
                        matches=matches,
                    )
                    return 1
                return self._handle_no_matches(
                    parsed,
                    excluded_tags,
                    matches,
                    skipped_tags,
                    exclusion_failures,
                )

            if matches:
                self._print_plan(matches)

            preflight_status = self._run_preflight_checks(
                parsed,
                matches,
                skipped_tags,
                exclusion_matches,
                exclusion_updates,
            )
            if preflight_status is not None:
                return preflight_status

            self._progress(
                "preflight",
                "success",
                "Preflight checks passed.",
                matches=matches,
            )
            if opts.dry_run:
                self.log.info(
                    "Dry-run only; no pull, restart, health wait, or cleanup performed."
                )
                self._progress("completion", "success", "Dry run completed.")
                return 0

            self._confirm_before_mutation()
            audit_parsed = self._prepare_mutation(
                parsed,
                matches,
                skipped_tags,
                exclusion_updates,
                exclusion_failures,
                lock,
            )

            stack_statuses = self._preflight_matching_stack_digests(matches)
            if stack_statuses:
                exclusion_statuses = {
                    update.source_line: StackStatus("failure", "preflight-skipped")
                    for update in exclusion_updates
                }
            else:
                exclusion_statuses = self._apply_tag_exclusions(exclusion_updates)
                stack_statuses = self._update_matching_stacks(matches)

            self._reconcile_pending_entries(
                audit_parsed,
                matches,
                stack_statuses,
                exclusion_updates,
                exclusion_statuses,
                lock,
            )
            return self._finish_run(
                matches,
                stack_statuses,
                exclusion_updates,
                exclusion_statuses,
                exclusion_failures,
            )
        except (CommandError, ComposeDiscoveryError, LineSpecError, OwnerConfigError, WudLockError) as exc:
            updater_audit.finish_audit_run(self, "failure", best_effort=True)
            raise UpdaterError(str(exc)) from exc
        except OSError as exc:
            updater_audit.finish_audit_run(self, "failure", best_effort=True)
            raise UpdaterError(f"Filesystem operation failed: {exc}") from exc
        except (sqlite3.Error, db.DatabaseError) as exc:
            updater_audit.finish_audit_run(self, "failure", best_effort=True)
            raise UpdaterError(f"Could not update audit database: {exc}") from exc
        except UpdaterError:
            updater_audit.finish_audit_run(self, "failure", best_effort=True)
            raise
        finally:
            if self.audit_conn is not None:
                self.audit_conn.close()
            lock.close()

    def _handle_no_matches(
        self,
        parsed: ParsedWudFile,
        excluded_tags: ParsedWudFile,
        matches: Sequence[Match],
        skipped_tags: Sequence[WudTarget],
        exclusion_failures: Sequence[tuple[WudTarget, str]],
    ) -> int:
        status = "failure" if exclusion_failures else "success"
        if not self.options.dry_run:
            audit_parsed = self._audit_parsed_file(
                parsed,
                [target.line_no for target in excluded_tags.targets],
            )
            updater_audit.start_audit(self, audit_parsed)
            updater_audit.mark_unmatched_pending(
                self,
                audit_parsed,
                matches,
                skipped_tags,
            )
            self._mark_tag_exclusion_failures(exclusion_failures)
            updater_audit.finish_audit_run(self, status)
        self.log.info("No stacks matched the list; nothing to do.")
        self._progress(
            "preflight",
            status,
            "No Compose stacks matched the selected pending entries.",
        )
        self._progress(
            "completion",
            status,
            "Updater finished without matching a stack.",
        )
        return 1 if exclusion_failures else 0

    def _run_preflight_checks(
        self,
        parsed: ParsedWudFile,
        matches: Sequence[Match],
        skipped_tags: Sequence[WudTarget],
        exclusion_matches: Sequence[Match],
        exclusion_updates: Sequence[TagExclusionUpdate],
    ) -> int | None:
        opts = self.options
        if not self._validate_tag_update_plan(matches):
            self._progress(
                "preflight",
                "failure",
                TAG_UPDATE_PLAN_VALIDATION_FAILED,
                matches=matches,
            )
            return 1
        if not self._validate_digest_pin_plan(matches):
            self._progress(
                "preflight",
                "failure",
                "Digest-pin plan validation failed.",
                matches=matches,
            )
            return 1
        if not self._validate_digest_unpin_plan(matches):
            self._progress(
                "preflight",
                "failure",
                "Digest-unpin plan validation failed.",
                matches=matches,
            )
            return 1

        preflight_matches = _unique_matches(
            (
                *matches,
                *_tag_exclusion_preflight_matches(
                    exclusion_matches,
                    exclusion_updates,
                ),
            )
        )
        if not self._validate_compose_runtime_ports(preflight_matches):
            if opts.dry_run:
                self.log.warning(
                    "Dry-run only; reported Compose runtime port issue without mutating."
                )
            else:
                audit_parsed = self._audit_parsed_file(
                    parsed,
                    [match.target.line_no for match in preflight_matches],
                )
                self._progress(
                    "preflight",
                    "failure",
                    "Compose runtime port preflight failed.",
                    matches=preflight_matches,
                )
                return self._finish_preflight_failure(
                    audit_parsed,
                    preflight_matches,
                    skipped_tags,
                )
        if not self._validate_compose_bind_mount_paths(matches):
            if opts.dry_run:
                self.log.warning(
                    "Dry-run only; reported container bind-mount path issue without mutating."
                )
            else:
                self._progress(
                    "preflight",
                    "failure",
                    "Compose bind-mount preflight failed.",
                    matches=matches,
                )
                return self._finish_preflight_failure(parsed, matches, skipped_tags)
        if not self._validate_tag_manifests(matches):
            self._progress(
                "preflight",
                "failure",
                "Target image manifest validation failed.",
                matches=matches,
            )
            return 1
        return None

    def _prepare_mutation(
        self,
        parsed: ParsedWudFile,
        matches: Sequence[Match],
        skipped_tags: Sequence[WudTarget],
        exclusion_updates: Sequence[TagExclusionUpdate],
        exclusion_failures: Sequence[tuple[WudTarget, str]],
        lock: DirectoryLock,
    ) -> ParsedWudFile:
        opts = self.options
        remove_line_numbers = parse_line_spec(
            opts.remove_lines_before_run,
            len(parsed.lines),
            "--remove-lines-before-run",
        )
        in_flight_lines = sorted(
            {match.target.line_no for match in matches}
            | set(remove_line_numbers)
            | {update.source_line for update in exclusion_updates}
        )
        audit_lines = sorted(
            set(in_flight_lines)
            | {target.line_no for target, _reason in exclusion_failures}
        )
        audit_parsed = self._audit_parsed_file(parsed, audit_lines)
        updater_audit.start_audit(self, audit_parsed)
        updater_audit.mark_unmatched_pending(
            self,
            audit_parsed,
            matches,
            skipped_tags,
        )
        updater_audit.mark_removed_pending(
            self,
            audit_parsed,
            remove_line_numbers,
            matches,
        )
        updater_audit.mark_matched_pending(self, matches, status="in_progress")
        self._mark_tag_exclusions_pending(exclusion_updates)
        self._mark_tag_exclusion_failures(exclusion_failures)
        wud_file.remove_lines_before_run(
            opts.wud_file,
            audit_parsed,
            in_flight_lines,
            lock=lock,
            owner=self.owner,
        )
        self.log.info("Removed in-flight WUD entries before update.")
        lock.release_parent()
        return audit_parsed

    def _update_matching_stacks(
        self,
        matches: Sequence[Match],
    ) -> dict[int, StackStatus]:
        stack_statuses: dict[int, StackStatus] = {}
        for stack in _stacks_to_update(matches):
            stack_matches = [
                match for match in matches if match.stack.index == stack.index
            ]
            stack_status = self._update_stack(stack, stack_matches)
            stack_statuses[stack.index] = stack_status
            if stack_status.status == "success":
                self._record_successful_completed_update_selections(stack_matches)
        return stack_statuses

    def _preflight_matching_stack_digests(
        self,
        matches: Sequence[Match],
    ) -> dict[int, StackStatus]:
        stacks = _stacks_to_update(matches)
        stale_statuses: dict[int, StackStatus] = {}
        for stack in stacks:
            stack_matches = [
                match for match in matches if match.stack.index == stack.index
            ]
            status = self.lifecycle._preflight_stack_expected_digests(
                stack,
                stack_matches,
            )
            if status is not None:
                stale_statuses[stack.index] = status
        if not stale_statuses:
            return {}
        stale_lines = {
            line_no
            for _stack_index, line_no, _image in self.preflight_digest_outcomes.stale
        }
        viable_lines = {
            line_no
            for _stack_index, line_no, _image in self.preflight_digest_outcomes.viable
        }
        definitively_stale_lines = stale_lines - viable_lines
        integrity_failed_lines = {
            line_no
            for _stack_index, line_no, _image in self.preflight_digest_outcomes.failed
        }
        self.preflight_skipped_pending_line_numbers = {
            match.target.line_no for match in matches
        } - definitively_stale_lines - integrity_failed_lines
        return {
            stack.index: stale_statuses.get(
                stack.index,
                StackStatus("failure", "preflight-skipped"),
            )
            for stack in stacks
        }

    def _reconcile_pending_entries(
        self,
        audit_parsed: ParsedWudFile,
        matches: Sequence[Match],
        stack_statuses: Mapping[int, StackStatus],
        exclusion_updates: Sequence[TagExclusionUpdate],
        exclusion_statuses: Mapping[int, StackStatus],
        lock: DirectoryLock,
    ) -> None:
        opts = self.options
        self._progress(
            "cleanup",
            "running",
            "Reconciling pending entries after the update attempt.",
            matches=matches,
        )
        failed_lines = _failed_line_numbers(matches, stack_statuses)
        failed_exclusion_lines = {
            update.source_line
            for update in exclusion_updates
            if exclusion_statuses.get(
                update.source_line,
                StackStatus("failure", "missing"),
            ).status
            != "success"
        }
        failed_lines.extend(sorted(failed_exclusion_lines))
        partial_lines = set(self.partially_selected_line_numbers)
        stale_failed_lines = (
            self._stale_pending_digest_line_numbers(matches, failed_lines)
            - partial_lines
        )
        restorable_failed_lines = [
            line_no
            for line_no in failed_lines
            if line_no not in stale_failed_lines
        ]
        restore_line_numbers = sorted(
            set(restorable_failed_lines) | partial_lines
        )
        if restore_line_numbers:
            wud_file.restore_failed_lines(
                opts.wud_file,
                audit_parsed,
                restore_line_numbers,
                lock=lock,
                owner=self.owner,
            )
        if failed_lines:
            if restorable_failed_lines:
                self.log.warning(f"Restored failed WUD entries in {opts.wud_file}")
            if stale_failed_lines:
                stale_lines = ", ".join(str(line) for line in sorted(stale_failed_lines))
                self.log.warning(
                    "Removed stale digest WUD entries from "
                    f"{opts.wud_file}: lines {stale_lines}. "
                    "Refresh or replace them before retrying."
                )
            self._mark_failed_lines_restored(restorable_failed_lines)
            updater_audit.mark_failed_pending(
                self,
                matches,
                stack_statuses,
                failed_lines,
            )
            cleanup_message = (
                "Failed entries were reconciled; stale digest entries were removed."
                if stale_failed_lines
                else "Restored failed entries to the pending file."
            )
            self._progress(
                "cleanup",
                "failure",
                cleanup_message,
                matches=matches,
            )
        else:
            self._mark_failed_lines_restored(())
            if partial_lines:
                lines = ", ".join(str(line_no) for line_no in sorted(partial_lines))
                self.log.info(
                    "Retained partially selected WUD entries for unselected "
                    f"Compose stacks: lines {lines}."
                )
            else:
                self.log.info("Successful WUD entries were removed before update.")
            self._progress(
                "cleanup",
                "success",
                "Pending entries were reconciled.",
                matches=matches,
            )

    def _finish_run(
        self,
        matches: Sequence[Match],
        stack_statuses: Mapping[int, StackStatus],
        exclusion_updates: Sequence[TagExclusionUpdate],
        exclusion_statuses: Mapping[int, StackStatus],
        exclusion_failures: Sequence[tuple[WudTarget, str]],
    ) -> int:
        updater_audit.mark_successful_pending(self, matches, stack_statuses)
        self._mark_successful_tag_exclusions(exclusion_updates, exclusion_statuses)
        updater_audit.record_update_events(
            self,
            matches,
            stack_statuses,
            insert_event=db.insert_update_event,
        )
        updater_audit.record_known_images(self, matches, stack_statuses)

        fail_count = sum(
            1 for status in stack_statuses.values() if status.status != "success"
        ) + sum(
            1
            for status in exclusion_statuses.values()
            if status.status != "success"
        ) + len(exclusion_failures)
        if fail_count:
            updater_audit.finish_audit_run(self, "failure")
            error_report = self._write_error_report()
            if error_report is not None:
                self.log.error(
                    f"Completed with {fail_count} failure(s). "
                    f"See log: {self.log_file}; error report: {error_report}"
                )
            else:
                self.log.error(
                    f"Completed with {fail_count} failure(s). See log: {self.log_file}"
                )
            self._progress(
                "completion",
                "failure",
                f"Updater completed with {fail_count} failure(s).",
                matches=matches,
            )
            return 1

        updater_audit.finish_audit_run(self, "success")
        self.log.info(f"Done. See log: {self.log_file}")
        self._progress(
            "completion",
            "success",
            "Updater completed successfully.",
            matches=matches,
        )
        return 0

def run_update_from_wud(
    options: UpdaterOptions,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        return UpdateFromWudRunner(options, environ=environ).run()
    except (UpdaterError, OwnerConfigError, WudLockError) as exc:
        log_dir = options.log_dir
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        print(f"[{updater_logging.timestamp()}] {exc}", file=sys.stderr)
        return 1
