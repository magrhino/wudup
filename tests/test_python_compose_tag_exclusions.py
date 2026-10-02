from __future__ import annotations

import os
import stat
from unittest import mock

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup.compose_rewrite import (
    apply_compose_tag_exclusions,
    merge_wud_exclude_regex,
    render_compose_tag_exclusions,
)
from wudup.updater_models import ComposeTagRewriteError


class ComposeTagExclusionRegexTests(ComposeRewriteTestCase):
    def test_merge_wud_exclude_regex_replaces_or_extends_managed_regex(self) -> None:
        self.assertEqual(
            merge_wud_exclude_regex(
                "",
                previous_managed="",
                next_managed=r"^2\.0$",
            ),
            r"^2\.0$",
        )
        self.assertEqual(
            merge_wud_exclude_regex(
                r"^1\.0$",
                previous_managed=r"^1\.0$",
                next_managed=r"^(?:1\.0|2\.0)$",
            ),
            r"^(?:1\.0|2\.0)$",
        )
        self.assertEqual(
            merge_wud_exclude_regex(
                r"^beta",
                previous_managed=r"^1\.0$",
                next_managed=r"^(?:1\.0|2\.0)$",
            ),
            r"(?:^beta)|(?:^(?:1\.0|2\.0)$)",
        )
        self.assertEqual(
            merge_wud_exclude_regex(
                r"^beta",
                previous_managed=r"^1\.0$",
                next_managed="",
            ),
            r"^beta",
        )

    def test_merge_wud_exclude_regex_same_as_next(self) -> None:
        self.assertEqual(
            merge_wud_exclude_regex(
                r"^1\.0$", previous_managed="", next_managed=r"^1\.0$"
            ),
            r"^1\.0$",
        )


class ComposeTagExclusionTests(ComposeRewriteTestCase):
    def test_render_merges_custom_label_with_existing_exact_tags(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.exclude: ^beta\n"
        )

        rendered, applied = render_compose_tag_exclusions(
            compose_file,
            (
                self.tag_exclusion_update(
                    tag="3.0",
                    scope="service",
                ),
            ),
            existing_exact_tags={"app": {"2.0"}},
        )

        self.assertEqual(applied[0].tags, ("2.0", "3.0"))
        self.assertIn("wud.tag.exclude: (?:^beta)|(?:^(?:2\\.0|3\\.0)$$)", rendered)
        self.assertNotIn(
            "3\\.0",
            compose_file.read_text(encoding="utf-8"),
        )

    def test_apply_writes_atomically_and_preserves_mode_and_owner(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )
        os.chmod(compose_file, 0o600)
        before = compose_file.stat()

        applied = apply_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        after = compose_file.stat()
        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(applied[0].tags, ("2.0",))
        self.assertIn("wud.tag.exclude=^2\\.0$$", content)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(list(self.root.glob(".compose.yml.exclude.*")), [])

    def test_materializes_service_merged_map_labels(self) -> None:
        compose_file = self.write_compose(
            "\n".join(
                [
                    "x-base: &base",
                    "  labels:",
                    "    wud.tag.exclude: ^beta",
                    "    foo: bar",
                    "services:",
                    "  app:",
                    "    <<: *base",
                    "    image: repo/app:1.0",
                    "  worker:",
                    "    <<: *base",
                    "    image: repo/worker:1.0",
                    "",
                ]
            )
        )

        apply_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("wud.tag.exclude: ^beta"), 1)
        self.assertEqual(
            content.count("wud.tag.exclude: (?:^beta)|(?:^2\\.0$$)"),
            1,
        )
        self.assertIn(
            "  app:\n"
            "    <<: *base\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.exclude: (?:^beta)|(?:^2\\.0$$)\n"
            "      foo: bar\n",
            content,
        )
        self.assertIn(
            "  worker:\n    <<: *base\n    image: repo/worker:1.0\n",
            content,
        )
        self.assertNotIn("repo/worker:1.0\n    labels:", content)

    def test_materializes_service_merged_list_labels(self) -> None:
        compose_file = self.write_compose(
            "\n".join(
                [
                    "x-base: &base",
                    "  labels:",
                    "    - wud.tag.exclude=^beta",
                    "    - foo=bar",
                    "services:",
                    "  app:",
                    "    <<: *base",
                    "    image: repo/app:1.0",
                    "",
                ]
            )
        )

        apply_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("- wud.tag.exclude=^beta"), 1)
        self.assertEqual(
            content.count("- wud.tag.exclude=(?:^beta)|(?:^2\\.0$$)"),
            1,
        )
        self.assertIn(
            "  app:\n"
            "    <<: *base\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude=(?:^beta)|(?:^2\\.0$$)\n"
            "    - foo=bar\n",
            content,
        )

    def test_preserves_internal_label_merge(self) -> None:
        compose_file = self.write_compose(
            "\n".join(
                [
                    "x-labels: &common",
                    "  wud.tag.exclude: ^beta",
                    "  foo: bar",
                    "services:",
                    "  app:",
                    "    image: repo/app:1.0",
                    "    labels:",
                    "      <<: *common",
                    "      baz: qux",
                    "",
                ]
            )
        )

        apply_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("wud.tag.exclude: ^beta"), 1)
        self.assertEqual(
            content.count("wud.tag.exclude: (?:^beta)|(?:^2\\.0$$)"),
            1,
        )
        self.assertIn("      <<: *common\n", content)
        self.assertIn("      baz: qux\n", content)

    def test_apply_compose_tag_exclusions_early_return(self) -> None:
        # If updates is empty, render returns source, and apply skips the write.
        compose_file = self.write_compose("services:\n  app:\n    image: a\n")
        result = apply_compose_tag_exclusions(compose_file, (), existing_exact_tags={})
        self.assertEqual(result, ())
        self.assertEqual(
            compose_file.read_text(encoding="utf-8"),
            "services:\n  app:\n    image: a\n",
        )

    def test_apply_compose_tag_exclusions_skips_write_for_noop_existing_exact_tag(
        self,
    ) -> None:
        original = (
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude=^2\\.0$$\n"
        )
        compose_file = self.write_compose(original)

        with mock.patch(
            "wudup.compose_rewrite._atomic_replace_compose"
        ) as replace:
            applied = apply_compose_tag_exclusions(
                compose_file,
                (self.tag_exclusion_update(tag="2.0"),),
                existing_exact_tags={"app": {"2.0"}},
            )

        self.assertEqual(applied, ())
        replace.assert_not_called()
        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_render_compose_tag_exclusions_service_not_map(self) -> None:
        compose_file = self.write_compose("services:\n  app: repo/app:1.0\n")
        with self.assertRaisesRegex(ComposeTagRewriteError, "is not a mapping"):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_rejects_duplicate_exclude_labels(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude=^beta\n"
            "    - wud.tag.exclude=^rc\n"
        )
        updates = (self.tag_exclusion_update(),)

        with self.assertRaisesRegex(
            ComposeTagRewriteError,
            "Service app lists the wud.tag.exclude label more than once",
        ):
            render_compose_tag_exclusions(compose_file, updates, existing_exact_tags={})

    def test_render_replaces_bare_exclude_label(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude\n"
            "    - keep=value\n"
        )

        rendered, applied = render_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        self.assertEqual(len(applied), 1)
        self.assertEqual(
            rendered,
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude=^2\\.0$$\n"
            "    - keep=value\n",
        )

    def test_get_service_label_value_none(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n    labels:\n      wud.tag.exclude: \n"
        )
        rendered, applied = render_compose_tag_exclusions(
            compose_file,
            (self.tag_exclusion_update(tag="2.0"),),
            existing_exact_tags={},
        )

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].service, "app")
        self.assertEqual(applied[0].image_repo, "repo/app")
        self.assertEqual(applied[0].tags, ("2.0",))
        self.assertEqual(
            rendered,
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.exclude: ^2\\.0$$\n",
        )
