"""Bounded, direct HTTPS transport for registry manifests and token challenges.

The worker process makes DNS, TLS, headers and body share one hard deadline.
It only connects to a validated numeric address; HTTP redirects and environment
proxies are deliberately not used.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import math
import os
import socket
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOKEN_BYTES = 64 * 1024
AUTH_ORIGINS_ENV = "WUD_REGISTRY_AUTH_ORIGINS"
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "::1/128",
        "fc00::/7",
    )
)


class RegistryRequestError(RuntimeError):
    """A registry request could not be safely completed."""


def https_origin(url: str) -> tuple[str, int]:
    try:
        if any(ord(char) <= 32 or ord(char) >= 127 for char in url) or "\\" in url:
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "%" in host
        ):
            raise ValueError
        port = parsed.port
        if port == 0:
            raise ValueError
        return host, port or 443
    except ValueError as exc:
        raise RegistryRequestError(
            "Registry request refused: use an HTTPS URL without credentials or fragments."
        ) from exc


def token_url(challenge: str, registry_url: str) -> tuple[str, bool]:
    """Authorize a realm against its registry; configuration is operator-owned."""
    registry = https_origin(registry_url)
    scheme, _, rest = challenge.partition(" ")
    if scheme.lower() != "bearer" or not rest:
        raise RegistryRequestError(
            "Registry authentication failed: expected a Bearer challenge."
        )
    try:
        values = urllib.request.parse_keqv_list(urllib.request.parse_http_list(rest))
        realm = values["realm"]
    except (KeyError, ValueError) as exc:
        raise RegistryRequestError(
            "Registry authentication failed: no valid token URL."
        ) from exc
    origin = https_origin(realm)
    allowed = {registry}
    if registry == ("registry-1.docker.io", 443):
        allowed.add(("auth.docker.io", 443))
    configured: set[tuple[str, int]] = set()
    try:
        entries = json.loads(os.environ.get(AUTH_ORIGINS_ENV, "{}"))
        if not isinstance(entries, dict):
            raise TypeError
        for source, targets in entries.items():
            if not isinstance(targets, list) or not all(
                isinstance(t, str) for t in targets
            ):
                raise ValueError
            source_origin = https_origin(source)
            target_origins = {https_origin(target) for target in targets}
            if source_origin == registry:
                configured.update(target_origins)
    except (TypeError, ValueError, RegistryRequestError) as exc:
        raise RegistryRequestError(
            f"Registry authentication failed: correct the JSON origin mapping in {AUTH_ORIGINS_ENV}."
        ) from exc
    if origin not in allowed | configured:
        raise RegistryRequestError(
            "Registry authentication refused an unrelated token server; "
            f"configure its HTTPS origin in {AUTH_ORIGINS_ENV} if it is trusted."
        )
    query = {key: values[key] for key in ("service", "scope") if values.get(key)}
    separator = "&" if urllib.parse.urlsplit(realm).query else "?"
    return realm + (
        separator + urllib.parse.urlencode(query) if query else ""
    ), origin in configured


def request_bytes(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    limit: int = MAX_MANIFEST_BYTES,
    peer: str = "",
    allow_private: bool = False,
) -> tuple[int, dict[str, str], bytes, str]:
    https_origin(url)
    if not math.isfinite(timeout) or timeout <= 0:
        raise RegistryRequestError(
            "Registry request failed: timeout must be positive and finite."
        )
    # Only application-owned paths belong in argv; request data stays in JSON stdin.
    try:
        worker_command = (sys.executable, "-I", str(Path(__file__).resolve()))
        result = subprocess.run(
            worker_command,
            input=json.dumps(
                {
                    "url": url,
                    "headers": dict(headers),
                    "timeout": timeout,
                    "limit": limit,
                    "peer": peer,
                    "allow_private": allow_private,
                }
            ),
            capture_output=True,
            shell=False,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RegistryRequestError(
                "Registry request failed: check registry DNS, TLS and network access."
            )
        reply = json.loads(result.stdout)
        if "error" in reply:
            raise RegistryRequestError(reply["error"])
        return (
            reply["status"],
            reply["headers"],
            base64.b64decode(reply["body"]),
            reply["peer"],
        )
    except subprocess.TimeoutExpired as exc:
        raise RegistryRequestError(
            "Registry request timed out; try again when the registry is responsive."
        ) from exc
    except (OSError, ValueError, KeyError) as exc:
        raise RegistryRequestError(
            "Registry request failed: could not complete the HTTPS request."
        ) from exc


def _validate_address(value: str, allow_private: bool) -> str:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    private = allow_private and any(address in network for network in _PRIVATE_NETWORKS)
    if not (address.is_global and not address.is_multicast) and not private:
        raise RegistryRequestError(
            "Registry request refused an unauthorized network address."
        )
    return value


def _worker_request(options: dict[str, Any]) -> dict[str, Any]:
    host, port = https_origin(options["url"])
    peer = options["peer"]
    if peer:
        addresses = [peer]
    else:
        addresses = [
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        ]
    if not addresses:
        raise RegistryRequestError(
            "Registry request failed: the server has no network address."
        )
    # Reject mixed safe/unsafe DNS answers rather than choosing a convenient one.
    for address in addresses:
        _validate_address(address, options["allow_private"])
    context = ssl.create_default_context()
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    connection = http.client.HTTPSConnection(
        host, port, timeout=options["timeout"], context=context
    )
    try:
        last_error: OSError | None = None
        for address in dict.fromkeys(addresses):
            try:
                raw = socket.create_connection(
                    (address, port), timeout=options["timeout"]
                )
                try:
                    connection.sock = context.wrap_socket(raw, server_hostname=host)
                except BaseException:
                    raw.close()
                    raise
                peer = address
                break
            except OSError as exc:
                last_error = exc
        if connection.sock is None:
            assert last_error is not None
            raise last_error
        parsed = urllib.parse.urlsplit(options["url"])
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection.request("GET", path, headers=options["headers"])
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RegistryRequestError(
                "Registry request refused a redirect; use a direct HTTPS endpoint."
            )
        # Error bodies are unnecessary, including an unauthenticated 401 body.
        body = response.read(options["limit"] + 1) if response.status == 200 else b""
        if len(body) > options["limit"]:
            raise RegistryRequestError("Registry response exceeded the permitted size.")
        return {
            "status": response.status,
            "headers": dict(response.getheaders()),
            "body": base64.b64encode(body).decode("ascii"),
            "peer": peer,
        }
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        print(json.dumps(_worker_request(json.load(sys.stdin))))
    except RegistryRequestError as exc:
        print(json.dumps({"error": str(exc)}))
    except (OSError, ValueError, http.client.HTTPException):
        print(
            json.dumps(
                {
                    "error": "Registry request failed: check registry DNS, TLS and network access."
                }
            )
        )
