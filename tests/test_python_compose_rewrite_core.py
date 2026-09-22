from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup import compose_rewrite
from wudup.compose_rewrite import (
    _backup_compose,
    _exact_tag_include_matches,
    _is_simple_exact_tag_include,
    exact_tags_regex,
)
from wudup.compose_source import _get_service_label_value, _yaml_scalar_boundary_matches
from wudup.updater_models import ComposeTagRewriteError


class ComposeSourceLookupTests(unittest.TestCase):
    def test_sequence_label_lookup_preserves_first_match_and_value(self) -> None:
        for labels, expected in (
            ([], ""),
            (["other=value", "target"], ""),
            (["target", "target=value=tail"], "value=tail"),
            (["target=", "target=later"], ""),
            (["other=value", "target=first", "target=later"], "first"),
            (["target=first", 123], "first"),
        ):
            with self.subTest(labels=labels):
                service = CommentedMap(labels=CommentedSeq(labels))
                self.assertEqual(_get_service_label_value(service, "target"), expected)

    def test_sequence_label_lookup_rejects_non_strings_before_match(self) -> None:
        for labels in ([123, "target=value"], ["other=value", None], ["target", 123]):
            with self.subTest(labels=labels):
                service = CommentedMap(labels=CommentedSeq(labels))
                with self.assertRaises(ComposeTagRewriteError) as caught:
                    _get_service_label_value(service, "target")
                self.assertEqual(
                    str(caught.exception),
                    "Service labels use unsupported non-string list entries.",
                )

    def test_yaml_scalar_boundaries_preserve_flow_and_block_rules(self) -> None:
        for tail, flow_expected, block_expected in (
            ("", False, True),
            (" \t", False, True),
            (", next", True, False),
            (" }", True, False),
            ("\t]", True, False),
            ("# comment", True, True),
            (" \t# comment", True, True),
            ("# comment\nnext", True, False),
            ("value", False, False),
            (" : value", False, False),
            ("[", False, False),
            ("{", False, False),
            ("\n,", False, False),
            ("\r,", False, False),
            ("\u00a0,", False, False),
        ):
            for flow, expected in ((True, flow_expected), (False, block_expected)):
                with self.subTest(tail=tail, flow=flow):
                    self.assertEqual(_yaml_scalar_boundary_matches(tail, flow=flow), expected)


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
            "wudup.compose_persistence.shutil.copy2",
            side_effect=OSError("copy failed"),
        ):
            with self.assertRaisesRegex(OSError, "copy failed"):
                compose_rewrite._backup_compose(compose_file)

        self.assertEqual(list(self.root.glob(".compose.yml.backup.*")), [])

    def test_backup_compose_file_not_found_on_unlink(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        with mock.patch("wudup.compose_persistence.shutil.copy2") as mock_copy2:
            mock_copy2.side_effect = RuntimeError("copy failed")

            with mock.patch("pathlib.Path.unlink") as mock_unlink:
                mock_unlink.side_effect = FileNotFoundError("already deleted")

                with self.assertRaisesRegex(RuntimeError, "copy failed"):
                    _backup_compose(compose_file)


class ComposeAtomicWriteTests(ComposeRewriteTestCase):
    def test_filesystem_failures_preserve_source_and_clean_temporary_file(self) -> None:
        original = b"services:\r\n  app:\r\n    image: repo/app:1.0\r\n"
        compose_file = self.root / "compose.yml"
        compose_file.write_bytes(original)
        for operation in ("chown", "chmod", "replace"):
            with self.subTest(operation=operation):
                error = OSError(f"{operation} failed")
                written_hashes = ["previous write"]
                with mock.patch(
                    f"wudup.compose_persistence.os.{operation}", side_effect=error
                ):
                    with self.assertRaises(OSError) as caught:
                        compose_rewrite._atomic_replace_compose(
                            compose_file, "changed", prefix="tag",
                            written_hashes=written_hashes,
                        )

                self.assertIs(caught.exception, error)
                self.assertEqual(compose_file.read_bytes(), original)
                self.assertEqual(written_hashes, ["previous write"])
                self.assertEqual(list(self.root.glob(".compose.yml.tag.*")), [])

    def test_stale_source_preserves_original_and_operation_specific_error(self) -> None:
        compose_file = self.write_compose("services: {}\n")
        for prefix, message in (
            ("tag", "Compose file changed before it could be rewritten; retry from a fresh state."),
            ("tracking-repair", "Compose file changed before tracking repair; preview it again."),
        ):
            with self.subTest(prefix=prefix):
                written_hashes: list[str] = []
                with self.assertRaises(ComposeTagRewriteError) as caught:
                    compose_rewrite._atomic_replace_compose(
                        compose_file, "changed", prefix=prefix,
                        expected_source_hash="stale", written_hashes=written_hashes,
                    )

                self.assertEqual(str(caught.exception), message)
                self.assertEqual(compose_file.read_text(), "services: {}\n")
                self.assertEqual(written_hashes, [])
                self.assertEqual(list(self.root.glob(f".compose.yml.{prefix}.*")), [])

    def test_success_records_exact_written_bytes_and_preserves_backup(self) -> None:
        compose_file = self.write_compose("services: {}\n")
        backup = _backup_compose(compose_file)
        rendered = "services:\r\n  app:\r\n    image: repo/app:2.0\r\n"
        written_hashes: list[str] = []

        compose_rewrite._atomic_replace_compose(
            compose_file, rendered, prefix="tag",
            expected_source_hash=hashlib.sha256(backup.read_bytes()).hexdigest(),
            written_hashes=written_hashes,
        )

        self.assertEqual(compose_file.read_bytes(), rendered.encode("utf-8"))
        self.assertEqual(written_hashes, [hashlib.sha256(compose_file.read_bytes()).hexdigest()])
        self.assertEqual(backup.read_bytes(), b"services: {}\n")
        self.assertEqual(list(self.root.glob(".compose.yml.tag.*")), [])
