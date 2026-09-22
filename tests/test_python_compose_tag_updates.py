from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from ruamel.yaml import YAML

from compose_rewrite_helpers import ComposeRewriteTestCase
from wudup.compose_rewrite import _backup_compose, apply_compose_tag_updates
from wudup.updater_models import (
    ComposeTagRewriteError,
    TagStreamUpdate,
    TagUpdate,
)


class ComposeTagUpdateTests(ComposeRewriteTestCase):
    def test_rewrite_rejects_change_after_backup(self) -> None:
        original = "services:\n  app:\n    image: repo/app:1.0\n  sibling:\n    image: repo/sibling:1.0\n"
        compose_file = self.write_compose(original)
        backup = _backup_compose(compose_file)
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        newer = original.replace("repo/sibling:1.0", "repo/sibling:2.0")
        compose_file.write_text(newer, encoding="utf-8")

        with self.assertRaisesRegex(ComposeTagRewriteError, "Compose file changed"):
            apply_compose_tag_updates(
                compose_file,
                (TagUpdate(
                    old_image="repo/app:1.0", desired_tag="2.0",
                    new_image="repo/app:2.0", services=("app",),
                ),),
                expected_source_hash=backup_hash,
            )

        self.assertEqual(compose_file.read_text(encoding="utf-8"), newer)

    def test_stream_image_and_list_label_are_rewritten_atomically(self) -> None:
        original = (
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.2.3-distroless\n"
            "    labels:\n"
            "      - keep=value\n"
        )
        compose_file = self.write_compose(original)
        compose_file.chmod(0o640)

        applied = apply_compose_tag_updates(
            compose_file,
            (
                TagUpdate(
                    old_image="repo/app:1.2.3-distroless",
                    desired_tag="1.3.0-distroless",
                    new_image="repo/app:1.3.0-distroless",
                    services=("app",),
                ),
            ),
            tag_stream_updates=(
                TagStreamUpdate(
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
                    current_label_value="",
                    proposed_label_value=r"^\d+\.\d+\.\d+-distroless$$",
                    proposed_label_regex=r"^\d+\.\d+\.\d+-distroless$",
                    approved=True,
                    reason="label-added",
                ),
            ),
            stack_name="stack",
        )

        parsed = YAML(typ="safe").load(compose_file.read_text(encoding="utf-8"))
        assert applied[0].replacements == 1
        assert parsed["services"]["app"]["image"] == "repo/app:1.3.0-distroless"
        assert "keep=value" in parsed["services"]["app"]["labels"]
        assert (
            r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$"
            in parsed["services"]["app"]["labels"]
        )
        assert stat.S_IMODE(compose_file.stat().st_mode) == 0o640

    def test_stream_rewrite_preserves_unrelated_compose_text(self) -> None:
        original = (
            'name: "jarvis"\n'
            "x-runtime: &runtime\n"
            "  restart: unless-stopped\n"
            "  logging:\n"
            "    driver: json-file\n"
            "    options:\n"
            '      max-size: "10m"\n'
            "services:\n"
            "  task-runner:\n"
            '    image: "n8nio/runners:2.33.5-distroless" # selected image\n'
            "    init: true\n"
            "    environment:\n"
            '      JSON_PAYLOAD: \'{"enabled":true,"items":[1,2]}\'\n'
            "      KEEP_EMPTY: \"\"\n"
            "    labels:\n"
            '      - "traefik.enable=true"\n'
            "      - 'wud.tag.include=^2\\.33\\.5-distroless$$' # managed stream\n"
            "      - keep=this-label-byte-for-byte\n"
            "    command: [\"run\", \"--mode=worker\"]\n"
            "    volumes:\n"
            "      - ./data:/data:ro\n"
            "  database:\n"
            "    <<: *runtime\n"
            "    image: postgres:17\n"
            "    environment:\n"
            "      POSTGRES_DB: app\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}\n"
            "networks:\n"
            "  default:\n"
            "    name: jarvis_default\n"
        )
        compose_file = self.write_compose(original)

        apply_compose_tag_updates(
            compose_file,
            (
                TagUpdate(
                    old_image="n8nio/runners:2.33.5-distroless",
                    desired_tag="2.34.4-distroless",
                    new_image="n8nio/runners:2.34.4-distroless",
                    services=("task-runner",),
                ),
            ),
            tag_stream_updates=(
                TagStreamUpdate(
                    line_no=1,
                    stack="jarvis",
                    stack_directory=str(compose_file.parent.resolve(strict=False)),
                    compose_file=compose_file.name,
                    service="task-runner",
                    current_tag="2.33.5-distroless",
                    reported_tag="2.34.4",
                    selected_tag="2.34.4-distroless",
                    decision="preserve",
                    label_key="wud.tag.include",
                    current_label_value=r"^2\.33\.5-distroless$",
                    proposed_label_value=r"^\d+\.\d+\.\d+-distroless$$",
                    proposed_label_regex=r"^\d+\.\d+\.\d+-distroless$",
                    approved=True,
                    reason="exact-regex-normalized",
                ),
            ),
            stack_name="jarvis",
        )

        expected = original.replace(
            "n8nio/runners:2.33.5-distroless",
            "n8nio/runners:2.34.4-distroless",
            1,
        ).replace(
            r"^2\.33\.5-distroless$$",
            r"^\d+\.\d+\.\d+-distroless$$",
            1,
        )
        self.assertEqual(compose_file.read_text(encoding="utf-8"), expected)

    def test_stream_rewrite_uses_existing_block_label_indentation(self) -> None:
        cases = (
            (
                "    labels:\n        - keep=value\n",
                (
                    "    labels:\n"
                    "        - keep=value\n"
                    "        - wud.tag.include=^\\d+-distroless$$\n"
                ),
            ),
            (
                "    labels:\n        keep: value\n",
                (
                    "    labels:\n"
                    "        keep: value\n"
                    "        wud.tag.include: ^\\d+-distroless$$\n"
                ),
            ),
        )
        for original_labels, expected_labels in cases:
            with self.subTest(labels=original_labels):
                original = (
                    "services:\n"
                    "  app:\n"
                    "    image: repo/app:1.0-distroless\n"
                    f"{original_labels}"
                    "    command: run\n"
                )
                compose_file = self.write_compose(original)

                self._apply_preserved_stream(compose_file)

                self.assertEqual(
                    compose_file.read_text(encoding="utf-8"),
                    original.replace("repo/app:1.0-distroless", "repo/app:2.0-distroless")
                    .replace(original_labels, expected_labels),
                )

    def test_stream_rewrite_supports_flow_and_empty_labels(self) -> None:
        cases = (
            (
                '    labels: ["keep=value"] # list\n',
                (
                    '    labels: ["keep=value", '
                    "wud.tag.include=^\\d+-distroless$$,] # list\n"
                ),
            ),
            (
                '    labels: {"keep": "value"} # map\n',
                (
                    '    labels: {"keep": "value", '
                    "wud.tag.include: ^\\d+-distroless$$,} # map\n"
                ),
            ),
            (
                "    labels: null # empty\n",
                (
                    "    labels: # empty\n"
                    "      - wud.tag.include=^\\d+-distroless$$\n"
                ),
            ),
        )
        for original_labels, expected_labels in cases:
            with self.subTest(labels=original_labels):
                original = (
                    "services:\n"
                    "  app:\n"
                    "    image: repo/app:1.0-distroless\n"
                    f"{original_labels}"
                    "    command: run\n"
                )
                compose_file = self.write_compose(original)

                self._apply_preserved_stream(compose_file)

                self.assertEqual(
                    compose_file.read_text(encoding="utf-8"),
                    original.replace("repo/app:1.0-distroless", "repo/app:2.0-distroless")
                    .replace(original_labels, expected_labels),
                )

    def test_stream_rewrite_quotes_inserted_label_scalars(self) -> None:
        proposed_label_value = r"^release, candidate #1[blue]$$"
        cases = (
            ("", False),
            ("    labels: null\n", False),
            ("    labels:\n      - keep=value\n", False),
            ("    labels:\n      keep: value\n", True),
            ('    labels: ["keep=value"]\n', False),
            ('    labels: {"keep": "value"}\n', True),
        )
        for original_labels, mapping_labels in cases:
            with self.subTest(labels=original_labels or "missing"):
                original = (
                    "services:\n"
                    "  app:\n"
                    "    image: repo/app:1.0-distroless\n"
                    f"{original_labels}"
                    "    command: run\n"
                )
                compose_file = self.write_compose(original)

                self._apply_preserved_stream(
                    compose_file,
                    proposed_label_value=proposed_label_value,
                )

                parsed = YAML(typ="safe").load(
                    compose_file.read_text(encoding="utf-8")
                )
                labels = parsed["services"]["app"]["labels"]
                if mapping_labels:
                    self.assertEqual(
                        labels["wud.tag.include"], proposed_label_value
                    )
                else:
                    self.assertIn(
                        f"wud.tag.include={proposed_label_value}", labels
                    )

    def test_stream_rewrite_preserves_multiline_flow_label_comments(self) -> None:
        cases = (
            (
                (
                    "    labels: [\n"
                    '      "keep=value" # keep ] inside comment\n'
                    "    ]\n"
                ),
                (
                    "    labels: [\n"
                    '      "keep=value", # keep ] inside comment\n'
                    "      wud.tag.include=^\\d+-distroless$$,\n"
                    "    ]\n"
                ),
            ),
            (
                (
                    "    labels: {\n"
                    '      "keep": "value" # keep } inside comment\n'
                    "    }\n"
                ),
                (
                    "    labels: {\n"
                    '      "keep": "value", # keep } inside comment\n'
                    "      wud.tag.include: ^\\d+-distroless$$,\n"
                    "    }\n"
                ),
            ),
        )
        for original_labels, expected_labels in cases:
            with self.subTest(labels=original_labels):
                original = (
                    "services:\n"
                    "  app:\n"
                    "    image: repo/app:1.0-distroless\n"
                    f"{original_labels}"
                    "    command: run\n"
                )
                compose_file = self.write_compose(original)

                self._apply_preserved_stream(compose_file)

                self.assertEqual(
                    compose_file.read_text(encoding="utf-8"),
                    original.replace("repo/app:1.0-distroless", "repo/app:2.0-distroless")
                    .replace(original_labels, expected_labels),
                )

    def _apply_preserved_stream(
        self,
        compose_file: Path,
        *,
        proposed_label_value: str = r"^\d+-distroless$$",
    ) -> None:
        apply_compose_tag_updates(
            compose_file,
            (
                TagUpdate(
                    old_image="repo/app:1.0-distroless",
                    desired_tag="2.0-distroless",
                    new_image="repo/app:2.0-distroless",
                    services=("app",),
                ),
            ),
            tag_stream_updates=(
                TagStreamUpdate(
                    line_no=1,
                    stack="stack",
                    stack_directory=str(compose_file.parent.resolve(strict=False)),
                    compose_file=compose_file.name,
                    service="app",
                    current_tag="1.0-distroless",
                    reported_tag="2.0",
                    selected_tag="2.0-distroless",
                    decision="preserve",
                    label_key="wud.tag.include",
                    current_label_value="",
                    proposed_label_value=proposed_label_value,
                    proposed_label_regex=proposed_label_value.removesuffix("$"),
                    approved=True,
                    reason="label-added",
                ),
            ),
            stack_name="stack",
        )

    def test_stream_map_label_stale_value_leaves_image_and_label_unchanged(self) -> None:
        original = (
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.2.3-distroless\n"
            "    labels:\n"
            "      wud.tag.include: ^custom-.+$$\n"
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
            current_label_value="^different$",
            proposed_label_value=r"^\d+\.\d+\.\d+-distroless$$",
            proposed_label_regex=r"^\d+\.\d+\.\d+-distroless$",
            approved=True,
            reason="approved",
        )

        with self.assertRaisesRegex(ComposeTagRewriteError, "changed since planning"):
            apply_compose_tag_updates(
                compose_file,
                (tag_update,),
                tag_stream_updates=(stream_update,),
                stack_name="stack",
            )

        assert compose_file.read_text(encoding="utf-8") == original

    def test_rewrites_only_direct_service_image_source_span(self) -> None:
        original = (
            "x-template:\n"
            "  image: repo/app:1.0\n"
            "services:\n"
            "  app:\n"
            '    image: "repo/app:1.0" # keep comment\n'
            "    labels:\n"
            "      image: repo/app:1.0\n"
            "  db:\n"
            "    image: repo/db:1.0\n"
        )
        compose_file = self.write_compose(original)

        applied = apply_compose_tag_updates(
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

        self.assertEqual(applied[0].replacements, 1)
        self.assertEqual(
            compose_file.read_text(encoding="utf-8"),
            original.replace(
                '    image: "repo/app:1.0" # keep comment',
                '    image: "repo/app:2.0" # keep comment',
            ),
        )

    def test_rewrites_multiline_digest_image_to_one_line_tag(self) -> None:
        digest = "d771c6193517d7ccbbf9bf5142e235234fc5888a583eab8c4538589351374a79"
        old_image = f"ghcr.io/vavallee/bindery@sha256:{digest}"
        new_image = "ghcr.io/vavallee/bindery:latest"
        original = (
            "services:\n"
            "  bindery:\n"
            "    image:\n"
            f"      {old_image}\n"
            "    container_name: bindery\n"
        )
        compose_file = self.write_compose(original)

        applied = apply_compose_tag_updates(
            compose_file,
            (
                TagUpdate(
                    old_image=old_image,
                    desired_tag="latest",
                    new_image=new_image,
                    services=("bindery",),
                ),
            ),
        )

        self.assertEqual(applied[0].replacements, 1)
        self.assertEqual(
            compose_file.read_text(encoding="utf-8"),
            (
                "services:\n"
                "  bindery:\n"
                f"    image: {new_image}\n"
                "    container_name: bindery\n"
            ),
        )

    def test_apply_compose_tag_updates_early_return(self) -> None:
        # If updates is empty, it returns early and does not read the file
        result = apply_compose_tag_updates(Path("does_not_exist.yml"), ())
        self.assertEqual(result, ())

    def test_missing_services_mapped(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:1.0\n"
        )

        with self.assertRaisesRegex(
            ComposeTagRewriteError, "No compose service was mapped for repo/app:1.0"
        ):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="2.0",
                        new_image="repo/app:2.0",
                        services=(),
                    ),
                ),
            )

    def test_mismatched_old_image(self) -> None:
        compose_file = self.write_compose(
            "services:\n  app:\n    image: repo/app:2.0\n"
        )

        with self.assertRaisesRegex(ComposeTagRewriteError, "expected repo/app:1.0"):
            apply_compose_tag_updates(
                compose_file,
                (
                    TagUpdate(
                        old_image="repo/app:1.0",
                        desired_tag="3.0",
                        new_image="repo/app:3.0",
                        services=("app",),
                    ),
                ),
            )

    def test_line_end_eof_without_newline(self) -> None:
        compose_file = self.write_compose("services:\n  app:\n    image: repo/app:1.0")
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
        self.assertIn("repo/app:2.0", compose_file.read_text(encoding="utf-8"))

    def test_apply_compose_tag_updates_service_not_map(self) -> None:
        compose_file = self.write_compose("services:\n  app: stringval\n")
        with self.assertRaisesRegex(ComposeTagRewriteError, "is not a mapping"):
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
