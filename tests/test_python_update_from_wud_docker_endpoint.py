from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from unittest import mock

from tests.update_from_wud_helpers import (
    UpdateFromWudRunnerTestCase,
)

from wudup.command import CommandError, CommandResult
from wudup.container_identity import container_dns_names, docker_endpoint_host

PROXY_IMAGE = "repo/socket-proxy:latest"
WUD_IMAGE = "repo/wud:latest"
PROXY_HOST = "socket-proxy-wud-updater"
MUTATION_CALLS = r"compose -f .* (?:pull|stop|pause|up -d)"


class DockerEndpointHostTests(unittest.TestCase):
    def test_network_docker_host_returns_lowercased_host(self) -> None:
        cases = {
            "tcp://Socket-Proxy:2375": "socket-proxy",
            "http://socket-proxy:2375/": "socket-proxy",
            "https://proxy.internal:2376": "proxy.internal",
            "tcp://172.18.0.5:2375": "172.18.0.5",
            "tcp://[fd00::5]:2375": "fd00::5",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(docker_endpoint_host({"DOCKER_HOST": value}), expected)

    def test_local_or_missing_docker_host_has_no_endpoint_host(self) -> None:
        for value in (
            "",
            "unix:///var/run/docker.sock",
            "npipe:////./pipe/docker_engine",
            "ssh://user@docker-host",
            "tcp://[bad",
        ):
            with self.subTest(value=value):
                self.assertEqual(docker_endpoint_host({"DOCKER_HOST": value}), "")
        self.assertEqual(docker_endpoint_host({}), "")

    def test_container_dns_names_include_names_aliases_and_addresses(self) -> None:
        names = container_dns_names({
            "Id": "0123456789abcdef0123",
            "Name": "/Socket-Proxy-1",
            "Service": "socket-proxy",
            "Networks": {
                "wud": {
                    "Aliases": ["socket-proxy-wud-updater"],
                    "DNSNames": ["dns-only"],
                    "IPAddress": "172.18.0.5",
                    "GlobalIPv6Address": "",
                },
                "broken": None,
            },
        })
        self.assertEqual(names, {
            "0123456789abcdef0123", "0123456789ab", "socket-proxy-1",
            "socket-proxy-wud-updater", "dns-only", "172.18.0.5",
        })

    def test_container_dns_names_ignore_service_label_without_network_alias(self) -> None:
        # A network_mode container has no DNS entry of its own.
        names = container_dns_names({
            "Id": "cid", "Name": "/app-vpn-client-1", "Service": "vpn-client", "Networks": {},
        })
        self.assertNotIn("vpn-client", names)


class UpdateFromWudDockerEndpointTests(UpdateFromWudRunnerTestCase):
    def _endpoint_stack(self, *, docker_host: str = f"tcp://{PROXY_HOST}:2375"):
        stack = self.make_stack("wud", [
            ("socket-proxy", PROXY_IMAGE, "cid-proxy"),
            ("wud", WUD_IMAGE, "cid-wud"),
        ])
        (self.fake_root / "containers/cid-proxy.dns.json").write_text(json.dumps({
            "Id": "cid-proxy",
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {"Aliases": [PROXY_HOST], "IPAddress": "172.18.0.5"}},
        }))
        for image in (PROXY_IMAGE, WUD_IMAGE):
            self.set_image_state(image, "old")
            self.set_image_after_pull(image, "new")
        self.env["DOCKER_HOST"] = docker_host
        return stack

    def _run(self, runner):
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            result = runner.run()
        return result, stderr.getvalue()

    def test_scope_with_docker_endpoint_service_is_blocked_before_mutation(self) -> None:
        stack = self._endpoint_stack()
        original_compose = (stack / "docker-compose.yml").read_text()
        self.wud_file.write_text(f"{PROXY_IMAGE}\n{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(
            [(f.reason, f.services) for f in runner.failures],
            [("docker-endpoint-recreate-blocked", ("socket-proxy",))],
        )
        self.assertIn("Docker endpoint WUDup uses (DOCKER_HOST", stderr)
        self.assertIn("Update that service from the host", stderr)
        self.assertEqual((stack / "docker-compose.yml").read_text(), original_compose)
        self.assertEqual(self.wud_file.read_text(), f"{PROXY_IMAGE}\n{WUD_IMAGE}\n")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_scope_with_only_wud_service_is_updated(self) -> None:
        self._endpoint_stack()
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, _stderr = self._run(runner)

        self.assertEqual(result, 0)
        self.assertEqual(runner.failures, [])
        self.assertIn(" stop wud", self.calls())
        self.assertNotRegex(self.calls(), r"(?:stop|pause|up -d) [^\n]*\bsocket-proxy\b")

    def test_unix_socket_docker_host_does_not_check_endpoint(self) -> None:
        self._endpoint_stack(docker_host="unix:///var/run/docker.sock")
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        with mock.patch.object(
            runner.docker, "container_dns_details", side_effect=AssertionError("unexpected"),
        ):
            result, _stderr = self._run(runner)

        self.assertEqual(result, 0)
        self.assertIn(" stop socket-proxy", self.calls())

    def test_endpoint_lookup_failure_fails_closed(self) -> None:
        stack = self._endpoint_stack()
        original_compose = (stack / "docker-compose.yml").read_text()
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        failure = CommandError(CommandResult(
            ("docker", "inspect"), None, 1, stderr="private daemon detail",
        ))

        with mock.patch.object(runner.docker, "container_dns_details", side_effect=failure):
            result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertIn("Could not check whether this update would stop the Docker endpoint", stderr)
        self.assertNotIn("private daemon detail", stderr)
        self.assertEqual((stack / "docker-compose.yml").read_text(), original_compose)
        self.assertEqual(self.wud_file.read_text(), f"{WUD_IMAGE}\n")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_web_apply_blocks_docker_endpoint_with_self_protection_enabled(self) -> None:
        self._endpoint_stack()
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        runner.options = replace(runner.options, protected_container="")

        result, _stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(
            [f.reason for f in runner.failures], ["docker-endpoint-recreate-blocked"],
        )
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_dry_run_reports_docker_endpoint_without_writing_state(self) -> None:
        self._endpoint_stack()
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        runner.options = replace(runner.options, dry_run=True)

        result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertIn("Docker endpoint WUDup uses (DOCKER_HOST", stderr)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.wud_file.read_text(), f"{PROXY_IMAGE}\n")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_scope_container_lookup_failure_fails_closed(self) -> None:
        self._endpoint_stack()
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        update_scope = runner.lifecycle._update_scope
        failing_ps = mock.patch.object(
            runner.compose, "ps_quiet_checked",
            side_effect=CommandError(CommandResult(("docker", "compose"), None, 1)),
        )
        self.addCleanup(failing_ps.stop)

        def scope_then_fail_container_lookup(*args, **kwargs):
            scope = update_scope(*args, **kwargs)
            if kwargs.get("strict"):
                failing_ps.start()
            return scope

        with mock.patch.object(
            runner.lifecycle, "_update_scope", side_effect=scope_then_fail_container_lookup,
        ):
            result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertIn("Could not check whether this update would stop the Docker endpoint", stderr)
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_cli_scope_discovery_failure_uses_endpoint_reason(self) -> None:
        self._endpoint_stack()
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        update_scope = runner.lifecycle._update_scope
        failure = CommandError(CommandResult(("docker", "compose"), None, 1))

        def fail_strict_scope(*args, **kwargs):
            if kwargs.get("strict"):
                raise failure
            return update_scope(*args, **kwargs)

        with mock.patch.object(runner.lifecycle, "_update_scope", side_effect=fail_strict_scope):
            result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertIn("Could not verify the update's recreate scope", stderr)
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_non_object_endpoint_details_fail_closed(self) -> None:
        self._endpoint_stack()
        (self.fake_root / "containers/cid-wud.dns.json").write_text("[]\n")
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, _stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_unreadable_endpoint_details_fail_closed(self) -> None:
        self._endpoint_stack()
        (self.fake_root / "containers/cid-wud.dns.json").write_text("not json\n")
        self.wud_file.write_text(f"{WUD_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, _stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)


if __name__ == "__main__":
    unittest.main()
