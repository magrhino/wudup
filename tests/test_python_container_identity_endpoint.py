"""Unit tests for DOCKER_HOST parsing and Docker endpoint container matching.

The updater and retag guards that use these helpers are covered end to end in
``test_python_update_from_wud_docker_endpoint.py`` and
``test_python_web_retag_apply_scope.py``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from wudup.container_identity import (
    DockerEndpoint,
    container_dns_names,
    container_serves_docker_endpoint,
    docker_endpoint,
    docker_endpoint_host,
    resolve_docker_endpoint,
    resolve_host_addresses,
)


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


if __name__ == "__main__":
    unittest.main()
