from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from tests.web_test_helpers import (
    _capture_discord_posts,
    _client,
    _fake_release_refresh,
    _install_wud_api,
    _store_web_setting,
    _wud_api_container,
)

from wudup import (
    web_release_notifications as notifications_module,
)
from wudup import (
    web_scheduler,
)
from wudup.db import init_db, insert_snooze, open_db
from wudup.release_notes import ReleaseNoteInfo as ReleaseNoteData
from wudup.release_notes import ReleaseNoteLink as ReleaseNoteLinkData
from wudup.release_notes import ReleaseSecurityAssessment
from wudup.web_release_notification_state import (
    RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND,
)

_ENV = {
    "WUD_WEB_DEV_NO_AUTH": "true",
    "WUD_WEB_MUTATIONS_ENABLED": "true",
    "WUD_RELEASE_NOTES_ENABLED": "true",
    "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
    "WUD_PENDING_SOURCE": "api",
}


def _shutdown(client) -> None:
    notifications_module.shutdown_release_notification_scheduler_state(client.app.state)
    web_scheduler.shutdown_auto_update_scheduler_state(client.app.state)


def test_poll_skips_when_delivery_mode_on_demand(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _store_web_setting(
        tmp_path,
        "release_notifications.delivery_mode",
        RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND,
    )
    _install_wud_api(
        monkeypatch,
        containers=[_wud_api_container(name="app")],
        triggers={"docker.local.app": (200, [])},
    )
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)

    def fail_trigger_lookup(*_args, **_kwargs):
        raise AssertionError("ordinary on-demand items must not fetch WUD triggers")

    monkeypatch.setattr(
        notifications_module.web_wud_api,
        "container_triggers",
        fail_trigger_lookup,
    )
    client = _client(tmp_path, _ENV)
    try:
        response = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
    finally:
        _shutdown(client)

    assert response is None
    assert posted == []


def test_poll_sends_verified_security_notification_in_on_demand_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _store_web_setting(
        tmp_path,
        "release_notifications.delivery_mode",
        RELEASE_NOTIFICATIONS_DELIVERY_MODE_ON_DEMAND,
    )
    _install_wud_api(
        monkeypatch,
        containers=[_wud_api_container(name="app")],
        triggers={"docker.local.app": (200, [])},
    )

    def verified_release(_conn, targets, _environ, **_kwargs):
        return [
            ReleaseNoteData(
                line_no=target.line_no,
                status="ready",
                provider="github",
                image_repo="acme/app",
                upstream_repo="acme/app",
                release_tag="2.0.0",
                title="Security patch",
                links=[
                    ReleaseNoteLinkData(
                        label="GHSA-aaaa-bbbb-cccc",
                        url="https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                        kind="security_advisory",
                    )
                ],
                security=ReleaseSecurityAssessment(
                    outcome="verified_critical_high",
                    severity="critical",
                    reason_code="verified_exposure",
                    reason="Verified Critical advisory affects 1.0 and is patched by 2.0.",
                    advisory_ids=["GHSA-aaaa-bbbb-cccc"],
                ),
            )
            for target in targets
        ]

    monkeypatch.setattr(
        notifications_module,
        "refresh_release_notes",
        verified_release,
    )
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _ENV)
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        insert_snooze(
            conn,
            service_key="app",
            snoozed_until="2099-01-01T00:00:00+00:00",
            reason="demo maintenance",
        )
    try:
        response = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
    finally:
        _shutdown(client)

    assert response is not None
    assert response.sent is True
    assert response.items[0].category == "security_urgent"
    assert "Critical/High security" in posted[0][1]["content"]


def test_poll_sends_wud_api_notifications_without_trigger_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[_wud_api_container(name="app")],
        triggers={"docker.local.app": (200, [])},
    )
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _ENV)
    # A stale shared file must never feed notifications.
    (tmp_path / "state" / "images.todo").write_text(
        "repo/stale:1.0 tag=9.9\n",
        encoding="utf-8",
    )
    try:
        response = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
    finally:
        _shutdown(client)

    assert response is not None
    assert response.sent is True
    assert response.source.active == "api"
    assert len(posted) == 1
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        row = conn.execute(
            """
            SELECT metadata_json
            FROM update_runs
            WHERE mode = 'web-release-notifications'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    metadata = json.loads(row["metadata_json"])
    assert metadata["source"] == notifications_module.SCHEDULER_ACTOR_TYPE
    assert metadata["actor_type"] == notifications_module.SCHEDULER_ACTOR_TYPE


def test_poll_refreshes_cached_wud_api_source_before_sending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    containers: list[dict[str, object]] = []
    _install_wud_api(
        monkeypatch,
        containers=containers,
        triggers={"docker.local.app": (200, [])},
    )
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)
    client = _client(
        tmp_path,
        {
            **_ENV,
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.notification-refresh.test:3000",
        },
    )
    try:
        assert client.get("/api/v1/status").json()["pending_count"] == 0
        containers.append(_wud_api_container(name="app"))

        response = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
        pending = client.get("/api/v1/pending").json()
    finally:
        _shutdown(client)

    assert response is not None
    assert response.sent is True
    assert response.source.active == "api"
    assert len(posted) == 1
    assert pending["source"]["active"] == "api"
    assert pending["count"] == 1


def test_poll_skips_duplicate_notifications(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[_wud_api_container(name="app")],
        triggers={"docker.local.app": (200, [])},
    )
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _ENV)
    try:
        first = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
        second = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
    finally:
        _shutdown(client)

    assert first is not None
    assert first.sent is True
    assert second is None
    assert len(posted) == 1


def test_poll_skips_degraded_wud_api_quietly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=(500, {}))
    posted = _capture_discord_posts(monkeypatch)

    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("release metadata should not refresh when WUD API is degraded")

    monkeypatch.setattr(notifications_module, "refresh_release_notes", fail_refresh)
    client = _client(tmp_path, _ENV)
    try:
        response = notifications_module.poll_wud_api_release_notifications(
            client.app.state.web_settings,
        )
    finally:
        _shutdown(client)

    assert response is None
    assert posted == []


def test_release_notification_scheduler_waits_before_first_tick(monkeypatch) -> None:
    calls: list[str] = []

    class StopBeforeTick:
        timeout: float | None = None

        def wait(self, timeout: float) -> bool:
            self.timeout = timeout
            return True

    def fake_tick(_settings):
        calls.append("tick")

    stop_event = StopBeforeTick()
    monkeypatch.setattr(
        notifications_module,
        "poll_wud_api_release_notifications",
        fake_tick,
    )
    notifications_module._release_notification_scheduler_loop(
        SimpleNamespace(),
        stop_event,
    )

    assert calls == []
    assert stop_event.timeout == notifications_module.RELEASE_NOTIFICATION_POLL_SECONDS


def test_release_notification_scheduler_logs_unexpected_tick_failure(
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []

    class StopAfterTick:
        waits = 0
        timeout: float | None = None

        def wait(self, timeout: float) -> bool:
            self.timeout = timeout
            self.waits += 1
            return self.waits > 1

    def fail_tick(_settings):
        calls.append("tick")
        raise RuntimeError("tick failed")

    stop_event = StopAfterTick()
    monkeypatch.setattr(
        notifications_module,
        "poll_wud_api_release_notifications",
        fail_tick,
    )

    with caplog.at_level(logging.ERROR, logger=notifications_module.LOGGER.name):
        notifications_module._release_notification_scheduler_loop(
            SimpleNamespace(),
            stop_event,
        )

    assert calls == ["tick"]
    assert stop_event.timeout == notifications_module.RELEASE_NOTIFICATION_POLL_SECONDS
    assert "release notification scheduler tick failed" in caplog.text


def test_release_notification_scheduler_starts_only_with_mutations(
    tmp_path: Path,
) -> None:
    read_only = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    mutating_root = tmp_path / "mutating"
    mutating_root.mkdir()
    mutating = _client(
        mutating_root,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    try:
        assert read_only.app.state.web_release_notification_thread is None
        assert mutating.app.state.web_release_notification_thread is not None
    finally:
        _shutdown(read_only)
        _shutdown(mutating)


def test_release_notification_scheduler_start_reuses_live_thread(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    try:
        settings = client.app.state.web_settings
        initial = client.app.state.web_release_notification_thread
        first = notifications_module.start_release_notification_scheduler(
            client.app,
            settings,
        )
        second = notifications_module.start_release_notification_scheduler(
            client.app,
            settings,
        )

        assert initial is not None
        assert initial.is_alive()
        assert first is initial
        assert second is initial
        assert first is client.app.state.web_release_notification_thread
        assert {thread for thread in (initial, first, second) if thread.is_alive()} == {
            initial
        }
    finally:
        _shutdown(client)
