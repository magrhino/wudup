from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.web_test_helpers import (
    _assert_generic_auth_failed,
    _client,
    _csrf_headers,
    _setup_admin,
    _web_env,
)

from wudup import web_auth as web_auth_module
from wudup.web import create_app


def _ipv4(octet1: int, octet2: int, octet3: int, octet4: int) -> str:
    return ".".join(str(octet) for octet in (octet1, octet2, octet3, octet4))


# Documentation-only address ranges keep tests independent of host network config.
_DOC_PROXY_V4 = _ipv4(192, 0, 2, 10)
_DOC_PROXY_ALT_V4 = _ipv4(192, 0, 2, 35)
_DOC_PROXY_V6 = str(ipaddress.IPv6Address((0x2001 << 112) | (0x0DB8 << 96) | 0x23))
_DOC_UNTRUSTED_CLIENT_V4 = _ipv4(192, 0, 2, 1)
_DOC_FORWARDED_CLIENT_V4 = _ipv4(198, 51, 100, 10)
_DOC_FORWARDED_CLIENT_ALT_V4 = _ipv4(203, 0, 113, 20)


def test_api_rejects_unauthenticated_requests_without_dev_bypass(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/status")

    assert response.status_code == 403
    assert response.json()["detail"] == "setup required"


def test_api_accepts_bearer_token(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_TOKEN": "secret"})
    _setup_admin(client)
    api_client = _client(tmp_path, {"WUD_WEB_TOKEN": "secret"})

    response = api_client.get(
        "/api/v1/status",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["auth_required"] is True


def test_request_actor_type_falls_back_to_unknown_without_auth(tmp_path: Path) -> None:
    settings = _client(tmp_path, {"WUD_WEB_TOKEN": "secret"}).app.state.web_settings
    request = SimpleNamespace(headers={}, cookies={})

    assert web_auth_module._request_actor_type(settings, request) == "unknown"


def test_csrf_endpoint_sets_double_submit_cookie(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/auth/csrf")

    assert response.status_code == 200
    assert response.json()["csrf_token"]
    assert client.cookies.get("wud_csrf_token") == response.json()["csrf_token"]


def test_browser_token_login_payload_is_not_accepted(tmp_path: Path) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path, {"WUD_WEB_TOKEN": "secret"})

    response = client.post(
        "/api/v1/auth/login",
        json={"token": "secret"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422


def test_auth_error_responses_do_not_leak_submitted_secrets_or_tokens(
    tmp_path: Path,
    caplog,
) -> None:
    web_token = "web-token-secret-value"
    setup_claim = "submitted-setup-claim-secret"
    setup_password = "submitted-setup-password"
    login_password = "submitted-login-password"
    reset_claim = "submitted-reset-claim-secret"
    reset_password = "submitted-reset-password"
    client = _client(tmp_path, {"WUD_WEB_TOKEN": web_token})

    with caplog.at_level(logging.DEBUG):
        invalid_setup = client.post(
            "/api/v1/setup/claim",
            json={
                "claim": setup_claim,
                "username": "admin",
                "password": setup_password,
            },
            headers=_csrf_headers(client),
        )
        _setup_admin(client)
        csrf_headers = _csrf_headers(client)
        csrf_token = csrf_headers["x-wud-csrf-token"]
        invalid_token_login = client.post(
            "/api/v1/auth/login",
            json={"token": web_token},
            headers=csrf_headers,
        )
        bad_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": login_password},
            headers=csrf_headers,
        )
        good_login = client.post(
            "/api/v1/auth/login",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
            },
            headers=csrf_headers,
        )
        session_cookie = client.cookies.get("wud_session")
        invalid_reset = client.post(
            "/api/v1/auth/reset-admin/claim",
            json={
                "claim": reset_claim,
                "username": "admin",
                "password": reset_password,
            },
            headers=_csrf_headers(client),
        )

    assert invalid_setup.status_code == 403
    assert invalid_token_login.status_code == 422
    _assert_generic_auth_failed(bad_login)
    assert good_login.status_code == 200
    assert session_cookie
    assert invalid_reset.status_code == 403

    response_text = "\n".join(
        response.text
        for response in (
            invalid_setup,
            invalid_token_login,
            bad_login,
            good_login,
            invalid_reset,
        )
    )
    for secret in (
        setup_claim,
        setup_password,
        web_token,
        login_password,
        session_cookie,
        csrf_token,
        reset_claim,
        reset_password,
    ):
        assert secret not in response_text
        assert secret not in caplog.text


def test_api_rejects_unauthenticated_requests_after_setup(
    tmp_path: Path,
) -> None:
    setup_client = _client(tmp_path)
    _setup_admin(setup_client)
    client = _client(tmp_path)

    response = client.get("/api/v1/status")

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


def test_api_allows_dev_auth_bypass_only_when_explicitly_enabled(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json()["auth_required"] is False
    assert response.json()["dev_auth_bypass"] is True


def test_csrf_origin_scaffold_rejects_unsafe_api_requests(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.post("/api/v1/runs")

    assert response.status_code == 403
    assert response.json()["detail"] == "origin header is required"


def test_host_allowlist_rejects_unknown_hosts(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/v1/setup/status", headers={"Host": "evil.test"})

    assert response.status_code == 400
    assert response.json()["detail"] == "host is not allowed"


def test_parse_trusted_proxies_resolves_hostnames(monkeypatch) -> None:
    def fake_getaddrinfo(hostname: str, _port, *_args, **_kwargs):
        assert hostname == "npmplus"
        return [
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_ALT_V4, 0),
            ),
            (
                web_auth_module.socket.AF_INET6,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_V6, 0, 0, 0),
            ),
        ]

    monkeypatch.setattr(web_auth_module.socket, "getaddrinfo", fake_getaddrinfo)

    networks = web_auth_module._parse_trusted_proxies("npmplus")

    assert [str(network) for network in networks] == [
        f"{_DOC_PROXY_ALT_V4}/32",
        f"{_DOC_PROXY_V6}/128",
    ]


def test_parse_trusted_proxies_deduplicates_resolved_hostnames(monkeypatch) -> None:
    def fake_getaddrinfo(hostname: str, _port, *_args, **_kwargs):
        assert hostname == "npmplus"
        return [
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_V4, 0),
            ),
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_ALT_V4, 0),
            ),
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_ALT_V4, 0),
            ),
        ]

    monkeypatch.setattr(web_auth_module.socket, "getaddrinfo", fake_getaddrinfo)

    networks = web_auth_module._parse_trusted_proxies(
        f"{_DOC_PROXY_V4}/32,npmplus,{_DOC_PROXY_ALT_V4}/32"
    )

    assert [str(network) for network in networks] == [
        f"{_DOC_PROXY_V4}/32",
        f"{_DOC_PROXY_ALT_V4}/32",
    ]


def test_parse_trusted_proxies_rejects_unresolved_hostname(monkeypatch) -> None:
    def fake_getaddrinfo(_hostname: str, _port, *_args, **_kwargs):
        raise web_auth_module.socket.gaierror("not found")

    monkeypatch.setattr(web_auth_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(
        web_auth_module.WebConfigError,
        match="WUD_WEB_TRUSTED_PROXIES contains unresolvable hostname: npmplus",
    ):
        web_auth_module._parse_trusted_proxies("npmplus")


def test_parse_trusted_proxies_rejects_hostname_without_usable_ips(
    monkeypatch,
) -> None:
    def fake_getaddrinfo(hostname: str, _port, *_args, **_kwargs):
        assert hostname == "npmplus"
        return [
            (
                web_auth_module.socket.AF_UNSPEC,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                ("ignored", 0),
            ),
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (),
            ),
            (
                web_auth_module.socket.AF_INET6,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                ("not-an-ip-address", 0, 0, 0),
            ),
        ]

    monkeypatch.setattr(web_auth_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(
        web_auth_module.WebConfigError,
        match=(
            "WUD_WEB_TRUSTED_PROXIES hostname did not resolve to an IP address: "
            "npmplus"
        ),
    ):
        web_auth_module._parse_trusted_proxies("npmplus")


def test_parse_trusted_proxies_rejects_invalid_cidr_hostname(monkeypatch) -> None:
    def unexpected_getaddrinfo(_hostname: str, _port, *_args, **_kwargs):
        raise AssertionError("CIDR-like values must not be resolved as hostnames")

    monkeypatch.setattr(
        web_auth_module.socket,
        "getaddrinfo",
        unexpected_getaddrinfo,
    )

    with pytest.raises(
        web_auth_module.WebConfigError,
        match="WUD_WEB_TRUSTED_PROXIES contains invalid address: npmplus/32",
    ):
        web_auth_module._parse_trusted_proxies("npmplus/32")


def test_forwarded_headers_require_trusted_proxy(tmp_path: Path) -> None:
    env = {
        "WUD_WEB_ALLOWED_HOSTS": "internal.test,wud.example.test",
        "WUD_WEB_TRUSTED_PROXIES": f"{_DOC_PROXY_V4}/32",
        "WUD_WEB_SECURE_COOKIES": "false",
    }
    app = create_app(environ=_web_env(tmp_path, env))
    headers = {
        "Host": "internal.test",
        "Origin": "https://wud.example.test",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "wud.example.test",
    }
    untrusted = TestClient(
        app,
        base_url="http://internal.test",
        client=(_DOC_UNTRUSTED_CLIENT_V4, 50000),
    )
    trusted = TestClient(
        app,
        base_url="http://internal.test",
        client=(_DOC_PROXY_V4, 50000),
    )
    csrf_response = untrusted.get("/api/v1/auth/csrf", headers=headers)
    untrusted_headers = {
        **headers,
        "x-wud-csrf-token": csrf_response.json()["csrf_token"],
    }

    untrusted_response = untrusted.post(
        "/api/v1/setup/claim",
        json={
            "claim": app.state.web_setup_claim,
            "username": "admin",
            "password": "correct horse battery staple",
        },
        headers=untrusted_headers,
    )
    csrf_response = trusted.get("/api/v1/auth/csrf", headers=headers)
    trusted_headers = {
        **headers,
        "x-wud-csrf-token": csrf_response.json()["csrf_token"],
    }
    trusted_response = trusted.post(
        "/api/v1/setup/claim",
        json={
            "claim": app.state.web_setup_claim,
            "username": "admin",
            "password": "correct horse battery staple",
        },
        headers=trusted_headers,
    )

    assert untrusted_response.status_code == 403
    assert untrusted_response.json()["detail"] == "origin is not allowed"
    assert trusted_response.status_code == 200


def test_forwarded_headers_trust_hostname_resolved_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_getaddrinfo(hostname: str, _port, *_args, **_kwargs):
        if hostname != "npmplus":
            raise web_auth_module.socket.gaierror("not found")
        return [
            (
                web_auth_module.socket.AF_INET,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_V4, 0),
            ),
            (
                web_auth_module.socket.AF_INET6,
                web_auth_module.socket.SOCK_STREAM,
                0,
                "",
                (_DOC_PROXY_V6, 0, 0, 0),
            ),
        ]

    monkeypatch.setattr(web_auth_module.socket, "getaddrinfo", fake_getaddrinfo)
    app = create_app(
        environ=_web_env(
            tmp_path,
            {
                "WUD_WEB_ALLOWED_HOSTS": "internal.test,wud.example.test",
                "WUD_WEB_TRUSTED_PROXIES": "npmplus",
            },
        )
    )
    settings = app.state.web_settings
    trusted_request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V4),
        headers={
            "x-forwarded-for": _DOC_FORWARDED_CLIENT_V4,
            "x-forwarded-proto": "https",
            "x-forwarded-host": "wud.example.test",
        },
    )
    trusted_ipv6_request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V6),
        headers=trusted_request.headers,
    )
    untrusted_request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_UNTRUSTED_CLIENT_V4),
        headers=trusted_request.headers,
    )

    assert (
        web_auth_module._trusted_forwarded_origin(trusted_request, settings)
        == "https://wud.example.test"
    )
    assert (
        web_auth_module._request_client_address(trusted_request, settings)
        == _DOC_FORWARDED_CLIENT_V4
    )
    assert (
        web_auth_module._trusted_forwarded_origin(trusted_ipv6_request, settings)
        == "https://wud.example.test"
    )
    assert (
        web_auth_module._request_client_address(trusted_ipv6_request, settings)
        == _DOC_FORWARDED_CLIENT_V4
    )
    assert web_auth_module._trusted_forwarded_origin(untrusted_request, settings) == ""
    assert (
        web_auth_module._request_client_address(untrusted_request, settings)
        == _DOC_UNTRUSTED_CLIENT_V4
    )


def test_trusted_forwarded_headers_use_last_proxy_hop(tmp_path: Path) -> None:
    app = create_app(
        environ=_web_env(
            tmp_path,
            {
                "WUD_WEB_ALLOWED_HOSTS": "internal.test,wud.example.test",
                "WUD_WEB_TRUSTED_PROXIES": f"{_DOC_PROXY_V4}/32",
            },
        )
    )
    settings = app.state.web_settings
    forwarded_request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V4),
        headers={
            "forwarded": (
                f"for={_DOC_FORWARDED_CLIENT_V4};proto=http;host=evil.test, "
                f"for={_DOC_FORWARDED_CLIENT_ALT_V4};proto=https;"
                "host=wud.example.test"
            ),
        },
    )
    x_forwarded_request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V4),
        headers={
            "x-forwarded-for": (
                f"{_DOC_FORWARDED_CLIENT_V4}, {_DOC_FORWARDED_CLIENT_ALT_V4}"
            ),
            "x-forwarded-proto": "http, https",
            "x-forwarded-host": "evil.test, wud.example.test",
        },
    )

    assert (
        web_auth_module._trusted_forwarded_origin(forwarded_request, settings)
        == "https://wud.example.test"
    )
    assert (
        web_auth_module._request_client_address(forwarded_request, settings)
        == _DOC_FORWARDED_CLIENT_ALT_V4
    )
    assert (
        web_auth_module._trusted_forwarded_origin(x_forwarded_request, settings)
        == "https://wud.example.test"
    )
    assert (
        web_auth_module._request_client_address(x_forwarded_request, settings)
        == _DOC_FORWARDED_CLIENT_ALT_V4
    )


def test_trusted_x_forwarded_headers_win_over_client_forwarded(
    tmp_path: Path,
) -> None:
    app = create_app(
        environ=_web_env(
            tmp_path,
            {
                "WUD_WEB_ALLOWED_HOSTS": "internal.test,wud.example.test",
                "WUD_WEB_TRUSTED_PROXIES": f"{_DOC_PROXY_V4}/32",
            },
        )
    )
    settings = app.state.web_settings
    request = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V4),
        headers={
            "forwarded": (
                f"for={_DOC_FORWARDED_CLIENT_ALT_V4};proto=http;host=evil.test"
            ),
            "x-forwarded-for": _DOC_FORWARDED_CLIENT_V4,
            "x-forwarded-proto": "https",
            "x-forwarded-host": "wud.example.test",
        },
    )

    assert (
        web_auth_module._trusted_forwarded_origin(request, settings)
        == "https://wud.example.test"
    )
    assert (
        web_auth_module._request_client_address(request, settings)
        == _DOC_FORWARDED_CLIENT_V4
    )


def test_trusted_forwarded_for_must_be_an_ip_address(tmp_path: Path) -> None:
    app = create_app(
        environ=_web_env(
            tmp_path,
            {"WUD_WEB_TRUSTED_PROXIES": f"{_DOC_PROXY_V4}/32"},
        )
    )
    settings = app.state.web_settings

    for value in ("_hidden", "not-an-ip", '"[not-an-ip]:80"'):
        request = SimpleNamespace(
            client=SimpleNamespace(host=_DOC_PROXY_V4),
            headers={"forwarded": f"for={value}"},
        )
        assert web_auth_module._request_client_address(request, settings) == (
            _DOC_PROXY_V4
        ), value
    bracketed = SimpleNamespace(
        client=SimpleNamespace(host=_DOC_PROXY_V4),
        headers={"forwarded": 'for="[2001:db8::7]:4711"'},
    )
    assert web_auth_module._request_client_address(bracketed, settings) == (
        "2001:db8::7"
    )


def test_secure_cookie_auto_follows_effective_origin(tmp_path: Path) -> None:
    http_client = _client(tmp_path)
    https_client = _client(
        tmp_path,
        {
            "WUD_WEB_PUBLIC_ORIGIN": "https://wud.example.test",
            "WUD_WEB_ALLOWED_HOSTS": "wud.example.test",
        },
    )

    http_response = http_client.get("/api/v1/auth/csrf")
    https_response = https_client.get(
        "/api/v1/auth/csrf",
        headers={"Host": "wud.example.test"},
    )

    assert "Secure" not in http_response.headers["set-cookie"]
    assert "Secure" in https_response.headers["set-cookie"]
