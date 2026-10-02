from __future__ import annotations

from pathlib import Path
from unittest import mock

from tests.update_from_wud_helpers import UpdateFromWudRunnerTestCase

from wudup import updater_tag_exclusions
from wudup.compose import ComposeStack, ServiceImage


class UpdateFromWudTagExclusionRecreateTests(UpdateFromWudRunnerTestCase):
    """`--recreate-excluded-services` keeps each service's runtime state."""

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
