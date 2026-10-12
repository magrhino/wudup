from __future__ import annotations

import unittest

from ruamel.yaml import YAML

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup.compose_rewrite import (
    _reject_yaml_anchor_or_alias_image_value,
    _reject_yaml_anchor_or_alias_labels,
    _reject_yaml_anchor_or_alias_service_config,
    apply_compose_digest_pins,
    apply_compose_tag_exclusions,
    apply_compose_tag_updates,
    render_compose_digest_pins,
    render_compose_tag_exclusions,
)
from wudup.updater_models import ComposeTagRewriteError, TagStreamUpdate, TagUpdate


class ComposeTagUpdateYamlSafetyTests(ComposeRewriteTestCase):
    def test_stream_rewrite_rejects_anchored_labels_without_write(self) -> None:
        original = (
            "x-labels: &labels\n"
            "  wud.tag.include: ^custom-.+$$\n"
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.2.3-distroless\n"
            "    labels: *labels\n"
        )
        compose_file = self.write_compose(original)
        tag_update = TagUpdate(
            old_image="repo/app:1.2.3-distroless",
            desired_tag="1.3.0-distroless",
            new_image="repo/app:1.3.0-distroless",
            services=("app",),
        )
        stream_update = TagStreamUpdate(
            line_no=1,
            stack="stack",
            stack_directory=str(compose_file.parent.resolve(strict=False)),
            compose_file=compose_file.name,
            service="app",
            current_tag="1.2.3-distroless",
            reported_tag="1.3.0",
            selected_tag="1.3.0-distroless",
            decision="preserve",
            label_key="wud.tag.include",
            current_label_value="^custom-.+$",
            proposed_label_value=r"^\d+\.\d+\.\d+-distroless$$",
            proposed_label_regex=r"^\d+\.\d+\.\d+-distroless$",
            approved=True,
            reason="approved",
        )

        with self.assertRaisesRegex(ComposeTagRewriteError, "anchors or aliases"):
            apply_compose_tag_updates(
                compose_file,
                (tag_update,),
                tag_stream_updates=(stream_update,),
                stack_name="stack",
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_invalid_yaml_without_write(self) -> None:
        original = "services:\n  app:\n    image: [repo/app:1.0\n"
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "could not be parsed"):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_non_utf8_file_without_write(self) -> None:
        # Docker Compose accepts UTF-16; the round-trip rewriter must refuse it
        # as a per-stack rewrite failure instead of crashing the run.
        original = "services:\n  app:\n    image: repo/app:1.0\n".encode("utf-16")
        compose_file = self.root / "compose.yml"
        compose_file.write_bytes(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "not UTF-8 text"):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_bytes(), original)

    def test_rejects_interpolated_image_without_write(self) -> None:
        original = "services:\n  app:\n    image: repo/app:${TAG}\n"
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "interpolation"):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:${TAG}",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_service_without_direct_image_without_write(self) -> None:
        original = (
            "x-template:\n"
            "  image: repo/app:1.0\n"
            "services:\n"
            "  app:\n"
            "    labels:\n"
            "      image: repo/app:1.0\n"
        )
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "direct string"):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_anchored_and_inherited_images_without_write(self) -> None:
        cases = (
            (
                "alias.yml",
                "x-base: &base\n  image: repo/app:1.0\nservices:\n  app: *base\n",
                "YAML anchors or aliases",
            ),
            (
                "merge.yml",
                (
                    "x-base: &base\n"
                    "  image: repo/app:1.0\n"
                    "services:\n"
                    "  app:\n"
                    "    <<: *base\n"
                ),
                "inherited",
            ),
        )

        for name, original, expected_error in cases:
            with self.subTest(name=name):
                compose_file = self.write_compose(original, name=name)

                with self.assertRaisesRegex(ComposeTagRewriteError, expected_error):
                    apply_compose_tag_updates(
                        compose_file,
                        (
                            TagUpdate(
                                old_image="repo/app:1.0",
                                desired_tag="2.0",
                                new_image="repo/app:2.0",
                                services=("app",),
                            ),
                        ),
                    )

                self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_commented_multiline_image_key_without_write(self) -> None:
        digest = "d771c6193517d7ccbbf9bf5142e235234fc5888a583eab8c4538589351374a79"
        old_image = f"ghcr.io/vavallee/bindery@sha256:{digest}"
        original = f"services:\n  bindery:\n    image: # managed\n      {old_image}\n"
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(
            ComposeTagRewriteError,
            "unsupported YAML syntax",
        ):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image=old_image,
                        desired_tag="latest",
                        new_image="ghcr.io/vavallee/bindery:latest",
                        services=("bindery",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_block_scalar_image_without_write(self) -> None:
        digest = "d771c6193517d7ccbbf9bf5142e235234fc5888a583eab8c4538589351374a79"
        old_image = f"ghcr.io/vavallee/bindery@sha256:{digest}"
        original = f"services:\n  bindery:\n    image: |\n      {old_image}\n"
        compose_file = self.write_compose(original)

        with self.assertRaises(ComposeTagRewriteError):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image=old_image,
                        desired_tag="latest",
                        new_image="ghcr.io/vavallee/bindery:latest",
                        services=("bindery",),
                    ),
                ),
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_duplicate_span_rejection(self) -> None:
        # Testing rejection of duplicate spans in apply_compose_tag_updates
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        with self.assertRaisesRegex(
            ComposeTagRewriteError, "was selected more than once"
        ):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app", "app"),
                    ),
                ),
            )


class ComposeTagExclusionYamlSafetyTests(ComposeRewriteTestCase):
    def test_rejects_interpolated_image_without_write(self) -> None:
        original = "services:\n  app:\n    image: repo/app:${TAG}\n"
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "interpolation"):
            apply_compose_tag_exclusions(
                compose_file,
                (
                    self.tag_exclusion_update(
                        image="repo/app:${TAG}",
                        tag="2.0",
                    ),
                ),
                existing_exact_tags={},
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_service_anchor_without_write(self) -> None:
        original = "\n".join(
            [
                "x-base: &base",
                "  image: repo/app:1.0",
                "services:",
                "  app: *base",
                "",
            ]
        )
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            apply_compose_tag_exclusions(
                compose_file,
                (self.tag_exclusion_update(tag="2.0"),),
                existing_exact_tags={},
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)

    def test_rejects_shared_label_alias_without_write(self) -> None:
        original = (
            "x-labels: &common\n"
            "  - foo=bar\n"
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels: *common\n"
            "  worker:\n"
            "    image: repo/worker:1.0\n"
            "    labels: *common\n"
        )
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            apply_compose_tag_exclusions(
                compose_file,
                (self.tag_exclusion_update(tag="2.0"),),
                existing_exact_tags={},
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)


class ComposeDigestPinYamlSafetyTests(ComposeRewriteTestCase):
    def test_apply_rejects_inherited_image_without_write(self) -> None:
        original = (
            "x-base: &base\n  image: repo/app:1.0\nservices:\n  app:\n    <<: *base\n"
        )
        compose_file = self.write_compose(original)

        with self.assertRaisesRegex(ComposeTagRewriteError, "inherited"):
            apply_compose_digest_pins(compose_file, (self.digest_pin_update(),))

        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)


class MalformedYamlTests(ComposeRewriteTestCase):
    def test_apply_compose_tag_updates_root_not_mapping(self) -> None:
        compose_file = self.write_compose("- services\n- app\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file is not a YAML mapping"
        ):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

    def test_apply_compose_tag_updates_services_not_mapping(self) -> None:
        compose_file = self.write_compose("services: []\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file has no services mapping"
        ):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=("app",),
                    ),
                ),
            )

    def test_render_compose_digest_pins_yaml_error(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: [repo/app:1.0\n"
        )
        with self.assertRaisesRegex(ComposeTagRewriteError, "could not be parsed"):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_digest_pins_root_not_mapping(self) -> None:
        compose_file = self.write_compose("- services\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file is not a YAML mapping"
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_digest_pins_services_not_mapping(self) -> None:
        compose_file = self.write_compose("services: []\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file has no services mapping"
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_tag_exclusions_yaml_error(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: [repo/app:1.0\n"
        )
        with self.assertRaisesRegex(ComposeTagRewriteError, "could not be parsed"):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_compose_tag_exclusions_root_not_mapping(self) -> None:
        compose_file = self.write_compose("- services\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file is not a YAML mapping"
        ):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_compose_tag_exclusions_services_not_mapping(self) -> None:
        compose_file = self.write_compose("services: []\n")
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Compose file has no services mapping"
        ):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_compose_digest_pins_unsupported_label_type(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n    labels:\n      - 123\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "unsupported non-string list entries"
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_tag_exclusions_unsupported_label_type(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n    labels:\n      - 123\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "unsupported non-string list entries"
        ):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_compose_digest_pins_label_dict_non_string(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.include: 123\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Label wud.tag.include is not a string value."
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_tag_exclusions_label_dict_non_string(self) -> None:
        compose_file = self.write_compose(
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "      wud.tag.exclude: 123\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Label wud.tag.exclude is not a string value."
        ):
            render_compose_tag_exclusions(
                compose_file, (self.tag_exclusion_update(),), existing_exact_tags={}
            )

    def test_render_compose_digest_pins_labels_unsupported_syntax_direct(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n    labels: my_label_string\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Service labels use unsupported YAML syntax."
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))

    def test_render_compose_digest_pins_labels_unsupported_syntax_inherited(
        self,
    ) -> None:
        compose_file = self.write_compose(
            "x-base: &base\n"
            "  labels: my_label_string\n"
            "services:\n"
            "  app:\n"
            "    <<: *base\n"
            "    image: repo/app:1.0\n"
        )
        with self.assertRaisesRegex(
            ComposeTagRewriteError, "Service app labels use unsupported YAML syntax."
        ):
            render_compose_digest_pins(compose_file, (self.digest_pin_update(),))


class YamlAnchorAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.yaml = YAML(typ="rt")
        self.yaml.preserve_quotes = True

    def _parse_services(self, yaml_str: str) -> dict:
        parsed = self.yaml.load(yaml_str)
        return parsed.get("services", {})

    def test_rejects_direct_anchor_image_value(self) -> None:
        yaml_str = """
services:
  app:
    image: &img repo/app:1.0
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_image_value(services, "app", services["app"])

    def test_rejects_shared_alias_image_value(self) -> None:
        yaml_str = """
services:
  app:
    image: &img repo/app:1.0
  worker:
    image: *img
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_image_value(
                services, "worker", services["worker"]
            )
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_image_value(services, "app", services["app"])

    def test_rejects_alias_from_extension_field_image(self) -> None:
        yaml_str = """
x-image: &img repo/app:1.0
services:
  app:
    image: *img
"""
        parsed = self.yaml.load(yaml_str)
        services = parsed.get("services", {})
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_image_value(services, "app", services["app"])

    def test_rejects_direct_anchor_service_config(self) -> None:
        yaml_str = """
services:
  app: &svc
    image: repo/app:1.0
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_service_config(
                services, "app", services["app"]
            )

    def test_rejects_shared_alias_service_config(self) -> None:
        yaml_str = """
services:
  app: &svc
    image: repo/app:1.0
  worker: *svc
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_service_config(
                services, "worker", services["worker"]
            )
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_service_config(
                services, "app", services["app"]
            )

    def test_rejects_alias_from_extension_field_service_config(self) -> None:
        yaml_str = """
x-base: &base
  image: repo/app:1.0
services:
  app: *base
"""
        parsed = self.yaml.load(yaml_str)
        services = parsed.get("services", {})
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_service_config(
                services, "app", services["app"]
            )

    def test_rejects_direct_anchor_labels(self) -> None:
        yaml_str = """
services:
  app:
    image: repo/app:1.0
    labels: &lbls
      - foo=bar
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_labels(
                services, "app", services["app"]["labels"]
            )

    def test_rejects_shared_alias_labels(self) -> None:
        yaml_str = """
services:
  app:
    image: repo/app:1.0
    labels: &lbls
      - foo=bar
  worker:
    image: repo/worker:1.0
    labels: *lbls
"""
        services = self._parse_services(yaml_str)
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_labels(
                services, "worker", services["worker"]["labels"]
            )
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_labels(
                services, "app", services["app"]["labels"]
            )

    def test_rejects_alias_from_extension_field_labels(self) -> None:
        yaml_str = """
x-labels: &lbls
  - foo=bar
services:
  app:
    image: repo/app:1.0
    labels: *lbls
"""
        parsed = self.yaml.load(yaml_str)
        services = parsed.get("services", {})
        with self.assertRaisesRegex(ComposeTagRewriteError, "YAML anchors or aliases"):
            _reject_yaml_anchor_or_alias_labels(
                services, "app", services["app"]["labels"]
            )

    def test_accepts_clean_yaml(self) -> None:
        yaml_str = """
services:
  app:
    image: repo/app:1.0
    labels:
      - foo=bar
  worker:
    image: repo/worker:1.0
    labels:
      - foo=bar
"""
        services = self._parse_services(yaml_str)
        _reject_yaml_anchor_or_alias_image_value(services, "app", services["app"])
        _reject_yaml_anchor_or_alias_service_config(services, "app", services["app"])
        _reject_yaml_anchor_or_alias_labels(services, "app", services["app"]["labels"])

        _reject_yaml_anchor_or_alias_image_value(services, "worker", services["worker"])
        _reject_yaml_anchor_or_alias_service_config(
            services, "worker", services["worker"]
        )
        _reject_yaml_anchor_or_alias_labels(
            services, "worker", services["worker"]["labels"]
        )
