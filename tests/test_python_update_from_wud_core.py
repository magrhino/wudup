from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from tests.update_from_wud_helpers import (
    UpdateFromWudRunnerTestCase,
    manifest_image,
    manifest_index_digest,
)

from wudup.compose import (
    ComposeStack,
    ServiceImage,
)
from wudup.digest_verifier import DigestCheckResult, DigestResolveResult
from wudup.updater_lifecycle_health import _updated_images
from wudup.updater_models import (
    STALE_PENDING_DIGEST_REASON,
    ImageState,
    Match,
)
from wudup.wud_file import parse_wud_text


class UpdateFromWudFacadeTests(unittest.TestCase):
    def test_updater_facade_exposes_runner_entrypoints(self) -> None:
        from wudup import updater

        self.assertTrue(callable(updater.UpdateFromWudRunner))
        self.assertTrue(callable(updater.run_update_from_wud))

    def test_updater_facade_does_not_expose_compose_rewrite_helpers(self) -> None:
        from wudup import updater

        self.assertFalse(hasattr(updater, "apply_compose_tag_updates"))
        self.assertFalse(hasattr(updater, "apply_compose_service_updates"))
        self.assertFalse(hasattr(updater, "rewrite_compose_file"))


class UpdateFromWudCoreTests(UpdateFromWudRunnerTestCase):
    def test_expected_digest_failure_reason_uses_only_failed_requirements(self) -> None:
        stack_dir = self.make_stack(
            "app",
            [
                ("stale", "repo/stale:latest", "cid-stale"),
                ("fresh", "repo/fresh:latest", "cid-fresh"),
            ],
        )
        stack = ComposeStack(
            index=2,
            directory=stack_dir,
            file="docker-compose.yml",
            name="app",
            images=("repo/stale:latest", "repo/fresh:latest"),
            service_images=(
                ServiceImage("stale", "repo/stale:latest"),
                ServiceImage("fresh", "repo/fresh:latest"),
            ),
        )
        targets = parse_wud_text(
            "repo/stale:latest@sha256:stale\n"
            "repo/fresh:latest@sha256:fresh\n"
        ).targets
        matches = (
            Match(
                stack=stack,
                target=targets[0],
                resolved="repo/stale:latest",
                compose_image="repo/stale:latest",
                service="stale",
            ),
            Match(
                stack=stack,
                target=targets[1],
                resolved="repo/fresh:latest",
                compose_image="repo/fresh:latest",
                service="fresh",
            ),
        )
        runner = self.make_runner()

        first_requirement = (stack.index, targets[0].line_no, stack.images[0])
        second_requirement = (stack.index, targets[1].line_no, stack.images[1])
        runner.expected_digest_outcomes.failed.add(first_requirement)
        runner.expected_digest_outcomes.stale.add(first_requirement)
        self.assertEqual(
            runner.lifecycle._expected_digest_failure_reason(matches),
            STALE_PENDING_DIGEST_REASON,
        )

        runner.expected_digest_outcomes.failed.add(second_requirement)
        self.assertEqual(
            runner.lifecycle._expected_digest_failure_reason(matches),
            "expected-digest-not-reached",
        )

        runner.expected_digest_outcomes.stale.add(second_requirement)
        self.assertEqual(
            runner.lifecycle._expected_digest_failure_reason(matches),
            STALE_PENDING_DIGEST_REASON,
        )
    def test_expected_digest_verification_checks_all_requirements(self) -> None:
        stack_dir = self.make_stack(
            "app",
            [
                ("first", "repo/first:latest", "cid-first"),
                ("second", "repo/second:latest", "cid-second"),
            ],
        )
        stack = ComposeStack(
            index=1,
            directory=stack_dir,
            file="docker-compose.yml",
            name="app",
            images=("repo/first:latest", "repo/second:latest"),
            service_images=(
                ServiceImage("first", "repo/first:latest"),
                ServiceImage("second", "repo/second:latest"),
            ),
        )
        targets = parse_wud_text(
            "repo/first:latest@sha256:first\n"
            "repo/second:latest@sha256:second\n"
        ).targets
        matches = tuple(
            Match(
                stack=stack,
                target=target,
                resolved=stack.images[index],
                compose_image=stack.images[index],
                service=stack.service_images[index].service,
            )
            for index, target in enumerate(targets)
        )
        digest_verifier = mock.Mock()
        digest_verifier.verify.side_effect = (
            DigestCheckResult(False, "mismatch", "digest-mismatch"),
            DigestCheckResult(True, "verified", "digest-match"),
        )
        runner = self.make_runner(digest_verifier=digest_verifier)

        verified = runner.lifecycle._verify_expected_digests(
            stack,
            matches,
        )

        self.assertFalse(verified)
        self.assertEqual(digest_verifier.verify.call_count, 2)
    def test_wrapper_default_dry_run_plans_without_mutation(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")

        result = self.run_python("--dry-run", wrapper=True)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "repo/app:latest\n")
        self.assertIn("line 1: repo/app:latest", result.stdout)
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
        self.assertNotRegex(self.calls(), r"compose -f .* up -d")
        self.assertFalse(self.db_path.exists())
    def test_python_updates_matched_service_and_cleans_wud_line(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack(
            "stack",
            [
                ("app", "repo/app:latest", "cid-app"),
                ("db", "repo/db:latest", "cid-db"),
            ],
        )
        self.set_image_state("repo/app:latest", "old-app", "sha256:old-app")
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")
        self.set_image_state("repo/db:latest", "old-db", "sha256:old-db")
        self.set_image_after_pull("repo/db:latest", "new-db", "sha256:new-db")

        result = self.run_python("--yes", "--mode", "stop")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertRegex(calls, r"compose -f docker-compose.yml pull app")
        self.assertRegex(calls, r"compose -f docker-compose.yml stop app")
        self.assertRegex(calls, r"compose -f docker-compose.yml up -d .* app")
        self.assertNotRegex(calls, r"compose -f docker-compose.yml pull db")
        runs = self.db_rows("SELECT * FROM update_runs")
        pending = self.db_rows("SELECT * FROM pending_updates")
        events = self.db_rows("SELECT * FROM update_events")
        known = self.db_rows("SELECT * FROM known_images")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "success")
        self.assertTrue(runs[0]["finished_at"].endswith("+00:00"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "resolved")
        self.assertEqual(pending[0]["status_reason"], "updated")
        self.assertEqual(pending[0]["service_key"], "stack/app")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "success")
        self.assertEqual(events[0]["service_name"], "app")
        self.assertEqual(events[0]["old_image_id"], "old-app")
        self.assertEqual(events[0]["new_image_id"], "new-app")
        self.assertTrue(events[0]["old_digest"].endswith("@sha256:old-app"))
        self.assertTrue(events[0]["new_digest"].endswith("@sha256:new-app"))
        self.assertEqual(json.loads(events[0]["metadata_json"]), {"reason": "updated"})
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["service_key"], "stack/app")
        self.assertEqual(known[0]["image"], "repo/app:latest")
        self.assertEqual(known[0]["image_id"], "new-app")
        self.assertTrue(known[0]["digest"].endswith("@sha256:new-app"))
    def test_python_updates_honors_configured_compose_ignore_paths(self) -> None:
        self.wud_file.write_text("repo/ignored:latest\n", encoding="utf-8")
        self.make_stack("active", [("app", "repo/app:latest", "cid-app")])
        self.make_stack(
            "ignored",
            [("app", "repo/ignored:latest", "cid-ignored")],
            parent=self.base / "old",
        )
        self.set_image_state("repo/ignored:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/ignored:latest", "new", "sha256:new")
        self.env["WUD_COMPOSE_IGNORE_PATHS"] = "old"

        result = self.run_python("--yes")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/ignored:latest\n",
        )
        self.assertNotRegex(self.calls(), r"repo/ignored")
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
    def test_remove_lines_before_run_records_discarded_audit_entries(self) -> None:
        self.wud_file.write_text(
            "repo/app:one\nrepo/app:two\nrepo/app:three\n",
            encoding="utf-8",
        )
        self.make_stack("stack", [("app", "repo/app:two", "cid-app")])
        self.set_image_state("repo/app:two", "old", "sha256:old")
        self.set_image_after_pull("repo/app:two", "new", "sha256:new")

        result = self.run_python(
            "--yes",
            "--only-lines",
            "2",
            "--remove-lines-before-run",
            "1,3",
        )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        pending = self.db_rows(
            "SELECT * FROM pending_updates ORDER BY line_no"
        )
        self.assertEqual(len(pending), 3)
        self.assertEqual(
            [(row["line_no"], row["status"], row["status_reason"]) for row in pending],
            [
                (1, "resolved", "removed-before-run"),
                (2, "resolved", "updated"),
                (3, "resolved", "removed-before-run"),
            ],
        )
    def test_non_ghcr_manifest_unavailable_warns_and_allows_update(self) -> None:
        self.wud_file.write_text("quay.io/acme/app:latest@sha256:good\n", encoding="utf-8")
        self.make_stack("app", [("app", "quay.io/acme/app:latest", "cid-app")])
        self.set_image_state("quay.io/acme/app:latest", "old", "sha256:old")
        self.set_image_after_pull("quay.io/acme/app:latest", "new", "sha256:bad")
        self.set_manifest_failure("quay.io/acme/app:latest", "manifest unavailable")

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        self.assertRegex(self.calls(), r"compose -f docker-compose.yml pull app")
        self.assertRegex(self.calls(), r"compose -f docker-compose.yml up -d .* app")
        pending = self.db_rows("SELECT * FROM pending_updates")
        runs = self.db_rows("SELECT * FROM update_runs")
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual(pending[0]["status"], "resolved")
        self.assertEqual(pending[0]["status_reason"], "updated")
        log_text = max(self.log_dir.glob("update-from-wud-v2-*.log")).read_text(
            encoding="utf-8"
        )
        self.assertIn("Digest verification was inconclusive", log_text)
        self.assertIn("Digest verification reason: manifest-unavailable-untrusted", log_text)
    def test_non_ghcr_platform_digest_allows_registry_verification(self) -> None:
        self.wud_file.write_text("acme/app:latest@sha256:child\n", encoding="utf-8")
        self.make_stack("app", [("app", "quay.io/acme/app:latest", "cid-app")])
        self.set_image_state("quay.io/acme/app:latest", "sha256:old", "sha256:old-index")
        self.set_image_after_pull(
            "quay.io/acme/app:latest",
            "sha256:config",
            "sha256:index",
        )
        self.set_manifest_stdout(
            "quay.io/acme/app:latest",
            manifest_index_digest("sha256:current", "sha256:child"),
        )
        self.set_manifest_stdout(
            "quay.io/acme/app@sha256:child",
            manifest_image("sha256:config"),
        )

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("manifest inspect quay.io/acme/app:latest", calls)
        self.assertIn("manifest inspect quay.io/acme/app@sha256:child", calls)
        self.assertRegex(calls, r"compose -f docker-compose.yml up -d .* app")
    def test_non_ghcr_stale_digest_removes_line_and_reports_stale_input(self) -> None:
        self.wud_file.write_text("quay.io/acme/app:latest@sha256:stale\n", encoding="utf-8")
        self.make_stack("app", [("app", "quay.io/acme/app:latest", "cid-app")])
        self.set_image_state("quay.io/acme/app:latest", "sha256:old", "sha256:old-index")
        self.set_image_after_pull(
            "quay.io/acme/app:latest",
            "sha256:config",
            "sha256:index",
        )
        self.set_manifest_stdout(
            "quay.io/acme/app:latest",
            manifest_index_digest("sha256:current", "sha256:child"),
        )
        self.set_manifest_stdout(
            "quay.io/acme/app@sha256:stale",
            manifest_image("sha256:config"),
        )

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("manifest inspect quay.io/acme/app:latest", calls)
        self.assertNotIn("manifest inspect quay.io/acme/app@sha256:stale", calls)
        self.assertNotRegex(calls, r"compose -f .* pull")
        self.assertNotRegex(calls, r"compose -f .* up -d")
        log_text = max(self.log_dir.glob("update-from-wud-v2-*.log")).read_text(
            encoding="utf-8"
        )
        self.assertIn("Pending WUD entry for line 1 is stale", log_text)
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("reason=stale-pending-digest", report)
        self.assertIn("wud_entries_restored=no", report)
        pending = self.db_rows("SELECT * FROM pending_updates")
        runs = self.db_rows("SELECT * FROM update_runs")
        self.assertEqual(runs[0]["status"], "failure")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "stale-pending-digest")
    def test_ghcr_platform_digest_allows_registryless_wud_line(self) -> None:
        self.wud_file.write_text("acme/app:latest@sha256:child\n", encoding="utf-8")
        self.make_stack("app", [("app", "ghcr.io/acme/app:latest", "cid-app")])
        self.set_image_state("ghcr.io/acme/app:latest", "sha256:old", "sha256:old-index")
        self.set_image_after_pull(
            "ghcr.io/acme/app:latest",
            "sha256:config",
            "sha256:index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/app:latest",
            manifest_index_digest("sha256:current", "sha256:child"),
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/app@sha256:child",
            manifest_image("sha256:config"),
        )

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 0, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("manifest inspect ghcr.io/acme/app:latest", calls)
        self.assertIn("manifest inspect ghcr.io/acme/app@sha256:child", calls)
        self.assertRegex(calls, r"compose -f docker-compose.yml up -d .* app")
    def test_ghcr_stale_digest_removes_line_and_reports_stale_input(self) -> None:
        self.wud_file.write_text("acme/app:latest@sha256:stale\n", encoding="utf-8")
        self.make_stack("app", [("app", "ghcr.io/acme/app:latest", "cid-app")])
        self.set_image_state("ghcr.io/acme/app:latest", "sha256:old", "sha256:old-index")
        self.set_image_after_pull(
            "ghcr.io/acme/app:latest",
            "sha256:config",
            "sha256:index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/app:latest",
            manifest_index_digest("sha256:current", "sha256:child"),
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/app@sha256:stale",
            manifest_image("sha256:config"),
        )

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), "")
        calls = self.calls()
        self.assertIn("manifest inspect ghcr.io/acme/app:latest", calls)
        self.assertNotIn("manifest inspect ghcr.io/acme/app@sha256:stale", calls)
        self.assertNotRegex(calls, r"compose -f .* pull")
        self.assertNotRegex(calls, r"compose -f .* up -d")
        log_text = max(self.log_dir.glob("update-from-wud-v2-*.log")).read_text(
            encoding="utf-8"
        )
        self.assertIn("Pending WUD entry for line 1 is stale", log_text)
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("reason=stale-pending-digest", report)
        self.assertIn("wud_entries_restored=no", report)
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "stale-pending-digest")
    def test_later_stale_stack_allows_unaffected_stack_update(self) -> None:
        self.wud_file.write_text(
            "ghcr.io/acme/current:latest@sha256:current\n"
            "ghcr.io/acme/stale:latest@sha256:stale\n",
            encoding="utf-8",
        )
        self.make_stack(
            "a-current",
            [("app", "ghcr.io/acme/current:latest", "cid-current")],
        )
        self.make_stack(
            "z-stale",
            [("app", "ghcr.io/acme/stale:latest", "cid-stale")],
        )
        self.set_image_state(
            "ghcr.io/acme/current:latest",
            "sha256:old-current",
            "sha256:old-current-index",
        )
        self.set_image_state(
            "ghcr.io/acme/stale:latest",
            "sha256:old-stale",
            "sha256:old-stale-index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/current:latest",
            manifest_index_digest("sha256:current", "sha256:current-child"),
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/stale:latest",
            manifest_index_digest("sha256:moved", "sha256:moved-child"),
        )

        self.set_image_after_pull(
            "ghcr.io/acme/current:latest", "sha256:new-current", "sha256:current"
        )
        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "",
        )
        calls = self.calls()
        self.assertIn("manifest inspect ghcr.io/acme/current:latest", calls)
        self.assertIn("manifest inspect ghcr.io/acme/stale:latest", calls)
        self.assertIn("pull app", calls)
        self.assertNotRegex(calls, r"/z-stale\tcompose -f .* (?:pull|stop|up -d)")
        self.assertIn("Completed with 1 failure(s). Failed: z-stale/app.", stderr)
        self.assertIn("Refresh pending updates for: z-stale/app; then retry.", stderr)
        pending = self.db_rows("SELECT * FROM pending_updates ORDER BY line_no")
        self.assertEqual(
            [(row["status"], row["status_reason"]) for row in pending],
            [
                ("resolved", "updated"),
                ("failed", "stale-pending-digest"),
            ],
        )
    def test_mixed_identity_stale_digest_restores_shared_pending_line(self) -> None:
        pending_line = "acme/app:latest@sha256:shared\n"
        self.wud_file.write_text(pending_line, encoding="utf-8")
        self.make_stack(
            "a-current",
            [("app", "ghcr.io/acme/app:latest", "cid-current")],
        )
        self.make_stack(
            "z-stale",
            [("app", "quay.io/acme/app:latest", "cid-stale")],
        )
        self.set_image_state(
            "ghcr.io/acme/app:latest",
            "sha256:old-current",
            "sha256:old-current-index",
        )
        self.set_image_state(
            "quay.io/acme/app:latest",
            "sha256:old-stale",
            "sha256:old-stale-index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/app:latest",
            manifest_index_digest("sha256:shared", "sha256:current-child"),
        )
        self.set_manifest_stdout(
            "quay.io/acme/app:latest",
            manifest_index_digest("sha256:moved", "sha256:moved-child"),
        )

        self.set_image_after_pull(
            "ghcr.io/acme/app:latest", "sha256:new-current", "sha256:shared"
        )
        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), pending_line)
        self.assertIn("pull app", self.calls())
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(pending[0]["status_reason"], "preflight-skipped")
        events = self.db_rows("SELECT * FROM update_events ORDER BY stack_name")
        self.assertEqual(
            {
                row["stack_name"]: json.loads(row["metadata_json"])["reason"]
                for row in events
            },
            {
                "a-current": "updated",
                "z-stale": "stale-pending-digest",
            },
        )
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertIn("stack=z-stale", report)
        self.assertNotIn("stack=a-current", report)
        self.assertIn("wud_entries_restored=yes", report)

    def test_postpull_mixed_identity_stale_digest_restores_shared_line(self) -> None:
        pending_line = "acme/app@sha256:shared\n"
        self.wud_file.write_text(pending_line, encoding="utf-8")
        self.make_stack(
            "app",
            [
                ("current", "ghcr.io/acme/app", "cid-current"),
                ("stale", "quay.io/acme/app", "cid-stale"),
            ],
        )
        self.set_image_state(
            "ghcr.io/acme/app",
            "sha256:old-current",
            "sha256:old-current-index",
        )
        self.set_image_after_pull(
            "ghcr.io/acme/app",
            "sha256:new-current",
            "sha256:shared",
        )
        self.set_image_state(
            "quay.io/acme/app",
            "sha256:old-stale",
            "sha256:old-stale-index",
        )
        self.set_image_after_pull(
            "quay.io/acme/app",
            "sha256:new-stale",
            "sha256:moved",
        )
        digest_verifier = mock.Mock()
        digest_verifier.verify_tag_digest.side_effect = (
            lambda _image, expected: DigestResolveResult(
                True,
                "resolved",
                "digest-match",
                digest=expected,
            )
        )
        digest_verifier.verify.side_effect = (
            lambda image, _expected: DigestCheckResult(
                False,
                "mismatch",
                "stale-digest",
                tag_digest="sha256:moved",
            )
            if image == "quay.io/acme/app:latest"
            else DigestCheckResult(True, "verified", "digest-match")
        )
        runner = self.make_runner(
            digest_verifier=digest_verifier,
            db_path=self.db_path,
        )

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            status = runner.run()

        self.assertEqual(status, 1)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), pending_line)
        self.assertEqual(
            {call.args[0] for call in digest_verifier.verify.call_args_list},
            {
                "ghcr.io/acme/app:latest",
                "quay.io/acme/app:latest",
            },
        )
        self.assertRegex(
            self.calls(),
            r"app\tcompose -f docker-compose.yml pull current stale",
        )
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(
            pending[0]["status_reason"],
            "expected-digest-sibling-failed",
        )
        events = self.db_rows("SELECT * FROM update_events ORDER BY service_name")
        self.assertEqual(
            {
                row["service_name"]: json.loads(row["metadata_json"])["reason"]
                for row in events
            },
            {
                "current": "expected-digest-sibling-failed",
                "stale": "stale-pending-digest",
            },
        )
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertNotIn("compose_image=ghcr.io/acme/app", report)
        self.assertIn("compose_image=quay.io/acme/app", report)
        self.assertIn("wud_entries_restored=yes", report)
    def test_same_stack_stale_digest_classifies_only_failed_line(self) -> None:
        current_line = "ghcr.io/acme/current:latest@sha256:current\n"
        self.wud_file.write_text(
            current_line + "ghcr.io/acme/stale:latest@sha256:stale\n",
            encoding="utf-8",
        )
        self.make_stack(
            "app",
            [
                ("current", "ghcr.io/acme/current:latest", "cid-current"),
                ("stale", "ghcr.io/acme/stale:latest", "cid-stale"),
            ],
        )
        self.set_image_state(
            "ghcr.io/acme/current:latest",
            "sha256:old-current",
            "sha256:old-current-index",
        )
        self.set_image_state(
            "ghcr.io/acme/stale:latest",
            "sha256:old-stale",
            "sha256:old-stale-index",
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/current:latest",
            manifest_index_digest("sha256:current", "sha256:current-child"),
        )
        self.set_manifest_stdout(
            "ghcr.io/acme/stale:latest",
            manifest_index_digest("sha256:moved", "sha256:moved-child"),
        )

        status, stdout, stderr = self.run_direct()

        self.assertEqual(status, 1, stderr + stdout)
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), current_line)
        pending = self.db_rows("SELECT * FROM pending_updates ORDER BY line_no")
        self.assertEqual(
            [(row["status"], row["status_reason"]) for row in pending],
            [
                ("failed", "preflight-skipped"),
                ("failed", "stale-pending-digest"),
            ],
        )
        events = self.db_rows("SELECT * FROM update_events ORDER BY service_name")
        self.assertEqual(
            {
                row["service_name"]: json.loads(row["metadata_json"])["reason"]
                for row in events
            },
            {
                "current": "preflight-skipped",
                "stale": "stale-pending-digest",
            },
        )
        report = self.latest_error_report().read_text(encoding="utf-8")
        self.assertNotIn("ghcr.io/acme/current:latest", report)
        self.assertIn("ghcr.io/acme/stale:latest", report)
    def test_multi_stack_failure_keeps_failure_reason_in_pending_audit(self) -> None:
        self.wud_file.write_text("repo/app:latest\n", encoding="utf-8")
        self.make_stack("ok", [("app", "repo/app:latest", "cid-ok")])
        self.make_stack("zbad", [("app", "repo/app:latest", "cid-bad")])
        self.set_image_state("repo/app:latest", "old", "sha256:old")
        self.set_image_after_pull("repo/app:latest", "new", "sha256:new")
        stack_state = self.fake_root / "stacks" / "zbad"
        (stack_state / "pull_fail").write_text("", encoding="utf-8")
        (stack_state / "pull_stderr").write_text("manifest fetch failed\n", encoding="utf-8")

        result = self.run_python("--yes")

        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        pending = self.db_rows("SELECT * FROM pending_updates")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "failed")
        self.assertEqual(pending[0]["status_reason"], "pull-failed")
        self.assertEqual(pending[0]["service_key"], "zbad/app")
        self.assertEqual(pending[0]["stack_name"], "zbad")
        self.assertEqual(pending[0]["service_name"], "app")
    def test_unmatched_entry_remains_pending_with_reason(self) -> None:
        self.wud_file.write_text("repo/missing:latest\n", encoding="utf-8")
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])

        result = self.run_python("--yes")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.wud_file.read_text(encoding="utf-8"),
            "repo/missing:latest\n",
        )
        self.assertNotRegex(self.calls(), r"compose -f .* pull")
        pending = self.db_rows("SELECT * FROM pending_updates")
        runs = self.db_rows("SELECT * FROM update_runs")
        self.assertEqual(runs[0]["status"], "success")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["status_reason"], "unmatched")



class UpdatedImagesTests(unittest.TestCase):
    def test_matches_rewritten_image_reference_by_repository_identity(self) -> None:
        before = {
            "repo/app:1.0": ImageState(
                image_id="sha256:old",
                digest="repo/app:1.0@sha256:old",
            ),
        }
        after = {
            "repo/app:2.0": ImageState(
                image_id="sha256:new",
                digest="repo/app:2.0@sha256:new",
            ),
        }

        self.assertEqual(
            _updated_images(before, after),
            [("repo/app:1.0", after["repo/app:2.0"])],
        )
