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
    def test_exclude_tag_line_recreate_keeps_stopped_service_stopped(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:1.0", None)])

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never "
            "--no-build --no-deps --no-start app",
            calls,
        )
        self.assertNotRegex(calls, r"compose -f docker-compose.yml up -d (?!.*--no-start)")
        self.assertNotIn("--wait", calls)
    def test_exclude_tag_line_recreate_preserves_mixed_runtime_state(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        self.make_stack(
            "app",
            [("app", "repo/app:1.0", "cid-app"), ("worker", "repo/app:1.0", None)],
        )

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 0, stderr + stdout)
        calls = self.calls()
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never "
            "--no-build --no-deps --no-start worker\n",
            calls,
        )
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never "
            "--no-build --no-deps app\n",
            calls,
        )
        self.assertNotRegex(calls, r"up -d [^\n]*(?<!--no-start) worker\n")
    def test_exclude_tag_line_recreate_fails_closed_without_runtime_state(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        (self.fake_root / "ps_fail").write_text("", encoding="utf-8")

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 1, stderr + stdout)
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (stack_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertNotRegex(self.calls(), r"compose -f docker-compose.yml up -d")
        self.assertIn(
            "Could not check the running state of service(s) app",
            stdout + stderr,
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "tag-exclusion-recreate-failed")
    def _write_exclusion_runtime_rows(self, stack_dir: Path, *states: str) -> None:
        runtime_prefix = (
            f"{stack_dir}\t{stack_dir / 'docker-compose.yml'}\t"
            f"{stack_dir.name}\tapp\tFalse\t"
        )
        (self.fake_root / "compose-runtime-all.tsv").write_text(
            "".join(f"{runtime_prefix}{state}\n" for state in states),
            encoding="utf-8",
        )
    def test_exclude_tag_line_recreate_keeps_exited_container_stopped(self) -> None:
        self._assert_exclusion_recreate_keeps_inactive_stopped("exited")
    def test_exclude_tag_line_recreate_keeps_created_container_stopped(self) -> None:
        self._assert_exclusion_recreate_keeps_inactive_stopped("created")
    def _assert_exclusion_recreate_keeps_inactive_stopped(self, state: str) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", None)])
        self._write_exclusion_runtime_rows(stack_dir, state)

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 0, stderr + stdout)
        calls = self.calls()
        self.assertIn("ps --all --format", calls)
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never "
            "--no-build --no-deps --no-start app\n",
            calls,
        )
        self.assertNotRegex(calls, r"compose -f docker-compose.yml up -d (?!.*--no-start)")
    def test_exclude_tag_line_recreate_fails_closed_for_paused_service(self) -> None:
        self._assert_exclusion_recreate_fails_closed("paused")
    def test_exclude_tag_line_recreate_fails_closed_for_scaled_service(self) -> None:
        self._assert_exclusion_recreate_fails_closed("running", "exited")
    def _assert_exclusion_recreate_fails_closed(self, *states: str) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        self._write_exclusion_runtime_rows(stack_dir, *states)

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 1, stderr + stdout)
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (stack_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertNotRegex(self.calls(), r"compose -f docker-compose.yml up -d")
        self.assertIn(
            "Service app has more than one container or an unexpected state",
            stdout + stderr,
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "tag-exclusion-recreate-failed")
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
    def test_exclude_tag_line_recreate_skips_running_consumer_of_stopped_provider(
        self,
    ) -> None:
        compose_file = self.prepare_network_mode_media_stack(
            include_provider_cid=False,
        )

        status, stdout, stderr = self.run_direct(
            exclude_tag_lines="1",
            recreate_excluded_services=True,
        )

        self.assertEqual(status, 1, stderr + stdout)
        self.assertIn(
            "wud.tag.exclude=^5\\.2\\.0$$",
            compose_file.read_text(encoding="utf-8"),
        )
        self.assertNotRegex(self.calls(), r"compose -f docker-compose.yml up -d")
        self.assertIn(
            "Service(s) qbittorrent are running but use the network of gluetun, "
            "which is not running.",
            stdout + stderr,
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "tag-exclusion-recreate-failed")
    def test_running_consumers_of_stopped_providers_follows_provider_chain(
        self,
    ) -> None:
        blocked = updater_tag_exclusions._running_consumers_of_stopped_providers(
            ("vpn-client", "app", "other"),
            ("gluetun",),
            {"app": "vpn-client", "vpn-client": "gluetun", "other": "proxy"},
        )

        self.assertEqual(blocked, ("vpn-client", "app"))
    def _exclusion_recreate_stack(self, *, project_name: str = "app") -> ComposeStack:
        return ComposeStack(
            index=0,
            directory=self.base / "app",
            file="docker-compose.yml",
            name="app",
            images=("repo/app:1.0",),
            service_images=(
                ServiceImage("app", "repo/app:1.0"),
                ServiceImage("worker", "repo/app:1.0"),
            ),
            project_name=project_name,
        )
    def _recreate_with_runtime(
        self,
        runner: mock.Mock,
        running: tuple[str, ...],
        stopped: tuple[str, ...],
    ) -> bool:
        with mock.patch.object(
            updater_tag_exclusions,
            "_tag_exclusion_runtime_state",
            return_value=(running, stopped),
        ):
            return updater_tag_exclusions._recreate_preserving_runtime_state(
                runner,
                self._exclusion_recreate_stack(),
                (*running, *stopped),
                {},
                no_deps=True,
            )
    def test_exclusion_recreate_stops_when_no_start_recreate_fails(self) -> None:
        runner = mock.Mock()
        runner.lifecycle._run_compose_up_no_start.return_value.ok = False

        self.assertFalse(self._recreate_with_runtime(runner, ("app",), ("worker",)))
        runner.lifecycle._run_compose_up_no_start.assert_called_once_with(
            mock.ANY,
            ("worker",),
        )
        runner._run_compose_up.assert_not_called()
        runner.lifecycle._verify_services_stopped.assert_not_called()
    def test_exclusion_recreate_fails_when_running_service_is_unhealthy(self) -> None:
        runner = mock.Mock()
        runner._run_compose_up.return_value.ok = True
        runner._run_compose_up.return_value.wait_handled = False
        runner._wait_for_health.return_value = False

        self.assertFalse(self._recreate_with_runtime(runner, ("app",), ()))
        runner._run_compose_up.assert_called_once_with(
            mock.ANY,
            ("app",),
            no_deps=True,
        )
        runner._wait_for_health.assert_called_once_with(mock.ANY, ("app",))
    def test_exclusion_runtime_state_requires_compose_project_name(self) -> None:
        runner = mock.Mock()

        runtime = updater_tag_exclusions._tag_exclusion_runtime_state(
            runner,
            self._exclusion_recreate_stack(project_name=""),
            ("app",),
        )

        self.assertIsNone(runtime)
        runner.docker.ps_format.assert_not_called()
        message = runner.log.error.call_args.args[0]
        self.assertIn("Could not check the running state of service(s) app", message)
        self.assertIn("Compose project identity is unavailable.", message)
    def test_plan_tag_exclusions_uses_service_scope_without_repo_updates(self) -> None:
        stack = self._exclusion_recreate_stack()
        target = mock.Mock(line_no=1, desired_tag="2.0")
        match = mock.Mock(
            stack=stack,
            service="app",
            compose_image="repo/app:1.0",
            target=target,
        )
        runner = mock.Mock()
        runner._tag_exclusion_repo_updates.return_value = []
        runner._existing_exact_tag_exclusions.return_value = {}

        with mock.patch(
            "wudup.compose_rewrite.render_compose_tag_exclusions",
            return_value=("", ()),
        ):
            updates, failures = updater_tag_exclusions.plan_tag_exclusions(
                runner,
                (match,),
                (stack,),
            )

        self.assertEqual(failures, [])
        self.assertEqual([update.scope for update in updates], ["service"])
        self.assertEqual(updates[0].service, "app")
        self.assertEqual(updates[0].tag, "2.0")
        runner.log.warning.assert_not_called()
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
    def test_exclude_tag_line_refuses_interpolated_exclude_label(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        original = (
            "services:\n"
            "  app:\n"
            "    image: repo/app:1.0\n"
            "    labels:\n"
            "    - wud.tag.exclude=${APP_TAG_EXCLUDE:-^.*-beta$$}\n"
        )
        compose_file.write_text(original, encoding="utf-8")

        status, stdout, stderr = self.run_direct(exclude_tag_lines="1")

        self.assertEqual(status, 1)
        self.assertEqual(compose_file.read_text(encoding="utf-8"), original)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:1.0 tag=2.0\n",
        )
        self.assertIn(
            "Service app wud.tag.exclude label uses a Compose variable",
            stdout + stderr,
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(
            pending[0]["status_reason"],
            "tag-exclusion-compose-label-unsupported",
        )
    def test_exclude_tag_line_warns_when_sibling_label_is_interpolated(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        app_stack = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        worker_stack = self.make_stack(
            "worker",
            [("worker", "repo/app:1.1", "cid-worker")],
        )
        worker_compose = worker_stack / "docker-compose.yml"
        worker_original = (
            "services:\n"
            "  worker:\n"
            "    image: repo/app:1.1\n"
            "    labels:\n"
            "    - wud.tag.exclude=${WORKER_TAG_EXCLUDE}\n"
        )
        worker_compose.write_text(worker_original, encoding="utf-8")

        status, stdout, stderr = self.run_direct(exclude_tag_lines="1")

        self.assertEqual(status, 0, stderr + stdout)
        self.assertIn(
            "wud.tag.exclude=^2\\.0$$",
            (app_stack / "docker-compose.yml").read_text(encoding="utf-8"),
        )
        self.assertEqual(worker_compose.read_text(encoding="utf-8"), worker_original)
        output = stdout + stderr
        self.assertIn(
            "cannot be written to every service using repo/app, so WUDup will "
            "only try the service(s) on this line",
            output,
        )
        self.assertIn(
            "Service worker wud.tag.exclude label uses a Compose variable",
            output,
        )
        rules = self.db_rows("SELECT * FROM tag_exclusion_rules")
        self.assertEqual([rule["scope"] for rule in rules], ["service"])
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
