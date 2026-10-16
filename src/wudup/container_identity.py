"""Helpers for finding the current Docker container identity."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_CONTAINER_ID_RE = re.compile(r"\b[0-9a-f]{64}\b")


def container_identity_candidates(
    environ: Mapping[str, str],
    *,
    cgroup_path: Path = Path("/proc/self/cgroup"),
) -> list[str]:
    """Return likely Docker container names or IDs for self-inspection."""

    candidates: list[str] = []
    for name in ("WUD_WEB_RESTART_CONTAINER", "HOSTNAME"):
        value = environ.get(name, "").strip()
        if value:
            candidates.append(value)

    try:
        cgroup_text = cgroup_path.read_text(encoding="utf-8")
    except OSError:
        cgroup_text = ""
    candidates.extend(_container_ids_from_cgroup(cgroup_text))
    return _unique(candidates)


def _container_ids_from_cgroup(cgroup_text: str) -> list[str]:
    return _unique(match.group(0) for match in _CONTAINER_ID_RE.finditer(cgroup_text))


def _unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


_NETWORK_DOCKER_HOST_SCHEMES = frozenset({"tcp", "http", "https"})
# Docker CLI default for a tcp:// DOCKER_HOST without a port.
_DEFAULT_TCP_DOCKER_PORT = 2375
_NETWORK_MODE_CONTAINER_PREFIX = "container:"


@dataclass(frozen=True)
class DockerEndpoint:
    """Where a network ``DOCKER_HOST`` reaches Docker, as seen from WUDup.

    ``addresses`` holds the IP addresses ``host`` resolved to; it is empty when
    the host could not be resolved.
    """

    host: str
    port: int | None
    addresses: frozenset[str] = frozenset()


def _split_docker_host(environ: Mapping[str, str]) -> tuple[str, int | None]:
    value = environ.get("DOCKER_HOST", "").strip()
    if not value:
        return "", None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in _NETWORK_DOCKER_HOST_SCHEMES:
            return "", None
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return "", None
    try:
        port = parsed.port
    except ValueError:
        port = None
    else:
        if port is None and scheme == "tcp":
            port = _DEFAULT_TCP_DOCKER_PORT
    return host, port


def docker_endpoint_host(environ: Mapping[str, str]) -> str:
    """Return the lowercased host of a network ``DOCKER_HOST``, or "" for local sockets."""

    return _split_docker_host(environ)[0]


def docker_endpoint(environ: Mapping[str, str]) -> DockerEndpoint | None:
    """Return the network ``DOCKER_HOST`` endpoint, or None for local sockets."""

    host, port = _split_docker_host(environ)
    if not host:
        return None
    return DockerEndpoint(host=host, port=port, addresses=resolve_host_addresses(host))


def resolve_host_addresses(host: str) -> frozenset[str]:
    """Return the IP addresses ``host`` resolves to here, or an empty set."""

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):  # UnicodeError is a ValueError
        return frozenset()
    return frozenset(
        address for address in (_normalize_ip(info[4][0]) for info in infos) if address
    )


def _normalize_ip(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().split("%", 1)[0]))
    except ValueError:
        return ""


def container_dns_names(details: Mapping[str, object]) -> set[str]:
    """Return names Docker DNS can resolve to the inspected container."""

    # The Compose service label is not used: Docker DNS only resolves it through
    # the network aliases below, on networks the container is attached to.
    names: list[object] = [details.get("Name")]
    container_id = details.get("Id")
    if isinstance(container_id, str):
        names.extend((container_id, container_id[:12]))
    networks = details.get("Networks")
    if isinstance(networks, Mapping):
        for endpoint in networks.values():
            if not isinstance(endpoint, Mapping):
                continue
            for key in ("Aliases", "DNSNames"):
                values = endpoint.get(key)
                if isinstance(values, list):
                    names.extend(values)
            names.extend((endpoint.get("IPAddress"), endpoint.get("GlobalIPv6Address")))
    return {
        name.strip().lstrip("/").lower()
        for name in names
        if isinstance(name, str) and name.strip().lstrip("/")
    }


def container_ip_addresses(details: Mapping[str, object]) -> set[str]:
    """Return the inspected container's addresses on its Docker networks."""

    addresses: set[str] = set()
    for endpoint in _network_endpoints(details):
        for key in ("IPAddress", "GlobalIPv6Address"):
            address = _normalize_ip(endpoint.get(key))
            if address:
                addresses.add(address)
    return addresses


def _network_endpoints(details: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
    networks = details.get("Networks")
    if isinstance(networks, Mapping):
        for endpoint in networks.values():
            if isinstance(endpoint, Mapping):
                yield endpoint


def network_mode_container(details: Mapping[str, object]) -> str:
    """Return the container whose network the inspected container shares, if any.

    Compose resolves ``network_mode: service:<name>`` to ``container:<id>``.
    """

    mode = details.get("NetworkMode")
    if isinstance(mode, str) and mode.startswith(_NETWORK_MODE_CONTAINER_PREFIX):
        return mode[len(_NETWORK_MODE_CONTAINER_PREFIX):].strip()
    return ""


def container_serves_docker_endpoint(
    details: Mapping[str, object],
    endpoint: DockerEndpoint,
) -> bool:
    """Return whether the inspected container may be what ``DOCKER_HOST`` reaches.

    ``details`` must describe the container that owns the network namespace; see
    ``network_mode_container``.
    """

    if endpoint.addresses:
        # Match the address WUDup actually reaches, so a same-named service on a
        # network WUDup is not attached to is not mistaken for the endpoint.
        if endpoint.addresses & container_ip_addresses(details):
            return True
    elif endpoint.host in container_dns_names(details):
        return True
    if endpoint.port is None:
        return False
    return any(
        host_port == endpoint.port and _host_binding_may_reach(host_ip, endpoint)
        for host_ip, host_port in _published_tcp_ports(details)
    )


def _published_tcp_ports(details: Mapping[str, object]) -> Iterator[tuple[str, int]]:
    """Yield ``(HostIp, HostPort)`` for TCP ports published on the Docker host."""

    for key in ("Ports", "PortBindings"):
        port_map = details.get(key)
        if isinstance(port_map, Mapping):
            for container_port, bindings in port_map.items():
                if _is_tcp_port_key(container_port) and isinstance(bindings, list):
                    yield from _host_port_bindings(bindings)


def _is_tcp_port_key(value: object) -> bool:
    return isinstance(value, str) and value.partition("/")[2].lower() in ("", "tcp")


def _host_port_bindings(bindings: list[object]) -> Iterator[tuple[str, int]]:
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        host_port = binding.get("HostPort")
        if isinstance(host_port, str) and host_port.strip().isdigit():
            host_ip = binding.get("HostIp")
            yield (host_ip if isinstance(host_ip, str) else ""), int(host_port)


def _host_binding_may_reach(host_ip: str, endpoint: DockerEndpoint) -> bool:
    """Return False only for a port bound to a different, specific host address."""

    address = _normalize_ip(host_ip)
    if not address:
        return True
    ip = ipaddress.ip_address(address)
    # Loopback bindings stay matched: WUDup on the host network, or a Docker
    # Desktop host.docker.internal forward, can reach them.
    return (
        ip.is_unspecified
        or ip.is_loopback
        or address in endpoint.addresses
        or address == _normalize_ip(endpoint.host)
    )
