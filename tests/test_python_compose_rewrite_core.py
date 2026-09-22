from __future__ import annotations

import unittest
from unittest import mock

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup import compose_rewrite
from wudup.compose_rewrite import (
    _backup_compose,
    _exact_tag_include_matches,
    _is_simple_exact_tag_include,
    exact_tags_regex,
)


class ComposeExactTagRegexTests(unittest.TestCase):
    def test_exact_tags_regex_sorts_deduplicates_and_escapes_tags(self) -> None:
        self.assertEqual(exact_tags_regex(()), "")
        self.assertEqual(exact_tags_regex(("2.0",)), r"^2\.0$")
        self.assertEqual(
            exact_tags_regex(("3+hotfix", "2.0", "2.0")),
            r"^(?:2\.0|3\+hotfix)$",
        )

    def test_simple_exact_tag_include_accepts_only_exact_valid_tags(self) -> None:
        valid_values = (
            r"^1\.2\.3$",
            "^latest$",
            "^v1-alpha$",
            "^my_tag$",
            "^20240101$",
        )
        invalid_values = (
            r"^beta|stable$",
            r"^1\.*$",
            "latest$",
            "^latest",
            "^$",
            "^",
            "",
            "^1.2$",
            "^1+2$",
            "^(abc)$",
            r"^abc\$",
            r"^abc\\def$",
            r"^abc\adef$",
            "^ $",
        )

        for value in valid_values:
            with self.subTest(value=value):
                self.assertTrue(_is_simple_exact_tag_include(value))
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertFalse(_is_simple_exact_tag_include(value))

    def test_exact_tag_include_matches(self) -> None:
        self.assertTrue(_exact_tag_include_matches(r"^1\.0$$", "1.0"))
        self.assertFalse(_exact_tag_include_matches(r"^2\.0$$", "1.0"))


class ComposeBackupTests(ComposeRewriteTestCase):
    def test_backup_lock_does_not_leave_stale_owner_file(self) -> None:
        compose_file = self.write_compose("services: {}\n")
        _backup_compose(compose_file)

        self.assertFalse(compose_file.with_name(f".{compose_file.name}.wudup.lock").exists())

    def test_backup_removes_created_temp_file_when_copy_fails(self) -> None:
        compose_file = self.write_compose("services: {}\n")

        with mock.patch(
            "wudup.compose_rewrite.shutil.copy2",
            side_effect=OSError("copy failed"),
        ):
            with self.assertRaisesRegex(OSError, "copy failed"):
                compose_rewrite._backup_compose(compose_file)

        self.assertEqual(list(self.root.glob(".compose.yml.backup.*")), [])

    def test_backup_compose_file_not_found_on_unlink(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        with mock.patch("wudup.compose_rewrite.shutil.copy2") as mock_copy2:
            mock_copy2.side_effect = RuntimeError("copy failed")

            with mock.patch("pathlib.Path.unlink") as mock_unlink:
                mock_unlink.side_effect = FileNotFoundError("already deleted")

                with self.assertRaisesRegex(RuntimeError, "copy failed"):
                    _backup_compose(compose_file)
