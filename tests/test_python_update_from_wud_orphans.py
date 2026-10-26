from __future__ import annotations

import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from tests.update_from_wud_helpers import (
    UpdateFromWudRunnerTestCase,
)

from wudup import updater_preflight
from wudup.command import CommandError, CommandResult
from wudup.compose import ComposeStack

APP_IMAGE = "repo/app:latest"
PROXY_HOST = "socket-proxy-wud-updater"
KEEP_ORPHANS_UP = "compose -f docker-compose.yml up -d --remove-orphans=false --pull never --no-build --no-deps app"
REMOVE_ORPHANS_UP = (
    "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app"
)
ORPHAN_LOOKUP = r'ps --all --format \{\{\.ID\}\}'


class UpdateFromWudOrphanTests(UpdateFromWudRunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        resolver = mock.patch(
            "wudup.container_identity.resolve_host_addresses",
            return_value=frozenset(),
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        self.make_stack("wud", [("app", APP_IMAGE, "cid-app")])
        self.set_image_state(APP_IMAGE, "old")
        self.set_image_after_pull(APP_IMAGE, "new")
        self.wud_file.write_text(f"{APP_IMAGE}\n")

    def _project_containers(self, *rows: str) -> None:
        (self.fake_root / "compose-containers.tsv").write_text(
            "".join(f"{row}\n" for row in ("cid-app\twud\tapp", *rows)),
        )

    def _orphan_proxy(self) -> None:
        """Leave socket-proxy running in project ``wud`` after its service was removed."""
        self.env["DOCKER_HOST"] = f"tcp://{PROXY_HOST}:2375"
        self._project_containers("cid-proxy\twud\tsocket-proxy")
        (self.fake_root / "containers/cid-proxy.dns.json").write_text(json.dumps({
            "Id": "cid-proxy",
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {"Aliases": [PROXY_HOST], "IPAddress": "172.18.0.5"}},
        }))

    def _run(self, runner):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = runner.run()
        return result, output.getvalue()

    def test_orphan_docker_endpoint_is_kept(self) -> None:
        self._orphan_proxy()
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(KEEP_ORPHANS_UP, self.calls())
        self.assertNotRegex(self.calls(), r"--remove-orphans(?!=false)")
        self.assertIn("socket-proxy still runs in this Compose project", output)
        self.assertIn("Recreating without --remove-orphans", output)

    def test_orphan_wudup_container_is_kept(self) -> None:
        self._project_containers("0123456789ab\twud\tupdater")
        runner = self.make_runner(db_path=self.db_path)
        runner.options = replace(runner.options, protected_container="updater")

        with mock.patch.object(
            runner.docker, "container_id", return_value="0123456789abcdef0123",
        ):
            result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(KEEP_ORPHANS_UP, self.calls())
        self.assertNotRegex(self.calls(), r"--remove-orphans(?!=false)")
        self.assertIn("updater still runs in this Compose project", output)

    def test_unrelated_orphan_is_still_removed(self) -> None:
        self._orphan_proxy()
        self._project_containers("cid-old\twud\told-worker", "cid-other\tother\tsocket-proxy")
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(REMOVE_ORPHANS_UP, self.calls())
        self.assertIn("inspect cid-old", self.calls())
        self.assertNotIn("inspect cid-other", self.calls())
        self.assertNotIn("still runs in this Compose project", output)

    def test_orphan_lookup_failure_keeps_orphans(self) -> None:
        self._orphan_proxy()
        runner = self.make_runner(db_path=self.db_path)
        ps_format = runner.docker.ps_format

        def fail_orphan_lookup(fmt, **kwargs):
            if fmt.startswith("{{.ID}}"):
                raise CommandError(CommandResult(
                    ("docker", "ps"), None, 1, stderr="private daemon detail",
                ))
            return ps_format(fmt, **kwargs)

        with mock.patch.object(runner.docker, "ps_format", side_effect=fail_orphan_lookup):
            result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(KEEP_ORPHANS_UP, self.calls())
        self.assertIn("Could not check whether leftover containers", output)
        self.assertNotIn("private daemon detail", output)

    def test_malformed_orphan_lookup_keeps_orphans(self) -> None:
        self._orphan_proxy()
        (self.fake_root / "compose-containers.tsv").write_text("cid-app\twud\n")
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(KEEP_ORPHANS_UP, self.calls())
        self.assertIn("Could not check whether leftover containers", output)

    def test_orphan_endpoint_inspect_failure_keeps_orphans(self) -> None:
        self._orphan_proxy()
        (self.fake_root / "containers/cid-proxy.dns.json").write_text("not json\n")
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(KEEP_ORPHANS_UP, self.calls())
        self.assertIn("Could not check whether leftover containers", output)

    def test_dry_run_reports_kept_orphans_without_mutating(self) -> None:
        self._orphan_proxy()
        runner = self.make_runner(db_path=self.db_path)
        runner.options = replace(runner.options, dry_run=True)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn("socket-proxy still runs in this Compose project", output)
        self.assertNotRegex(self.calls(), r"compose -f .* (?:pull|stop|pause|up -d)")
        self.assertEqual(self.wud_file.read_text(), f"{APP_IMAGE}\n")

    def test_rollback_recreate_also_keeps_orphans(self) -> None:
        self._orphan_proxy()
        self.wud_file.write_text(f"{APP_IMAGE} tag=2.0\n")
        self.set_image_after_pull("repo/app:2.0", "new")
        (self.fake_root / "stacks/wud/up_fail").write_text("")
        runner = self.make_runner(db_path=self.db_path, allow_tag_updates=True)

        result, output = self._run(runner)

        self.assertEqual(result, 1, output)
        # The failed apply, then the rollback recreate on the restored Compose file.
        up_calls = [line for line in self.calls().splitlines() if " up -d " in line]
        self.assertEqual(len(up_calls), 2, self.calls())
        self.assertTrue(all(KEEP_ORPHANS_UP in line for line in up_calls), up_calls)
        self.assertIn(
            f"image: {APP_IMAGE}", (self.base / "wud/docker-compose.yml").read_text(),
        )

    def test_orphans_are_listed_once_and_kept_only_for_affected_stack(self) -> None:
        self._orphan_proxy()
        self.make_stack("media", [("web", "repo/web:latest", "cid-web")])
        self.set_image_state("repo/web:latest", "old")
        self.set_image_after_pull("repo/web:latest", "new")
        self._project_containers("cid-proxy\twud\tsocket-proxy", "cid-web\tmedia\tweb")
        self.wud_file.write_text(f"{APP_IMAGE}\nrepo/web:latest\n")
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        calls = self.calls()
        self.assertEqual(len(re.findall(ORPHAN_LOOKUP, calls)), 1, calls)
        self.assertIn(KEEP_ORPHANS_UP, calls)
        self.assertIn(
            "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build "
            "--no-deps web",
            calls,
        )

    def test_missing_project_name_keeps_orphans(self) -> None:
        stack = ComposeStack(
            index=3, directory=self.base / "wud", file="docker-compose.yml", name="wud",
            images=(), service_images=(), service_names=("app",),
        )
        runner = SimpleNamespace(stacks_keeping_orphans=set(), log=mock.Mock())
        containers = mock.Mock(side_effect=AssertionError("unexpected lookup"))

        updater_preflight._keep_protected_orphans(
            runner, stack, "cid-self", None, mock.Mock(), containers,
        )

        self.assertEqual(runner.stacks_keeping_orphans, {3})
        self.assertIn("Could not check whether leftover containers", runner.log.warning.call_args[0][0])

    def test_orphans_are_not_listed_without_protected_containers(self) -> None:
        self._project_containers("cid-proxy\twud\tsocket-proxy")
        runner = self.make_runner(db_path=self.db_path)

        result, output = self._run(runner)

        self.assertEqual(result, 0, output)
        self.assertIn(REMOVE_ORPHANS_UP, self.calls())
        self.assertNotRegex(self.calls(), ORPHAN_LOOKUP)


if __name__ == "__main__":
    unittest.main()
