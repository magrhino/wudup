from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from tests.update_from_wud_helpers import (
    UpdateFromWudRunnerTestCase,
    manifest_index_digest,
)

from wudup import updater_tag_exclusions
from wudup.command import CommandRunner
from wudup.compose import (
    ComposeStack,
    ServiceImage,
)
from wudup.compose_rewrite import apply_compose_tag_exclusions
from wudup.updater import (
    UpdateFromWudRunner,
)
from wudup.updater_models import (
    ComposeTagRewriteError,
    TagExclusionUpdate,
    UpdaterOptions,
)


class UpdateFromWudTagExclusionTests(UpdateFromWudRunnerTestCase):
    def test_exclude_tag_line_writes_wud_label_and_cleans_line(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])

        result = self.run_python("--yes", "--exclude-tag-lines", "1")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("wud.tag.exclude=^2\\.0$$", content)
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
        self.assertNotRegex(self.calls(), r"compose -f .* up -d")
        pending = self.db_rows("SELECT * FROM pending_updates")
        rules = self.db_rows("SELECT * FROM tag_exclusion_rules")
        self.assertEqual(pending[0]["status"], "resolved")
        self.assertEqual(pending[0]["status_reason"], "tag-excluded")
        self.assertEqual(rules[0]["scope"], "image_repo")
        self.assertEqual(rules[0]["image_repo"], "repo/app")
        self.assertEqual(rules[0]["tag"], "2.0")
    def test_exclude_tag_line_updates_all_services_for_image_repo(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        app_stack = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        worker_stack = self.make_stack(
            "worker",
            [("worker", "registry.example.com/repo/app:1.1", "cid-worker")],
        )

        result = self.run_python("--yes", "--exclude-tag-lines", "1")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (app_stack / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (worker_stack / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        rules = self.db_rows("SELECT * FROM tag_exclusion_rules")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["scope"], "image_repo")
    def test_exclude_tag_line_can_recreate_affected_services(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])

        result = self.run_python(
            "--yes",
            "--exclude-tag-lines",
            "1",
            "--recreate-excluded-services",
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertRegex(
            self.calls(),
            r"compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
        )
    def _run_stale_exclusion(self) -> CompletedProcess[str]:
        self.wud_file.write_text(
            "repo/excluded:1.0 tag=2.0\n"
            "ghcr.io/acme/stale:latest@sha256:stale\n",
            encoding="utf-8",
        )
        self.set_image_state(
            "ghcr.io/acme/stale:latest",
            "sha256:old",
            "sha256:old-index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/stale:latest",
            manifest_index_digest("sha256:moved", "sha256:moved-child"),
        )

        result = self.run_python(
            "--yes",
            "--exclude-tag-lines",
            "1",
            "--recreate-excluded-services",
        )

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        return result

    def _assert_exclusion_result(self, stack: Path, *, skipped: bool) -> None:
        expected_pending = "repo/excluded:1.0 tag=2.0\n" if skipped else ""
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), expected_pending)
        compose_text = (stack / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual("wud.tag.exclude" in compose_text, not skipped)
        pending = self.db_rows("SELECT * FROM pending_updates ORDER BY line_no")
        expected_exclusion = (
            ("failed", "preflight-skipped") if skipped else ("resolved", "tag-excluded")
        )
        self.assertEqual(
            [(row["status"], row["status_reason"]) for row in pending],
            [expected_exclusion, ("failed", "stale-pending-digest")],
        )

    def test_stale_digest_allows_unaffected_tag_exclusion(self) -> None:
        exclusion_stack = self.make_stack(
            "excluded",
            [("app", "repo/excluded:1.0", "cid-excluded")],
        )
        self.make_stack(
            "stale",
            [("app", "ghcr.io/acme/stale:latest", "cid-stale")],
        )
        self._run_stale_exclusion()
        self._assert_exclusion_result(exclusion_stack, skipped=False)
        self.assertIn("up -d", self.calls())
    def test_stale_digest_blocks_same_stack_tag_exclusion(self) -> None:
        exclusion_stack = self.make_stack(
            "excluded",
            [
                ("app", "repo/excluded:1.0", "cid-excluded"),
                ("stale", "ghcr.io/acme/stale:latest", "cid-stale"),
            ],
        )
        self._run_stale_exclusion()
        self._assert_exclusion_result(exclusion_stack, skipped=True)
        self.assertNotRegex(self.calls(), r"compose -f .* (?:pull|stop|up -d)")
    def test_shared_exclusion_attributes_skip_to_blocked_stack(self) -> None:
        healthy_stack = self.make_stack(
            "a-healthy", [("app", "repo/excluded:1.0", "cid-healthy")]
        )
        exclusion_stack = self.make_stack(
            "z-blocked",
            [
                ("app", "repo/excluded:1.0", "cid-excluded"),
                ("stale", "ghcr.io/acme/stale:latest", "cid-stale"),
            ],
        )
        result = self._run_stale_exclusion()
        self._assert_exclusion_result(exclusion_stack, skipped=True)
        self.assertIn("wud.tag.exclude", (healthy_stack / "docker-compose.yml").read_text())
        self.assertNotRegex(self.calls(), r"/z-blocked\tcompose -f .* (?:pull|stop|up -d)")
        self.assertRegex(self.calls(), r"/a-healthy\tcompose -f .* up -d")
        self.assertIn("Completed with 1 failure(s). Failed: z-blocked/stale.", result.stderr)
        self.assertIn("Skipped exclusions: z-blocked/app (stack preflight failed).", result.stderr)
        self.assertNotIn("Failed: a-healthy", result.stderr)
        pending = self.db_rows("SELECT * FROM pending_updates ORDER BY line_no")
        self.assertEqual(pending[0]["stack_name"], "z-blocked")
        self.assertEqual(pending[0]["service_name"], "app")
    def test_exclude_tag_line_does_not_recreate_already_excluded_service(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])

        initial = self.run_python("--yes", "--exclude-tag-lines", "1")
        self.assertEqual(initial.returncode, 0, initial.stderr + initial.stdout)

        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        result = self.run_python(
            "--yes",
            "--exclude-tag-lines",
            "1",
            "--recreate-excluded-services",
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        self.assertNotRegex(self.calls(), r"compose -f docker-compose.yml up -d")
    def test_can_apply_tag_exclusions_uses_existing_exact_tags(self) -> None:
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        stack = ComposeStack(
            index=0,
            directory=stack_dir,
            file="docker-compose.yml",
            name="app",
            images=("repo/app:1.0",),
            service_images=(ServiceImage("app", "repo/app:1.0"),),
        )
        update = TagExclusionUpdate(
            stack=stack,
            service="app",
            image="repo/app:1.0",
            image_repo="repo/app",
            tag="3.0",
            source_line=1,
            scope="service",
        )
        captured: dict[str, object] = {}

        class Runner:
            def _existing_exact_tag_exclusions(
                self,
                updates: list[TagExclusionUpdate],
            ) -> dict[str, set[str]]:
                captured["updates"] = updates
                return {"app": {"2.0"}}

        def fake_render_compose_tag_exclusions(
            compose_path: Path,
            updates: list[TagExclusionUpdate],
            *,
            existing_exact_tags: dict[str, set[str]],
        ) -> tuple[str, tuple[object, ...]]:
            captured["compose_path"] = compose_path
            captured["render_updates"] = updates
            captured["existing_exact_tags"] = existing_exact_tags
            return "", ()

        with mock.patch(
            "wudup.compose_rewrite.render_compose_tag_exclusions",
            side_effect=fake_render_compose_tag_exclusions,
        ):
            result = updater_tag_exclusions.can_apply_tag_exclusions(
                Runner(),
                (update,),
            )

        self.assertTrue(result)
        self.assertEqual(captured["updates"], [update])
        self.assertEqual(captured["render_updates"], [update])
        self.assertEqual(captured["compose_path"], stack_dir / "docker-compose.yml")
        self.assertEqual(captured["existing_exact_tags"], {"app": {"2.0"}})
    def test_exclude_tag_line_recreate_includes_missing_network_provider(self) -> None:
        compose_file = self.prepare_network_mode_media_stack(
            include_provider_cid=False,
            write_provider_hook=True,
        )

        result = self.run_python(
            "--yes",
            "--exclude-tag-lines",
            "1",
            "--recreate-excluded-services",
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        self.assertIn(
            "wud.tag.exclude=^5\\.2\\.0$$",
            compose_file.read_text(encoding="utf-8"),
        )
        calls = self.calls()
        self.assertRegex(
            calls,
            r"compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build gluetun qbittorrent",
        )
        self.assertNotRegex(calls, r"compose -f docker-compose.yml up -d .*--no-deps")
    def test_exclude_tag_line_recreates_only_successful_label_writes(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        app_stack = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        worker_stack = self.make_stack(
            "worker",
            [("worker", "registry.example.com/repo/app:1.1", "cid-worker")],
        )
        options = UpdaterOptions(
            docker_base=self.base,
            wud_file=self.wud_file,
            log_dir=self.log_dir,
            max_wait=0,
            assume_yes=True,
            no_color=True,
            exclude_tag_lines="1",
            recreate_excluded_services=True,
            db_path=self.db_path,
        )
        runner = UpdateFromWudRunner(
            options,
            environ=self.env,
            command_runner=CommandRunner(env=self.env),
        )

        def flaky_apply_compose_tag_exclusions(
            compose_path: Path,
            updates: tuple[TagExclusionUpdate, ...],
            *,
            existing_exact_tags: dict[str, set[str]],
        ) -> object:
            if compose_path.parent.name == "worker":
                raise ComposeTagRewriteError("synthetic label write failure")
            return apply_compose_tag_exclusions(
                compose_path,
                updates,
                existing_exact_tags=existing_exact_tags,
            )

        with mock.patch(
            "wudup.compose_rewrite.apply_compose_tag_exclusions",
            side_effect=flaky_apply_compose_tag_exclusions,
        ):
            result = runner.run()

        self.assertEqual(result, 1)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:1.0 tag=2.0\n",
        )
        calls = self.calls()
        self.assertIn(
            f"{app_stack}\tcompose -f docker-compose.yml up -d "
            "--remove-orphans --pull never --no-build --no-deps app",
            calls,
        )
        self.assertNotIn(
            f"{worker_stack}\tcompose -f docker-compose.yml up -d "
            "--remove-orphans --pull never --no-build --no-deps worker",
            calls,
        )
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (app_stack / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "wud.tag.exclude",
            (worker_stack / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "tag-exclusion-label-failed")
    def test_exclude_tag_line_failure_leaves_line_pending(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
            "services:\n  app:\n    image: repo/app:1.0\n    labels: unsupported\n",
            encoding="utf-8",
        )

        result = self.run_python("--yes", "--exclude-tag-lines", "1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:1.0 tag=2.0\n",
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(
            pending[0]["status_reason"],
            "tag-exclusion-compose-label-unsupported",
        )
    def test_exclude_tag_line_materializes_service_merged_labels(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
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
                    "",
                ]
            ),
            encoding="utf-8",
        )

        result = self.run_python("--yes", "--exclude-tag-lines", "1")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("wud.tag.exclude: ^beta"), 1)
        self.assertIn("wud.tag.exclude: (?:^beta)|(?:^2\\.0$$)", content)
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "resolved")
        self.assertEqual(pending[0]["status_reason"], "tag-excluded")
