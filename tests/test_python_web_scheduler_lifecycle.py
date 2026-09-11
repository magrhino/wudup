from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

from tests.web_scheduler_test_helpers import _auto_update_tick
from tests.web_test_helpers import (
    _client,
)

from wudup import web_scheduler
from wudup.db import open_db


def test_auto_update_scheduler_loop_runs_initial_tick_before_wait(
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []

    class StopAfterInitialTick:
        timeout: float | None = None

        def wait(self, timeout: float) -> bool:
            self.timeout = timeout
            return True

    def fail_tick(*_args, **_kwargs):
        calls.append("tick")
        raise RuntimeError("tick failed")

    stop_event = StopAfterInitialTick()
    monkeypatch.setattr(web_scheduler, "_auto_update_tick", fail_tick)

    with caplog.at_level(logging.ERROR, logger=web_scheduler.LOGGER.name):
        web_scheduler._auto_update_scheduler_loop(
            SimpleNamespace(),
            SimpleNamespace(),
            stop_event,
            lambda _settings: None,
        )

    assert calls == ["tick"]
    assert stop_event.timeout == web_scheduler.AUTO_UPDATE_POLL_SECONDS
    assert "auto update scheduler tick failed" in caplog.text


def test_auto_update_scheduler_does_not_start_without_mutations(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = _auto_update_tick(client, datetime(2026, 5, 30, 14, 30, tzinfo=timezone.utc))

    assert client.app.state.web_auto_update_thread is None
    assert response is None


def test_auto_update_scheduler_start_initializes_database(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    try:
        with open_db(tmp_path / "state" / "wud.sqlite") as conn:
            row = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'web_settings'
                """
            ).fetchone()
    finally:
        web_scheduler.shutdown_auto_update_scheduler_state(client.app.state)

    assert row is not None


def test_auto_update_scheduler_start_refuses_duplicate_thread(tmp_path: Path) -> None:
    release_existing = Event()
    existing = Thread(target=release_existing.wait, daemon=True)
    existing.start()
    app = SimpleNamespace(
        state=SimpleNamespace(
            web_auto_update_thread=existing,
            web_auto_update_stop=Event(),
        )
    )
    settings = SimpleNamespace(
        mutations_enabled=True,
        config=SimpleNamespace(db_path=tmp_path / "state" / "wud.sqlite"),
    )
    try:
        thread = web_scheduler.start_auto_update_scheduler(
            app,
            settings,
            effective_config_loader=lambda _settings: None,
        )
    finally:
        release_existing.set()
        existing.join(timeout=1.0)

    assert thread is existing
    assert app.state.web_auto_update_thread is existing


def test_auto_update_scheduler_start_replaces_stopped_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    old_stop = Event()
    old_stop.set()
    app = SimpleNamespace(
        state=SimpleNamespace(
            web_auto_update_thread=None,
            web_auto_update_stop=old_stop,
        )
    )
    settings = SimpleNamespace(
        mutations_enabled=True,
        config=SimpleNamespace(db_path=tmp_path / "state" / "wud.sqlite", out_uid=None),
    )
    observed_stop_states: list[bool] = []

    def fake_loop(_app, _settings, stop_event, _effective_config_loader):
        observed_stop_states.append(stop_event.is_set())

    monkeypatch.setattr(web_scheduler, "_auto_update_scheduler_loop", fake_loop)

    thread = web_scheduler.start_auto_update_scheduler(
        app,
        settings,
        effective_config_loader=lambda _settings: None,
    )

    assert thread is not None
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert observed_stop_states == [False]
    assert app.state.web_auto_update_stop is not old_stop
    assert app.state.web_auto_update_thread is thread
