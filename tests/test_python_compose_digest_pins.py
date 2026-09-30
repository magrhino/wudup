from __future__ import annotations

from dataclasses import replace
from unittest import mock

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup.compose_rewrite import (
    apply_compose_digest_pins,
    apply_compose_retag_updates,
    render_compose_digest_pins,
    render_compose_retag_updates,
)
from wudup.updater_models import (
    ComposeTagRewriteError,
    DigestPinLabelRewriteApproval,
    DigestPinLabelRewriteApprovalRequired,
)


class ComposeDigestPinTests(ComposeRewriteTestCase):
    def test_retag_sequence_replaces_then_clears_resolved_tag_marker(
        self,
    ) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # keep operator note\n"
            "    # wudup.resolved-tag=latest\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )

        rendered, _applied = render_compose_digest_pins(
            compose_file,
            (
                self.digest_pin_update(
                    old_image="repo/app:latest",
                    resolved_tag="v1.25.0",
                    planned_digest="sha256:v125",
                ),
            ),
        )

        self.assertIn("# keep operator note", rendered)
        self.assertNotIn("wudup.resolved-tag=latest", rendered)
        self.assertEqual(rendered.count("wudup.resolved-tag=v1.25.0"), 1)
        compose_file.write_text(rendered, encoding="utf-8")
        update = replace(
            self.digest_pin_update(
                old_image="repo/app@sha256:v125",
                resolved_tag="v1.33.2",
                planned_digest="sha256:v133",
            ),
            resolved_image="repo/app:v1.33.2",
            final_image="repo/app:v1.33.2",
            marker="",
        )

        rendered, _applied = render_compose_retag_updates(
            compose_file,
            (update,),
        )

        self.assertIn("# keep operator note", rendered)
        self.assertNotIn("wudup.resolved-tag", rendered)
        self.assertIn("image: repo/app:v1.33.2", rendered)

    def test_retag_to_four_part_version_pattern_adds_wud_tag_transform(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest", resolved_tag="4.0.19.2979-ls321"),
            final_image="repo/app:4.0.19.2979-ls321",
            marker="",
            label_value=r"^\d+\.\d+\.\d+\.\d+-ls\d+$$",
        )

        rendered, applied = render_compose_retag_updates(
            compose_file, (update,), config_transforms={}
        )

        self.assertIn(
            r"- wud.tag.transform=^(\d+)\.(\d+)\.(\d+)\.(\d+)-ls(\d+)$$ => $$1.$$2.$$3-$$4.$$5",
            rendered,
        )
        self.assertEqual(
            applied[0].added_transforms,
            (("app", r"^(\d+)\.(\d+)\.(\d+)\.(\d+)-ls(\d+)$ => $1.$2.$3-$4.$5"),),
        )

    def test_retag_adds_wud_tag_transform_to_map_style_labels(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "      wud.tag.include: ^latest$$\n"
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest", resolved_tag="v5.26.2.10099"),
            final_image="repo/app:v5.26.2.10099",
            marker="",
            label_value=r"^v\d+(?:\.\d+)+$$",
        )

        rendered, _applied = render_compose_retag_updates(
            compose_file, (update,), config_transforms={}
        )

        self.assertIn(
            r"wud.tag.transform: ^v(\d+)\.(\d+)\.(\d+)\.(\d+)$$ => $$1.$$2.$$3-$$4",
            rendered,
        )
        self.assertNotIn("- wud.tag.transform", rendered)

    def test_digest_pin_exact_filter_and_custom_transform_skip_managed_transform(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:4.0.9.2244-ls257\n"
            "    labels:\n"
            "    - wud.tag.include=^4\\.0\\.9\\.2244-ls257$$\n"
        )
        rendered, _applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(old_image="repo/app:4.0.9.2244-ls257", resolved_tag="4.0.19.2979-ls321"),),
        )
        self.assertNotIn("wud.tag.transform", rendered)

        compose_file.write_text(
            "services:\n"
            "  app:\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
            "    - wud.tag.transform=^custom$$ => $$0\n",
            encoding="utf-8",
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest", resolved_tag="4.0.19.2979-ls321"),
            final_image="repo/app:4.0.19.2979-ls321",
            marker="",
            label_value=r"^\d+\.\d+\.\d+\.\d+-ls\d+$$",
        )
        rendered, _applied = render_compose_retag_updates(
            compose_file, (update,), config_transforms={"app": "^custom$ => $0"}
        )
        self.assertEqual(rendered.count("wud.tag.transform="), 1)
        self.assertIn("- wud.tag.transform=^custom$$ => $$0", rendered)

    def test_retag_transform_is_opt_in_and_follows_resolved_config(self) -> None:
        source = (
            "services:\n"
            "  app:\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )
        compose_file = self.write_compose(source)
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest", resolved_tag="4.0.19.2979-ls321"),
            final_image="repo/app:4.0.19.2979-ls321",
            marker="",
            label_value=r"^\d+\.\d+\.\d+\.\d+-ls\d+$$",
        )

        # Digest-pin and CLI callers never pass config transforms, so nothing undisclosed is added.
        rendered, applied = render_compose_retag_updates(compose_file, (update,))
        self.assertNotIn("wud.tag.transform", rendered)
        self.assertEqual(applied[0].added_transforms, ())

        # An inherited (extends) transform shows only in resolved config; keep it.
        rendered, applied = render_compose_retag_updates(
            compose_file, (update,), config_transforms={"app": "^inherited$ => $0"}
        )
        self.assertNotIn("wud.tag.transform", rendered)
        self.assertEqual(applied[0].added_transforms, ())

        # A raw label that resolves empty (unset variable) must not be overwritten.
        compose_file.write_text(
            source + "    - wud.tag.transform=${WUD_TRANSFORM}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ComposeTagRewriteError, "wud.tag.transform"):
            render_compose_retag_updates(compose_file, (update,), config_transforms={"app": ""})

    def test_retag_does_not_overwrite_explicitly_empty_transform_label(self) -> None:
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest", resolved_tag="4.0.19.2979-ls321"),
            final_image="repo/app:4.0.19.2979-ls321",
            marker="",
            label_value=r"^\d+\.\d+\.\d+\.\d+-ls\d+$$",
        )
        sources = (
            "    labels:\n    - wud.tag.include=^latest$$\n    - wud.tag.transform=\n",
            "    labels:\n    - wud.tag.include=^latest$$\n    - wud.tag.transform\n",
            '    labels:\n      wud.tag.include: ^latest$$\n      wud.tag.transform: ""\n',
            "    labels:\n      wud.tag.include: ^latest$$\n      wud.tag.transform:\n",
        )
        for labels in sources:
            with self.subTest(labels=labels):
                compose_file = self.write_compose(
                    "services:\n  app:\n    image: repo/app:latest\n" + labels
                )
                with self.assertRaisesRegex(ComposeTagRewriteError, "wud.tag.transform"):
                    render_compose_retag_updates(
                        compose_file, (update,), config_transforms={"app": ""}
                    )

    def test_render_retag_clears_conflicting_resolved_tag_markers(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # keep operator note\n"
            "    # wudup.resolved-tag=latest\n"
            "    # wudup.resolved-tag=v1.25.0\n"
            "    image: repo/app:latest\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )
        update = replace(
            self.digest_pin_update(
                old_image="repo/app:latest",
                resolved_tag="v1.33.2",
            ),
            final_image="repo/app:v1.33.2",
            marker="",
        )

        rendered, _applied = render_compose_retag_updates(
            compose_file,
            (update,),
        )

        self.assertIn("# keep operator note", rendered)
        self.assertNotIn("wudup.resolved-tag", rendered)
        self.assertIn("image: repo/app:v1.33.2", rendered)

    def test_render_retag_clears_empty_resolved_tag_markers(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # keep operator note\n"
            "    # wudup.resolved-tag=\n"
            "    # wud-updater.resolved-tag=\n"
            "    image: repo/app:latest\n"
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app:latest"),
            final_image="repo/app:v1.33.2",
            marker="",
        )

        rendered, _applied = render_compose_retag_updates(
            compose_file,
            (update,),
        )

        self.assertIn("# keep operator note", rendered)
        self.assertNotIn("resolved-tag=", rendered)

    def test_render_retag_writes_selected_tag_and_removes_digest_marker(
        self,
    ) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    # wudup.resolved-tag=1.0\n"
            "    image: repo/app@sha256:old\n"
            "    labels:\n"
            "    - wud.tag.include=^latest$$\n"
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app@sha256:old"),
            final_image="repo/app:2.0",
            marker="",
        )

        rendered, applied = render_compose_retag_updates(
            compose_file,
            (update,),
            stack_name="stack",
        )

        self.assertEqual(applied[0].final_image, "repo/app:2.0")
        self.assertIn("image: repo/app:2.0", rendered)
        self.assertIn("wud.tag.include=^2\\.0$$", rendered)
        self.assertNotIn("wudup.resolved-tag", rendered)

    def test_apply_retag_writes_selected_tag_file(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app@sha256:old\n"
        )
        update = replace(
            self.digest_pin_update(old_image="repo/app@sha256:old"),
            final_image="repo/app:2.0",
            marker="",
        )

        applied = apply_compose_retag_updates(compose_file, (update,))

        self.assertEqual(applied[0].final_image, "repo/app:2.0")
        self.assertIn(
            "image: repo/app:2.0",
            compose_file.read_text(encoding="utf-8"),
        )

    def test_render_empty_updates_returns_source_without_applied_updates(self) -> None:
        original = "services:\n  app:\n    image: repo/app:1.0\n"
        compose_file = self.write_compose(original)

        rendered, applied = render_compose_digest_pins(compose_file, ())

        self.assertEqual(rendered, original)
        self.assertEqual(applied, ())

    def test_render_writes_image_marker_label_and_rewrite_metadata(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=1.0\n"
            "    image: repo/app:1.0\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertIn("# wudup.resolved-tag=2.0", rendered)
        self.assertIn("image: repo/app@sha256:pin", rendered)
        self.assertIn("wud.tag.include=^2\\.0$$", rendered)
        self.assertEqual(
            applied[0].label_rewrites[0].reason,
            "plain-tag-normalized",
        )
        self.assertEqual(applied[0].label_rewrites[0].current_label_value, "1.0")

    def test_apply_writes_digest_pin_file(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        applied = apply_compose_digest_pins(compose_file, (self.digest_pin_update(),))

        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(applied[0].final_image, "repo/app@sha256:pin")
        self.assertIn("# wudup.resolved-tag=2.0", content)
        self.assertIn("image: repo/app@sha256:pin", content)

    def test_apply_retags_only_selected_service_when_image_is_shared(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.include=^1\\.0$$\n"
            "  sibling:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.include=^1\\.0$$\n"
        )

        applied = apply_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(services=("app",)),),
        )

        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].services, ("app",))
        self.assertIn("# wudup.resolved-tag=2.0", content)
        self.assertEqual(content.count("image: repo/app@sha256:pin"), 1)
        self.assertIn("  sibling:\n    image: repo/app:1.0", content)
        self.assertEqual(content.count("wud.tag.include=^2\\.0$$"), 1)
        self.assertEqual(content.count("wud.tag.include=^1\\.0$$"), 1)

    def test_render_keeps_real_digest_image_on_one_line(self) -> None:
        digest = "d771c6193517d7ccbbf9bf5142e235234fc5888a583eab8c4538589351374a79"
        compose_file = self.write_compose(
            "services:\n  bindery:\n    image: ghcr.io/vavallee/bindery:latest\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (
                self.digest_pin_update(
                    old_image="ghcr.io/vavallee/bindery:latest",
                    resolved_tag="latest",
                    planned_digest=f"sha256:{digest}",
                    services=("bindery",),
                ),
            ),
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertIn(
            f"image: ghcr.io/vavallee/bindery@sha256:{digest}",
            rendered,
        )
        self.assertNotIn("image: \n", rendered)

    def test_apply_rejects_empty_digest_pin_render_without_write(self) -> None:
        original = "services:\n  app:\n    image: repo/app:1.0\n"
        compose_file = self.write_compose(original)

        with (
            mock.patch(
                "wudup.compose_rewrite.render_compose_digest_pins",
                return_value=("", ()),
            ),
            mock.patch(
                "wudup.compose_rewrite._atomic_replace_compose"
            ) as replace,
        ):
            with self.assertRaisesRegex(ComposeTagRewriteError, "produced no output"):
                apply_compose_digest_pins(compose_file, (self.digest_pin_update(),))

        replace.assert_not_called()
        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_render_requires_approval_for_custom_include_label(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^beta|^stable\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired) as raised:
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                stack_name="stack",
            )

        self.assertEqual(raised.exception.service, "app")
        self.assertEqual(raised.exception.current_label_value, "^beta|^stable")
        self.assertEqual(raised.exception.proposed_label_value, "^2\\.0$$")
        self.assertEqual(raised.exception.proposed_label_regex, "^2\\.0$")

    def test_render_normalizes_plain_planned_tag_label(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=latest\n"
            "    image: repo/app:latest\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (
                self.digest_pin_update(
                    old_image="repo/app:latest",
                    resolved_tag="latest",
                ),
            ),
            stack_name="stack",
        )

        self.assertIn("wud.tag.include=^latest$$", rendered)
        self.assertEqual(applied[0].label_rewrites[0].reason, "plain-tag-normalized")
        self.assertEqual(applied[0].label_rewrites[0].current_label_value, "latest")

    def test_render_requires_approval_for_different_plain_tag_label(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=stable\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired) as raised:
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                stack_name="stack",
            )

        self.assertEqual(raised.exception.current_label_value, "stable")
        self.assertEqual(raised.exception.proposed_label_value, "^2\\.0$$")

    def test_render_accepts_matching_custom_include_label_approval(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^beta|^stable\n"
            "    image: repo/app:1.0\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
            label_rewrite_approvals=(
                DigestPinLabelRewriteApproval(
                    stack="stack",
                    service="app",
                    label_key="wud.tag.include",
                    current_label_value="^beta|^stable",
                    planned_tag="2.0",
                    proposed_label_value="^2\\.0$$",
                ),
            ),
            stack_name="stack",
        )

        self.assertIn("wud.tag.include=^2\\.0$$", rendered)
        self.assertEqual(applied[0].label_rewrites[0].reason, "approved")
        self.assertTrue(applied[0].label_rewrites[0].approved)

    def test_render_rejects_stale_custom_include_label_approval(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^beta|^stable\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired):
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                label_rewrite_approvals=(
                    DigestPinLabelRewriteApproval(
                        stack="stack",
                        service="app",
                        label_key="wud.tag.include",
                        current_label_value="^beta|^stable",
                        planned_tag="2.0",
                        proposed_label_value="^3\\.0$$",
                    ),
                ),
                stack_name="stack",
            )

    def test_render_label_already_matching_proposed_regex_has_no_rewrite(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^2\\.0$$\n"
            "    image: repo/app:1.0\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].label_rewrites, ())
        self.assertIn("wud.tag.include=^2\\.0$$", rendered)

    def test_render_service_without_include_label_has_no_rewrite(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        _rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
            stack_name="stack",
        )

        self.assertEqual(applied[0].label_rewrites, ())

    def test_render_normalizes_exact_regex_label(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^1\\.0$\n"
            "    image: repo/app:1.0\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
            stack_name="stack",
        )

        self.assertIn("wud.tag.include=^2\\.0$$", rendered)
        self.assertEqual(applied[0].label_rewrites[0].reason, "exact-regex-normalized")
        self.assertEqual(applied[0].label_rewrites[0].current_label_value, "^1\\.0$")
        self.assertFalse(applied[0].label_rewrites[0].approved)

    def test_render_rejects_approval_with_wrong_stack(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^custom|regex\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired):
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                label_rewrite_approvals=(
                    DigestPinLabelRewriteApproval(
                        stack="other-stack",
                        service="app",
                        label_key="wud.tag.include",
                        current_label_value="^custom|regex",
                        planned_tag="2.0",
                        proposed_label_value="^2\\.0$$",
                    ),
                ),
                stack_name="stack",
            )

    def test_render_rejects_approval_with_wrong_service(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^custom|regex\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired):
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                label_rewrite_approvals=(
                    DigestPinLabelRewriteApproval(
                        stack="stack",
                        service="other-service",
                        label_key="wud.tag.include",
                        current_label_value="^custom|regex",
                        planned_tag="2.0",
                        proposed_label_value="^2\\.0$$",
                    ),
                ),
                stack_name="stack",
            )

    def test_render_rejects_approval_with_wrong_current_label_value(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "    - wud.tag.include=^custom|regex\n"
            "    image: repo/app:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired):
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(),),
                label_rewrite_approvals=(
                    DigestPinLabelRewriteApproval(
                        stack="stack",
                        service="app",
                        label_key="wud.tag.include",
                        current_label_value="^different|regex",
                        planned_tag="2.0",
                        proposed_label_value="^2\\.0$$",
                    ),
                ),
                stack_name="stack",
            )

    def test_approval_required_exception_exposes_rewrite_fields(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  myservice:\n"
            "    labels:\n"
            "    - wud.tag.include=^custom.*regex\n"
            "    image: repo/myimage:1.0\n"
        )

        with self.assertRaises(DigestPinLabelRewriteApprovalRequired) as raised:
            render_compose_digest_pins(
                compose_file,
                (
                    self.digest_pin_update(
                        old_image="repo/myimage:1.0",
                        services=("myservice",),
                    ),
                ),
                stack_name="mystack",
            )

        exc = raised.exception
        self.assertEqual(exc.service, "myservice")
        self.assertEqual(exc.label_key, "wud.tag.include")
        self.assertEqual(exc.current_label_value, "^custom.*regex")
        self.assertEqual(exc.planned_tag, "2.0")
        self.assertEqual(exc.proposed_label_value, "^2\\.0$$")
        self.assertEqual(exc.proposed_label_regex, "^2\\.0$")
        self.assertIn("myservice", str(exc))
        self.assertIn("^custom.*regex", str(exc))

    def test_render_rejects_service_not_in_compose(self) -> None:
        compose_file = self.write_compose(
            "services:\n  other:\n    image: repo/other:1.0\n"
        )

        with self.assertRaises(ComposeTagRewriteError):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_rejects_image_mismatch(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/different:1.0\n"
        )

        with self.assertRaisesRegex(ComposeTagRewriteError, "expected"):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_rejects_empty_services_list(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        with self.assertRaisesRegex(ComposeTagRewriteError, "No compose service"):
            render_compose_digest_pins(
                compose_file,
                (self.digest_pin_update(services=()),),
            )

    def test_render_accepts_resolved_image_already_written(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:2.0\n"
        )

        rendered, applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
        )

        self.assertEqual(len(applied), 1)
        self.assertIn("repo/app@sha256:pin", rendered)

    def test_render_updates_map_style_labels(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.include: ^1\\.0$\n"
        )

        rendered, _applied = render_compose_digest_pins(
            compose_file,
            (self.digest_pin_update(),),
        )

        self.assertIn("wud.tag.include: ^2\\.0$$", rendered)
