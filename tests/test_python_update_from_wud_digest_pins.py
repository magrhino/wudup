from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from tests.update_from_wud_helpers import (
    FailingManifestResolver,
    FakeDockerTestCase,
    UpdateFromWudRunnerTestCase,
    manifest_image,
    manifest_index,
    manifest_index_digest,
    verbose_manifest_item,
)

from wudup.command import CommandRunner
from wudup.compose import (
    ComposeStack,
    ServiceImage,
)
from wudup.config import load_config
from wudup.digest_verifier import (
    DigestCheckResult,
    DigestResolveResult,
    DigestVerifier,
    DockerManifestResolver,
)
from wudup.docker_cli import DockerCli
from wudup.plans import build_dry_run_plan
from wudup.updater import (
    UpdateFromWudRunner,
)
from wudup.updater_digest_pin import digest_pin_update_from_values
from wudup.updater_models import (
    UpdaterOptions,
)


class UpdateFromWudDigestPinTests(UpdateFromWudRunnerTestCase):
    def test_digest_pin_tag_update_writes_pinned_compose_and_metadata(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:1.0", "old", "sha256:old")
        self.set_image_after_pull("repo/app:2.0", "new", "sha256:index")
        self.set_image_state("repo/app@sha256:index", "new", "sha256:index")
        self.set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index("sha256:child"),
        )
        self.set_manifest_verbose_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:index", "sha256:child"),
        )

        status, stdout, stderr = self.run_direct(
            allow_tag_updates=True,
            digest_pin_updates=True,
        )

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("# wudup.resolved-tag=2.0", content)
        self.assertIn("image: repo/app@sha256:index", content)
        self.assertIn("wud.tag.include=^2\\.0$$", content)
        calls = self.calls()
        self.assertRegex(calls, r"manifest inspect --verbose docker.io/repo/app:2.0")
        self.assertRegex(calls, r"manifest inspect repo/app:2.0")
        self.assertEqual(
            calls.count("manifest inspect --verbose docker.io/repo/app:2.0"),
            2,
        )
        self.assertNotRegex(calls, r"(?m)^manifest inspect docker\.io/repo/app:2\.0$")
        self.assertRegex(calls, r"compose -f docker-compose.yml pull app")
        pending = self.db_rows("SELECT * FROM pending_updates")
        events = self.db_rows("SELECT * FROM update_events")
        known = self.db_rows("SELECT * FROM known_images")
        self.assertEqual(events[0]["target_image"], "repo/app@sha256:index")
        self.assertEqual(events[0]["new_digest"], "sha256:index")
        self.assertEqual(events[0]["digest_source_image"], "repo/app:1.0")
        self.assertEqual(events[0]["digest_resolved_tag"], "2.0")
        self.assertEqual(events[0]["digest_watch_tag"], "2.0")
        self.assertEqual(events[0]["digest_target_digest"], "sha256:index")
        self.assertEqual(events[0]["digest_final_image"], "repo/app@sha256:index")
        self.assertEqual(events[0]["digest_provenance_source"], "apply")
        self.assertEqual(events[0]["digest_provenance_confidence"], "verified")
        self.assertEqual(pending[0]["digest_source_image"], "repo/app:1.0")
        self.assertEqual(pending[0]["digest_resolved_tag"], "2.0")
        self.assertEqual(pending[0]["digest_watch_tag"], "2.0")
        self.assertEqual(pending[0]["digest_target_digest"], "sha256:index")
        self.assertEqual(pending[0]["digest_final_image"], "repo/app@sha256:index")
        self.assertEqual(pending[0]["digest_provenance_source"], "apply")
        self.assertEqual(known[0]["image"], "repo/app@sha256:index")
        self.assertEqual(known[0]["digest_source_image"], "repo/app:1.0")
        self.assertEqual(known[0]["digest_resolved_tag"], "2.0")
        self.assertEqual(known[0]["digest_target_digest"], "sha256:index")
        self.assertEqual(known[0]["digest_provenance_source"], "apply")
    def test_digest_pin_os_error_records_failure_without_tag_rollback(self) -> None:
        self.prepare_digest_pin_latest_update()

        with mock.patch(
            "wudup.compose_rewrite.apply_compose_digest_pins",
            side_effect=OSError("write denied"),
        ):
            status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 1, stderr + stdout)
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("phase=compose-digest-pin", report)
        self.assertIn("reason=compose-digest-pin-failed", report)
        self.assertIn("write denied", report)
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "compose-digest-pin-failed")
    def test_digest_pin_empty_apply_records_failure_without_tag_rollback(self) -> None:
        self.prepare_digest_pin_latest_update()

        with mock.patch(
            "wudup.compose_rewrite.apply_compose_digest_pins",
            return_value=(),
        ):
            status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 1, stderr + stdout)
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("phase=compose-digest-pin", report)
        self.assertIn("reason=compose-digest-pin-failed", report)
        self.assertIn("No compose image lines were digest pinned.", report)
    def test_digest_pin_verification_matches_canonical_compose_image(self) -> None:
        stack = ComposeStack(
            index=1,
            directory=self.root,
            file="docker-compose.yml",
            name="app",
            images=("docker.io/repo/app:2.0",),
            service_images=(ServiceImage("app", "docker.io/repo/app:2.0"),),
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        self.set_image_state("docker.io/repo/app:2.0", "new", "sha256:index")
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        runner = UpdateFromWudRunner(
            UpdaterOptions(
                docker_base=self.base,
                wud_file=self.wud_file,
                log_dir=self.log_dir,
                max_wait=0,
                digest_pin_updates=True,
                no_color=True,
            ),
            environ=self.env,
            command_runner=command_runner,
            digest_verifier=DigestVerifier(
                docker,
                primary_resolver=FailingManifestResolver(),
                fallback_resolver=DockerManifestResolver(docker),
            ),
        )

        self.assertTrue(
            runner._verify_digest_pin_updates(
                stack,
                (
                    digest_pin_update_from_values(
                        old_image="repo/app:1.0",
                        resolved_tag="2.0",
                        planned_digest="sha256:index",
                        services=("app",),
                    ),
                ),
                stack.images,
            )
        )
    def test_digest_pin_verification_checks_all_updates_after_failure(self) -> None:
        stack = ComposeStack(
            index=1,
            directory=self.root,
            file="docker-compose.yml",
            name="app",
            images=("docker.io/repo/second:2.0",),
            service_images=(
                ServiceImage("second", "docker.io/repo/second:2.0"),
            ),
        )
        updates = (
            digest_pin_update_from_values(
                old_image="repo/first:1.0",
                resolved_tag="2.0",
                planned_digest="sha256:first",
                services=("first",),
            ),
            digest_pin_update_from_values(
                old_image="repo/second:1.0",
                resolved_tag="2.0",
                planned_digest="sha256:second",
                services=("second",),
            ),
        )
        digest_verifier = mock.Mock()
        digest_verifier.verify.return_value = DigestCheckResult(
            True,
            "verified",
            "digest-match",
        )
        runner = self.make_runner(digest_verifier=digest_verifier)

        with mock.patch.object(
            runner.lifecycle,
            "_verify_digest_pin_update_target",
            side_effect=(
                DigestResolveResult(False, "mismatch", "stale-digest"),
                DigestResolveResult(
                    True,
                    "verified",
                    "digest-match",
                    digest="sha256:second",
                ),
            ),
        ) as resolve:
            verified = runner._verify_digest_pin_updates(
                stack,
                updates,
                stack.images,
            )

        self.assertFalse(verified)
        self.assertEqual(resolve.call_count, 2)
        digest_verifier.verify.assert_called_once_with(
            "docker.io/repo/second:2.0",
            "sha256:second",
        )
    def test_digest_pin_verification_checks_all_updates_after_digest_failure(
        self,
    ) -> None:
        stack = ComposeStack(
            index=1,
            directory=self.root,
            file="docker-compose.yml",
            name="app",
            images=(
                "docker.io/repo/first:2.0",
                "docker.io/repo/second:2.0",
            ),
            service_images=(
                ServiceImage("first", "docker.io/repo/first:2.0"),
                ServiceImage("second", "docker.io/repo/second:2.0"),
            ),
        )
        updates = (
            digest_pin_update_from_values(
                old_image="repo/first:1.0",
                resolved_tag="2.0",
                planned_digest="sha256:first",
                services=("first",),
            ),
            digest_pin_update_from_values(
                old_image="repo/second:1.0",
                resolved_tag="2.0",
                planned_digest="sha256:second",
                services=("second",),
            ),
        )
        digest_verifier = mock.Mock()
        digest_verifier.verify.side_effect = (
            DigestCheckResult(False, "mismatch", "digest-mismatch"),
            DigestCheckResult(True, "verified", "digest-match"),
        )
        runner = self.make_runner(digest_verifier=digest_verifier)

        with mock.patch.object(
            runner.lifecycle,
            "_verify_digest_pin_update_target",
            side_effect=(
                DigestResolveResult(
                    True,
                    "verified",
                    "digest-match",
                    digest="sha256:first",
                ),
                DigestResolveResult(
                    True,
                    "verified",
                    "digest-match",
                    digest="sha256:second",
                ),
            ),
        ) as resolve:
            verified = runner._verify_digest_pin_updates(
                stack,
                updates,
                stack.images,
            )

        self.assertFalse(verified)
        self.assertEqual(
            resolve.call_args_list,
            [
                mock.call(updates[0]),
                mock.call(updates[1]),
            ],
        )
        self.assertEqual(
            digest_verifier.verify.call_args_list,
            [
                mock.call("docker.io/repo/first:2.0", "sha256:first"),
                mock.call("docker.io/repo/second:2.0", "sha256:second"),
            ],
        )
    def test_digest_pin_plan_includes_digest_actions_and_hashes_digest(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        self.set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:first", "sha256:child"),
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "true",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=True,
            environ=self.env,
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:second", "sha256:child"),
        )
        moved_plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=True,
            environ=self.env,
        )

        self.assertEqual(plan.status, "ready")
        self.assertTrue(plan.digest_pin_updates)
        self.assertEqual(plan.stacks[0].lines[0].action, "digest-pin")
        self.assertEqual(plan.stacks[0].lines[0].target_image, "repo/app@sha256:first")
        self.assertEqual(
            plan.stacks[0].digest_pin_updates[0].planned_digest,
            "sha256:first",
        )
        self.assertIn(
            "compose-digest-pin",
            {action.kind for action in plan.stacks[0].actions},
        )
        self.assertNotEqual(plan.plan_id, moved_plan.plan_id)
    def test_digest_pin_plan_accepts_tagged_digest_only_latest_child(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "true",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "ready")
        self.assertTrue(plan.digest_pin_updates)
        self.assertEqual(plan.stacks[0].lines[0].action, "digest-pin")
        self.assertEqual(plan.stacks[0].lines[0].desired_tag, "")
        self.assertEqual(plan.stacks[0].lines[0].target_image, "repo/app@sha256:child")
        digest_pin = plan.stacks[0].digest_pin_updates[0]
        self.assertEqual(digest_pin.resolved_tag, "latest")
        self.assertEqual(digest_pin.watch_tag, "latest")
        self.assertEqual(digest_pin.planned_digest, "sha256:child")
        self.assertEqual(digest_pin.final_image, "repo/app@sha256:child")
    def test_digest_pin_plan_uses_label_fallback_for_unpin_when_disabled(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        (stack_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "ready")
        self.assertFalse(plan.digest_pin_updates)
        self.assertEqual(plan.summary.matched_target_count, 1)
        self.assertEqual(plan.targets[0].action, "digest-unpin")
        self.assertEqual(plan.stacks[0].lines[0].action, "digest-unpin")
        self.assertEqual(plan.stacks[0].lines[0].target_image, "repo/app:latest")
        self.assertEqual(
            plan.stacks[0].digest_unpin_updates[0].source_image,
            "repo/app@sha256:old",
        )
    def test_tag_update_digest_metadata_does_not_plan_digest_unpin(self) -> None:
        digest = "sha256:" + ("a" * 64)
        self.wud_file.write_text(
            f"repo/app:1.0 tag=2.0 sha256={digest}\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        (stack_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^1\\.0$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=True,
            environ=self.env,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.summary.matched_target_count, 0)
        self.assertEqual(plan.targets[0].action, "unmatched")
        self.assertNotEqual(plan.targets[0].action, "digest-unpin")
        self.assertEqual(plan.stacks, ())
    def test_digest_pin_plan_blocks_stale_tagged_digest_only_latest_child(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:stale\n",
            encoding="utf-8",
        )
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:current"),
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "true",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertFalse(plan.can_apply)
        self.assertEqual(plan.issues[0].code, "digest-pin-digest-stale")
        self.assertIn("Digest-pin target moved", plan.issues[0].message)
        self.assertEqual(plan.stacks[0].digest_pin_updates, ())
    def test_digest_pin_plan_blocks_conflicting_tagged_digest_only_digests(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:first\n"
            "repo/app:latest@sha256:second\n",
            encoding="utf-8",
        )
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "true",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1, 2),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.issues[0].code, "digest-pin-conflict")
        self.assertIn("Conflicting digest-pin digests", plan.issues[0].message)
        self.assertEqual(plan.stacks[0].digest_pin_updates, ())
    def test_digest_pin_apply_rejects_moved_planned_digest(self) -> None:
        self.wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
        stack_dir = self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:1.0", "old", "sha256:old")
        self.set_image_after_pull("repo/app:2.0", "new", "sha256:moved")
        self.set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:moved", "sha256:child"),
        )
        planned = (
            digest_pin_update_from_values(
                old_image="repo/app:1.0",
                resolved_tag="2.0",
                planned_digest="sha256:planned",
                services=("app",),
            ),
        )

        progress = []
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        options = UpdaterOptions(
            docker_base=self.base,
            wud_file=self.wud_file,
            log_dir=self.log_dir,
            max_wait=0,
            assume_yes=True,
            allow_tag_updates=True,
            digest_pin_updates=True,
            digest_pin_plan=planned,
            no_color=True,
            db_path=self.db_path,
        )
        runner = UpdateFromWudRunner(
            options,
            environ=self.env,
            command_runner=command_runner,
            digest_verifier=DigestVerifier(
                docker,
                primary_resolver=FailingManifestResolver(),
                fallback_resolver=DockerManifestResolver(docker),
            ),
            progress_callback=progress.append,
        )
        stdout_buf = StringIO()
        stderr_buf = StringIO()

        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            status = runner.run()

        stdout = stdout_buf.getvalue()
        stderr = stderr_buf.getvalue()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:1.0 tag=2.0\n",
        )
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("image: repo/app:1.0", content)
        self.assertNotIn("wudup.resolved-tag", content)
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(
            pending[0]["status_reason"],
            "digest-pin-verification-failed",
        )
        pull_failures = [
            event
            for event in progress
            if event.phase == "pull" and event.status == "failure"
        ]
        self.assertEqual(len(pull_failures), 1)
        self.assertEqual(pull_failures[0].stack, "app")
        self.assertEqual(pull_failures[0].services, ("app",))
        self.assertIn("digest", pull_failures[0].message.lower())
    def test_stale_digest_preflight_rejects_moved_digest_pin_target(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:planned\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:planned")
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:moved-index", "sha256:moved"),
        )
        planned = (
            digest_pin_update_from_values(
                old_image="repo/app:latest",
                resolved_tag="latest",
                planned_digest="sha256:planned",
                services=("app",),
            ),
        )

        status, stdout, stderr = self.run_direct(
            digest_pin_updates=True,
            digest_pin_plan=planned,
        )

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("image: repo/app:latest", content)
        self.assertNotIn("wudup.resolved-tag", content)
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
        self.assertNotRegex(self.calls(), r"compose -f .* up -d")
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "stale-pending-digest")
    def test_digest_pin_apply_writes_tagged_digest_only_latest_child(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:latest", "sha256:old-config", "sha256:old")
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_image_state(
            "repo/app@sha256:child",
            "sha256:new-config",
            "sha256:docker-repodigest",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        self.set_manifest_stdout(
            "docker.io/repo/app@sha256:child",
            manifest_image("sha256:new-config"),
        )

        status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("# wudup.resolved-tag=latest", content)
        self.assertIn("image: repo/app@sha256:child", content)
        events = self.db_rows("SELECT * FROM update_events")
        pending = self.db_rows("SELECT * FROM pending_updates")
        known = self.db_rows("SELECT * FROM known_images")
        self.assertEqual(events[0]["target_image"], "repo/app@sha256:child")
        self.assertEqual(events[0]["new_digest"], "sha256:child")
        self.assertEqual(events[0]["digest_source_image"], "repo/app:latest")
        self.assertEqual(events[0]["digest_resolved_tag"], "latest")
        self.assertEqual(events[0]["digest_watch_tag"], "latest")
        self.assertEqual(events[0]["digest_target_digest"], "sha256:child")
        self.assertEqual(events[0]["digest_final_image"], "repo/app@sha256:child")
        self.assertEqual(events[0]["digest_provenance_source"], "apply")
        self.assertEqual(events[0]["digest_provenance_confidence"], "verified")
        self.assertEqual(pending[0]["digest_source_image"], "repo/app:latest")
        self.assertEqual(pending[0]["digest_resolved_tag"], "latest")
        self.assertEqual(pending[0]["digest_target_digest"], "sha256:child")
        self.assertEqual(pending[0]["digest_provenance_source"], "apply")
        self.assertEqual(known[0]["image"], "repo/app@sha256:child")
        self.assertTrue(known[0]["digest"].endswith("@sha256:docker-repodigest"))
        self.assertEqual(known[0]["digest_target_digest"], "sha256:child")
        self.assertEqual(known[0]["digest_final_image"], "repo/app@sha256:child")
        self.assertEqual(known[0]["digest_provenance_source"], "apply")
    def test_digest_pin_only_health_failure_rolls_back_compose(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:latest", "sha256:old-config", "sha256:old")
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_image_state(
            "repo/app@sha256:child",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        self.set_manifest_stdout(
            "docker.io/repo/app@sha256:child",
            manifest_image("sha256:new-config"),
        )
        (self.fake_root / "containers" / "cid-app.healthlog").write_text(
            "digest-pinned image failed health check\n",
            encoding="utf-8",
        )
        hook = self.fake_root / "post-up-hook"
        hook.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "compose_file=\"${2:?compose file is required}\"\n"
            "if grep -q 'repo/app@sha256:child' \"$compose_file\"; then\n"
            "  printf '/cid-app|running|unhealthy|1|0\\n' > \"${FAKE_DOCKER_ROOT:?}/containers/cid-app.summary\"\n"
            "else\n"
            "  printf '/cid-app|running|healthy|0|0\\n' > \"${FAKE_DOCKER_ROOT:?}/containers/cid-app.summary\"\n"
            "fi\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:latest@sha256:child\n",
        )
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("image: repo/app:latest", content)
        self.assertNotIn("image: repo/app@sha256:child", content)
        self.assertNotIn("wudup.resolved-tag", content)
        incidents = sorted(stack_dir.glob("error-*.logs"))
        self.assertTrue(incidents)
        incident = incidents[-1].read_text(encoding="utf-8")
        self.assertIn("reason=health-failed", incident)
        self.assertIn("manual_review_required=no", incident)
        pending = self.db_rows("SELECT * FROM pending_updates")
        events = self.db_rows("SELECT * FROM update_events")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "health-failed")
        self.assertEqual(
            json.loads(events[0]["metadata_json"]),
            {
                "failure_phase": "health",
                "health_evidence": "timed_out",
                "reason": "health-failed",
            },
        )
    def test_digest_pin_apply_revalidates_tagged_digest_with_verbose_docker(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        self.set_image_state("repo/app:latest", "sha256:old-config", "sha256:old")
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_image_state(
            "repo/app@sha256:child",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            [verbose_manifest_item("sha256:child")],
        )

        status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("image: repo/app@sha256:child", content)
        calls = self.calls()
        self.assertEqual(
            calls.count("manifest inspect --verbose docker.io/repo/app:latest"),
            2,
        )
        self.assertTrue(
            all(
                "--verbose" in call.split()
                for call in calls.splitlines()
                if call.startswith("manifest inspect ")
                and "docker.io/repo/app:latest" in call.split()
            ),
            calls,
        )
    def test_digest_pin_apply_updates_existing_tagged_digest_pin(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    # wudup.resolved-tag=latest",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.set_image_state(
            "repo/app@sha256:old",
            "sha256:old-config",
            "sha256:old",
        )
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_image_state(
            "repo/app@sha256:child",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        self.set_manifest_stdout(
            "docker.io/repo/app@sha256:child",
            manifest_image("sha256:new-config"),
        )

        status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("# wudup.resolved-tag=latest", content)
        self.assertIn("image: repo/app@sha256:child", content)
        self.assertIn("wud.tag.include=^latest$$", content)
        self.assertRegex(self.calls(), r"compose -f docker-compose.yml pull app")
        events = self.db_rows("SELECT * FROM update_events")
        self.assertEqual(events[0]["target_image"], "repo/app@sha256:child")
    def test_digest_pin_apply_updates_existing_tagged_digest_pin_with_tag_ref(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack(
            "app",
            [("app", "repo/app:latest@sha256:old", "cid-app")],
        )
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    # wudup.resolved-tag=latest",
                    "    image: repo/app:latest@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.set_image_state(
            "repo/app:latest@sha256:old",
            "sha256:old-config",
            "sha256:old",
        )
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_image_state(
            "repo/app@sha256:child",
            "sha256:new-config",
            "sha256:child",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )

        status, stdout, stderr = self.run_direct(digest_pin_updates=True)

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("# wudup.resolved-tag=latest", content)
        self.assertIn("image: repo/app@sha256:child", content)
        self.assertNotIn("image: repo/app:latest@sha256:old", content)
        self.assertRegex(self.calls(), r"compose -f docker-compose.yml pull app")
        events = self.db_rows("SELECT * FROM update_events")
        self.assertEqual(events[0]["target_image"], "repo/app@sha256:child")
    def test_digest_pin_disabled_does_not_rematch_existing_tagged_digest_pin(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    # wudup.resolved-tag=latest",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.set_image_state(
            "repo/app@sha256:old",
            "sha256:old-config",
            "sha256:old",
        )

        status, stdout, stderr = self.run_direct(digest_pin_updates=False)

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/app:latest@sha256:child\n",
        )
        self.assertIn(
            "image: repo/app@sha256:old",
            compose_file.read_text(encoding="utf-8"),
        )
        self.assertNotRegex(self.calls(), r"compose -f docker-compose.yml pull app")
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["status_reason"], "unmatched")
    def test_digest_pin_plan_adds_unpin_for_existing_pin_when_disabled(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        (stack_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    # wudup.resolved-tag=latest",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "false",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "ready")
        self.assertFalse(plan.digest_pin_updates)
        self.assertEqual(plan.summary.matched_target_count, 1)
        self.assertEqual(plan.targets[0].action, "digest-unpin")
        self.assertEqual(plan.issues, ())
        self.assertEqual(plan.stacks[0].lines[0].action, "digest-unpin")
        self.assertEqual(plan.stacks[0].digest_unpin_updates[0].tag_image, "repo/app:latest")


    def test_digest_unpin_plan_reports_blank_transform_label(self) -> None:
        self.wud_file.write_text(
            "repo/app:4.0.19.2979-ls321@sha256:child\n",
            encoding="utf-8",
        )
        stack_dir = self.make_stack("app", [("app", "repo/app@sha256:old", "cid-app")])
        (stack_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    # wudup.resolved-tag=4.0.19.2979-ls321",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^4\\.0\\.19\\.2979-ls321$$",
                    "      - wud.tag.transform=",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(
            {
                "DOCKER_BASE": str(self.base),
                "WUD_OUT_FILE": str(self.wud_file),
                "WUD_LOG_DIR": str(self.log_dir),
                "WUD_DIGEST_PIN_UPDATES": "false",
            },
            home=str(self.root),
        )

        plan = build_dry_run_plan(
            config,
            line_numbers=(1,),
            allow_tag_updates=False,
            environ=self.env,
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn(
            "compose-digest-unpin-unsupported",
            [issue.code for issue in plan.issues],
        )


class DigestPinNoTagRequiredTests(FakeDockerTestCase):
    """Tests for _validate_digest_pin_plan rejecting lines without resolved tags."""

    def test_digest_pin_without_tag_rejects_at_preflight(self) -> None:
        """digest_pin_updates=True without a tag token should fail at preflight."""
        self.wud_file.write_text("repo/app:1.0\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:1.0", "cid-app")])

        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        options = UpdaterOptions(
            docker_base=self.base,
            wud_file=self.wud_file,
            log_dir=self.log_dir,
            max_wait=0,
            assume_yes=True,
            allow_tag_updates=True,
            digest_pin_updates=True,
            no_color=True,
            db_path=self.db_path,
        )
        runner = UpdateFromWudRunner(
            options,
            environ=self.env,
            command_runner=command_runner,
            digest_verifier=DigestVerifier(
                docker,
                primary_resolver=FailingManifestResolver(),
                fallback_resolver=DockerManifestResolver(docker),
            ),
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = runner.run()

        # A non-tag-update line with digest_pin_updates=True should fail closed
        self.assertEqual(status, 1)
