from __future__ import annotations

import base64
import json
import logging
import sqlite3
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event, local
from types import SimpleNamespace
from typing import Any

import pytest
from tests.web_test_helpers import (
    WUD_API_ACCESS_KEY_ID,
    WUD_API_AUTHORIZATION_HEADER,
    WUD_API_SECRET_ACCESS_KEY,
    _client,
    _csrf_headers,
    _install_wud_api,
    _web_env,
    _wud_image_payload,
)

from wudup import (
    web as web_module,
)
from wudup import (
    web_jobs,
    web_release_notifications,
    web_retags,
    web_scheduler,
    web_security,
    web_wud_api,
    web_wud_observation_store,
    web_wud_observations,
    web_wud_transport,
)
from wudup import (
    web_release_notes as release_notes_module,
)
from wudup.config import ConfigError
from wudup.release_notes import (
    ReleaseNoteInfo as ReleaseNoteData,
)
from wudup.release_notes import (
    release_note_contexts,
)
from wudup.web import create_app, load_web_settings
from wudup.web_auth import WebConfigError


def _settings(
    tmp_path: Path,
    base_url: str,
    env: dict[str, str] | None = None,
):
    values = {"WUD_API_BASE_URL": base_url}
    if env:
        values.update(env)
    return load_web_settings(
        environ=_web_env(tmp_path, values),
    )


def _persisted_observations(
    settings: Any,
) -> tuple[web_wud_observation_store.StoredPendingObservation, ...]:
    return web_wud_observation_store.load_pending_observations(
        settings.config.db_path,
        source=web_wud_observation_store.source_key(settings.wud_api_base_url),
    )


def _container_payload(
    *,
    name: str = "app",
    image: str = "registry.example/acme/app",
    tag: str = "1.0.0",
    remote_tag: str = "1.1.0",
    result_digest: str = "sha256:remote",
    update_kind: str = "tag",
    local_value: str | None = None,
    remote_value: str | None = None,
    source: str = "https://github.com/acme/app",
    link: str = "https://github.com/acme/app/releases/tag/v1.1.0",
    update_available: bool = True,
    platform: dict[str, str] | None = None,
    registry_url: str = "",
) -> dict[str, Any]:
    image_payload = _wud_image_payload(
        image=image,
        tag=tag,
        digest="sha256:local",
        registry_url=registry_url,
        platform=platform,
    )
    return {
        "id": f"docker.local.{name}",
        "name": name,
        "displayName": name.title(),
        "status": "running",
        "watcher": "local",
        "image": image_payload,
        "result": {
            "tag": remote_tag,
            "digest": result_digest,
            "link": link,
        },
        "updateKind": {
            "kind": update_kind,
            "localValue": tag if local_value is None else local_value,
            "remoteValue": remote_tag if remote_value is None else remote_value,
            "semverDiff": "minor",
        },
        "labels": {
            "org.opencontainers.image.source": source,
        },
        "error": {"message": ""},
        "updateAvailable": update_available,
    }


def _production_degraded_payload(
    *,
    name: str = "bazarr",
    image: str = "ghcr.io/linuxserver/bazarr",
    image_id: str = "sha256:stable-image",
    error: str = "Request failed with status code 429",
) -> dict[str, Any]:
    payload = _container_payload(
        name=name,
        image=image,
        update_available=False,
        platform={"os": "linux", "architecture": "amd64"},
    )
    image_payload = payload["image"]
    assert isinstance(image_payload, dict)
    image_payload["id"] = image_id
    image_payload["digest"] = {
        "repo": "sha256:repo-digest",
        "watch": "sha256:watch-digest",
    }
    payload["result"] = None
    payload["updateKind"] = {
        "kind": "unknown",
        "localValue": None,
        "remoteValue": None,
        "semverDiff": None,
    }
    payload["error"] = {"message": error}
    return payload


def test_wud_api_persisted_container_round_trips_without_transient_fields() -> None:
    container = web_wud_api.WudApiContainer(
        id="docker.local.app",
        name="app",
        display_name="Application",
        status="running",
        watcher="local",
        image="registry.example/acme/app:1.0.0",
        local_tag="1.0.0",
        local_digest="sha256:local",
        remote_tag="1.1.0",
        remote_digest="sha256:remote",
        update_kind="tag",
        semver_diff="minor",
        link="https://github.com/acme/app/releases/tag/v1.1.0",
        error="transient registry error",
        labels={"org.opencontainers.image.source": "https://github.com/acme/app"},
        platform=web_wud_api.ImagePlatform("linux", "amd64"),
        local_image_id="sha256:image",
    )

    stored = web_wud_observations._stored_observation(container)
    restored = web_wud_observations._container_from_stored_observation(stored)

    assert stored.keys() == web_wud_observations._PERSISTED_WUD_API_CONTAINER_FIELDS
    assert restored == replace(container, error="", labels={})


class _ToggleableWudApi:
    def __init__(self, monkeypatch, *, reachable: bool) -> None:
        self.now = 0.0
        self.reachable = reachable
        self.calls: list[str] = []
        monkeypatch.setattr(web_wud_api.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(web_wud_api, "_request_json", self.request_json)

    def request_json(self, url: str, _client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        self.calls.append(path)
        if not self.reachable:
            raise OSError("connection refused")
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            return [_container_payload(name="app")]
        raise AssertionError(f"unexpected WUD API URL: {url}")


def test_wud_api_reconciliation_keeps_earlier_snapshots_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = _settings(tmp_path, "https://wud.snapshot-lifetime.test:3000")
    healthy = _container_payload()
    _install_wud_api(monkeypatch, containers=[healthy])
    fresh = web_wud_api.get_snapshot(settings, include_containers=True, force=True)
    degraded = {**healthy, "error": "HTTP 429", "result": {}}
    _install_wud_api(monkeypatch, containers=[degraded])
    retained = web_wud_api.get_snapshot(settings, include_containers=True, force=True)
    _install_wud_api(monkeypatch, containers=[])
    empty = web_wud_api.get_snapshot(settings, include_containers=True, force=True)

    assert empty.containers == ()
    assert fresh.containers[0].metadata_status == "fresh"
    assert fresh.containers[0].error == ""
    assert fresh.containers[0].remote_tag == "1.1.0"
    assert retained.containers[0].metadata_status == "retained"
    assert retained.containers[0].remote_tag == "1.1.0"
    assert retained.retained_update_count == 1
    assert retained.observation_diagnostics[0].outcome == "retained"


def test_wud_api_snapshot_reads_update_metadata(tmp_path: Path, monkeypatch) -> None:
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(),
                _container_payload(
                    name="already-current",
                    update_available=False,
                    remote_value="1.0.0",
                ),
            ],
        ),
    )

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.test:3000"),
        include_containers=True,
        force=True,
    )

    assert snapshot.status.state == "ready"
    assert snapshot.status.available is True
    assert snapshot.status.metadata_available is True
    assert len(snapshot.containers) == 1
    container = snapshot.containers[0]
    assert container.name == "app"
    assert container.image == "registry.example/acme/app:1.0.0"
    assert container.remote_tag == "1.1.0"
    assert container.remote_digest == "sha256:remote"
    assert container.update_kind == "tag"
    assert container.semver_diff == "minor"
    assert snapshot.hidden_update_candidates == ()


def test_wud_api_snapshot_tracks_only_retryable_degraded_container_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    unsupported = _production_degraded_payload(
        name="socket-proxy",
        image="lscr.io/linuxserver/socket-proxy",
        error="Unsupported Registry unknown",
    )
    _install_wud_api(
        monkeypatch,
        containers=[
            _container_payload(name="app"),
            _production_degraded_payload(),
            unsupported,
        ],
    )

    settings = _settings(
        tmp_path,
        "https://wud.retryable-degraded.test:3000",
        {"WUDUP_LEGACY_SCRIPTS": "false"},
    )
    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert snapshot.retryable_degraded_container_ids == (
        "docker.local.bazarr",
    )
    assert snapshot.degraded_container_count == 1
    assert snapshot.retained_update_count == 0
    assert snapshot.recovered_update_count == 0
    assert "Update status is unknown for 1 container" in snapshot.status.detail
    assert "WUD skipped 1 container" in snapshot.status.detail
    diagnostics = web_wud_api.get_observation_diagnostics(settings)
    assert diagnostics.counts.model_dump() == {
        "available": 1,
        "degraded": 1,
        "retained": 0,
        "recovered": 0,
        "unresolved": 1,
        "unsupported_ignored": 1,
    }
    assert [item.model_dump() for item in diagnostics.items] == [
        {
            "outcome": "unresolved",
            "reason_code": "reported_error",
            "container_id": "docker.local.bazarr",
            "name": "bazarr",
            "image": "ghcr.io/linuxserver/bazarr:1.0.0",
            "registry": "ghcr.io",
            "watcher": "local",
            "update_available": False,
            "usable_result": False,
            "retryable": True,
            "error": (
                "The registry rate-limited the last WUD update check "
                "(HTTP 429: too many requests). Wait before rescanning; "
                "a successful check clears this error."
            ),
        },
        {
            "outcome": "unsupported_ignored",
            "reason_code": "unsupported_registry",
            "container_id": "docker.local.socket-proxy",
            "name": "socket-proxy",
            "image": "lscr.io/linuxserver/socket-proxy:1.0.0",
            "registry": "lscr.io",
            "watcher": "local",
            "update_available": False,
            "usable_result": False,
            "retryable": False,
            "error": "Unsupported registry",
        },
    ]


def test_wud_api_observation_diagnostics_classify_malformed_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_image = {
        "id": "docker.local.missing-image",
        "name": "missing-image",
        "watcher": "local",
        "image": {},
        "updateAvailable": False,
        "rawOnlyMarker": "must-not-leak",
    }
    invalid_flag = _container_payload(name="invalid-flag")
    invalid_flag["updateAvailable"] = "true"
    missing_result = _container_payload(
        name="missing-result",
        update_available=False,
    )
    missing_result["result"] = None
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            ["not-an-object", missing_image, invalid_flag, missing_result],
        ),
    )
    settings = _settings(
        tmp_path,
        "https://wud.observation-reasons.test:3000",
        {"WUDUP_LEGACY_SCRIPTS": "false"},
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert snapshot.degraded_container_count == 4
    assert [item.reason_code for item in snapshot.observation_diagnostics] == [
        "malformed_observation",
        "missing_image",
        "invalid_update_flag",
        "missing_scan_result",
    ]
    assert all(
        item.outcome == "unresolved"
        for item in snapshot.observation_diagnostics
    )
    assert [item.retryable for item in snapshot.observation_diagnostics] == [
        False,
        False,
        True,
        True,
    ]
    assert "must-not-leak" not in json.dumps(
        [item.model_dump() for item in snapshot.observation_diagnostics]
    )


def test_wud_api_retains_unique_last_good_when_current_digest_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    healthy = _container_payload(
        name="bazarr",
        image="ghcr.io/linuxserver/bazarr",
        platform={"os": "linux", "architecture": "amd64"},
    )
    healthy_image = healthy["image"]
    assert isinstance(healthy_image, dict)
    healthy_image["id"] = "sha256:stable-image"
    containers = [healthy]
    _install_wud_api(monkeypatch, containers=containers)
    settings = _settings(
        tmp_path,
        "https://wud.missing-current-digest.test:3000",
        {"WUDUP_LEGACY_SCRIPTS": "false"},
    )

    web_wud_api.get_snapshot(settings, include_containers=True, force=True)
    containers[:] = [_production_degraded_payload()]
    degraded = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert degraded.retained_update_count == 1
    assert degraded.containers[0].remote_tag == "1.1.0"
    assert degraded.containers[0].metadata_status == "retained"
    assert degraded.retryable_degraded_container_ids == (
        "docker.local.bazarr",
    )
    cache_key = web_wud_api._cache_key(settings, settings.wud_api_base_url)
    cached_identities = tuple(web_wud_api._pending_observation_cache[cache_key])
    assert len(cached_identities) == 1
    assert cached_identities[0][5] == "sha256:local"


@pytest.mark.parametrize(
    ("case", "image_id", "digest"),
    [
        ("image", "sha256:replacement-image", None),
        (
            "digest",
            "sha256:stable-image",
            {"value": "sha256:replacement-digest"},
        ),
    ],
)
def test_wud_api_does_not_retain_last_good_across_changed_local_identity(
    tmp_path: Path,
    monkeypatch,
    case: str,
    image_id: str,
    digest: object,
) -> None:
    healthy = _container_payload(
        name="bazarr",
        image="ghcr.io/linuxserver/bazarr",
        platform={"os": "linux", "architecture": "amd64"},
    )
    healthy_image = healthy["image"]
    assert isinstance(healthy_image, dict)
    healthy_image["id"] = "sha256:stable-image"
    containers = [healthy]
    _install_wud_api(monkeypatch, containers=containers)
    settings = _settings(
        tmp_path,
        f"https://wud.changed-local-identity-{case}.test:3000",
        {"WUDUP_LEGACY_SCRIPTS": "false"},
    )
    web_wud_api.get_snapshot(settings, include_containers=True, force=True)

    current = _production_degraded_payload(image_id=image_id)
    current_image = current["image"]
    assert isinstance(current_image, dict)
    if digest is not None:
        current_image["digest"] = digest
    containers[:] = [current]
    degraded = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert degraded.containers == ()
    assert degraded.retained_update_count == 0


def test_wud_api_forced_refreshes_serialize_last_good_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = local()
    first_waiting = Event()
    release_first = Event()
    second_requested = Event()
    calls: list[str] = []

    def request_json(url: str, _client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(path)
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            if clock.name == "first":
                first_waiting.set()
                assert release_first.wait(timeout=5)
                return [_container_payload(name="app")]
            second_requested.set()
            degraded = _container_payload(name="app", update_available=False)
            degraded["result"] = None
            degraded["error"] = {"message": "registry lookup failed"}
            return [degraded]
        raise AssertionError(f"unexpected WUD API URL: {url}")

    def refresh(name: str) -> web_wud_api.WudApiSnapshot:
        clock.name = name
        return web_wud_api.get_snapshot(
            settings,
            include_containers=True,
            force=True,
        )

    monkeypatch.setattr(web_wud_api, "_request_json", request_json)
    settings = _settings(tmp_path, "https://wud.concurrent-refresh.test:3000")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(refresh, "first")
        assert first_waiting.wait(timeout=5)
        second_future = executor.submit(refresh, "second")
        assert not second_requested.wait(timeout=0.1)
        release_first.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first.containers[0].remote_tag == "1.1.0"
    assert second.containers[0].remote_tag == "1.1.0"
    assert second.containers[0].error == "registry lookup failed"
    assert second.retained_update_count == 1
    assert calls.count("/api/containers") == 2


def test_wud_api_pending_observation_survives_process_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    containers = [_container_payload(name="app")]
    _install_wud_api(monkeypatch, containers=containers)
    settings = _settings(tmp_path, "https://wud.restart-cache.test:3000")
    settings.config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.config.db_path)):
        pass
    monkeypatch.setattr(web_wud_api, "_snapshot_cache", {})
    monkeypatch.setattr(web_wud_api, "_pending_observation_cache", {})

    web_wud_api.initialize_pending_observation_cache(settings)
    ready = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert ready.retained_update_count == 0
    assert _persisted_observations(settings) == ()

    web_wud_api.checkpoint_pending_observation_cache(settings)
    assert len(_persisted_observations(settings)) == 1

    web_wud_api._snapshot_cache.clear()
    web_wud_api._pending_observation_cache.clear()
    degraded = _container_payload(name="app", update_available=False)
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    containers[:] = [degraded]

    web_wud_api.initialize_pending_observation_cache(settings)
    restored = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert len(restored.containers) == 1
    assert restored.containers[0].name == "app"
    assert restored.containers[0].remote_tag == "1.1.0"
    assert restored.containers[0].error == "registry lookup failed"
    assert restored.degraded_container_count == 1
    assert restored.retained_update_count == 1

    containers[:] = [
        _container_payload(
            name="app",
            update_available=False,
            remote_tag="1.0.0",
            update_kind="unknown",
            remote_value="1.0.0",
        )
    ]
    authoritative = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert authoritative.containers == ()
    assert authoritative.degraded_container_count == 0
    web_wud_api.checkpoint_pending_observation_cache(settings)

    web_wud_api._snapshot_cache.clear()
    web_wud_api._pending_observation_cache.clear()
    containers[:] = [degraded]
    web_wud_api.initialize_pending_observation_cache(settings)
    cleared = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert cleared.containers == ()
    assert cleared.degraded_container_count == 1
    assert cleared.retained_update_count == 0


def test_wud_api_persisted_observation_does_not_cross_container_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    initial = _container_payload(name="app")
    initial_image = initial["image"]
    assert isinstance(initial_image, dict)
    initial_image["id"] = "sha256:original"
    containers = [initial]
    _install_wud_api(monkeypatch, containers=containers)
    settings = _settings(tmp_path, "https://wud.restart-identity.test:3000")
    settings.config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.config.db_path)):
        pass
    monkeypatch.setattr(web_wud_api, "_snapshot_cache", {})
    monkeypatch.setattr(web_wud_api, "_pending_observation_cache", {})

    web_wud_api.initialize_pending_observation_cache(settings)
    web_wud_api.get_snapshot(settings, include_containers=True, force=True)
    web_wud_api.checkpoint_pending_observation_cache(settings)

    web_wud_api._snapshot_cache.clear()
    web_wud_api._pending_observation_cache.clear()
    replacement = _container_payload(name="app", update_available=False)
    replacement_image = replacement["image"]
    assert isinstance(replacement_image, dict)
    replacement_image["id"] = "sha256:replacement"
    replacement["result"] = None
    replacement["error"] = {"message": "registry lookup failed"}
    containers[:] = [replacement]

    web_wud_api.initialize_pending_observation_cache(settings)
    degraded = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert degraded.containers == ()
    assert degraded.degraded_container_count == 1
    assert degraded.retained_update_count == 0


def test_wud_api_authenticated_client_does_not_load_persisted_observations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    containers = [_container_payload(name="app")]
    _install_wud_api(monkeypatch, containers=containers)
    base_url = "https://wud.authenticated-cache.test:3000"
    unauthenticated = _settings(tmp_path, base_url)
    unauthenticated.config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(unauthenticated.config.db_path)):
        pass
    monkeypatch.setattr(web_wud_api, "_snapshot_cache", {})
    monkeypatch.setattr(web_wud_api, "_pending_observation_cache", {})

    web_wud_api.initialize_pending_observation_cache(unauthenticated)
    web_wud_api.get_snapshot(
        unauthenticated,
        include_containers=True,
        force=True,
    )
    web_wud_api.checkpoint_pending_observation_cache(unauthenticated)

    web_wud_api._snapshot_cache.clear()
    web_wud_api._pending_observation_cache.clear()
    degraded = _container_payload(name="app", update_available=False)
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    containers[:] = [degraded]
    authenticated = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "new-principal-secret"},
    )

    web_wud_api.initialize_pending_observation_cache(authenticated)
    snapshot = web_wud_api.get_snapshot(
        authenticated,
        include_containers=True,
        force=True,
    )

    assert _persisted_observations(authenticated) == ()
    assert snapshot.containers == ()
    assert snapshot.degraded_container_count == 1
    assert snapshot.retained_update_count == 0


def test_web_app_wires_pending_observation_lifecycle_and_contains_checkpoint_failure(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls: list[str] = []
    settings = _settings(
        tmp_path,
        "https://wud.lifecycle.test:3000",
        {"WUD_WEB_DEV_NO_AUTH": "true"},
    )
    monkeypatch.setattr(
        web_wud_api,
        "initialize_pending_observation_cache",
        lambda active_settings: calls.append(
            f"load:{active_settings.wud_api_base_url}"
        ),
    )

    def fail_checkpoint(active_settings) -> None:
        calls.append(f"save:{active_settings.wud_api_base_url}")
        raise RuntimeError("checkpoint exploded")

    monkeypatch.setattr(
        web_wud_api,
        "checkpoint_pending_observation_cache",
        fail_checkpoint,
    )
    monkeypatch.setattr(web_wud_api, "startup_probe", lambda _settings: None)
    for module, attribute, name in (
        (
            web_release_notifications,
            "shutdown_release_notification_scheduler_state",
            "release-notifications",
        ),
        (web_scheduler, "shutdown_auto_update_scheduler_state", "scheduler"),
        (web_retags, "shutdown_retag_preview_state", "retag-preview"),
        (web_security, "shutdown_security_scan_state", "security-scan"),
        (web_jobs, "shutdown_apply_job_state", "apply-jobs"),
    ):
        monkeypatch.setattr(
            module,
            attribute,
            lambda _state, label=name: calls.append(f"stop:{label}"),
        )

    app = create_app(settings)
    shutdown = next(
        callback
        for callback in app.router.on_shutdown
        if callback.__name__ == "shutdown_apply_executor"
    )
    with caplog.at_level(logging.ERROR, logger=web_module.LOGGER.name):
        shutdown()

    assert calls == [
        "load:https://wud.lifecycle.test:3000",
        "stop:release-notifications",
        "stop:scheduler",
        "stop:retag-preview",
        "stop:security-scan",
        "stop:apply-jobs",
        "save:https://wud.lifecycle.test:3000",
    ]
    assert "WUD API pending observation checkpoint failed" in caplog.text
    assert "checkpoint exploded" not in caplog.text


def test_wud_api_checkpoint_waits_for_active_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    refresh_waiting = Event()
    release_refresh = Event()
    checkpoint_finished = Event()

    def request_json(url: str, _client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            refresh_waiting.set()
            assert release_refresh.wait(timeout=5)
            return [_container_payload(name="app")]
        raise AssertionError(f"unexpected WUD API URL: {url}")

    monkeypatch.setattr(web_wud_api, "_request_json", request_json)
    monkeypatch.setattr(web_wud_api, "_snapshot_cache", {})
    monkeypatch.setattr(web_wud_api, "_pending_observation_cache", {})
    settings = _settings(tmp_path, "https://wud.checkpoint-race.test:3000")
    settings.config.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.config.db_path)):
        pass
    web_wud_api.initialize_pending_observation_cache(settings)

    def checkpoint() -> None:
        web_wud_api.checkpoint_pending_observation_cache(settings)
        checkpoint_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh_future = executor.submit(
            web_wud_api.get_snapshot,
            settings,
            include_containers=True,
            force=True,
        )
        assert refresh_waiting.wait(timeout=5)
        checkpoint_future = executor.submit(checkpoint)
        assert not checkpoint_finished.wait(timeout=0.1)
        release_refresh.set()
        refreshed = refresh_future.result(timeout=5)
        checkpoint_future.result(timeout=5)

    assert refreshed.containers[0].name == "app"
    assert len(_persisted_observations(settings)) == 1


def test_wud_api_ignores_unsupported_registry_observation_with_pending_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    unsupported = _container_payload(
        name="socket-proxy",
        image="linuxserver/socket-proxy",
        update_available=False,
        update_kind="unknown",
    )
    unsupported["result"] = None
    unsupported["error"] = {"message": "Unsupported Registry unknown"}
    _install_wud_api(
        monkeypatch,
        containers=[_container_payload(name="app"), unsupported],
    )
    settings = _settings(tmp_path, "https://wud.unsupported.test:3000")
    settings.config.wud_out_file.write_text(
        "linuxserver/socket-proxy:1.0.0 tag=1.1.0\n",
        encoding="utf-8",
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert len(snapshot.containers) == 1
    assert snapshot.containers[0].name == "app"
    assert snapshot.degraded_container_count == 0
    assert snapshot.retained_update_count == 0
    assert snapshot.recovered_update_count == 0
    assert snapshot.status.detail == (
        "1 update is available. "
        "WUD skipped 1 container because its registry is unsupported."
    )


def test_wud_api_retains_prior_update_when_registry_becomes_unsupported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    containers = [_container_payload(name="app")]
    _install_wud_api(monkeypatch, containers=containers)
    settings = _settings(tmp_path, "https://wud.unsupported-retain.test:3000")

    ready = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert len(ready.containers) == 1

    unsupported = _container_payload(name="app", update_available=False)
    unsupported["result"] = None
    unsupported["error"] = {"message": "Unsupported Registry unknown"}
    containers[:] = [unsupported]

    degraded = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert len(degraded.containers) == 1
    assert degraded.containers[0].remote_tag == "1.1.0"
    assert degraded.degraded_container_count == 1
    assert degraded.retained_update_count == 1
    diagnostic = degraded.observation_diagnostics[0]
    assert diagnostic.outcome == "retained"
    assert diagnostic.reason_code == "unsupported_registry"
    assert diagnostic.retryable is False


def test_wud_api_recovers_cold_start_update_from_matching_pending_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_digest = f"sha256:{'a' * 64}"
    degraded = _container_payload(name="app", update_available=False)
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=[degraded])
    settings = _settings(tmp_path, "https://wud.pending-recovery.test:3000")
    settings.config.wud_out_file.write_text(
        "registry.example/acme/app:1.0.0 "
        f"tag=1.1.0 sha256={target_digest}\n",
        encoding="utf-8",
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert len(snapshot.containers) == 1
    recovered = snapshot.containers[0]
    assert recovered.id == "docker.local.app"
    assert recovered.remote_tag == "1.1.0"
    assert recovered.remote_digest == target_digest
    assert recovered.update_kind == "tag"
    assert recovered.error == "registry lookup failed"
    assert recovered.metadata_status == "recovered"
    assert snapshot.degraded_container_count == 1
    assert snapshot.retained_update_count == 0
    assert snapshot.recovered_update_count == 1
    assert snapshot.status.detail == (
        "1 update is available. "
        "The last WUD update check failed for 1 container. "
        "1 update was recovered from the pending file."
    )
    diagnostic = snapshot.observation_diagnostics[0]
    assert diagnostic.outcome == "recovered"
    assert diagnostic.reason_code == "reported_error"
    assert diagnostic.retryable is True


def test_wud_api_pending_file_recovery_requires_matching_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = _container_payload(
        name="app",
        image="registry-b.example/acme/app",
        update_available=False,
    )
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=[degraded])
    settings = _settings(tmp_path, "https://wud.pending-recovery-registry.test:3000")
    settings.config.wud_out_file.write_text(
        "registry-a.example/acme/app:1.0.0 tag=1.1.0\n",
        encoding="utf-8",
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert snapshot.containers == ()
    assert snapshot.recovered_update_count == 0


def test_wud_api_pending_file_recovery_treats_docker_hub_alias_as_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = _container_payload(
        name="app",
        image="acme/app",
        update_available=False,
    )
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=[degraded])
    settings = _settings(tmp_path, "https://wud.pending-recovery-docker-hub.test:3000")
    settings.config.wud_out_file.write_text(
        "docker.io/acme/app:1.0.0 tag=1.1.0\n",
        encoding="utf-8",
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert len(snapshot.containers) == 1
    assert snapshot.containers[0].remote_tag == "1.1.0"
    assert snapshot.recovered_update_count == 1


def test_wud_api_pending_file_recovery_ignores_entry_without_update_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = _container_payload(name="app", update_available=False)
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=[degraded])
    settings = _settings(tmp_path, "https://wud.pending-recovery-bare.test:3000")
    settings.config.wud_out_file.write_text(
        "registry.example/acme/app:1.0.0\n",
        encoding="utf-8",
    )

    snapshot = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )

    assert snapshot.containers == ()
    assert snapshot.recovered_update_count == 0


def test_wud_api_does_not_recover_applied_or_legacy_disabled_pending_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = _container_payload(name="app", update_available=False)
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=[degraded])

    applied_settings = _settings(
        tmp_path,
        "https://wud.pending-recovery-applied.test:3000",
    )
    applied_settings.config.wud_out_file.write_text(
        "registry.example/acme/app:1.0.0 tag=1.0.0\n",
        encoding="utf-8",
    )
    applied = web_wud_api.get_snapshot(
        applied_settings,
        include_containers=True,
        force=True,
    )

    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled_settings = _settings(
        disabled_root,
        "https://wud.pending-recovery-disabled.test:3000",
        {"WUDUP_LEGACY_SCRIPTS": "false"},
    )
    disabled_settings.config.wud_out_file.write_text(
        "registry.example/acme/app:1.0.0 tag=1.1.0\n",
        encoding="utf-8",
    )
    disabled = web_wud_api.get_snapshot(
        disabled_settings,
        include_containers=True,
        force=True,
    )

    assert applied.containers == ()
    assert applied.recovered_update_count == 0
    assert disabled.containers == ()
    assert disabled.recovered_update_count == 0


def test_wud_api_snapshot_reads_hidden_update_candidates_from_update_kind_delta(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(
                    name="snoozed",
                    update_available=False,
                    update_kind="digest",
                    local_value="sha256:local",
                    remote_value="sha256:remote",
                ),
                _container_payload(
                    name="unknown-kind",
                    update_available=False,
                    update_kind="unknown",
                    local_value="1.0.0",
                    remote_value="1.1.0",
                ),
                _container_payload(
                    name="same-tag",
                    update_available=False,
                    local_value="1.0.0",
                    remote_value="1.0.0",
                ),
            ],
        ),
    )

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.hidden-candidates.test:3000"),
        include_containers=True,
        force=True,
    )

    assert snapshot.containers == ()
    assert len(snapshot.hidden_update_candidates) == 1
    candidate = snapshot.hidden_update_candidates[0]
    assert candidate.name == "snoozed"
    assert candidate.image == "registry.example/acme/app:1.0.0"
    assert candidate.remote_tag == "1.1.0"
    assert candidate.remote_digest == "sha256:remote"
    assert candidate.update_kind == "digest"


def test_wud_api_snapshot_preserves_registry_url_for_unqualified_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(
                    name="dozzle",
                    image="amir20/dozzle",
                    tag="v10.6.6",
                    remote_tag="v10.6.7",
                    result_digest="",
                    registry_url="https://ghcr.io",
                ),
                _container_payload(
                    name="explicit",
                    image="ghcr.io/acme/app",
                    registry_url="https://ghcr.io",
                ),
                _container_payload(
                    name="hub",
                    image="library/nginx",
                    registry_url="https://index.docker.io/v1/",
                ),
                _container_payload(
                    name="digest",
                    image="amir20/dozzle@sha256:local",
                    tag="",
                    remote_tag="",
                    result_digest="sha256:remote",
                    update_kind="digest",
                    local_value="sha256:local",
                    remote_value="sha256:remote",
                    registry_url="https://ghcr.io",
                ),
            ],
        ),
    )

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.registry-url.test:3000"),
        include_containers=True,
        force=True,
    )

    images = {container.name: container.image for container in snapshot.containers}
    assert images["dozzle"] == "ghcr.io/amir20/dozzle:v10.6.6"
    assert images["explicit"] == "ghcr.io/acme/app:1.0.0"
    assert images["hub"] == "library/nginx:1.0.0"
    assert images["digest"] == "ghcr.io/amir20/dozzle@sha256:local"


def test_wud_api_snapshot_reads_tag_digest_from_remote_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    digest = f"sha256:{'b' * 64}"
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(
                    result_digest="",
                    remote_value=f"registry.example/acme/app:1.1.0@{digest}",
                ),
            ],
        ),
    )

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.tag-digest.test:3000"),
        include_containers=True,
        force=True,
    )

    assert len(snapshot.containers) == 1
    assert snapshot.containers[0].remote_tag == "1.1.0"
    assert snapshot.containers[0].remote_digest == digest


def test_wud_api_watch_uses_longer_timeout_than_metadata_reads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, str, float]] = []

    def response(payload: object):
        return nullcontext(
            SimpleNamespace(read=lambda: json.dumps(payload).encode("utf-8"))
        )

    def urlopen(request, *, timeout: float):
        path = urllib.parse.urlsplit(request.get_full_url()).path
        calls.append((request.get_method(), path, timeout))
        if path == "/health":
            return response({"status": "ok"})
        if path == "/api/containers/watch":
            return response({"status": "ok"})
        if path == "/api/containers":
            return response([_container_payload()])
        raise AssertionError(f"unexpected WUD API URL: {request.get_full_url()}")

    monkeypatch.setattr(web_wud_transport.urllib.request, "urlopen", urlopen)

    watch = web_wud_api.watch_all(_settings(tmp_path, "https://wud.timeout.test:3000"))

    assert watch.watched is True
    assert (
        "POST",
        "/api/containers/watch",
        web_wud_api.WUD_API_WATCH_TIMEOUT_SECONDS,
    ) in calls
    assert (
        "GET",
        "/api/containers",
        web_wud_api.WUD_API_TIMEOUT_SECONDS,
    ) in calls
    assert {
        timeout
        for _method, path, timeout in calls
        if path == "/health"
    } == {web_wud_api.WUD_API_TIMEOUT_SECONDS}


def test_wud_api_container_watch_has_one_batch_timeout_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    containers = [
        _container_payload(name="one"),
        _container_payload(name="two"),
        _container_payload(name="three"),
    ]
    _install_wud_api(monkeypatch, containers=(200, containers))
    clock = [0.0]
    calls: list[tuple[str, float]] = []

    def post_json(url: str, _client_config=None, *, timeout: float) -> object:
        calls.append((urllib.parse.urlsplit(url).path, timeout))
        clock[0] += 3.0
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(web_wud_api, "_post_json", post_json)
    monkeypatch.setattr(web_wud_api, "WUD_API_WATCH_BATCH_TIMEOUT_SECONDS", 5.0)

    watch = web_wud_api.watch_containers(
        _settings(tmp_path, "https://wud.batch-timeout.test:3000"),
        (
            "docker.local.one",
            "docker.local.two",
            "docker.local.three",
        ),
    )

    assert watch.watched is False
    assert watch.requested_count == 3
    assert watch.watched_count == 2
    assert calls == [
        ("/api/containers/docker.local.one/watch", pytest.approx(5.0)),
        ("/api/containers/docker.local.two/watch", pytest.approx(2.0)),
    ]


def test_wud_api_watch_cooldown_prunes_expired_identities(monkeypatch) -> None:
    clock = [10.0]
    cache_key = ("https://wud.cooldown-prune.test:3000", "fingerprint")
    old_key = (cache_key, "docker.local.old")
    new_key = (cache_key, "docker.local.new")
    monkeypatch.setattr(web_wud_api, "_watch_rate_limit_until", {})
    monkeypatch.setattr(web_wud_api.time, "monotonic", lambda: clock[0])

    web_wud_api._start_watch_rate_limit_cooldown(cache_key, old_key[1])
    clock[0] += web_wud_api.WUD_API_RATE_LIMIT_COOLDOWN_SECONDS + 1
    web_wud_api._start_watch_rate_limit_cooldown(cache_key, new_key[1])

    assert old_key not in web_wud_api._watch_rate_limit_until
    assert new_key in web_wud_api._watch_rate_limit_until

    clock[0] += web_wud_api.WUD_API_RATE_LIMIT_COOLDOWN_SECONDS + 1
    assert (
        web_wud_api._watch_rate_limit_cooldown_remaining(
            cache_key,
            "docker.local.unrelated",
        )
        == 0.0
    )
    assert new_key not in web_wud_api._watch_rate_limit_until


def test_container_triggers_ignores_non_object_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def request_json(_url: str, _client_config=None) -> object:
        return [
            {
                "id": "discord.release",
                "type": "discord",
                "name": "release",
            },
            "not-a-trigger",
            None,
        ]

    monkeypatch.setattr(web_wud_api, "_request_json", request_json)

    triggers, warning = web_wud_api.container_triggers(
        _settings(tmp_path, "https://wud.triggers.test:3000"),
        "docker.local.app",
    )

    assert warning == ""
    assert [trigger.model_dump() for trigger in triggers] == [
        {"id": "discord.release", "type": "discord", "name": "release"}
    ]


def test_wud_api_configuration_diagnostics_reads_endpoint_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch)

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config.test:3000"),
        force=True,
    )

    assert diagnostics.health.state == "ready"
    assert diagnostics.app.status.state == "ready"
    assert diagnostics.app.name == "wud"
    assert diagnostics.app.version == "5.0.0"
    assert diagnostics.log.level == "debug"
    assert diagnostics.store.path == ".store"
    assert diagnostics.store.file == "wud.json"
    assert len(diagnostics.watchers) == 1
    assert diagnostics.watchers[0].id == "docker.local"
    assert diagnostics.watchers[0].cron == "0 * * * *"
    assert diagnostics.watchers[0].watch_by_default is True
    assert len(diagnostics.registries) == 1
    assert diagnostics.registries[0].id == "hub.private"


def test_wud_api_configuration_diagnostics_redacts_sensitive_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redaction_value = "registry-redaction-value"
    _install_wud_api(
        monkeypatch,
        watchers=(
            200,
            [
                {
                    "id": "docker.local",
                    "type": "docker",
                    "name": "local",
                    "configuration": {
                        "socket": "/var/run/docker.sock",
                        "headers": {WUD_API_AUTHORIZATION_HEADER: redaction_value},
                        "cron": "0 * * * *",
                        "watchbydefault": True,
                    },
                }
            ],
        ),
        registries=(
            200,
            [
                {
                    "id": "ecr.private",
                    "type": "ecr",
                    "name": "private",
                    "configuration": {
                        "region": "eu-west-1",
                        WUD_API_ACCESS_KEY_ID: "redaction-access-value",
                        WUD_API_SECRET_ACCESS_KEY: redaction_value,
                    },
                }
            ],
        ),
    )

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config-redaction.test:3000"),
        force=True,
    )
    serialized = diagnostics.model_dump_json()

    assert redaction_value not in serialized
    assert "redaction-access-value" not in serialized
    assert diagnostics.watchers[0].configuration["socket"] == "[REDACTED_PATH]"
    assert diagnostics.watchers[0].configuration["headers"] == "<redacted>"
    assert diagnostics.registries[0].configuration["region"] == "eu-west-1"
    assert diagnostics.registries[0].configuration[WUD_API_ACCESS_KEY_ID] == "<redacted>"
    assert (
        diagnostics.registries[0].configuration[WUD_API_SECRET_ACCESS_KEY]
        == "<redacted>"
    )


def test_wud_api_bearer_auth_applies_to_get_and_post_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bearer_value = "fixture-bearer-value"
    calls: list[tuple[str, str, dict[str, str]]] = []

    def request_json(url: str, client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(("GET", path, web_wud_transport._request_headers(client_config)))
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            return [_container_payload(name="app")]
        raise AssertionError(f"unexpected WUD API URL: {url}")

    def post_json(url: str, client_config=None, **_kwargs) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(("POST", path, web_wud_transport._request_headers(client_config)))
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_api, "_request_json", request_json)
    monkeypatch.setattr(web_wud_api, "_post_json", post_json)
    settings = _settings(
        tmp_path,
        "https://wud.auth-header.test:3000",
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: bearer_value},
    )

    snapshot = web_wud_api.get_snapshot(settings, include_containers=True, force=True)
    watch = web_wud_api.watch_all(settings)

    assert snapshot.status.state == "ready"
    assert watch.watched is True
    assert calls
    assert {
        (method, path)
        for method, path, _headers in calls
    } >= {
        ("GET", "/health"),
        ("GET", "/api/containers"),
        ("POST", "/api/containers/watch"),
    }
    for _method, _path, headers in calls:
        assert headers["Authorization"] == f"Bearer {bearer_value}"
        assert headers["Accept"] == "application/json"
        assert headers["User-Agent"] == web_wud_api.WUD_API_USER_AGENT


def test_wud_api_basic_auth_password_file_builds_authorization_header(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "wud-api-basic-password"
    password_file.write_text("basic-password-secret\n", encoding="utf-8")
    settings = _settings(
        tmp_path,
        "https://wud.basic-auth.test:3000",
        {
            web_wud_api.WUD_API_AUTH_BASIC_USER_ENV: "wud-user",
            web_wud_api.WUD_API_AUTH_BASIC_PASSWORD_FILE_ENV: str(password_file),
        },
    )
    expected_token = base64.b64encode(
        b"wud-user:basic-password-secret"
    ).decode("ascii")

    headers = web_wud_transport._request_headers(settings.wud_api_client)

    assert headers["Authorization"] == f"Basic {expected_token}"
    assert "basic-password-secret" in settings.wud_api_client.secret_values
    assert headers["Authorization"] in settings.wud_api_client.secret_values


def test_wud_api_static_json_headers_are_added_to_requests(tmp_path: Path) -> None:
    headers_file = tmp_path / "wud-api-headers.json"
    headers_file.write_text(
        json.dumps(
            {
                "X-Api-Key": "static-header-secret",
                "X-WUD-Trace": "enabled",
            }
        ),
        encoding="utf-8",
    )

    settings = _settings(
        tmp_path,
        "https://wud.static-headers.test:3000",
        {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(headers_file)},
    )
    headers = web_wud_transport._request_headers(settings.wud_api_client)

    assert headers["X-Api-Key"] == "static-header-secret"
    assert headers["X-WUD-Trace"] == "enabled"
    assert headers["Accept"] == "application/json"
    assert "static-header-secret" in settings.wud_api_client.secret_values


def test_wud_api_static_json_headers_file_read_error(tmp_path: Path) -> None:
    env = {
        web_wud_api.WUD_API_HEADERS_FILE_ENV: str(tmp_path / "missing-headers.json"),
    }

    with pytest.raises(WebConfigError) as excinfo:
        _settings(tmp_path, "https://wud.static-headers.test:3000", env)

    assert str(excinfo.value) == "WUD_API_HEADERS_FILE could not be read"


def test_wud_api_client_config_fingerprint_is_opaque_without_secret_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tokens = iter(("opaque-one", "opaque-two", "opaque-three"))
    monkeypatch.setattr(web_wud_transport.secrets, "token_hex", lambda _bytes: next(tokens))
    base_url = "https://wud.fingerprint.test:3000"
    first = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "same-token-secret"},
    )
    second = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "same-token-secret"},
    )
    third = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "other-token-secret"},
    )

    fingerprint = first.wud_api_client.fingerprint

    assert fingerprint == "opaque-one"
    assert second.wud_api_client.fingerprint == "opaque-two"
    assert third.wud_api_client.fingerprint == "opaque-three"
    assert "same-token-secret" not in fingerprint
    assert "Bearer" not in fingerprint


def test_wud_api_auth_rejected_state_mentions_configured_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=(401, {"error": "authentication required"}))

    snapshot = web_wud_api.get_snapshot(
        _settings(
            tmp_path,
            "https://wud.rejected-auth.test:3000",
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: (
                    "wud-api-rejected-secret"
                )
            },
        ),
        include_containers=True,
        force=True,
    )

    assert snapshot.status.state == "auth_required"
    assert snapshot.status.detail == "configured WUD API credentials were rejected"


def test_wud_api_snapshot_cache_is_separated_by_auth_headers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def request_json(url: str, client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        authorization = web_wud_transport._request_headers(client_config)["Authorization"]
        calls.append(f"{authorization} {path}")
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            name = "one" if authorization.endswith("one-token") else "two"
            return [_container_payload(name=name)]
        raise AssertionError(f"unexpected WUD API URL: {url}")

    monkeypatch.setattr(web_wud_api, "_request_json", request_json)
    base_url = "https://wud.auth-cache.test:3000"
    first_settings = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "one-token"},
    )
    second_settings = _settings(
        tmp_path,
        base_url,
        {web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "two-token"},
    )

    first = web_wud_api.get_snapshot(
        first_settings,
        include_containers=True,
        force=True,
    )
    second = web_wud_api.get_snapshot(second_settings, include_containers=True)

    assert first.containers[0].name == "one"
    assert second.containers[0].name == "two"
    assert calls == [
        "Bearer one-token /health",
        "Bearer one-token /api/containers",
        "Bearer two-token /health",
        "Bearer two-token /api/containers",
    ]


def test_wud_api_auth_config_values_are_redacted_from_details(tmp_path: Path) -> None:
    token_file = tmp_path / "wud-api-token"
    token_file.write_text("file-token-secret\n", encoding="utf-8")
    headers_file = tmp_path / "wud-api-headers.json"
    headers_file.write_text(
        json.dumps({"X-Api-Key": "static-header-secret"}),
        encoding="utf-8",
    )
    settings = _settings(
        tmp_path,
        "https://wud.redaction.test:3000",
        {
            web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV: str(token_file),
            web_wud_api.WUD_API_HEADERS_FILE_ENV: str(headers_file),
        },
    )

    detail = web_wud_transport._sanitize_detail(
        settings,
        "file-token-secret static-header-secret Bearer file-token-secret",
    )

    assert "file-token-secret" not in detail
    assert "static-header-secret" not in detail
    assert detail.count("<redacted>") == 3


def test_wud_api_auth_config_rejects_malformed_inputs(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token-secret\n", encoding="utf-8")
    empty_file = tmp_path / "empty"
    empty_file.write_text("\n", encoding="utf-8")
    bad_json_file = tmp_path / "headers-bad-json"
    bad_json_file.write_text("{not json", encoding="utf-8")
    unreadable_headers = tmp_path / "headers-unreadable"
    unreadable_headers.mkdir()
    empty_headers = tmp_path / "headers-empty"
    empty_headers.write_text("", encoding="utf-8")
    non_object_headers = tmp_path / "headers-list"
    non_object_headers.write_text("[]", encoding="utf-8")
    non_string_headers = tmp_path / "headers-non-string"
    non_string_headers.write_text(json.dumps({"X-Api-Key": 7}), encoding="utf-8")
    invalid_name_headers = tmp_path / "headers-invalid-name"
    invalid_name_headers.write_text(
        json.dumps({"X Invalid Header": "value"}),
        encoding="utf-8",
    )
    duplicate_headers = tmp_path / "headers-duplicate"
    duplicate_headers.write_text(
        json.dumps({"X-Api-Key": "one", "x-api-key": "two"}),
        encoding="utf-8",
    )
    newline_headers = tmp_path / "headers-newline"
    newline_headers.write_text(
        json.dumps({"X-Api-Key": "bad\nvalue"}),
        encoding="utf-8",
    )
    cr_newline_headers = tmp_path / "headers-cr-newline"
    cr_newline_headers.write_text(
        json.dumps({"X-Api-Key": "bad\r\nvalue"}),
        encoding="utf-8",
    )
    auth_headers = tmp_path / "headers-auth"
    auth_headers.write_text(
        json.dumps({"Authorization": "Bearer static-secret"}),
        encoding="utf-8",
    )

    cases = [
        (
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "direct-token",
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV: str(token_file),
            },
            "cannot both be set",
        ),
        (
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV: str(empty_file),
            },
            "must not be empty",
        ),
        (
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_FILE_ENV: str(
                    tmp_path / "missing-token"
                ),
            },
            "could not be read",
        ),
        (
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "direct-token",
                web_wud_api.WUD_API_AUTH_BASIC_USER_ENV: "wud-user",
                web_wud_api.WUD_API_AUTH_BASIC_PASSWORD_ENV: "basic-password",
            },
            "bearer and basic auth cannot both be configured",
        ),
        (
            {web_wud_api.WUD_API_AUTH_BASIC_USER_ENV: "wud-user"},
            "must be set together",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(bad_json_file)},
            "must contain a JSON object",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(unreadable_headers)},
            "could not be read",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(empty_headers)},
            "must contain a JSON object",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(non_object_headers)},
            "must contain a JSON object",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(invalid_name_headers)},
            "contains an invalid header",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(duplicate_headers)},
            "must not define duplicate headers",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(non_string_headers)},
            "values must be strings",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(newline_headers)},
            "must not contain newlines",
        ),
        (
            {web_wud_api.WUD_API_HEADERS_FILE_ENV: str(cr_newline_headers)},
            "must not contain newlines",
        ),
        (
            {
                web_wud_api.WUD_API_AUTH_BEARER_TOKEN_ENV: "direct-token",
                web_wud_api.WUD_API_HEADERS_FILE_ENV: str(auth_headers),
            },
            "must not define Authorization",
        ),
    ]

    for env, expected in cases:
        with pytest.raises(WebConfigError, match=expected):
            _settings(tmp_path, f"https://wud.invalid-{len(expected)}.test:3000", env)


def test_wud_api_configuration_diagnostics_reports_unreachable_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=OSError("connection refused"))

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config-unreachable.test:3000"),
        force=True,
    )

    assert diagnostics.health.state == "unavailable"
    assert diagnostics.app.status.state == "unavailable"
    assert diagnostics.watchers_status.state == "unavailable"
    assert diagnostics.watchers == []
    assert diagnostics.registries == []


def test_wud_api_configuration_diagnostics_reports_health_auth_required(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=(401, {"error": "authentication required"}))

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config-auth.test:3000"),
        force=True,
    )

    assert diagnostics.health.state == "auth_required"
    assert diagnostics.health.available is True
    assert diagnostics.app.status.state == "auth_required"
    assert diagnostics.registries_status.state == "auth_required"


def test_wud_api_configuration_diagnostics_reports_partial_endpoint_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        registries=(401, {"error": "authentication required"}),
    )

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config-partial.test:3000"),
        force=True,
    )

    assert diagnostics.app.status.state == "ready"
    assert diagnostics.watchers_status.state == "ready"
    assert diagnostics.registries_status.state == "auth_required"
    assert diagnostics.registries == []


def test_wud_api_configuration_diagnostics_rejects_malformed_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        app=(200, []),
        log=(200, []),
        store=(200, {"configuration": []}),
        watchers=(200, {"items": []}),
        registries=(200, {"items": []}),
    )

    diagnostics = web_wud_api.get_configuration_diagnostics(
        _settings(tmp_path, "https://wud.config-malformed.test:3000"),
        force=True,
    )

    assert diagnostics.app.status.state == "error"
    assert diagnostics.log.status.state == "error"
    assert diagnostics.store.status.state == "error"
    assert diagnostics.watchers_status.state == "error"
    assert diagnostics.registries_status.state == "error"


def test_wud_api_snapshot_reports_unreachable_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=OSError("connection refused"))

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.unreachable.test:3000"),
        include_containers=True,
        force=True,
    )

    assert snapshot.status.state == "unavailable"
    assert snapshot.status.available is False
    assert snapshot.status.metadata_available is False
    assert snapshot.containers == ()


def test_startup_probe_waits_for_wud_api_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_request_json(url: str, _client_config=None) -> object:
        calls.append(url)
        if len(calls) == 1:
            raise OSError("connection refused")
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_api, "_request_json", fake_request_json)
    monkeypatch.setattr(
        web_wud_api,
        "WUD_API_STARTUP_RETRY_INTERVAL_SECONDS",
        0.0,
    )
    settings = load_web_settings(
        environ=_web_env(
            tmp_path,
            {
                "WUD_API_BASE_URL": "https://wud.startup-wait.test:3000",
                "WUD_API_STARTUP_WAIT_SECONDS": "1",
            },
        ),
    )

    snapshot = web_wud_api.startup_probe(settings)

    assert snapshot.status.state == "ready"
    assert snapshot.status.available is True
    assert len(calls) == 2


@pytest.mark.parametrize("value", ["soon", "-1", "nan", "inf"])
def test_wud_api_startup_wait_rejects_invalid_values(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(WebConfigError):
        load_web_settings(
            environ=_web_env(
                tmp_path,
                {"WUD_API_STARTUP_WAIT_SECONDS": value},
            ),
        )


def test_pending_source_rejects_invalid_values(tmp_path: Path) -> None:
    with pytest.raises(WebConfigError) as exc_info:
        load_web_settings(
            environ=_web_env(
                tmp_path,
                {"WUD_PENDING_SOURCE": "queue"},
            ),
        )

    assert str(exc_info.value) == "WUD_PENDING_SOURCE must be one of: api, auto, file"


def test_pending_source_defaults_to_api(tmp_path: Path) -> None:
    settings = load_web_settings(environ=_web_env(tmp_path))

    assert settings.pending_source == "api"


def test_legacy_scripts_rejects_invalid_bool(tmp_path: Path) -> None:
    environ = _web_env(
        tmp_path,
        {"WUDUP_LEGACY_SCRIPTS": "treu"},
    )

    with pytest.raises(ConfigError, match="WUDUP_LEGACY_SCRIPTS"):
        load_web_settings(environ=environ)


def test_wud_api_snapshot_reports_auth_required_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=(401, {"error": "authentication required"}),
    )

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.auth.test:3000"),
        include_containers=True,
        force=True,
    )

    assert snapshot.status.state == "auth_required"
    assert snapshot.status.available is True
    assert snapshot.status.metadata_available is False
    assert snapshot.status.detail == "WUD API container metadata requires authentication"


def test_wud_api_snapshot_rejects_invalid_container_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=(200, {"items": []}))

    snapshot = web_wud_api.get_snapshot(
        _settings(tmp_path, "https://wud.invalid.test:3000"),
        include_containers=True,
        force=True,
    )

    assert snapshot.status.state == "error"
    assert snapshot.status.available is True
    assert snapshot.status.metadata_available is False
    assert snapshot.status.detail == "WUD API container metadata payload was not a list"


def test_wud_api_snapshot_reports_degraded_after_ready_cache_expires(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _ToggleableWudApi(monkeypatch, reachable=True)
    settings = _settings(tmp_path, "https://wud.cache-expiry.test:3000")

    ready = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert ready.status.state == "ready"
    assert ready.status.metadata_available is True

    api.reachable = False
    api.now = web_wud_api.WUD_API_CACHE_TTL_SECONDS / 2
    cached = web_wud_api.get_snapshot(settings, include_containers=True)
    assert cached.status.state == "ready"
    assert api.calls == ["/health", "/api/containers"]

    api.now = web_wud_api.WUD_API_CACHE_TTL_SECONDS + 0.1
    degraded = web_wud_api.get_snapshot(settings, include_containers=True)
    assert degraded.status.state == "unavailable"
    assert degraded.status.metadata_available is False
    assert degraded.containers == ()
    assert api.calls == ["/health", "/api/containers", "/health"]


def test_wud_api_degraded_snapshot_retries_after_short_interval_and_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _ToggleableWudApi(monkeypatch, reachable=False)
    settings = _settings(tmp_path, "https://wud.retry.test:3000")

    unavailable = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert unavailable.status.state == "unavailable"
    assert unavailable.status.metadata_available is False
    assert unavailable.containers == ()
    assert api.calls == ["/health"]

    api.reachable = True
    api.now = web_wud_api.WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS / 2
    cached = web_wud_api.get_snapshot(settings, include_containers=True)
    assert cached.status.state == "unavailable"
    assert cached.status.metadata_available is False
    assert cached.containers == ()
    assert api.calls == ["/health"]

    api.now = web_wud_api.WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS + 0.1
    recovered = web_wud_api.get_snapshot(settings, include_containers=True)
    assert recovered.status.state == "ready"
    assert recovered.status.metadata_available is True
    assert recovered.containers[0].name == "app"
    assert api.calls == ["/health", "/health", "/api/containers"]


def test_wud_api_partially_degraded_snapshot_retries_after_short_interval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = SimpleNamespace(now=0.0)
    containers = [_container_payload(name="app")]
    calls: list[str] = []

    def request_json(url: str, _client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(path)
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            return containers
        raise AssertionError(f"unexpected WUD API URL: {url}")

    monkeypatch.setattr(web_wud_api.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(web_wud_api, "_request_json", request_json)
    settings = _settings(tmp_path, "https://wud.partial-retry.test:3000")

    ready = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert ready.degraded_container_count == 0

    degraded_row = _container_payload(name="app", update_available=False)
    degraded_row["result"] = None
    degraded_row["error"] = {"message": "registry lookup failed"}
    containers[:] = [degraded_row]
    clock.now = 1.0

    degraded = web_wud_api.get_snapshot(
        settings,
        include_containers=True,
        force=True,
    )
    assert degraded.degraded_container_count == 1
    assert degraded.retained_update_count == 1

    containers[:] = [
        _container_payload(
            name="app",
            update_available=False,
            remote_tag="1.0.0",
            update_kind="unknown",
            remote_value="1.0.0",
        )
    ]
    clock.now = 1.0 + web_wud_api.WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS / 2

    cached = web_wud_api.get_snapshot(settings, include_containers=True)
    assert cached.degraded_container_count == 1
    assert calls == [
        "/health",
        "/api/containers",
        "/health",
        "/api/containers",
    ]

    clock.now = (
        1.0 + web_wud_api.WUD_API_DEGRADED_RETRY_INTERVAL_SECONDS + 0.1
    )
    recovered = web_wud_api.get_snapshot(settings, include_containers=True)

    assert recovered.containers == ()
    assert recovered.degraded_container_count == 0
    assert calls == [
        "/health",
        "/api/containers",
        "/health",
        "/api/containers",
        "/health",
        "/api/containers",
    ]


def test_web_startup_continues_when_wud_api_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=OSError("connection refused"))

    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.startup.test:3000",
        },
    )

    response = client.get("/api/v1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["wud_api"]["state"] == "unavailable"
    assert body["wud_api"]["available"] is False


def test_pending_endpoint_enriches_items_from_wud_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(
                    name="app",
                    platform={
                        "os": "linux",
                        "architecture": "arm64",
                        "variant": "v8",
                    },
                )
            ],
        ),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.pending.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "app\n"
    wud_file.write_text(original, encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    metadata = body["items"][0]["wud_metadata"]
    assert body["wud_api"]["metadata_available"] is True
    assert metadata["name"] == "app"
    assert metadata["remote_tag"] == "1.1.0"
    assert metadata["remote_digest"] == "sha256:remote"
    assert metadata["platform"] == "linux/arm64/v8"
    assert metadata["platform_os"] == "linux"
    assert metadata["platform_architecture"] == "arm64"
    assert metadata["platform_variant"] == "v8"
    assert body["grouping"]["unmatched"][0]["wud_metadata"] == metadata
    assert wud_file.read_text(encoding="utf-8") == original


def test_pending_endpoint_keeps_images_todo_fallback_when_wud_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, health=OSError("connection refused"))
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.fallback.test:3000",
            "WUD_PENDING_SOURCE": "auto",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("registry.example/acme/app:1.0.0\n", encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["image"] == "registry.example/acme/app:1.0.0"
    assert body["items"][0]["wud_metadata"] is None
    assert body["wud_api"]["metadata_available"] is False


def test_pending_endpoint_falls_back_after_wud_api_connection_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = _ToggleableWudApi(monkeypatch, reachable=True)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.pending-loss.test:3000",
            "WUD_PENDING_SOURCE": "auto",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "app\n"
    wud_file.write_text(original, encoding="utf-8")

    ready_response = client.get("/api/v1/pending")
    assert ready_response.status_code == 200
    ready_body = ready_response.json()
    assert ready_body["wud_api"]["metadata_available"] is True
    assert ready_body["items"][0]["wud_metadata"]["name"] == "app"

    api.reachable = False
    api.now = web_wud_api.WUD_API_CACHE_TTL_SECONDS + 0.1
    degraded_response = client.get("/api/v1/pending")

    assert degraded_response.status_code == 200
    degraded_body = degraded_response.json()
    assert degraded_body["count"] == 1
    assert degraded_body["items"][0]["image"] == "app"
    assert degraded_body["items"][0]["wud_metadata"] is None
    assert degraded_body["wud_api"]["state"] == "unavailable"
    assert wud_file.read_text(encoding="utf-8") == original


def test_release_notes_refresh_uses_wud_source_and_safe_remote_tag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_refresh_release_notes(
        _conn,
        targets,
        environ,
        *,
        source_resolver=None,
        target_tag_resolver=None,
        **_kwargs,
    ):
        contexts = release_note_contexts(
            targets,
            environ,
            source_resolver=source_resolver,
            target_tag_resolver=target_tag_resolver,
        )
        captured["contexts"] = contexts
        return [
            ReleaseNoteData(
                line_no=context.line_no,
                status="missing",
                provider=context.provider,
                image_repo=context.image_repo,
                upstream_repo=context.upstream_repo,
            )
            for context in contexts
        ]

    monkeypatch.setattr(
        release_notes_module,
        "refresh_release_notes",
        fake_refresh_release_notes,
    )
    _install_wud_api(
        monkeypatch,
        containers=(
            200,
            [
                _container_payload(
                    image="registry.example/acme/app",
                    tag="1.0.0",
                    remote_tag="1.1.0",
                ),
                _container_payload(
                    name="api",
                    image="registry.example/acme/api",
                    tag="2.0.0",
                    remote_tag="2.1.0",
                    source="https://github.com/acme/api",
                ),
            ],
        ),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.release-notes.test:3000",
            "WUD_PENDING_SOURCE": "file",
            "WUD_RELEASE_NOTES_ENABLED": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "\n".join(
            (
                "registry.example/acme/app:1.0.0",
                "registry.example/acme/api:2.0.0 tag=3.0.0",
                "",
            )
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notes/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    contexts = captured["contexts"]
    assert [context.provider for context in contexts] == ["github", "github"]
    assert [context.upstream_repo for context in contexts] == [
        "acme/app",
        "acme/api",
    ]
    assert [context.target_tag for context in contexts] == ["1.1.0", "3.0.0"]
    assert response.json()["wud_api"]["metadata_available"] is True
