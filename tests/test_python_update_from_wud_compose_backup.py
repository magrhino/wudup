from __future__ import annotations

import errno
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from tests.update_from_wud_helpers import UpdateFromWudRunnerTestCase

from wudup import compose_persistence


class UpdateFromWudComposeBackupTests(UpdateFromWudRunnerTestCase):
    def _tag_update_stack(self) -> Path:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        self.set_image_state("repo/app:1.0", "old", "sha256:old")
        self.set_image_after_pull("repo/app:2.0", "new", "sha256:new")
        return stack_dir

    def _run(self) -> tuple[int, str]:
        runner = self.make_runner(allow_tag_updates=True)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = runner.run()
        return status, stderr.getvalue() + stdout.getvalue()

    @staticmethod
    def _backups(stack_dir: Path) -> list[Path]:
        return sorted(stack_dir.glob(".docker-compose.yml.backup.*"))

    def test_successful_tag_update_removes_compose_backup(self) -> None:
        stack_dir = self._tag_update_stack()

        status, output = self._run()

        self.assertEqual(status, 0, output)
        self.assertIn(
            "image: repo/app:2.0",
            (stack_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertEqual(self._backups(stack_dir), [])

    def test_restored_rollback_removes_compose_backup(self) -> None:
        stack_dir = self._tag_update_stack()
        (self.fake_root / "stacks" / "app" / "pull_fail").write_text("", encoding="utf-8")

        status, output = self._run()

        self.assertEqual(status, 1, output)
        self.assertIn(
            "image: repo/app:1.0",
            (stack_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertEqual(self._backups(stack_dir), [])

    def test_failed_restore_keeps_compose_backup_and_logs_its_path(self) -> None:
        stack_dir = self._tag_update_stack()
        original = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
        (self.fake_root / "stacks" / "app" / "pull_fail").write_text("", encoding="utf-8")

        with mock.patch(
            "wudup.compose_rewrite.restore_compose_backup",
            side_effect=OSError("disk full"),
        ):
            status, output = self._run()

        self.assertEqual(status, 1, output)
        backups = self._backups(stack_dir)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
        self.assertIn(f"Kept the previous Compose file at {backups[0]}", output)

    def test_directory_sync_failure_after_tag_rewrite_restores_compose(self) -> None:
        stack_dir = self._tag_update_stack()
        compose_file = stack_dir / "docker-compose.yml"
        original = compose_file.read_text(encoding="utf-8")
        real_fsync_directory = compose_persistence._fsync_directory
        calls = 0

        def fail_after_first_rewrite(directory: Path) -> None:
            nonlocal calls
            calls += 1
            # Call 1 syncs the backup; call 2 follows the tag rewrite.
            if calls == 2:
                raise OSError(errno.EIO, "I/O error")
            real_fsync_directory(directory)

        with mock.patch(
            "wudup.compose_persistence._fsync_directory",
            side_effect=fail_after_first_rewrite,
        ):
            status, output = self._run()

        self.assertEqual(status, 1, output)
        self.assertIn("could not be synced to disk", output)
        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:1.0 tag=2.0\n",
        )
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
        self.assertEqual(self._backups(stack_dir), [])
