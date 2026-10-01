from __future__ import annotations

from pathlib import Path

import pytest

from wudup import web_pending_sources, web_wud_api, web_wud_refresh
from wudup.config import UpdaterConfig
from wudup.web_models import WebSettings
from wudup.wud_file import parse_wud_text


def _settings(tmp_path: Path) -> WebSettings:
    root = tmp_path / "state"
    return WebSettings(
        config=UpdaterConfig(
            docker_base=tmp_path / "docker",
            wud_out_file=root / "images.todo",
            log_dir=root / "logs",
            db_path=root / "wud.sqlite",
            update_mode="stop",
            max_wait=180,
            lock_timeout=30,
            timezone_name="UTC",
            compose_ignore_paths=(),
            digest_pin_updates=False,
            out_uid=None,
            out_gid=None,
        ),
        auth_token="",
    )


def _pending_source_result() -> web_pending_sources.PendingSourceResult:
    return web_pending_sources.PendingSourceResult(
        configured="file",
        active="file",
        label="Pending file",
        source_file="test",
        exists=True,
        parsed=parse_wud_text(""),
        text="",
        source_hash="empty",
    )


def _watch_result() -> web_wud_api.WudApiWatchResult:
    return web_wud_api.WudApiWatchResult(
        snapshot=web_wud_api.WudApiSnapshot(
            status=web_wud_api.WudApiStatus(
                state="ready",
                available=True,
                metadata_available=True,
                last_checked_at="2026-01-01T00:00:00+00:00",
            )
        ),
        watched=True,
    )


def test_refresh_wud_pending_source_watches_and_resolves_with_same_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    source = _pending_source_result()
    watch_result = _watch_result()
    observed_watch: list[WebSettings] = []
    observed_resolve: list[tuple[WebSettings, bool, bool]] = []

    def fake_watch_all(active: WebSettings) -> web_wud_api.WudApiWatchResult:
        observed_watch.append(active)
        return watch_result

    def fake_resolve_pending_source(
        active: WebSettings,
        *,
        include_wud_metadata: bool = False,
        force_api: bool = False,
    ) -> web_pending_sources.PendingSourceResult:
        observed_resolve.append((active, include_wud_metadata, force_api))
        return source

    monkeypatch.setattr(web_wud_refresh.web_wud_api, "watch_all", fake_watch_all)
    monkeypatch.setattr(
        web_wud_refresh.web_pending_sources,
        "resolve_pending_source",
        fake_resolve_pending_source,
    )

    result = web_wud_refresh.refresh_wud_pending_source(settings, watch_all=True)

    assert isinstance(result, web_wud_refresh.WudPendingRefresh)
    assert result.source is source
    assert result.watch_result is watch_result
    assert observed_watch == [settings]
    assert observed_resolve == [(settings, True, False)]


@pytest.mark.parametrize(
    ("watch_all", "force"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_refresh_wud_pending_source_passes_force_for_watch_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    watch_all: bool,
    force: bool,
) -> None:
    source = _pending_source_result()
    watch_result = _watch_result()
    observed_force: list[bool] = []
    watch_calls = 0

    def fake_watch_all(_settings: WebSettings) -> web_wud_api.WudApiWatchResult:
        nonlocal watch_calls
        watch_calls += 1
        return watch_result

    def fake_resolve_pending_source(
        _settings: WebSettings,
        *,
        include_wud_metadata: bool = False,
        force_api: bool = False,
    ) -> web_pending_sources.PendingSourceResult:
        observed_force.append(force_api)
        return source

    monkeypatch.setattr(web_wud_refresh.web_wud_api, "watch_all", fake_watch_all)
    monkeypatch.setattr(
        web_wud_refresh.web_pending_sources,
        "resolve_pending_source",
        fake_resolve_pending_source,
    )

    result = web_wud_refresh.refresh_wud_pending_source(
        _settings(tmp_path),
        include_wud_metadata=False,
        force=force,
        watch_all=watch_all,
    )

    assert isinstance(result, web_wud_refresh.WudPendingRefresh)
    assert result.source is source
    assert result.watch_result is (watch_result if watch_all else None)
    assert observed_force == [force]
    assert watch_calls == int(watch_all)
