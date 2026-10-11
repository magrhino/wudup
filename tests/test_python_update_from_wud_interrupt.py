from __future__ import annotations

import errno
import os
import signal
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from tests.update_from_wud_helpers import UpdateFromWudRunnerTestCase

from wudup import compose_rewrite
from wudup.cli import _stop_signals_interrupt
from wudup.command import CommandRunner
from wudup.compose import ComposeCli
from wudup.compose_source import _load_compose_yaml
from wudup.locks import DirectoryLock, WudLockTimeout
from wudup.updater import UpdateFromWudRunner
from wudup.updater_lifecycle import StackLifecycleExecutor
from wudup.updater_logging import Logger
from wudup.updater_models import ComposeTagRewriteError, UpdaterError

TAG_LINE = "repo/app:1.0 tag=2.0\n"
OTHER_LINE = "repo/other:latest\n"


class UpdateFromWudInterruptTests(UpdateFromWudRunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Stacks update in discovery order: "a-other" first, then "b-app".
        self.wud_file.write_text(TAG_LINE + OTHER_LINE, encoding="utf-8")
        self.other_dir = self.make_stack(
            "a-other", [("other", "repo/other:latest", "cid-other")],
        )
        self.app_dir = self.make_stack("b-app", [("app", "repo/app:1.0", "cid-app")])
        self.original_app_compose = self.compose_file.read_text(encoding="utf-8")
        self.set_image_state("repo/other:latest", "old-other", "sha256:old-other")
        self.set_image_after_pull("repo/other:latest", "new-other", "sha256:new-other")
        self.set_image_state("repo/app:1.0", "old", "sha256:old")
        self.set_image_after_pull("repo/app:2.0", "new", "sha256:new")

    @property
    def compose_file(self) -> Path:
        return self.app_dir / "docker-compose.yml"

    def _run_raising(
        self,
        exc_type: type[BaseException],
        *,
        during: str,
        before_raise: Callable[[], None] | None = None,
        **option_changes: Any,
    ) -> str:
        real_pull = ComposeCli.pull

        def pull(compose: ComposeCli, directory: Path, *args: Any, **kwargs: Any) -> Any:
            if directory.name == during:
                if before_raise is not None:
                    before_raise()
                raise exc_type("stopped during pull")
            return real_pull(compose, directory, *args, **kwargs)

        runner = UpdateFromWudRunner(
            replace(self.updater_options(allow_tag_updates=True), **option_changes),
            environ=self.env,
            command_runner=CommandRunner(env=self.env),
        )
        output = StringIO()
        with (
            mock.patch.object(ComposeCli, "pull", autospec=True, side_effect=pull),
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaises(exc_type),
        ):
            runner.run()
        return output.getvalue()

    def _pending_rows(self) -> dict[int, tuple[str, str]]:
        return {
            row["line_no"]: (row["status"], row["status_reason"])
            for row in self.db_rows("SELECT line_no, status, status_reason FROM pending_updates")
        }

    def _run_status(self) -> str:
        rows = self.db_rows("SELECT status, finished_at FROM update_runs")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["finished_at"])
        return rows[0]["status"]

    def _backups(self) -> list[Path]:
        return sorted(self.app_dir.glob(".docker-compose.yml.backup.*"))

    def test_interrupt_during_pull_restores_compose_and_unfinished_wud_entry(self) -> None:
        output = self._run_raising(KeyboardInterrupt, during="b-app")

        self.assertEqual(
            self.compose_file.read_text(encoding="utf-8"),
            self.original_app_compose,
        )
        self.assertEqual(self._backups(), [])
        self.assertIn("restored the previous docker-compose.yml", output)
        # The finished stack's entry stays removed; the interrupted one returns.
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), TAG_LINE)
        self.assertIn("kept unfinished WUD entries on lines 1 pending", output)
        self.assertEqual(self._run_status(), "failure")
        rows = self._pending_rows()
        self.assertEqual(rows[1], ("failed", "run-interrupted"))
        self.assertEqual(rows[2][0], "resolved")
        events = self.db_rows("SELECT stack_name, status FROM update_events")
        self.assertEqual(
            [(row["stack_name"], row["status"]) for row in events],
            [("a-other", "success")],
        )

    def test_interrupt_before_any_stack_finished_restores_every_entry(self) -> None:
        self._run_raising(KeyboardInterrupt, during="a-other")

        self.assertCountEqual(
            self.wud_file.read_text(encoding="utf-8").splitlines(keepends=True),
            [TAG_LINE, OTHER_LINE],
        )
        self.assertEqual(
            self.compose_file.read_text(encoding="utf-8"),
            self.original_app_compose,
        )
        self.assertEqual(
            self._pending_rows(),
            {1: ("failed", "run-interrupted"), 2: ("failed", "run-interrupted")},
        )
        self.assertEqual(self._run_status(), "failure")

    def test_unexpected_error_restores_compose_and_wud_entry(self) -> None:
        self._run_raising(RuntimeError, during="b-app")

        self.assertEqual(
            self.compose_file.read_text(encoding="utf-8"),
            self.original_app_compose,
        )
        self.assertEqual(self._backups(), [])
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), TAG_LINE)
        self.assertEqual(self._pending_rows()[1], ("failed", "run-interrupted"))
        self.assertEqual(self._run_status(), "failure")

    def test_lock_timeout_before_removal_skips_restore(self) -> None:
        output = StringIO()
        with (
            mock.patch.object(
                DirectoryLock,
                "acquire",
                side_effect=WudLockTimeout("Timed out waiting for WUD file lock"),
            ),
            mock.patch("wudup.wud_file.restore_failed_lines") as restore,
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaisesRegex(UpdaterError, "Timed out waiting"),
        ):
            self.make_runner(allow_tag_updates=True).run()

        restore.assert_not_called()
        self.assertNotIn("could not be kept pending", output.getvalue())
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            TAG_LINE + OTHER_LINE,
        )
        self.assertEqual(self._run_status(), "failure")

    def test_failed_wud_restore_names_entries_to_add_back(self) -> None:
        with mock.patch(
            "wudup.wud_file.restore_failed_lines",
            side_effect=OSError("disk full"),
        ):
            output = self._run_raising(KeyboardInterrupt, during="b-app")

        self.assertIn("could not be kept pending", output)
        self.assertIn("add them back by hand if it does not: repo/app:1.0 tag=2.0", output)
        self.assertNotIn("next WUD check", output)

    def test_log_failure_after_successful_finish_keeps_success_status(self) -> None:
        real_info = Logger.info

        def info(logger: Logger, message: str) -> None:
            if message.startswith("Done."):
                raise OSError(errno.ENOSPC, "No space left on device")
            real_info(logger, message)

        output = StringIO()
        with (
            mock.patch.object(Logger, "info", autospec=True, side_effect=info),
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaisesRegex(UpdaterError, "No space left"),
        ):
            self.make_runner(allow_tag_updates=True).run()

        self.assertEqual(self._run_status(), "success")
        self.assertEqual(
            {status for status, _reason in self._pending_rows().values()},
            {"resolved"},
        )

    def test_failing_log_writes_do_not_stop_abort_cleanup(self) -> None:
        def break_logging() -> None:
            # A hung-up terminal (SIGHUP) or a full disk makes every write fail.
            for name in ("info", "warning", "error"):
                patcher = mock.patch.object(
                    Logger, name, side_effect=OSError(errno.EIO, "I/O error"),
                )
                patcher.start()
                self.addCleanup(patcher.stop)

        self._run_raising(KeyboardInterrupt, during="b-app", before_raise=break_logging)

        self.assertEqual(
            self.compose_file.read_text(encoding="utf-8"),
            self.original_app_compose,
        )
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), TAG_LINE)
        self.assertEqual(self._run_status(), "failure")
        rows = self._pending_rows()
        self.assertEqual(rows[1], ("failed", "run-interrupted"))
        self.assertEqual(rows[2][0], "resolved")

    def test_failed_restore_with_failing_log_writes_keeps_original_interrupt(
        self,
    ) -> None:
        def break_logging() -> None:
            for name in ("info", "warning", "error"):
                patcher = mock.patch.object(
                    Logger, name, side_effect=OSError(errno.EIO, "I/O error"),
                )
                patcher.start()
                self.addCleanup(patcher.stop)

        with mock.patch(
            "wudup.compose_rewrite.restore_compose_backup",
            side_effect=OSError("disk full"),
        ):
            self._run_raising(
                KeyboardInterrupt, during="b-app", before_raise=break_logging,
            )

        # The restore failed, so the backup holds the only copy of the old file.
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.original_app_compose)
        self.assertEqual(self._run_status(), "failure")

    def test_abort_messages_name_the_pending_source_label(self) -> None:
        output = self._run_raising(
            KeyboardInterrupt, during="b-app", wud_file_label="WUD API",
        )

        self.assertIn("kept unfinished WUD entries on lines 1 pending in WUD API", output)
        self.assertNotIn(str(self.wud_file), output)

    def test_second_interrupt_during_cleanup_still_finishes_audit_run(self) -> None:
        with mock.patch(
            "wudup.wud_file.restore_failed_lines",
            side_effect=KeyboardInterrupt("second Ctrl-C"),
        ):
            self._run_raising(KeyboardInterrupt, during="b-app")

        self.assertEqual(self._run_status(), "failure")
        self.assertNotIn(
            "in_progress",
            {status for status, _reason in self._pending_rows().values()},
        )

    def test_abort_keeps_real_reason_for_stack_that_already_failed(self) -> None:
        (self.fake_root / "stacks" / "a-other" / "pull_fail").write_text("", encoding="utf-8")

        self._run_raising(KeyboardInterrupt, during="b-app")

        self.assertCountEqual(
            self.wud_file.read_text(encoding="utf-8").splitlines(keepends=True),
            [TAG_LINE, OTHER_LINE],
        )
        rows = self._pending_rows()
        self.assertEqual(rows[1], ("failed", "run-interrupted"))
        self.assertEqual(rows[2], ("failed", "pull-failed"))

    def test_abort_resolves_tag_exclusion_applied_before_the_stop(self) -> None:
        self.wud_file.write_text(
            TAG_LINE + OTHER_LINE + "repo/excl:1.0 tag=2.0\n",
            encoding="utf-8",
        )
        excl_dir = self.make_stack("c-excl", [("excl", "repo/excl:1.0", "cid-excl")])

        self._run_raising(KeyboardInterrupt, during="b-app", exclude_tag_lines="3")

        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (excl_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), TAG_LINE)
        self.assertEqual(self._pending_rows()[3], ("resolved", "tag-excluded"))

    def test_interrupt_after_health_gate_keeps_verified_compose_file(self) -> None:
        real_remember = StackLifecycleExecutor._remember_applied_digest_changes

        def remember(executor: StackLifecycleExecutor, state: Any) -> None:
            if state.stack.name == "b-app":
                raise KeyboardInterrupt("stopped after health gate")
            real_remember(executor, state)

        output = StringIO()
        with (
            mock.patch.object(
                StackLifecycleExecutor,
                "_remember_applied_digest_changes",
                autospec=True,
                side_effect=remember,
            ),
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.make_runner(allow_tag_updates=True).run()

        self.assertIn(
            "image: repo/app:2.0",
            self.compose_file.read_text(encoding="utf-8"),
        )
        self.assertEqual(self._backups(), [])
        self.assertNotIn("Kept the previous Compose file", output.getvalue())
        self.assertNotIn("restored the previous", output.getvalue())

    def test_non_utf8_compose_file_fails_that_stack_only(self) -> None:
        real_apply = compose_rewrite.apply_compose_tag_updates

        def apply_to_utf16(compose_path: Path, *args: Any, **kwargs: Any) -> Any:
            # Real Compose reads UTF-16 files; the fake Docker cannot, so the
            # file is converted after discovery.
            compose_path.write_bytes(self.original_app_compose.encode("utf-16"))
            return real_apply(compose_path, *args, **kwargs)

        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch(
                "wudup.compose_rewrite.apply_compose_tag_updates",
                side_effect=apply_to_utf16,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = self.make_runner(allow_tag_updates=True).run()

        output = stderr.getvalue() + stdout.getvalue()
        self.assertEqual(status, 1, output)
        self.assertIn("Compose file is not UTF-8 text", output)
        self.assertEqual(
            self.compose_file.read_bytes(),
            self.original_app_compose.encode("utf-16"),
        )
        self.assertEqual(self._backups(), [])
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), TAG_LINE)
        self.assertEqual(self._pending_rows()[1][0], "failed")
        self.assertEqual(self._pending_rows()[2][0], "resolved")


class ComposeSourceDecodeTests(UpdateFromWudRunnerTestCase):
    def test_non_utf8_compose_file_raises_rewrite_error(self) -> None:
        compose_file = self.root / "docker-compose.yml"
        compose_file.write_bytes("services:\n  app:\n    image: a:1\n".encode("utf-16"))

        with self.assertRaisesRegex(ComposeTagRewriteError, "not UTF-8 text"):
            _load_compose_yaml(compose_file)


class StopSignalsInterruptTests(UpdateFromWudRunnerTestCase):
    def test_sigterm_raises_keyboard_interrupt_and_restores_handler(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)

        with self.assertRaises(KeyboardInterrupt), _stop_signals_interrupt():
            os.kill(os.getpid(), signal.SIGTERM)

        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_ignored_sighup_stays_ignored(self) -> None:
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)

        with _stop_signals_interrupt():
            # Under nohup a hang-up must not stop the update.
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGHUP)

        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
