"""Characterize the WUD transport used by both cache and metadata adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager

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
