from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from wudup.file_ops import (
    OwnerConfig,
    OwnerConfigError,
    atomic_rewrite,
)
from wudup.locks import DirectoryLock, WudLockTimeout, lock_dir_for
from wudup.wud_file import (
    parse_wud_file,
    remove_lines_before_run,
)


class WudFileCleanupTests(unittest.TestCase):
    def test_caller_held_lock_stays_held_across_multiple_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text(
                "repo/app:one\nrepo/app:two\nrepo/app:three\n",
                encoding="utf-8",
            )
            parsed = parse_wud_file(path)
            lock = DirectoryLock(path, timeout_seconds=0)
            lock.acquire()
            try:
                remove_lines_before_run(path, parsed, [1, 3], lock=lock)
                self.assertTrue(lock_dir_for(path).is_dir())

                remove_lines_before_run(path, parsed, [1, 2, 3], lock=lock)
                self.assertEqual(path.read_text(encoding="utf-8"), "")
                self.assertTrue(lock_dir_for(path).is_dir())
            finally:
                lock.release()

            self.assertFalse(lock_dir_for(path).exists())

    def test_parent_lock_context_manager_releases_parent_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/app:latest\n", encoding="utf-8")
            lock_dir_for(path).mkdir()

            with DirectoryLock(path, timeout_seconds=0, parent_held=True):
                self.assertTrue(lock_dir_for(path).is_dir())

            self.assertFalse(lock_dir_for(path).exists())

    def test_parent_lock_is_reused_and_released_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text(
                "repo/app:one\nrepo/app:two\nrepo/app:three\n",
                encoding="utf-8",
            )
            parsed = parse_wud_file(path)
            lock_dir_for(path).mkdir()
            lock = DirectoryLock(path, timeout_seconds=0, parent_held=True)

            remove_lines_before_run(path, parsed, [1, 3], lock=lock)
            self.assertEqual(path.read_text(encoding="utf-8"), "repo/app:two\n")
            self.assertTrue(lock_dir_for(path).is_dir())

            remove_lines_before_run(path, parsed, [1, 2, 3], lock=lock)
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertTrue(lock_dir_for(path).is_dir())

            lock.release_parent()
            self.assertFalse(lock_dir_for(path).exists())

    def test_timeout_leaves_file_and_existing_lock_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/old:latest\n", encoding="utf-8")
            parsed = parse_wud_file(path)
            lock_dir_for(path).mkdir()

            with self.assertRaisesRegex(WudLockTimeout, "Timed out waiting"):
                remove_lines_before_run(path, parsed, [1], lock_timeout=0)

            self.assertEqual(path.read_text(encoding="utf-8"), "repo/old:latest\n")
            self.assertTrue(lock_dir_for(path).is_dir())

    def test_selected_line_removal_keeps_unselected_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/app:one\nrepo/app:two\n", encoding="utf-8")
            parsed = parse_wud_file(path, selected_lines=[2])

            remove_lines_before_run(
                path,
                parsed,
                [target.line_no for target in parsed.targets],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "repo/app:one\n")

    def test_duplicate_raw_line_removal_preserves_one_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/app:latest\nrepo/app:latest\n", encoding="utf-8")
            parsed = parse_wud_file(path)

            remove_lines_before_run(path, parsed, [1])

            self.assertEqual(path.read_text(encoding="utf-8"), "repo/app:latest\n")

    def test_appended_duplicate_survives_pre_run_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text(
                "repo/app:one\nrepo/app:two\nrepo/app:three\n",
                encoding="utf-8",
            )
            parsed = parse_wud_file(path)

            with path.open("a", encoding="utf-8", newline="") as file:
                file.write("repo/app:two\n")
            remove_lines_before_run(path, parsed, [1, 2, 3])

            self.assertEqual(path.read_text(encoding="utf-8"), "repo/app:two\n")

    def test_comments_and_blank_lines_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("# header\n\nrepo/app:latest\n# footer\n", encoding="utf-8")
            parsed = parse_wud_file(path)

            remove_lines_before_run(path, parsed, [3])

            self.assertEqual(path.read_text(encoding="utf-8"), "# header\n\n# footer\n")

    def test_owner_and_mode_are_preserved_when_removing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/app:latest\n", encoding="utf-8")
            os.chmod(path, 0o660)
            before = path.stat()
            parsed = parse_wud_file(path)

            remove_lines_before_run(path, parsed, [1])

            after = path.stat()
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))

    def test_readonly_mode_and_owner_are_preserved_when_removing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("repo/app:latest\n", encoding="utf-8")
            os.chmod(path, 0o440)
            before = path.stat()
            parsed = parse_wud_file(path)

            remove_lines_before_run(path, parsed, [1])

            after = path.stat()
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))


class FileOpsTests(unittest.TestCase):
    def test_atomic_rewrite_uses_target_directory_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("old\n", encoding="utf-8")

            atomic_rewrite(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(Path(tmpdir).glob(".images.todo.*")), [])

    def test_out_guid_alias_is_applied_to_owner_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "images.todo"
            path.write_text("old\n", encoding="utf-8")
            os.chmod(path, 0o640)
            before = path.stat()
            owner = OwnerConfig.from_values(
                str(os.getuid()),
                out_guid=str(os.getgid()),
            )

            atomic_rewrite(path, "new\n", owner=owner)

            after = path.stat()
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual((after.st_uid, after.st_gid), (os.getuid(), os.getgid()))
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))

    def test_owner_config_requires_uid_and_group(self) -> None:
        with self.assertRaisesRegex(
            OwnerConfigError,
            "OUT_UID and OUT_GID/OUT_GUID must be set together",
        ):
            OwnerConfig.from_values(str(os.getuid()))


if __name__ == "__main__":
    unittest.main()
