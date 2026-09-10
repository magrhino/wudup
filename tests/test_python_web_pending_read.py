from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.web_test_helpers import (
    _assert_pending_grouping_did_not_mutate,
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _install_wud_api,
    _make_fake_stack,
    _write_fake_container_labels,
    _wud_api_container,
)
from tests.web_wud_rescan_helpers import install_recording_wud_api

from wudup import web_pending as pending_module
from wudup import web_wud_api
from wudup.config import ConfigError
from wudup.db import init_db, insert_snooze, open_db


def test_pending_endpoint_reads_wud_file_without_mutation(tmp_path: Path) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    original = "# ignored\nnginx:1.25 tag=1.26\nredis:latest@sha256:abc\n"
    wud_file.write_text(original, encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["count"] == 2
    assert body["items"][0]["image"] == "nginx:1.25"
    assert body["items"][0]["current_tag"] == "1.25"
    assert body["items"][0]["desired_tag"] == "1.26"
    assert body["items"][1]["current_tag"] == "latest"
    assert body["items"][1]["digest"] == "sha256:abc"
    assert body["grouping"]["status"] == "unavailable"
    assert body["grouping"]["groups"] == []
    assert [item["line_no"] for item in body["grouping"]["unmatched"]] == [2, 3]
    assert body["grouping"]["unmatched"][0]["action"] == "tag-update"
    assert body["grouping"]["unmatched"][1]["action"] == "recreate_stack"
    unavailable_provenance = body["grouping"]["unmatched"][1]["digest_provenance"]
    assert body["items"][1]["digest_provenance"] == unavailable_provenance
    assert unavailable_provenance == {
        "source_image": "redis:latest",
        "resolved_tag": "latest",
        "watch_tag": "latest",
        "target_digest": "sha256:abc",
        "final_image": "redis@sha256:abc",
        "provenance_source": "compose",
        "provenance_confidence": "recovered",
    }
    assert body["grouping"]["warnings"]
    assert wud_file.read_text(encoding="utf-8") == original


def test_pending_endpoint_fails_closed_when_completion_state_is_unreadable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redacted_value = "pending-completion-redaction-fixture"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_TOKEN": redacted_value,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/shared:latest\n", encoding="utf-8")

    def fail_load(*_args, **_kwargs):
        raise OSError(
            f"read failed for {tmp_path / 'state' / 'wud.sqlite'} "
            f"with {redacted_value}"
        )

    monkeypatch.setattr(
        pending_module.web_file_selection_store,
        "load_completed_update_selections",
        fail_load,
    )
    response = client.get("/api/v1/pending")
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not read pending source: ")
    assert redacted_value not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail


def test_pending_shared_line_has_stable_stack_scoped_selection_ids(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/shared:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/shared:latest", "cid-active")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "backup",
        [("app", "repo/shared:latest", "cid-backup")],
    )

    first = client.get("/api/v1/pending")
    second = client.get("/api/v1/pending")

    assert first.status_code == 200
    assert second.status_code == 200
    first_items = {
        group["name"]: group["items"][0]
        for group in first.json()["grouping"]["groups"]
    }
    second_ids = {
        group["name"]: group["items"][0]["selection_id"]
        for group in second.json()["grouping"]["groups"]
    }
    assert set(first_items) == {"active", "backup"}
    assert {item["line_no"] for item in first_items.values()} == {1}
    assert all(
        item["selection_id"].startswith("sel-v1-")
        for item in first_items.values()
    )
    assert len({item["selection_id"] for item in first_items.values()}) == 2
    assert {
        name: item["selection_id"] for name, item in first_items.items()
    } == second_ids


def test_pending_endpoint_reads_wud_api_source_without_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    remote_digest = f"sha256:{'b' * 64}"
    remote_platform = "linux/arm64/v8"
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                remote_digest=remote_digest,
                platform=remote_platform,
            )
        ],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.api-source.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:1.0", "cid-app")],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["configured"] == "api"
    assert body["source"]["active"] == "api"
    assert body["source"]["label"] == "WUD API"
    assert body["exists"] is True
    assert body["source_file"] == "WUD API"
    assert body["count"] == 1
    assert body["items"][0]["line_no"] == 1
    assert body["items"][0]["raw"] == (
        f"repo/app:1.0 tag=2.0 platform={remote_platform} "
        f"sha256={remote_digest}"
    )
    assert body["items"][0]["digest"] == remote_digest
    assert body["items"][0]["source"] == "api"
    assert body["items"][0]["source_id"] == "docker.local.app"
    assert body["items"][0]["wud_metadata"]["remote_tag"] == "2.0"
    assert body["grouping"]["status"] == "ready"
    assert body["grouping"]["groups"][0]["items"][0]["source"] == "api"
    assert body["grouping"]["groups"][0]["items"][0]["source_id"] == "docker.local.app"
    assert not (tmp_path / "state" / "images.todo").exists()
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_bypasses_wud_snapshot_cache_with_gets_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, _fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(name=f"app-{index}", image=f"repo/app-{index}")
        for index in range(7)
    ]
    calls = install_recording_wud_api(monkeypatch, containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.pending-refresh.test:3000",
            **fake_env,
        },
    )
    calls.clear()

    cached = client.get("/api/v1/status")

    assert cached.status_code == 200
    assert cached.json()["pending_count"] == 7
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]

    containers.extend(
        _wud_api_container(name=f"app-{index}", image=f"repo/app-{index}")
        for index in range(7, 17)
    )
    calls.clear()

    refreshed = client.get("/api/v1/pending")

    assert refreshed.status_code == 200
    assert refreshed.json()["count"] == 17
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]

    calls.clear()

    status = client.get("/api/v1/status")

    assert status.status_code == 200
    assert status.json()["pending_count"] == 17
    assert calls == []


def test_pending_endpoint_preserves_updates_across_degraded_wud_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, _fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(name=f"app-{index}", image=f"repo/app-{index}")
        for index in range(17)
    ]
    calls = install_recording_wud_api(monkeypatch, containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.pending-degraded.test:3000",
            **fake_env,
        },
    )
    calls.clear()

    initial = client.get("/api/v1/pending")

    assert initial.status_code == 200
    assert initial.json()["count"] == 17
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]

    degraded_rows = []
    for index in range(8, 17):
        row = _wud_api_container(
            name=f"app-{index}",
            image=f"repo/app-{index}",
            update_available=False,
            update_kind="unknown",
        )
        row["result"] = None if index % 2 == 0 else "malformed"
        row["error"] = (
            {"message": "registry lookup failed"} if index % 2 == 0 else {}
        )
        degraded_rows.append(row)
    containers[:] = [
        _wud_api_container(name=f"app-{index}", image=f"repo/app-{index}")
        for index in range(8)
    ] + degraded_rows
    calls.clear()

    degraded = client.get("/api/v1/pending")

    assert degraded.status_code == 200
    degraded_body = degraded.json()
    assert degraded_body["count"] == 17
    assert degraded_body["source"]["fresh"] is False
    assert degraded_body["source"]["degraded"] is True
    assert degraded_body["source"]["detail"] == (
        "17 updates are available. "
        "The last WUD update check failed for 9 containers. "
        "9 updates use results from the last successful WUD check."
    )
    assert degraded_body["warnings"] == [degraded_body["source"]["detail"]]
    retained = [
        item
        for item in degraded_body["items"]
        if int(item["wud_metadata"]["name"].removeprefix("app-")) >= 8
    ]
    assert len(retained) == 9
    assert all(item["wud_metadata"]["error"] for item in retained)
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]
    assert all(method == "GET" for method, _path in calls)

    calls.clear()

    repeated_degraded = client.get("/api/v1/pending")

    assert repeated_degraded.status_code == 200
    repeated_degraded_body = repeated_degraded.json()
    assert repeated_degraded_body["count"] == 17
    assert repeated_degraded_body["source"]["degraded"] is True
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]
    assert all(method == "GET" for method, _path in calls)

    containers[:] = [
        _wud_api_container(name=f"app-{index}", image=f"repo/app-{index}")
        for index in range(8)
    ] + [
        _wud_api_container(
            name=f"app-{index}",
            image=f"repo/app-{index}",
            remote_tag="1.0",
            update_kind="unknown",
            update_available=False,
            remote_value="1.0",
        )
        for index in range(8, 17)
    ]
    calls.clear()

    authoritative = client.get("/api/v1/pending")

    assert authoritative.status_code == 200
    authoritative_body = authoritative.json()
    assert authoritative_body["count"] == 8
    assert authoritative_body["source"]["fresh"] is True
    assert authoritative_body["source"]["degraded"] is False
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]
    assert all(method == "GET" for method, _path in calls)


def test_pending_endpoint_stays_fresh_with_unrelated_unsupported_registry_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, _fake_root = _fake_docker_env(tmp_path)
    containers = [_wud_api_container(name="app", image="repo/app")]
    unsupported = _wud_api_container(
        name="socket-proxy",
        image="linuxserver/socket-proxy",
        update_available=False,
        update_kind="unknown",
    )
    unsupported["result"] = None
    unsupported["error"] = {"message": "Unsupported Registry unknown"}
    containers.append(unsupported)
    calls = install_recording_wud_api(monkeypatch, containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.pending-unsupported.test:3000",
            **fake_env,
        },
    )
    calls.clear()

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["source"]["fresh"] is True
    assert body["source"]["degraded"] is False
    assert body["warnings"] == []
    assert body["wud_api"]["detail"] == (
        "1 update is available. "
        "WUD skipped 1 container because its registry is unsupported."
    )
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]


def test_pending_endpoint_clears_update_missing_from_authoritative_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, _fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(name="app", image="repo/app"),
        _wud_api_container(name="worker", image="repo/worker"),
    ]
    calls = install_recording_wud_api(monkeypatch, containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.pending-disappearance.test:3000",
            **fake_env,
        },
    )

    initial = client.get("/api/v1/pending")

    assert initial.status_code == 200
    assert initial.json()["count"] == 2

    containers[:] = [_wud_api_container(name="app", image="repo/app")]
    calls.clear()

    authoritative = client.get("/api/v1/pending")

    assert authoritative.status_code == 200
    body = authoritative.json()
    assert body["count"] == 1
    assert body["items"][0]["source_id"] == "docker.local.app"
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]
    assert all(method == "GET" for method, _path in calls)


def test_pending_endpoint_does_not_transfer_retained_update_to_changed_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, _fake_root = _fake_docker_env(tmp_path)
    containers = [_wud_api_container(name="app", image="repo/app")]
    initial_image = containers[0]["image"]
    assert isinstance(initial_image, dict)
    initial_image["id"] = "sha256:original"
    calls = install_recording_wud_api(monkeypatch, containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.pending-identity.test:3000",
            **fake_env,
        },
    )

    initial = client.get("/api/v1/pending")
    assert initial.status_code == 200
    assert initial.json()["count"] == 1

    changed = _wud_api_container(
        name="app",
        image="repo/app",
        update_available=False,
        update_kind="unknown",
    )
    changed_image = changed["image"]
    assert isinstance(changed_image, dict)
    changed_image["id"] = "sha256:replacement"
    changed["result"] = None
    changed["error"] = {"message": "registry lookup failed"}
    containers[:] = [changed]
    calls.clear()

    degraded = client.get("/api/v1/pending")

    assert degraded.status_code == 200
    degraded_body = degraded.json()
    assert degraded_body["count"] == 0
    assert degraded_body["source"]["degraded"] is True
    assert calls == [("GET", "/health"), ("GET", "/api/containers")]
    assert all(method == "GET" for method, _path in calls)


def test_pending_endpoint_returns_hidden_wud_api_snoozed_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                name="app",
                update_available=False,
                update_kind="tag",
                local_value="1.0",
                remote_value="2.0",
            ),
            _wud_api_container(
                name="db",
                image="repo/db",
                update_available=False,
                update_kind="unknown",
                local_value="1.0",
                remote_value="2.0",
            ),
            _wud_api_container(
                name="worker",
                image="repo/worker",
                update_available=False,
                update_kind="tag",
                local_value="1.0",
                remote_value="2.0",
            ),
        ],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:1.0", "cid-app"),
            ("db", "repo/db:1.0", "cid-db"),
            ("worker", "repo/worker:1.0", "cid-worker"),
        ],
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "wud.sqlite"
    future = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    ).isoformat()
    with open_db(db_path) as conn:
        init_db(conn)
        insert_snooze(
            conn,
            service_key="stack/app",
            snoozed_until=future,
            reason="maintenance window",
        )
        insert_snooze(
            conn,
            service_key="stack/db",
            snoozed_until=future,
            reason="unknown kind should not surface",
        )

    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.hidden-candidates.test:3000",
            **fake_env,
        },
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["active"] == "api"
    assert body["count"] == 0
    assert body["items"] == []
    assert body["source_hash"] == hashlib.sha256(b"").hexdigest()
    assert body["grouping"]["status"] == "ready"
    assert body["grouping"]["groups"] == []
    assert body["grouping"]["unmatched"] == []
    assert [item["service_key"] for item in body["snoozed_candidates"]] == [
        "stack/app"
    ]
    candidate = body["snoozed_candidates"][0]
    assert candidate["image"] == "repo/app:1.0"
    assert candidate["target_image"] == "repo/app:2.0"
    assert candidate["source_id"] == "docker.local.app"
    assert candidate["snooze_kind"] == "time"
    assert candidate["reason"] == "maintenance window"
    assert candidate["wud_metadata"]["update_kind"] == "tag"
    assert candidate["wud_metadata"]["remote_tag"] == "2.0"
    assert "line_no" not in candidate

    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    )
    cleanup = client.post(
        "/api/v1/pending/cleanup",
        json={
            "cleanup_id": "candidate",
            "lines": [{"line_no": 1, "raw": candidate["image"]}],
            "confirmation": "remove_unmatched",
        },
        headers=headers,
    )
    removal = client.post(
        "/api/v1/pending/removal-plan",
        json={"line_numbers": [1]},
        headers=headers,
    )

    assert plan.status_code == 422
    assert plan.json()["detail"] == (
        "line_numbers must reference actionable WUD target lines: 1"
    )
    assert cleanup.status_code == 409
    assert cleanup.json()["detail"] == (
        "pending cleanup only supports WUD_OUT_FILE source"
    )
    assert removal.status_code == 409
    assert removal.json()["detail"] == (
        "pending removal only supports WUD_OUT_FILE source"
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_legacy_disabled_forces_api_pending_source_without_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=[_wud_api_container(name="app")])

    def fail_file_read(_path: Path):
        raise AssertionError("legacy-disabled WebUI should not read images.todo")

    monkeypatch.setattr(
        pending_module.web_pending_sources,
        "_read_pending_file",
        fail_file_read,
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUDUP_LEGACY_SCRIPTS": "FALSE",
            "WUD_PENDING_SOURCE": "file",
        },
    )

    response = client.get("/api/v1/pending")
    body = response.json()

    assert response.status_code == 200
    assert body["source"]["configured"] == "api"
    assert body["source"]["active"] == "api"
    assert body["count"] == 1


def test_pending_endpoint_preserves_tag_for_wud_api_digest_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    remote_digest = f"sha256:{'a' * 64}"
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                tag="latest",
                remote_tag="",
                remote_digest=remote_digest,
                update_kind="digest",
            )
        ],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.api-digest-source.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/app:stable", "cid-worker"),
        ],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["raw"] == f"repo/app:latest@{remote_digest}"
    assert body["items"][0]["digest"] == remote_digest
    assert body["grouping"]["status"] == "ready"
    assert body["grouping"]["groups"][0]["items"][0]["services"] == ["app"]
    calls = _fake_docker_calls(fake_root)
    assert "worker" not in calls
    _assert_pending_grouping_did_not_mutate(calls)


def test_pending_metadata_endpoint_refreshes_file_source_without_wud_watch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = install_recording_wud_api(
        monkeypatch,
        [_wud_api_container(image="repo/app", tag="latest", remote_tag="2.0")],
    )

    def fail_watch_all(_settings):
        raise AssertionError("watch_all called")

    def fail_watch_containers(_settings, _ids):
        raise AssertionError("watch_containers called")

    monkeypatch.setattr(web_wud_api, "watch_all", fail_watch_all)
    monkeypatch.setattr(web_wud_api, "watch_containers", fail_watch_containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.metadata-file.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    calls.clear()

    response = client.post(
        "/api/v1/pending/metadata",
        json={
            "source_hash": pending_body["source_hash"],
            "lines": [
                {
                    "line_no": item["line_no"],
                    "raw": item["raw"],
                    "source_id": item["source_id"],
                }
                for item in pending_body["items"]
            ],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["requires_pending_reload"] is False
    assert body["source_hash"] == pending_body["source_hash"]
    assert len(body["items"]) == 1
    assert body["items"][0]["line_no"] == 1
    assert body["items"][0]["raw"] == "repo/app:latest"
    assert body["items"][0]["source_id"] == "file:1"
    assert body["items"][0]["wud_metadata"]["remote_tag"] == "2.0"
    assert [method for method, _path in calls if method == "POST"] == []


def test_pending_metadata_endpoint_reports_changed_source_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=[])
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.metadata-stale.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    wud_file.write_text("repo/other:latest\n", encoding="utf-8")

    response = client.post(
        "/api/v1/pending/metadata",
        json={
            "source_hash": pending_body["source_hash"],
            "lines": [
                {
                    "line_no": 1,
                    "raw": "repo/app:latest",
                    "source_id": "file:1",
                }
            ],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stale"
    assert body["requires_pending_reload"] is True
    assert body["source_hash"] != pending_body["source_hash"]
    assert body["items"] == []


def test_pending_metadata_endpoint_reports_stale_line_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=[])
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.metadata-line-stale.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()

    for stale_line in (
        {"line_no": 2, "raw": "repo/app:latest", "source_id": "file:1"},
        {"line_no": 1, "raw": "repo/changed:latest", "source_id": "file:1"},
        {"line_no": 1, "raw": "repo/app:latest", "source_id": "file:2"},
    ):
        response = client.post(
            "/api/v1/pending/metadata",
            json={
                "source_hash": pending_body["source_hash"],
                "lines": [stale_line],
            },
            headers=_csrf_headers(client),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "stale"
        assert body["requires_pending_reload"] is True
        assert body["items"] == []


def test_pending_metadata_endpoint_clears_unavailable_wud_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(monkeypatch, containers=[], health_error=OSError("offline"))
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_API_BASE_URL": "https://wud.metadata-unavailable.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()

    response = client.post(
        "/api/v1/pending/metadata",
        json={
            "source_hash": pending_body["source_hash"],
            "lines": [
                {
                    "line_no": 1,
                    "raw": "repo/app:latest",
                    "source_id": "file:1",
                }
            ],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["requires_pending_reload"] is False
    assert body["wud_api"]["metadata_available"] is False
    assert body["items"][0]["wud_metadata"] is None


def test_pending_metadata_endpoint_refreshes_api_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[_wud_api_container(image="repo/app", tag="1.0", remote_tag="2.0")],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.metadata-api.test:3000",
        },
    )
    pending_body = client.get("/api/v1/pending").json()

    response = client.post(
        "/api/v1/pending/metadata",
        json={
            "source_hash": pending_body["source_hash"],
            "lines": [
                {
                    "line_no": item["line_no"],
                    "raw": item["raw"],
                    "source_id": item["source_id"],
                }
                for item in pending_body["items"]
            ],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["requires_pending_reload"] is False
    assert body["source"] == pending_body["source"]
    assert body["items"][0]["source_id"] == "docker.local.app"
    assert body["items"][0]["wud_metadata"]["remote_tag"] == "2.0"


def test_pending_endpoint_auto_falls_back_to_wud_file_when_api_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[],
        health_error=OSError("connection refused"),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "auto",
            "WUD_API_BASE_URL": "https://wud.unavailable-source.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/file:latest\n", encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["configured"] == "auto"
    assert body["source"]["active"] == "file"
    assert body["source"]["degraded"] is True
    assert body["source"]["fresh"] is False
    assert "connection refused" in body["source"]["fallback_reason"]
    assert body["count"] == 1
    assert body["items"][0]["raw"] == "repo/file:latest"
    assert body["items"][0]["source"] == "file"
    assert body["warnings"][0].startswith("WUD API pending source degraded")


def test_pending_endpoint_api_mode_does_not_fallback_to_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[],
        health_error=OSError("connection refused"),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.api-unavailable.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/file:latest\n", encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["configured"] == "api"
    assert body["source"]["active"] == "api"
    assert body["source"]["degraded"] is True
    assert body["count"] == 0
    assert body["items"] == []
    assert body["source_file"] == "WUD API"
    assert wud_file.read_text(encoding="utf-8") == "repo/file:latest\n"


def test_pending_endpoint_wraps_effective_config_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "pending-secret-token"

    def invalid_config(_settings):
        raise ConfigError(
            f"failed to parse {tmp_path / 'state' / 'config.env'} with {secret}"
        )

    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_TOKEN": secret,
        },
    )
    monkeypatch.setattr(pending_module, "_effective_config_loader", invalid_config)
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")

    response = client.get("/api/v1/pending")
    detail = response.json()["detail"]

    assert response.status_code == 400
    assert detail.startswith("could not read effective config: ")
    assert secret not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail


def test_pending_endpoint_sanitizes_wud_file_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "pending-read-secret"

    def failed_read(_path):
        raise OSError(
            f"open failed for {tmp_path / 'state' / 'images.todo'} with {secret}"
        )

    monkeypatch.setattr(pending_module.web_pending_sources, "_read_pending_file", failed_read)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_TOKEN": secret,
        },
    )

    response = client.get("/api/v1/pending")
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not read pending source: ")
    assert secret not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail


def test_pending_endpoint_groups_items_by_compose_stack_without_mutation(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\nrepo/db:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("db", "repo/db:latest", "cid-db"),
        ],
    )
    compose_before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    assert [item["line_no"] for item in body["items"]] == [1, 2]
    grouping = body["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["warnings"] == []
    assert grouping["unmatched"] == []
    assert len(grouping["groups"]) == 1
    group = grouping["groups"][0]
    assert group["name"] == "stack"
    assert group["compose_file"] == "docker-compose.yml"
    assert group["directory"] == str(compose_dir)
    assert group["project_directory"] == ""
    assert group["project_name"] == "stack"
    assert group["services"] == ["app", "db"]
    assert group["services_label"] == "app, db"
    assert group["line_numbers"] == [1, 2]
    assert [item["line_no"] for item in group["items"]] == [1, 2]
    assert group["items"][0]["services"] == ["app"]
    assert group["items"][0]["compose_images"] == ["repo/app:latest"]
    assert group["items"][0]["resolved_image"] == "repo/app:latest"
    assert group["items"][0]["target_image"] == "repo/app:latest"
    assert group["items"][0]["action"] == "recreate_service"
    assert group["items"][0]["runtime_state"] == "running"
    assert group["items"][0]["running_services"] == ["app"]
    assert group["items"][0]["stopped_services"] == []
    assert wud_file.read_text(encoding="utf-8") == original
    assert (
        (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
        == compose_before
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_reports_stopped_service_runtime_state(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\nrepo/worker:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/worker:latest", None),
        ],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    items = response.json()["grouping"]["groups"][0]["items"]
    by_service = {item["services"][0]: item for item in items}
    assert by_service["app"]["runtime_state"] == "running"
    assert by_service["app"]["running_services"] == ["app"]
    assert by_service["worker"]["runtime_state"] == "not-running"
    assert by_service["worker"]["running_services"] == []
    assert by_service["worker"]["stopped_services"] == ["worker"]
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_reports_unknown_for_mixed_scaled_service(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-running")],
    )
    runtime_row = (
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'}\t"
        "stack\tapp\tFalse\t"
    )
    (fake_root / "compose-runtime-all.tsv").write_text(
        f"{runtime_row}running\n{runtime_row}exited\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["running_services"] == []
    assert item["stopped_services"] == []
    assert "ps --all --format" in _fake_docker_calls(fake_root)
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


@pytest.mark.parametrize("state", ["running", "exited"])
def test_pending_endpoint_reports_unknown_for_homogeneous_scaled_service(
    tmp_path: Path,
    state: str,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-running")],
    )
    runtime_row = (
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'}\t"
        f"stack\tapp\tFalse\t{state}\n"
    )
    (fake_root / "compose-runtime-all.tsv").write_text(
        runtime_row * 2,
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["running_services"] == []
    assert item["stopped_services"] == []
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_includes_stopped_network_provider_in_runtime_state(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("provider", "repo/provider:latest", None),
            ("app", "repo/app:latest", "cid-app"),
        ],
    )
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  provider:\n"
        "    image: repo/provider:latest\n"
        "  app:\n"
        "    image: repo/app:latest\n"
        "    network_mode: service:provider\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["services"] == ["app"]
    assert item["runtime_state"] == "mixed"
    assert item["running_services"] == ["app"]
    assert item["stopped_services"] == ["provider"]
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_matches_running_multi_file_compose_service(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    (fake_root / "compose-runtime.tsv").write_text(
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'},"
        f"{compose_dir / 'compose.override.yml'}\tstack\tapp\tFalse\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["runtime_state"] == "running"
    assert item["running_services"] == ["app"]
    assert item["stopped_services"] == []
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_fails_safe_when_runtime_state_is_unavailable(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    (fake_root / "ps_fail").write_text("", encoding="utf-8")
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["running_services"] == []
    assert item["stopped_services"] == []


def test_pending_endpoint_fails_safe_when_runtime_state_times_out(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    (fake_root / "ps_hang").write_text("", encoding="utf-8")
    monkeypatch.setattr(pending_module, "_PENDING_DOCKER_TIMEOUT_SECONDS", 0.01)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    item = response.json()["grouping"]["groups"][0]["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["running_services"] == []
    assert item["stopped_services"] == []


def test_pending_endpoint_reports_unknown_without_compose_project_identity(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    (fake_root / "stacks" / "stack" / "config_json_fail").write_text(
        "",
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    group = response.json()["grouping"]["groups"][0]
    assert group["project_name"] == ""
    item = group["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["running_services"] == []
    assert item["stopped_services"] == []
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_recovers_digest_pin_provenance_from_compose(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest@sha256:child\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:old", "cid-app")],
    )
    (compose_dir / "docker-compose.yml").write_text(
        "\n".join(
            [
                "services:",
                "  app:",
                "    # wudup.resolved-tag=latest",
                "    image: repo/app@sha256:old",
                "    labels:",
                "      - wud.tag.include=^latest$$",
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    item_provenance = body["items"][0]["digest_provenance"]
    group_provenance = body["grouping"]["groups"][0]["items"][0][
        "digest_provenance"
    ]
    assert item_provenance == group_provenance
    assert item_provenance["source_image"] == "repo/app:latest"
    assert item_provenance["resolved_tag"] == "latest"
    assert item_provenance["watch_tag"] == "latest"
    assert item_provenance["target_digest"] == "sha256:child"
    assert item_provenance["final_image"] == "repo/app@sha256:child"
    assert item_provenance["provenance_source"] == "compose"
    assert item_provenance["provenance_confidence"] == "recovered"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_keeps_digest_pin_provenance_for_unmatched_item(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/other:latest@sha256:child\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    body = response.json()
    grouping = body["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["groups"] == []
    assert len(grouping["unmatched"]) == 1
    unmatched_provenance = grouping["unmatched"][0]["digest_provenance"]
    assert body["items"][0]["digest_provenance"] == unmatched_provenance
    assert unmatched_provenance == {
        "source_image": "repo/other:latest",
        "resolved_tag": "latest",
        "watch_tag": "latest",
        "target_digest": "sha256:child",
        "final_image": "repo/other@sha256:child",
        "provenance_source": "compose",
        "provenance_confidence": "recovered",
    }
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_marks_recreate_stack_label_action(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("db", "repo/db:latest", None),
        ],
    )
    _write_fake_container_labels(
        fake_root,
        "cid-app",
        {"WUD-UPDATER-RECREATE-STACK": "true"},
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    group = response.json()["grouping"]["groups"][0]
    assert group["items"][0]["services"] == ["app"]
    assert group["items"][0]["action"] == "recreate_stack"
    assert group["items"][0]["runtime_state"] == "mixed"
    assert group["items"][0]["running_services"] == ["app"]
    assert group["items"][0]["stopped_services"] == ["db"]
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_honors_env_compose_ignore_paths(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_COMPOSE_IGNORE_PATHS": "old",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/ignored:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/app:latest", "cid-app")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "ignored",
        [("app", "repo/ignored:latest", "cid-ignored")],
        parent=tmp_path / "docker" / "old",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    grouping = response.json()["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["groups"] == []
    assert [item["line_no"] for item in grouping["unmatched"]] == [1]


def test_pending_endpoint_honors_webui_managed_compose_ignore_paths(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    headers = _csrf_headers(client)
    settings_update = client.post(
        "/api/v1/settings/managed",
        json={"values": {"compose_ignore_paths": "old"}},
        headers=headers,
    )
    assert settings_update.status_code == 200
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/ignored:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/app:latest", "cid-app")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "ignored",
        [("app", "repo/ignored:latest", "cid-ignored")],
        parent=tmp_path / "docker" / "old",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    grouping = response.json()["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["groups"] == []
    assert [item["line_no"] for item in grouping["unmatched"]] == [1]


def test_pending_endpoint_allows_empty_webui_managed_compose_ignore_paths(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    headers = _csrf_headers(client)
    settings_update = client.post(
        "/api/v1/settings/managed",
        json={"values": {"compose_ignore_paths": ""}},
        headers=headers,
    )
    assert settings_update.status_code == 200
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/archived:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "archived",
        [("app", "repo/archived:latest", "cid-archived")],
        parent=tmp_path / "docker" / "old",
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    grouping = response.json()["grouping"]
    assert grouping["status"] == "ready"
    assert len(grouping["groups"]) == 1
    assert grouping["groups"][0]["items"][0]["line_no"] == 1


def test_pending_endpoint_reports_unmatched_grouping_items(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/other:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    grouping = response.json()["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["groups"] == []
    assert len(grouping["unmatched"]) == 1
    assert grouping["unmatched"][0]["line_no"] == 1
    assert grouping["unmatched"][0]["image"] == "repo/other:latest"
    assert grouping["unmatched"][0]["action"] == "unmatched"
    assert grouping["unmatched"][0]["services"] == []
    assert grouping["unmatched"][0]["compose_images"] == []
    diagnostic = grouping["unmatched"][0]["diagnostic"]
    assert diagnostic["code"] == "unmatched"
    assert (
        diagnostic["message"]
        == "This pending update no longer matches any discovered Compose service."
    )
    assert "service removal" in diagnostic["hint"]
    assert "image rename" in diagnostic["hint"]
    assert "No running Docker container matched this pending line." in diagnostic[
        "details"
    ]["preflight_findings"]
    assert "The Compose service was removed or renamed." in diagnostic["details"][
        "possible_reasons"
    ]
    assert "Remove the stale WUD line" in " ".join(
        diagnostic["details"]["recommended_actions"]
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_pending_endpoint_diagnoses_archived_compose_label_stale_entry(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("homarr-labs/homarr:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/app:latest", "cid-app")],
    )
    archived = tmp_path / "docker" / "homarr" / "docker-compose.archive.yml"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "services:\n  homarr:\n    image: ghcr.io/homarr-labs/homarr:latest\n",
        encoding="utf-8",
    )
    with (fake_root / "containers.tsv").open("a", encoding="utf-8") as file:
        file.write("homarr\tghcr.io/homarr-labs/homarr:latest\n")
    _write_fake_container_labels(
        fake_root,
        "homarr",
        {
            "com.docker.compose.project": "homarr",
            "com.docker.compose.project.working_dir": str(tmp_path / "docker" / "homarr"),
            "com.docker.compose.project.config_files": str(
                tmp_path / "docker" / "homarr" / "docker-compose.yml"
            ),
            "com.docker.compose.service": "homarr",
        },
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    diagnostic = response.json()["grouping"]["unmatched"][0]["diagnostic"]
    assert diagnostic["code"] == "compose-label-active-file-missing"
    assert diagnostic["stack"] == "homarr"
    assert diagnostic["service"] == "homarr"
    assert "homarr/docker-compose.yml" in diagnostic["message"]
    assert "homarr/docker-compose.archive.yml" in diagnostic["message"]
    assert str(tmp_path) not in diagnostic["message"]
    assert diagnostic["found_files"] == ["homarr/docker-compose.archive.yml"]
    assert "Running container homarr still matches this pending line." in diagnostic[
        "details"
    ]["preflight_findings"]
    assert (
        "The active Compose file was renamed to an archived or nonstandard filename."
        in diagnostic["details"]["possible_reasons"]
    )
    assert "Update Docker base or ignore paths if the stack moved." in diagnostic[
        "details"
    ]["recommended_actions"]


def test_pending_endpoint_diagnoses_undiscovered_active_compose_label_entry(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_COMPOSE_IGNORE_PATHS": "old",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("homarr-labs/homarr:latest\n", encoding="utf-8")
    ignored_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "homarr",
        [("homarr", "ghcr.io/homarr-labs/homarr:latest", "cid-homarr")],
        parent=tmp_path / "docker" / "old",
    )
    active_file = ignored_dir / "docker-compose.yml"
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/app:latest", "cid-app")],
    )
    with (fake_root / "containers.tsv").open("a", encoding="utf-8") as file:
        file.write("homarr\tghcr.io/homarr-labs/homarr:latest\n")
    _write_fake_container_labels(
        fake_root,
        "homarr",
        {
            "com.docker.compose.project": "homarr",
            "com.docker.compose.project.working_dir": str(ignored_dir),
            "com.docker.compose.project.config_files": str(active_file),
            "com.docker.compose.service": "homarr",
        },
    )

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    diagnostic = response.json()["grouping"]["unmatched"][0]["diagnostic"]
    assert diagnostic["code"] == "compose-label-undiscovered-active-file"
    assert "old/homarr/docker-compose.yml" in diagnostic["message"]
    assert str(tmp_path) not in diagnostic["message"]
    assert "Compose discovery did not include that stack." in diagnostic["details"][
        "preflight_findings"
    ]
    assert "The stack is excluded by Compose ignore paths." in diagnostic["details"][
        "possible_reasons"
    ]
    assert "Update Docker base or ignore paths so discovery includes the stack." in (
        diagnostic["details"]["recommended_actions"]
    )


def test_pending_endpoint_groups_tag_updates_without_allowing_tag_updates(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:1.0 tag=2.0\n"
    wud_file.write_text(original, encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:1.0", "cid-app")],
    )
    compose_before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")

    response = client.get("/api/v1/pending")

    assert response.status_code == 200
    grouping = response.json()["grouping"]
    assert grouping["status"] == "ready"
    assert grouping["unmatched"] == []
    item = grouping["groups"][0]["items"][0]
    assert item["line_no"] == 1
    assert item["action"] == "tag-update"
    assert item["desired_tag"] == "2.0"
    assert item["resolved_image"] == "repo/app:1.0"
    assert item["target_image"] == "repo/app:2.0"
    assert item["compose_images"] == ["repo/app:1.0"]
    assert item["services"] == ["app"]
    assert wud_file.read_text(encoding="utf-8") == original
    assert (
        (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
        == compose_before
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))
