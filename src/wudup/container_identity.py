"""Helpers for finding the current Docker container identity."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


def docker_endpoint_host(environ: Mapping[str, str]) -> str:
    """Return the lowercased host of a network ``DOCKER_HOST``, or "" for local sockets."""

    value = environ.get("DOCKER_HOST", "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _NETWORK_DOCKER_HOST_SCHEMES:
            return ""
        return (parsed.hostname or "").strip().lower()
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
