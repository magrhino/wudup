"""Characterization of Discord payload rendering and bounded HTTP delivery."""
from __future__ import annotations

import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wudup import web_discord as discord
from wudup.web_models import ReleaseNoteLink, ReleaseNotificationItem


@pytest.mark.parametrize("status", [200, 204])
def test_delivery_preserves_json_headers_timeout_and_response_cleanup(monkeypatch, status):
    response = MagicMock()
    response.__enter__.return_value = SimpleNamespace(status=status)
    opening = MagicMock(return_value=response)
    monkeypatch.setattr(discord.urllib.request, "urlopen", opening)

    discord._post_discord_payload("https://discord.test/example-only", {"content": "A → B"})

    request = opening.call_args.args[0]
    assert request.full_url == "https://discord.test/example-only"
    assert request.method == "POST"
    assert request.data == b'{"content":"A \\u2192 B"}'
    assert dict(request.header_items()) == {
        "Accept": "application/json",
        "Content-type": "application/json",
        "User-agent": "wudup-webui-release-notifications/1.0",
    }
    assert opening.call_args.kwargs == {"timeout": 10.0}
    response.__exit__.assert_called_once_with(None, None, None)


def test_delivery_preserves_non_success_error_and_cleanup(monkeypatch):
    response = MagicMock()
    headers = {"Retry-After": "2"}
    response.__enter__.return_value = SimpleNamespace(status=429, headers=headers)
    monkeypatch.setattr(discord.urllib.request, "urlopen", MagicMock(return_value=response))

    with pytest.raises(urllib.error.HTTPError) as caught:
        discord._post_discord_payload("https://discord.test/example-only", {})

    assert caught.value.code == 429
    assert caught.value.reason == "Discord webhook request failed"
    assert caught.value.headers is headers
    assert caught.value.url == "https://discord.test/example-only"
    assert response.__exit__.call_args.args[1] is caught.value


def test_delivery_propagates_transport_error_identity(monkeypatch):
    failure = urllib.error.URLError("test connection failure")
    monkeypatch.setattr(discord.urllib.request, "urlopen", MagicMock(side_effect=failure))

    with pytest.raises(urllib.error.URLError) as caught:
        discord._post_discord_payload("https://discord.test/example-only", {})

    assert caught.value is failure


def test_digest_payload_preserves_exact_copy_escaping_and_suppression():
    item = ReleaseNotificationItem(
        line_no=1,
        image="repo/app:1.0",
        service_key="media/`app`\nworker",
        title="App Tag Update",
        description="Release ready",
        status="ready",
        current_version="1.0",
        target_version="2.0",
        category="routine",
        reason_label="patch update with release notes",
        links=[ReleaseNoteLink(
            label="GitHub release", url="https://example.test/release/2.0)",
            kind="github_release",
        )],
    )
    skipped = item.model_copy(update={"line_no": 2, "skipped_reason": "already sent"})

    batches = discord._payload_batches([skipped, item])

    assert batches == [{
        "index": 1,
        "count": 1,
        "items": [item],
        "payload": {
            "username": "WUDup Release Notes",
            "allowed_mentions": {"parse": []},
            "flags": 4,
            "content": (
                "🧾 WUDup batch — 1 updates found\n\n🟢 Routine\n"
                "• media/'app' worker `1.0` → `2.0` — patch update with release notes"
                " — [release](https://example.test/release/2.0%29)\n\n"
                "Open WUDup for full notes, digests, and apply plan."
            ),
        },
    }]
