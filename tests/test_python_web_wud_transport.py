"""Characterize the WUD transport used by both cache and metadata adapters."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from wudup import web_wud_api, web_wud_transport
from wudup.web_models import WudApiClientConfig


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("body, expected", [(b"", {}), (b'{"ok": true}', {"ok": True})])
def test_transport_requests_preserve_headers_timeout_and_response_cleanup(
    monkeypatch, method, body, expected,
):
    calls = []
    closed = []

    @contextmanager
    def urlopen(request, *, timeout):
        calls.append((request, timeout))

        class Response:
            def read(self):
                return body

        try:
            yield Response()
        finally:
            closed.append(True)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    config = WudApiClientConfig(header_items=(("Authorization", "Bearer test-value"),))
    url = "https://wud.example/base/api/containers"
    if method == "GET":
        result = web_wud_transport._request_json(url, config)
        expected_timeout = web_wud_api.WUD_API_TIMEOUT_SECONDS
    else:
        result = web_wud_transport._post_json(url, config, timeout=17.5)
        expected_timeout = 17.5

    assert result == expected
    assert closed == [True]
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == url
    assert request.get_method() == method
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer test-value"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == web_wud_api.WUD_API_USER_AGENT
    assert timeout == expected_timeout


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_requests_propagate_the_original_transport_error(monkeypatch, method):
    error = urllib.error.HTTPError("https://wud.example", 429, "limited", {}, None)

    def urlopen(_request, *, timeout):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    request = web_wud_transport._request_json if method == "GET" else web_wud_transport._post_json
    with pytest.raises(urllib.error.HTTPError) as caught:
        request("https://wud.example")
    assert caught.value is error


def test_transport_requests_close_response_before_json_error(monkeypatch):
    closed = []

    @contextmanager
    def urlopen(_request, *, timeout):
        class Response:
            def read(self):
                return b"not JSON"

        try:
            yield Response()
        finally:
            closed.append(True)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(json.JSONDecodeError):
        web_wud_transport._request_json("https://wud.example")
    assert closed == [True]


_BEARER_CONFIG = WudApiClientConfig(
    header_items=(("Authorization", "Bearer test-value"), ("X-Api-Key", "static")),
)


def test_transport_keeps_configured_headers_off_redirected_requests(monkeypatch):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        return contextmanager(lambda: (yield SimpleNamespace(read=lambda: b"{}")))()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    web_wud_transport._request_json("https://wud.example/api", _BEARER_CONFIG)

    request = calls[0]
    assert request.unredirected_hdrs == {
        "Authorization": "Bearer test-value",
        "X-api-key": "static",
    }
    assert "Authorization" not in request.headers
    assert request.headers["Accept"] == "application/json"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_reports_redirect_when_credentials_are_configured(monkeypatch, method):
    def urlopen(_request, *, timeout):
        response = SimpleNamespace(url="https://sso.example/login?state=secret", read=lambda: b"{}")
        return contextmanager(lambda: (yield response))()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    request = web_wud_transport._request_json if method == "GET" else web_wud_transport._post_json

    with pytest.raises(web_wud_transport.WudApiRedirectError) as caught:
        request("https://wud.example/api", _BEARER_CONFIG)

    assert isinstance(caught.value, OSError)
    assert "WUD_API_BASE_URL" in str(caught.value)
    assert "sso.example" not in str(caught.value)


def test_transport_reports_redirect_that_ends_in_http_error(monkeypatch):
    def urlopen(_request, *, timeout):
        raise urllib.error.HTTPError("https://sso.example/login", 401, "denied", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(web_wud_transport.WudApiRedirectError):
        web_wud_transport._request_json("https://wud.example/api", _BEARER_CONFIG)


def test_transport_follows_redirect_without_credentials(monkeypatch):
    def urlopen(_request, *, timeout):
        response = SimpleNamespace(url="https://wud.example/api/", read=lambda: b'{"ok": true}')
        return contextmanager(lambda: (yield response))()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert web_wud_transport._request_json("https://wud.example/api") == {"ok": True}


def _serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_real_redirect_does_not_forward_credentials(monkeypatch, method):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    received = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    target = _serve(Target)

    class Wud(BaseHTTPRequestHandler):
        def _redirect(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/sso/login")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _redirect

        def log_message(self, *_args):
            pass

    wud = _serve(Wud)
    try:
        request = (
            web_wud_transport._request_json
            if method == "GET"
            else web_wud_transport._post_json
        )
        with pytest.raises(web_wud_transport.WudApiRedirectError):
            request(f"http://127.0.0.1:{wud.server_port}/api", _BEARER_CONFIG)
    finally:
        wud.shutdown()
        target.shutdown()

    assert len(received) == 1
    assert "Authorization" not in received[0]
    assert "X-Api-Key" not in received[0]
