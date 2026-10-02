from __future__ import annotations

from unittest import mock

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup.compose_rewrite import (
    apply_compose_digest_unpins,
    render_compose_digest_unpins,
)
from wudup.updater_digest_unpin import digest_unpin_update_from_values
from wudup.updater_models import ComposeTagRewriteError, ResolvedTagMarkerConflictError


class ComposeDigestUnpinTests(ComposeRewriteTestCase):
    def test_render_empty_updates_returns_source_without_applied_updates(self) -> None:
        original = "services:\n  app:\n    image: repo/app@sha256:old\n"
        compose_file = self.write_compose(original)

        rendered, applied = render_compose_digest_unpins(compose_file, ())

        self.assertEqual(rendered, original)
        self.assertEqual(applied, ())

    def test_render_writes_tag_image_and_removes_resolved_tag_marker(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wudup.resolved-tag=latest\n"
            "    image: repo/app@sha256:old\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )

        rendered, applied = render_compose_digest_unpins(
            compose_file,
            (self.digest_unpin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertIn("image: repo/app:latest", rendered)
        self.assertIn("wud.tag.include=^latest$$", rendered)
        self.assertNotIn("wudup.resolved-tag", rendered)
        self.assertNotIn(
            "repo/app:latest",
            compose_file.read_text(encoding="utf-8"),
        )

    def _unpin(self, tag: str, include: str, extra: str = "", **kwargs):
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            f"    # wudup.resolved-tag={tag}\n"
            "    image: repo/app@sha256:old\n"
            "    labels:\n"
            f"    - wud.tag.include={include}\n" + extra
        )
        update = digest_unpin_update_from_values(
            old_image="repo/app@sha256:old",
            resolved_tag=tag,
            target_digest="sha256:new",
            services=("app",),
        )
        rendered, _applied = render_compose_digest_unpins(
            compose_file, (update,), stack_name="stack", **kwargs
        )
        return rendered

    def test_unpin_replaces_exact_filter_with_release_line(self) -> None:
        rendered = self._unpin("v1.14.1", "^v1\\.14\\.1$$")

        self.assertIn("image: repo/app:v1.14.1", rendered)
        self.assertIn("wud.tag.include=^v\\d+(?:\\.\\d+)+$$", rendered)
        self.assertNotIn("v1\\.14\\.1", rendered)

    def test_unpin_to_four_part_tag_adds_managed_transform(self) -> None:
        transform = (
            "wud.tag.transform=^(\\d+)\\.(\\d+)\\.(\\d+)\\.(\\d+)-ls(\\d+)$$"
            " => $$1.$$2.$$3-$$4.$$5"
        )
        include = "^4\\.0\\.19\\.2979-ls321$$"

        rendered = self._unpin("4.0.19.2979-ls321", include, config_transforms={})
        self.assertIn("wud.tag.include=^\\d+\\.\\d+\\.\\d+\\.\\d+-ls\\d+$$", rendered)
        self.assertIn(f"- {transform}\n", rendered)

        inherited = self._unpin(
            "4.0.19.2979-ls321", include, config_transforms={"app": "^custom$ => $0"}
        )
        self.assertNotIn("wud.tag.transform", inherited)
        self.assertNotIn("wud.tag.transform", self._unpin("4.0.19.2979-ls321", include))

    def test_unpin_to_three_part_tag_adds_no_transform(self) -> None:
        rendered = self._unpin("1.2.3", "^1\\.2\\.3$$", config_transforms={})

        self.assertIn("wud.tag.include=^\\d+(?:\\.\\d+)+$$", rendered)
        self.assertNotIn("wud.tag.transform", rendered)

    def test_unpin_refuses_blank_transform_label(self) -> None:
        with self.assertRaisesRegex(ComposeTagRewriteError, "resolves to an empty value"):
            self._unpin(
                "4.0.19.2979-ls321",
                "^4\\.0\\.19\\.2979-ls321$$",
                extra="    - wud.tag.transform=\n",
                config_transforms={"app": ""},
            )

    def test_unpin_rejects_custom_filter(self) -> None:
        with self.assertRaisesRegex(ComposeTagRewriteError, "for digest unpin"):
            self._unpin("v1.14.1", "^beta|^stable")

    def test_render_removes_legacy_resolved_tag_marker(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wud-updater.resolved-tag=latest\n"
            "    image: repo/app@sha256:old\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )

        rendered, applied = render_compose_digest_unpins(
            compose_file,
            (self.digest_unpin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertIn("image: repo/app:latest", rendered)
        self.assertNotIn("wud-updater.resolved-tag", rendered)

    def test_render_removes_resolved_tag_marker_from_image_comment_slot(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wudup.resolved-tag=latest\n"
            "    image: repo/app@sha256:old\n"
            "    # wudup.resolved-tag=latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )

        rendered, applied = render_compose_digest_unpins(
            compose_file,
            (self.digest_unpin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertIn("image: repo/app:latest", rendered)
        self.assertNotIn("wudup.resolved-tag", rendered)

    def test_render_rejects_conflicting_resolved_tag_markers(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wudup.resolved-tag=latest\n"
            "    # wudup.resolved-tag=other\n"
            "    image: repo/app@sha256:old\n"
        )

        with self.assertRaises(ResolvedTagMarkerConflictError):
            render_compose_digest_unpins(
                compose_file,
                (self.digest_unpin_update(),),
                stack_name="stack",
            )

    def test_render_removes_marker_and_keeps_adjacent_operator_comments(self) -> None:
        for comments in (
            "    # wudup.resolved-tag=latest\n    # keep pinned until X\n",
            "    # keep pinned until X\n    #\n    # wudup.resolved-tag=latest\n",
        ):
            with self.subTest(comments=comments):
                compose_file = self.write_compose(
                    "services:\n"
                    "  app:\n"
                    "    restart: always\n"
                    f"{comments}"
                    "    image: repo/app@sha256:old\n"
                )

                rendered, applied = render_compose_digest_unpins(
                    compose_file,
                    (self.digest_unpin_update(),),
                    stack_name="stack",
                )

                self.assertEqual(applied[0].replacements, 1)
                self.assertIn("image: repo/app:latest", rendered)
                self.assertIn("# keep pinned until X", rendered)
                self.assertNotIn("wudup.resolved-tag", rendered)

    def test_render_rejects_duplicate_include_labels(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wudup.resolved-tag=latest\n"
            "    image: repo/app@sha256:old\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
            "    - wud.watch=true\n"
            "    - wud.tag.include=^latest$$\n"
        )

        with self.assertRaisesRegex(
            ComposeTagRewriteError,
            "Service app lists the wud.tag.include label more than once",
        ):
            render_compose_digest_unpins(
                compose_file,
                (self.digest_unpin_update(),),
                stack_name="stack",
            )

    def test_apply_rejects_empty_digest_unpin_render_without_write(self) -> None:
        original = "services:\n  app:\n    image: repo/app@sha256:old\n"
        compose_file = self.write_compose(original)

        with (
            mock.patch(
                "wudup.compose_rewrite.render_compose_digest_unpins",
                return_value=("", ()),
            ),
            mock.patch(
                "wudup.compose_rewrite._atomic_replace_compose"
            ) as replace,
        ):
            with self.assertRaisesRegex(ComposeTagRewriteError, "produced no output"):
                apply_compose_digest_unpins(
                    compose_file,
                    (self.digest_unpin_update(),),
                )

        replace.assert_not_called()
        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)
