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
from wudup.container_identity import (
    DockerEndpoint,
    container_dns_names,
    container_serves_docker_endpoint,
    docker_endpoint,
    docker_endpoint_host,
    resolve_docker_endpoint,
    resolve_host_addresses,
)

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

    def test_docker_endpoint_port_defaults_to_docker_cli_tcp_port(self) -> None:
        cases = {
            "tcp://socket-proxy": 2375,
            "tcp://socket-proxy:2376": 2376,
            "https://proxy.internal:8443": 8443,
            "http://proxy.internal": None,
            "tcp://socket-proxy:not-a-port": None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value), mock.patch(
                "wudup.container_identity.resolve_host_addresses", return_value=frozenset(),
            ):
                endpoint = docker_endpoint({"DOCKER_HOST": value})
                self.assertIsNotNone(endpoint)
                self.assertEqual(endpoint.port, expected)
        self.assertIsNone(docker_endpoint({"DOCKER_HOST": "unix:///var/run/docker.sock"}))

    def test_docker_endpoint_resolves_only_on_request(self) -> None:
        with mock.patch(
            "wudup.container_identity.resolve_host_addresses",
            return_value=frozenset({"172.18.0.5"}),
        ) as resolver:
            endpoint = docker_endpoint({"DOCKER_HOST": "tcp://socket-proxy:2375"})
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.addresses, frozenset())
            resolver.assert_not_called()

            resolved = resolve_docker_endpoint(endpoint)

        resolver.assert_called_once_with("socket-proxy")
        self.assertEqual(resolved, DockerEndpoint("socket-proxy", 2375, frozenset({"172.18.0.5"})))

    def test_ipv6_only_container_address_serves_endpoint(self) -> None:
        details = {"Networks": {"v6": {"IPAddress": "", "GlobalIPv6Address": "FD00:0::5"}}}
        self.assertTrue(container_serves_docker_endpoint(
            details, DockerEndpoint("socket-proxy", 2375, frozenset({"fd00::5"})),
        ))
        self.assertFalse(container_serves_docker_endpoint(
            details, DockerEndpoint("socket-proxy", 2375, frozenset({"fd00::6"})),
        ))

    def test_resolve_host_addresses_normalizes_and_tolerates_failure(self) -> None:
        infos = [
            (2, 1, 6, "", ("172.18.0.5", 0)),
            (30, 1, 6, "", ("FD00:0::5%eth0", 0, 0, 0)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=infos):
            self.assertEqual(resolve_host_addresses("proxy"), {"172.18.0.5", "fd00::5"})
        with mock.patch("socket.getaddrinfo", side_effect=OSError("no such host")):
            self.assertEqual(resolve_host_addresses("proxy"), frozenset())

    def test_published_host_port_serves_endpoint(self) -> None:
        endpoint = DockerEndpoint("192.168.1.10", 2375, frozenset({"192.168.1.10"}))
        cases = {
            "wildcard": ({"Ports": {"2375/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2375"}]}}, True),
            "ipv6 wildcard": ({"Ports": {"2375/tcp": [{"HostIp": "::", "HostPort": "2375"}]}}, True),
            "same address": (
                {"Ports": {"2375/tcp": [{"HostIp": "192.168.1.10", "HostPort": "2375"}]}}, True,
            ),
            "loopback": (
                {"Ports": {"2375/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2375"}]}}, True,
            ),
            "configured only": (
                {"Ports": None, "PortBindings": {"2375/tcp": [{"HostIp": "", "HostPort": "2375"}]}},
                True,
            ),
            "other port": ({"Ports": {"2375/tcp": [{"HostIp": "", "HostPort": "2376"}]}}, False),
            "other address": (
                {"Ports": {"2375/tcp": [{"HostIp": "192.168.1.20", "HostPort": "2375"}]}}, False,
            ),
            "udp": ({"Ports": {"2375/udp": [{"HostIp": "", "HostPort": "2375"}]}}, False),
            "unpublished": ({"Ports": {"2375/tcp": None}}, False),
        }
        for label, (details, expected) in cases.items():
            with self.subTest(label):
                self.assertIs(container_serves_docker_endpoint(details, endpoint), expected)

    def test_resolved_address_replaces_name_matching(self) -> None:
        details = {
            "Name": "/socket-proxy",
            "Networks": {"other": {"Aliases": ["socket-proxy"], "IPAddress": "172.20.0.9"}},
        }
        self.assertFalse(container_serves_docker_endpoint(
            details, DockerEndpoint("socket-proxy", 2375, frozenset({"172.18.0.5"})),
        ))
        self.assertTrue(container_serves_docker_endpoint(
            details, DockerEndpoint("socket-proxy", 2375, frozenset({"172.20.0.9"})),
        ))
        self.assertTrue(container_serves_docker_endpoint(
            details, DockerEndpoint("socket-proxy", 2375),
        ))


class UpdateFromWudDockerEndpointTests(UpdateFromWudRunnerTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Keep DNS lookups out of the tests; hosts resolve only when listed here.
        self.resolved: dict[str, frozenset[str]] = {}
        resolver = mock.patch(
            "wudup.container_identity.resolve_host_addresses",
            side_effect=lambda host: self.resolved.get(host, frozenset()),
        )
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)

    def _write_details(self, container_id: str, **details: object) -> None:
        (self.fake_root / f"containers/{container_id}.dns.json").write_text(
            json.dumps({"Id": container_id, **details}),
        )

    def _endpoint_stack(
        self,
        *,
        docker_host: str = f"tcp://{PROXY_HOST}:2375",
        proxy_details: dict[str, object] | None = None,
    ):
        stack = self.make_stack("wud", [
            ("socket-proxy", PROXY_IMAGE, "cid-proxy"),
            ("wud", WUD_IMAGE, "cid-wud"),
        ])
        self._write_details("cid-proxy", **(proxy_details or {
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

    def _assert_proxy_blocked(self, runner) -> None:
        result, stderr = self._run(runner)

        self.assertEqual(result, 1, stderr)
        self.assertEqual(
            [(f.reason, f.services) for f in runner.failures],
            [("docker-endpoint-recreate-blocked", ("socket-proxy",))],
        )
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_proxy_reached_through_published_host_port_is_blocked(self) -> None:
        published = {
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {"IPAddress": "172.18.0.5"}},
            "Ports": {"2375/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2375"}]},
            "PortBindings": {"2375/tcp": [{"HostIp": "", "HostPort": "2375"}]},
            "NetworkMode": "wud_default",
        }
        for docker_host, resolved in (
            ("tcp://192.168.1.10:2375", {"192.168.1.10"}),
            ("tcp://127.0.0.1:2375", {"127.0.0.1"}),
            ("tcp://host.docker.internal:2375", {"192.168.65.254"}),
            ("tcp://host.docker.internal", set()),
        ):
            with self.subTest(docker_host=docker_host):
                (self.fake_root / "calls.log").write_text("")
                self.resolved = {docker_host.split("//")[1].split(":")[0]: frozenset(resolved)}
                self._endpoint_stack(docker_host=docker_host, proxy_details=published)
                self.wud_file.write_text(f"{PROXY_IMAGE}\n")
                self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))

    def test_service_publishing_another_port_is_not_blocked(self) -> None:
        self._endpoint_stack(docker_host="tcp://192.168.1.10:2375", proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {"IPAddress": "172.18.0.5"}},
            "Ports": {"2375/tcp": [{"HostIp": "0.0.0.0", "HostPort": "12375"}]},
        })
        self.resolved = {"192.168.1.10": frozenset({"192.168.1.10"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, stderr = self._run(runner)

        self.assertEqual(result, 0, stderr)
        self.assertIn(" stop socket-proxy", self.calls())

    def test_proxy_sharing_provider_network_is_blocked(self) -> None:
        self._endpoint_stack(docker_host="tcp://vpn:2375", proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {},
            "NetworkMode": "container:cid-vpn",
        })
        self._write_details(
            "cid-vpn", Name="/vpn-gateway-1", Service="vpn",
            Networks={"wud": {"Aliases": ["vpn"], "IPAddress": "172.18.0.7"}},
            NetworkMode="wud_default",
        )
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        self._assert_proxy_blocked(runner)
        self.assertIn("inspect cid-vpn", self.calls())

    def test_proxy_sharing_provider_published_port_is_blocked(self) -> None:
        self._endpoint_stack(docker_host="tcp://192.168.1.10:2375", proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {},
            "NetworkMode": "container:vpn-gateway-1",
        })
        self._write_details(
            "vpn-gateway-1", Name="/vpn-gateway-1",
            Networks={"vpn": {"IPAddress": "172.30.0.2"}},
            Ports={"2375/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2375"}]},
        )
        self.resolved = {"192.168.1.10": frozenset({"192.168.1.10"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")

        self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))

    def test_network_provider_lookup_failure_fails_closed(self) -> None:
        self._endpoint_stack(proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {},
            "NetworkMode": "container:cid-vpn",
        })
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)
        inspect = runner.docker.container_dns_details
        failure = CommandError(CommandResult(("docker", "inspect"), None, 1))

        def fail_for_provider(container: str):
            if container == "cid-vpn":
                raise failure
            return inspect(container)

        with mock.patch.object(
            runner.docker, "container_dns_details", side_effect=fail_for_provider,
        ):
            result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertIn("Could not check whether this update would stop the Docker endpoint", stderr)
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_network_mode_cycle_fails_closed(self) -> None:
        self._endpoint_stack(proxy_details={
            "Name": "/wud-socket-proxy-1", "Service": "socket-proxy",
            "Networks": {}, "NetworkMode": "container:cid-a",
        })
        self._write_details("cid-a", Name="/a", Networks={}, NetworkMode="container:cid-b")
        self._write_details("cid-b", Name="/b", Networks={}, NetworkMode="container:cid-a")
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, _stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_network_mode_chain_within_hop_limit_is_followed(self) -> None:
        self._endpoint_stack(docker_host="tcp://vpn:2375", proxy_details={
            "Name": "/wud-socket-proxy-1", "Service": "socket-proxy",
            "Networks": {}, "NetworkMode": "container:hop-1",
        })
        for hop in range(1, 5):
            self._write_details(
                f"hop-{hop}", Name=f"/hop-{hop}", Networks={},
                NetworkMode=f"container:hop-{hop + 1}",
            )
        self._write_details(
            "hop-5", Name="/vpn-gateway-1",
            Networks={"wud": {"Aliases": ["vpn"], "IPAddress": "172.18.0.7"}},
        )
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")

        self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))

    def test_network_mode_chain_past_hop_limit_fails_closed(self) -> None:
        self._endpoint_stack(docker_host="tcp://vpn:2375", proxy_details={
            "Name": "/wud-socket-proxy-1", "Service": "socket-proxy",
            "Networks": {}, "NetworkMode": "container:hop-1",
        })
        for hop in range(1, 6):
            self._write_details(
                f"hop-{hop}", Name=f"/hop-{hop}", Networks={},
                NetworkMode=f"container:hop-{hop + 1}",
            )
        self._write_details(
            "hop-6", Name="/vpn-gateway-1",
            Networks={"wud": {"Aliases": ["vpn"], "IPAddress": "172.18.0.7"}},
        )
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, stderr = self._run(runner)

        self.assertEqual(result, 1)
        self.assertEqual(runner.failures[0].reason, "docker-endpoint-recreate-blocked")
        self.assertIn("Could not check whether this update would stop the Docker endpoint", stderr)
        self.assertNotIn("inspect hop-6", self.calls())
        self.assertNotRegex(self.calls(), MUTATION_CALLS)

    def test_configured_port_binding_alone_blocks_proxy(self) -> None:
        self._endpoint_stack(docker_host="tcp://192.168.1.10:2375", proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {"IPAddress": "172.18.0.5"}},
            "Ports": None,
            "PortBindings": {"2375/tcp": [{"HostIp": "", "HostPort": "2375"}]},
        })
        self.resolved = {"192.168.1.10": frozenset({"192.168.1.10"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")

        self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))

    def test_ipv6_only_proxy_address_is_blocked(self) -> None:
        self._endpoint_stack(proxy_details={
            "Name": "/wud-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"wud": {
                "Aliases": [], "IPAddress": "", "GlobalIPv6Address": "fd00::5",
            }},
        })
        self.resolved = {PROXY_HOST: frozenset({"fd00::5"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")

        self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))

    def test_endpoint_host_is_resolved_once_and_only_when_checked(self) -> None:
        self.make_stack("idle", [("app", "repo/app:latest", None)])
        self.set_image_state("repo/app:latest", "old")
        self.set_image_after_pull("repo/app:latest", "new")
        self._endpoint_stack()
        self.wud_file.write_text("repo/app:latest\n")

        result, stderr = self._run(self.make_runner(db_path=self.db_path))

        self.assertEqual(result, 0, stderr)
        self.resolver.assert_not_called()

        self.make_stack("other", [("api", "repo/api:latest", "cid-api")])
        self.set_image_state("repo/api:latest", "old")
        self.set_image_after_pull("repo/api:latest", "new")
        self.wud_file.write_text(f"{WUD_IMAGE}\nrepo/api:latest\n")

        result, stderr = self._run(self.make_runner(db_path=self.db_path))

        self.assertEqual(result, 0, stderr)
        self.resolver.assert_called_once_with(PROXY_HOST)

    def test_same_named_service_on_another_network_is_not_blocked(self) -> None:
        # WUDup resolves the proxy name to a container on its own network, so a
        # service with the same alias on an unrelated network is not the endpoint.
        self._endpoint_stack(proxy_details={
            "Name": "/other-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"other": {"Aliases": [PROXY_HOST], "IPAddress": "172.20.0.9"}},
        })
        self.resolved = {PROXY_HOST: frozenset({"172.18.0.5"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")
        runner = self.make_runner(db_path=self.db_path)

        result, stderr = self._run(runner)

        self.assertEqual(result, 0, stderr)
        self.assertEqual(runner.failures, [])
        self.assertIn(" stop socket-proxy", self.calls())

    def test_resolved_endpoint_address_blocks_matching_container(self) -> None:
        self._endpoint_stack()
        self.resolved = {PROXY_HOST: frozenset({"172.18.0.5"})}
        self.wud_file.write_text(f"{PROXY_IMAGE}\n")

        self._assert_proxy_blocked(self.make_runner(db_path=self.db_path))


if __name__ == "__main__":
    unittest.main()
