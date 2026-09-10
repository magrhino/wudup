"""Offline regression checks for registry-controlled authentication URLs."""

import base64
import importlib.util
import io
import json
import socket
import ssl
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from wudup import digest_verifier, registry_http
from wudup.digest_verifier import (
    ManifestLookupError,
    RegistryHttpManifestResolver,
    parse_registry_image,
)
from wudup.registry_http import RegistryRequestError

REGISTRY = "https://registry-1.docker.io/v2/library/alpine/manifests/latest"
PUBLIC = "8.8.8.8"
PRIVATE = "10.0.0.20"


def challenge(url):
    return (
        f'Bearer realm="{url}",service="registry.example",scope="repository:app:pull"'
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv(registry_http.AUTH_ORIGINS_ENV, raising=False)
    monkeypatch.setattr(
        socket, "getaddrinfo", mock.Mock(side_effect=AssertionError("unexpected DNS"))
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        mock.Mock(side_effect=AssertionError("unexpected network")),
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///example-only",
        "data:application/json,{}",
        "http://auth.docker.io/token",
        "https://user:secret@auth.docker.io/token",
        "https://auth.docker.io/token#fragment",
        "https://auth.docker.io:bad/token",
        "https://auth.docker.io:0/token",
        "https://auth.docker.io\\@localhost/token",
        " https://auth.docker.io/token",
        "https://auth.docker.io\n/token",
        "https://[fe80::1%25en0]/token",
        "https://auth.docker.io.evil.example/token",
        "https://auth.docker.io:8443/token",
        "https://127.1/token",
        "https://2130706433/token",
        "https://169.254.169.254/token",
        "https://unrelated.example/token",
    ],
)
def test_realm_refused_before_transport(url):
    resolver = RegistryHttpManifestResolver()
    with mock.patch.object(
        digest_verifier,
        "request_bytes",
        return_value=(
            401,
            {"WWW-Authenticate": challenge(url)},
            b"",
            PUBLIC,
        ),
    ) as request:
        with pytest.raises(ManifestLookupError):
            resolver._request_json(REGISTRY)
    assert request.call_count == 1


def test_public_auth_and_cache_are_scoped_to_registry():
    resolver = RegistryHttpManifestResolver()
    auth = challenge("https://auth.docker.io/token")
    with mock.patch.object(
        digest_verifier,
        "request_bytes",
        side_effect=[
            (401, {"WWW-Authenticate": auth}, b"", PUBLIC),
            (200, {}, b'{"token":"example-token"}', "8.8.4.4"),
            (
                200,
                {"Docker-Content-Digest": "sha256:test"},
                b'{"schemaVersion":2}',
                PUBLIC,
            ),
            (401, {"WWW-Authenticate": auth}, b"", PUBLIC),
        ],
    ) as request:
        headers, payload, _body = resolver._request_json(REGISTRY)
        assert payload == {"schemaVersion": 2}
        assert headers["Docker-Content-Digest"] == "sha256:test"
        token_request = request.call_args_list[1]
        assert token_request.args[0].startswith("https://auth.docker.io/token?service=")
        assert token_request.kwargs["limit"] == registry_http.MAX_TOKEN_BYTES
        assert not token_request.kwargs["allow_private"]
        assert not request.call_args_list[0].kwargs["allow_private"]
        assert not request.call_args_list[2].kwargs["allow_private"]
        assert request.call_args_list[2].kwargs["peer"] == PUBLIC
        assert (
            request.call_args_list[2].kwargs["headers"]["Authorization"]
            == "Bearer example-token"
        )
        with pytest.raises(ManifestLookupError, match="unrelated"):
            resolver._request_json("https://unrelated.example/v2/app/manifests/latest")
        assert request.call_count == 4


def test_private_same_origin_is_pinned_to_original_peer():
    resolver = RegistryHttpManifestResolver()
    with mock.patch.object(
        digest_verifier,
        "request_bytes",
        side_effect=[
            (
                401,
                {"WWW-Authenticate": challenge("https://registry.example:5000/token")},
                b"",
                PRIVATE,
            ),
            (200, {}, b'{"token":"example"}', PRIVATE),
            (200, {}, b"{}", PRIVATE),
        ],
    ) as request:
        resolver._request_json("https://registry.example:5000/v2/app/manifests/latest")
    assert request.call_args_list[0].kwargs["allow_private"]
    for call in request.call_args_list[1:]:
        assert call.kwargs["peer"] == PRIVATE
        assert call.kwargs["allow_private"]


def test_explicit_private_token_server(monkeypatch):
    monkeypatch.setenv(
        registry_http.AUTH_ORIGINS_ENV,
        json.dumps(
            {
                "https://registry.example:5000": ["https://auth.example:8443"],
            }
        ),
    )
    url, private = registry_http.token_url(
        challenge("https://auth.example:8443/token"),
        "https://registry.example:5000/v2/",
    )
    assert url.startswith("https://auth.example:8443/token?")
    assert private
    with pytest.raises(RegistryRequestError, match="unrelated"):
        registry_http.token_url(challenge("https://auth.example:8443/token"), REGISTRY)


@pytest.mark.parametrize(
    "config", ["[]", "{", '{"https://registry.example":"https://auth.example"}']
)
def test_bad_config_fails_closed(monkeypatch, config):
    monkeypatch.setenv(registry_http.AUTH_ORIGINS_ENV, config)
    with pytest.raises(RegistryRequestError, match="JSON origin mapping"):
        registry_http.token_url(challenge("https://auth.docker.io/token"), REGISTRY)


@pytest.mark.parametrize(
    "status,body", [(401, b""), (200, b"[]"), (200, b"{"), (200, b"{}")]
)
def test_bad_token_response_does_not_recurse(status, body):
    with mock.patch.object(
        digest_verifier,
        "request_bytes",
        side_effect=[
            (
                401,
                {"WWW-Authenticate": challenge("https://auth.docker.io/token")},
                b"",
                PUBLIC,
            ),
            (
                status,
                {"WWW-Authenticate": challenge("file:///example-only")},
                body,
                PUBLIC,
            ),
        ],
    ) as request:
        with pytest.raises(ManifestLookupError):
            RegistryHttpManifestResolver()._request_json(REGISTRY)
    assert request.call_count == 2


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
    ],
)
def test_public_auth_rejects_non_public_addresses(address):
    with pytest.raises(RegistryRequestError, match="network address"):
        registry_http._validate_address(address, False)


@pytest.mark.parametrize(
    "address", [PRIVATE, "127.0.0.1", "::1", "fd00::1", "::ffff:10.0.0.20"]
)
def test_explicit_private_addresses_remain_supported(address):
    assert registry_http._validate_address(address, True) == address


@pytest.mark.parametrize(
    "address", ["169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1"]
)
def test_special_addresses_always_refused(address):
    with pytest.raises(RegistryRequestError):
        registry_http._validate_address(address, True)


def worker_options(**changes):
    return {
        "url": "https://auth.docker.io/token",
        "headers": {"Accept": "application/json"},
        "timeout": 1,
        "limit": 32,
        "peer": "",
        "allow_private": False,
        **changes,
    }


def fake_transport(
    monkeypatch, *, status=200, body=b'{"token":"example"}', addresses=(PUBLIC,)
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        mock.Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                for address in addresses
            ]
        ),
    )
    raw = mock.Mock()
    connect = mock.Mock(return_value=raw)
    monkeypatch.setattr(socket, "create_connection", connect)
    context = mock.Mock(minimum_version=ssl.TLSVersion.MINIMUM_SUPPORTED)
    monkeypatch.setattr(
        registry_http.ssl, "create_default_context", mock.Mock(return_value=context)
    )
    response = mock.Mock(status=status)
    response.read.side_effect = io.BytesIO(body).read
    response.getheaders.return_value = [("Location", "file:///example-only")]
    connection = mock.Mock(sock=None)
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        registry_http.http.client, "HTTPSConnection", mock.Mock(return_value=connection)
    )
    return connect, context, connection, response


@pytest.fixture
def inline_registry_worker(monkeypatch):
    """Run the real transport policy in-process with mocked DNS and sockets."""

    def run_worker(_command, *, input, **_options):
        reply = registry_http._worker_request(json.loads(input))
        return mock.Mock(returncode=0, stdout=json.dumps(reply))

    monkeypatch.setattr(registry_http.subprocess, "run", run_worker)


@pytest.mark.parametrize(
    "host",
    ["registry-1.docker.io", "docker.io", "index.docker.io", "REGISTRY-1.DOCKER.IO."],
)
@pytest.mark.parametrize(
    "addresses", [("127.0.0.1",), (PRIVATE,), ("::ffff:127.0.0.1",), (PUBLIC, PRIVATE)]
)
def test_docker_hub_manifest_rejects_private_dns_before_connect(
    monkeypatch, inline_registry_worker, host, addresses,
):
    connect, _, _, _ = fake_transport(monkeypatch, body=b"{}", addresses=addresses)
    resolver = RegistryHttpManifestResolver()
    url = f"https://{host}/v2/library/alpine/manifests/latest"

    with pytest.raises(ManifestLookupError, match="unauthorized network address"):
        resolver._request_json(url)

    connect.assert_not_called()


@pytest.mark.parametrize(
    ("host", "address"),
    [
        ("registry-1.docker.io", PUBLIC),
        ("registry.example", PRIVATE),
        ("localhost", "127.0.0.1"),
    ],
)
def test_manifest_preserves_public_hub_and_explicit_private_registries(
    monkeypatch, inline_registry_worker, host, address,
):
    connect, _, _, _ = fake_transport(monkeypatch, body=b"{}", addresses=(address,))
    resolver = RegistryHttpManifestResolver()

    _, payload, _body = resolver._request_json(f"https://{host}/v2/app/manifests/latest")

    assert payload == {}
    connect.assert_called_once_with((address, 443), timeout=resolver.timeout)


def test_transport_pins_dns_preserves_tls_name_and_ignores_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    connect, context, connection, _ = fake_transport(monkeypatch)
    result = registry_http._worker_request(worker_options())
    assert result["peer"] == PUBLIC
    assert base64.b64decode(result["body"]) == b'{"token":"example"}'
    connect.assert_called_once_with((PUBLIC, 443), timeout=1)
    assert context.wrap_socket.call_args.kwargs["server_hostname"] == "auth.docker.io"
    socket.getaddrinfo.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("initial,expected", [
    (ssl.TLSVersion.MINIMUM_SUPPORTED, ssl.TLSVersion.TLSv1_2),
    (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2),
    (ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_3),
])
def test_transport_enforces_tls_floor_before_handshake(monkeypatch, initial, expected):
    context = ssl.create_default_context()
    context.minimum_version = initial
    fake_transport(monkeypatch)
    monkeypatch.setattr(registry_http.ssl, "create_default_context", lambda: context)

    def wrap_socket(_raw, *, server_hostname):
        assert context.minimum_version == expected
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert server_hostname == "auth.docker.io"
        return mock.Mock()

    wrap = mock.Mock(side_effect=wrap_socket)
    monkeypatch.setattr(context, "wrap_socket", wrap)
    registry_http._worker_request(worker_options())
    wrap.assert_called_once()


def test_pinned_private_peer_does_not_resolve_again(monkeypatch):
    connect, _, _, _ = fake_transport(monkeypatch)
    registry_http._worker_request(worker_options(peer=PRIVATE, allow_private=True))
    socket.getaddrinfo.assert_not_called()
    connect.assert_called_once_with((PRIVATE, 443), timeout=1)


def test_mixed_dns_answers_fail_before_connect(monkeypatch):
    connect, _, _, _ = fake_transport(monkeypatch, addresses=(PUBLIC, PRIVATE))
    with pytest.raises(RegistryRequestError, match="network address"):
        registry_http._worker_request(worker_options())
    connect.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_followed(monkeypatch, status):
    connect, _, connection, response = fake_transport(monkeypatch, status=status)
    with pytest.raises(RegistryRequestError, match="redirect"):
        registry_http._worker_request(worker_options())
    connect.assert_called_once()
    connection.request.assert_called_once()
    response.read.assert_not_called()


def test_oversize_body_is_rejected_without_unbounded_read(monkeypatch):
    _, _, connection, response = fake_transport(monkeypatch, body=b"x" * 100)
    with pytest.raises(RegistryRequestError, match="permitted size"):
        registry_http._worker_request(worker_options())
    response.read.assert_called_once_with(33)
    connection.close.assert_called_once()


def test_failed_connections_cannot_fall_back_to_unvalidated_dns(monkeypatch):
    connect, _, connection, _ = fake_transport(
        monkeypatch, addresses=(PUBLIC, "8.8.4.4", PUBLIC)
    )
    connect.side_effect = OSError("offline")
    with pytest.raises(OSError):
        registry_http._worker_request(worker_options())
    connection.request.assert_not_called()


def test_request_deadline_kills_stalled_worker(monkeypatch, tmp_path):
    worker = tmp_path / "stalled.py"
    worker.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(registry_http, "__file__", str(worker))
    start = time.monotonic()
    with pytest.raises(RegistryRequestError, match="timed out"):
        registry_http.request_bytes(REGISTRY, headers={}, timeout=0.1)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("image_ref", [
    "alpine:3.20",
    "docker.io/library/alpine:3.20",
    "index.docker.io/library/alpine:3.20",
    "registry-1.docker.io/library/alpine:3.20",
    "DOCKER.IO/library/alpine:3.20",
])
def test_live_probe_uses_production_policy(image_ref):
    path = Path(__file__).with_name("live-digest-verification.py")
    spec = importlib.util.spec_from_file_location("digest_probe_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        with mock.patch.object(
            digest_verifier,
            "request_bytes",
            return_value=(
                401,
                {"WWW-Authenticate": challenge("file:///example-only")},
                b"",
                PUBLIC,
            ),
        ) as request:
            with pytest.raises(module.ProbeError):
                module.fetch_manifest(module.parse_image_ref(image_ref))
        assert request.call_count == 1
        assert request.call_args.args[0] == (
            "https://registry-1.docker.io/v2/library/alpine/manifests/3.20"
        )
        with mock.patch.object(
            digest_verifier,
            "request_bytes",
            return_value=(
                200, {"Docker-Content-Digest": "sha256:" + "a" * 64}, b"{}", PUBLIC,
            ),
        ):
            with pytest.raises(module.ProbeError, match="integrity check failed"):
                module.fetch_manifest(module.parse_image_ref(image_ref))
        image = parse_registry_image("alpine:3.20")
        assert image.http_registry == "registry-1.docker.io"
    finally:
        sys.modules.pop(spec.name, None)
