from __future__ import annotations

from dataclasses import fields

import pytest
from tests.web_test_helpers import _web_env

from wudup import web_wud_cache, web_wud_transport
from wudup.web import load_web_settings
from wudup.web_models import WudApiStatus


@pytest.mark.parametrize(
    ("payload", "expected_ids"),
    [
        ({"id": "observed"}, ["observed"]),
        ([{"id": "observed"}, {}, {"id": ""}], ["observed", "requested"]),
    ],
)
def test_watch_response_records_each_cooldown_before_reading_next_payload(
    tmp_path,
    monkeypatch,
    payload,
    expected_ids,
):
    settings = load_web_settings(environ=_web_env(tmp_path))
    cache_key = ("https://wud.example", "fixture")
    events = []
    monkeypatch.setattr(web_wud_cache.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(web_wud_cache, "_watch_rate_limit_until", {})
    monkeypatch.setattr(
        web_wud_transport, "_post_json", lambda *_args, **_kwargs: payload
    )

    def parse(item, _settings):
        events.append(("parse", item.get("id")))
        return item.get("id")

    original_start = web_wud_cache._start_watch_rate_limit_cooldown

    def start(key, container_id):
        original_start(key, container_id)
        events.append(("cooldown", container_id))

    monkeypatch.setattr(web_wud_cache, "_watch_rate_limited_container_id", parse)
    monkeypatch.setattr(web_wud_cache, "_start_watch_rate_limit_cooldown", start)
    result = web_wud_cache._watch_batch(
        settings,
        base_url=cache_key[0],
        normalized_base_url=cache_key[0],
        cache_key=cache_key,
        watch_items=[("/watch", "requested")],
        requested_count=1,
        cooldown_remaining=300.0,
    )

    expected_events = [("parse", "observed"), ("cooldown", "observed")]
    if isinstance(payload, list):
        expected_events.extend(
            [
                ("parse", None),
                ("parse", ""),
                ("cooldown", "requested"),
            ]
        )
    assert events == expected_events
    assert result.watched_all is False
    assert result.watched_count == 1
    assert result.cooldown_remaining == 300.0
    assert result.error is None
    assert set(web_wud_cache._watch_rate_limit_until) == {
        (cache_key, container_id) for container_id in expected_ids
    }


def test_watch_response_preserves_partial_cooldown_when_later_payload_fails(
    tmp_path,
    monkeypatch,
):
    settings = load_web_settings(environ=_web_env(tmp_path))
    cache_key = ("https://wud.example", "fixture")
    monkeypatch.setattr(web_wud_cache.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(web_wud_cache, "_watch_rate_limit_until", {})
    monkeypatch.setattr(web_wud_cache, "_snapshot_cache", {})
    monkeypatch.setattr(
        web_wud_transport,
        "_post_json",
        lambda *_args, **_kwargs: ["first", "bad"],
    )

    def parse(item, _settings):
        if item == "bad":
            assert (cache_key, "observed") in web_wud_cache._watch_rate_limit_until
            raise ValueError("invalid observation fixture")
        return "observed"

    monkeypatch.setattr(web_wud_cache, "_watch_rate_limited_container_id", parse)
    result = web_wud_cache._watch_batch(
        settings,
        base_url=cache_key[0],
        normalized_base_url=cache_key[0],
        cache_key=cache_key,
        watch_items=[("/watch", "requested")],
        requested_count=1,
        cooldown_remaining=0.0,
    )

    assert result.watched_all is False
    assert result.watched_count == 1
    assert (
        result.cooldown_remaining == web_wud_cache.WUD_API_RATE_LIMIT_COOLDOWN_SECONDS
    )
    assert result.error is not None
    assert result.error.watched_count == 1
    assert result.error.snapshot.status.state == "error"
    assert "invalid observation fixture" in result.error.snapshot.status.detail
    assert web_wud_cache._snapshot_cache[cache_key] is result.error.snapshot


def test_watch_dataclass_replacements_preserve_concrete_types_and_other_fields(
    tmp_path,
    monkeypatch,
):
    settings = load_web_settings(environ=_web_env(tmp_path))
    snapshot = web_wud_cache.WudApiSnapshot(
        status=WudApiStatus(
            state="ready",
            available=True,
            metadata_available=True,
            last_checked_at="2026-09-22T00:00:00Z",
            detail="Existing detail.",
        ),
        retryable_degraded_container_ids=("observed",),
        metadata_checked=True,
        checked_monotonic=12.0,
    )
    original = web_wud_cache.WudApiWatchResult(
        snapshot=snapshot,
        watched=True,
        requested_count=2,
        watched_count=1,
    )
    monkeypatch.setattr(web_wud_cache, "_watch_paths", lambda *_args: original)

    updated = web_wud_cache.watch_all(settings)

    assert type(updated) is web_wud_cache.WudApiWatchResult
    assert updated is not original
    assert updated.remaining_degraded_container_ids == ("observed",)
    for field in fields(original):
        if field.name != "remaining_degraded_container_ids":
            assert getattr(updated, field.name) is getattr(original, field.name)

    limited = web_wud_cache._with_watch_rate_limit_detail(snapshot, 3.2)

    assert type(limited) is web_wud_cache.WudApiSnapshot
    assert limited is not snapshot
    assert limited.status is not snapshot.status
    assert limited.status.detail == (
        "Existing detail. WUD temporarily paused registry checks after receiving "
        "HTTP 429. Try again in 4 seconds."
    )
    assert snapshot.status.detail == "Existing detail."
    for field in fields(snapshot):
        if field.name != "status":
            assert getattr(limited, field.name) is getattr(snapshot, field.name)
