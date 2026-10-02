"""Retries after an update failed between the image pull and a running container."""

from __future__ import annotations

import json
import shlex
from functools import partial
from types import SimpleNamespace
from unittest import mock

from tests import update_from_wud_helpers
from tests.update_from_wud_helpers import UpdateFromWudRunnerTestCase

from wudup.command import CommandError, CommandResult
from wudup.compose import ServiceImage
from wudup.docker_cli import DockerCli
from wudup.updater_models import ImageState, UpdaterOptions


class UpdateFromWudRetryTests(UpdateFromWudRunnerTestCase):
    def run_update(self, *, mode: str = "stop") -> tuple[int, str]:
        """Run the updater in-process and return its status and combined output."""
        with mock.patch.object(
            update_from_wud_helpers,
            "UpdaterOptions",
            partial(UpdaterOptions, mode=mode),
        ):
            status, stdout, stderr = self.run_direct()
        return status, stderr + stdout

    def test_retry_after_failed_recreate_recreates_container_on_old_image(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "old\n", encoding="utf-8"
        )
        up_fail = self.fake_root / "stacks" / "app" / "up_fail"
        up_fail.write_text("", encoding="utf-8")

        first_status, first_output = self.run_update()

        self.assertEqual(first_status, 1, first_output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        # Stop mode must not leave the old container stopped after the failed
        # up, or the retry would treat the service as intentionally stopped.
        first_calls = self.calls()
        self.assertIn("compose -f docker-compose.yml stop app", first_calls)
        self.assertIn("compose -f docker-compose.yml start app", first_calls)
        self.assertGreater(
            first_calls.index("compose -f docker-compose.yml start app"),
            first_calls.index("compose -f docker-compose.yml stop app"),
        )
        self.assertIn("were started again after it failed", first_output)

        up_fail.unlink()
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")
        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertNotIn("All images up to date", output)
        self.assertIn("still use an older image and will be recreated: app", output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("compose -f docker-compose.yml ps -a -q app", calls)
        self.assertIn("compose -f docker-compose.yml stop app", calls)
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
            calls,
        )
        event = self.db_rows(
            "SELECT status, old_image_id, new_image_id FROM update_events "
            "ORDER BY id DESC LIMIT 1"
        )[0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["old_image_id"], "old")
        self.assertEqual(event["new_image_id"], "new")

    def test_failed_recovery_start_keeps_wud_line_and_gives_start_command(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "old\n", encoding="utf-8"
        )
        stack_state = self.fake_root / "stacks" / "app"
        (stack_state / "up_fail").write_text("", encoding="utf-8")
        (stack_state / "start_fail").write_text("", encoding="utf-8")

        status, output = self.run_update()

        self.assertEqual(status, 1, output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        calls = self.calls()
        # Exactly one recovery attempt, after the stop.
        self.assertEqual(calls.count("compose -f docker-compose.yml start app"), 1)
        command = (
            f"cd {shlex.quote(str(stack_dir))} && "
            "docker compose -f docker-compose.yml start app"
        )
        self.assertIn(
            "Service(s) app were stopped for the update, and starting them again "
            "after the failed update also failed, so they are still stopped and "
            f"not running the new image. Fix the error above, then start them with: "
            f"{command}. The pending update was kept, so rerunning the update after "
            "that is safe and retries it.",
            output,
        )
        self.assertNotIn("rerun the update or start them", output)
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("reason=up-or-health-failed", report)
        self.assertIn(command, report)

    def test_retry_starts_container_whose_start_failed_in_stop_mode(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        start_fail = self.fake_root / "stacks" / "app" / "up_start_fail"
        start_fail.write_text("", encoding="utf-8")

        first_status, first_output = self.run_update()

        self.assertEqual(first_status, 1, first_output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        self.assertIn("compose -f docker-compose.yml start app", self.calls())
        self.assertIn(
            "starting them again after the failed update also failed",
            first_output,
        )

        start_fail.unlink()
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")
        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertNotIn("All images up to date", output)
        self.assertNotIn("will remain stopped", output)
        self.assertIn("They will be started with this update: app", output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
            calls,
        )
        self.assertNotIn("--no-start", calls)
        event = self.db_rows(
            "SELECT metadata_json FROM update_events ORDER BY id DESC LIMIT 1"
        )[0]
        metadata = json.loads(str(event["metadata_json"]))
        self.assertEqual(metadata["runtime_state_before"], "not-running")
        self.assertEqual(metadata["runtime_state_after"], "running")
        self.assertEqual(metadata["stopped_services_after"], [])

    def test_retry_with_newer_image_starts_container_whose_start_failed(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        start_fail = self.fake_root / "stacks" / "app" / "up_start_fail"
        start_fail.write_text("", encoding="utf-8")

        first_status, first_output = self.run_update(mode="live")

        self.assertEqual(first_status, 1, first_output)
        self.assertNotIn("compose -f docker-compose.yml start app", self.calls())

        # WUD published again before the retry, so the image changes too.
        start_fail.unlink()
        self.set_image_after_pull("repo/app:latest", "newer", "sha256:newer")
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")
        status, output = self.run_update(mode="live")

        self.assertEqual(status, 0, output)
        self.assertIn("Image updated: repo/app:latest", output)
        self.assertIn("They will be started with this update: app", output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
            calls,
        )
        self.assertNotIn("--no-start", calls)

    def test_stopped_container_with_old_start_error_stays_stopped(self) -> None:
        # A container that ran before and whose later start failed (for
        # example at boot) may have been left stopped on purpose.
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", None)])
        (self.fake_root / "stacks" / "app" / "cids-all-app.txt").write_text(
            "cid-app\n", encoding="utf-8"
        )
        (self.fake_root / "compose-runtime.tsv").write_text(
            f"{stack_dir}\t{stack_dir / 'docker-compose.yml'}\tapp\tapp\tFalse\texited\n",
            encoding="utf-8",
        )
        (self.fake_root / "containers" / "cid-app.state-error").write_text(
            "error while creating mount source path: no such file or directory\n",
            encoding="utf-8",
        )
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "new\n", encoding="utf-8"
        )
        self.set_image_state("repo/app:latest", "new", "sha256:new")

        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertIn("already stopped and will remain stopped: app", output)
        self.assertIn("All images up to date, skipping restart", output)
        self.assertNotIn("will be started", output)
        self.assertNotIn("compose -f docker-compose.yml up", self.calls())

    def test_retry_keeps_wud_line_when_container_image_cannot_be_read(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "new", "sha256:new")

        status, output = self.run_update()

        self.assertEqual(status, 1, output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        self.assertIn("Could not confirm which image service(s) app are using", output)
        self.assertNotIn("compose -f docker-compose.yml up", self.calls())
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("reason=runtime-image-unverified", report)

    def test_retry_keeps_wud_line_when_container_lookup_fails(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "stacks" / "app" / "ps_fail").write_text("", encoding="utf-8")

        status, output = self.run_update()

        self.assertEqual(status, 1, output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        self.assertIn("Could not confirm which image service(s) app are using", output)
        self.assertNotIn("compose -f docker-compose.yml up", self.calls())

    def test_retry_check_ignores_services_outside_the_wud_line(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack(
            "app",
            [("app", "repo/app:latest", "cid-app"), ("db", "repo/db:latest", "cid-db")],
        )
        (self.fake_root / "containers" / "cid-app.labels").write_text(
            "WUD-UPDATER-RECREATE-STACK=true\n",
            encoding="utf-8",
        )
        self.set_image_state("repo/app:latest", "new", "sha256:new")
        self.set_image_state("repo/db:latest", "db-new", "sha256:db-new")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "new\n", encoding="utf-8"
        )
        (self.fake_root / "containers" / "cid-db.image-id").write_text(
            "db-old\n", encoding="utf-8"
        )

        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertIn("All images up to date, skipping restart", output)
        calls = self.calls()
        self.assertIn("compose -f docker-compose.yml ps -a -q app", calls)
        self.assertNotIn("ps -a -q db", calls)
        self.assertNotIn("compose -f docker-compose.yml up", calls)

    def test_retry_recreates_stopped_container_on_old_image_without_starting(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", None)])
        self.set_image_state("repo/app:latest", "new", "sha256:new")
        stack_state = self.fake_root / "stacks" / "app"
        (stack_state / "cids-all-app.txt").write_text("cid-app\n", encoding="utf-8")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "old\n", encoding="utf-8"
        )

        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps --no-start app",
            calls,
        )
        self.assertNotIn("compose -f docker-compose.yml stop app", calls)

    def test_container_already_on_pulled_image_skips_recreate(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "new\n", encoding="utf-8"
        )

        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertIn("All images up to date, skipping restart", output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("compose -f docker-compose.yml ps -a -q app", calls)
        self.assertNotIn("compose -f docker-compose.yml up", calls)

    def test_retry_recreates_when_old_image_digest_cannot_be_read(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "containers" / "cid-app.image-id").write_text(
            "old\n", encoding="utf-8"
        )
        image_digest = DockerCli.image_digest

        def digest_unless_old(docker: DockerCli, image: str) -> str:
            if image == "old":
                raise CommandError(
                    CommandResult(("docker", "image", "inspect", image), None, 1)
                )
            return image_digest(docker, image)

        with mock.patch.object(
            DockerCli, "image_digest", autospec=True, side_effect=digest_unless_old
        ):
            status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertIn("still use an older image and will be recreated: app", output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        event = self.db_rows(
            "SELECT status, old_image_id, old_digest FROM update_events "
            "ORDER BY id DESC LIMIT 1"
        )[0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["old_image_id"], "old")
        self.assertEqual(event["old_digest"], "")

    def test_failed_recreate_of_stopped_service_does_not_start_it(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", None)])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        (self.fake_root / "stacks" / "app" / "up_fail").write_text("", encoding="utf-8")

        status, output = self.run_update()

        self.assertEqual(status, 1, output)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        calls = self.calls()
        self.assertIn("--no-start app", calls)
        self.assertNotIn("compose -f docker-compose.yml stop app", calls)
        self.assertNotIn("compose -f docker-compose.yml start app", calls)
        self.assertNotIn("were started again", output)

    def test_failed_start_of_service_outside_the_wud_line_stays_stopped(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        stack_dir = self.make_stack(
            "app",
            [("app", "repo/app:latest", "cid-app"), ("db", "repo/db:latest", None)],
        )
        (self.fake_root / "containers" / "cid-app.labels").write_text(
            "WUD-UPDATER-RECREATE-STACK=true\n", encoding="utf-8"
        )
        (self.fake_root / "stacks" / "app" / "cids-all-db.txt").write_text(
            "cid-db\n", encoding="utf-8"
        )
        (self.fake_root / "containers" / "cid-db.state-error").write_text(
            "port is already allocated\n", encoding="utf-8"
        )
        with (self.fake_root / "compose-runtime.tsv").open("a", encoding="utf-8") as file:
            file.write(
                f"{stack_dir}\t{stack_dir / 'docker-compose.yml'}\tapp\tdb\tFalse\tcreated\n"
            )
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        self.set_image_state("repo/db:latest", "db", "sha256:db")

        status, output = self.run_update()

        self.assertEqual(status, 0, output)
        self.assertIn("already stopped and will remain stopped: db", output)
        self.assertNotIn("will be started", output)
        self.assertNotIn("ps -a -q db", self.calls())

    def test_recovery_start_skips_when_no_service_was_stopped(self) -> None:
        lifecycle = self.make_runner(db_path=self.db_path).lifecycle
        state = SimpleNamespace(
            stack=SimpleNamespace(name="app"),
            running_stop_services=(),
            running_services=(),
        )

        with mock.patch.object(lifecycle.compose, "start") as start:
            self.assertEqual(lifecycle._restart_services_stopped_for_update(state), "")

        start.assert_not_called()

    def test_unfinished_update_check_skips_stack_without_selected_services(self) -> None:
        lifecycle = self.make_runner(db_path=self.db_path).lifecycle
        state = SimpleNamespace(
            stack=SimpleNamespace(name="app"),
            matches=(),
            running_services=(),
            stopped_services=(),
        )

        with mock.patch.object(lifecycle, "_check_container_images") as check:
            self.assertIs(lifecycle._find_unfinished_update(state), False)

        check.assert_not_called()

    def test_container_image_check_skips_services_without_a_pulled_image(self) -> None:
        lifecycle = self.make_runner(db_path=self.db_path).lifecycle
        stack = SimpleNamespace(
            directory=self.base / "app",
            file="docker-compose.yml",
            project_directory=None,
            service_images=(
                ServiceImage("app", "repo/app:latest"),
                ServiceImage("worker", "repo/worker:latest"),
                ServiceImage("sidecar", "repo/sidecar:latest"),
            ),
        )
        after = {
            "repo/app:latest": ImageState(image_id="new", digest=""),
            "repo/worker:latest": ImageState(image_id="", digest=""),
        }

        with (
            mock.patch.object(
                lifecycle.compose, "ps_quiet_checked", return_value=["cid-app"]
            ) as ps,
            mock.patch.object(
                lifecycle.docker, "container_image_id", return_value="old"
            ),
        ):
            check = lifecycle._check_container_images(
                stack, ("app", "worker", "sidecar"), after
            )

        self.assertEqual(check.behind, {"app": ("repo/app:latest", "old")})
        self.assertEqual(check.unverified, ())
        self.assertEqual([call.args[2] for call in ps.call_args_list], [("app",)])

